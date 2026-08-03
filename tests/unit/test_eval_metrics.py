"""구매자 추천 평가 지표의 손계산·경계값 테스트."""

from __future__ import annotations

import math
import random

import pytest

from evals.metrics.metrics import (
    catalog_coverage,
    diversity,
    filter_accuracy,
    hard_constraint_violations,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    unique_ranked_ids,
)


def test_ranking_metrics_match_hand_calculation() -> None:
    ranked = [9, 2, 3, 1]
    relevant = {1, 2}
    grades = {1: 3, 2: 1}

    assert precision_at_k(ranked, relevant, 3) == pytest.approx(1 / 3)
    assert recall_at_k(ranked, relevant, 3) == pytest.approx(1 / 2)
    assert mean_reciprocal_rank(ranked, relevant) == pytest.approx(1 / 2)
    assert ndcg_at_k(ranked, grades, 3) == pytest.approx(
        (1 / math.log2(3)) / (3 + 1 / math.log2(3))
    )


def test_precision_uses_k_as_denominator_for_short_results() -> None:
    assert precision_at_k([1], {1}, 5) == pytest.approx(0.2)
    assert recall_at_k([1], {1, 2}, 5) == pytest.approx(0.5)


@pytest.mark.parametrize("metric", [precision_at_k, recall_at_k, ndcg_at_k])
def test_k_must_be_positive(metric) -> None:
    with pytest.raises(ValueError, match="K"):
        metric([], {}, 0)


def test_duplicate_ids_count_once_and_preserve_first_position() -> None:
    unique, duplicate_count = unique_ranked_ids([2, 2, 1, 2, 3])
    assert unique == [2, 1, 3]
    assert duplicate_count == 2
    assert precision_at_k([2, 2, 1], {1, 2}, 3) == pytest.approx(2 / 3)
    assert mean_reciprocal_rank([9, 2, 2, 1], {1}) == pytest.approx(1 / 3)


def test_ndcg_supports_binary_graded_unknown_empty_and_ties() -> None:
    assert ndcg_at_k([2, 1], {1: 1, 2: 1}, 2) == 1.0
    assert ndcg_at_k([20, 10], {10: 3, 20: 1}, 2) < 1.0
    assert ndcg_at_k([999, 10], {10: 3}, 2) < 1.0
    assert ndcg_at_k([], {}, 5) is None
    assert ndcg_at_k([2, 1], {1: 2, 2: 2}, 2) == 1.0


def test_perfect_reversed_partial_and_empty_rankings_have_expected_order() -> None:
    grades = {1: 3, 2: 2, 3: 1}
    perfect = ndcg_at_k([1, 2, 3], grades, 3)
    partial = ndcg_at_k([1, 2], grades, 3)
    reversed_score = ndcg_at_k([3, 2, 1], grades, 3)

    assert perfect == 1.0
    assert perfect > partial > reversed_score > 0.0
    assert precision_at_k([], {1}, 5) == 0.0
    assert recall_at_k([], {1}, 5) == 0.0
    assert mean_reciprocal_rank([], {1}) == 0.0
    assert ndcg_at_k([], grades, 5) == 0.0


def test_filter_accuracy_penalizes_missing_and_extra_fields() -> None:
    expected = {"keyword": "텐트", "priceMax": 100_000}
    assert filter_accuracy(expected, expected) == 1.0
    assert filter_accuracy(expected, {"keyword": "텐트", "brand": ["A"]}) == pytest.approx(1 / 3)
    assert filter_accuracy({}, {}) == 1.0


def test_hard_constraints_report_every_violation() -> None:
    catalog = {
        1: {"productId": 1, "price": 50, "categoryName": "허용"},
        2: {"productId": 2, "price": 200, "categoryName": "금지"},
    }
    hard = {
        "priceMax": 100,
        "priceMin": 60,
        "forbiddenCategories": ["금지"],
        "forbiddenProductIds": [2],
    }
    violations = hard_constraint_violations([1, 2, 999], hard, [1], catalog)
    assert {(row["productId"], row["constraint"]) for row in violations} == {
        (1, "priceMin"),
        (1, "mustExclude"),
        (2, "priceMax"),
        (2, "forbiddenCategory"),
        (2, "forbiddenProductId"),
    }


def test_coverage_and_diversity_use_declared_denominators() -> None:
    catalog = {
        1: {"categoryName": "A"},
        2: {"categoryName": "A"},
        3: {"categoryName": "B"},
    }
    assert catalog_coverage([[1, 2], [2, 3]], {1, 2, 3, 4}) == pytest.approx(0.75)
    assert diversity([1, 2, 3], catalog) == pytest.approx(2 / 3)
    assert diversity([], catalog) == 0.0


def test_all_bounded_metrics_remain_between_zero_and_one() -> None:
    rng = random.Random(20260803)
    universe = list(range(1, 31))
    for _ in range(200):
        ranked = [rng.choice(universe) for _ in range(rng.randint(0, 40))]
        relevant = set(rng.sample(universe, rng.randint(0, 10)))
        grades = {product_id: rng.randint(0, 3) for product_id in relevant}
        k = rng.randint(1, 20)
        values = [
            precision_at_k(ranked, relevant, k),
            recall_at_k(ranked, relevant, k),
            mean_reciprocal_rank(ranked, relevant),
            ndcg_at_k(ranked, grades, k),
        ]
        assert all(value is None or 0.0 <= value <= 1.0 for value in values)
