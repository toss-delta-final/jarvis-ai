"""축별 필터 지표 순수 함수 테스트(#334) — 결정론, 실 LLM 없음."""

from __future__ import annotations

import pytest

from evals.filter_axes.metrics import (
    aggregate_axis_metrics,
    axis_outcome,
    axis_presence_set,
    case_axis_outcomes,
    judge_candidate_subset,
    judge_direction,
    judge_invariance,
    judge_profile_leak,
)
from evals.filter_axes.spec import load_axes_spec

SPEC = load_axes_spec()


# ── 정규화·outcome ──


def test_brand_string_and_single_element_list_match() -> None:
    assert axis_outcome("brand", {"brand": "Apple"}, {"brand": ["Apple"]}, SPEC) == "match"


def test_keyword_axis_accepts_semantic_query_as_presence() -> None:
    outcome = axis_outcome("keyword", {"keyword": "이어폰"}, {"semanticQuery": "이어폰"}, SPEC)
    assert outcome == "valueMismatch"


def test_keyword_axis_matches_only_when_both_keyword_fields_equal() -> None:
    outcome = axis_outcome("keyword", {"keyword": "이어폰"}, {"keyword": "이어폰"}, SPEC)
    assert outcome == "match"


def test_keyword_axis_missing_when_expected_has_no_keyword_or_semantic() -> None:
    assert axis_outcome("keyword", {}, {}, SPEC) == "bothEmpty"
    assert axis_outcome("keyword", {"keyword": "이어폰"}, {}, SPEC) == "missing"
    assert axis_outcome("keyword", {}, {"semanticQuery": "이어폰"}, SPEC) == "spurious"


@pytest.mark.parametrize(
    "filters",
    [
        {"keyword": "이어폰"},
        {"semanticQuery": "이어폰"},
        {"keyword": "이어폰", "semanticQuery": "무선 이어폰"},
    ],
    ids=["keyword_only", "semantic_only", "both"],
)
def test_keyword_axis_is_reflexive_against_itself(filters: dict) -> None:
    """리뷰 R3-1 — decompose의 semantic_query 폴백 때문에 semanticQuery만 있는 필터가
    흔한데, 옛 구현은 keyword↔keyword만 봐서 자기 자신과 비교해도 match가 아니었다
    (`axis_outcome("keyword", X, X) != "match"`, 비반사성 버그)."""
    assert axis_outcome("keyword", filters, filters, SPEC) == "match"


def test_keyword_axis_matches_via_semantic_query_equality_even_when_keyword_differs() -> None:
    expected = {"keyword": "이어폰", "semanticQuery": "무선 음향기기"}
    actual = {"keyword": "다른값", "semanticQuery": "무선 음향기기"}
    assert axis_outcome("keyword", expected, actual, SPEC) == "match"


def test_keyword_axis_cross_field_stays_value_mismatch_even_when_text_equal() -> None:
    """존재 흡수 규칙은 유지 — 정답 keyword vs 예측 semanticQuery처럼 필드가 다르면
    문자열이 같아도(리터럴 동일) valueMismatch다."""
    assert (
        axis_outcome("keyword", {"keyword": "이어폰"}, {"semanticQuery": "이어폰"}, SPEC)
        == "valueMismatch"
    )
    assert (
        axis_outcome("keyword", {"keyword": "이어폰"}, {"semanticQuery": "다른값"}, SPEC)
        == "valueMismatch"
    )


def test_string_normalization_is_case_and_whitespace_insensitive() -> None:
    assert axis_outcome("color", {"color": " Red "}, {"color": "red"}, SPEC) == "match"


def test_numeric_zero_counts_as_set() -> None:
    outcome = axis_outcome("rating_min", {"ratingMin": 0}, {}, SPEC)
    assert outcome == "missing"


def test_numeric_int_float_equivalence_matches() -> None:
    assert axis_outcome("price_max", {"priceMax": 5}, {"priceMax": 5.0}, SPEC) == "match"


def test_both_empty_axis_is_excluded_marker() -> None:
    assert axis_outcome("category", {}, {}, SPEC) == "bothEmpty"


