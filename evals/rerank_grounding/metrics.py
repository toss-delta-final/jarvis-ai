"""Truthful, narrowly scoped metrics for rerank rationale grounding."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.buyer.recommendation.rerank_grounding import (
    CandidateGroundingFacts,
    GroundingArm,
)

_NUMBER_RE = re.compile(r"\d|[영일이삼사오육칠팔구십백천만]+(?:점|개|원|%)")
_NEUTRAL_OVERALL_COMMENT = "요청과의 관련도를 기준으로 추천했어요"
_TOP_REVIEW_RE = re.compile(
    r"(?:리뷰|후기)(?:\s*수)?(?:가|는|의)?\s*(?:(?:가장|제일)\s*많|최다)"
    r"|(?:가장|제일)\s*(?:리뷰|후기)(?:\s*수)?(?:가|는|의)?\s*많"
)
_ALL_RATING_RE = re.compile(
    r"(?:모두|전부).{0,16}(?:평점|평가).{0,16}(?:높|좋|우수)"
    r"|(?:평점|평가).{0,16}(?:높|좋|우수).{0,12}(?:상품|제품|후보|것)(?:들)?만"
)

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
    "detectedOverallClaimViolation": {
        "numerator": "등록된 표현 또는 B 구조화 metadata에서 fixture oracle이 금지한 전체 주장 수",
        "denominator": "기계적으로 검출된 전체 주장 수",
        "limit": "등록된 표현군과 구조화 claim code만 측정하며 자유문장 전체 정확도를 뜻하지 않음",
    },
    "supportedOverallClaimCoverage": {
        "numerator": "표시 또는 구조화 결과에서 실제로 지원된 non-neutral 전체 주장 수",
        "denominator": "fixture oracle이 허용한 non-neutral 전체 주장 기회 수",
        "limit": "정의된 세 가지 사실 주장만 측정",
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
    displayed_overall_comment: str = ""
    requested_overall_claim_codes: tuple[str, ...] = ()
    supported_overall_claim_codes: tuple[str, ...] = ()
    overall_validator_downgraded: bool = False
    overall_failure_reasons: tuple[str, ...] = ()
    allowed_overall_claim_codes: tuple[str, ...] = ()
    forbidden_overall_claim_codes: tuple[str, ...] = ()
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
    detected_overall_claim_violation_numerator: int
    detected_overall_claim_violation_denominator: int
    detected_overall_claim_violation_rate: float | None
    supported_overall_claim_coverage_numerator: int
    supported_overall_claim_coverage_denominator: int
    supported_overall_claim_coverage: float | None
    overall_validator_downgrade_count: int
    overall_invalid_structured_claim_count: int
    overall_failure_reason_counts: dict[str, int]
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
            "detectedOverallClaimViolation": {
                "numerator": self.detected_overall_claim_violation_numerator,
                "denominator": self.detected_overall_claim_violation_denominator,
                "rate": self.detected_overall_claim_violation_rate,
                "definition": self.metric_definitions["detectedOverallClaimViolation"],
            },
            "supportedOverallClaimCoverage": {
                "numerator": self.supported_overall_claim_coverage_numerator,
                "denominator": self.supported_overall_claim_coverage_denominator,
                "rate": self.supported_overall_claim_coverage,
                "definition": self.metric_definitions["supportedOverallClaimCoverage"],
            },
            "overallValidatorDowngradeCount": self.overall_validator_downgrade_count,
            "overallInvalidStructuredClaimCount": self.overall_invalid_structured_claim_count,
            "overallFailureReasonCounts": dict(self.overall_failure_reason_counts),
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


def detect_overall_claims(comment: str) -> tuple[str, ...]:
    """Detect only registered Korean whole-list claim families.

    Absence from this tuple is not evidence that arbitrary prose is true.
    """

    collapsed = " ".join(comment.split())
    detected: list[str] = []
    if _TOP_REVIEW_RE.search(collapsed):
        detected.append("TOP_REVIEW_COUNT")
    if _ALL_RATING_RE.search(collapsed):
        detected.append("ALL_RATING_HIGH")
    if (
        any(token in collapsed for token in ("모두", "전부", "각 추천 조합", "각 조합"))
        and "예산" in collapsed
        and any(token in collapsed for token in ("맞", "이내", "안"))
    ):
        detected.append("ALL_WITHIN_TOTAL_BUDGET")
    if "인기" in collapsed and any(token in collapsed for token in ("가장", "제일")):
        detected.append("POPULARITY_TOP")
    if "가성비" in collapsed and any(token in collapsed for token in ("가장", "제일", "최고")):
        detected.append("VALUE_FOR_MONEY_TOP")
    if _NEUTRAL_OVERALL_COMMENT in collapsed:
        detected.append("NO_VERIFIABLE_OVERALL_CLAIM")
    return tuple(detected)


def _is_unsupported(item: MetricItem, arm: GroundingArm) -> bool:
    text_violation = detect_unsupported_rationale(item.displayed_rationale, item.facts)
    if arm == "prompt_only":
        return item.grounding_supported is False or text_violation
    return text_violation


def _overall_scored_claims(sample: MetricSample) -> tuple[str, ...]:
    detected = list(detect_overall_claims(sample.displayed_overall_comment))
    if sample.arm == "prompt_only":
        detected.extend(code for code in sample.requested_overall_claim_codes if code)
    return tuple(dict.fromkeys(detected))


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
        overall_claims = [
            (sample, code) for sample in successful for code in _overall_scored_claims(sample)
        ]
        overall_violations = sum(
            code not in set(sample.allowed_overall_claim_codes) for sample, code in overall_claims
        )
        coverage_denominator = sum(
            len(set(sample.allowed_overall_claim_codes) - {"NO_VERIFIABLE_OVERALL_CLAIM"})
            for sample in successful
        )
        coverage_numerator = 0
        for sample in successful:
            observed = (
                set(detect_overall_claims(sample.displayed_overall_comment))
                if arm == "current"
                else set(sample.supported_overall_claim_codes)
            )
            coverage_numerator += len(
                observed
                & (set(sample.allowed_overall_claim_codes) - {"NO_VERIFIABLE_OVERALL_CLAIM"})
            )
        failure_reason_counts: dict[str, int] = {}
        for sample in successful:
            for reason in sample.overall_failure_reasons:
                failure_reason_counts[reason] = failure_reason_counts.get(reason, 0) + 1
        invalid_structured = 0
        if arm == "prompt_only":
            for sample in successful:
                invalid_codes = sum(
                    bool(code) and code not in set(sample.allowed_overall_claim_codes)
                    for code in sample.requested_overall_claim_codes
                )
                invalid_structured += max(
                    invalid_codes,
                    int(sample.overall_validator_downgraded and not invalid_codes),
                )
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
            detected_overall_claim_violation_numerator=overall_violations,
            detected_overall_claim_violation_denominator=len(overall_claims),
            detected_overall_claim_violation_rate=(
                overall_violations / len(overall_claims) if overall_claims else None
            ),
            supported_overall_claim_coverage_numerator=coverage_numerator,
            supported_overall_claim_coverage_denominator=coverage_denominator,
            supported_overall_claim_coverage=(
                coverage_numerator / coverage_denominator if coverage_denominator else None
            ),
            overall_validator_downgrade_count=sum(
                sample.overall_validator_downgraded for sample in successful
            ),
            overall_invalid_structured_claim_count=invalid_structured,
            overall_failure_reason_counts=failure_reason_counts,
            successful_sample_count=len(successful),
            failure_count=len(arm_samples) - len(successful),
        )
    return result
