"""축 채점 변이 시험 — #462. 공허성 방어(§12 4항): 지표가 입력 변이에 안 움직이면 그 지표는
아무것도 재는 것이 아니다. 전부 가짜라 API/pg 콜이 0이다."""

from __future__ import annotations

from app.agents.profile.graph_models import make_node_id
from app.agents.profile.store import reset_profile_store
from app.core.config import get_settings
from evals.taste_probe.baseline import score_baseline
from evals.taste_probe.fakes import NoiseFalsePositiveLLM, ScriptedDeltaLLM, build_fake_catalog
from evals.taste_probe.loader import load_golden_set
from evals.taste_probe.metrics import build_confusion, match_sample, score_all
from evals.taste_probe.runner import ProducedTriple, run_probe, unfilled_sessions
from evals.taste_probe.schema import ExpectedTriple
from evals.taste_probe.seams import fake_catalog_seam

GOLDEN = load_golden_set("default")
CATALOG = build_fake_catalog(GOLDEN)
N = 2
_BRAND_KIND_TRIPLE_COUNT = sum(
    1
    for session in GOLDEN.sessions
    for triple in session.expected_triples
    if triple.kind == "brand"
)


async def _no_sleep(seconds: float) -> None:
    return None


async def _run(llm) -> list:
    # 한 시험 안에서 `_run` 을 여러 번 부르는 경우가 있다(비교 시험) — 표본 키(user_id)는 세션당
    # 시도 번호로만 갈리므로, 두 번째 `_run` 이 리셋 없이 첫 번째 `_run` 과 같은 user_id 를 다시
    # 쓰면 첫 런의 fact 가 그대로 남아 있어 두 번째 런이 "이전 런의 잔여 fact 를 관측"하게 된다.
    # CLI 는 런당 1회만 리셋하면 되지만(런이 프로세스당 1회), 테스트는 여러 런을 한 프로세스
    # 안에서 잇달아 돌리므로 런 경계마다 리셋해 그 전제를 지킨다.
    reset_profile_store()
    with fake_catalog_seam(CATALOG):
        return await run_probe(
            llm=llm,
            golden_set=GOLDEN,
            n=N,
            attempt_multiplier=3,
            concurrency=4,
            settings=get_settings(),
            sleep=_no_sleep,
        )


async def test_all_empty_llm_matches_trivial_baseline_exactly() -> None:
    """ "아무것도 안 내는" 가짜 → recall 분자 0 이고 trivial baseline 과 정확히 같은 수치."""
    llm = ScriptedDeltaLLM(GOLDEN, omit_every=1)  # 매 시도 모든 기대 트리플을 생략
    results = await _run(llm)
    axes = score_all(results, GOLDEN, n=N)
    baseline = score_baseline(GOLDEN, n=N)

    assert axes["recall"].numerator == 0
    assert axes["recall"].denominator == baseline["baselineRecall"]["denominator"] * N
    # baseline 은 결정론 1회 정의라 분모가 세션 기준(×1) — 파이프라인은 ×N. 분자는 둘 다 0 으로 동일.
    assert baseline["baselineRecall"]["numerator"] == 0
    assert axes["falsePositiveRate"].numerator == 0  # 산출 자체가 없다
    assert axes["noiseFalsePositiveRate"].numerator == 0


async def test_all_correct_llm_yields_perfect_recall_and_no_false_positives() -> None:
    """ "전부 정답" 가짜 → recall == 1.0, falsePositiveRate == 0."""
    llm = ScriptedDeltaLLM(GOLDEN)  # 모든 *_every=0 — 결함 없음
    results = await _run(llm)
    axes = score_all(results, GOLDEN, n=N)

    assert axes["recall"].ratio == 1.0
    assert axes["falsePositiveRate"].numerator == 0
    assert axes["nodeIdAgreement"].ratio == 1.0


async def test_noise_only_false_positive_llm_raises_noise_fp_without_moving_recall() -> None:
    """[A-14] noise 세션에만 조작 트리플을 **더하는** 가짜(비노이즈는 정답 그대로) →
    `recall` 은 1.0 을 유지한 채 `noiseFalsePositiveRate` 만 오른다 — 이게 진짜 축 독립성 시험이다.
    종전 버전(비노이즈도 전부 생략)은 recall 이 1→0 으로 함께 움직여 "noise 축만 오른다"는
    독립성을 재지 못했다(리뷰 재현).
    """
    correct_axes = score_all(await _run(ScriptedDeltaLLM(GOLDEN)), GOLDEN, n=N)

    noise_axes = score_all(await _run(NoiseFalsePositiveLLM(GOLDEN)), GOLDEN, n=N)

    assert noise_axes["noiseFalsePositiveRate"].numerator > 0
    assert noise_axes["recall"].ratio == 1.0  # 비노이즈 세션은 여전히 전부 정답
    assert noise_axes["recall"].ratio == correct_axes["recall"].ratio
    assert (
        noise_axes["noiseFalsePositiveRate"].numerator
        != correct_axes["noiseFalsePositiveRate"].numerator
    )


