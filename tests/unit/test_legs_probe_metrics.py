"""leg-그룹 매칭 규칙과 축 채점 — 합성 표본으로 분자·분모를 검증한다 (#332)."""

from __future__ import annotations

import json

from app.agents.buyer.recommendation.state import CategoryQuery
from evals.legs_probe.loader import load_anchor_set, resolve_fixture_path
from evals.legs_probe.metrics import (
    compute_baseline,
    compute_leg_diagnostics,
    diagnostics,
    is_utterance_echo,
    score_all,
)
from evals.legs_probe.runner import CellResult, Sample
from evals.legs_probe.schema import AnchorSet

ANCHORS = load_anchor_set()
BY_ID = {anchor.case_id: anchor for anchor in ANCHORS.utterances}


def _raw_anchors() -> dict:
    return json.loads(resolve_fixture_path(None).read_text(encoding="utf-8"))


def _sample(case_id: str, index: int, **overrides: object) -> Sample:
    base = {
        "case_id": case_id,
        "sample_index": index,
        "intent": "recommend",
        "case": 3,
        "category_queries": (),
        "buy_all": False,
        "total_budget": None,
        "latency_ms": 0,
    }
    base.update(overrides)
    return Sample(**base)  # type: ignore[arg-type]


# ─────────── leg-그룹 매칭 규칙(§3) ───────────


def test_earphone_synonym_matches_head_noun_phrase() -> None:
    anchor = BY_ID["buy-srch-0003"]  # coverageGroups: earphone: [이어폰]
    leg = CategoryQuery(raw_category=None, query="무선 이어폰")
    diag = compute_leg_diagnostics(_sample(anchor.case_id, 0, category_queries=(leg,)), anchor)
    assert diag.matched_coverage_group_ids == ("earphone",)
    assert diag.over_expanded_leg_count == 0


def test_unrelated_product_sharing_a_substring_does_not_match() -> None:
    anchor = BY_ID["buy-srch-0003"]  # earphone: [이어폰]
    leg = CategoryQuery(raw_category=None, query="이어폰 케이스")
    diag = compute_leg_diagnostics(_sample(anchor.case_id, 0, category_queries=(leg,)), anchor)
    assert diag.matched_coverage_group_ids == ()
    assert diag.over_expanded_leg_count == 1


def test_space_removed_synonym_matches_spaced_query() -> None:
    anchor = BY_ID["legs-situ-0007"]  # mat: [요가매트, 요가 매트, 매트]
    leg = CategoryQuery(raw_category=None, query="요가 매트")
    diag = compute_leg_diagnostics(_sample(anchor.case_id, 0, category_queries=(leg,)), anchor)
    assert "mat" in diag.matched_coverage_group_ids


def test_spaced_synonym_matches_as_substring() -> None:
    anchor = BY_ID["legs-situ-0004"]  # garlic: [마늘, 다진마늘, "다진 마늘"]
    leg = CategoryQuery(raw_category=None, query="국산 다진 마늘 한 팩")
    diag = compute_leg_diagnostics(_sample(anchor.case_id, 0, category_queries=(leg,)), anchor)
    assert "garlic" in diag.matched_coverage_group_ids


def test_raw_category_fallback_matches_when_query_does_not() -> None:
    anchor = BY_ID["buy-srch-0003"]  # earphone: [이어폰]
    leg = CategoryQuery(raw_category="음향가전 > 이어폰", query="무선 헤드셋")
    diag = compute_leg_diagnostics(_sample(anchor.case_id, 0, category_queries=(leg,)), anchor)
    assert diag.matched_coverage_group_ids == ("earphone",)


