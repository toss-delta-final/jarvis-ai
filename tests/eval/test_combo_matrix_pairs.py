"""INV/DIR 쌍 실검증 러너 게이트 (이슈 #371 §7) — @pytest.mark.eval / slow, 기본 PR pytest 에서는 제외되고 별도 워크플로우에서 돈다."""

from __future__ import annotations

import pytest

from evals.combo_matrix import pair_runner as pair_runner_module
from evals.combo_matrix.loader import load_cases, load_pair_checks
from evals.combo_matrix.pair_runner import (
    PAIR_CHECKS_MD_PATH,
    Projection,
    _axis_diff,
    generate_pair_checks_md,
    run_pair_checks,
)
from evals.combo_matrix.schema import ComboCase, PairCheckSpec

# run_pair_checks()가 INV/DIR 쌍마다 base+perturbed 전체 그래프를 재관측하고,
# md 재생성 테스트가 그 전체를 한 번 더 돌린다 — combo_matrix_eval과 같은 이유로 CI에서 제외.
pytestmark = [pytest.mark.eval, pytest.mark.slow]


def _stub_case(case_id: str, *, kind: str, perturbation_of: str | None) -> ComboCase:
    """판정 엔진 단위 테스트 전용 최소 케이스 — 실행하지 않으므로 axes/utterance 는 무의미하다."""
    return ComboCase(
        case_id=case_id,
        axes={},
        utterance="stub",
        checklist_type=kind,
        label_required=False,
        observation_mode="ci",
        perturbation_of=perturbation_of,
    )


def _stub_projection(**overrides: object) -> Projection:
    defaults: dict[str, object] = {
        "terminal": "done",
        "finish_reason": "stop",
        "error_code": None,
        "search_filters": {},
        "push_product_count": 3,
    }
    defaults.update(overrides)
    return Projection(**defaults)


# ─────────── 1. 정합 ───────────


def test_pair_check_specs_match_perturbation_cases() -> None:
    """spec 의 case_id 집합 == perturbation_of 케이스 집합(양방향) — 미래에 INV/DIR 케이스가
    늘면 spec 없이는 이 테스트가 깨져 검증 누락을 강제로 드러낸다."""
    cases = {c.case_id: c for c in load_cases()}
    perturbation_case_ids = {c.case_id for c in cases.values() if c.perturbation_of}
    specs = load_pair_checks()
    spec_case_ids = {s.case_id for s in specs}
    assert spec_case_ids == perturbation_case_ids

    for spec in specs:
        case = cases[spec.case_id]
        assert spec.kind == case.checklist_type, (
            f"{spec.case_id}: spec.kind={spec.kind} != case.checklist_type={case.checklist_type}"
        )
        assert case.perturbation_of in cases, f"{spec.case_id}: 원본 케이스가 없다"
        if spec.mode == "ci" and spec.kind == "DIR":
            assert spec.metric is not None, f"{spec.case_id}: mode=ci kind=DIR 는 metric 필수"
            assert spec.direction is not None, f"{spec.case_id}: mode=ci kind=DIR 는 direction 필수"


# ─────────── 2. 축 diff 단일성 ───────────


def test_pair_axis_diff_is_exactly_one_axis() -> None:
    """각 쌍의 base vs perturbed 축 diff 가 정확히 1개 축인지(현행 3쌍 전제)."""
    cases = {c.case_id: c for c in load_cases()}
    for spec in load_pair_checks():
        case = cases[spec.case_id]
        base_case = cases[case.perturbation_of]
        diff = _axis_diff(base_case, case)
        assert len(diff) == 1, f"{spec.case_id}: 축 diff 가 1개가 아니다 ({diff})"


# ─────────── 3. ci 쌍 실행 통과 ───────────


