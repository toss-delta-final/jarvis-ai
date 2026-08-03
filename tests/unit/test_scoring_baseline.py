from __future__ import annotations

import inspect
import math
import random
from datetime import datetime

import pytest

from app.agents.buyer.recommendation import graph as recommendation_graph
from app.services import spring_client
from evals.goldenset.loader import load_cases
from evals.metrics.settings import EvaluationSettings
from evals.metrics.runner import load_evaluation_fixtures
from evals.scoring.adapter import ScoringBuyerAdapter
from evals.scoring.adapter import weights_from_settings
from evals.scoring.components import (
    popularity_score,
    profile_match_score,
    recency_score,
    semantic_score,
)
from evals.scoring.hard_filter import HardConstraints, apply_hard_filters
from evals.scoring.scorer import ScoringWeights, score_products

_REAL_GET_RECENT_PURCHASES = spring_client.get_recent_purchases


def _product(
    product_id: int,
    *,
    category: str = "축구",
    brand: str = "브랜드",
    price: int = 10_000,
    rating: float = 4.0,
    reviews: int = 10,
) -> dict[str, object]:
    return {
        "productId": product_id,
        "categoryName": category,
        "brandName": brand,
        "price": price,
        "rating": rating,
        "reviewCount": reviews,
    }


def test_scoring_weights_and_score_products_require_explicit_weights() -> None:
    with pytest.raises(TypeError):
        ScoringWeights()
    assert inspect.signature(score_products).parameters["weights"].default is inspect.Parameter.empty


def test_components_are_bounded_and_report_degradation() -> None:
    rng = random.Random(20260803)
    for _ in range(100):
        query = [rng.uniform(-1, 1) for _ in range(8)]
        document = [rng.uniform(-1, 1) for _ in range(8)]
        assert 0 <= semantic_score(query, document).value <= 1
        assert 0 <= popularity_score(rng.randint(0, 1000), rng.uniform(0, 5), 1000).value <= 1

    assert semantic_score([1.0], None).degraded
    assert profile_match_score("축구", "브랜드", None).degraded
    assert recency_score(1, None).degraded


def test_hard_filters_cannot_be_bypassed_by_score_or_profile() -> None:
    products = [
        _product(1, price=999_999, category="축구"),
        _product(2, category="금지"),
        _product(3),
        _product(4),
        _product(5, price=1),
    ]
    constraints = HardConstraints(
        price_max=50_000,
        price_min=100,
        forbidden_categories=frozenset({"금지"}),
        forbidden_product_ids=frozenset({3}),
        must_exclude_product_ids=frozenset({4}),
    )

    first = apply_hard_filters(products, constraints)
    second = apply_hard_filters(products, constraints)

    assert first == second
    assert first.products == []
    assert {(cut.product_id, cut.reason) for cut in first.cuts} == {
        (1, "priceMax"),
        (2, "forbiddenCategory"),
        (3, "forbiddenProductId"),
        (4, "mustExclude"),
        (5, "priceMin"),
    }


def test_unknown_price_fails_closed_only_when_price_constraint_exists() -> None:
    unknown = _product(1)
    unknown["price"] = None
    nan = _product(2)
    nan["price"] = float("nan")

    constrained = apply_hard_filters(
        [unknown, nan], HardConstraints(price_max=50_000)
    )
    unconstrained = apply_hard_filters([unknown, nan], HardConstraints())

    assert constrained.products == []
    assert [(cut.product_id, cut.reason) for cut in constrained.cuts] == [
        (1, "priceUnknown"),
        (2, "priceUnknown"),
    ]
    assert unconstrained.products == [unknown, nan]


def test_scoring_adapter_pins_reference_clock_against_process_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = next(case for case in load_cases("dev") if case.case_id == "buy-pers-0001")
    fixtures = load_evaluation_fixtures()
    monkeypatch.setattr(
        spring_client, "get_recent_purchases", _REAL_GET_RECENT_PURCHASES
    )
    pinned = ScoringBuyerAdapter()(case, fixtures)["rankedProductIds"]

    monkeypatch.setattr(recommendation_graph, "_now", lambda: datetime(2030, 1, 1))
    future_process = ScoringBuyerAdapter()(case, fixtures)["rankedProductIds"]

    assert future_process == pinned