def test_query_match_wins_over_an_earlier_groups_raw_category_match() -> None:
    """[R2-2] 2-pass 회귀 가드 — 리뷰어 재현: A=휴대폰, B=케이스, leg
    query="휴대폰 케이스"·raw="휴대폰". 그룹 단위(첫 매치에서 종료) 탐색이면 A 가 raw_category
    규칙(3)으로 먼저 걸려 A 로 잘못 귀속된다 — 문서 §3 의 우선순위(전 그룹 query 우선, 그래도
    안 잡히면 전 그룹 raw)대로면 B 가 query 규칙(2, head-token)으로 맞아야 한다.
    """
    from evals.legs_probe.schema import Anchor, CoverageGroup, Expected

    expected = Expected(
        case=3,
        legs_min=1,
        legs_max=2,
        coverage_target=1,
        coverage_groups=[
            CoverageGroup(group_id="A", synonyms=["휴대폰"]),
            CoverageGroup(group_id="B", synonyms=["케이스"]),
        ],
    )
    anchor = Anchor(
        case_id="synthetic-r2-2-0001",
        slice="situational",
        utterance="휴대폰 케이스 사려고",
        expected=expected,
        baseline_groups_hit=0,
    )
    leg = CategoryQuery(raw_category="휴대폰", query="휴대폰 케이스")
    diag = compute_leg_diagnostics(_sample(anchor.case_id, 0, category_queries=(leg,)), anchor)
    assert diag.matched_coverage_group_ids == ("B",)


def test_acceptable_group_does_not_count_toward_coverage_but_is_not_over_expansion() -> None:
    anchor = BY_ID["buy-srch-0001"]  # acceptable: noodle: [수제비, 라면사리, 사리]
    leg = CategoryQuery(raw_category=None, query="라면사리")
    diag = compute_leg_diagnostics(_sample(anchor.case_id, 0, category_queries=(leg,)), anchor)
    assert diag.matched_coverage_group_ids == ()
    assert diag.over_expanded_leg_count == 0


def test_leg_coverage_ratio_is_capped_at_one() -> None:
    anchor = BY_ID["legs-mult-0001"]  # earphone / laptop, coverageTarget=2
    legs = (
        CategoryQuery(raw_category=None, query="이어폰"),
        CategoryQuery(raw_category=None, query="노트북"),
        CategoryQuery(raw_category=None, query="파우치"),  # 과전개, 커버리지엔 안 잡힘
    )
    diag = compute_leg_diagnostics(_sample(anchor.case_id, 0, category_queries=legs), anchor)
    assert diag.coverage_ratio == 1.0
    assert diag.over_expanded_leg_count == 1


# ─────────── 발화 에코 진단(#198 D3) ───────────


def test_utterance_echo_leg_is_detected() -> None:
    leg = CategoryQuery(raw_category=None, query="감자탕 재료")
    assert is_utterance_echo(leg, "감자탕 재료 추천")


def test_non_echo_leg_is_not_flagged() -> None:
    leg = CategoryQuery(raw_category=None, query="돼지고기")
    assert not is_utterance_echo(leg, "감자탕 재료 추천")


def test_echo_leg_counted_only_when_over_expanded() -> None:
    """leg '감자탕 재료' 는 발화를 그대로 복사(echo)했지만 어떤 그룹에도 매칭되지 않아
    과전개(over-expanded)다 — echo 는 과전개 leg 중에서만 센다(§3)."""
    anchor = BY_ID["buy-srch-0001"]
    leg = CategoryQuery(raw_category=None, query="감자탕 재료")
    diag = compute_leg_diagnostics(_sample(anchor.case_id, 0, category_queries=(leg,)), anchor)
    assert diag.over_expanded_leg_count == 1
    assert diag.echo_leg_count == 1


# ─────────── 축 채점(§3) ───────────


def _cell_result(anchor_id: str, samples: list[Sample]) -> CellResult:
    anchor = BY_ID[anchor_id]
    return CellResult(
        cell_id=anchor.case_id,
        case_id=anchor.case_id,
        slice=anchor.slice,
        samples=samples,
        attempts=len(samples),
        filled=True,
    )


def test_case3_under_expansion_rate_uses_produced_case_not_expected() -> None:
    """분모는 **산출** case==3 인 표본이다 — expected.case 가 아니다."""
    anchor_id = "legs-single-0001"  # expected.case == 1
    samples = [
        _sample(anchor_id, 0, case=3, category_queries=(CategoryQuery(query="a"),)),
        _sample(anchor_id, 1, case=1, category_queries=(CategoryQuery(query="b"),)),
    ]
    scored = score_all([_cell_result(anchor_id, samples)], ANCHORS)
    axis = scored["axes"]["case3UnderExpansionRate"]
    # 산출 case==3 인 표본은 1건(첫 샘플) — 그 표본의 legs 는 1개(<=1)라 과소전개.
    assert axis.denominator == 1
    assert axis.numerator == 1


