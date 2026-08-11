"""사람 평가 원시 행의 고정 스키마와 PII 최소화 검증."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

SCHEMA_VERSION = "blind-pairwise-response-v1"
VARIANTS = ("baseline", "recommendation_v2")
# 원시 응답은 평가자가 보는 A/B만 기록한다. 알고리즘 variant 이름은 assignment
# artifact를 가진 분석 단계에서만 복원한다.
PREFERENCES = ("A", "B", "tie", "abstain")
DIMENSIONS = ("relevance_fit", "explainability", "trustworthiness")
SCORE_MIN = 1
SCORE_MAX = 5
DISAGREEMENT_TAGS = (
    "both_plausible",
    "neither_fit",
    "missing_context",
    "unclear_explanation",
    "trust_concern",
)

_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,63}$")
_EVALUATOR_PATTERN = re.compile(r"^eval-[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
_PHONE_PATTERN = re.compile(r"(?:\+?\d[\d .-]{7,}\d)")
_EMAIL_PATTERN = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")


class ValidationError(ValueError):
    """원시 평가 행이 고정 스키마를 위반할 때 발생한다."""


@dataclass(frozen=True, slots=True)
class RawResponse:
    """평가자 1명이 1개 assignment에 제출한 비식별 원시 응답."""

    response_id: str
    assignment_id: str
    pair_id: str
    evaluator_id: str
    response_origin: str
    consent: bool
    preference: str
    dimension_scores: dict[str, dict[str, int | None]]
    disagreement_tags: tuple[str, ...]
    submitted_at: str
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RawResponse":
        """camelCase JSON 한 행을 검증하고 내부 표현으로 변환한다."""
        if not isinstance(value, Mapping):
            raise ValidationError("raw response must be an object")
        expected = {
            "schemaVersion",
            "responseId",
            "assignmentId",
            "pairId",
            "evaluatorId",
            "responseOrigin",
            "consent",
            "preference",
            "dimensionScores",
            "disagreementTags",
            "submittedAt",
        }
        unknown = set(value) - expected
        if unknown:
            raise ValidationError(f"unknown field: {sorted(unknown)[0]}")
        missing = (expected - {"disagreementTags"}) - set(value)
        if missing:
            raise ValidationError(f"missing field: {sorted(missing)[0]}")

        schema_version = value["schemaVersion"]
        if schema_version != SCHEMA_VERSION:
            raise ValidationError(f"schemaVersion must be {SCHEMA_VERSION}")

        response_id = _require_id(value["responseId"], "responseId")
        assignment_id = _require_id(value["assignmentId"], "assignmentId")
        pair_id = _require_id(value["pairId"], "pairId")
        evaluator_id = value["evaluatorId"]
        if not isinstance(evaluator_id, str) or not _EVALUATOR_PATTERN.fullmatch(evaluator_id):
            raise ValidationError("evaluatorId must be pseudonymous (eval-…)")
        _reject_pii(evaluator_id, "evaluatorId")

        response_origin = value["responseOrigin"]
        if response_origin != "human":
            raise ValidationError("responseOrigin must be human; synthetic rows are rejected")

        consent = value["consent"]
        if consent is not True:
            raise ValidationError("consent must be true before a response is accepted")

        preference = value["preference"]
        if preference not in PREFERENCES:
            raise ValidationError(f"preference must be one of {PREFERENCES}")

        dimension_scores = _validate_dimension_scores(value["dimensionScores"], preference)
        disagreement_tags = _validate_tags(value.get("disagreementTags", []))
        submitted_at = _validate_timestamp(value["submittedAt"])
        return cls(
            response_id=response_id,
            assignment_id=assignment_id,
            pair_id=pair_id,
            evaluator_id=evaluator_id,
            response_origin=response_origin,
            consent=consent,
            preference=preference,
            dimension_scores=dimension_scores,
            disagreement_tags=disagreement_tags,
            submitted_at=submitted_at,
        )

    def to_dict(self) -> dict[str, Any]:
        """정렬 가능한 canonical JSON 표현을 반환한다."""
        return {
            "schemaVersion": self.schema_version,
            "responseId": self.response_id,
            "assignmentId": self.assignment_id,
            "pairId": self.pair_id,
            "evaluatorId": self.evaluator_id,
            "responseOrigin": self.response_origin,
            "consent": self.consent,
            "preference": self.preference,
            "dimensionScores": {
                dimension: {
                    label: self.dimension_scores[dimension][label] for label in ("A", "B")
                }
                for dimension in DIMENSIONS
            },
            "disagreementTags": list(self.disagreement_tags),
            "submittedAt": self.submitted_at,
        }


def _require_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ValidationError(f"{field} must be an opaque identifier")
    _reject_pii(value, field)
    return value


def _reject_pii(value: str, field: str) -> None:
    if _EMAIL_PATTERN.search(value) or _PHONE_PATTERN.search(value):
        raise ValidationError(f"{field} must not contain PII")


def _validate_dimension_scores(value: object, preference: str) -> dict[str, dict[str, int | None]]:
    if not isinstance(value, Mapping) or set(value) != set(DIMENSIONS):
        raise ValidationError("dimension scores must contain every rubric dimension")
    result: dict[str, dict[str, int | None]] = {}
    for dimension in DIMENSIONS:
        scores = value[dimension]
        if not isinstance(scores, Mapping) or set(scores) != {"A", "B"}:
            raise ValidationError(f"dimension scores for {dimension} must contain A and B")
        result[dimension] = {}
        for label in ("A", "B"):
            score = scores[label]
            if score is None and preference == "abstain":
                result[dimension][label] = None
                continue
            if isinstance(score, bool) or not isinstance(score, int):
                raise ValidationError(f"{dimension} scores must be integers from 1 to 5")
            if not SCORE_MIN <= score <= SCORE_MAX:
                raise ValidationError(f"{dimension} scores must be integers from 1 to 5")
            result[dimension][label] = score
    return result


def _validate_tags(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(tag not in DISAGREEMENT_TAGS for tag in value):
        raise ValidationError("disagreementTags must use the controlled rubric vocabulary")
    if len(value) != len(set(value)):
        raise ValidationError("disagreementTags must not contain duplicates")
    return tuple(value)


def _validate_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("submittedAt must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("submittedAt must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError("submittedAt must include a timezone")
    return value


def validate_raw_response(value: Mapping[str, Any] | RawResponse) -> RawResponse:
    """응답 한 행을 검증한다. 이미 검증된 ``RawResponse``는 그대로 돌려준다."""
    if isinstance(value, RawResponse):
        return value
    return RawResponse.from_dict(value)


__all__ = [
    "DIMENSIONS",
    "DISAGREEMENT_TAGS",
    "PREFERENCES",
    "RawResponse",
    "SCHEMA_VERSION",
    "ValidationError",
    "validate_raw_response",
]
