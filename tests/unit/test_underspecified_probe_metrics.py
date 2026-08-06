"""축 정의·채점(§D8) + 원인 축 분해(§D11) + trivial baseline(§D12) — 손계산 대조 (#380).

각 테스트는 "엔진을 무조건 pass 로 바꿔도 통과하는가"를 스스로 묻는다 — 지표는 손계산한
기대값과 대조하고, `keys()`·존재 여부만 보는 검사는 넣지 않는다(공허한 검증 금지, 패킷 §D14).
"""

from __future__ import annotations

import pytest

from app.agents.buyer.recommendation.needs_expansion import detect_expansion_need
from app.agents.buyer.recommendation.state import RouteDecision
from app.schemas.spring import ProductSearchFilters
from evals.underspecified_probe import metrics as metrics_module
from evals.underspecified_probe.loader import load_anchor_set
from evals.underspecified_probe.metrics import (
    AxisResult,
    axis_expansion_gate_would_fire_rate,
    axis_false_alarm_rate,
    axis_false_alarm_rate_with_non_recommend_intent,
    axis_flag_off_invariant,
    axis_judgment_accuracy,
    axis_miss_rate,
    axis_miss_rate_under_expansion_assumption,
    axis_miss_rate_with_non_recommend_intent,
    axis_prior_gate_invariant,
    cause_axis_summary,
    compute_baseline,
    diagnostics,
    sample_rows,
    wilson_ci,
)
from evals.underspecified_probe.runner import (
    CellResult,
    JudgmentSettings,
    Sample,
    cause_axes_for_miss,
    nonblank_blocking_axes,
)

ANCHORS = load_anchor_set()
BY_ID = {anchor.case_id: anchor for anchor in ANCHORS.utterances}
JUDGMENT = JudgmentSettings(underspecified_reask_enabled=True)


def _blank_decision(**overrides: object) -> RouteDecision:
    base: dict[str, object] = {
        "intent": "recommend",
        "filters": ProductSearchFilters(),
        "case": 2,
        "semantic_query_is_fallback": True,
    }
    base.update(overrides)
    return RouteDecision(**base)  # type: ignore[arg-type]


def _sample(
    case_id: str, *, verdict: bool, decision: RouteDecision | None = None, index: int = 0
) -> Sample:
    return Sample(
        case_id=case_id,
        sample_index=index,
        decision=decision if decision is not None else _blank_decision(),
        prior=None,
        verdict=verdict,
        expansion_reason=None,
        latency_ms=1,
    )


def _cell(case_id: str, samples: list[Sample]) -> CellResult:
    anchor = BY_ID[case_id]
    return CellResult(
        cell_id=case_id,
        case_id=case_id,
        slice=anchor.slice,
        samples=samples,
        attempts=len(samples),
        filled=True,
    )


# ─────────── missRate/falseAlarmRate/judgmentAccuracy 손계산 ───────────


def test_miss_rate_numerator_denominator_hand_computed() -> None:
    """buy-under-0002(no_condition, expectedReask=true) 표본 3개: [True, False, False]."""
    samples = [
        _sample("buy-under-0002", verdict=v, index=i) for i, v in enumerate([True, False, False])
    ]
    results = [_cell("buy-under-0002", samples)]
    axis = axis_miss_rate(metrics_module._pairs(results, ANCHORS))
    assert (axis.numerator, axis.denominator) == (2, 3)
    assert axis.ratio == pytest.approx(2 / 3)


def test_false_alarm_rate_gate_slice_exclusion_uses_a_different_denominator() -> None:
    """[#234/#240 회귀 가드] 제외판·포함판이 같은 분모를 쓰면 안 된다."""
    what_axis_samples = [
        _sample("buy-under-0004", verdict=True, index=0)
    ]  # what_axis, expectedReask=false
    gate_samples = [
        _sample("buy-under-0006", verdict=True, index=0)
    ]  # multiturn_gate, expectedReask=false
    results = [_cell("buy-under-0004", what_axis_samples), _cell("buy-under-0006", gate_samples)]
    pairs = metrics_module._pairs(results, ANCHORS)
    excluded = axis_false_alarm_rate(pairs, include_gate_slice=False)
    included = axis_false_alarm_rate(pairs, include_gate_slice=True)
    assert (excluded.numerator, excluded.denominator) == (1, 1)
    assert (included.numerator, included.denominator) == (2, 2)
    assert excluded.denominator != included.denominator


