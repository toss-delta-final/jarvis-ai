"""Strict wire contracts for the rerank LLM blind judge."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

JudgeSide = Literal["A", "B"]
JudgeWinner = Literal["A", "B", "tie"]
JudgeArm = Literal["current", "structured"]
ReasonCode = Literal[
    "QUERY_CONSTRAINT_FIT",
    "TOP_ORDER_QUALITY",
    "PROFILE_TIEBREAK_FIT",
    "USEFUL_COVERAGE",
    "IRRELEVANCE_OR_REDUNDANCY",
    "INSUFFICIENT_EVIDENCE",
]

_ARM_IDENTITY_RE = re.compile(r"\b(?:current|structured|hybrid|code_assisted)\b", re.IGNORECASE)


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class CandidateFact(CamelModel):
    product_id: int
    name: str
    brand: str | None = None
    category: str | None = None
    price_level: str
    rating_level: str
    review_level: str


class BlindPresentation(CamelModel):
    schema_version: Literal["rerank-blind-presentation-v1"] = "rerank-blind-presentation-v1"
    presentation_id: str
    pair_id: str
    query: str
    profile_summary: str
    candidates: tuple[CandidateFact, ...]
    ranking_a: tuple[int, ...]
    ranking_b: tuple[int, ...]
    orientation: Literal[0, 1] = Field(exclude=True)

    @model_validator(mode="after")
    def _rankings_reference_candidates(self) -> BlindPresentation:
        candidate_ids = [row.product_id for row in self.candidates]
        if candidate_ids != sorted(candidate_ids) or len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidates must contain distinct product IDs in sorted order")
        allowed = set(candidate_ids)
        for label, ranking in (("A", self.ranking_a), ("B", self.ranking_b)):
            if not ranking or len(ranking) != len(set(ranking)):
                raise ValueError(f"ranking {label} must contain distinct product IDs")
            if not set(ranking) <= allowed:
                raise ValueError(f"ranking {label} contains a product outside candidates")
        return self


class CoordinatorMapping(CamelModel):
    schema_version: Literal["rerank-blind-mapping-v1"] = "rerank-blind-mapping-v1"
    presentation_id: str
    pair_id: str
    orientation: Literal[0, 1]
    case_id: str
    order_seed: int
    repeat: int
    side_a_arm: JudgeArm
    side_b_arm: JudgeArm
    slices: tuple[str, ...]
    identity: Literal["guest", "member"]
    stratum: str
    source_response_sha256: dict[JudgeArm, str]

    @model_validator(mode="after")
    def _opposite_arms(self) -> CoordinatorMapping:
        if self.side_a_arm == self.side_b_arm:
            raise ValueError("A and B must map to different arms")
        if set(self.source_response_sha256) != {"current", "structured"}:
            raise ValueError("source hashes must cover both arms")
        return self


class JudgeVerdict(CamelModel):
    schema_version: Literal["rerank-blind-verdict-v1"] = "rerank-blind-verdict-v1"
    winner: JudgeWinner
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: tuple[ReasonCode, ...] = Field(min_length=1, max_length=3)
    explanation: str = Field(min_length=1, max_length=300)

    @field_validator("reason_codes")
    @classmethod
    def _unique_reason_codes(cls, value: tuple[ReasonCode, ...]) -> tuple[ReasonCode, ...]:
        if len(value) != len(set(value)):
            raise ValueError("reasonCodes must be unique")
        return value

    @field_validator("explanation")
    @classmethod
    def _no_arm_identity(cls, value: str) -> str:
        if _ARM_IDENTITY_RE.search(value):
            raise ValueError("explanation must not disclose arm identity")
        return value


class JudgeResponse(CamelModel):
    schema_version: Literal["rerank-blind-response-v1"] = "rerank-blind-response-v1"
    presentation_id: str
    pair_id: str
    attempt: int = Field(ge=1)
    latency_ms: int = Field(ge=0)
    raw_response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: JudgeVerdict


class JudgeFailure(CamelModel):
    schema_version: Literal["rerank-blind-failure-v1"] = "rerank-blind-failure-v1"
    presentation_id: str
    pair_id: str
    attempt: int = Field(ge=1)
    error_type: str
    message: str
