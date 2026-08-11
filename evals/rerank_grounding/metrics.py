"""Truthful, narrowly scoped metrics for rerank rationale grounding."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.buyer.recommendation.rerank_grounding import (
    CandidateGroundingFacts,
    GroundingArm,
)

_NUMBER_RE = re.compile(r"\d|[영일이삼사오육칠팔구십백천만]+(?:점|개|원|%)")

_METRIC_DEFINITIONS = {
    "unsupportedEvidence": {
        "numerator": (
            "표시 근거가 후보 tier와 충돌하거나, prompt-only 구조화 근거가 검증에 실패한 항목 수"
        ),
        "denominator": "유효 후보 ID와 함께 사용자에게 표시된 전체 rerank 항목 수",
        "limit": "rating/review/relative-price와 정확한 숫자 주장만 측정",
    },
    "validRankCoverage": {
        "numerator": "유효 후보 ID와 함께 표시된 rerank 항목 수",
        "denominator": "해당 표본의 후보 수 합계",
    },
}


@dataclass(frozen=True)
class MetricItem:
    product_id: int
    displayed_rationale: str
    facts: CandidateGroundingFacts
    grounding_supported: bool | None = None
    validator_downgraded: bool = False


@dataclass(frozen=True)
class MetricSample:
    case_id: str
    test_type: str
    pair_id: str | None
    arm: GroundingArm
    items: tuple[MetricItem, ...]
    candidate_count: int
    out_of_candidate_id_count: int = 0
    duplicate_id_count: int = 0
    failure_type: str | None = None


@dataclass(frozen=True)
class ArmMetrics:
    arm: GroundingArm
    unsupported_evidence_numerator: int
    unsupported_evidence_denominator: int
    unsupported_evidence_rate: float | None
    out_of_candidate_id_count: int
    duplicate_id_count: int
    invalid_structured_evidence_count: int
    validator_downgrade_count: int
    valid_rank_coverage: float | None
    successful_sample_count: int
    failure_count: int
    metric_definitions: dict[str, dict[str, str]] = field(
        default_factory=lambda: {key: dict(value) for key, value in _METRIC_DEFINITIONS.items()}
    )

    def as_dict(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "unsupportedEvidence": {
                "numerator": self.unsupported_evidence_numerator,
                "denominator": self.unsupported_evidence_denominator,
                "rate": self.unsupported_evidence_rate,
                "definition": self.metric_definitions["unsupportedEvidence"],
            },
            "outOfCandidateIdCount": self.out_of_candidate_id_count,
            "duplicateIdCount": self.duplicate_id_count,
            "invalidStructuredEvidenceCount": self.invalid_structured_evidence_count,
            "validatorDowngradeCount": self.validator_downgrade_count,
            "validRankCoverage": self.valid_rank_coverage,
            "successfulSampleCount": self.successful_sample_count,
            "failureCount": self.failure_count,
        }


def detect_unsupported_rationale(rationale: str, facts: CandidateGroundingFacts) -> bool:
    """Detect only claim families whose truth is available in candidate tiers.

    Returning ``False`` does not mean that every semantic statement is true.  It
    means that this intentionally narrow detector found no contradiction in the
    three registered tier-backed claim families and no fabricated exact number.
    """

    collapsed = " ".join(rationale.split())
    if _NUMBER_RE.search(collapsed):
        return True
    if "평점" in collapsed and any(token in collapsed for token in ("높", "좋", "우수")):
        if facts.rating_level not in {"높음", "매우높음"}:
            return True
    if any(token in collapsed for token in ("리뷰", "후기")) and "많" in collapsed:
        if facts.review_level not in {"많음", "매우많음"}:
            return True
    if any(token in collapsed for token in ("저렴", "가성비", "싼")):
        if facts.price_level not in {"저렴", "매우저렴"}:
            return True
    return False


def _is_unsupported(item: MetricItem, arm: GroundingArm) -> bool:
    text_violation = detect_unsupported_rationale(item.displayed_rationale, item.facts)
    if arm == "prompt_only":
        return item.grounding_supported is False or text_violation
    return text_violation


def score_samples(samples: list[MetricSample]) -> dict[GroundingArm, ArmMetrics]:
    grouped: dict[GroundingArm, list[MetricSample]] = {}
    for sample in samples:
        grouped.setdefault(sample.arm, []).append(sample)

    result: dict[GroundingArm, ArmMetrics] = {}
    for arm, arm_samples in grouped.items():
        successful = [sample for sample in arm_samples if sample.failure_type is None]
        items = [item for sample in successful for item in sample.items]
        unsupported = sum(_is_unsupported(item, arm) for item in items)
        denominator = len(items)
        candidate_count = sum(max(sample.candidate_count, 0) for sample in successful)
        coverage = denominator / candidate_count if candidate_count else None
        result[arm] = ArmMetrics(
            arm=arm,
            unsupported_evidence_numerator=unsupported,
            unsupported_evidence_denominator=denominator,
            unsupported_evidence_rate=unsupported / denominator if denominator else None,
            out_of_candidate_id_count=sum(
                sample.out_of_candidate_id_count for sample in successful
            ),
            duplicate_id_count=sum(sample.duplicate_id_count for sample in successful),
            invalid_structured_evidence_count=(unsupported if arm == "validated" else 0),
            validator_downgrade_count=sum(
                item.validator_downgraded for item in items if arm == "validated"
            ),
            valid_rank_coverage=coverage,
            successful_sample_count=len(successful),
            failure_count=len(arm_samples) - len(successful),
        )
    return result
