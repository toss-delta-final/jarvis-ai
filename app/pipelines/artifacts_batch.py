"""AI 생성물 갱신 배치 — I-17 pull 러너 (api-spec §4.8, C-4, 이슈 #7).

fetch_product_changes 로 변경분을 커서 기반 pull(hasMore 루프) → HIDDEN 은 생성물 삭제 →
ON_SALE 은 enrich(Haiku) → search_doc 조립 → 임베딩 → artifact_store upsert. 커서는 페이지 처리
성공 후에만 전진한다.

[#325] ON_SALE 단건 실패는 격리 후보를 실패 **종류**로 가른다 — enrich_product(LLM 호출+
JSON 파싱) 단계의 내용 실패만 attempts 회 재시도 후 격리(dead-letter 기록)하고 페이지를
계속 진행한다(head-of-line blocking 해소). embed()·store.upsert() 같은 인프라 실패와,
enrichment 단계라도 재시도 소진 후 타임아웃 계열로 판정되는 실패는 항목 내용과 무관한
광역 장애로 보고 원칙적으로 격리하지 않고 그대로 전파한다 — 그 페이지는 커서를 전진시키지
않고 자연 복구(동일 커서 재개)로 되돌아간다. 페이지 ON_SALE 실패 비율 가드는 3선 방어다
(인프라는 멀쩡한데 enrichment 결과가 대량으로 깨지는 경우, 예: 프롬프트 회귀).

[#325 R4] 다만 광역 장애와 항목 고유 결정적 실패는 단일 주기 관측으로는 원리적으로 구별할
수 없다 — 실제로 둘을 가르는 신호는 시간이다. 그래서 위 전파 대상(embed·store 인프라 실패,
타임아웃 계열)은 상품별 연속 실패 스트릭(주기 간 유지)을 세고, 같은 상품이
``artifacts_batch_item_dead_letter_cycles``(기본 3주기 ≈ 15분)만큼 연속 실패하면 항목 고유
실패로 확정해 격리한다. 그 미만이면 종전대로 전파(자연 복구)한다 — "언제까지나 막히지
않는다"를 보장하는 2선.

[#325 R5] 3선(비율 가드)에는 R4의 시간 유계가 걸려 있지 않았다 — 1선이 다건을 매 주기
즉시 격리하는 광역 파손(프롬프트 회귀 등)에서는 2선 스트릭이 쌓이지 않으므로, 비율 가드만
같은 커서에서 매 주기 반복 발동해 커서가 영원히 전진하지 않을 수 있었다(3선이 스스로 #325
의 무기한 정지를 재현). 그래서 같은 커서에서 비율 가드가 연속 발동한 횟수를 세고,
``artifacts_batch_page_failure_max_cycles``(기본 3주기 ≈ 15분)에 도달하면 그 페이지를
격리(항목은 이미 1·2선에서 dead-letter 기록됨)하고 커서를 전진시킨다. 다만 HIDDEN 삭제
실패·status 계약 위반(ProductChange 단계)은 이 시간 유계의 대상이 아니다 — api-spec §4.8 이
명시한 fail-closed 규약(항목별 ack/DLQ 계약 부재)이라 계속 무기한 전파한다.

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
from app.core.llm import LLMClient, get_llm, is_timeout_error
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

# [#325 R4] 상품별 연속 실패 스트릭(product_id → 연속 실패 횟수, 주기 간 유지) — 광역 장애와
# 항목 고유 결정적 실패를 단일 주기 관측만으로는 구별할 수 없어, "몇 주기째 같은 자리에서
# 계속 실패하는가"라는 시간 신호로 가른다(config.artifacts_batch_item_dead_letter_cycles).
# 스케줄러 잡은 max_instances=1·단일 프로세스 전제(scheduler.py docstring)라 프로세스 메모리로
# 충분하다. 영속화하지 않는다(인메모리, human gate 대상 — 이번 범위 밖) — 프로세스 재시작 시
# 리셋되는 것은 의도된 단순화다(최악의 경우 재시작 직후 몇 주기를 더 쓸 뿐). 성공 시 해당
# product_id 엔트리를 삭제해 "연속" 실패만 센다.
_item_failure_streaks: dict[int, int] = {}
# 방어적 메모리 상한(튜너블 아님 — 운영 조정 대상이 아니라 순수 메모리 방어). 정상 동작에서는
# 실패 중인 상품만 남으므로 실질적으로 도달하지 않는다.
_ITEM_FAILURE_STREAK_MAX_ENTRIES = 10_000


def reset_item_failure_streaks() -> None:
    """모듈 수준 상품별 연속 실패 스트릭을 비운다 — 테스트 간 격리용(#325 R4)."""
    _item_failure_streaks.clear()


def _bump_item_failure_streak(product_id: int) -> int:
    """product_id 의 연속 실패 횟수를 1 늘리고 반환한다. 상한 초과 시 방어적으로 전체를 비운다."""
    if (
        product_id not in _item_failure_streaks
        and len(_item_failure_streaks) >= _ITEM_FAILURE_STREAK_MAX_ENTRIES
    ):
        _log.warning(
            "I-17 항목 실패 스트릭 캐시가 상한(%d)에 도달 — 방어적으로 비움",
            _ITEM_FAILURE_STREAK_MAX_ENTRIES,
        )
        _item_failure_streaks.clear()
    streak = _item_failure_streaks.get(product_id, 0) + 1
    _item_failure_streaks[product_id] = streak
    return streak


# [#325 R5] 커서별 3선(비율 가드) 연속 발동 횟수(cursor → 연속 발동 횟수, 주기 간 유지) — 2선
# (R4)과 같은 시간 신호를 3선에도 적용한다. 1선이 다건을 매 주기 즉시 격리하는 광역 파손에서는
# 2선 스트릭이 쌓이지 않아 그 상한이 걸리지 않고, 비율 가드만 같은 커서에서 매 주기 반복
# 발동해 커서가 영원히 전진하지 않을 수 있다(config.artifacts_batch_page_failure_max_cycles).
# 키는 그 페이지를 가져온 커서(fetch 에 넘긴 값)다 — hasMore 로 여러 페이지를 도는 경우
# 페이지마다 다른 페이지 정체성을 나타낸다. 프로세스 메모리, 재시작 시 리셋(의도된 단순화).
_page_failure_streaks: dict[str, int] = {}
# 방어적 메모리 상한(튜너블 아님) — _ITEM_FAILURE_STREAK_MAX_ENTRIES 와 같은 방식.
_PAGE_FAILURE_STREAK_MAX_ENTRIES = 10_000


def reset_page_failure_streaks() -> None:
    """모듈 수준 커서별 3선(비율 가드) 연속 발동 횟수를 비운다 — 테스트 간 격리용(#325 R5)."""
    _page_failure_streaks.clear()


def reset_batch_failure_state() -> None:
    """artifacts_batch 모듈 수준 실패 상태(항목 스트릭 + 페이지 스트릭)를 모두 비운다(#325 R5)."""
    reset_item_failure_streaks()
    reset_page_failure_streaks()


def _bump_page_failure_streak(cursor: str) -> int:
    """cursor 의 비율 가드 연속 발동 횟수를 1 늘리고 반환한다. 상한 초과 시 방어적으로 전체를 비운다."""
    if (
        cursor not in _page_failure_streaks
        and len(_page_failure_streaks) >= _PAGE_FAILURE_STREAK_MAX_ENTRIES
    ):
        _log.warning(
            "I-17 페이지 실패 스트릭 캐시가 상한(%d)에 도달 — 방어적으로 비움",
            _PAGE_FAILURE_STREAK_MAX_ENTRIES,
        )
        _page_failure_streaks.clear()
    streak = _page_failure_streaks.get(cursor, 0) + 1
    _page_failure_streaks[cursor] = streak
    return streak


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
                scan_max_values=(settings.color_synonym_harvest_scan_max_values_per_product),
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


async def _enrich_change(
    change: ProductChange, *, llm: LLMClient, settings: Settings
) -> tuple[dict, dict]:
    """ON_SALE 항목의 enrich_product 단계만 수행한다 — 격리 후보가 되는 유일한 단계(#325 R3).

    embed()·store.upsert() 는 이 함수 밖(``_finish_change``)에서 수행되므로, 그 실패는 이
    함수의 예외 표면에 섞이지 않는다. ``_drain`` 의 단건 재시도/격리 루프가 이 함수만
    감싸는 구조로 embed·store 실패를 격리 경로에서 원천 배제한다(타입 매칭이 아니라
    "어느 단계가 실패했는가"라는 구조로 판정).
    """
    product = {
        "name": change.name,
        "description": change.description,
        "category": change.category,
        "brand": change.brand,
        "attributes": change.attributes,
    }
    extras = await enrich_product(product, llm=llm, settings=settings)
    return product, extras


async def _finish_change(
    change: ProductChange,
    product: dict,
    extras: dict,
    *,
    embed: Embed,
    store: ArtifactStore,
    settings: Settings,
) -> None:
    """enrich 이후 단계(임베딩·upsert·색상 수확) — 이 단계 실패는 격리하지 않고 그대로 전파한다.

    embed()·store.upsert() 실패는 항목 내용과 무관한 인프라 장애(#325 R3 규칙 1)이므로
    여기서 삼키지 않는다 — 호출부(``_drain``)가 그대로 전파받아 페이지 커서를 전진시키지
    않는다(자연 복구).
    """
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


async def _process_change(
    change: ProductChange,
    *,
    llm: LLMClient,
    embed: Embed,
    store: ArtifactStore,
    settings: Settings,
) -> None:
    """ON_SALE 항목 1건을 처음부터 끝까지 처리한다(enrich → embed/upsert/색상 수확).

    ``_drain`` 은 이 함수를 그대로 쓰지 않고 ``_enrich_change``/``_finish_change`` 를 나눠
    호출한다 — 재시도·격리는 enrich 단계에만 걸려야 하기 때문이다(#325 R3). 이 함수는
    단건 처리가 필요한 다른 호출부(테스트 등)를 위한 합성 편의 함수다.
    """
    product, extras = await _enrich_change(change, llm=llm, settings=settings)
    await _finish_change(change, product, extras, embed=embed, store=store, settings=settings)


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

    격리 대상은 실패 **종류**로 가른다(#325 R3) — 비율은 poison 단건과 광역 장애를 구별하는
    대리 지표일 뿐이고, 운영 증분 페이지처럼 소량 표본에서는 그 대리가 무너진다. 다만 종류
    판정도 단일 주기 관측만으로는 광역 장애와 항목 고유 결정적 실패를 구별하지 못하므로(#325
    R4), "그 종류"로 전파 대상이 된 실패는 상품별 연속 실패 스트릭으로 시간 유계를 건다:

    - **성공** → ``_item_failure_streaks`` 에서 해당 product_id 를 삭제(연속만 센다).
    - **1선(구조) — enrich 내용 실패**: ``_enrich_change``(enrich_product 단계)의 내용 실패만
      settings.enrichment_item_attempts 회 재시도 후 **즉시** 격리(dead-letter 로그)하고 다음
      항목으로 진행한다(#325 head-of-line blocking 해소). 정의상 항목 고유 실패이므로 스트릭
      판정을 거치지 않는다 — 스트릭도 삭제한다(격리했으므로 다음 주기 큐에 없다).
    - **2선(시간 유계) — 타임아웃·embed·store**: enrich 재시도 소진 후 그 예외(또는 원인
      체인)가 ``app.core.llm.is_timeout_error`` 로 타임아웃 계열이거나, ``_finish_change``
      (embed()·store.upsert())가 실패하면 — 항목 내용과 무관한 인프라 장애 후보이지만 단일
      주기로는 poison 단건과 구별 불가하므로 — 해당 product_id 스트릭을 +1 한다.
      스트릭이 settings.artifacts_batch_item_dead_letter_cycles 미만이면 종전대로 **전파**
      (자연 복구, 커서 미전진) — WARNING 로 현재 스트릭/상한을 남긴다. 상한 이상이면 항목
      고유 실패로 확정해 **격리**(dead-letter ERROR, product_id·스트릭·상한·단계 표기)하고
      다음 항목으로 계속한다 — failed 증가, 스트릭 삭제.
    - **3선(비율, 방어)**: 페이지의 ON_SALE 표본이 settings.artifacts_batch_failure_min_sample
      이상이고 실패 비율이 settings.artifacts_batch_failure_ratio_threshold 이상이면 — 그
      페이지를 가져온 커서(fetch 에 넘긴 값)의 연속 발동 횟수를 +1 한다. 표본이 min_sample
      미만이면 비율 판정을 생략하고 격리+전진한다.
      - 연속 발동이 settings.artifacts_batch_page_failure_max_cycles 미만이면 종전대로
        PageFailureThresholdExceeded 를 던져 커서를 전진시키지 않는다(자연 복구) — WARNING 로
        현재 연속 횟수/상한을 남긴다. 인프라는 멀쩡한데 enrichment 결과 자체가 대량으로 깨지는
        경우(프롬프트 회귀 등)를 잡는다.
      - 상한 이상이면 대량 파손이 자연 회복되지 않는 것으로 확정해 **던지지 않는다** — ERROR 로
        연속 횟수/상한·failed·page_total·커서를 남기고 그 페이지를 그대로 커서 전진시킨다
        (항목들은 이미 1선/2선에서 dead-letter 기록됨). 연속 발동 카운터는 삭제한다(#325 R5) —
        1선이 다건을 매 주기 즉시 격리하는 광역 파손에서는 2선 스트릭이 쌓이지 않아 그 상한이
        걸리지 않고, 이 3선 시간 유계가 없으면 비율 가드 자체가 #325 의 무기한 정지를
        재현한다. 페이지가 비율 임계를 넘지 않고 정상 종료하면 그 커서의 카운터를 삭제한다
        (연속만 센다).

    HIDDEN 삭제 실패·status 파싱 실패(ProductChange 단계)는 이 시간 유계의 대상이 아니라
    그대로(무기한) 전파한다(스트릭 대상도 아니다) — api-spec §4.8 이 명시적으로 정한 fail-closed
    규약이다: 항목별 ack/DLQ 계약이 없어 skip-전진하면 삭제 이벤트가 영구 유실되므로, 승인받은
    개정 범위 밖인 이 경로만은 시간 유계에서 제외된다. 이미 성공한 앞 페이지는 artifact와
    커서가 함께 저장된 유효 체크포인트이므로 롤백하지 않는다.
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
            product = extras = None
            for _attempt in range(settings.enrichment_item_attempts):
                try:
                    product, extras = await _enrich_change(change, llm=llm, settings=settings)
                except Exception as exc:  # noqa: BLE001 - enrich 단계만 격리 후보(#325 R3)
                    last_exc = exc
                    continue
                last_exc = None
                break

            stage_exc: Exception | None = None
            stage = ""
            if last_exc is not None and not is_timeout_error(last_exc):
                # enrichment 내용 실패(타임아웃 아님) — 정의상 항목 고유 실패이므로 스트릭
                # 판정 없이 즉시 격리한다(#325 R3 규칙 1 그대로).
                _item_failure_streaks.pop(change.product_id, None)
                failed += 1
                page_failed += 1
                _log.error(
                    "I-17 항목 실패 — 격리 기록 후 계속: product_id=%s attempts=%d",
                    change.product_id,
                    settings.enrichment_item_attempts,
                    exc_info=last_exc,
                )
                continue
            if last_exc is not None:
                # enrichment 재시도 소진 후에도 타임아웃 계열 — 광역 장애 후보, 스트릭 판정(아래).
                stage_exc = last_exc
                stage = "enrichment_timeout"
            else:
                # enrichment 성공 — 나머지 단계(embed·upsert·색상 수확)는 인프라 장애 후보다.
                assert product is not None and extras is not None
                try:
                    await _finish_change(
                        change, product, extras, embed=embed, store=target, settings=settings
                    )
                except Exception as exc:  # noqa: BLE001 - 스트릭 판정 대상(#325 R4)
                    stage_exc = exc
                    stage = "finish"

            if stage_exc is None:
                _item_failure_streaks.pop(change.product_id, None)
                processed += 1
                page_succeeded += 1
                continue

            # [#325 R4] 단일 주기로는 광역 장애와 항목 고유 결정적 실패를 구별할 수 없다 —
            # 같은 상품이 주기를 가로질러 연속 실패한 횟수로 시간 유계를 건다.
            streak = _bump_item_failure_streak(change.product_id)
            cycles_limit = settings.artifacts_batch_item_dead_letter_cycles
            if streak < cycles_limit:
                _log.warning(
                    "I-17 항목 연속 실패 — 전파(자연 복구): product_id=%s stage=%s streak=%d/%d",
                    change.product_id,
                    stage,
                    streak,
                    cycles_limit,
                )
                raise stage_exc
            _item_failure_streaks.pop(change.product_id, None)
            failed += 1
            page_failed += 1
            _log.error(
                "I-17 항목 연속 실패 상한 도달 — 격리 기록 후 계속: product_id=%s stage=%s "
                "streak=%d/%d",
                change.product_id,
                stage,
                streak,
                cycles_limit,
                exc_info=stage_exc,
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
                _page_failure_streaks.pop(cursor, None)
            else:
                ratio = page_failed / page_total
                if ratio >= settings.artifacts_batch_failure_ratio_threshold:
                    # [#325 R5] 3선도 2선(R4)과 같은 시간 유계 — 같은 커서에서 비율 가드가
                    # 연속 발동한 횟수로 대량 파손의 자연 회복 여부를 판정한다.
                    page_streak = _bump_page_failure_streak(cursor)
                    page_cycles_limit = settings.artifacts_batch_page_failure_max_cycles
                    if page_streak < page_cycles_limit:
                        _log.warning(
                            "I-17 페이지 실패율 임계 연속 발동 — 전파(자연 복구): "
                            "cursor=%s failed=%d total=%d ratio=%.2f streak=%d/%d",
                            cursor,
                            page_failed,
                            page_total,
                            ratio,
                            page_streak,
                            page_cycles_limit,
                        )
                        raise PageFailureThresholdExceeded(
                            f"I-17 페이지 실패율 임계 초과: failed={page_failed} "
                            f"total={page_total} ratio={ratio:.2f}"
                        )
                    _page_failure_streaks.pop(cursor, None)
                    _log.error(
                        "I-17 페이지 실패율 임계 연속 발동 상한 도달 — 격리 후 전진: "
                        "cursor=%s failed=%d total=%d ratio=%.2f streak=%d/%d",
                        cursor,
                        page_failed,
                        page_total,
                        ratio,
                        page_streak,
                        page_cycles_limit,
                    )
                else:
                    _page_failure_streaks.pop(cursor, None)
        else:
            _page_failure_streaks.pop(cursor, None)
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
