"""Immutable records for paired rerank-scoring evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.agents.buyer.recommendation.rerank_code_assisted import CodeAssistedDecision
from app.agents.buyer.recommendation.rerank_grounding import (
    GroundingArm,
    GroundingDecision,
)
from app.agents.buyer.recommendation.rerank_scoring import RankingArm, RankingDecision
from app.schemas.spring import SpringProduct


@dataclass(frozen=True)
class RankingCaseInput:
    case_id: str
    query: str
    candidates: tuple[SpringProduct, ...]
    search_rank_by_id: Mapping[int, int]
    profile_summary: str | None
    relevance_grades: Mapping[int, int]
    hard_constraints: Mapping[str, object]
    must_exclude_product_ids: tuple[int, ...]
    slices: tuple[str, ...]


@dataclass(frozen=True)
class RankingFailure:
    case_id: str
    arm: RankingArm
    order_seed: int
    repeat: int
    attempt: int
    error_type: str
    message: str
    full_fallback: bool = False


@dataclass(frozen=True)
class RankingSample:
    case_id: str
    arm: RankingArm
    order_seed: int
    repeat: int
    attempt: int
    candidate_order: tuple[int, ...]
    ranked_product_ids: tuple[int, ...]
    top3_product_ids: tuple[int, ...]
    top1_product_id: int | None
    latency_ms: int
    raw_response_sha256: str
    provider_called: bool
    ranking_decisions: tuple[RankingDecision, ...]
    grounding_decisions: tuple[GroundingDecision, ...]
    relevance_grades: Mapping[int, int]
    hard_constraints: Mapping[str, object]
    must_exclude_product_ids: tuple[int, ...]
    slices: tuple[str, ...]
    code_assisted_decisions: tuple[CodeAssistedDecision, ...] = ()
    foreign_evaluation_count: int = 0
    duplicate_evaluation_count: int = 0
    invalid_score_count: int = 0
    evaluated_coverage: float = 1.0
    partial_fallback: bool = False
    full_fallback: bool = False
    hard_constraint_violation_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    usage_unknown_reason: str | None = "provider usage unavailable"
    ndcg_at_10: float | None = None
    dataset_hash: str = ""
    prompt_hash: str = ""
    model_config_json: str = ""


@dataclass(frozen=True)
class CaseArmResult:
    arm: RankingArm
    sample: RankingSample | None
    failure: RankingFailure | None
    raw_response_sha256: str
    provider_called: bool


@dataclass(frozen=True)
class RankingProbeRun:
    samples: tuple[RankingSample, ...]
    failures: tuple[RankingFailure, ...]
    arms: tuple[RankingArm, ...]
    repeats: int
    order_seeds: tuple[int, ...]
    dataset_version: str
    dataset_hash: str
    grounding_arm: GroundingArm
    alpha: float
    k: int
