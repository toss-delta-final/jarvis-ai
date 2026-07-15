"""카탈로그 검색 서비스 — SearchBackend 심(seam) (확정 2026-07-15).

MVP: 질의 시점 Spring 위임. decompose 가 만든 필터를 spring_client.search_products 로 넘기고,
최근 구매 dedup(exclude_product_ids)을 적용한다. Spring 이 price/stock 을 함께 반환한다.

[OPEN — 질의 시점 후보 흐름, api-spec §4.8 말미] 방식1(AI 벡터 검색 → Spring id 제약 조회)
vs 방식2(Spring 검색 → 임베딩 재정렬 보조) 병행 검토 — SearchBackend 인터페이스로 양쪽
교체 가능하게 유지하고 골든셋/실측으로 확정한다. AI 생성물(임베딩)은 MVP 소속으로 I-8
배치(§4.8)가 갱신한다. 상품 원본 컬럼 미러는 영구 미채택.
"""

from __future__ import annotations

from typing import Protocol

from app.schemas.spring import ProductSearchFilters, ProductSearchResult
from app.services import spring_client


class SearchBackend(Protocol):
    """검색 백엔드 계약. 기본=Spring 위임(방식2 계열), 방식1(AI 벡터 우선)로 교체 가능한 심."""

    async def search(self, filters: ProductSearchFilters) -> ProductSearchResult:
        """필터로 상품을 검색해 결과를 반환한다."""
        ...


class SpringSearchBackend:
    """MVP 백엔드 — Spring /products/search 위임."""

    async def search(self, filters: ProductSearchFilters) -> ProductSearchResult:
        """Spring 위임 검색 (dedup 은 filters.exclude_product_ids 로 전달)."""
        return await spring_client.search_products(filters)


# MVP 기본 백엔드. 후보 흐름 OPEN 확정(방식1) 시 벡터 우선 백엔드로 교체 가능(§4.8).
default_backend: SearchBackend = SpringSearchBackend()


async def search_catalog(
    filters: ProductSearchFilters,
    exclude_product_ids: list[str] | None = None,
    backend: SearchBackend | None = None,
) -> ProductSearchResult:
    """활성 백엔드로 카탈로그를 검색한다.

    exclude_product_ids: 최근 구매 dedup 대상 (GET /orders/recent 유래 §4.7, 결정 14-F).
    backend: 미지정 시 default_backend(Spring 위임) 사용 — 테스트에서 주입 가능.
    """
    used = backend or default_backend
    if exclude_product_ids:
        # dedup id 를 필터에 병합해 백엔드로 전달한다.
        merged = {*filters.exclude_product_ids, *exclude_product_ids}
        filters = filters.model_copy(update={"exclude_product_ids": sorted(merged)})
    return await used.search(filters)