def test_non_recommend_intent_samples_are_excluded() -> None:
    anchor_id = "buy-srch-0001"
    samples = [
        _sample(anchor_id, 0, intent="general", case=3, category_queries=()),
        _sample(
            anchor_id, 1, intent="recommend", case=3, category_queries=(CategoryQuery(query="x"),)
        ),
    ]
    scored = score_all([_cell_result(anchor_id, samples)], ANCHORS)
    assert scored["axes"]["caseAccuracy"].denominator == 1


def test_prompt_example_anchors_excluded_from_primary_confirmatory() -> None:
    prompt_example_id = "legs-situ-0009"  # promptExample=true
    non_prompt_example_id = "legs-situ-0001"
    samples_pe = [
        _sample(prompt_example_id, 0, case=3, category_queries=(CategoryQuery(query="x"),))
    ]
    samples_normal = [
        _sample(non_prompt_example_id, 0, case=3, category_queries=(CategoryQuery(query="x"),))
    ]
    results = [
        _cell_result(prompt_example_id, samples_pe),
        _cell_result(non_prompt_example_id, samples_normal),
    ]
    scored = score_all(results, ANCHORS)
    primary = scored["axes"]["case3UnderExpansionRate"]
    with_pe = scored["axes"]["case3UnderExpansionRateWithPromptExamples"]
    assert primary.denominator == 1  # promptExample 앵커 제외
    assert with_pe.denominator == 2  # 포함


def test_prompt_example_anchors_excluded_from_confirmatory_leg_coverage() -> None:
    """[F-1] legCoverage 는 confirmatory-secondary 라 primary 와 같은 규칙으로 promptExample
    을 뺀다 — situational 슬라이스 분모(예문 0009·0010 포함 시 82)가 이 축에서만 조용히
    그대로면 문서(README §「confirmatory 사전 등록」)와 산출물이 모순된다."""
    prompt_example_id = "legs-situ-0009"  # promptExample=true, coverageGroups 있음
    non_prompt_example_id = "legs-situ-0001"  # coverageGroups 있음
    samples_pe = [
        _sample(prompt_example_id, 0, case=3, category_queries=(CategoryQuery(query="수면양말"),))
    ]
    samples_normal = [
        _sample(non_prompt_example_id, 0, case=3, category_queries=(CategoryQuery(query="텐트"),))
    ]
    results = [
        _cell_result(prompt_example_id, samples_pe),
        _cell_result(non_prompt_example_id, samples_normal),
    ]
    scored = score_all(results, ANCHORS)
    confirmatory = scored["axes"]["legCoverage"]
    with_pe = scored["axes"]["legCoverageWithPromptExamples"]
    assert confirmatory.denominator == 1  # promptExample 앵커 제외
    assert with_pe.denominator == 2  # 포함
    assert confirmatory.nature == "confirmatory-secondary"
    assert with_pe.nature == "exploratory"


def test_serialized_axis_id_matches_its_map_key_for_every_axis() -> None:
    """[R2-3] `axis.as_dict()["axisId"]` 가 `score_all` 의 map key 와 어긋나면 소비자가 제외판·
    포함판을 구분할 수 없다(#234/#240 이 밟은 "같은 이름, 다른 뜻" 사고와 같은 모양)."""
    scored = score_all([], ANCHORS)
    for map_key, axis in scored["axes"].items():
        assert axis.axis_id == map_key, f"{map_key} 의 axisId 가 {axis.axis_id} 로 어긋난다"


def test_case3_under_expansion_definition_differs_between_excluded_and_included() -> None:
    """[R2-3] 제외판·포함판이 같은 정의 문장이면 "같은 이름·같은 정의, 다른 숫자" 사고가 난다
    — legCoverage 는 이미 있었고(F-1), case3 쪽에도 같은 명시가 필요하다."""
    scored = score_all([], ANCHORS)
    excluded = scored["axes"]["case3UnderExpansionRate"]
    included = scored["axes"]["case3UnderExpansionRateWithPromptExamples"]
    assert excluded.definition_denominator != included.definition_denominator
    assert "제외" in excluded.definition_denominator
    assert "포함" in included.definition_denominator


def test_leg_coverage_slice_breakdown_excludes_prompt_example() -> None:
    prompt_example_id = "legs-situ-0009"
    samples_pe = [
        _sample(prompt_example_id, 0, case=3, category_queries=(CategoryQuery(query="수면양말"),))
    ]
    scored = score_all([_cell_result(prompt_example_id, samples_pe)], ANCHORS)
    assert scored["slices"]["legCoverage"]["situational"].denominator == 0


