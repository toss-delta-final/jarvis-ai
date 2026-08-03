"""추천 점수 성분별 순수 함수."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ComponentValue:
    """정규화 값과 명시적 degrade 상태."""

    value: float
    degraded: bool = False
    reason: str | None = None


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def semantic_score(
    query_embedding: Sequence[float] | None,
    product_embedding: Sequence[float] | None,
) -> ComponentValue:
    """코사인 [-1, 1]을 [0, 1]로 옮긴 의미 유사도."""
    if not query_embedding or not product_embedding:
        return ComponentValue(0.0, True, "missingEmbedding")
    if len(query_embedding) != len(product_embedding):
        return ComponentValue(0.0, True, "embeddingDimensionMismatch")
    query_norm = math.sqrt(sum(value * value for value in query_embedding))
    product_norm = math.sqrt(sum(value * value for value in product_embedding))
    if query_norm == 0 or product_norm == 0:
        return ComponentValue(0.0, True, "zeroNormEmbedding")
    cosine = sum(
        query * product for query, product in zip(query_embedding, product_embedding, strict=True)
    ) / (query_norm * product_norm)
    return ComponentValue(_clamp((cosine + 1.0) / 2.0))


def profile_match_score(
    category_name: str | None,
    brand_name: str | None,
    preferences: Mapping[str, Mapping[str, float]] | None,
) -> ComponentValue:
    """구매 빈도 기반 카테고리·브랜드 선호의 동일 비중 평균."""
    if not preferences:
        return ComponentValue(0.0, True, "missingProfile")
    categories = preferences.get("categories", {})
    brands = preferences.get("brands", {})
    category = float(categories.get(category_name or "", 0.0))
    brand = float(brands.get(brand_name or "", 0.0))
    return ComponentValue(_clamp((category + brand) / 2.0))


def popularity_score(review_count: int, rating: float, max_review_count: int) -> ComponentValue:
    """후보 내 log 리뷰 수와 5점 평점을 동일 비중으로 결합."""
    review = (
        math.log1p(max(0, review_count)) / math.log1p(max_review_count)
        if max_review_count > 0
        else 0.0
    )
    return ComponentValue(_clamp((review + _clamp(rating / 5.0)) / 2.0))


def recency_score(
    product_id: int, recency_by_product: Mapping[int, float] | None
) -> ComponentValue:
    """외부에서 주입한 상품별 최신성."""
    if recency_by_product is None or product_id not in recency_by_product:
        return ComponentValue(0.0, True, "missingRecency")
    return ComponentValue(_clamp(float(recency_by_product[product_id])))


def diversity_bonus(category_name: str | None, exposed_categories: set[str]) -> ComponentValue:
    """아직 노출하지 않은 비어 있지 않은 카테고리에 1."""
    if not category_name:
        return ComponentValue(0.0, True, "missingCategory")
    return ComponentValue(float(category_name not in exposed_categories))


def recent_purchase_penalty(product_id: int, recent_product_ids: set[int]) -> ComponentValue:
    """최근 window 내 정확한 상품 재구매에 1."""
    return ComponentValue(float(product_id in recent_product_ids))