async def test_ci_pairs_pass_verdict() -> None:
    """pair_runner 실행 → 모든 ci 쌍 verdict == pass. 결과 집합에 spec 전 행이 상태와 함께
    존재(조용한 탈락 금지) — manual 행도 status="manual" 로 포함돼야 한다."""
    specs = load_pair_checks()
    cases = {c.case_id: c for c in load_cases()}
    results = await run_pair_checks(specs, cases)

    assert {r.spec.case_id for r in results} == {s.case_id for s in specs}

    for result in results:
        if result.spec.mode == "manual":
            assert result.status == "manual", f"{result.spec.case_id}"
            continue
        assert result.status == "pass", f"{result.spec.case_id}: verdict={result.status}"
        if result.spec.kind == "INV":
            assert result.mismatched_fields == [], (
                f"{result.spec.case_id}: 불일치 필드 {result.mismatched_fields}"
            )
        else:  # DIR
            assert result.guard_results, f"{result.spec.case_id}: guard 결과 없음"
            assert all(result.guard_results.values()), (
                f"{result.spec.case_id}: guard 실패 {result.guard_results}"
            )
            assert result.metric_base is not None and result.metric_perturbed is not None


# ─────────── 4. 공허 방지 fixture 가드 ───────────


async def test_hard_filter_pair_fixture_actually_narrows() -> None:
    """하드필터 추가 쌍의 base_count > perturbed_count(엄격 감소) — PAIR_CATALOG 가 그 필터를
    실제로 태우는지의 sanity. 누가 PAIR_CATALOG 를 바꿔 필터가 안 물게 되면 여기서 시끄럽게 깨진다.

    case id 는 재생성마다 바뀐다(구 combo-0054 → #367 이후 combo-0053 → #386 이후 combo-0056,
    흔드는 축도 category → price_min 으로 바뀌었다) — **번호를 박지 않고 spec 의 성격으로 찾는다.**
    """
    specs = {s.case_id: s for s in load_pair_checks()}
    cases = {c.case_id: c for c in load_cases()}
    dir_specs = [s for s in specs.values() if s.kind == "DIR" and s.metric == "push_product_count"]
    assert len(dir_specs) == 1, dir_specs
    results = await run_pair_checks(dir_specs, cases)
    result = results[0]
    assert result.metric_base is not None
    assert result.metric_perturbed is not None
    assert result.metric_base > result.metric_perturbed, (
        f"{result.spec.case_id}: base={result.metric_base}, "
        f"perturbed={result.metric_perturbed} — "
        "필터 추가가 결과 수를 실제로 줄이지 않는다(공허 통과 위험)"
    )


# ─────────── 5. 산출물 재생성 일치 ───────────


async def test_pair_checks_md_regeneration_matches_committed() -> None:
    """pair_runner 가 생성한 PAIR_CHECKS.md 텍스트 == 커밋본(바이트 동일) — 실행이 전부
    결정론이므로 가능하다."""
    text, _results = await generate_pair_checks_md(write=False)
    committed = PAIR_CHECKS_MD_PATH.read_text(encoding="utf-8")
    assert text == committed, "PAIR_CHECKS.md 가 최신 실행과 다르다 — 재생성 후 커밋할 것"


# ─────────── 6. manual 분리 회귀 가드 ───────────


def test_recall_pair_is_manual_with_goldenset_link() -> None:
    """recall 쌍(identity guest→member) spec 이 mode=manual 이고 link 가 goldenset 을 가리키는지
    (규약 3항 분리의 회귀 가드).

    위 하드필터 쌍과 같은 이유로 case id 를 박지 않는다 — 재생성마다 번호가 밀린다
    (구 combo-0055 → #367 이후 combo-0054 → #386 이후 combo-0057).
    """
    specs = {s.case_id: s for s in load_pair_checks()}
    manual_specs = [s for s in specs.values() if s.mode == "manual"]
    assert len(manual_specs) == 1, manual_specs
    spec = manual_specs[0]
    assert spec.link is not None and "goldenset" in spec.link


# ─────────── 7. 판정 엔진 자체 검증 (리뷰 R2 F2) ───────────
#
# 위 테스트들은 전부 "현재 픽스처로 실행하면 pass 한다"만 잠근다 — 판정 로직이 망가져 무조건
# pass 를 돌려줘도 아무 것도 안 깨진다. 여기서는 파이프라인을 다시 태우지 않고 `_execute` 를
# monkeypatch 해 조작된 `Projection` 쌍을 직접 먹여, 판정 로직만 정확히 겨냥한다.


