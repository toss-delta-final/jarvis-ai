"""추천 점수와 완전히 분리된 hard constraint 컷."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence


@dataclass(frozen=True)
class HardConstraints:
    price_max: int | None = None
    price_min: int | None = None
    forbidden_categories: frozenset[str] = field(default_factory=frozenset)
    forbidden_product_ids: frozenset[int] = field(default_factory=frozenset)
    must_exclude_product_ids: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class FilterCut:
    product_id: int
    reason: str


@dataclass(frozen=True)
class FilterResult:
    products: list[Mapping[str, object]]
    cuts: list[FilterCut]


def apply_hard_filters(
    products: Sequence[Mapping[str, object]], constraints: HardConstraints
) -> FilterResult:
    """profile 입력 없이 현재 발화의 명시 제약만 적용한다."""
    kept: list[Mapping[str, object]] = []
    cuts: list[FilterCut] = []
    for product in products:
        product_id = int(product["productId"])
        price = product.get("price")
        category = product.get("categoryName")
        reason = None
        has_price_constraint = (
            constraints.price_max is not None or constraints.price_min is not None
        )
        price_value: float | None = None
        if has_price_constraint:
            try:
                price_value = float(price) if price is not None else None
            except (TypeError, ValueError):
                price_value = None
            if price_value is None or not math.isfinite(price_value):
                reason = "priceUnknown"
        if reason is None and constraints.price_max is not None:
            if price_value is not None and price_value > constraints.price_max:
                reason = "priceMax"
        if reason is None and constraints.price_min is not None:
            if price_value is not None and price_value < constraints.price_min:
                reason = "priceMin"
        if reason is None and category in constraints.forbidden_categories:
            reason = "forbiddenCategory"
        if reason is None and product_id in constraints.forbidden_product_ids:
            reason = "forbiddenProductId"
        if reason is None and product_id in constraints.must_exclude_product_ids:
            reason = "mustExclude"
        if reason is None:
            kept.append(product)
        else:
            cuts.append(FilterCut(product_id, reason))
    return FilterResult(kept, cuts)
