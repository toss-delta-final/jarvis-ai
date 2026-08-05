"""가중합, diversity greedy 재선정, 안정적 tie-break."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, NamedTuple, Sequence

from evals.scoring.components import (
    ComponentValue,
    diversity_bonus,
    popularity_score,
    profile_match_score,
    recency_score,
    recent_purchase_penalty,
    semantic_score,
)


class ScoringWeights(NamedTuple):
    semantic: float
    profile_match: float
    popularity: float
    recency: float
    diversity_bonus: float
    recent_purchase_penalty: float


@dataclass(frozen=True)
class WeightedComponent:
    value: float
    weight: float
    contribution: float
    degraded: bool
    reason: str | None


@dataclass(frozen=True)
class ProductScore:
    product_id: int
    final_score: float
    components: dict[str, WeightedComponent]


@dataclass(frozen=True)
class ScoringResult:
    ranked_product_ids: list[int]
    scores: list[ProductScore]


def _weighted(value: ComponentValue, weight: float) -> WeightedComponent:
    return WeightedComponent(
        value=value.value,
        weight=weight,
        contribution=value.value * weight,
        degraded=value.degraded,
        reason=value.reason,
    )


def score_products(
    products: Sequence[Mapping[str, object]],
    *,
    query_embedding: Sequence[float] | None = None,
    product_embeddings: Mapping[int, Sequence[float]] | None = None,
    profile_preferences: Mapping[str, Mapping[str, float]] | None = None,
    recency_by_product: Mapping[int, float] | None = None,
    recent_product_ids: Sequence[int] = (),
    weights: ScoringWeights,
) -> ScoringResult:
    """각 성분을 기록한 뒤 greedy diversity로 최종 순위를 만든다."""
    embeddings = product_embeddings or {}
    recent = set(recent_product_ids)
    max_reviews = max((int(product.get("reviewCount") or 0) for product in products), default=0)
    pending: dict[int, tuple[Mapping[str, object], dict[str, WeightedComponent], float]] = {}
    for product in products:
        product_id = int(product["productId"])
        components = {
            "semantic": _weighted(
                semantic_score(query_embedding, embeddings.get(product_id)), weights.semantic
            ),
            "profileMatch": _weighted(
                profile_match_score(
                    str(product.get("categoryName") or ""),
                    str(product.get("brandName") or ""),
                    profile_preferences,
                ),
                weights.profile_match,
            ),
            "popularity": _weighted(
                popularity_score(
                    int(product.get("reviewCount") or 0),
                    float(product.get("rating") or 0.0),
                    max_reviews,
                ),
                weights.popularity,
            ),
            "recency": _weighted(
                recency_score(product_id, recency_by_product), weights.recency
            ),
            "recentPurchasePenalty": _weighted(
                recent_purchase_penalty(product_id, recent),
                -weights.recent_purchase_penalty,
            ),
        }
        base = sum(component.contribution for component in components.values())
        pending[product_id] = (product, components, base)

    exposed_categories: set[str] = set()
    selected: list[ProductScore] = []
    while pending:
        candidates: list[tuple[float, int, ComponentValue]] = []
        for product_id, (product, _, base) in pending.items():
            diversity = diversity_bonus(
                str(product.get("categoryName") or ""), exposed_categories
            )
            candidates.append(
                (base + diversity.value * weights.diversity_bonus, product_id, diversity)
            )
        final_score, product_id, diversity = min(
            candidates, key=lambda row: (-row[0], row[1])
        )
        product, components, _ = pending.pop(product_id)
        components["diversityBonus"] = _weighted(diversity, weights.diversity_bonus)
        selected.append(ProductScore(product_id, final_score, components))
        category = str(product.get("categoryName") or "")
        if category:
            exposed_categories.add(category)

    return ScoringResult(
        ranked_product_ids=[row.product_id for row in selected],
        scores=selected,
    )
