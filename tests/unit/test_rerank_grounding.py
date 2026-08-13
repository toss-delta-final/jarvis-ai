from __future__ import annotations

import json

import pytest

from app.agents.buyer.recommendation.rerank_grounding import (
    NEUTRAL_RATIONALE,
    CandidateGroundingFacts,
    validate_and_render_grounding,
)
from app.schemas.spring import SpringProduct


def _facts(**changes: object) -> CandidateGroundingFacts:
    values: dict[str, object] = {
        "product_id": 101,
        "rating_level": "높음",
        "review_level": "많음",
        "price_level": "저렴",
    }
    values.update(changes)
    return CandidateGroundingFacts(**values)  # type: ignore[arg-type]


def _item(
    *,
    code: str,
    field: str | None,
    rationale: str = "model rationale",
    product_id: object = 101,
) -> dict[str, object]:
    return {
        "productId": product_id,
        "rationale": rationale,
        "reasonCode": code,
        "evidenceFields": [] if field is None else [field],
    }


def test_rating_high_requires_exact_field_and_supported_tier() -> None:
    decision = validate_and_render_grounding(
        _item(
            code="RATING_HIGH",
            field="ratingLevel",
            rationale="평점 평가가 높은 상품이에요",
        ),
        _facts(),
    )

    assert decision.supported is True
    assert decision.downgraded is False
    assert decision.rendered_rationale == "평점 평가가 높은 상품이에요"
    assert decision.failure_reason is None


def test_rating_high_downgrades_missing_rating_without_dropping_id() -> None:
    decision = validate_and_render_grounding(
        _item(code="RATING_HIGH", field="ratingLevel", rationale="평점이 아주 높아요"),
        _facts(rating_level="평가없음"),
    )

    assert decision.product_id == 101
    assert decision.supported is False
    assert decision.downgraded is True
    assert decision.rendered_rationale == NEUTRAL_RATIONALE
    assert decision.failure_reason == "candidate_tier_not_supported"


def test_reason_code_rejects_wrong_evidence_field() -> None:
    decision = validate_and_render_grounding(
        _item(code="REVIEW_MANY", field="ratingLevel", rationale="리뷰가 많아요"),
        _facts(),
    )

    assert decision.supported is False
    assert decision.failure_reason == "evidence_fields_mismatch"
    assert decision.rendered_rationale == NEUTRAL_RATIONALE


def test_no_verifiable_evidence_is_a_supported_neutral_result() -> None:
    decision = validate_and_render_grounding(
        _item(
            code="NO_VERIFIABLE_EVIDENCE",
            field=None,
            rationale=NEUTRAL_RATIONALE,
        ),
        _facts(
            rating_level="평가없음",
            review_level="정보없음",
            price_level="정보없음",
        ),
    )

    assert decision.supported is True
    assert decision.downgraded is False
    assert decision.rendered_rationale == NEUTRAL_RATIONALE


@pytest.mark.parametrize(
    ("code", "field", "fact_name", "allowed", "rejected", "expected_rationale"),
    [
        (
            "RATING_HIGH",
            "ratingLevel",
            "rating_level",
            ("높음", "매우높음"),
            "보통",
            "평점 평가가 높은 상품이에요",
        ),
        (
            "REVIEW_MANY",
            "reviewLevel",
            "review_level",
            ("많음", "매우많음"),
            "적음",
            "리뷰 정보가 많은 상품이에요",
        ),
        (
            "PRICE_RELATIVE_LOW",
            "priceLevel",
            "price_level",
            ("저렴", "매우저렴"),
            "보통",
            "같은 후보군에서 비교적 저렴해요",
        ),
    ],
)
def test_reason_code_tier_boundaries(
    code: str,
    field: str,
    fact_name: str,
    allowed: tuple[str, ...],
    rejected: str,
    expected_rationale: str,
) -> None:
    for value in allowed:
        decision = validate_and_render_grounding(
            _item(code=code, field=field),
            _facts(**{fact_name: value}),
        )
        assert decision.supported is True
        assert decision.rendered_rationale == expected_rationale

    decision = validate_and_render_grounding(
        _item(code=code, field=field),
        _facts(**{fact_name: rejected}),
    )
    assert decision.supported is False
    assert decision.rendered_rationale == NEUTRAL_RATIONALE


