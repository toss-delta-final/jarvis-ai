"""AI 생성물 갱신 배치 — I-17 pull 러너 (api-spec §4.8, C-4, 이슈 #7).

fetch_product_changes 로 변경분을 커서 기반 pull(hasMore 루프) → DELISTED 는 생성물 삭제 →
나머지는 enrich(Haiku) → search_doc 조립 → 임베딩 → artifact_store upsert. 커서는 페이지 처리
성공 후에만 전진(자연 복구, §4.8). 초기 전체 구축은 full_rebuild=True(since="0").

fetch·llm·embed·store 는 주입형(테스트·오프라인 대체) — torch 미설치 환경에서도 embed 주입으로 동작.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.core.llm import LLMClient, get_llm
from app.pipelines import embedding as _embedding
from app.pipelines.artifact_store import (
    CatalogArtifact,
    CatalogArtifactStore,
    get_catalog_store,
)
from app.pipelines.enrichment import enrich_product
from app.schemas.spring import ProductChange, ProductChangesPage
from app.services import spring_client

_log = logging.getLogger(__name__)
_DELISTED = "DELISTED"

Fetch = Callable[[str | None, int], Awaitable[ProductChangesPage]]
Embed = Callable[[list[str]], list[list[float]]]


@dataclass
class BatchResult:
    processed: int
    delisted: int
    pages: int
    cursor: str | None


async def _process_change(
    change: ProductChange,
    *,
    llm: LLMClient,
    embed: Embed,
    store: CatalogArtifactStore,
    settings: Settings,
) -> None:
    product = {
        "name": change.name,
        "description": change.description,
        "category": change.category,
        "brand": change.brand,
        "attributes": change.attributes,
    }
    extras = await enrich_product(product, llm=llm, settings=settings)
    doc = _embedding.build_search_doc({**product, "extras": extras})
    vec = embed([doc])[0]
    store.upsert(
        CatalogArtifact(
            product_id=change.product_id,
            search_doc=doc,
            embedding=vec,
            extras=extras,
            name=change.name,
            category=change.category,
        )
    )


async def run_artifacts_batch(
    *,
    fetch: Fetch | None = None,
    llm: LLMClient | None = None,
    embed: Embed | None = None,
    store: CatalogArtifactStore | None = None,
    settings: Settings | None = None,
    full_rebuild: bool = False,
) -> BatchResult:
    """I-17 배치 1회 실행. full_rebuild=True 면 since="0" 초기 전체 구축."""
    settings = settings or get_settings()
    fetch = fetch or spring_client.fetch_product_changes
    llm = llm or get_llm()
    embed = embed or _embedding.embed_texts
    store = store or get_catalog_store()
    if llm is None:
        raise RuntimeError(
            "run_artifacts_batch: LLM 미구성 — enrichment 불가(config anthropic_api_key)"
        )

    cursor = "0" if full_rebuild else (store.get_cursor() or "0")
    processed = delisted = pages = 0
    while True:
        page = await fetch(cursor, settings.catalog_batch_page_size)
        for change in page.items:
            if change.status == _DELISTED:
                store.delete(change.product_id)
                delisted += 1
                continue
            await _process_change(change, llm=llm, embed=embed, store=store, settings=settings)
            processed += 1
        pages += 1
        if page.next_cursor:
            cursor = page.next_cursor
        store.set_cursor(cursor)  # 페이지 처리 성공 후에만 전진(자연 복구)
        if not page.has_more:
            break
        if not page.next_cursor:
            _log.warning("hasMore=True 이나 nextCursor 없음 — 배치 중단(무한루프 방지)")
            break

    _log.info("artifacts batch: processed=%d delisted=%d pages=%d", processed, delisted, pages)
    return BatchResult(processed=processed, delisted=delisted, pages=pages, cursor=cursor)
