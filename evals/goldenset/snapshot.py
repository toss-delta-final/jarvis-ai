"""라이브 Spring I-1 응답을 결정론적 fixture로 기록하는 수동 도구."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from app.core.config import get_settings
from app.schemas.spring import ProductSearchFilters, ProductSearchResult, SpringProduct
from app.services.spring_client import search_products

SearchFn = Callable[[ProductSearchFilters], Awaitable[ProductSearchResult]]
I1_FIELDS = (
    "productId",
    "name",
    "summary",
    "attributes",
    "price",
    "rating",
    "reviewCount",
    "categoryName",
    "brandName",
)


def _i1_dict(product: SpringProduct) -> dict:
    raw = product.model_dump(by_alias=True)
    return {field: raw.get(field) for field in I1_FIELDS}


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


async def record_snapshots(
    queries: Mapping[str, dict[str, object]],
    *,
    search: SearchFn = search_products,
    catalog_path: Path,
    responses_path: Path,
    recorded_at: str,
    per_query_max: int | None = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """주입된 검색 함수로 fixture별 I-1 요청/응답 순서와 상품 합집합을 기록한다."""
    limit = (
        per_query_max
        if per_query_max is not None
        else get_settings().goldenset_snapshot_per_query_max
    )
    if limit <= 0:
        raise ValueError("per_query_max는 0보다 커야 합니다")
    if not recorded_at.strip():
        raise ValueError("recorded_at은 비어 있을 수 없습니다")
    catalog: dict[str, dict] = {}
    responses: dict[str, dict] = {}
    for fixture_id in sorted(queries):
        request = dict(queries[fixture_id])
        filters = ProductSearchFilters.model_validate({**request, "limit": limit})
        result = await search(filters)
        products = result.products[:limit]
        for product in products:
            catalog[str(product.product_id)] = _i1_dict(product)
        responses[fixture_id] = {
            "request": request,
            "productIds": [product.product_id for product in products],
            "totalCount": result.total_count,
            "recordedAt": recorded_at,
            "source": "live-spring-i1",
        }
    ordered_catalog = {key: catalog[key] for key in sorted(catalog, key=lambda value: int(value))}
    _write_json(catalog_path, ordered_catalog)
    _write_json(responses_path, responses)
    return ordered_catalog, responses