def test_judgment_accuracy_hand_computed() -> None:
    hit = _sample("buy-under-0002", verdict=True, index=0)
    miss = _sample("buy-under-0002", verdict=False, index=1)
    results = [_cell("buy-under-0002", [hit, miss])]
    axis = axis_judgment_accuracy(metrics_module._pairs(results, ANCHORS), include_gate_slice=False)
    assert (axis.numerator, axis.denominator) == (1, 2)


# ─────────── F-1 — intent != "recommend" 표본은 confirmatory 분모에서 빠진다 ───────────


def test_miss_rate_excludes_non_recommend_intent_samples() -> None:
    """buy-under-0002(no_condition, expectedReask=true) 표본 3개: recommend 미탐 1건 +
    general 미탐 1건 + recommend 히트 1건. general 표본은 프로덕션에서 판정에 도달하지 않으므로
    분모에서 빠져야 한다."""
    recommend_hit = _sample("buy-under-0002", verdict=True, index=0)
    recommend_miss = _sample("buy-under-0002", verdict=False, index=1)
    general_miss = _sample(
        "buy-under-0002",
        verdict=False,
        decision=_blank_decision(intent="general"),
        index=2,
    )
    results = [_cell("buy-under-0002", [recommend_hit, recommend_miss, general_miss])]
    pairs = metrics_module._pairs(results, ANCHORS)
    axis = axis_miss_rate(pairs)
    # general 표본이 빠지므로 분모는 2(recommend 표본만), 미탐은 1건.
    assert (axis.numerator, axis.denominator) == (1, 2)


def test_miss_rate_with_non_recommend_intent_uses_a_different_wider_denominator() -> None:
    """[#234/#240 회귀 가드] 포함판은 intent 무관 전체를 쓰므로 제외판과 분모가 달라야 한다."""
    recommend_miss = _sample("buy-under-0002", verdict=False, index=0)
    general_miss = _sample(
        "buy-under-0002", verdict=False, decision=_blank_decision(intent="general"), index=1
    )
    results = [_cell("buy-under-0002", [recommend_miss, general_miss])]
    pairs = metrics_module._pairs(results, ANCHORS)
    excluded = axis_miss_rate(pairs)
    included = axis_miss_rate_with_non_recommend_intent(pairs)
    assert (excluded.numerator, excluded.denominator) == (1, 1)
    assert (included.numerator, included.denominator) == (2, 2)
    assert excluded.denominator != included.denominator


def test_false_alarm_rate_excludes_non_recommend_intent_samples() -> None:
    recommend_false_alarm = _sample("buy-under-0004", verdict=True, index=0)  # what_axis
    general_false_alarm = _sample(
        "buy-under-0004", verdict=True, decision=_blank_decision(intent="general"), index=1
    )
    results = [_cell("buy-under-0004", [recommend_false_alarm, general_false_alarm])]
    pairs = metrics_module._pairs(results, ANCHORS)
    excluded = axis_false_alarm_rate(pairs, include_gate_slice=False)
    included = axis_false_alarm_rate_with_non_recommend_intent(pairs)
    assert (excluded.numerator, excluded.denominator) == (1, 1)
    assert (included.numerator, included.denominator) == (2, 2)


def test_non_recommend_intent_count_diagnostic_counts_by_case_id_and_intent() -> None:
    general_sample = _sample(
        "buy-under-0002", verdict=False, decision=_blank_decision(intent="general"), index=0
    )
    cart_view_sample = _sample(
        "buy-under-0002", verdict=False, decision=_blank_decision(intent="cart_view"), index=1
    )
    recommend_sample = _sample("buy-under-0002", verdict=True, index=2)
    results = [_cell("buy-under-0002", [general_sample, cart_view_sample, recommend_sample])]
    diag = diagnostics(results, ANCHORS)
    assert diag["nonRecommendIntentCount"] == {"buy-under-0002": {"cart_view": 1, "general": 1}}


