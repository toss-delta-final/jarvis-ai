from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest

from app.agents.buyer.recommendation.rerank_grounding import NEUTRAL_RATIONALE
from app.core.config import get_settings
from app.core.llm import LLMError
from app.schemas.spring import SpringProduct


class _PayloadLLM:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self.payload = payload
        self.systems: list[str] = []
        self.users: list[str] = []
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
        self.users.append(user)
        self.max_tokens.append(max_tokens)
        return json.dumps(self.payload, ensure_ascii=False)


def _evaluation(
    product_id: int,
    intent_fit: int,
    need_fit: int,
    profile_fit: int,
    *,
    invalid_reason: bool = False,
) -> dict[str, object]:
    return {
        "productId": product_id,
        "intentFit": intent_fit,
        "needFit": need_fit,
        "profileFit": profile_fit,
        "rationale": f"상품 {product_id} 모델 근거",
        "reasonCode": "REVIEW_MANY" if invalid_reason else "RATING_HIGH",
        "evidenceFields": ["ratingLevel"],
    }


def _scored_payload(
    *rows: tuple[int, int, int, int],
    invalid_reason_for: int | None = None,
    overall_claims: object | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "evaluations": [
            _evaluation(
                product_id,
                intent_fit,
                need_fit,
                profile_fit,
                invalid_reason=product_id == invalid_reason_for,
            )
            for product_id, intent_fit, need_fit, profile_fit in rows
        ],
        "overallComment": "골라봤어요",
    }
    if overall_claims is not None:
        payload["overallClaims"] = overall_claims
    return payload


async def _call_scored(
    payload: Mapping[str, object],
    *,
    ranking_arm: str,
    grounding_arm: str = "validated",
    expose_max: int,
    candidate_ids: Sequence[int] = (101, 102),
    profile_summary: str | None = None,
    search_rank_by_id: Mapping[int, int] | None = None,
):
    from app.agents.buyer.recommendation.rerank import rerank

    llm = _PayloadLLM(payload)
    result = await rerank(
        llm,
        query="가벼운 업무용 상품",
        candidates=[
            SpringProduct(
                product_id=product_id,
                name=f"상품 {product_id}",
                price=10_000 + index * 1_000,
                rating=4.8,
                review_count=120,
                category="테스트",
            )
            for index, product_id in enumerate(candidate_ids)
        ],
        profile_summary=profile_summary,
        tier="smart",
        expose_max=expose_max,
        grounding_arm=grounding_arm,
        ranking_arm=ranking_arm,
        search_rank_by_id=search_rank_by_id,
    )
    return result, llm


async def test_current_ranking_keeps_validated_grounding_prompt_and_budget() -> None:
    from app.agents.buyer.recommendation.rerank import (
        _SYSTEM_STRUCTURED_GROUNDING,
        rerank,
    )

    llm = _PayloadLLM(
        {
            "ranked": [
                {
                    "productId": 101,
                    "rationale": "모델 문장",
                    "reasonCode": "RATING_HIGH",
                    "evidenceFields": ["ratingLevel"],
                }
            ],
            "overallComment": "골라봤어요",
        }
    )
    result = await rerank(
        llm,
        query="q",
        candidates=[
            SpringProduct(
                product_id=101,
                name="p",
                rating=4.8,
                review_count=120,
            )
        ],
        profile_summary=None,
        tier="smart",
        expose_max=1,
        grounding_arm="validated",
        ranking_arm="current",
    )
    settings = get_settings()

    assert llm.systems == [_SYSTEM_STRUCTURED_GROUNDING]
    assert result.ranked == [(101, "평점 평가가 높은 상품이에요")]
    assert result.ranking_decisions == []
    assert llm.max_tokens == [settings.rerank_max_tokens_base + settings.rerank_max_tokens_per_item]