def test_score_is_reconstructable_and_ties_use_product_id() -> None:
    products = [_product(2), _product(1)]
    result = score_products(
        products,
        query_embedding=[1.0, 0.0],
        product_embeddings={1: [1.0, 0.0], 2: [1.0, 0.0]},
        weights=ScoringWeights(
            semantic=1.0,
            profile_match=0.0,
            popularity=0.0,
            recency=0.0,
            diversity_bonus=0.0,
            recent_purchase_penalty=0.0,
        ),
    )

    assert result.ranked_product_ids == [1, 2]
    for row in result.scores:
        reconstructed = sum(
            component.value * component.weight for component in row.components.values()
        )
        assert math.isclose(row.final_score, reconstructed, abs_tol=1e-12)


@pytest.mark.parametrize(
    ("weight", "products", "query", "embeddings", "preferences", "recency", "recent", "winner"),
    [
        ("semantic", [_product(1), _product(2)], [1.0, 0.0], {1: [-1, 0], 2: [1, 0]}, None, None, (), 2),
        (
            "profile_match",
            [_product(1, category="선크림"), _product(2, category="축구")],
            None,
            {},
            {"categories": {"축구": 1.0}, "brands": {}},
            None,
            (),
            2,
        ),
        (
            "popularity",
            [_product(1, rating=1, reviews=0), _product(2, rating=5, reviews=100)],
            None,
            {},
            None,
            None,
            (),
            2,
        ),
        ("recency", [_product(1), _product(2)], None, {}, None, {1: 0, 2: 1}, (), 2),
        (
            "recent_purchase_penalty",
            [_product(1), _product(2)],
            None,
            {},
            None,
            None,
            (1,),
            2,
        ),
    ],
)
def test_each_weight_changes_ranking(
    weight,
    products,
    query,
    embeddings,
    preferences,
    recency,
    recent,
    winner,
) -> None:
    setting_names = {
        "semantic": "scoring_weight_semantic",
        "profile_match": "scoring_weight_profile_match",
        "popularity": "scoring_weight_popularity",
        "recency": "scoring_weight_recency",
        "recent_purchase_penalty": "scoring_weight_recent_purchase_penalty",
    }
    zero_kwargs = {
        "scoring_weight_semantic": 0,
        "scoring_weight_profile_match": 0,
        "scoring_weight_popularity": 0,
        "scoring_weight_recency": 0,
        "scoring_weight_diversity_bonus": 0,
        "scoring_weight_recent_purchase_penalty": 0,
    }
    zero = ScoringWeights(0, 0, 0, 0, 0, 0)
    tuned_overrides = {setting_names[weight]: 1}
    if weight == "recent_purchase_penalty":
        # penalty-only 설정은 validator가 거부한다. 이 시나리오는 query embedding이 없어
        # semantic 값이 양 상품 모두 0으로 degrade되므로 양의 신호를 켜도 순위 효과는 penalty뿐이다.
        tuned_overrides["scoring_weight_semantic"] = 1
    tuned = weights_from_settings(
        EvaluationSettings(
            **{
                **zero_kwargs,
                **tuned_overrides,
            }
        )
    )

    baseline = score_products(
        products,
        query_embedding=query,
        product_embeddings=embeddings,
        profile_preferences=preferences,
        recency_by_product=recency,
        recent_product_ids=recent,
        weights=zero,
    )
    changed = score_products(
        products,
        query_embedding=query,
        product_embeddings=embeddings,
        profile_preferences=preferences,
        recency_by_product=recency,
        recent_product_ids=recent,
        weights=tuned,
    )

    assert changed.ranked_product_ids[0] == winner
    assert changed.ranked_product_ids != baseline.ranked_product_ids


def test_diversity_weight_changes_second_selection() -> None:
    products = [
        _product(1, category="가"),
        _product(2, category="가"),
        _product(3, category="나"),
    ]
    zero = ScoringWeights(0, 0, 0, 0, 0, 0)
    diversified = weights_from_settings(
        EvaluationSettings(
            scoring_weight_semantic=0,
            scoring_weight_profile_match=0,
            scoring_weight_popularity=0,
            scoring_weight_recency=0,
            scoring_weight_diversity_bonus=1,
            scoring_weight_recent_purchase_penalty=0,
        )
    )

    baseline = score_products(products, weights=zero)
    changed = score_products(products, weights=diversified)

    assert baseline.ranked_product_ids == [1, 2, 3]
    assert changed.ranked_product_ids == [1, 3, 2]
