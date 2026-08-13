"""Normalized deterministic indexes over the pinned catalog snapshot."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass


def _text(value: object) -> str:
    return str(value or "").strip()


def _integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


def _float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class CatalogProduct:
    product_id: int
    name: str
    category: str
    category_parent: str
    category_leaf: str
    brand: str
    price: int | None
    rating: float | None
    review_count: int | None

    @classmethod
    def from_wire(cls, product_id: int, raw: Mapping[str, object]) -> CatalogProduct | None:
        name = _text(raw.get("name"))
        category = _text(raw.get("categoryName"))
        if not name or not category:
            return None
        parts = tuple(part.strip() for part in category.split(" > ") if part.strip())
        return cls(
            product_id=product_id,
            name=name,
            category=category,
            category_parent=parts[0],
            category_leaf=parts[-1],
            brand=_text(raw.get("brandName")),
            price=_integer(raw.get("price")),
            rating=_float(raw.get("rating")),
            review_count=_integer(raw.get("reviewCount")),
        )


@dataclass(frozen=True)
class CatalogIndex:
    by_id: Mapping[int, CatalogProduct]
    by_category: Mapping[str, tuple[CatalogProduct, ...]]
    by_parent: Mapping[str, tuple[CatalogProduct, ...]]
    by_brand: Mapping[str, tuple[CatalogProduct, ...]]

    @classmethod
    def from_snapshot(cls, catalog: Mapping[str, Mapping[str, object]]) -> CatalogIndex:
        by_id: dict[int, CatalogProduct] = {}
        category_rows: defaultdict[str, list[CatalogProduct]] = defaultdict(list)
        parent_rows: defaultdict[str, list[CatalogProduct]] = defaultdict(list)
        brand_rows: defaultdict[str, list[CatalogProduct]] = defaultdict(list)
        for raw_id, raw in catalog.items():
            try:
                product_id = int(raw.get("productId", raw_id))
            except (TypeError, ValueError):
                continue
            product = CatalogProduct.from_wire(product_id, raw)
            if product is None or product_id in by_id:
                continue
            by_id[product_id] = product
            category_rows[product.category].append(product)
            parent_rows[product.category_parent].append(product)
            if product.brand:
                brand_rows[product.brand].append(product)

        def frozen(
            rows: Mapping[str, list[CatalogProduct]],
        ) -> dict[str, tuple[CatalogProduct, ...]]:
            return {
                key: tuple(sorted(values, key=lambda product: product.product_id))
                for key, values in sorted(rows.items())
            }

        return cls(
            by_id=dict(sorted(by_id.items())),
            by_category=frozen(category_rows),
            by_parent=frozen(parent_rows),
            by_brand=frozen(brand_rows),
        )

    def brands_in_category(self, category: str) -> dict[str, tuple[CatalogProduct, ...]]:
        grouped: defaultdict[str, list[CatalogProduct]] = defaultdict(list)
        for product in self.by_category[category]:
            if product.brand:
                grouped[product.brand].append(product)
        return {brand: tuple(products) for brand, products in sorted(grouped.items())}