def test_scored_prompt_declares_full_scoring_and_grounding_contract() -> None:
    from app.agents.buyer.recommendation.rerank import _SYSTEM_STRUCTURED_SCORING

    for value in (
        "evaluations",
        "intentFit",
        "needFit",
        "profileFit",
        "0..4",
        "0..3",
        "0..1",
        "모든 후보",
        "정확히 한 번",
        "RATING_HIGH",
        "NO_VERIFIABLE_EVIDENCE",
        "overallClaims",
        "ALL_WITHIN_TOTAL_BUDGET",
    ):
        assert value in _SYSTEM_STRUCTURED_SCORING


async def test_structured_ranks_all_candidates_from_valid_scores() -> None:
    payload = _scored_payload((101, 4, 3, 0), (102, 3, 3, 0))

    result, _ = await _call_scored(payload, ranking_arm="structured", expose_max=2)

    assert [product_id for product_id, _ in result.ranked] == [101, 102]
    assert [row.final_rank for row in result.ranking_decisions] == [1, 2]


async def test_hybrid_uses_same_scored_schema_but_changes_only_code_order() -> None:
    from app.agents.buyer.recommendation.rerank import _SYSTEM_STRUCTURED_SCORING

    payload = _scored_payload((101, 0, 0, 0), (102, 4, 3, 0))

    structured, structured_llm = await _call_scored(payload, ranking_arm="structured", expose_max=2)
    hybrid, hybrid_llm = await _call_scored(payload, ranking_arm="hybrid", expose_max=2)

    assert structured_llm.systems == hybrid_llm.systems == [_SYSTEM_STRUCTURED_SCORING]
    assert [row[0] for row in structured.ranked] == [102, 101]
    assert [row[0] for row in hybrid.ranked] == [101, 102]


async def test_scored_arm_token_budget_reserves_reasoning_and_uses_candidate_count() -> None:
    payload = _scored_payload(
        (101, 4, 3, 0),
        (102, 3, 3, 0),
        (103, 2, 3, 0),
    )

    _, llm = await _call_scored(
        payload,
        ranking_arm="structured",
        expose_max=1,
        candidate_ids=(101, 102, 103),
    )
    settings = get_settings()

    assert llm.max_tokens == [
        settings.rerank_max_tokens_base
        + settings.rerank_scoring_reasoning_token_reserve
        + settings.rerank_max_tokens_per_item * 3
    ]


async def test_scored_arm_all_invalid_converts_schema_error_to_llm_error() -> None:
    payload = _scored_payload((101, 5, 3, 0))

    with pytest.raises(LLMError, match="no valid evaluations"):
        await _call_scored(
            payload,
            ranking_arm="structured",
            expose_max=1,
            candidate_ids=(101,),
        )


async def test_scored_missing_candidate_recovers_with_empty_rationale() -> None:
    payload = _scored_payload((102, 4, 3, 0))

    result, _ = await _call_scored(payload, ranking_arm="structured", expose_max=2)

    assert result.ranked == [(102, "평점 평가가 높은 상품이에요"), (101, "")]
    assert len(result.grounding_decisions) == 1


async def test_invalid_grounding_neutralizes_reason_without_changing_scored_order() -> None:
    payload = _scored_payload(
        (102, 4, 3, 0),
        (101, 3, 3, 0),
        invalid_reason_for=102,
    )

    result, _ = await _call_scored(payload, ranking_arm="structured", expose_max=2)

    assert [row[0] for row in result.ranked] == [102, 101]
    assert result.ranked[0][1] == NEUTRAL_RATIONALE
    assert result.ranking_decisions[1].final_rank == 1


async def test_explicit_search_rank_survives_prompt_candidate_permutation() -> None:
    payload = _scored_payload((102, 4, 3, 0), (101, 4, 3, 0))

    result, _ = await _call_scored(
        payload,
        ranking_arm="hybrid",
        expose_max=2,
        candidate_ids=(102, 101),
        search_rank_by_id={101: 1, 102: 2},
    )

    assert [row[0] for row in result.ranked] == [101, 102]
    assert {row.product_id: row.search_rank for row in result.ranking_decisions} == {
        101: 1,
        102: 2,
    }