def test_wilson_ci_bounds_are_between_zero_and_one() -> None:
    from evals.legs_probe.metrics import wilson_ci

    ci = wilson_ci(3, 10)
    assert ci is not None
    low, high = ci
    assert 0.0 <= low < 3 / 10 < high <= 1.0


def test_wilson_ci_is_none_for_empty_denominator() -> None:
    from evals.legs_probe.metrics import wilson_ci

    assert wilson_ci(0, 0) is None


def test_axis_result_reports_exploratory_below_sample_threshold() -> None:
    anchor_id = "legs-single-0001"
    samples = [_sample(anchor_id, 0, case=1, category_queries=(CategoryQuery(query="청바지"),))]
    scored = score_all([_cell_result(anchor_id, samples)], ANCHORS)
    axis = scored["axes"]["caseAccuracy"]
    assert axis.denominator < 40
    assert axis.as_dict()["belowSampleThreshold"] is True


# ─────────── trivial baseline (§3, 1급 산출물) ───────────


def test_baseline_case3_under_expansion_is_structurally_one() -> None:
    baseline = compute_baseline(ANCHORS)
    assert baseline["case3UnderExpansionRate"] == 1.0


def test_baseline_over_expansion_is_zero() -> None:
    baseline = compute_baseline(ANCHORS)
    assert baseline["overExpansionRate"] == 0.0


def test_baseline_leg_coverage_uses_baseline_groups_hit() -> None:
    baseline = compute_baseline(ANCHORS)
    anchor = BY_ID["buy-srch-0001"]  # coverageTarget=3, baselineGroupsHit=0
    assert baseline["legCoveragePerAnchor"][anchor.case_id] == 0.0
    single_anchor = BY_ID["buy-srch-0003"]  # coverageTarget=1, baselineGroupsHit=1
    assert baseline["legCoveragePerAnchor"][single_anchor.case_id] == 1.0


def test_baseline_conditions_slice_has_no_leg_coverage() -> None:
    baseline = compute_baseline(ANCHORS)
    conditions_anchor = next(a for a in ANCHORS.utterances if a.slice == "conditions")
    assert baseline["legCoveragePerAnchor"][conditions_anchor.case_id] is None
    assert baseline["legCoveragePerSlice"]["conditions"] is None


def test_baseline_per_slice_excludes_prompt_example_anchors() -> None:
    """[F-1] baseline 도 legCoverage(confirmatory) 와 같은 규칙으로 promptExample 을 뺀다 —
    안 그러면 "LLM vs baseline" 대조표가 사과-사과가 아니게 된다.

    situational 슬라이스의 promptExample 앵커(legs-situ-0009)는 원래 baselineGroupsHit==0 인데
    ==coverageTarget 로 인위적으로 올려 100% 기여를 만든다 — 제외판 평균은 그 기여를 안 보고,
    포함판 평균은 본다.
    """
    data = _raw_anchors()
    prompt_example = next(u for u in data["utterances"] if u["caseId"] == "legs-situ-0009")
    prompt_example["baselineGroupsHit"] = 1
    prompt_example["expected"]["coverageTarget"] = 1  # baselineGroupsHit/target == 1.0
    anchors = AnchorSet.model_validate(data)
    baseline = compute_baseline(anchors)
    excluded = baseline["legCoveragePerSlice"]["situational"]
    included = baseline["legCoveragePerSliceWithPromptExamples"]["situational"]
    assert excluded is not None and included is not None
    assert included > excluded


# ─────────── 진단 카운터(§3) ───────────


def test_empty_legs_on_case3_is_counted() -> None:
    anchor_id = "buy-srch-0001"
    samples = [_sample(anchor_id, 0, case=3, category_queries=())]
    diag = diagnostics([_cell_result(anchor_id, samples)], ANCHORS)
    assert diag["emptyLegsOnCase3Count"] == 1


def test_non_recommend_intent_count_is_keyed_by_anchor() -> None:
    anchor_id = "buy-srch-0001"
    samples = [_sample(anchor_id, 0, intent="cart_add", case=2, category_queries=())]
    diag = diagnostics([_cell_result(anchor_id, samples)], ANCHORS)
    assert diag["nonRecommendIntentCount"] == {anchor_id: 1}