def test_flag_off_and_prior_gate_invariants_still_include_non_recommend_samples() -> None:
    """[F-1] 두 불변식은 판정 함수 게이트 자체를 보는 것이라 intent 와 무관하게 전 표본을 쓴다."""
    general_sample = _sample(
        "buy-under-0002", verdict=True, decision=_blank_decision(intent="general"), index=0
    )
    results = [_cell("buy-under-0002", [general_sample])]
    pairs = metrics_module._pairs(results, ANCHORS)
    assert axis_flag_off_invariant(pairs).denominator == 1
    assert axis_prior_gate_invariant(pairs).denominator == 1


# ─────────── F-6 — 전개 게이트 축 분자 경로가 실제로 태워진다 ───────────


def test_expansion_gate_would_fire_rate_numerator_increments_on_case3_no_legs() -> None:
    """`detect_expansion_need` 는 case==3 ∧ 신호 있는 leg 없음이면 "no_legs" 를 낸다(프로덕션
    함수를 그대로 호출해 확인한다 — 문자열을 손으로 지어내지 않는다) — 판정 True(=이 하네스가
    되물음 대상으로 판정)인 표본에서 이 사유가 잡히면 분자에 들어간다."""
    decision = _blank_decision(case=3)  # category_queries=[] 기본값(신호 있는 leg 없음)
    expansion_reason = detect_expansion_need(
        decision.category_queries, case=decision.case, unresolved=[]
    )
    assert expansion_reason == "no_legs"  # 전제 확인 — 실제로 이 사유가 나오는 형상인가
    sample = Sample(
        case_id="buy-under-0002",
        sample_index=0,
        decision=decision,
        prior=None,
        verdict=True,
        expansion_reason=expansion_reason,
        latency_ms=1,
    )
    other_sample = _sample("buy-under-0002", verdict=True, index=1)  # expansion_reason=None
    results = [_cell("buy-under-0002", [sample, other_sample])]
    pairs = metrics_module._pairs(results, ANCHORS)
    axis = axis_expansion_gate_would_fire_rate(pairs)
    assert (axis.numerator, axis.denominator) == (1, 2)


def test_miss_rate_under_expansion_assumption_counts_expansion_gate_hits_as_misses() -> None:
    """전개가 항상 leg 을 낸다고 가정하면, 판정 True 라도 전개 게이트가 발동할 표본은
    "프로덕션에서는 결국 미탐이 아니게 될 수도 있었다"가 아니라 **상한 가정**으로는 미탐 취급이다
    (§D10.3 — 이 축은 상한이므로 분자를 늘리는 방향)."""
    expansion_decision = _blank_decision(case=3)
    expansion_hit = Sample(
        case_id="buy-under-0002",
        sample_index=0,
        decision=expansion_decision,
        prior=None,
        verdict=True,
        expansion_reason=detect_expansion_need(
            expansion_decision.category_queries, case=expansion_decision.case, unresolved=[]
        ),
        latency_ms=1,
    )
    clean_hit = _sample("buy-under-0002", verdict=True, index=1)  # expansion_reason=None
    real_miss = _sample("buy-under-0002", verdict=False, index=2)
    results = [_cell("buy-under-0002", [expansion_hit, clean_hit, real_miss])]
    pairs = metrics_module._pairs(results, ANCHORS)
    axis = axis_miss_rate_under_expansion_assumption(pairs)
    # 분자: real_miss(판정 False) + expansion_hit(판정 True ∧ expansion_reason 있음) = 2.
    # clean_hit 은 판정 True ∧ expansion_reason 없음이라 분자에서 빠진다.
    assert (axis.numerator, axis.denominator) == (2, 3)


# ─────────── Wilson CI 경계 ───────────


def test_wilson_ci_denominator_zero_is_undefined() -> None:
    assert wilson_ci(0, 0) is None


def test_wilson_ci_zero_numerator_lower_bound_is_zero() -> None:
    ci = wilson_ci(0, 20)
    assert ci is not None
    assert ci[0] == 0.0
    assert 0.0 < ci[1] < 1.0


