"""Deterministic scoring and search-rank fusion for buyer rerank candidates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

RankingArm: TypeAlias = Literal["current", "structured", "hybrid", "code_assisted"]
ScoredRankingArm: TypeAlias = Literal["structured", "hybrid"]


class ScoringSchemaError(ValueError):
    """The scored rerank response cannot produce a trustworthy ranking."""


@dataclass(frozen=True)
class RankingDecision:
    product_id: int
    search_rank: int
    intent_fit: int | None
    need_fit: int | None
    profile_fit: int | None
    rubric_score: int | None
    llm_rank: int
    final_score: float | None
    final_rank: int
    score_valid: bool
    fallback_reason: str | None


@dataclass(frozen=True)
class RankingComputation:
    ordered_product_ids: tuple[int, ...]
    decisions: tuple[RankingDecision, ...]
    model_items_by_id: Mapping[int, Mapping[str, object]]
    foreign_evaluation_count: int
    duplicate_evaluation_count: int
    invalid_evaluation_count: int


@dataclass(frozen=True)
class _EvaluatedCandidate:
    product_id: int
    search_rank: int
    intent_fit: int | None
    need_fit: int | None
    profile_fit: int | None
    rubric_score: int | None
    score_valid: bool
    fallback_reason: str | None


def _validate_config(*, arm: object, profile_available: object, alpha: object, k: object) -> None:
    if arm not in ("structured", "hybrid"):
        raise ScoringSchemaError("scored ranking arm must be structured or hybrid")
    if not isinstance(profile_available, bool):
        raise ScoringSchemaError("profile_available must be boolean")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0 <= alpha <= 1:
        raise ScoringSchemaError("RRF alpha must be between 0 and 1")
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ScoringSchemaError("RRF k must be a positive integer")


def _validate_candidates(candidate_ids: Sequence[int]) -> tuple[int, ...]:
    normalized = tuple(candidate_ids)
    if not normalized:
        raise ScoringSchemaError("candidate_ids must not be empty")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in normalized):
        raise ScoringSchemaError("candidate_ids must contain integers")
    if len(set(normalized)) != len(normalized):
        raise ScoringSchemaError("candidate_ids must be unique")
    return normalized


def _search_ranks(
    candidate_ids: tuple[int, ...], search_rank_by_id: Mapping[int, int] | None
) -> dict[int, int]:
    if search_rank_by_id is None:
        return {product_id: rank for rank, product_id in enumerate(candidate_ids, 1)}
    if any(
        isinstance(product_id, bool) or not isinstance(product_id, int)
        for product_id in search_rank_by_id
    ):
        raise ScoringSchemaError("search rank keys must be integer product IDs")
    if set(search_rank_by_id) != set(candidate_ids):
        raise ScoringSchemaError("search ranks must cover candidates exactly")
    ranks = list(search_rank_by_id.values())
    if any(isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0 for rank in ranks):
        raise ScoringSchemaError("search ranks must be positive integers")
    if set(ranks) != set(range(1, len(candidate_ids) + 1)):
        raise ScoringSchemaError("search ranks must be unique and contiguous")
    return dict(search_rank_by_id)


def _bounded_int(item: Mapping[str, object], key: str, maximum: int) -> int | None:
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        return None
    return value


def _validated_score(
    item: Mapping[str, object], *, profile_available: bool
) -> tuple[int | None, int | None, int | None, int | None, str | None]:
    intent_fit = _bounded_int(item, "intentFit", 4)
    if intent_fit is None:
        return None, None, None, None, "invalid_intent_fit"
    need_fit = _bounded_int(item, "needFit", 3)
    if need_fit is None:
        return None, None, None, None, "invalid_need_fit"
    profile_fit = _bounded_int(item, "profileFit", 1)
    if profile_fit is None:
        return None, None, None, None, "invalid_profile_fit"
    if not profile_available and profile_fit != 0:
        return intent_fit, need_fit, profile_fit, None, "profile_fit_without_profile"
    return intent_fit, need_fit, profile_fit, intent_fit * 4 + need_fit * 2 + profile_fit, None


def compute_scored_ranking(
    candidate_ids: Sequence[int],
    raw_evaluations: object,
    *,
    arm: ScoredRankingArm,
    profile_available: bool,
    alpha: float,
    k: int,
    search_rank_by_id: Mapping[int, int] | None = None,
) -> RankingComputation:
    """Validate scored rows and deterministically rank every input candidate."""

    _validate_config(arm=arm, profile_available=profile_available, alpha=alpha, k=k)
    candidates = _validate_candidates(candidate_ids)
    search_ranks = _search_ranks(candidates, search_rank_by_id)
    if not isinstance(raw_evaluations, list):
        raise ScoringSchemaError("evaluations must be a list")

    candidate_set = set(candidates)
    rows_by_id: dict[int, list[Mapping[str, object]]] = {}
    foreign_evaluation_count = 0
    invalid_evaluation_count = 0
    for raw_item in raw_evaluations:
        if not isinstance(raw_item, Mapping):
            invalid_evaluation_count += 1
            continue
        product_id = raw_item.get("productId")
        if isinstance(product_id, bool) or not isinstance(product_id, int):
            invalid_evaluation_count += 1
            continue
        if product_id not in candidate_set:
            foreign_evaluation_count += 1
            continue
        rows_by_id.setdefault(product_id, []).append(raw_item)

    duplicate_evaluation_count = sum(max(0, len(rows) - 1) for rows in rows_by_id.values())
    model_items_by_id: dict[int, Mapping[str, object]] = {}
    evaluated: list[_EvaluatedCandidate] = []
    for product_id in candidates:
        rows = rows_by_id.get(product_id, [])
        if not rows:
            evaluated.append(
                _EvaluatedCandidate(
                    product_id=product_id,
                    search_rank=search_ranks[product_id],
                    intent_fit=None,
                    need_fit=None,
                    profile_fit=None,
                    rubric_score=None,
                    score_valid=False,
                    fallback_reason="missing_evaluation",
                )
            )
            continue
        if len(rows) > 1:
            evaluated.append(
                _EvaluatedCandidate(
                    product_id=product_id,
                    search_rank=search_ranks[product_id],
                    intent_fit=None,
                    need_fit=None,
                    profile_fit=None,
                    rubric_score=None,
                    score_valid=False,
                    fallback_reason="duplicate_evaluation",
                )
            )
            continue

        item = rows[0]
        model_items_by_id[product_id] = item
        intent_fit, need_fit, profile_fit, rubric_score, failure_reason = _validated_score(
            item, profile_available=profile_available
        )
        if failure_reason is not None:
            invalid_evaluation_count += 1
        evaluated.append(
            _EvaluatedCandidate(
                product_id=product_id,
                search_rank=search_ranks[product_id],
                intent_fit=intent_fit,
                need_fit=need_fit,
                profile_fit=profile_fit,
                rubric_score=rubric_score,
                score_valid=failure_reason is None,
                fallback_reason=failure_reason,
            )
        )

    valid = [row for row in evaluated if row.score_valid]
    if not valid:
        raise ScoringSchemaError("scored rerank has no valid evaluations")
    recovered = [row for row in evaluated if not row.score_valid]
    llm_order = sorted(
        valid,
        key=lambda row: (-int(row.rubric_score or 0), row.search_rank, row.product_id),
    ) + sorted(recovered, key=lambda row: (row.search_rank, row.product_id))
    llm_rank_by_id = {row.product_id: rank for rank, row in enumerate(llm_order, 1)}

    final_score_by_id: dict[int, float | None]
    if arm == "structured":
        final_order = llm_order
        final_score_by_id = {row.product_id: None for row in evaluated}
    else:
        effective_alpha = float(alpha)
        if not profile_available:
            effective_alpha += (1 - effective_alpha) / 23
        final_score_by_id = {
            row.product_id: effective_alpha / (k + row.search_rank)
            + (1 - effective_alpha) / (k + llm_rank_by_id[row.product_id])
            for row in evaluated
        }
        final_order = sorted(
            evaluated,
            key=lambda row: (
                -float(final_score_by_id[row.product_id] or 0.0),
                row.search_rank,
                row.product_id,
            ),
        )
    final_rank_by_id = {row.product_id: rank for rank, row in enumerate(final_order, 1)}

    decisions = tuple(
        RankingDecision(
            product_id=row.product_id,
            search_rank=row.search_rank,
            intent_fit=row.intent_fit,
            need_fit=row.need_fit,
            profile_fit=row.profile_fit,
            rubric_score=row.rubric_score,
            llm_rank=llm_rank_by_id[row.product_id],
            final_score=final_score_by_id[row.product_id],
            final_rank=final_rank_by_id[row.product_id],
            score_valid=row.score_valid,
            fallback_reason=row.fallback_reason,
        )
        for row in sorted(evaluated, key=lambda value: (value.search_rank, value.product_id))
    )
    return RankingComputation(
        ordered_product_ids=tuple(row.product_id for row in final_order),
        decisions=decisions,
        model_items_by_id=model_items_by_id,
        foreign_evaluation_count=foreign_evaluation_count,
        duplicate_evaluation_count=duplicate_evaluation_count,
        invalid_evaluation_count=invalid_evaluation_count,
    )
