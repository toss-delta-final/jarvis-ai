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


def _relaxed_variants(filters: Mapping[str, object]) -> list[tuple[str, dict[str, object]]]:
    """후보가 얕을 때 별도 요청으로 넓혀 볼 완화 검색 변형(keyword-only, category-only).

    priceMax/priceMin은 하드 제약이라 완화 대상에서 제외한다 — Spring이 가격을
    서버단에서 필터링하므로, 완화 요청에서 가격 경계를 빼면 원래 케이스가 요구하는
    가격 범위를 벗어난 실제 상품이 golden_filter 후보로 섞여 들어와 HCV로 샌다
    (#333 Part 2 라이브 실측: buy-mult-0001 외 3건).
    """
    variants: list[tuple[str, dict[str, object]]] = []
    price_bounds = {
        key: filters[key] for key in ("priceMax", "priceMin") if filters.get(key) is not None
    }
    keyword = filters.get("keyword")
    if keyword:
        variants.append(("keyword-only", {"keyword": keyword, **price_bounds}))
    category = filters.get("category")
    if category:
        variants.append(("category-only", {"category": category, **price_bounds}))
    return variants


async def _search_ids(
    search: SearchFn, filters: Mapping[str, object], limit: int, catalog: dict[str, dict]
) -> tuple[list[int], int]:
    parsed = ProductSearchFilters.model_validate({**filters, "limit": limit})
    result = await search(parsed)
    products = result.products[:limit]
    for product in products:
        catalog[str(product.product_id)] = _i1_dict(product)
    return [product.product_id for product in products], result.total_count


async def record_snapshots_v2(
    cases: Mapping[str, dict[str, object]],
    *,
    search: SearchFn = search_products,
    catalog_path: Path,
    responses_path: Path,
    recorded_at: str,
    target_candidates: int | None = None,
    per_query_max: int | None = None,
    relaxed_limit: int | None = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """v2 후보 provenance를 기록한다 — 케이스별 (1) 골든 expectedFilters 검색(limit
    ``per_query_max``, 기본 30)을 기록하고, (2) 후보가 목표(기본 30) 미만이면 완화 검색
    변형을 **별도 요청**(limit ``relaxed_limit``, 기본은 ``per_query_max``와 동일)으로
    추가 기록한다.

    v1 ``record_snapshots``와 같은 ``search`` 주입 시그니처를 그대로 쓴다 — 검색 함수 자체는
    바뀐 것이 없고, 요청을 여러 번(골든 1회 + 완화 최대 2회) 보낼 수 있다는 점만 다르다.
    완화 요청은 catalog를 넓혀 하드 네거티브 채굴(inject.py)의 catalog 커버리지를 늘리는 것이
    목적이라 골든보다 큰 limit을 쓸 수 있다(#333 Part 2 §3-2).
    """
    settings = get_settings()
    limit = (
        per_query_max if per_query_max is not None else settings.goldenset_snapshot_per_query_max
    )
    resolved_relaxed_limit = relaxed_limit if relaxed_limit is not None else limit
    target = (
        target_candidates if target_candidates is not None else settings.goldenset_target_candidates
    )
    if limit <= 0:
        raise ValueError("per_query_max는 0보다 커야 합니다")
    if resolved_relaxed_limit <= 0:
        raise ValueError("relaxed_limit은 0보다 커야 합니다")
    if target <= 0:
        raise ValueError("target_candidates는 0보다 커야 합니다")
    if not recorded_at.strip():
        raise ValueError("recorded_at은 비어 있을 수 없습니다")

    catalog: dict[str, dict] = {}
    responses: dict[str, dict] = {}
    for fixture_id in sorted(cases):
        expected_filters = dict(cases[fixture_id])
        primary_ids, total_count = await _search_ids(search, expected_filters, limit, catalog)
        candidates: dict[int, dict] = {
            product_id: {
                "productId": product_id,
                "source": "golden_filter",
                "rule": None,
                "from": "primary",
            }
            for product_id in primary_ids
        }
        if len(candidates) < target:
            for label, relaxed_filters in _relaxed_variants(expected_filters):
                if len(candidates) >= target:
                    break
                relaxed_ids, _ = await _search_ids(
                    search, relaxed_filters, resolved_relaxed_limit, catalog
                )
                # relaxed_ids 전량은 catalog 확장에 이미 반영됐다(_search_ids가 응답 전체를
                # catalog에 기록) — 여기서는 이 케이스의 골든 후보 목록만 target까지 채운다.
                # 그래야 relaxed_limit(카탈로그 확장용, 최대 120)이 그대로 후보 수가 되지
                # 않는다 — 실측(Part 2 라이브 실행)에서 89~120건까지 새는 것을 확인했다.
                for product_id in relaxed_ids:
                    if len(candidates) >= target:
                        break
                    if product_id not in candidates:
                        candidates[product_id] = {
                            "productId": product_id,
                            "source": "golden_filter",
                            "rule": "broadened_search",
                            "from": label,
                        }
        ordered_ids = sorted(candidates)
        responses[fixture_id] = {
            "request": expected_filters,
            "productIds": ordered_ids,
            "totalCount": total_count,
            "recordedAt": recorded_at,
            "source": "live-spring-i1",
            "candidates": [candidates[product_id] for product_id in ordered_ids],
        }
    ordered_catalog = {key: catalog[key] for key in sorted(catalog, key=lambda value: int(value))}
    _write_json(catalog_path, ordered_catalog)
    _write_json(responses_path, responses)
    return ordered_catalog, responses
