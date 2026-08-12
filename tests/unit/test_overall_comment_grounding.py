from __future__ import annotations

import pytest

from app.agents.buyer.recommendation.overall_comment_grounding import (
    NEUTRAL_OVERALL_COMMENT,
    FinalRecommendationView,
    validate_and_render_overall_comment,
)
from app.core.config import get_settings
from app.schemas.spring import SpringProduct


def _product(
    product_id: int,
    *,
    price: int | None = 10_000,
    rating: float | None = 4.8,
    review_count: int | None = 100,
) -> SpringProduct:
    return SpringProduct(
        product_id=product_id,
        name=f"p{product_id}",
        price=price,
        rating=rating,
        review_count=review_count,
    )


def _products(*products: SpringProduct) -> dict[int, SpringProduct]:
    return {product.product_id: product for product in products}


def _view(
    *groups: tuple[int, ...],
    list_type: str = "PICK_ONE",
    total_budget: int | None = None,
) -> FinalRecommendationView:
    return FinalRecommendationView(  # type: ignore[arg-type]
        list_type=list_type,
        total_budget=total_budget,
        product_groups=groups,
    )


def _claim(
    code: str,
    *,
    scope: str,
    subjects: list[int],
    evidence: list[str],
) -> dict[str, object]:
    return {
        "claimCode": code,
        "scope": scope,
        "subjectProductIds": subjects,
        "evidenceFields": evidence,
    }


def _validate(
    proposals: list[dict[str, object]],
    *,
    view: FinalRecommendationView | None = None,
    products: dict[int, SpringProduct] | None = None,
):
    return validate_and_render_overall_comment(
        proposals,
        final_view=view or _view((1, 2)),
        products_by_id=products or _products(_product(1), _product(2, review_count=50)),
        settings=get_settings(),
    )


@pytest.mark.parametrize(
    ("proposal", "failure_reason"),
    [
        ({"__invalidShape": "text"}, "invalid_claim_shape"),
        (
            _claim(
                "POPULARITY_TOP",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[1],
                evidence=["popularity"],
            ),
            "unknown_claim_code",
        ),
        (
            _claim(
                "TOP_REVIEW_COUNT",
                scope="FINAL_RECOMMENDATION_LISTS",
                subjects=[1],
                evidence=["reviewCount"],
            ),
            "scope_mismatch",
        ),
        (
            _claim(
                "TOP_REVIEW_COUNT",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[1],
                evidence=["reviewLevel"],
            ),
            "evidence_fields_mismatch",
        ),
        (
            _claim(
                "TOP_REVIEW_COUNT",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[2],
                evidence=["reviewCount"],
            ),
            "subject_ids_mismatch",
        ),
        (
            _claim(
                "TOP_REVIEW_COUNT",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[999],
                evidence=["reviewCount"],
            ),
            "subject_outside_final_view",
        ),
    ],
)
def test_invalid_claim_shape_downgrades_to_neutral(
    proposal: dict[str, object], failure_reason: str
) -> None:
    decision = _validate([proposal])

    assert decision.supported_claim_codes == ()
    assert decision.rendered_comment == NEUTRAL_OVERALL_COMMENT
    assert decision.downgraded is True
    assert failure_reason in decision.failure_reasons


def test_duplicate_claim_code_downgrades_to_neutral() -> None:
    proposal = _claim(
        "TOP_REVIEW_COUNT",
        scope="FINAL_EXPOSED_PRODUCTS",
        subjects=[1],
        evidence=["reviewCount"],
    )

    decision = _validate([proposal, proposal])

    assert decision.supported_claim_codes == ()
    assert decision.failure_reasons == ("duplicate_claim_code",)
    assert decision.rendered_comment == NEUTRAL_OVERALL_COMMENT


