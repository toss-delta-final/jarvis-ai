"""Deterministic grounding contract for buyer rerank rationales.

The LLM may choose a ranking and a small reason code.  This module owns the
mechanical question: whether that reason code is supported by the candidate
facts already sent to the model, and which safe user-facing sentence follows.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

GroundingArm = Literal["current", "prompt_only", "validated"]
ReasonCode = Literal[
    "RATING_HIGH",
    "REVIEW_MANY",
    "PRICE_RELATIVE_LOW",
    "NO_VERIFIABLE_EVIDENCE",
]

NEUTRAL_RATIONALE = "요청과의 관련도를 기준으로 추천했어요"


@dataclass(frozen=True)
class CandidateGroundingFacts:
    product_id: int
    rating_level: str
    review_level: str
    price_level: str


@dataclass(frozen=True)
class GroundingDecision:
    product_id: int
    requested_reason_code: str
    evidence_fields: tuple[str, ...]
    model_rationale: str
    rendered_rationale: str
    supported: bool
    downgraded: bool
    failure_reason: str | None = None


@dataclass(frozen=True)
class _ReasonSpec:
    evidence_fields: tuple[str, ...]
    fact_name: str | None
    allowed_values: frozenset[str]
    rationale: str


_REASON_SPECS: dict[str, _ReasonSpec] = {
    "RATING_HIGH": _ReasonSpec(
        evidence_fields=("ratingLevel",),
        fact_name="rating_level",
        allowed_values=frozenset({"높음", "매우높음"}),
        rationale="평점 평가가 높은 상품이에요",
    ),
    "REVIEW_MANY": _ReasonSpec(
        evidence_fields=("reviewLevel",),
        fact_name="review_level",
        allowed_values=frozenset({"많음", "매우많음"}),
        rationale="리뷰 정보가 많은 상품이에요",
    ),
    "PRICE_RELATIVE_LOW": _ReasonSpec(
        evidence_fields=("priceLevel",),
        fact_name="price_level",
        allowed_values=frozenset({"저렴", "매우저렴"}),
        rationale="같은 후보군에서 비교적 저렴해요",
    ),
    "NO_VERIFIABLE_EVIDENCE": _ReasonSpec(
        evidence_fields=(),
        fact_name=None,
        allowed_values=frozenset(),
        rationale=NEUTRAL_RATIONALE,
    ),
}


def _downgrade(
    *,
    facts: CandidateGroundingFacts,
    reason_code: str,
    evidence_fields: tuple[str, ...],
    model_rationale: str,
    failure_reason: str,
) -> GroundingDecision:
    return GroundingDecision(
        product_id=facts.product_id,
        requested_reason_code=reason_code,
        evidence_fields=evidence_fields,
        model_rationale=model_rationale,
        rendered_rationale=NEUTRAL_RATIONALE,
        supported=False,
        downgraded=True,
        failure_reason=failure_reason,
    )


def validate_and_render_grounding(
    item: Mapping[str, object], facts: CandidateGroundingFacts
) -> GroundingDecision:
    """Validate one structured rerank item and render its safe rationale.

    A failure never invalidates the candidate itself.  It only returns a
    downgraded neutral rationale and a machine-readable failure reason.
    """

    reason_value = item.get("reasonCode")
    reason_code = reason_value if isinstance(reason_value, str) else ""
    rationale_value = item.get("rationale")
    model_rationale = rationale_value if isinstance(rationale_value, str) else ""

    fields_value = item.get("evidenceFields")
    evidence_fields: tuple[str, ...] = ()
    if isinstance(fields_value, list) and all(isinstance(value, str) for value in fields_value):
        evidence_fields = tuple(fields_value)

    product_id = item.get("productId")
    if isinstance(product_id, bool) or not isinstance(product_id, int):
        return _downgrade(
            facts=facts,
            reason_code=reason_code,
            evidence_fields=evidence_fields,
            model_rationale=model_rationale,
            failure_reason="invalid_product_id",
        )
    if product_id != facts.product_id:
        return _downgrade(
            facts=facts,
            reason_code=reason_code,
            evidence_fields=evidence_fields,
            model_rationale=model_rationale,
            failure_reason="product_id_mismatch",
        )
    if not isinstance(fields_value, list) or not all(
        isinstance(value, str) for value in fields_value
    ):
        return _downgrade(
            facts=facts,
            reason_code=reason_code,
            evidence_fields=evidence_fields,
            model_rationale=model_rationale,
            failure_reason="invalid_evidence_fields",
        )

    spec = _REASON_SPECS.get(reason_code)
    if spec is None:
        return _downgrade(
            facts=facts,
            reason_code=reason_code,
            evidence_fields=evidence_fields,
            model_rationale=model_rationale,
            failure_reason="unknown_reason_code",
        )
    if evidence_fields != spec.evidence_fields:
        return _downgrade(
            facts=facts,
            reason_code=reason_code,
            evidence_fields=evidence_fields,
            model_rationale=model_rationale,
            failure_reason="evidence_fields_mismatch",
        )
    if spec.fact_name is not None and getattr(facts, spec.fact_name) not in spec.allowed_values:
        return _downgrade(
            facts=facts,
            reason_code=reason_code,
            evidence_fields=evidence_fields,
            model_rationale=model_rationale,
            failure_reason="candidate_tier_not_supported",
        )

    return GroundingDecision(
        product_id=facts.product_id,
        requested_reason_code=reason_code,
        evidence_fields=evidence_fields,
        model_rationale=model_rationale,
        rendered_rationale=spec.rationale,
        supported=True,
        downgraded=False,
    )