def test_wilson_ci_full_numerator_upper_bound_is_at_most_one() -> None:
    ci = wilson_ci(20, 20)
    assert ci is not None
    assert ci[1] <= 1.0
    assert ci[0] > 0.5  # Wilson 보정이 있어도 하한이 1 근처로 붙어야 한다


# ─────────── belowSampleThreshold 라벨 ───────────


def test_below_sample_threshold_flips_at_forty() -> None:
    below = AxisResult(
        axis_id="x",
        title="t",
        numerator=1,
        denominator=39,
        definition_numerator="n",
        definition_denominator="d",
        nature="exploratory",
    )
    at_threshold = AxisResult(
        axis_id="x",
        title="t",
        numerator=1,
        denominator=40,
        definition_numerator="n",
        definition_denominator="d",
        nature="exploratory",
    )
    assert below.is_exploratory_by_sample_size is True
    assert at_threshold.is_exploratory_by_sample_size is False


# ─────────── 원인 축 분해(§D11) — ablation 이 실제로 원인을 짚는가 ───────────


def test_cause_axes_for_miss_points_to_the_single_filled_axis() -> None:
    decision = _blank_decision(filters=ProductSearchFilters(brand=["나이키"]))
    assert cause_axes_for_miss(decision, None, JUDGMENT) == ["filters.brand"]


def test_cause_axes_for_miss_is_multiple_when_two_axes_block_together() -> None:
    decision = _blank_decision(filters=ProductSearchFilters(brand=["나이키"], color="빨강"))
    assert cause_axes_for_miss(decision, None, JUDGMENT) == ["multiple"]


def test_nonblank_blocking_axes_lists_exactly_the_filled_axes() -> None:
    decision = _blank_decision(
        filters=ProductSearchFilters(rating_min=4.0), repurchase_products=["무선이어폰"]
    )
    assert nonblank_blocking_axes(decision) == sorted(["filters.rating_min", "repurchaseProducts"])


def test_sample_rows_computes_outcome_and_cause_axes_for_a_real_miss() -> None:
    decision = _blank_decision(filters=ProductSearchFilters(brand=["나이키"]))
    sample = _sample("buy-under-0002", verdict=False, decision=decision, index=0)
    results = [_cell("buy-under-0002", [sample])]
    rows = sample_rows(results, ANCHORS, JUDGMENT)
    assert rows[0]["outcome"] == "miss"
    assert rows[0]["causeAxes"] == ["filters.brand"]
    assert rows[0]["blockingAxes"] == ["filters.brand"]


def test_sample_rows_false_alarm_uses_anchor_reference_axes_as_cause() -> None:
    # buy-under-0004(what_axis, expectedReask=false, referenceAxes=["categoryQueries"]) — 판정이
    # True(오탐)로 나온 표본은 "채워졌어야 할 축"인 앵커의 referenceAxes 를 원인으로 싣는다.
    sample = _sample("buy-under-0004", verdict=True, index=0)
    results = [_cell("buy-under-0004", [sample])]
    rows = sample_rows(results, ANCHORS, JUDGMENT)
    assert rows[0]["outcome"] == "falseAlarm"
    assert rows[0]["causeAxes"] == ["categoryQueries"]


def test_cause_axis_summary_aggregates_outcomes_and_axis_counts() -> None:
    rows = [
        {"outcome": "miss", "causeAxes": ["filters.brand"], "blockingAxes": ["filters.brand"]},
        {
            "outcome": "miss",
            "causeAxes": ["multiple"],
            "blockingAxes": ["categoryQueries", "semanticQueryIsFallback"],
        },
        {
            "outcome": "miss",
            "causeAxes": ["multiple"],
            "blockingAxes": ["filters.attrConditions", "semanticQueryIsFallback"],
        },
        {"outcome": "falseAlarm", "causeAxes": ["categoryQueries"], "blockingAxes": []},
        {"outcome": "hit", "causeAxes": [], "blockingAxes": []},
        {"outcome": "correctReject", "causeAxes": [], "blockingAxes": []},
    ]
    summary = cause_axis_summary(rows)
    assert summary["outcomeCounts"] == {"correctReject": 1, "falseAlarm": 1, "hit": 1, "miss": 3}
    assert summary["missCauseAxisCounts"] == {"filters.brand": 1, "multiple": 2}
    assert summary["falseAlarmCauseAxisCounts"] == {"categoryQueries": 1}
    # [F-2] `multiple` 로 뭉개지는 두 미탐이 서로 다른 blockingAxes 조합이라는 것을 이 표가
    # 드러낸다 — SPEC/baseline README 가 "19건 전부 categoryQueries" 라고 잘못 서술했던 사고
    # (F-2, 2차 리뷰어 발견)를 재발 방지한다.
    assert summary["missBlockingAxisComboCounts"] == {
        "filters.brand": 1,
        "categoryQueries;semanticQueryIsFallback": 1,
        "filters.attrConditions;semanticQueryIsFallback": 1,
    }