def test_neutral_claim_cannot_coexist_with_factual_claim() -> None:
    neutral = _claim(
        "NO_VERIFIABLE_OVERALL_CLAIM",
        scope="FINAL_EXPOSED_PRODUCTS",
        subjects=[],
        evidence=[],
    )
    factual = _claim(
        "TOP_REVIEW_COUNT",
        scope="FINAL_EXPOSED_PRODUCTS",
        subjects=[1],
        evidence=["reviewCount"],
    )

    decision = _validate([neutral, factual])

    assert decision.supported_claim_codes == ()
    assert decision.failure_reasons == ("neutral_claim_conflict",)


def test_more_than_two_claims_downgrades_to_neutral() -> None:
    decision = _validate(
        [
            _claim(
                "TOP_REVIEW_COUNT",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[1],
                evidence=["reviewCount"],
            ),
            _claim(
                "ALL_RATING_HIGH",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[1, 2],
                evidence=["ratingLevel"],
            ),
            _claim(
                "NO_VERIFIABLE_OVERALL_CLAIM",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[],
                evidence=[],
            ),
        ]
    )

    assert decision.supported_claim_codes == ()
    assert decision.failure_reasons == ("too_many_claims",)


def test_unique_top_review_count_is_supported() -> None:
    decision = _validate(
        [
            _claim(
                "TOP_REVIEW_COUNT",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[1],
                evidence=["reviewCount"],
            )
        ]
    )

    assert decision.supported_claim_codes == ("TOP_REVIEW_COUNT",)
    assert decision.rendered_comment == "리뷰 수가 가장 많은 상품부터 보여드렸어요."
    assert decision.downgraded is False


def test_tied_top_review_count_is_supported() -> None:
    decision = _validate(
        [
            _claim(
                "TOP_REVIEW_COUNT",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[1],
                evidence=["reviewCount"],
            )
        ],
        products=_products(_product(1, review_count=100), _product(2, review_count=100)),
    )

    assert decision.supported_claim_codes == ("TOP_REVIEW_COUNT",)


def test_missing_review_count_blocks_top_claim() -> None:
    decision = _validate(
        [
            _claim(
                "TOP_REVIEW_COUNT",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[1],
                evidence=["reviewCount"],
            )
        ],
        products=_products(_product(1), _product(2, review_count=None)),
    )

    assert decision.supported_claim_codes == ()
    assert decision.failure_reasons == ("missing_candidate_fact",)


def test_all_final_ratings_high_is_supported() -> None:
    decision = _validate(
        [
            _claim(
                "ALL_RATING_HIGH",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[1, 2],
                evidence=["ratingLevel"],
            )
        ]
    )

    assert decision.supported_claim_codes == ("ALL_RATING_HIGH",)
    assert decision.rendered_comment == "평점 정보가 높은 상품들만 골랐어요."


@pytest.mark.parametrize(
    "unsupported",
    [
        _product(2, rating=None, review_count=10),
        _product(2, rating=4.9, review_count=0),
        _product(2, rating=3.0, review_count=10),
    ],
)
def test_missing_or_low_rating_blocks_all_high(unsupported: SpringProduct) -> None:
    decision = _validate(
        [
            _claim(
                "ALL_RATING_HIGH",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[1, 2],
                evidence=["ratingLevel"],
            )
        ],
        products=_products(_product(1), unsupported),
    )

    assert decision.supported_claim_codes == ()
    assert decision.failure_reasons == ("candidate_fact_not_supported",)


def test_rating_claim_ignores_candidate_excluded_from_final_view() -> None:
    decision = _validate(
        [
            _claim(
                "ALL_RATING_HIGH",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[1, 2],
                evidence=["ratingLevel"],
            )
        ],
        products=_products(_product(1), _product(2), _product(3, rating=2.0)),
    )

    assert decision.supported_claim_codes == ("ALL_RATING_HIGH",)


