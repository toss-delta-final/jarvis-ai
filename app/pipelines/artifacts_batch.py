"""AI 생성물 갱신 배치 — I-17 pull 러너 (api-spec §4.8, C-4, 이슈 #7).

fetch_product_changes 로 변경분을 커서 기반 pull(hasMore 루프) → HIDDEN 은 생성물 삭제 →
ON_SALE 은 enrich(Haiku) → search_doc 조립 → 임베딩 → artifact_store upsert. 커서는 페이지 처리
성공 후에만 전진한다.

[#325] ON_SALE 단건 실패(예: enrichment 파싱 실패)는 attempts 회 재시도 후 격리(dead-letter
기록)하고 페이지를 계속 진행한다 — 실패 상품 1개가 그 뒤 모든 변경을 영구 차단하던
head-of-line blocking 을 없앤다. 페이지 실패 비율이 임계 이상이면(광역 장애로 간주) 그
페이지는 커서를 전진시키지 않고 예외를 던져 자연 복구(동일 커서 재개)로 되돌아간다.

증분(기본): 대상 스토어에 직접 upsert 하고 페이지마다 커서 전진.
전체 재구축(full_rebuild): since="0" 부터 **임시 스토어**에 쌓은 뒤, 성공 시 원자 교체(replace_all)한다
  — 재구축 중 실패해도 기존 정상 데이터가 보존되고, 더 이상 존재하지 않는 상품의 stale artifact 가 제거된다.

fetch·llm·embed·store 는 주입형(테스트·오프라인 대체) — torch 미설치 환경에서도 embed 주입으로 동작.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.core.llm import LLMClient, get_llm
from app.pipelines import embedding as _embedding
from app.pipelines import color_synonym_seed
from app.pipelines.artifact_store import (
    ArtifactStore,
    CatalogArtifact,
    CatalogArtifactStore,
    get_catalog_store,
)
from app.pipelines.enrichment import enrich_product
from app.schemas.spring import ProductChange, ProductChangesPage
from app.services import spring_client

_log = logging.getLogger(__name__)
_HIDDEN = "HIDDEN"

Fetch = Callable[[str | None, int], Awaitable[ProductChangesPage]]
Embed = Callable[[list[str]], list[list[float]]]
_harvest_limiters: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_harvest_limiter_lock = threading.Lock()
_background_harvest_tasks: set[asyncio.Task[int]] = set()


@dataclass
class BatchResult:
    processed: int
    hidden: int
    pages: int
    cursor: str | None
    # [#325] 격리된 단건 ON_SALE 실패 수(dead-letter 기록됨) — 관측 사각도 해소.
    failed: int = 0


class PageFailureThresholdExceeded(RuntimeError):
    """페이지 ON_SALE 실패 비율이 임계 이상 — 광역 장애로 보고 커서를 전진시키지 않는다(#325)."""


def _harvest_limiter(dsn: str, max_concurrency: int) -> threading.BoundedSemaphore:
    key = (dsn, max_concurrency)
    limiter = _harvest_limiters.get(key)
    if limiter is None:
        with _harvest_limiter_lock:
            limiter = _harvest_limiters.get(key)
            if limiter is None:
                limiter = threading.BoundedSemaphore(max_concurrency)
                _harvest_limiters[key] = limiter
    return limiter


def _consume_background_harvest(task: asyncio.Task[int]) -> None:
    """타임아웃 뒤 shield task의 늦은 실패를 기록하고 예외를 회수한다."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.warning(
            "색상 표기 백그라운드 수확 실패",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


async def _harvest_change_colors(change: ProductChange, *, settings: Settings) -> int:
    """I-17 한 변경분의 신규 색상 표기를 동기 DB/API 작업 스레드에서 pending으로 제안한다."""
    embed = functools.partial(_embedding.embed_texts, task_type=settings.embedding_task_document)
    limiter = _harvest_limiter(
        settings.catalog_db_url,
        settings.color_synonym_harvest_max_concurrency,
    )
    if not limiter.acquire(blocking=False):
        _log.warning("색상 표기 수확 동시 실행 상한 — 해당 change 수확 건너뜀")
        return 0

    def run() -> int:
        try:
            return color_synonym_seed.harvest_new_terms(
                settings.catalog_db_url,
                change.attributes,
                embed,
                settings.embedding_model_id,
                settings.color_synonym_cluster_threshold,
                max_terms=settings.color_synonym_harvest_max_terms_per_product,
                max_term_length=settings.color_synonym_harvest_max_term_length,
                scan_max_values=(
                    settings.color_synonym_harvest_scan_max_values_per_product
                ),
            )
        finally:
            # wait_for가 먼저 끝나도 실제 worker가 종료될 때까지 슬롯을 계속 점유한다.
            limiter.release()

    task = asyncio.create_task(asyncio.to_thread(run))
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=settings.color_synonym_query_timeout_s,
        )
    except (TimeoutError, asyncio.CancelledError):
        # timeout·호출자 취소 뒤에도 worker는 shield 아래 계속 돈다. 완료 시 예외를 회수하되,
        # 현재 호출의 기존 실패/취소 전파 규약은 그대로 유지한다.
        _background_harvest_tasks.add(task)
        task.add_done_callback(_background_harvest_tasks.discard)
        task.add_done_callback(_consume_background_harvest)
        raise


async def _process_change(
    change: ProductChange,
    *,
    llm: LLMClient,
    embed: Embed,
    store: ArtifactStore,
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
            # 임베딩 프로비넌스(이슈 #65, embedding_meta_complete CHECK 대응).
            # embed_dim 은 실제 반환 벡터 길이에서 도출 — embed 주입 교체 시에도 기록값이
            # 실제 벡터와 어긋나지 않는다(PR 리뷰). model·task 는 벡터에 없어 settings 소관.
            embed_model=settings.embedding_model_id,
            embed_dim=len(vec),
            embed_task=settings.embedding_task_document,
            normalized=settings.embedding_normalized,
        )
    )
    # 기본 off: 새 표기마다 임베딩 API와 DB write가 추가되고 검수 테이블도 아직 미검수다.
    # 초기 검수 완료 뒤에만 켜며, 어떤 실패도 본 I-17 생성물 갱신으로 전파하지 않는다.
    if settings.color_synonym_batch_harvest_enabled:
        try:
            await _harvest_change_colors(change, settings=settings)
        except Exception:
            _log.warning("색상 표기 수확 실패 — I-17 생성물 갱신은 계속", exc_info=True)


async def _drain(
    fetch: Fetch,
    start_cursor: str,
    target: ArtifactStore,
    *,
    llm: LLMClient,
    embed: Embed,
    settings: Settings,
    persist_cursor: bool,
) -> BatchResult:
    """start_cursor 부터 hasMore 소진까지 target 에 반영한다. persist_cursor=True 면 페이지마다 커서 전진.

    ON_SALE 단건 실패는 settings.enrichment_item_attempts 회 재시도 후 격리(dead-letter 로그)하고
    다음 항목으로 진행한다(#325 head-of-line blocking 해소). 페이지의 ON_SALE 표본이
    settings.artifacts_batch_failure_min_sample 이상이고 실패 비율이
    settings.artifacts_batch_failure_ratio_threshold 이상이면 광역 장애로 보고
    PageFailureThresholdExceeded 를 던진다 — 그 페이지는 커서를 전진시키지 않는다(자연 복구).
    표본이 min_sample 미만이면 비율 판정을 생략하고 격리+전진한다 — 운영 증분 페이지는 대개
    소수 항목이라(#325) poison 단건과 광역 장애를 비율만으로 구별할 수 없기 때문이다.
    HIDDEN 삭제 실패·status 파싱 실패(ProductChange 단계)는 격리 대상이 아니라 그대로 전파한다.
    이미 성공한 앞 페이지는 artifact와 커서가 함께 저장된 유효 체크포인트이므로 롤백하지 않는다.
    """
    cursor = start_cursor
    processed = hidden = pages = failed = 0
    while True:
        page = await fetch(cursor, settings.catalog_batch_page_size)
        page_failed = page_succeeded = 0
        for change in page.items:
            if change.status == _HIDDEN:
                target.delete(change.product_id)
                hidden += 1
                continue
            last_exc: Exception | None = None
            for _attempt in range(settings.enrichment_item_attempts):
                try:
                    await _process_change(
                        change, llm=llm, embed=embed, store=target, settings=settings
                    )
                except Exception as exc:  # noqa: BLE001 - 항목 격리(#325), 다음 시도/항목으로 계속
                    last_exc = exc
                    continue
                last_exc = None
                break
            if last_exc is None:
                processed += 1
                page_succeeded += 1
            else:
                failed += 1
                page_failed += 1
                _log.error(
                    "I-17 항목 실패 — 격리 기록 후 계속: product_id=%s attempts=%d",
                    change.product_id,
                    settings.enrichment_item_attempts,
                    exc_info=last_exc,
                )
        pages += 1
        page_total = page_failed + page_succeeded
        if page_failed > 0 and page_total > 0:
            if page_total < settings.artifacts_batch_failure_min_sample:
                _log.warning(
                    "I-17 페이지 표본 부족으로 광역장애 판정 생략 — "
                    "failed=%d total=%d min_sample=%d (격리 후 전진)",
                    page_failed,
                    page_total,
                    settings.artifacts_batch_failure_min_sample,
                )
            else:
                ratio = page_failed / page_total
                if ratio >= settings.artifacts_batch_failure_ratio_threshold:
                    raise PageFailureThresholdExceeded(
                        f"I-17 페이지 실패율 임계 초과: failed={page_failed} total={page_total} "
                        f"ratio={ratio:.2f}"
                    )
        if page.next_cursor:
            cursor = page.next_cursor
        if persist_cursor:
            target.set_cursor(cursor)  # 페이지 처리 성공 후에만 전진
        if not page.has_more:
            break
        if not page.next_cursor:
            _log.warning("hasMore=True 이나 nextCursor 없음 — 배치 중단(무한루프 방지)")
            break
    return BatchResult(
        processed=processed, hidden=hidden, pages=pages, cursor=cursor, failed=failed
    )


async def run_artifacts_batch(
    *,
    fetch: Fetch | None = None,
    llm: LLMClient | None = None,
    embed: Embed | None = None,
    store: ArtifactStore | None = None,
    settings: Settings | None = None,
    full_rebuild: bool = False,
) -> BatchResult:
    """I-17 배치 1회 실행. full_rebuild=True 면 since="0" 초기 전체 구축(원자 교체)."""
    settings = settings or get_settings()
    fetch = fetch or spring_client.fetch_product_changes
    llm = llm or get_llm()
    # 미주입 기본값은 문서(document) 임베딩 — 비대칭 임베딩 바인딩(이슈 #65)
    embed = embed or functools.partial(
        _embedding.embed_texts, task_type=settings.embedding_task_document
    )
    store = store or get_catalog_store()
    if llm is None:
        raise RuntimeError(
            "run_artifacts_batch: LLM 미구성 — enrichment 불가(config anthropic_api_key)"
        )

    async def rebuild() -> BatchResult:
        # 임시 스토어에 전체 구축 후 성공 시 원자 교체 — 중간 실패해도 기존 데이터 보존 + stale 제거.
        work = CatalogArtifactStore()
        rebuilt = await _drain(
            fetch, "0", work, llm=llm, embed=embed, settings=settings, persist_cursor=False
        )
        store.replace_all_and_set_cursor(work.all(), rebuilt.cursor)
        return rebuilt

    did_rebuild = full_rebuild
    if full_rebuild:
        result = await rebuild()
    else:
        start = store.get_cursor() or "0"
        try:
            result = await _drain(
                fetch, start, store, llm=llm, embed=embed, settings=settings, persist_cursor=True
            )
        except spring_client.InvalidCursorError:
            checkpoint = store.get_cursor()
            if start == "0" and checkpoint in (None, "0"):
                raise
            # 앞서 성공한 페이지가 있으면 그 artifact·cursor 체크포인트는 유지한다. rebuild는 별도
            # 임시 스토어에서 수행하므로 실패해도 이 마지막 성공 체크포인트를 덮어쓰지 않는다.
            # 최초 실행도 실제 커서가 0에서 전진했다면 이후 INVALID_CURSOR를 즉시 복구한다.
            _log.warning("I-17 커서 무효 — since=0 원자적 전체 재구축으로 복구")
            result = await rebuild()
            did_rebuild = True

    _log.info(
        "artifacts batch: processed=%d hidden=%d pages=%d failed=%d rebuild=%s",
        result.processed,
        result.hidden,
        result.pages,
        result.failed,
        did_rebuild,
    )
    return result