# ─────────── D9 불변 재판정 — 합성 위반 주입으로 카운터 자체를 검증 ───────────
#
# `is_underspecified_turn` 은 flag off/ prior 게이트 둘 다 구조적으로 항상 False 를 반환하므로
# (첫 두 줄 조기 반환) 실제 위반 표본을 만들 수 없다 — 대신 재판정 함수를 몽키패치해 "위반이
# 있었다면 카운터가 실제로 세는가"를 검증한다(패킷 §D14 항목 7).


def test_flag_off_invariant_counts_a_synthetic_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    violating = _sample("buy-under-0002", verdict=True, index=0)
    clean = _sample("buy-under-0002", verdict=True, index=1)
    results = [_cell("buy-under-0002", [violating, clean])]
    pairs = metrics_module._pairs(results, ANCHORS)

    def _fake_reverdict(decision: RouteDecision, _prior: object) -> bool:
        return decision is violating.decision

    monkeypatch.setattr(metrics_module, "reverdict_with_flag_off", _fake_reverdict)
    axis = axis_flag_off_invariant(pairs)
    assert (axis.numerator, axis.denominator) == (1, 2)


def test_prior_gate_invariant_counts_a_synthetic_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    samples = [_sample("buy-under-0002", verdict=True, index=0)]
    results = [_cell("buy-under-0002", samples)]
    pairs = metrics_module._pairs(results, ANCHORS)
    monkeypatch.setattr(metrics_module, "reverdict_with_prior_gate", lambda _decision: True)
    axis = axis_prior_gate_invariant(pairs)
    assert (axis.numerator, axis.denominator) == (1, 1)


def test_invariants_are_zero_against_the_real_judgment_function() -> None:
    """[대조] 몽키패치 없이 실제 함수로 재면 항상 0/N 이어야 한다(구조적 보장의 실측 확인)."""
    samples = [_sample("buy-under-0002", verdict=True, index=i) for i in range(3)]
    results = [_cell("buy-under-0002", samples)]
    pairs = metrics_module._pairs(results, ANCHORS)
    assert axis_flag_off_invariant(pairs).numerator == 0
    assert axis_prior_gate_invariant(pairs).numerator == 0


# ─────────── trivial baseline(§D12) ───────────


def test_trivial_baseline_structural_values_are_one_and_zero() -> None:
    baseline = compute_baseline([], ANCHORS)
    assert baseline["missRate"] == 1.0
    assert baseline["falseAlarmRate"] == 0.0


def test_trivial_baseline_judgment_accuracy_hand_computed() -> None:
    # no_condition(expectedReask=true) 표본 2개 + what_axis(expectedReask=false) 표본 1개.
    # baseline 은 항상 False 를 내므로 expectedReask=false 표본(1개)만 맞춘다.
    samples_nc = [_sample("buy-under-0002", verdict=False, index=i) for i in range(2)]
    samples_wa = [_sample("buy-under-0004", verdict=False, index=0)]
    results = [_cell("buy-under-0002", samples_nc), _cell("buy-under-0004", samples_wa)]
    baseline = compute_baseline(results, ANCHORS)
    assert baseline["judgmentAccuracy"]["numerator"] == 1
    assert baseline["judgmentAccuracy"]["denominator"] == 3
    assert baseline["judgmentAccuracy"]["ratio"] == pytest.approx(1 / 3)
