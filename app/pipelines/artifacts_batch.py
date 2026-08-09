"""AI 생성물 갱신 배치 — I-17 pull 러너 (api-spec §4.8, C-4, 이슈 #7).

fetch_product_changes 로 변경분을 커서 기반 pull(hasMore 루프) → HIDDEN 은 생성물 삭제 →
ON_SALE 은 enrich(Haiku) → search_doc 조립 → 임베딩 → artifact_store upsert. 커서는 페이지 처리
성공 후에만 전진한다.

[#325] ON_SALE 단건 실패는 격리 후보를 실패 **종류**로 가른다 — enrich_product(LLM 호출+
JSON 파싱) 단계의 증명된 콘텐츠 실패만 attempts 회 재시도 후 격리(dead-letter 기록)하고
페이지를 계속 진행한다(head-of-line blocking 해소). embed()·store.upsert() 같은 인프라
실패와, enrichment 단계라도 재시도 소진 후 콘텐츠 실패로 확정되지 않는 실패(429·연결
오류·5xx·타임아웃·모르는 예외 포함)는 항목 내용과 무관한 광역 장애 후보로 보고 원칙적으로
격리하지 않고 그대로 전파한다 — 그 페이지는 커서를 전진시키지 않고 자연 복구(동일 커서
재개)로 되돌아간다. 페이지 ON_SALE 실패 비율 가드는 3선 방어다(인프라는 멀쩡한데
enrichment 결과가 대량으로 깨지는 경우, 예: 프롬프트 회귀).

[#325 R6] 콘텐츠 실패 판정은 **화이트리스트**다(``_is_enrichment_content_failure``) —
"모르는 실패는 시간 유계 경로(2선)로 보내고, 즉시 격리(1선)는 증명된 콘텐츠 실패에만"이
기본값 방향이다. 예전(R3) 판정은 "타임아웃이 아니면 콘텐츠"라는 블랙리스트여서, 429·5xx
같은 흔한 일시적 인프라 장애가 R4·R5 의 시간 유계 보호를 우회해 첫 주기에 영구 격리됐다
(PR #399 리뷰 3차). ``is_timeout_error``(app/core/llm.py)는 사용자 대면 ``LLM_TIMEOUT``
매핑 전용이라 판정 범위를 그대로 두고, 콘텐츠 실패 판정은 별도 헬퍼로 분리했다.

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

[이슈 #416] 2선·3선 스트릭은 프로세스 메모리 dict 대신 ``ArtifactStore``
(bump_failure_streak/clear_failure_streak/purge_stale_failure_streaks, artifact_store.py)에
영속한다 — 스케줄러 프로세스가 수렴 창(기본 3주기 ≈ 15분)보다 자주 재시작되면(연속 배포·
크래시 루프) 스트릭이 매번 0으로 리셋돼 poison 상품이 dead-letter 상한에 영영 도달하지
못했다. ``artifacts_batch_failure_streak_ttl_s``(기본 1h) 안의 연속만 세어, 영속화가
무관한 과거 실패를 오늘 실패와 합치는 것을 막는다. 저장 실패 시 프로세스 메모리 폴백으로
위임해(PgCatalogArtifactStore) 종전 동작과 같은 하한을 보장한다.

[이슈 #421] 1선(enrich 증명된 콘텐츠 실패)은 즉시 영구 격리 대신
``artifacts_batch_content_retry_cycles``(기본 1주기) 만큼 다음 주기 재시도 기회를 준다 —
JSON 파싱 실패는 LLM 샘플링 노이즈(코드펜스 혼입 등)로도 나므로, 우연히 재시도 상한만큼
연속 실패한 정상 상품이 2선·3선과 달리 시간 유계 없이 첫 주기에 영구 격리되는 것을 막는다.
재시도 대기 큐(``_content_retry_queue``)는 상품 원본 필드(``ProductChange``)를 담으므로
AI DB 에 저장하지 않고 프로세스 메모리에만 둔다(CLAUDE.md 원본 컬럼 사본 금지) — 재시작에
유실되면 동작은 종전(즉시 격리)과 같아 하한이 종전이다.

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
from app.core.llm import LLMClient, LLMError, LLMNotConfigured, get_llm, is_output_length_error
from app.pipelines import embedding as _embedding
from app.pipelines import color_synonym_seed
from app.pipelines.artifact_store import (
    EXTRAS_NAME_PRESENT_KEY,
    FAILURE_STREAK_KIND_ITEM,
    FAILURE_STREAK_KIND_PAGE,
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


# [이슈 #421] 1선(enrich 증명된 콘텐츠 실패)의 cross-cycle 재시도 대기 큐 — product_id → 대기
# 항목. ProductChange 페이로드(name·description·category·brand·attributes)를 담으므로 상품
# 원본 컬럼 사본 금지(CLAUDE.md) 원칙에 따라 DB 에 저장하지 않고 프로세스 메모리에만 둔다.
# 재시작에 유실돼도 동작은 종전(즉시 격리)과 같아 하한이 종전이다(#416 의 스트릭 영속화와
# 저장처가 다른 이유). dict 삽입 순서 = 등재 순서이므로 상한 축출은 ``next(iter(...))``로
# 가장 오래된 항목을 찾는다.
@dataclass
class _PendingContentRetry:
    change: ProductChange
    # [F4 정합] "지금까지 실제로 실패로 확정된 cross-cycle 재시도 횟수" — 등재 시 0(아직 재시도
    # 실패가 한 번도 없음)에서 시작해 +1 한다. 격리 여부는
    # ``retry_attempts < artifacts_batch_content_retry_cycles`` 로 판정한다 — 이 불변식이라야
    # ``artifacts_batch_content_retry_cycles=N`` 이 "cross-cycle 재시도가 정확히 N 회 일어난
    # 뒤에야 영구 격리"라는 이름·문서 그대로의 뜻이 된다(등재 시 1로 시작하면 N≥2 에서 실제
    # 재시도가 N-1 회로 한 주기 모자라게 끝났다 — PR 리뷰 라운드 1 F4).
    #
    # [T1, PR 리뷰 라운드 2] 이 카운터를 소진시키는 "실패"는 ``_run_content_retry_pass``
    # (cross-cycle 재시도)뿐 아니라 ``_drain`` 자신의 재처리도 포함한다 — enrichment 가 실제로
    # 쓰는 필드(name·description·category·brand·attributes)가 이전 대기 항목과 같은 채로 새
    # 페이지 항목이 도착하면(가격·재고처럼 enrichment 와 무관한 필드만 갱신), 그 재실패도 같은
    # poison 내용에 대한 재시도이므로 예산을 태운다. 안 그러면 그런 항목은 매 주기
    # "등재 → 이번 주기 skip(F1) → 다음 주기 새 변경분 도착 → 0 으로 재등재"를 반복해 예산이
    # 영원히 소진되지 않는다(dead-letter ERROR 가 영영 안 뜨고 WARNING 만 반복).
    retry_attempts: int


_content_retry_queue: dict[int, _PendingContentRetry] = {}
# 방어적 메모리 상한(튜너블 아님 — 운영 조정 대상이 아니라 순수 메모리 방어).
_CONTENT_RETRY_MAX_PENDING = 1_000

# [F1, PR 리뷰 라운드 1] 이번 run_artifacts_batch 호출의 _drain 실행 중 새로 등재된
# product_id 집합 — 재시도 패스가 방금 같은 실행에서 큐에 오른 항목을 즉시 재시도하면
# "cross-cycle 예산"의 의미가 사라지므로(그 항목은 아직 다음 주기를 한 번도 안 기다렸다),
# run_artifacts_batch 가 _drain 호출 직전에 비우고 호출 직후 스냅샷을 읽어 재시도 패스에
# skip 인자로 넘긴다.
_content_retry_enqueued_this_drain: set[int] = set()


def reset_batch_failure_state() -> None:
    """artifacts_batch 모듈 수준 재시도 큐를 비운다 — 테스트 간 격리용(#421).

    2선·3선 스트릭은 이제 ArtifactStore(#416)에 살고 스토어 수명과 함께 가므로, 여기서
    비울 모듈 수준 상태는 #421 재시도 큐(및 이번 실행 등재분 추적 집합)뿐이다.
    """
    _content_retry_queue.clear()
    _content_retry_enqueued_this_drain.clear()


def _enrichment_inputs_unchanged(old: ProductChange, new: ProductChange) -> bool:
    """``_enrich_change`` 가 실제로 쓰는 필드만 비교한다(T1, PR 리뷰 라운드 2).

    가격·재고처럼 enrichment 결과에 영향을 주지 않는 필드(``updated_at`` 등)는 비교하지
    않는다 — 그런 필드만 매 주기 바뀌는 poison 상품을 "내용이 바뀌었다"고 오판하면
    cross-cycle 재시도 예산이 매번 리셋돼 결정적 실패가 영원히 dead-letter 되지 않는다.
    dict(``attributes``) 비교는 파이썬 ``==`` 가 키 순서와 무관하게 내용으로 비교하므로
    별도 정규화가 필요 없다(결정적).
    """
    return (
        old.name == new.name
        and old.description == new.description
        and old.category == new.category
        and old.brand == new.brand
        and old.attributes == new.attributes
    )


def _enqueue_content_retry(change: ProductChange, *, retry_attempts: int = 0) -> bool:
    """콘텐츠 실패 항목을 다음 주기 재시도 큐에 (재)등재한다(#421). 반환값은 상한 축출 발생 여부.

    ``retry_attempts``(기본 0 — 신규 콘텐츠)는 호출부(``_drain``)가 판정해서 넘긴다 — [T1,
    PR 리뷰 라운드 2] 이전 대기 항목과 enrichment 입력이 같으면 그 예산을 이어받아(+1) 넘기고,
    다르면 0(새 내용에는 새 예산)을 넘긴다.

    상한(``_CONTENT_RETRY_MAX_PENDING``) 도달 시 가장 오래된 항목을 축출하고 ERROR
    dead-letter 로 남긴다 — 조용한 유실 금지. ``_content_retry_enqueued_this_drain`` 에도
    함께 기록해, 이번 ``_drain`` 실행이 끝난 뒤 재시도 패스가 같은 실행분을 건너뛸 수
    있게 한다(F1). [T6, PR 리뷰 라운드 4] 축출은 지금 등재하는 ``change`` 와 무관한
    **다른(가장 오래된) 항목**의 격리 확정이다 — 호출부가 이를 ``BatchResult.failed`` 에
    반영할 수 있도록 발생 여부를 bool 로 알린다(조용한 유실이 아니라 관측 카운터에도 드러나야
    한다).
    """
    evicted = False
    if (
        change.product_id not in _content_retry_queue
        and len(_content_retry_queue) >= _CONTENT_RETRY_MAX_PENDING
    ):
        oldest_id = next(iter(_content_retry_queue))
        _content_retry_queue.pop(oldest_id, None)
        evicted = True
        _log.error(
            "I-17 콘텐츠 재시도 큐 상한(%d) 도달 — 최고참 항목 축출(dead-letter): product_id=%s",
            _CONTENT_RETRY_MAX_PENDING,
            oldest_id,
        )
    _content_retry_queue[change.product_id] = _PendingContentRetry(
        change=change, retry_attempts=retry_attempts
    )
    _content_retry_enqueued_this_drain.add(change.product_id)
    return evicted


@dataclass
class BatchResult:
    processed: int
    hidden: int
    pages: int
    cursor: str | None
    # [#325] 격리가 **확정**된 단건 ON_SALE 실패 수(dead-letter 기록됨) — 관측 사각도 해소.
    # [#421] 재시도 예산 소진 후 격리·재시도 큐 상한 축출도 여기 합산된다. [T6, PR 리뷰
    # 라운드 4] 재시도 큐에 막 등재됐을 뿐 아직 격리되지 않은 건은 세지 않는다(그 건은
    # ``retry_pending``·WARNING 으로 드러난다) — "이번 주기에 실제로 반영되지 않았다"만
    # 세는 3선 비율 가드용 ``page_failed``(지역 변수, 이 필드와 목적이 다르다)와는 대상이
    # 다르다: 격리 "확정" 시점에만 올려 2선(전파 단계엔 안 올리고 격리 확정 시에만 올림)과
    # 시점을 맞췄다.
    failed: int = 0
    # [#421] cross-cycle 콘텐츠 재시도로 회복된 건수(노이즈였음이 확인된 경우).
    recovered: int = 0
    # [#421] 이번 주기 종료 시 다음 주기 재시도를 기다리는 대기 항목 수(관측).
    retry_pending: int = 0


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
    extras = {**extras, EXTRAS_NAME_PRESENT_KEY: bool((change.name or "").strip())}
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


def _is_enrichment_content_failure(exc: BaseException) -> bool:
    """enrich 단계 실패가 **증명된 콘텐츠 실패**인지 판정한다(#325 R6) — True 만 1선(재시도
    예산 소진 후 격리, #421) 대상이다.

    PR #399 리뷰(3차): 예전 판정은 "타임아웃이면 2선, 아니면 1선"이라는 **블랙리스트**였다.
    ``is_timeout_error`` 가 아는 타입은 ``TimeoutError``·``httpx.TimeoutException``·
    ``anthropic/openai APITimeoutError`` 뿐이라, ``openai.RateLimitError``(429)·
    ``APIConnectionError``·``InternalServerError``(5xx) 같은 명백한 일시적 인프라 장애가
    "타임아웃 아님 = 콘텐츠 실패"로 오분류돼 첫 주기에 곧바로 영구 격리됐다 — R4·R5 가 만든
    시간 유계 보호를 흔한 장애가 통째로 우회한 것이다.

    안전한 기본값은 반대다: **모르는 실패는 시간 유계 경로(2선)로 보내고, 격리는
    증명된 콘텐츠 실패에만** 적용한다(화이트리스트) — #421 이후 이 격리도 즉시가 아니라
    ``artifacts_batch_content_retry_cycles`` 재시도 예산 소진 후에 확정된다(기본 1주기).
    2선으로 보낸 실패도 계속 반복되면 결국 스트릭 상한에서 격리되므로, 오판의 비용은
    최대 몇 주기 지연뿐이다 — 반면 인프라 장애를 콘텐츠로 오판하면 되돌릴 수 없는 영구
    손실이다.

    True(1선)로 여기 열거하는 것만 격리 후보로 삼는다(#421 재시도 예산 소진 후 확정):
    - ``is_output_length_error`` — 출력 토큰 예산 소진(#325 원 사례). 같은 상품이면 재시도해도
      같은 자리에서 끊기는, 정의상 그 항목 고유의 결정적 실패다.
    - ``LLMError`` 이면서 원인 체인이 **없거나**(``extract_json`` 의 "JSON 을 찾지 못함"/
      "객체가 아님" — LLM 이 아예 JSON 을 안 준 경우) 원인이 ``ValueError``/``TypeError``
      (``json.loads`` 실패 — ``JSONDecodeError`` 는 ``ValueError`` 하위)인 경우. LLM 이 반환한
      텍스트 자체가 깨진 경우로, 인프라가 아니라 그 응답 내용의 문제다.

    False(2선)는 그 외 전부다 — 429·연결 오류·5xx·타임아웃·모르는 예외. ``LLMNotConfigured``
    는 ``LLMError`` 의 하위타입이라 원인 체인 규칙에 걸릴 수 있어 **명시적으로 제외**한다
    (API key 미구성은 항목과 무관한 구성 오류이지 콘텐츠 실패가 아니다).
    """
    if isinstance(exc, LLMNotConfigured):
        return False
    if is_output_length_error(exc):
        return True
    if isinstance(exc, LLMError):
        cause = exc.__cause__
        return cause is None or isinstance(cause, (ValueError, TypeError))
    return False


async def _drain(
    fetch: Fetch,
    start_cursor: str,
    target: ArtifactStore,
    *,
    llm: LLMClient,
    embed: Embed,
    settings: Settings,
    persist_cursor: bool,
    failure_store: ArtifactStore,
) -> BatchResult:
    """start_cursor 부터 hasMore 소진까지 target 에 반영한다. persist_cursor=True 면 페이지마다 커서 전진.

    ``failure_store`` 는 2선·3선 연속 실패 스트릭(#416)을 영속하는 스토어다 — full_rebuild
    에서는 ``target`` 이 replace_all 로 버려지는 임시 인메모리 스토어라 여기 스트릭을 두면
    증발하므로, 호출부(run_artifacts_batch)가 항상 진짜 스토어를 별도로 넘긴다.

    격리 대상은 실패 **종류**로 가른다 — 비율은 poison 단건과 광역 장애를 구별하는 대리
    지표일 뿐이고, 운영 증분 페이지처럼 소량 표본에서는 그 대리가 무너진다. 종류 판정도
    단일 주기 관측만으로는 광역 장애와 항목 고유 결정적 실패를 구별하지 못하므로(#325 R4),
    전파 대상이 된 실패는 상품별 연속 실패 스트릭으로 시간 유계를 건다. 규칙은 네 갈래로
    정리되고 이 순서로 적용된다(#325 R6, api-spec §4.8):

    - **성공** → ``failure_store.clear_failure_streak(item, product_id)``(연속만 센다).
    - **1선 — enrich 의 증명된 콘텐츠 실패**: ``_enrich_change``(enrich_product 단계)에서
      ``_is_enrichment_content_failure`` 가 True 로 판정하는 실패(출력 예산 소진, 원인 없는/
      ValueError·TypeError 원인의 LLMError — JSON 파싱 자체가 깨진 경우)만
      settings.enrichment_item_attempts 회 재시도 후 처리한다(#325 head-of-line blocking
      해소). 정의상 항목 고유 실패이므로 스트릭 판정을 거치지 않는다 — 스트릭도 삭제한다.
      [#421] ``artifacts_batch_content_retry_cycles`` 가 0 이면 종전대로 **즉시** 영구
      격리(dead-letter ERROR)하고, 1 이상이면 재시도 대기 큐에 등재(WARNING)해 다음 주기
      재시도 패스(``_run_content_retry_pass``)로 넘긴다 — JSON 파싱 실패는 LLM 샘플링
      노이즈로도 나므로 우연히 연속 실패한 정상 상품을 즉시 오격리하지 않기 위해서다. [T1,
      PR 리뷰 라운드 2] 이 페이지 항목이 같은 product 의 **이전 대기 항목과 enrichment
      입력이 동일**하면(가격·재고처럼 무관한 필드만 갱신) 그 예산을 이어받아 소진시키고,
      소진되면 이 시점에 곧바로 격리한다 — 그래야 그런 poison 상품이 매 주기
      "등재→skip→새 변경분에 0 으로 재등재"를 반복하며 예산을 영원히 소진하지 못하는 것을
      막는다(``_enrichment_inputs_unchanged``). 어느 쪽이든 ``page_failed`` 는 증가한다
      (3선 비율 가드 동작 불변 — 그 항목은 이번 주기에 실제로 반영되지 않았다는 사실 자체를
      센다). [T6, PR 리뷰 라운드 4] ``BatchResult.failed``(관측)는 그와 달리 **격리가
      확정된 경우에만** 증가한다 — 예산 0 즉시 격리, 예산 소진으로 이 시점에 격리(T1 경로),
      재시도 큐 상한 축출(다른 항목의 확정 격리)뿐이고, 재시도 큐에 막 등재된 경우는 아직
      격리가 아니므로 증가하지 않는다(``retry_pending``·WARNING 으로 드러난다) — 안 그러면
      "재시도 큐에 방금 등재됐을 뿐"인 건도 ``failed`` 로 잡혀 dead-letter ERROR 로그가
      하나도 없는데 알람만 뜬다(2선이 이미 지키는 "전파 단계엔 안 올리고 격리 확정 시에만
      올린다"는 원칙과 시점을 맞춘 것).
    - **2선(시간 유계) — 그 외 모든 단건 실패**: enrich 재시도 소진 후에도
      ``_is_enrichment_content_failure`` 가 False 인 실패(429·연결 오류·5xx·타임아웃·모르는
      예외 포함 — "모르면 2선"이 기본값), 또는 ``_finish_change``(embed()·store.upsert())의
      실패는 — 항목 내용과 무관한 인프라 장애 후보이지만 단일 주기로는 poison 단건과 구별
      불가하므로 — 해당 product_id 스트릭을 +1 한다. 스트릭이
      settings.artifacts_batch_item_dead_letter_cycles 미만이면 종전대로 **전파**(자연 복구,
      커서 미전진) — WARNING 로 현재 스트릭/상한을 남긴다. 상한 이상이면 항목 고유 실패로
      확정해 **격리**(dead-letter ERROR, product_id·스트릭·상한·단계 표기)하고 다음 항목으로
      계속한다 — failed 증가, 스트릭 삭제.
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
        (항목들은 이미 1선/2선에서 dead-letter 기록됐거나, 1선인 경우 재시도 큐로 넘어갔다).
        연속 발동 카운터는 삭제한다(#325 R5) — 1선이 다건을 격리하는 광역 파손(즉시든, #421
        재시도 예산 소진 후든)에서는 2선 스트릭이 쌓이지 않아 그 상한이 걸리지 않고, 이 3선
        시간 유계가 없으면 비율 가드 자체가 #325 의 무기한 정지를 재현한다. 페이지가 비율
        임계를 넘지 않고 정상 종료하면 그 커서의 카운터를 삭제한다(연속만 센다).
    - **계약(fail-closed, 불변)** — HIDDEN 삭제 실패·status 파싱 실패(ProductChange 단계)는
      이 시간 유계의 대상이 아니라 그대로(무기한) 전파한다(스트릭 대상도 아니다) — api-spec
      §4.8 이 명시적으로 정한 fail-closed 규약이다: 항목별 ack/DLQ 계약이 없어 skip-전진하면
      삭제 이벤트가 영구 유실되므로, 승인받은 개정 범위 밖인 이 경로만은 시간 유계에서
      제외된다. 이미 성공한 앞 페이지는 artifact와 커서가 함께 저장된 유효 체크포인트이므로
      롤백하지 않는다.
    """
    cursor = start_cursor
    processed = hidden = pages = failed = 0
    ttl_s = settings.artifacts_batch_failure_streak_ttl_s
    while True:
        page = await fetch(cursor, settings.catalog_batch_page_size)
        page_failed = page_succeeded = 0
        for change in page.items:
            # [#421] 이 페이지가 이 product 에 대한 새 변경분을 들고 왔다 — HIDDEN·ON_SALE
            # 불문 낡은 대기 페이로드는 결국 버려지거나 재등재로 갈아끼워진다. [T1, PR 리뷰
            # 라운드 2] ON_SALE 이면 조회 결과를 잠시 들고 있는다 — 아래 콘텐츠 실패 분기에서
            # enrichment 입력이 실제로 같은지 판정할 근거로 쓴다.
            #
            # [T7, PR 리뷰 라운드 5] 여기서는 **peek 만 하고 pop 하지 않는다** — 이 항목의
            # 운명이 이번 주기에 실제로 "확정"되는 지점(HIDDEN 삭제 성공·처리 성공·콘텐츠
            # 예산 소진 격리·2선 스트릭 상한 격리)에서만 아래 개별 분기가 큐를 건드린다.
            # pop 은 되돌릴 수 없는 상태 변경인데, 바로 아래 2선 분기는 스트릭이 아직 상한
            # 미만이면 `raise stage_exc` 로 `_drain` 전체를 중단시킨다 — 여기서 무조건
            # pop 해버리면 그 사이 쌓아온 cross-cycle 재시도 진행분(`retry_attempts`)이
            # 중단과 함께 통째로 사라지고, 다음 주기엔 같은 change 가 다시 와도 예산이 0 부터
            # 다시 시작한다("콘텐츠 실패(예산+1) → 2선 중단(예산 유실)"을 반복하면 콘텐츠
            # 예산·2선 스트릭 둘 다 상한에 영영 도달하지 못하는 회귀 — #421/#416 이 없애려던
            # "poison 상품이 상한에 도달 못 한다"와 같은 계열). 2선이 전파(raise)하는 경로는
            # 큐를 전혀 건드리지 않아 다음 주기가 정확히 같은 상태에서 재개된다. 유령 상품
            # 불변식은 그대로 유지된다 — 재시도 패스는 `_drain` 이 **정상 완료**했을 때만
            # 돌고(F1), 정상 완료한 실행에서는 모든 항목이 아래 분기 중 하나로 확정 처리됐기
            # 때문이다(중단된 실행은 애초에 재시도 패스까지 도달하지 않는다).
            previous_retry = _content_retry_queue.get(change.product_id)
            if change.status == _HIDDEN:
                target.delete(change.product_id)
                # delete() 가 실패하면 이 줄에 도달하지 않아 fail-closed 무기한 전파(스트릭·
                # 재시도 큐 모두 불변)가 그대로 유지된다 — 삭제 성공 시에만 큐 항목을 뗀다
                # (재시도 패스가 HIDDEN 상품을 되살려 유령 상품을 만드는 것을 막는다).
                _content_retry_queue.pop(change.product_id, None)
                # [F3, #416 후속] 삭제 성공은 그 항목의 정상 처리이므로 스트릭도 성공과
                # 동일하게 clear 한다 — 안 그러면 영속화(TTL 최대 1시간) 이후로 "ON_SALE 에서
                # 인프라 실패 2회 → HIDDEN → 1시간 안에 재판매 → 재판매 후 첫 일시 실패가 과거
                # 2와 합쳐져 3/3 으로 즉시 오격리"가 가능해진다(상품 생명주기가 스트릭을
                # 넘나들면 안 된다).
                failure_store.clear_failure_streak(FAILURE_STREAK_KIND_ITEM, str(change.product_id))
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
            if last_exc is not None and _is_enrichment_content_failure(last_exc):
                # 증명된 콘텐츠 실패(#325 R6 화이트리스트) — 정의상 항목 고유 실패이므로
                # 스트릭 판정 없이 처리한다(1선).
                failure_store.clear_failure_streak(FAILURE_STREAK_KIND_ITEM, str(change.product_id))
                # [T6, PR 리뷰 라운드 4] page_failed(3선 비율 가드용)는 이 항목이 이번
                # 주기에 반영되지 않았다는 사실 자체를 세므로 재시도 큐 등재 여부와 무관하게
                # 여기서 바로 늘린다(동작 불변). failed(관측)는 격리가 "확정"된 경우에만
                # 늘려야 하므로 여기서는 미루고 아래 각 분기에서 실제로 확정될 때만 늘린다.
                page_failed += 1
                retry_budget = settings.artifacts_batch_content_retry_cycles
                if retry_budget <= 0:
                    # [#421] 회귀 탈출구 — 예산 0 이면 종전대로 즉시 영구 격리.
                    # [T7] 격리 확정 — 대기 중이던 항목(있었다면)을 뗀다.
                    _content_retry_queue.pop(change.product_id, None)
                    failed += 1
                    _log.error(
                        "I-17 항목 실패 — 격리 기록 후 계속: product_id=%s attempts=%d",
                        change.product_id,
                        settings.enrichment_item_attempts,
                        exc_info=last_exc,
                    )
                else:
                    # [T1, PR 리뷰 라운드 2] 이전 대기 항목과 enrichment 입력이 같으면 그
                    # 예산을 이어받아 +1 한다 — 이번 _drain 자신의 재실패도 같은 poison
                    # 내용에 대한 cross-cycle 재시도로 셈해야, 가격·재고처럼 enrichment 와
                    # 무관한 필드만 매 주기 바뀌는 poison 상품이 "등재 → 이번 주기 skip(F1)
                    # → 다음 주기 새 변경분에 0 으로 재등재"를 반복하며 예산을 영원히
                    # 소진하지 못하는 것을 막는다. 내용이 실제로 바뀌면(#421 원 취지) 새
                    # 예산 — 0부터 다시 준다.
                    retry_attempts = 0
                    if previous_retry is not None and _enrichment_inputs_unchanged(
                        previous_retry.change, change
                    ):
                        retry_attempts = previous_retry.retry_attempts + 1
                    if retry_attempts >= retry_budget:
                        # [T7] 격리 확정 — 대기 중이던 항목을 뗀다(이번 실패로 예산을 다
                        # 썼으니 재등재하지 않는다).
                        _content_retry_queue.pop(change.product_id, None)
                        failed += 1
                        _log.error(
                            "I-17 콘텐츠 재시도 예산 소진(내용 불변 반복 실패) — 격리 기록 "
                            "후 계속: product_id=%s retry_attempts=%d/%d",
                            change.product_id,
                            retry_attempts,
                            retry_budget,
                            exc_info=last_exc,
                        )
                    else:
                        # [#421] JSON 파싱 실패는 LLM 샘플링 노이즈로도 나므로, 즉시 영구
                        # 격리 대신 다음 주기 재시도 기회를 준다 — 첫 주기 오격리를 막는다.
                        # [T7] previous_retry 를 pop 하지 않고 peek 만 했으므로 이미 있던
                        # 항목이면 그 키에 그대로 덮어써 재등재한다(_enqueue_content_retry 의
                        # 상한 축출 판정은 "이미 있는 키"를 정확히 skip 한다). [T6] 상한
                        # 축출은 지금 등재하는 change 와 무관한 다른(가장 오래된) 항목의
                        # 확정 격리다 — 조용한 유실이 아니라 failed 에도 드러난다.
                        if _enqueue_content_retry(change, retry_attempts=retry_attempts):
                            failed += 1
                        _log.warning(
                            "I-17 콘텐츠 실패 — 다음 주기 재시도 예약: product_id=%s "
                            "retry_attempts=%d/%d",
                            change.product_id,
                            retry_attempts,
                            retry_budget,
                            exc_info=last_exc,
                        )
                continue
            if last_exc is not None:
                # 콘텐츠 화이트리스트 밖(429·연결 오류·5xx·타임아웃·모르는 예외 포함) — 광역
                # 장애 후보로 취급해 스트릭 판정(아래). "모르면 2선"이 기본값이다(#325 R6).
                stage_exc = last_exc
                stage = "enrichment"
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
                # [T7] 처리 성공 — 대기 중이던 항목(있었다면)을 뗀다. 방금 새 페이로드로
                # 성공했으니 낡은 대기분은 더 이상 의미가 없다.
                _content_retry_queue.pop(change.product_id, None)
                failure_store.clear_failure_streak(FAILURE_STREAK_KIND_ITEM, str(change.product_id))
                processed += 1
                page_succeeded += 1
                continue

            # [#325 R4] 단일 주기로는 광역 장애와 항목 고유 결정적 실패를 구별할 수 없다 —
            # 같은 상품이 주기를 가로질러 연속 실패한 횟수로 시간 유계를 건다.
            streak = failure_store.bump_failure_streak(
                FAILURE_STREAK_KIND_ITEM, str(change.product_id), ttl_s=ttl_s
            )
            cycles_limit = settings.artifacts_batch_item_dead_letter_cycles
            if streak < cycles_limit:
                # [T7, PR 리뷰 라운드 5] 여기서 raise 하면 _drain 전체가 중단된다 — 큐를
                # 절대 건드리지 않는다(peek 만 했으므로 이미 건드리지 않은 상태). 그래야
                # previous_retry 의 cross-cycle 예산이 이 중단에 삼켜지지 않고, 다음 주기가
                # 정확히 같은 상태(같은 retry_attempts)에서 재개된다 — 안 그러면 "콘텐츠
                # 실패(예산 소진 진행 중) → 2선 중단"이 반복될 때 예산이 매번 사라져 콘텐츠
                # 예산·2선 스트릭 어느 쪽 상한에도 영영 도달하지 못할 수 있었다.
                _log.warning(
                    "I-17 항목 연속 실패 — 전파(자연 복구): product_id=%s stage=%s streak=%d/%d",
                    change.product_id,
                    stage,
                    streak,
                    cycles_limit,
                )
                raise stage_exc
            # [T7] 격리 확정 — 대기 중이던 항목(있었다면, 낡은 콘텐츠 실패 이력)을 뗀다.
            _content_retry_queue.pop(change.product_id, None)
            failure_store.clear_failure_streak(FAILURE_STREAK_KIND_ITEM, str(change.product_id))
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
                failure_store.clear_failure_streak(FAILURE_STREAK_KIND_PAGE, cursor)
            else:
                ratio = page_failed / page_total
                if ratio >= settings.artifacts_batch_failure_ratio_threshold:
                    # [#325 R5] 3선도 2선(R4)과 같은 시간 유계 — 같은 커서에서 비율 가드가
                    # 연속 발동한 횟수로 대량 파손의 자연 회복 여부를 판정한다.
                    page_streak = failure_store.bump_failure_streak(
                        FAILURE_STREAK_KIND_PAGE, cursor, ttl_s=ttl_s
                    )
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
                    failure_store.clear_failure_streak(FAILURE_STREAK_KIND_PAGE, cursor)
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
                    failure_store.clear_failure_streak(FAILURE_STREAK_KIND_PAGE, cursor)
        else:
            failure_store.clear_failure_streak(FAILURE_STREAK_KIND_PAGE, cursor)
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

    try:
        store.purge_stale_failure_streaks(settings.artifacts_batch_failure_streak_ttl_s)
    except Exception:  # noqa: BLE001 - 청소 실패는 무시하고 계속(#416, 스토어가 자체 폴백도 갖는다)
        _log.warning("I-17 실패 스트릭 청소 실패 — 무시하고 계속", exc_info=True)

    async def rebuild() -> BatchResult:
        # [#421] 전체 재구축은 모든 항목을 어차피 다시 처리하므로 재시도 큐를 비운다 — 큐에
        # 남아 있던 payload 는 이 재구축이 fetch(since="0")로 다시 받아온다.
        _content_retry_queue.clear()
        # 임시 스토어에 전체 구축 후 성공 시 원자 교체 — 중간 실패해도 기존 데이터 보존 + stale 제거.
        # failure_store 는 항상 진짜 store(#416) — work 는 replace_all 로 버려지는 임시본이다.
        work = CatalogArtifactStore()
        rebuilt = await _drain(
            fetch,
            "0",
            work,
            llm=llm,
            embed=embed,
            settings=settings,
            persist_cursor=False,
            failure_store=store,
        )
        store.replace_all_and_set_cursor(work.all(), rebuilt.cursor)
        return rebuilt

    did_rebuild = full_rebuild
    if full_rebuild:
        result = await rebuild()
    else:
        # [F1, PR 리뷰 라운드 1 blocker] 재시도 패스는 _drain 이 **정상 완료한 뒤에만** 돈다.
        # _drain 이 hasMore 를 소진해 정상 반환했다는 것은 Spring 이 지금까지 발행한 변경분을
        # 전부 소비했다는 뜻이다 — 그 시점에 큐에 남은 항목은 이번 주기 동안 HIDDEN 이 된 적이
        # 없음이 보장된다(HIDDEN 이었다면 방금 그 변경분을 소비하며 큐에서 제거됐다). 반대로
        # _drain 이 중단되면(2선 전파·PageFailureThresholdExceeded·fetch 실패·InvalidCursorError
        # → rebuild) 아직 못 본 변경분이 남아 있을 수 있으므로 재시도 패스를 아예 돌리지
        # 않는다 — 그러지 않으면 그 사이 HIDDEN 으로 바뀐 상품을 재시도 패스가 낡은 페이로드로
        # 되살려 §4.8 이 fail-closed 로 못박은 유령 상품 금지를 정면으로 뚫는다. "배치가
        # 따라잡은 뒤에만 되살린다"가 이를 구조적으로 막는 불변식이다.
        #
        # 재시도 패스가 돌지 않는 실패 경로(위 목록)에서는 이번 _drain 이 새로 등재한 항목이
        # 그대로 큐에 남고, 다음 실행이 다시 같은 페이지를 보며 자연히 흡수한다 — 별도 처리가
        # 필요 없다.
        _content_retry_enqueued_this_drain.clear()
        start = store.get_cursor() or "0"
        try:
            result = await _drain(
                fetch,
                start,
                store,
                llm=llm,
                embed=embed,
                settings=settings,
                persist_cursor=True,
                failure_store=store,
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
            # rebuild() 가 이미 재시도 큐를 비웠다(전체 재구축이 모든 항목을 다시 본다) — 위
            # docstring 규칙대로 이 경로에서는 재시도 패스를 돌리지 않는다.
        else:
            # [F1·F2] 이번 _drain 실행 중 새로 등재된 product_id 는 아직 "다음 주기"를 한 번도
            # 겪지 않았으므로 이번 재시도 패스에서 건너뛴다 — 안 그러면 유령 상품 위험(F1)과
            # 더불어 poison 상품에 대한 LLM 호출이 주기당 최대 2배로 증폭된다(F2).
            enqueued_this_run = set(_content_retry_enqueued_this_drain)
            try:
                recovered, retry_dead_lettered = await _run_content_retry_pass(
                    llm=llm,
                    embed=embed,
                    store=store,
                    failure_store=store,
                    settings=settings,
                    skip=enqueued_this_run,
                )
            except Exception:  # noqa: BLE001 - [#421] 재시도 패스 실패는 본 배치를 막지 않는다
                _log.exception("I-17 콘텐츠 재시도 패스 실패 — 본 배치 계속")
                recovered, retry_dead_lettered = 0, 0
            result.recovered = recovered
            result.failed += retry_dead_lettered

    result.retry_pending = len(_content_retry_queue)

    _log.info(
        "artifacts batch: processed=%d hidden=%d pages=%d failed=%d recovered=%d "
        "retry_pending=%d rebuild=%s",
        result.processed,
        result.hidden,
        result.pages,
        result.failed,
        result.recovered,
        result.retry_pending,
        did_rebuild,
    )
    return result


async def _run_content_retry_pass(
    *,
    llm: LLMClient,
    embed: Embed,
    store: ArtifactStore,
    failure_store: ArtifactStore,
    settings: Settings,
    skip: set[int],
) -> tuple[int, int]:
    """[#421] cross-cycle 콘텐츠 재시도 큐를 결정적 순서(product_id 오름차순)로 1회 순회한다.

    ``skip`` 은 이번 실행의 ``_drain`` 이 방금 새로 등재한 product_id 집합이다(F1·F2, PR 리뷰
    라운드 1) — 여기 속한 항목은 건드리지 않고 큐에 그대로 둔다. 두 가지 이유가 있다:
    (1) 정합성 — 이 함수는 ``_drain`` 이 **정상 완료한 뒤에만** 호출되므로(run_artifacts_batch),
    호출 시점에 큐에 있는 "오래된" 항목은 이번 주기 동안 HIDDEN 이 된 적이 없음이 보장된다
    (HIDDEN 이었다면 방금 ``_drain`` 이 소비하며 큐에서 제거했다) — 그런데 방금 새로 등재된
    항목은 이 불변식이 성립하기 **전**(이번 주기 자체에서 실패한 것)이라 같은 실행에서
    건드리면 안 된다. (2) 증폭 방지 — 방금 실패해 등재된 항목을 같은 실행에서 곧바로
    재시도하면 "cross-cycle 예산"이 의미를 잃고, poison 상품에 대한 LLM 호출이 주기당
    최대 2배로 뛴다. 다음 주기의 새 변경분(같은 product_id 가 다시 실패해 새로 등재되는
    경우)은 이 skip 과 무관하게 그 다음다음 주기부터 정상적으로 예산을 받는다.

    본 루프(``_drain``)와 동일하게 ``settings.enrichment_item_attempts`` 회 재시도한다.

    [T2, PR 리뷰 라운드 2] 재시도 시점의 실패는 ``_drain`` 과 동일한 기준으로 갈린다 —
    ``_is_enrichment_content_failure`` 가 True 인 **증명된 콘텐츠 실패만**
    ``retry_attempts`` (콘텐츠 예산)을 소진시킨다. 그 외(enrich 단계의 비콘텐츠 실패
    429·5xx·타임아웃·모르는 예외, 그리고 ``_finish_change`` 인프라 실패)는 콘텐츠가 이미
    한 번 살아났다는 뜻이므로 예산을 태우지 않고, ``_drain`` 2선과 같은
    ``failure_store.bump_failure_streak(item, product_id)`` 시간 유계 스트릭으로 판정한다
    (``artifacts_batch_item_dead_letter_cycles`` 도달 시 격리, 항목은 그 전까지 큐에 남는다).
    수정 전에는 실패 종류를 가리지 않고 재시도 시점 실패마다 콘텐츠 예산을 소진시켰는데,
    기본 예산이 1이면 "재시도 시점에 콘텐츠는 살아났지만 embed·store 가 하필 그 순간
    일시 장애"라는 단 한 번의 불운이 정상 상품을 영구 격리했다 — #421 이 없애려던 바로 그
    오격리를 재시도 패스 안에서 재현하고 있었다.

    [T5, PR 리뷰 라운드 3] 콘텐츠 실패 분기는 큐 유지/즉시 격리 결과와 무관하게 **항상**
    item 스트릭을 clear 한다(``_drain`` 의 대응 분기와 동일 불변식) — 콘텐츠 실패는 정의상
    항목 고유 실패라 2선 스트릭의 "연속"을 끊어야 한다(#325 R6). T2 가 이 함수에 2선 스트릭
    경로를 처음 넣으며 이 clear 를 빠뜨려, "인프라 실패(streak bump) → 콘텐츠 실패(clear
    없이 retry_attempts 만 +1) → 인프라 실패(streak 가 안 끊기고 이어짐)" 순서에서 연속이
    아닌 인프라 실패가 연속으로 오판돼 실제보다 이르게 격리될 수 있었다.

    ``retry_attempts``(F4 정합) 는 "지금까지 실제로 콘텐츠 실패로 확정된 cross-cycle
    재시도 횟수"다 — 등재 시 0에서 시작해 이 함수가 **콘텐츠 실패**를 확정할 때만 +1 하고,
    ``retry_attempts < budget`` 이면 유지, 아니면 격리한다. 이 불변식이라야
    ``artifacts_batch_content_retry_cycles=N`` 이 "cross-cycle 콘텐츠 재시도가 정확히 N 회
    일어난 뒤에야 영구 격리"라는 뜻이 된다.

    이 패스의 실패는 절대 밖으로 전파하지 않는다 — 커서·본 배치 진행을 건드리면 안 되고,
    ``PageFailureThresholdExceeded`` 판정 대상도 아니다(2선 스트릭으로 보내는 것과 스트릭
    상한 도달 시 격리하는 것 모두 이 함수 안에서 끝나며, 절대 raise 하지 않는다). 반환은
    (recovered, dead_lettered) — dead_lettered 는 콘텐츠 예산 소진과 스트릭 상한 도달을
    합산한다.
    """
    budget = settings.artifacts_batch_content_retry_cycles
    ttl_s = settings.artifacts_batch_failure_streak_ttl_s
    streak_cycles_limit = settings.artifacts_batch_item_dead_letter_cycles
    recovered = 0
    dead_lettered = 0
    for product_id in sorted(_content_retry_queue):
        if product_id in skip:
            continue  # [F1·F2] 이번 실행에서 방금 등재됨 — 다음 주기부터 대상이 된다.
        pending = _content_retry_queue.get(product_id)
        if pending is None:
            continue  # 이 순회 중 다른 곳(예: 방어적 상한 축출)이 이미 제거했을 수 있다.
        change = pending.change
        try:
            last_exc: Exception | None = None
            product = extras = None
            for _attempt in range(settings.enrichment_item_attempts):
                try:
                    product, extras = await _enrich_change(change, llm=llm, settings=settings)
                except Exception as exc:  # noqa: BLE001 - 재시도 루프(#421, 본 루프와 동일 구조)
                    last_exc = exc
                    continue
                last_exc = None
                break

            # [T2] enrich 실패라면 여기서 콘텐츠/비콘텐츠를 판정한다. finish 실패는
            # _drain 과 동일하게 항상 비콘텐츠(2선)다 — enrich 가 성공했다는 것 자체가
            # 콘텐츠는 이미 살아났다는 뜻이라 콘텐츠 실패일 수 없다.
            is_content_failure = last_exc is not None and _is_enrichment_content_failure(last_exc)
            if last_exc is None:
                assert product is not None and extras is not None
                try:
                    await _finish_change(
                        change, product, extras, embed=embed, store=store, settings=settings
                    )
                except Exception as exc:  # noqa: BLE001 - finish 실패는 항상 2선(T2)
                    last_exc = exc

            if last_exc is None:
                _content_retry_queue.pop(product_id, None)
                failure_store.clear_failure_streak(FAILURE_STREAK_KIND_ITEM, str(product_id))
                recovered += 1
                _log.info(
                    "I-17 콘텐츠 실패 재시도 성공(노이즈 회복): product_id=%s retry_attempts=%d/%d",
                    product_id,
                    pending.retry_attempts,
                    budget,
                )
                continue

            if is_content_failure:
                # [T5, PR 리뷰 라운드 3] 콘텐츠 실패는 정의상 항목 고유 실패이므로 2선 스트릭의
                # "연속"을 끊는다(#325 R6 의도, _drain 의 대응 분기와 동일) — 큐 유지/즉시 격리
                # 결과와 무관하게 항상 clear 한다. 안 그러면 인프라 실패(streak bump)와 콘텐츠
                # 실패가 번갈아 나도 스트릭이 안 끊겨(인프라→콘텐츠→인프라 순서에서 마지막
                # streak 가 1 이 아니라 2), item_dead_letter_cycles 가 요구하는 "연속 인프라
                # 실패"가 아닌데도 실제보다 이르게 격리될 수 있었다(T2 가 재시도 패스에 2선
                # 스트릭 경로를 새로 넣으며 생긴 비대칭).
                failure_store.clear_failure_streak(FAILURE_STREAK_KIND_ITEM, str(product_id))
                pending.retry_attempts += 1
                if pending.retry_attempts < budget:
                    _log.warning(
                        "I-17 콘텐츠 재시도 실패(콘텐츠 예산 소진) — 다음 주기 재시도 유지: "
                        "product_id=%s retry_attempts=%d/%d",
                        product_id,
                        pending.retry_attempts,
                        budget,
                        exc_info=last_exc,
                    )
                else:
                    _content_retry_queue.pop(product_id, None)
                    dead_lettered += 1
                    _log.error(
                        "I-17 콘텐츠 재시도 예산 소진 — 격리: product_id=%s retry_attempts=%d/%d",
                        product_id,
                        pending.retry_attempts,
                        budget,
                        exc_info=last_exc,
                    )
            else:
                # [T2] 재시도 시점 인프라 실패(enrich 비콘텐츠 또는 finish) — 콘텐츠 예산은
                # 그대로 두고 _drain 2선과 동일한 시간 유계 스트릭으로 판정한다.
                streak = failure_store.bump_failure_streak(
                    FAILURE_STREAK_KIND_ITEM, str(product_id), ttl_s=ttl_s
                )
                if streak < streak_cycles_limit:
                    _log.warning(
                        "I-17 콘텐츠 재시도 중 인프라 실패(스트릭 누적) — 큐 유지: "
                        "product_id=%s streak=%d/%d retry_attempts=%d/%d",
                        product_id,
                        streak,
                        streak_cycles_limit,
                        pending.retry_attempts,
                        budget,
                        exc_info=last_exc,
                    )
                else:
                    _content_retry_queue.pop(product_id, None)
                    failure_store.clear_failure_streak(FAILURE_STREAK_KIND_ITEM, str(product_id))
                    dead_lettered += 1
                    _log.error(
                        "I-17 콘텐츠 재시도 중 인프라 실패 스트릭 상한 도달 — 격리: "
                        "product_id=%s streak=%d/%d",
                        product_id,
                        streak,
                        streak_cycles_limit,
                        exc_info=last_exc,
                    )
        except Exception:  # noqa: BLE001 - [#421] 재시도 패스는 항목 단위로도 본 배치에 전파하지 않는다
            _log.exception("I-17 콘텐츠 재시도 패스 예외 — 본 배치 계속: product_id=%s", product_id)
    return recovered, dead_lettered