def test_missing_is_expected_only_and_spurious_is_actual_only() -> None:
    assert axis_outcome("category", {"category": "가전"}, {}, SPEC) == "missing"
    assert axis_outcome("category", {}, {"category": "가전"}, SPEC) == "spurious"


def test_attr_conditions_dict_normalization() -> None:
    expected = {"attrConditions": {" 소재 ": "린넨"}}
    actual = {"attrConditions": {"소재": " 린넨 "}}
    assert axis_outcome("attr_conditions", expected, actual, SPEC) == "match"


def test_case_axis_outcomes_covers_all_evaluated_axes_only() -> None:
    outcomes = case_axis_outcomes({}, {}, SPEC)
    evaluated_axes = {
        axis for axis, axis_def in SPEC["axes"].items() if axis_def.get("evaluated", True)
    }
    assert set(outcomes) == evaluated_axes
    assert all(outcome == "bothEmpty" for outcome in outcomes.values())


# ── 집계 ──


def test_aggregate_denominator_zero_is_none_not_zero() -> None:
    aggregated = aggregate_axis_metrics([{"category": "bothEmpty"}])
    assert aggregated["category"]["valueStrict"]["precision"] is None
    assert aggregated["category"]["valueStrict"]["recall"] is None
    assert aggregated["category"]["presence"]["precision"] is None


def test_aggregate_micro_matches_hand_computation() -> None:
    outcomes = [
        {"category": "match"},
        {"category": "valueMismatch"},
        {"category": "spurious"},
        {"category": "missing"},
        {"category": "bothEmpty"},
    ]
    aggregated = aggregate_axis_metrics(outcomes)
    counts = aggregated["category"]["counts"]
    assert counts == {
        "match": 1,
        "valueMismatch": 1,
        "spurious": 1,
        "missing": 1,
        "bothEmpty": 1,
    }
    # valueStrict.precision = match / (match+mismatch+spurious) = 1/3
    assert aggregated["category"]["valueStrict"]["precision"] == pytest.approx(1 / 3)
    # valueStrict.recall = match / (match+mismatch+missing) = 1/3
    assert aggregated["category"]["valueStrict"]["recall"] == pytest.approx(1 / 3)
    # presence.precision = (match+mismatch) / (match+mismatch+spurious) = 2/3
    assert aggregated["category"]["presence"]["precision"] == pytest.approx(2 / 3)
    # presence.recall = (match+mismatch) / (match+mismatch+missing) = 2/3
    assert aggregated["category"]["presence"]["recall"] == pytest.approx(2 / 3)
    assert aggregated["category"]["support"] == 3


@pytest.mark.parametrize("seed", range(30))
def test_value_strict_precision_recall_never_exceed_presence(seed: int) -> None:
    import random

    rng = random.Random(seed)
    pool: list[str] = []
    for _ in range(rng.randint(1, 20)):
        pool.append(rng.choice(["match", "valueMismatch", "spurious", "missing", "bothEmpty"]))
    outcomes = [{"category": outcome} for outcome in pool]
    aggregated = aggregate_axis_metrics(outcomes)
    value_strict = aggregated["category"]["valueStrict"]
    presence = aggregated["category"]["presence"]
    for key in ("precision", "recall"):
        if value_strict[key] is not None and presence[key] is not None:
            assert value_strict[key] <= presence[key] + 1e-9


# ── INV/DIR/leak ──


def test_invariance_ok_when_axis_sets_and_numeric_values_equal() -> None:
    base = {"keyword": "이어폰", "priceMax": 50000}
    variant = {"keyword": "이어폰", "priceMax": 50000}
    result = judge_invariance(base, variant, SPEC)
    assert result == {"ok": True, "brokenAxes": []}


def test_invariance_flags_added_removed_and_value_changed_axes() -> None:
    base = {"keyword": "이어폰", "priceMax": 50000}
    variant = {"keyword": "이어폰", "priceMax": 40000, "color": "빨강"}
    result = judge_invariance(base, variant, SPEC)
    assert result["ok"] is False
    assert {"axis": "color", "kind": "added"} in result["brokenAxes"]
    assert {"axis": "price_max", "kind": "valueChanged"} in result["brokenAxes"]