async def test_scored_current_grounding_keeps_model_rationale_and_omits_claims() -> None:
    proposal = {
        "claimCode": "ALL_RATING_HIGH",
        "scope": "FINAL_EXPOSED_PRODUCTS",
        "subjectProductIds": [101, 102],
        "evidenceFields": ["ratingLevel"],
    }
    payload = _scored_payload(
        (101, 4, 3, 0),
        (102, 3, 3, 0),
        overall_claims=[proposal],
    )

    result, _ = await _call_scored(
        payload,
        ranking_arm="structured",
        grounding_arm="current",
        expose_max=2,
    )

    assert result.ranked[0] == (101, "상품 101 모델 근거")
    assert result.grounding_decisions == []
    assert result.overall_claims == ()


@pytest.mark.parametrize("grounding_arm", ["prompt_only", "validated"])
async def test_scored_structured_grounding_preserves_raw_overall_claims(
    grounding_arm: str,
) -> None:
    proposal = {
        "claimCode": "ALL_RATING_HIGH",
        "scope": "FINAL_EXPOSED_PRODUCTS",
        "subjectProductIds": [101, 102],
        "evidenceFields": ["ratingLevel"],
    }
    payload = _scored_payload(
        (101, 4, 3, 0),
        (102, 3, 3, 0),
        overall_claims=[proposal],
    )

    result, _ = await _call_scored(
        payload,
        ranking_arm="structured",
        grounding_arm=grounding_arm,
        expose_max=2,
    )

    assert result.overall_claims == (proposal,)


async def test_scored_invalid_overall_claim_shape_reaches_downstream_validator() -> None:
    payload = _scored_payload(
        (101, 4, 3, 0),
        (102, 3, 3, 0),
        overall_claims="not-an-array",
    )

    result, _ = await _call_scored(payload, ranking_arm="structured", expose_max=2)

    assert result.overall_claims == ({"__invalidShape": "not-an-array"},)


def test_code_assisted_prompt_requires_a_nonempty_complete_selection() -> None:
    from app.agents.buyer.recommendation.rerank import _SYSTEM_CODE_ASSISTED

    for value in (
        "반드시 1개 이상",
        "빈 배열",
        "모든 필드",
        "JSON 정수",
    ):
        assert value in _SYSTEM_CODE_ASSISTED


def test_code_assisted_empty_valid_set_reports_bounded_rejection_counts() -> None:
    from app.agents.buyer.recommendation.rerank_code_assisted import (
        CandidateCodeSignals,
        CodeAssistedSchemaError,
        parse_code_assisted_ranking,
    )
    from app.agents.buyer.recommendation.rerank_grounding import CandidateGroundingFacts

    signals = CandidateCodeSignals(
        product_id=101,
        search_rank=1,
        need=None,
        facts=CandidateGroundingFacts(
            product_id=101,
            rating_level="높음",
            review_level="많음",
            price_level="보통",
        ),
        evidence=(),
        rating_quality=2,
        review_confidence=2,
        condition_matched=0,
        condition_applicable=0,
    )
    raw_ranked = [
        {
            "productId": 999,
            "semanticIntentFit": 4,
            "useCaseFit": 3,
            "profileFit": 0,
        },
        {
            "productId": 101,
            "semanticIntentFit": 4,
            "useCaseFit": 3,
            "profileFit": 1,
        },
    ]

    with pytest.raises(CodeAssistedSchemaError) as captured:
        parse_code_assisted_ranking(
            raw_ranked,
            {101: signals},
            profile_available=False,
            expose_max=1,
        )

    message = str(captured.value)
    assert "rows=2" in message
    assert "foreign_product_id=1" in message
    assert "profile_fit_without_profile=1" in message
    assert "productId" not in message