@pytest.mark.parametrize(
    ("item", "failure_reason"),
    [
        (_item(code="MADE_UP", field="ratingLevel"), "unknown_reason_code"),
        (_item(code="RATING_HIGH", field="ratingLevel", product_id=True), "invalid_product_id"),
        (
            {
                "productId": 101,
                "rationale": "model",
                "reasonCode": "RATING_HIGH",
                "evidenceFields": "ratingLevel",
            },
            "invalid_evidence_fields",
        ),
    ],
)
def test_malformed_evidence_downgrades_safely(item: dict[str, object], failure_reason: str) -> None:
    decision = validate_and_render_grounding(item, _facts())

    assert decision.supported is False
    assert decision.downgraded is True
    assert decision.rendered_rationale == NEUTRAL_RATIONALE
    assert decision.failure_reason == failure_reason


_MISSING = object()


class _StructuredLLM:
    def __init__(
        self,
        ranked: list[dict[str, object]],
        *,
        overall_claims: object = _MISSING,
    ) -> None:
        self.ranked = ranked
        self.overall_claims = overall_claims
        self.systems: list[str] = []
        self.max_tokens: list[int] = []

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        self.systems.append(system)
        self.max_tokens.append(max_tokens)
        payload: dict[str, object] = {
            "ranked": self.ranked,
            "overallComment": "골라봤어요",
        }
        if self.overall_claims is not _MISSING:
            payload["overallClaims"] = self.overall_claims
        return json.dumps(payload, ensure_ascii=False)


async def _call_rerank(
    llm: _StructuredLLM,
    *,
    grounding_arm: str,
    rating: float | None = None,
    review_count: int | None = 0,
):
    from app.agents.buyer.recommendation.rerank import rerank

    return await rerank(
        llm,
        query="q",
        candidates=[
            SpringProduct(
                product_id=101,
                name="p",
                price=10000,
                rating=rating,
                review_count=review_count,
                category="c",
            )
        ],
        profile_summary=None,
        tier="smart",
        expose_max=1,
        grounding_arm=grounding_arm,
    )


async def test_current_arm_keeps_legacy_prompt_and_rationale() -> None:
    from app.agents.buyer.recommendation.rerank import _SYSTEM, rerank

    llm = _StructuredLLM([{"productId": 101, "rationale": "legacy"}])
    result = await rerank(
        llm,
        query="q",
        candidates=[SpringProduct(product_id=101, name="p")],
        profile_summary=None,
        tier="smart",
        expose_max=1,
    )

    assert llm.systems == [_SYSTEM]
    assert result.ranked == [(101, "legacy")]
    assert result.grounding_decisions == []
    assert result.ranking_decisions == []
    assert result.overall_claims == ()
    assert "overallClaims" not in _SYSTEM


async def test_prompt_only_preserves_model_rationale_when_metadata_is_invalid() -> None:
    llm = _StructuredLLM(
        [
            {
                "productId": 101,
                "rationale": "평점이 아주 높아요",
                "reasonCode": "RATING_HIGH",
                "evidenceFields": ["ratingLevel"],
            }
        ]
    )

    result = await _call_rerank(llm, grounding_arm="prompt_only")

    assert result.ranked == [(101, "평점이 아주 높아요")]
    assert result.grounding_decisions[0].supported is False
    assert result.grounding_decisions[0].failure_reason == "candidate_tier_not_supported"