async def test_kind_misclassification_lowers_recall_and_appears_in_confusion_matrix() -> None:
    """[A-14] kind 만 틀린 가짜 → recall 이 떨어지고, 회전된 kind 쌍이 **정확히** 잡힌다(모호한
    `or` 단언 대신 결정론 단언 — brand→attribute 회전은 attribute 가 무어휘라 항상 성공한다)."""
    llm = ScriptedDeltaLLM(GOLDEN, misclassify_every=1)  # 매 시도 kind 회전
    results = await _run(llm)
    axes = score_all(results, GOLDEN, n=N)
    confusion = build_confusion(results, GOLDEN)

    assert axes["recall"].ratio is not None
    assert axes["recall"].ratio < 1.0

    brand_to_attribute = next(
        (
            row
            for row in confusion
            if row["expectedKind"] == "brand" and row["producedKind"] == "attribute"
        ),
        None,
    )
    assert brand_to_attribute is not None, confusion
    # brand kind 는 무어휘(attribute 로 회전해도 항상 resolve) — 골든셋의 brand 기대 트리플 수 ×
    # N 개 표본 전부에서 결정론적으로 이 쌍이 잡혀야 한다.
    assert brand_to_attribute["count"] == _BRAND_KIND_TRIPLE_COUNT * N


async def test_axes_are_not_constant_across_variants() -> None:
    """[A-14] 공허성 방어 — `recall` 뿐 아니라 `nodeIdAgreement`·`sessionExactSet`·
    `falsePositiveRate` 도 최소 두 변이 사이에서 값이 달라야 한다. 이 넷을 전부 상수로 구현해도
    이름만 다른 옛 시험은 통과했다(리뷰 지적)."""
    empty_axes = score_all(await _run(ScriptedDeltaLLM(GOLDEN, omit_every=1)), GOLDEN, n=N)
    correct_axes = score_all(await _run(ScriptedDeltaLLM(GOLDEN)), GOLDEN, n=N)

    assert empty_axes["recall"].ratio == 0.0
    assert correct_axes["recall"].ratio == 1.0

    assert empty_axes["nodeIdAgreement"].ratio is None  # 매칭 자체가 없다 — 분모 0
    assert correct_axes["nodeIdAgreement"].ratio == 1.0

    assert empty_axes["falsePositiveRate"].ratio is None  # 산출 자체가 없다 — 분모 0
    assert correct_axes["falsePositiveRate"].ratio == 0.0

    # noise 세션([]==[])만 우연히 exact-set 을 만족한다 — 정확히 noise 세션 비율.
    noise_ratio = sum(1 for s in GOLDEN.sessions if s.slice_id == "noise") / len(GOLDEN.sessions)
    assert empty_axes["sessionExactSet"].ratio == noise_ratio
    assert correct_axes["sessionExactSet"].ratio == 1.0


def test_match_sample_score_is_order_invariant() -> None:
    """[A-13] 그리디 매칭은 배열 순서에 매칭 크기가 의존했다(리뷰 재현: E1.accept={A,B},
    E2.accept={A}, produced=[A,B] 일 때 순서만 바꿔 recall 50%↔100%). 최대 이분 매칭은 크기가
    순서 불변이어야 한다."""
    e1 = ExpectedTriple.model_validate(
        {
            "kind": "brand",
            "predicate": "likes",
            "accept": ["A", "B"],
            "canonicalLabel": "A",
            "nodeId": make_node_id("brand", "A"),
        }
    )
    e2 = ExpectedTriple.model_validate(
        {
            "kind": "brand",
            "predicate": "likes",
            "accept": ["A"],
            "canonicalLabel": "A",
            "nodeId": make_node_id("brand", "A"),
        }
    )
    produced_a = ProducedTriple(
        node_id="brand:a",
        node_type="brand",
        node_label="A",
        verified=False,
        predicate="likes",
        salience=0.9,
        method=None,
        distance=None,
        margin=None,
    )
    produced_b = ProducedTriple(
        node_id="brand:b",
        node_type="brand",
        node_label="B",
        verified=False,
        predicate="likes",
        salience=0.9,
        method=None,
        distance=None,
        margin=None,
    )

    forward, _, _ = match_sample([e1, e2], [produced_a, produced_b])
    backward, _, _ = match_sample([e2, e1], [produced_a, produced_b])
    forward_swapped, _, _ = match_sample([e1, e2], [produced_b, produced_a])

    assert len(forward) == 2
    assert len(backward) == 2
    assert len(forward_swapped) == 2


async def test_denominators_count_full_golden_set_on_partial_results() -> None:
    """[A-12] `results` 에 세션 1개만 있어도 `expectedDenominator` 는 골든셋 전체 기준이고,
    `unfilledSessions` 는 나머지 29개 세션을 전부 "시작도 못 함"으로 드러낸다 — 부분 산출이
    100% 처럼 과대평가되면 안 된다(리뷰 재현: 세션 1개만 넣었더니 recall=1/1=100% 이었다)."""
    all_results = await _run(ScriptedDeltaLLM(GOLDEN))
    partial_results = [next(r for r in all_results if r.session_id == "kc-brand-01")]

    full_axes = score_all(all_results, GOLDEN, n=N)
    partial_axes = score_all(partial_results, GOLDEN, n=N)

    assert partial_axes["recall"].expected_denominator == full_axes["recall"].expected_denominator
    assert (
        partial_axes["sessionExactSet"].expected_denominator
        == full_axes["sessionExactSet"].expected_denominator
    )

    unfilled = unfilled_sessions(partial_results, GOLDEN, n=N)
    assert len(unfilled) == 29
    assert all(
        row["errorTypes"] == ["notStarted"] for row in unfilled if row["sessionId"] != "kc-brand-01"
    )