def test_direction_ok_when_variant_superset_and_has_expected_new_axes() -> None:
    base = {"keyword": "이어폰"}
    variant = {"keyword": "이어폰", "priceMax": 50000}
    result = judge_direction(base, variant, ["price_max"], SPEC)
    assert result == {
        "ok": True,
        "violatedAxes": [],
        "lostAxes": [],
        "missingNewAxes": [],
    }


def test_direction_flags_lost_axis_and_missing_new_axis() -> None:
    base = {"keyword": "이어폰"}
    variant = {"color": "빨강"}
    result = judge_direction(base, variant, ["price_max"], SPEC)
    assert result["ok"] is False
    assert result["lostAxes"] == ["keyword"]
    assert result["missingNewAxes"] == ["price_max"]


def test_profile_leak_reproduces_issue_119() -> None:
    guest = {"keyword": "이어폰"}
    member = {"keyword": "이어폰", "priceMax": 50000}
    result = judge_profile_leak(guest, member, SPEC)
    assert result == {"leak": True, "leakedAxes": ["price_max"], "lostAxes": []}


def test_profile_leak_ok_when_axis_sets_equal() -> None:
    guest = {"keyword": "이어폰"}
    member = {"keyword": "이어폰"}
    result = judge_profile_leak(guest, member, SPEC)
    assert result == {"leak": False, "leakedAxes": [], "lostAxes": []}


def test_profile_leak_true_even_when_not_a_proper_superset() -> None:
    """리뷰 F1 재현 — member 가 guest 축을 잃으면서 다른 축을 새로 얻으면 진상위집합이 아니라서
    옛 구현(guest_set < member_set)은 leak 을 놓쳤다(false negative). leakedAxes 가 있으면
    lostAxes 와 무관하게 항상 leak=true 여야 한다."""
    guest = {"keyword": "이어폰", "category": "가전"}
    member = {"keyword": "이어폰", "priceMax": 50000}
    result = judge_profile_leak(guest, member, SPEC)
    assert result == {
        "leak": True,
        "leakedAxes": ["price_max"],
        "lostAxes": ["category"],
    }


def test_profile_leak_false_when_member_only_loses_axes() -> None:
    guest = {"keyword": "이어폰", "category": "가전"}
    member = {"keyword": "이어폰"}
    result = judge_profile_leak(guest, member, SPEC)
    assert result == {"leak": False, "leakedAxes": [], "lostAxes": ["category"]}


def test_candidate_subset_ok_when_variant_is_subset() -> None:
    assert judge_candidate_subset([1, 2, 3], [1, 2]) == {"ok": True, "extraIds": []}


def test_candidate_subset_flags_extra_ids() -> None:
    assert judge_candidate_subset([1, 2], [1, 2, 3]) == {"ok": False, "extraIds": [3]}


def test_axis_presence_set_includes_non_evaluated_axes() -> None:
    filters = {"totalBudget": 50000, "buyAll": True, "excludeProductIds": [1]}
    present = axis_presence_set(filters, SPEC)
    assert present == {"total_budget", "buy_all", "exclude_product_ids"}


# ── axes.json 정합성(드리프트 감지) ──


def test_axes_spec_covers_decompose_filter_axes_and_extra_axes() -> None:
    from app.agents.buyer.recommendation.decompose import _FILTER_AXES

    axis_names = set(SPEC["axes"])
    expected = set(_FILTER_AXES) | {"exclude_product_ids", "total_budget", "buy_all"}
    assert expected <= axis_names, (
        "decompose._FILTER_AXES 또는 확장 축이 axes.json 축 이름 집합에서 빠졌다 — "
        "app 쪽 하드필터 축이 늘면 이 테스트가 깨져 드리프트를 알린다."
    )


def test_axes_spec_keyword_axis_covers_semantic_query_field() -> None:
    assert "semanticQuery" in SPEC["axes"]["keyword"]["fields"]


def test_axes_spec_limit_is_excluded_not_an_axis() -> None:
    assert "limit" not in SPEC["axes"]
    assert "limit" in SPEC["excludedFields"]