async def test_verdict_engine_catches_inv_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV: invariant_fields 중 하나가 다른 쌍 → status=fail 이고 mismatched_fields 에 그 필드가 담긴다."""
    base_case = _stub_case("combo-9001", kind="INV", perturbation_of=None)
    perturbed_case = _stub_case("combo-9002", kind="INV", perturbation_of="combo-9001")
    projections = {
        "combo-9001": _stub_projection(push_product_count=3),
        "combo-9002": _stub_projection(push_product_count=2),  # pushProductCount 만 다르다
    }

    async def fake_execute(case: ComboCase) -> Projection:
        return projections[case.case_id]

    monkeypatch.setattr(pair_runner_module, "_execute", fake_execute)

    spec = PairCheckSpec(
        case_id="combo-9002",
        kind="INV",
        mode="ci",
        invariant_fields=["pushProductCount", "terminal"],
        reason="test",
    )
    results = await run_pair_checks([spec], {"combo-9001": base_case, "combo-9002": perturbed_case})
    result = results[0]
    assert result.status == "fail"
    assert result.mismatched_fields == ["pushProductCount"]


async def test_verdict_engine_fails_on_broken_superset_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DIR: 방향은 성립하지만(3→2, non_increase) perturbed_filters_strict_superset guard 가
    거짓인 쌍 → status=fail(guard 실패는 방향이 맞아도 FAIL 이라는 계약)."""
    base_case = _stub_case("combo-9003", kind="DIR", perturbation_of=None)
    perturbed_case = _stub_case("combo-9004", kind="DIR", perturbation_of="combo-9003")
    same_filters = {"category": "무선이어폰"}  # 양쪽이 완전히 같다 — 진상위집합이 아니다
    projections = {
        "combo-9003": _stub_projection(search_filters=same_filters, push_product_count=3),
        "combo-9004": _stub_projection(search_filters=same_filters, push_product_count=2),
    }

    async def fake_execute(case: ComboCase) -> Projection:
        return projections[case.case_id]

    monkeypatch.setattr(pair_runner_module, "_execute", fake_execute)

    spec = PairCheckSpec(
        case_id="combo-9004",
        kind="DIR",
        mode="ci",
        metric="push_product_count",
        direction="non_increase",
        guards=["perturbed_filters_strict_superset"],
        reason="test",
    )
    results = await run_pair_checks([spec], {"combo-9003": base_case, "combo-9004": perturbed_case})
    result = results[0]
    assert result.metric_base == 3
    assert result.metric_perturbed == 2  # 방향 부등식 자체는 성립
    assert result.guard_results == {"perturbed_filters_strict_superset": False}
    assert result.status == "fail"


async def test_verdict_engine_fails_on_zero_base_count_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DIR: base_count_positive 가 거짓인 쌍(base metric 0) → status=fail(0→0 은 non_increase 를
    공허하게 만족하므로 guard 없이는 잡히지 않는다)."""
    base_case = _stub_case("combo-9005", kind="DIR", perturbation_of=None)
    perturbed_case = _stub_case("combo-9006", kind="DIR", perturbation_of="combo-9005")
    projections = {
        "combo-9005": _stub_projection(push_product_count=0),
        "combo-9006": _stub_projection(push_product_count=0),
    }

    async def fake_execute(case: ComboCase) -> Projection:
        return projections[case.case_id]

    monkeypatch.setattr(pair_runner_module, "_execute", fake_execute)

    spec = PairCheckSpec(
        case_id="combo-9006",
        kind="DIR",
        mode="ci",
        metric="push_product_count",
        direction="non_increase",
        guards=["base_count_positive"],
        reason="test",
    )
    results = await run_pair_checks([spec], {"combo-9005": base_case, "combo-9006": perturbed_case})
    result = results[0]
    assert result.guard_results == {"base_count_positive": False}
    assert result.status == "fail"