def test_each_buy_all_group_is_checked_independently() -> None:
    decision = _validate(
        [
            _claim(
                "ALL_WITHIN_TOTAL_BUDGET",
                scope="FINAL_RECOMMENDATION_LISTS",
                subjects=[1, 2, 3, 4],
                evidence=["price", "totalBudget"],
            )
        ],
        view=_view((1, 2), (3, 4), list_type="BUY_ALL", total_budget=25_000),
        products=_products(
            _product(1, price=10_000),
            _product(2, price=12_000),
            _product(3, price=11_000),
            _product(4, price=13_000),
        ),
    )

    assert decision.supported_claim_codes == ("ALL_WITHIN_TOTAL_BUDGET",)
    assert decision.rendered_comment == "각 추천 조합이 모두 예산 안에 들어와요."


@pytest.mark.parametrize(
    ("view", "products", "failure_reason"),
    [
        (
            _view((1, 2), list_type="BUY_ALL", total_budget=15_000),
            _products(_product(1, price=10_000), _product(2, price=10_000)),
            "candidate_fact_not_supported",
        ),
        (
            _view((1, 2), list_type="BUY_ALL", total_budget=30_000),
            _products(_product(1, price=10_000), _product(2, price=None)),
            "missing_candidate_fact",
        ),
        (
            _view((1, 2), list_type="PICK_ONE", total_budget=30_000),
            _products(_product(1), _product(2)),
            "budget_context_not_supported",
        ),
    ],
)
def test_budget_claim_requires_supported_final_context(
    view: FinalRecommendationView,
    products: dict[int, SpringProduct],
    failure_reason: str,
) -> None:
    decision = _validate(
        [
            _claim(
                "ALL_WITHIN_TOTAL_BUDGET",
                scope="FINAL_RECOMMENDATION_LISTS",
                subjects=[1, 2],
                evidence=["price", "totalBudget"],
            )
        ],
        view=view,
        products=products,
    )

    assert decision.supported_claim_codes == ()
    assert decision.failure_reasons == (failure_reason,)


@pytest.mark.parametrize("unsupported_code", ["POPULARITY_TOP", "VALUE_FOR_MONEY_TOP"])
def test_unsupported_superlative_codes_downgrade(unsupported_code: str) -> None:
    decision = _validate(
        [
            _claim(
                unsupported_code,
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[1],
                evidence=[],
            )
        ]
    )

    assert decision.supported_claim_codes == ()
    assert decision.rendered_comment == NEUTRAL_OVERALL_COMMENT
    assert decision.failure_reasons == ("unknown_claim_code",)


def test_supported_templates_use_fixed_priority_and_two_claim_cap() -> None:
    rating = _claim(
        "ALL_RATING_HIGH",
        scope="FINAL_EXPOSED_PRODUCTS",
        subjects=[1, 2],
        evidence=["ratingLevel"],
    )
    budget = _claim(
        "ALL_WITHIN_TOTAL_BUDGET",
        scope="FINAL_RECOMMENDATION_LISTS",
        subjects=[1, 2],
        evidence=["price", "totalBudget"],
    )

    decision = _validate(
        [rating, budget],
        view=_view((1, 2), list_type="BUY_ALL", total_budget=30_000),
    )

    assert decision.supported_claim_codes == ("ALL_WITHIN_TOTAL_BUDGET", "ALL_RATING_HIGH")
    assert decision.rendered_comment == (
        "각 추천 조합이 모두 예산 안에 들어와요. 평점 정보가 높은 상품들만 골랐어요."
    )


def test_supported_neutral_claim_renders_neutral_without_downgrade() -> None:
    decision = _validate(
        [
            _claim(
                "NO_VERIFIABLE_OVERALL_CLAIM",
                scope="FINAL_EXPOSED_PRODUCTS",
                subjects=[],
                evidence=[],
            )
        ]
    )

    assert decision.supported_claim_codes == ("NO_VERIFIABLE_OVERALL_CLAIM",)
    assert decision.rendered_comment == NEUTRAL_OVERALL_COMMENT
    assert decision.downgraded is False
    assert decision.failure_reasons == ()