async def test_validated_replaces_invalid_model_rationale_with_neutral_template() -> None:
    llm = _StructuredLLM(
        [
            {
                "productId": 101,
                "rationale": "평점이 아주 높아요",
                "reasonCode": "RATING_HIGH",
                "evidenceFields": ["ratingLevel"],
            }
        ]
    )

    result = await _call_rerank(llm, grounding_arm="validated")

    assert result.ranked == [(101, NEUTRAL_RATIONALE)]
    assert result.grounding_decisions[0].downgraded is True


async def test_validated_uses_template_for_supported_evidence() -> None:
    llm = _StructuredLLM(
        [
            {
                "productId": 101,
                "rationale": "모델이 쓴 다른 문장",
                "reasonCode": "RATING_HIGH",
                "evidenceFields": ["ratingLevel"],
            }
        ]
    )

    result = await _call_rerank(
        llm,
        grounding_arm="validated",
        rating=4.8,
        review_count=120,
    )

    assert result.ranked == [(101, "평점 평가가 높은 상품이에요")]
    assert result.grounding_decisions[0].supported is True


async def test_validated_keeps_candidate_when_only_evidence_is_invalid() -> None:
    llm = _StructuredLLM(
        [
            {
                "productId": 101,
                "rationale": "리뷰가 많아요",
                "reasonCode": "REVIEW_MANY",
                "evidenceFields": ["ratingLevel"],
            }
        ]
    )

    result = await _call_rerank(llm, grounding_arm="validated")

    assert [product_id for product_id, _ in result.ranked] == [101]
    assert result.ranked[0][1] == NEUTRAL_RATIONALE


async def test_structured_prompt_declares_exact_grounding_contract() -> None:
    from app.agents.buyer.recommendation.rerank import _SYSTEM_STRUCTURED_GROUNDING

    for value in (
        "RATING_HIGH",
        "REVIEW_MANY",
        "PRICE_RELATIVE_LOW",
        "NO_VERIFIABLE_EVIDENCE",
        "rationale",
        "후보 목록(CANDIDATES)",
        "overallClaims",
        "TOP_REVIEW_COUNT",
        "ALL_RATING_HIGH",
        "ALL_WITHIN_TOTAL_BUDGET",
        "NO_VERIFIABLE_OVERALL_CLAIM",
        "scope",
        "subjectProductIds",
    ):
        assert value in _SYSTEM_STRUCTURED_GROUNDING


@pytest.mark.parametrize("grounding_arm", ["prompt_only", "validated"])
async def test_structured_arms_preserve_raw_overall_claims(grounding_arm: str) -> None:
    proposal = {
        "claimCode": "ALL_RATING_HIGH",
        "scope": "FINAL_EXPOSED_PRODUCTS",
        "subjectProductIds": [101],
        "evidenceFields": ["ratingLevel"],
    }
    llm = _StructuredLLM(
        [
            {
                "productId": 101,
                "rationale": "평점이 높아요",
                "reasonCode": "RATING_HIGH",
                "evidenceFields": ["ratingLevel"],
            }
        ],
        overall_claims=[proposal],
    )

    result = await _call_rerank(
        llm,
        grounding_arm=grounding_arm,
        rating=4.8,
        review_count=10,
    )

    assert result.overall_claims == (proposal,)
    assert result.overall_comment == "골라봤어요"


@pytest.mark.parametrize(
    ("raw_claims", "sentinel_value"),
    [
        ("not-an-array", "not-an-array"),
        (["not-an-object"], "not-an-object"),
    ],
)
async def test_structured_arms_preserve_invalid_overall_shape_for_validator(
    raw_claims: object, sentinel_value: object
) -> None:
    llm = _StructuredLLM(
        [
            {
                "productId": 101,
                "rationale": "중립",
                "reasonCode": "NO_VERIFIABLE_EVIDENCE",
                "evidenceFields": [],
            }
        ],
        overall_claims=raw_claims,
    )

    result = await _call_rerank(llm, grounding_arm="validated")

    assert result.overall_claims == ({"__invalidShape": sentinel_value},)
