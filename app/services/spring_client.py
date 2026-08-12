"""Spring 역방향 호출 클라이언트 (api-spec v0.15.x §4 — 판매자 SpringClient 는 §4.4/§4.5).

AI → Spring 질의 시점 역방향 + 배치 (구매자, 모듈 레벨 함수):
  - search_products        : 후보 확보 (I-1, GET /internal/products/search, §4.6, C-15) — [배선 완료]
  - get_recent_purchases   : 구매 이력 조회 (I-19, GET /internal/members/{id}/orders, §4.7, C-6) — dedup·프로필 소스
  - get_order_status       : 주문 상태 요약 (I-4, GET /internal/members/{id}/orders/status, §4.10)
  - add_to_cart            : 장바구니 담기 (I-2, POST /internal/cart/items, 단건, §4.1)
  - get_cart               : 장바구니 조회 (I-18, GET /internal/cart, §4.9, C-16)
  - push_recommendations   : 최종 랭크 id push (I-21, POST /internal/recommendations, 경로 B, §4.2) — [배선 완료]
  - fetch_product_changes  : AI 생성물 갱신 배치 pull (I-17, §4.8, C-4)
판매자 조회 8종 + 쓰기 3종(I-6~I-16 집계, I-9~I-12 CRUD)은 아래 `SpringClient` 클래스 소관 —
구스텁 get_seller_aggregates·get_product_detail 은 대체·삭제(DESIGN-SELLER-TOOLS-STAGE1).
AI 는 커머스 DB 에 직접 write 하지 않는다. 와이어 포맷은 camelCase (스키마 alias).

인증 레인 (api-spec §2.3, v0.13.0 통일): AI→Spring 역호출은 전 구간 X-Internal-Token 서비스 토큰
  + 본문/쿼리 신원(AI-검증 JWT sub 유래, IDOR 방지). 판매자는 brandId(JWT 클레임)를 {brandId} path 에.
타임아웃: AI→Spring 전 구간 3s 통일 (api-spec §2.9 c — BE I-2 문서 기준).

[배선 v이슈#2] search_products = **GET**(사용자 확정 "그냥 GET으로") — BE I-1 파라미터
  keyword/categoryName/minPrice/maxPrice/brandName (size 제거 v2026-07-23, top-K 는 AI 쪽). dedup·평점·정렬은 요청 파라미터가 아니라
  AI 사후필터(§4.6 v0.15.5, C-15). push_recommendations = POST I-21(productIds 만, 경로 B).

[변경 DESIGN-SELLER-TOOLS-STAGE1] 판매자 조회 8종 + 쓰기 3종은 아래 `SpringClient` 클래스로
신설한다(구스텁 `get_seller_aggregates`·`get_product_detail`는 대체·삭제) — 구매자 함수는
불변이다. `SpringClient`는 X-Internal-Token 서비스 토큰 + `{brandId}` path + 3s 타임아웃
(api-spec §2.3 v0.13.0·§2.9 c)을 한 곳에 바인딩하고, `transport` 인자로 테스트용
`httpx.MockTransport`를 주입할 수 있다(respx 미설치, §0 의존성 실측).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import math
import threading
from types import TracebackType
from typing import Callable, Literal, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.tracing import TraceNode, current_request_trace, trace_span
from app.pipelines import brand_aliases
from app.schemas.spring import (
    AccountEventsResult,
    AddToCartRequest,
    AddWishlistRequest,
    CartOption,
    AddToCartResult,
    BehaviorEventsResult,
    CartView,
    ChangeCartQuantityRequest,
    ChangeCartQuantityResult,
    ChurnResult,
    FunnelResult,
    OrderEventsResult,
    ORDER_STATUS_RECENT,
    OrderItemStatusResult,
    OrderItemStatusUpdate,
    OrderStatusSummary,
    ProductChangeLogResult,
    ProductChangesPage,
    ProductCreate,
    ProductCreateResult,
    ProductDeleteResult,
    ProductSearchFilters,
    ProductSearchResult,
    ProductUpdate,
    ProductUpdateResult,
    RecentPurchases,
    RecommendationPush,
    SalesResult,
    SellerCustomerFeaturesResult,
    SellerOrderList,
    SellerProductList,
    SellerReviewList,
    SellerReviewStats,
    SpringProduct,
    WishlistAddResult,
    WishlistItem,
    WishlistView,
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_SpringOperation = Literal[
    "search_products",
    "get_popular_products",
    "get_recent_purchases",
    "get_order_status",
    "add_to_cart",
    "get_cart",
    "delete_cart_item",
    "change_cart_quantity",
    "add_wishlist",
    "remove_wishlist",
    "get_wishlist",
    "push_recommendations",
    "fetch_product_changes",
    "get_sales",
    "get_funnel",
    "get_events",
    "get_order_events",
    "get_product_changes",
    "get_churn",
    "get_account_events",
    "get_customer_features",
    "list_products",
    "create_product",
    "update_product",
    "delete_product",
    "get_orders",
    "update_order_status",
    "get_reviews",
    "get_review_stats",
]


_log = logging.getLogger(__name__)
_color_synonym_limiters: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_color_synonym_limiter_lock = threading.Lock()
_background_synonym_tasks: set[asyncio.Task[dict[str, list[str]]]] = set()
# [#427, DESIGN-SHARED-BUDGET-384 §3 D3] 구제 체인 런타임 좁히기가 `search_products` 의 총시간
# 예산(`budget_s`)을 잔여값으로 주입하는 통로 — ContextVar 인 이유는 좁힌 값의 전달이
# `graph.py → search_catalog → SearchBackend → spring_client.search_products` 로 함수 경계를
# 넘어야 하는데(D3 이 `rescue_deadline` 자체의 ContextVar 승격은 기각했다 — 그건 graph 로컬
# 판단이라서다), `SearchBackend` Protocol(SpringSearchBackend·EmbeddingRerankBackend·
# VectorSearchBackend)·`search_catalog` 시그니처·테스트 fake 를 건드리지 않는 것이 실익이기
# 때문이다. gather 자식 태스크는 생성 시 컨텍스트를 복사한다.
_search_budget_override: ContextVar[float | None] = ContextVar(
    "search_budget_override", default=None
)
# [#406] 호출 시그니처를 넓히지 않고 I-1 실제 재시도 진입만 관측하는 일회성 seam이다.
_search_retry_observer: ContextVar[Callable[[], None] | None] = ContextVar(
    "search_retry_observer", default=None
)


class SearchBudgetExceeded(TimeoutError):
    """I-1 검색 총시간 가드가 예산을 넘겨 잘라낸 실패 (#132).

    `TimeoutError` 를 상속해 기존 타임아웃 처리와 호환되면서도, `_transport_status_class` 가
    **이 타입만** 전송 계층 `timeout` 으로 분류하게 해 다른 Spring 호출의 무관한 `TimeoutError`
    가 같은 라벨을 얻지 않게 한다(PR #293 리뷰).
    """


# [#132 PR #293 리뷰] I-1 응답 파싱 전용 스레드풀. `asyncio.to_thread` 는 **앱 전역 기본
# executor** 를 쓰는데, 그 풀은 임베딩·카테고리 매핑·색상 사전·예산 세트 조립이 함께 쓴다.
# 총시간 가드는 await 만 취소하고 **스레드는 끝까지 돈다** — 가드가 자주 트립하는 부하에서
# 버려진 파싱 스레드가 공유 풀을 점유하면, 정작 검색과 무관한 요청까지 슬롯을 기다린다.
# 전용 풀로 격리해 고갈의 파급을 검색 파싱 안에 가둔다(개별 호출 지연은 가드가, 워커 전체
# 처리량은 이 격리가 맡는다). 크기 기본값은 기존 기본 executor 와 같은 식이라 격리만 바뀌고
# 동시 처리량 특성은 그대로다.
_parse_executor: ThreadPoolExecutor | None = None
_parse_executor_lock = threading.Lock()


def _get_parse_executor() -> ThreadPoolExecutor:
    """파싱 전용 executor 를 지연 생성한다(이중 확인 락 — 스케줄러·이벤트루프 동시 진입 방어).

    **큐는 스스로 빠진다** — 가드가 초과를 잡아 `wait_for` 가 코루틴을 취소하면 그 취소가
    `run_in_executor` future 로 전파돼, **아직 시작되지 않은** 파싱 작업은 큐에서 제거되고
    영영 실행되지 않는다(PR #293 리뷰 검증: 실 코드 구조로 재현). 그래서 "버려진 작업이 큐를
    채워 신규 요청이 파싱을 시작도 못 하고 예산을 태우는" 자기증폭은 성립하지 않는다.

    남는 점유는 **이미 실행 중이던** 작업뿐이고 그 수는 `max_workers` 로 유계다(스레드는 취소가
    안 되므로 끝까지 돈다). 각 작업은 실측 200ms 안에 끝나므로(13.33MB/7,245건) 포화는 그 시간
    규모에서 자연히 풀린다. 다만 **파싱 시간이 예산을 넘게 커지면** 이 전제가 깨진다 — 그때는
    모든 요청이 파싱 도중 버려져 워커가 계속 점유되므로, 그 지점에서는 큐 깊이 기반
    backpressure(포화 시 즉시 실패)가 필요하다. 현 규모(예산 3~6s vs 파싱 0.2s)에서는 멀다.
    """
    global _parse_executor
    if _parse_executor is None:
        with _parse_executor_lock:
            if _parse_executor is None:
                _parse_executor = ThreadPoolExecutor(
                    max_workers=get_settings().search_parse_max_workers,
                    thread_name_prefix="i1-parse",
                )
    return _parse_executor


async def _run_in_parse_executor(fn, *args):
    """전용 풀에서 동기 함수를 돌린다 — `asyncio.to_thread` 의 전용 executor 판."""
    return await asyncio.get_running_loop().run_in_executor(_get_parse_executor(), fn, *args)


# [#306] `suppress_search_retry()` 는 제거됐다 — #277 이 미룬 턴의 I-1 재시도만 끄려고 둔
# 컨텍스트 매니저였다. 그 근거(미룬 턴의 첫 SSE 가 검색 뒤라 재시도가 first-token 예산을
# 먹는다)는 #396 이 `progress` 를 검색 앞으로 보내며 사라졌고, 이제 재시도 여부는 턴 유형이
# 아니라 `settings.spring_max_retries` 하나가 정한다.


@contextmanager
def narrow_search_budget(budget_s: float) -> Iterator[None]:
    """현재 호출 컨텍스트의 I-1 검색 총시간 예산을 `budget_s` 로 좁힌다 (#427 D3·D4).

    `search_products` 의 `asyncio.wait_for(..., timeout=budget_s)` 가 쓰는 총시간(재시도 포함)
    상한을 override 한다 — httpx 스칼라 타임아웃은 손대지 않는다(`wait_for` 가 총시간을 이미
    집행한다, #132). `observe_search_retry` 와 **동일한 누수 방지 규율**을 따른다: 이 `with`
    블록은 `await` 직후 즉시 닫아야 한다 — `yield` 를 그 안에 두면 다음 턴으로 ContextVar 가
    샌다.
    """
    token = _search_budget_override.set(budget_s)
    try:
        yield
    finally:
        _search_budget_override.reset(token)


@contextmanager
def observe_search_retry(callback: Callable[[], None]) -> Iterator[None]:
    """현재 호출 컨텍스트의 I-1 실제 재시도 진입을 `callback`으로 관측한다 (#406)."""
    token = _search_retry_observer.set(callback)
    try:
        yield
    finally:
        _search_retry_observer.reset(token)


def notify_search_retry() -> None:
    """현재 컨텍스트의 재시도 관측자에게 실제 재시도 진입을 알린다 (#406)."""
    callback = _search_retry_observer.get()
    if callback is None:
        return
    try:
        callback()
    except Exception:
        # 관측 실패가 I-1 검색 자체를 중단시키면 진행 신호가 가용성을 해치는 역전이 된다.
        _log.warning("spring_search_retry_observer_failed", exc_info=True)


def _color_synonym_limiter(dsn: str, max_concurrency: int) -> threading.BoundedSemaphore:
    key = (dsn, max_concurrency)
    limiter = _color_synonym_limiters.get(key)
    if limiter is None:
        with _color_synonym_limiter_lock:
            limiter = _color_synonym_limiters.get(key)
            if limiter is None:
                limiter = threading.BoundedSemaphore(max_concurrency)
                _color_synonym_limiters[key] = limiter
    return limiter


def _color_synonym_runtime_max_concurrency(settings) -> int:
    """배치가 꺼져 있으면 공유 풀 전체를, 켜져 있으면 예약분을 제외한 검색 몫을 반환한다."""
    if not settings.color_synonym_batch_harvest_enabled:
        return settings.color_synonym_pool_max_size
    return settings.color_synonym_pool_max_size - settings.color_synonym_harvest_max_concurrency


def _consume_background_synonym_lookup(task: asyncio.Task[dict[str, list[str]]]) -> None:
    """타임아웃 뒤 승인 사전 조회의 늦은 실패를 기록하고 예외를 회수한다."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.warning(
            "색상 동의어 백그라운드 조회 실패",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


async def _load_color_synonym_map(settings) -> dict[str, list[str]] | None:
    """배치 예산을 제외한 검색 전용 worker만 허용하고 포화 시 즉시 degrade한다."""
    from app.pipelines import color_synonyms  # noqa: PLC0415 - lazy DB 경로

    limiter = _color_synonym_limiter(
        settings.catalog_db_url,
        _color_synonym_runtime_max_concurrency(settings),
    )
    if not limiter.acquire(blocking=False):
        _log.warning("색상 동의어 조회 동시 실행 상한 — 원문 단수 color로 검색")
        return None

    def run() -> dict[str, list[str]]:
        try:
            return color_synonyms.get_synonym_map(
                settings.catalog_db_url,
                ttl_s=settings.color_synonym_cache_ttl_s,
                warn_if_empty=settings.color_synonym_expansion_enabled,
            )
        finally:
            # wait_for가 먼저 끝나도 실제 worker 종료 전에는 풀 크기 슬롯을 반환하지 않는다.
            limiter.release()

    task = asyncio.create_task(asyncio.to_thread(run))
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=settings.color_synonym_query_timeout_s,
        )
    except (TimeoutError, asyncio.CancelledError):
        _background_synonym_tasks.add(task)
        task.add_done_callback(_background_synonym_tasks.discard)
        task.add_done_callback(_consume_background_synonym_lookup)
        raise


@contextmanager
def _spring_span(operation: _SpringOperation, method: str) -> Iterator[TraceNode | None]:
    """Trace one outbound Spring request without retaining request or response data."""
    # ⚠️ 의도된 stash-and-rethrow — `raise` 로 되돌리지 말 것(f77af25).
    # `trace_span` 은 본문에서 예외가 전파될 때 `node.error_type = type(exc).__name__` 을 채운다.
    # Spring 스팬은 예외 클래스명(CartStockInsufficient 등 업스트림 상태 유출)까지 막고 유계
    # transport 메타데이터만 내보내는 게 계약이라, 예외를 여기 모았다가 `trace_span` 이 정상
    # 종료한 뒤 밖에서 재던져 error_type 을 비워 둔다. 전역 시리얼라이저에서 errorType 을 막는
    # 대안은 기각 — 비-Spring 스팬은 자동 예외 분류가 그대로 필요하다.
    # 실패 표식은 statusClass 로만 남는다: timeout·connection_error(아래) + 4xx/5xx
    # (`_record_spring_status` 가 raise_for_status 앞에서 기록). 회귀 테스트는
    # tests/unit/test_buyer_tracing.py::test_buyer_spring_* 4종 + seller 대응.
    failure: tuple[BaseException, TracebackType | None] | None = None
    with trace_span(
        f"spring.{operation}",
        "tool",
        {"httpMethod": method, "upstream": "spring"},
    ) as span:
        try:
            yield span
        except BaseException as exc:
            # 분류는 `_transport_status_class` 한 곳에만 둔다 — 로그(`_failure_status_class`)와
            # 어휘가 갈리지 않게 코드로 보장한다(PR #235 리뷰). 전송 계층 실패가 아니면 None 이라
            # 손대지 않는다(4xx/5xx 는 `_record_spring_status` 가 raise_for_status 앞에서 이미 기록).
            if span is not None and (label := _transport_status_class(exc)) is not None:
                span.metadata["statusClass"] = label
            failure = (exc, exc.__traceback__)

    if failure is not None:
        exc, traceback = failure
        raise exc.with_traceback(traceback)


def _record_spring_status(span: TraceNode | None, response: httpx.Response) -> None:
    if span is not None:
        span.metadata["statusClass"] = f"{response.status_code // 100}xx"
        # [#326] 콘텐츠 추적 모드에서만 요청 URL·본문과 응답 페이로드를 싣는다. 위 "유계
        # transport 메타데이터만" 계약의 **명시적 디버깅 예외**이며(기본 off), 헤더는 싣지
        # 않는다(X-Internal-Token 유출 방지).
        if (trace := current_request_trace()) and trace.captures_content:
            request = response.request
            inputs: dict[str, object] = {"url": str(request.url)}
            if request.content:
                inputs["requestBody"] = request.content.decode("utf-8", errors="replace")
            trace.record_span_content(span, inputs=inputs, outputs={"responseBody": response.text})


# 재시도 대상 4xx (#133, PR #235 리뷰) — "4xx 는 다시 보내도 같은 거절"이라는 일반 규칙의
# **예외**다. 이 둘은 요청 자체가 유효하고 서버·인프라의 일시 상태일 뿐이라 5xx 와 성격이 같다.
#   408 Request Timeout   : 서버가 요청 대기 중 끊음
#   429 Too Many Requests : 레이트 리밋 — **즉시 응답**이라 재시도 비용이 타임아웃과 달리
#                           밀리초다(턴 예산을 태우지 않는다). 상한 1회라 증폭도 2배를 넘지 않는다.
# I-1 계약에 두 코드가 명시돼 있지는 않지만 프록시·게이트웨이가 낼 수 있어 방어적으로 받는다.
# ⚠️ `Retry-After` 는 존중하지 않는다 — backoff 가 없어 즉시 다시 찌른다. 상한 1회라 증폭은
# 2배로 묶이지만 서버 지시를 따르는 것은 아니다. `spring_max_retries` 를 올릴 때는 backoff 와
# 함께 이 헤더 처리도 넣어야 한다(현재 상한이 1인 이유).
_RETRYABLE_4XX = frozenset({408, 429})


def _transport_status_class(exc: BaseException) -> str | None:
    """전송 계층 실패의 유계 라벨 — 없으면 None (#141, PR #235 리뷰).

    **분류를 여기 한 곳에만 둔다.** 로그(`_failure_status_class`)와 trace(`_spring_span`)가
    각자 isinstance 를 따로 구현하면, 새 예외 타입을 한쪽에만 추가했을 때 라벨이 조용히
    갈린다 — 이 PR 이 `RemoteProtocolError` 로 그 사고를 실제로 두 번 냈다. 주석으로 "같은
    어휘를 쓴다"고 약속하는 대신 코드가 보장하게 한다.

    `RemoteProtocolError`(서버가 응답 도중 연결 종료)는 `NetworkError` 의 하위가 아니라
    **형제**(둘 다 `TransportError` 직계)라 함께 적지 않으면 어느 분기에도 안 걸린다.

    [#132] 검색 총시간 가드의 초과도 `timeout` 으로 분류하되, **가드 전용 타입**
    (`SearchBudgetExceeded`)으로만 받는다 — 내장 `TimeoutError` 를 그대로 매칭하면 이 함수가
    쓰이는 **모든** Spring 호출(`get_recent_purchases`·`add_to_cart`·`push_recommendations` …)에서
    무관한 이유로 난 `TimeoutError` 까지 전송 계층 실패로 둔갑한다(PR #293 리뷰). 나중에 다른
    경로에 `wait_for` 가 하나 생기면 그때부터 trace 가 조용히 거짓말을 시작하는 종류의 넓이다.
    """
    if isinstance(exc, httpx.TimeoutException | SearchBudgetExceeded):
        return "timeout"
    if isinstance(exc, httpx.NetworkError | httpx.RemoteProtocolError):
        return "connection_error"
    return None


def _failure_status_class(exc: BaseException) -> str:
    """재시도 로그 전용 유계 라벨 — 예외 원문은 싣지 않는다(#141).

    재시도 여부는 `_is_retryable` 이 따로 판단한다. 이 함수는 **관측 전용**이다.
    전송 계층 분류는 `_transport_status_class` 를 공유해 trace 와 어휘가 갈리지 않게 한다.
    """
    if (label := _transport_status_class(exc)) is not None:
        return label
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.response.status_code // 100}xx"
    return "malformed_response"


def _is_retryable(exc: BaseException) -> bool:
    """재시도가 의미 있는 실패만 True (#133).

    타임아웃·연결 오류·응답 중단·5xx 와 `_RETRYABLE_4XX`(408·429)는 일시 장애라 같은 요청이
    다음엔 성공할 수 있다. 반면 그 밖의 4xx 계약 오류와 응답 파싱 실패(ValueError/
    ValidationError)는 **같은 응답이 또 온다** — 재시도는 턴 예산만 태우고 결과를 바꾸지 못한다.

    전송 계층 판정은 `_transport_status_class` 를 재사용한다 — 재시도 대상과 관측 라벨이 같은
    분류에서 나와야 "재시도했다"와 "무엇이 실패했다"가 어긋나지 않는다.
    `LocalProtocolError`(우리 요청이 잘못됨)는 그 함수에서 None 이라 자연히 제외된다.
    """
    if _transport_status_class(exc) is not None:
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code in _RETRYABLE_4XX
    return False


_BatchT = TypeVar("_BatchT")


async def _fetch_with_batch_retries(
    factory: Callable[[], Awaitable[_BatchT]],
    *,
    operation: str,
    brand_id: int,
    retries: int,
) -> _BatchT:
    """무인 배치 전용 재시도 래퍼(#601) — `get_customer_features`(#592)와 같은 패턴을 공유한다.

    대화형 호출은 개별 GET 메서드를 `retries=0`(기본값)으로 그대로 부르므로 이 래퍼를
    거쳐도 조회 1회·무재시도 동작은 바뀌지 않는다. 배치 호출부(무인 스캔·스냅샷 배선,
    OPS-RUNTIME 결정 R-1)만 `retries>=1`을 넘긴다. 재시도 판정·backoff는
    `get_customer_features`와 동일 상수(`seller_customer_features_retry_backoff_s`)를
    재사용한다 — 무인 배치 전용 상수를 또 만들면 튜닝 지점이 흩어진다.
    """
    settings = get_settings()
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except SpringUnavailableError as exc:
            cause = exc.__cause__ or exc
            if attempt >= attempts or not _is_retryable(cause):
                raise
            _log.warning(
                "spring_batch_retry",
                extra={
                    "attempt": attempt,
                    "maxAttempts": attempts,
                    "operation": operation,
                    "brandId": brand_id,
                    "statusClass": _failure_status_class(cause),
                },
            )
            await asyncio.sleep(settings.seller_customer_features_retry_backoff_s * attempt)
    raise SpringUnavailableError(  # pragma: no cover - 루프가 항상 return/raise 함
        f"{operation}: retries 소진(brand_id={brand_id})"
    )


class SpringUnavailableError(Exception):
    """Spring 서버 도달 불가/오류 응답. 상위에서 SEARCH_FAILED 등으로 매핑한다."""


class SpringRejected(SpringUnavailableError):
    """#620 — `error_code_map` 에 없는 4xx 응답(BE 가 거부, 재시도해도 같은 결과).

    `_request` 의 공통 폴백은 매핑 안 된 오류를 전부 `SpringUnavailableError` 로 냈는데,
    그러면 5xx/타임아웃(진짜 일시 장애)과 4xx(영구 거부)가 같은 예외·같은 "재시도
    가능" 안내로 뭉개진다. `SpringUnavailableError` 의 하위로 두어 기존
    `except SpringUnavailableError` catch-all 은 그대로 잡되(하위 호환), 4xx 를
    구분해서 다뤄야 하는 호출부(`_confirm_stream` 등)는 이 예외를 먼저 잡아
    `retryable=False` 로 낼 수 있게 한다."""


class OrderStatusUnavailableError(SpringUnavailableError):
    """I-4 안전한 실패 분류. 원 예외·경로·응답 데이터는 보존하지 않는다."""

    def __init__(self, category: Literal["upstream_unavailable", "malformed_response"]) -> None:
        if category not in {"upstream_unavailable", "malformed_response"}:
            raise ValueError("invalid order status error category")
        self.category = category
        super().__init__(f"order status unavailable: {category}")


class InvalidCursorError(SpringUnavailableError):
    """I-17 400 INVALID_CURSOR — 안전한 전체 재구축이 필요한 신호."""


class CartOptionRequired(Exception):
    """I-2 400 CART_OPTION_REQUIRED — 옵션 필수인데 optionId 없음(§4.1). options 목록 동반."""

    def __init__(self, options: list[CartOption]) -> None:
        super().__init__("cart option required")
        self.options = options


class CartOptionInvalid(Exception):
    """I-2 400 CART_OPTION_INVALID — 옵션이 상품 소속 아님(§4.1). options 재확인용 동반."""

    def __init__(self, options: list[CartOption]) -> None:
        super().__init__("cart option invalid")
        self.options = options


class CartProductNotFound(Exception):
    """I-2 404 PRODUCT_NOT_FOUND — 없는 상품(§4.1)."""


class CartError(Exception):
    """I-2 담기 운영 오류(401 INTERNAL_TOKEN_INVALID·도달 불가·미상 코드) → action CART_ERROR."""


class CartStockInsufficient(Exception):
    """I-2 담기 재고 부족(400 CART_STOCK_INSUFFICIENT) → action reason STOCK_INSUFFICIENT.

    합산 수량 > 재고. [#508, 2026-08-09 BE 배포] 재고는 **담은 옵션 단위**다 —
    BE 가 재고 원천을 `product.stock_quantity`(상품 단위)에서 `product_stock(product_id,
    option_id, quantity)`(옵션 단위, 02 D33)로 전환했다. available_stock 은
    BE error.detail.availableStock — **담으려던 옵션의 남은 재고** — LLM "재고가 N개뿐이에요"
    안내용. 없으면 None. 옵션 필수 상품에서 구매 가능한 옵션이 하나도 안 남으면
    `CartOptionRequired` 대신 이 예외가 `available_stock=0` 으로 온다(api-spec §4.1).
    """

    def __init__(self, available_stock: int | None) -> None:
        self.available_stock = available_stock
        super().__init__(f"CART_STOCK_INSUFFICIENT (available={available_stock})")


class CartQuantityExceeded(CartError):
    """I-2 담기 수량 상한 초과(400 VALIDATION_ERROR, 합산 > 99) → action reason CART_ERROR.

    BE는 합산 수량 > CartItem.MAX_QUANTITY(99)일 때 VALIDATION_ERROR로 차단(CartService.addItem,
    재고검사보다 먼저). CartError 하위라 전용 핸들러가 없어도 CART_ERROR로 낙성한다.
    """


class CartItemNotFound(Exception):
    """I-24 404 CART_ITEM_NOT_FOUND(확정 2026-08-05) — 삭제 대상이 없음. 비멱등: 두 번째 호출도
    404. **[라운드 23]** `error.code` 가 정확히 이 값일 때만 낸다 — 엔드포인트 미배포로 오는
    다른/없는 code 의 404 는 `CartError` 다(`delete_cart_item` 참조)."""


class WishlistDuplicate(Exception):
    """I-26 409(확정 2026-08-05) — WISHLIST_DUPLICATE · RESOURCE_CONFLICT(UNIQUE 경합) 둘 다 이
    예외로 낙성한다("이미 찜함"으로 동일 처리, 정본 SSE 확장안도 두 코드를 구별하지 않는다).
    **[라운드 23]** `error.code` 가 이 둘 중 하나일 때만 낸다(`add_wishlist` 참조)."""


class WishlistProductNotFound(Exception):
    """I-26 404 PRODUCT_NOT_FOUND(확정 2026-08-05) — 없는 상품만. HIDDEN·품절은 찜 가능이라 이
    예외가 아니다. **[라운드 23]** `error.code` 가 정확히 이 값일 때만 낸다(`add_wishlist` 참조)."""


class WishlistNotFound(Exception):
    """I-27 404 WISHLIST_NOT_FOUND(확정 2026-08-05) — 찜 안 한 상품 삭제 시도. 없는 상품도 동일
    코드라 구별 불가(비멱등, 안내 문구는 하나로 통일). **[라운드 23]** `error.code` 가 정확히
    이 값일 때만 낸다(`remove_wishlist` 참조)."""


class WishlistError(Exception):
    """I-26/I-27/I-28 찜 운영 오류(401·400·도달 불가·미상 코드) → action *_ERROR reason WISHLIST_ERROR.

    403 AUTH_FORBIDDEN 도 여기로 떨어진다 — CartError 의 401 낙성과 같은 규약으로, 전용 예외를
    두면 "이 상품은 남의 소유"류 소유권 정보가 사용자에게 흘러나갈 수 있어 일부러 두지 않는다.
    """


# ── I-11/I-12 상품 쓰기 예외 (api-spec §4.5 — 정본 Notion I-11·I-12, 2026-08-05 개정) ──
#
# 삭제는 `status=DELETED` 전이이며 `HIDDEN`(숨김·판매정지)과 **다른 상태**다(BE 02 D41).
# 아래 409 두 종은 "재시도하면 되는 장애"가 아니라 **"안 되는 일"** 이라 SpringUnavailableError
# 로 뭉개면 HITL 이 재confirm 을 권해 판매자가 무한 재시도에 갇힌다 — I-30 과 같은 규약으로
# error.code 기반 전용 예외로 분리한다. SpringUnavailableError 하위가 아니다(catch-all 회피).


class ProductAlreadyDeleted(Exception):
    """I-12 409 ALREADY_DELETED — 이미 삭제된 상품(멱등 200 금지). `error.code` 가 정확히
    이 값일 때만 낸다. `HIDDEN`→`DELETED` 는 정상 전이라 여기 해당하지 않는다."""


class ProductDeletedNotEditable(Exception):
    """I-11 409 PRODUCT_DELETED — 삭제된 상품은 수정·복구 대상이 아니다(삭제는 I-12 전용
    전이). 404 가 아닌 이유: 상품은 실제로 존재하고 주문 내역·매출 통계에도 남아 있어
    에이전트가 "없는 상품"이라 답하면 사실과 다르다."""


class InvalidStock(Exception):
    """I-10/I-11 422 INVALID_STOCK (#524) — `stocks[].quantity` 음수 **또는** 그 상품의
    옵션이 아닌 `optionId`. 옵션별 재고 도입으로 BE 가 기존 재고 오류 코드에 두 번째
    조건을 접었다(BE 04 §9, 2026-08-09 — 새 code 를 만들지 않았다).

    AI 는 두 조건을 모두 **선차단**한다 — 음수는 `hitl._parse_int` 의 `isdigit()`,
    타 상품 옵션은 `stock_options.resolve_stock_option` 의 유일 매칭이다. 그래서 정상
    경로에서는 이 예외가 나지 않는다. 여기 도달하는 건 confirm 시점 I-9 재조회와 PATCH
    사이에 옵션이 삭제·변경된 **레이스**뿐이고, 그래서 안내가 "재조회 후 새 초안" 이다.
    SpringUnavailableError 하위가 아니다 — 재시도해도 같은 결과라 catch-all 로 뭉개면
    판매자가 무한 재confirm 에 갇힌다(I-12 ALREADY_DELETED 와 같은 규약)."""


class ProductCategoryInvalid(Exception):
    """I-10 400 PRODUCT_CATEGORY_INVALID (#541) — `categoryId` 가 없는 카테고리이거나
    **대분류**다(BE `SellerProductService.create` → `Category.isRoot()` 거부).

    AI 는 소분류만 담긴 스냅샷에서 id 를 고르므로 정상 경로에서는 나지 않는다. 여기
    도달하는 건 **스냅샷이 정본 DB 보다 낡은** 경우뿐이다(파일 교체 = 배포, #506).
    같은 초안을 다시 confirm 해도 결과가 같으므로 안내는 "재시도"가 아니라 **"카테고리를
    다시 말해 새 초안"** 이다. SpringUnavailableError 하위가 아니다(catch-all 회피)."""


class ProductFieldMissing(Exception):
    """I-10 422 MISSING_FIELD (#541) — BE 가 필수값(name·price·stockQuantity·categoryId)
    누락으로 거부. 본문의 `error.message` 에 누락 필드명이 실린다(BE `requireFields`).

    정상 경로에서는 `validate_draft` 가 4종을 모두 강제하므로 나지 않는다. 실제로 여기
    오는 경로는 **와이어 형식 불일치**다 — 예: `seller_stock_wire_mode="stocks"` 를 BE
    PR B 배포 전에 켜면 `stockQuantity` 를 안 실어 구 BE 가 누락으로 거부한다(#524 가
    말한 "등록은 시끄럽게 실패한다"의 그 지점). 종전엔 매핑이 없어 이 시끄러운 실패가
    `SpringUnavailableError` → "일시적인 오류(재시도 가능)" 로 **조용해졌다**.
    설정·배포를 고쳐야 풀리는 문제라 재시도 안내를 하면 안 된다."""


class InvalidPrice(Exception):
    """I-10/I-11 422 INVALID_PRICE(#620) — `price > originalPrice`(BE `validatePriceRange`,
    저장된 값 기준 교차 검증 — PATCH 에서 한쪽만 보내도 생략분은 DB 현재값으로 채워
    비교한다).

    AI 는 update 를 **row-aware 로 선차단**한다(`hitl.validate_draft` 의 `row` 인자) —
    confirm 직전 재조회한 현재값과 초안의 변경분을 합성해 미리 비교한다. 그래도 여기
    도달하는 건 draft 표시와 confirm 사이 다른 채널(웹 UI 등)에서 가격이 바뀐 **레이스**
    뿐이다. `InvalidStock`·`ProductCategoryInvalid` 와 같은 이유로 `SpringUnavailableError`
    하위가 아니다 — 재시도해도 같은 결과라 catch-all 로 뭉개면 판매자가 무한 재confirm
    에 갇힌다."""


# ── I-30 발송 처리 예외 (이슈 #297, §4.19 — 🔶 초안, BE 협의 전) ──────────────────
#
# HITL 쓰기(발송)는 "이미 된 일"과 "방금 한 일"과 "안 되는 일"을 구분해야 거짓 성공
# 보고를 막는다(I-12 ALREADY_DELETED 논리) — SpringUnavailableError 로 뭉개면 셋 다
# "재시도 가능한 장애"가 되므로 error.code 기반 전용 예외로 분리한다. 이 예외들은
# SpringUnavailableError 하위가 아니다 — 도구/HITL 의 catch-all 에 삼켜지지 않는다.


class OrderItemNotFound(Exception):
    """I-30 404 ORDER_ITEM_NOT_FOUND — 없는 orderItemId 또는 타 브랜드 아이템(존재 은닉,
    403 없음 — 2026-07-28 정리 상속). `error.code` 가 정확히 이 값일 때만 낸다."""


class OrderAlreadyShipped(Exception):
    """I-30 409 ORDER_ALREADY_SHIPPED — 이미 SHIPPING(멱등 200 금지, I-12 논리).
    2026-08-05 개명(구 ALREADY_SHIPPED — 공통 규약 `<도메인>_<사유>` 형식). 과도기
    호환으로 구 코드도 이 예외로 낙성한다."""


class OrderInvalidTransition(Exception):
    """I-30 400 ORDER_INVALID_TRANSITION — 허용 전이 아님(현재 상태가 ORDERED 가 아님,
    활성 클레임 포함) · 발송 후 역전이 일체 · MVP 미허용 전이."""


def _client(*, timeout: float | None = None) -> httpx.AsyncClient:
    """공용 httpx.AsyncClient 팩토리. base_url·서비스 토큰은 설정에서 주입한다.

    타임아웃 3s — api-spec §2.9 c (AI→Spring 콜백 통일 기준). 초과 시 각 계약의
    degrade 규칙 적용(조회 생략·담기 CART_ERROR·dedup 생략 등).

    [#427] `timeout` 이 None 이면 종전대로 `settings.spring_timeout_s` 를 쓴다 — **`search_
    products` 호출부만** 검색 전용 타임아웃(`settings.spring_search_timeout_s`)을 넘긴다. 다른
    호출부(I-2 담기·I-3 인기·I-18/I-19 조회·I-21 push·판매자 레인)는 손대지 않는다(#394 가
    기각된 "스칼라 하나를 공유해 전 구간이 함께 늘어난다" 실패 모드를 되풀이하지 않는다).
    """
    settings = get_settings()
    headers = (
        {"X-Internal-Token": settings.internal_api_token} if settings.internal_api_token else {}
    )
    return httpx.AsyncClient(
        base_url=settings.spring_base_url,
        timeout=timeout if timeout is not None else settings.spring_timeout_s,
        headers=headers,
    )


def _search_query_params(
    filters: ProductSearchFilters,
    *,
    color_values: list[str] | None = None,
    brand_values: list[str] | None = None,
) -> dict:
    """decompose 필터 → BE I-1 GET 쿼리 파라미터 (§4.6, C-15).

    BE I-1 파라미터는 keyword/categoryName/minPrice/maxPrice/brandName/color 뿐이다(color=#100 P1).
    [2026-07-23, BE 합의] size 제거 — 라운드1은 고정필터 매칭을 전량 반환하고, 결과 수 제한(top-K)은
    AI 쪽(search_catalog 가 filters.limit 로 절단)에서 적용한다(api-spec §4.6). [#395, 2026-08-07]
    재도입 폐지 확정. brandName 은 다중 —
    브랜드 전량을 반복 파라미터로 실어 BE IN 필터에 맡긴다(방법 D, #100 P1, BE 배열 수용 협의 대상).
    나머지 필터(excludeProductIds·ratingMin)는 여기 싣지 않고 AI 사후필터(search_service)로 처리한다.
    정렬은 rerank(LLM) 소관(#100 P2, sort 필드 제거).
    """
    params: dict[str, object] = {}
    # LLM(decompose) 산출 텍스트 필터는 공백-only('  ') 도 빈 값으로 보고 미전송한다 —
    # `if filters.X:` 는 ''(falsy)만 막고 ' '(truthy)는 통과시켜 BE 에 빈값이 나갔다(#127 리뷰).
    if filters.keyword and filters.keyword.strip():
        params["keyword"] = filters.keyword
    if filters.category and filters.category.strip():
        params["categoryName"] = filters.category
    if filters.price_min is not None:
        params["minPrice"] = filters.price_min
    if filters.price_max is not None:
        params["maxPrice"] = filters.price_max
    if filters.brand:
        # 다중 브랜드 전량 전송(방법 D) — httpx 가 brandName=A&brandName=B 반복 파라미터로
        # 직렬화 → BE IN 필터(#100 P1). 조건칩(state)도 전 브랜드를 표시하므로 요청·표시가 일치한다.
        # 빈/공백 요소는 제거(LLM 이 [""] 등을 낼 수 있음 — brandName= 빈값 전송 방지, #127 리뷰).
        # [#466] `brand_values` 는 법인 표기 확장(`app.pipelines.brand_aliases`)의 결과다 —
        # None 이면 종전대로 원문을 싣는다. 확장값은 **사용자 원문을 먼저 포함**하는 가산 목록
        # 이라(그 함수의 계약) 여기서 원문과 합치지 않는다. 조건칩은 `filters.brand`(원문)를
        # 그대로 보므로 확장이 표시에 새지 않는다.
        source = filters.brand if brand_values is None else brand_values
        brands = [b for b in source if b and b.strip()]
        if brands:
            params["brandName"] = brands
    if filters.color and filters.color.strip():
        # None은 계약 변경 전 현행 단수 경로다. 리스트는 api-spec §4.6 `color: string[]`
        # 개정과 BE 배포 뒤 플래그가 켜진 경우에만 httpx 반복 파라미터로 직렬화한다.
        params["color"] = filters.color if color_values is None else color_values
    return params


def search_filter_axes(
    filters: ProductSearchFilters, *, color_values: list[str] | None = None
) -> set[str]:
    """실제 I-1 로 나가는 쿼리 파라미터 키 집합 (#393).

    판정이 **decompose 산출**(어떤 필드가 채워졌는가)이 아니라 **최종 payload**(실제로 Spring
    에 나가는 파라미터가 무엇인가) 기준이 되게 하는 단일 출처다 — `_search_query_params` 를
    그대로 위임한다(축 목록 사본을 두면 새 필터가 생겼을 때 한쪽만 늘어나 드리프트한다).
    `rating_min`·`attr_conditions`·`exclude_product_ids` 는 **AI 사후필터**(search_service)라
    Spring payload 축이 아니다 — 아무리 값이 있어도 Spring 은 매칭 전량을 내려준다.
    """
    return set(_search_query_params(filters, color_values=color_values).keys())


def _parse_search_response(data: object) -> ProductSearchResult:
    """BE I-1 응답 → ProductSearchResult (§4.6, v0.15.5).

    현재 Spring ``ApiResponse<List<...>>`` 는 data 자체가 배열({success, data:[...]})이다.
    구 계약의 data:{items:[...]} 형태도 브랜치 간 호환을 위해 함께 수용한다. total_count 는
    파싱 성공 후보 수다.
    [PR#127 리뷰] 항목은 **개별 검증**한다 — 후보 1건의 스키마 위반(누락 필드·타입 이상)이
    단일 comprehension 이면 리스트 전체 생성을 실패시켜, 멀쩡한 나머지 후보까지 통째로 버려진다.
    실패분만 WARNING 로그로 남기고 skip 해 나머지를 보존한다. 단 dict 항목이 있었는데 **전부**
    검증 실패(정상 0건)면 개별 이상이 아니라 systematic 스키마 붕괴이므로 조용한 zero-result 로
    위장하지 않고 예외를 재발생시켜 SEARCH_FAILED 로 degrade 한다(§7 fail-closed 유지).
    [PR#127 리뷰] 최상위 envelope 자체가 어긋난 경우도 같은 §7 원칙으로 fail-closed 한다 —
    (1) `success:false`(200 이어도 실패 envelope, fetch_product_changes 와 동일 규약),
    (2) data 키 없음·미인식 형태·비 list/dict. 정상 0건과 구분 안 되는 빈 결과로 삼키지 않는다.
    단 `data:null`·`data:[]` 은 정상 0건이라 예외 없이 빈 결과를 반환한다. 구 wrapped 형태
    (data:{items:[...]})만 호환 수용하고, data 키 없는 top-level items 레거시 분기는 제거했다.
    """
    items: list = []
    if isinstance(data, list):
        items = data  # 래퍼 없이 바디가 곧 배열인 경우도 수용
    elif isinstance(data, dict):
        # [PR#127 리뷰] success:false 는 200 이어도 실패 envelope — fetch_product_changes 와 동일
        # 규약. 정상 0건(빈 결과)으로 삼키지 않고 fail-closed(§7)로 degrade 한다.
        if data.get("success") is False:
            _log.warning("검색 응답 success=false — 실패 envelope, SEARCH_FAILED degrade(§7)")
            raise ValueError("검색 응답 success=false(실패 envelope)")
        payload = data.get("data")
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            # 구 계약 wrapped 형태(data:{items:[...]}) 만 호환 수용. top-level items(data 키 없이)는
            # data 키 부재 drift 로 아래 fail-closed 가 잡는다(#127 리뷰, 레거시 분기 제거).
            items = payload["items"]
        elif payload is not None:
            # data 키는 있으나 알려진 형태(list · {items})와 안 맞음 — envelope drift.
            # [PR#127 리뷰] 정상 0건과 구분 안 되는 빈 결과 대신 fail-closed(§7) — 항목 단위와 일관.
            _log.warning(
                "검색 응답 data 형태 미인식 — envelope drift, SEARCH_FAILED degrade(§7), 타입=%s",
                type(payload).__name__,
            )
            raise ValueError(
                f"검색 응답 data 형태 미인식(envelope drift): {type(payload).__name__}"
            )
        elif "data" not in data:
            # data 키 자체가 없음(= data:null 과 구분) — 더 의심스러운 drift → fail-closed(§7).
            _log.warning("검색 응답에 data 키가 없음 — envelope drift, SEARCH_FAILED degrade(§7)")
            raise ValueError("검색 응답에 data 키가 없음(envelope drift)")
        # (payload is None 이고 data 키는 있음 = data:null → 정상 0건, 예외 아님)
    else:
        _log.warning(
            "검색 응답 최상위 형태 미인식 — envelope drift, SEARCH_FAILED degrade(§7), type=%s",
            type(data).__name__,
        )
        raise ValueError(f"검색 응답 최상위 형태 미인식(envelope drift): {type(data).__name__}")
    products: list[SpringProduct] = []
    invalid = 0  # 검증 실패로 skip 된 항목 수(비-object 포함)
    last_err: ValidationError | None = None
    for it in items:
        if not isinstance(it, dict):
            # 항목이 object 조차 아님 — 개별 필드 결측보다 심각한 최상위 타입 붕괴다. skip 하되
            # invalid 로 세어 아래 fail-closed 가드가 이 케이스도 잡게 한다(PR#127 리뷰).
            invalid += 1
            _log.warning("검색 후보가 object 아님(skip) — type=%s", type(it).__name__)
            continue
        try:
            products.append(SpringProduct.model_validate(it))
        except ValidationError as exc:
            # 후보 1건의 스키마 이상은 그 항목만 skip — 나머지 정상 후보 보존(PR#127 리뷰).
            invalid += 1
            last_err = exc
            _log.warning("검색 후보 파싱 실패로 skip(전체 실패 아님) — %s", exc)
    # §7 fail-closed: 항목이 있었는데 **전부** 무효(정상 0건)면 개별 이상이 아니라 systematic
    # 스키마 붕괴다 → 조용한 zero-result 로 위장하지 않고 SEARCH_FAILED 로 degrade. dict 항목의
    # ValidationError 는 그대로 재발생, 전부 비-object 등 재발생할 예외가 없으면 ValueError 로.
    if invalid and not products:
        _log.warning("검색 후보 전량 무효(%d건) — SEARCH_FAILED degrade(§7)", invalid)
        raise last_err or ValueError("검색 응답 항목이 전부 유효하지 않음(스키마 붕괴, §7)")
    if invalid:
        _log.warning("검색 후보 %d건 skip(스키마 이상) / 정상 %d건", invalid, len(products))
    return ProductSearchResult(products=products, total_count=len(products))


def _parse_error_code(resp: httpx.Response) -> str | None:
    """실패 응답에서 error.code(|code) 만 뽑는다 — 옵션·재고 파싱이 필요 없는 호출용.

    I-24(삭제)·I-26/I-27(찜)은 CART_OPTION_REQUIRED 류 옵션 동반 응답이 없어 `_parse_cart_error`
    의 옵션·재고 파싱까지 돌릴 필요가 없다. code 추출 로직을 두 함수가 각자 구현하면 새 실패
    코드가 추가될 때 한쪽만 고쳐지는 드리프트가 생기므로, 이 헬퍼를 단일 출처로 두고
    `_parse_cart_error` 도 내부에서 이걸 쓴다(중복 파서 금지).
    """
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    err = body.get("error") if isinstance(body.get("error"), dict) else None
    code = (err or {}).get("code") or body.get("code")
    return code if isinstance(code, str) else None


def _parse_cart_error(resp: httpx.Response) -> tuple[str | None, list[CartOption], int | None]:
    """I-2 실패 응답에서 code·options 를 방어적으로 파싱한다(§4.1, BE 스키마 🔴).

    code 는 `_parse_error_code` 공유(error.code | code). options 는 [BE 확정 2026-07-18]
    **error.detail.options**([{optionId, name, extraPrice}])를 우선하고, 구버전 위치
    (error.options·options·data.options)도 방어적으로 본다. name 은 name|optionName,
    extraPrice(추가금)까지 읽는다.
    """
    code = _parse_error_code(resp)
    try:
        body = resp.json()
    except ValueError:
        return None, [], None
    if not isinstance(body, dict):
        return None, [], None
    err = body.get("error") if isinstance(body.get("error"), dict) else None
    detail = (err or {}).get("detail") if isinstance((err or {}).get("detail"), dict) else None
    # BE 확정 위치(error.detail.options)는 '키 존재'로 우선한다 — 빈 배열이어도 그 값을 신뢰하고
    # 구버전 위치로 조용히 폴백하지 않는다(잔재 options 오선택 방지).
    if detail is not None and "options" in detail:
        raw = detail.get("options") or []
    else:
        raw = (
            (err or {}).get("options")
            or body.get("options")
            or (body.get("data") or {}).get("options")
            or []
        )
    options: list[CartOption] = []
    for opt in raw if isinstance(raw, list) else []:
        if isinstance(opt, dict) and opt.get("optionId") is not None:
            # extraPrice 는 표시용 부가 필드 — 어떤 병적 입력(NaN/Inf/초대형 정수/이상 타입)에도
            # 옵션 자체를 버리거나 스트림을 죽이지 않게 통째로 방어(실패 시 None 강등).
            raw_extra = opt.get("extraPrice")
            try:
                if (
                    isinstance(raw_extra, bool)
                    or not isinstance(raw_extra, (int, float))
                    or not math.isfinite(raw_extra)
                ):
                    extra = None
                else:
                    # BE(Java) BigDecimal/Double 직렬화가 1000.0·999.9999998 처럼 올 수 있어 반올림 수용.
                    extra = round(raw_extra)
            except (ValueError, OverflowError, TypeError):
                extra = None
            try:
                options.append(
                    CartOption(
                        option_id=opt["optionId"],
                        name=opt.get("name") or opt.get("optionName") or "",
                        extra_price=extra,
                    )
                )
            except (ValidationError, ValueError, TypeError):
                continue  # 형식 이상 옵션은 건너뜀 — 되물음 흐름 전체가 죽지 않게(방어적)
    # 파싱 실패를 집계해 한 번에 로그(부분 실패도 포함). error.detail.options 는 BE 확정 계약이라
    # 실패는 계약 위반 신호 — REQUIRED/INVALID 공통 파서이므로 실제 code 를 함께 찍어 진단 오도 방지.
    if isinstance(raw, list) and raw:
        dropped = len(raw) - len(options)
        if not options:
            _log.warning(
                "cart 옵션 응답(code=%r) options 전부 파싱 실패(계약 위반 가능): %r", code, raw
            )
        elif dropped:
            _log.warning("cart 옵션 %d/%d개 파싱 실패(부분, code=%r)", dropped, len(raw), code)
    # 재고 부족(CART_STOCK_INSUFFICIENT)일 때 error.detail.availableStock(남은 재고). 병적 입력 방어.
    available_stock: int | None = None
    if detail is not None:
        raw_stock = detail.get("availableStock")
        if isinstance(raw_stock, bool):
            raw_stock = None
        if isinstance(raw_stock, int) and raw_stock >= 0:
            available_stock = raw_stock
        elif isinstance(raw_stock, float) and math.isfinite(raw_stock) and raw_stock >= 0:
            # BE(Java) Double 직렬화가 4.9999998·5.0000002 처럼 올 수 있어 반올림(extraPrice 파싱과 동일).
            # int() 절삭은 실제보다 1 적게 안내할 수 있음(재고는 정수 count).
            available_stock = round(raw_stock)
    return code, options, available_stock


async def search_products(filters: ProductSearchFilters) -> ProductSearchResult:
    """Spring 상품 검색 위임 — I-1 GET /internal/products/search (api-spec §4.6 / C-15).

    유일·영구 후보 확보 경로. 사용자 확정에 따라 **GET** 으로 호출한다. dedup·평점·정렬은
    응답 후 AI 사후필터(search_service.search_catalog)에서 적용한다. 도달 불가/오류 응답은
    SpringUnavailableError 로 전파 — 상위(추천 그래프)가 SEARCH_FAILED 로 낸다.

    [#133] 재시도 가능한 실패(`_is_retryable`)는 config 상한만큼 재시도한다. 검색은 GET·멱등이라
    안전하며, 후보가 없으면 추천 자체가 성립하지 않아 한 번의 일시 지연이 곧 턴 종료였다.
    **재시도 루프는 span 안쪽**이다 — 호출당 span 1개라는 계약(테스트가 고정)을 지키고,
    `statusClass` 는 마지막 시도의 결과를 남긴다. 재시도 사실은 구조화 로그로만 관측한다.
    """
    settings = get_settings()
    color_values: list[str] | None = None
    # 계약 게이트: api-spec §4.6 color가 string→string[]으로 개정되고 BE가 배포된 뒤에만 on.
    # off면 색상 사전 캐시/DB를 아예 건드리지 않아 기존 와이어를 바이트 단위로 보존한다.
    if settings.color_synonym_expansion_enabled and filters.color and filters.color.strip():
        try:
            from app.pipelines import color_synonyms  # noqa: PLC0415 - lazy DB 경로

            mapping = await _load_color_synonym_map(settings)
            if mapping is not None:
                color_values = color_synonyms.expand_color(filters.color, mapping)
        except Exception:
            # 색상 확장은 보조 품질 경로다. DB 장애가 본 검색을 죽이지 않게 기존 단수로 degrade.
            _log.warning("색상 동의어 확장 실패 — 원문 단수 color로 검색", exc_info=True)
    # [#466] 브랜드 법인 표기 확장 — 순수 함수라 DB·네트워크 의존이 없어 색상처럼 try 로
    # 감싸지 않는다(삼킬 실패가 없다). 꺼져 있으면 None → 와이어가 종전과 바이트 동일하다.
    brand_values = brand_aliases.brand_wire_values(
        filters.brand,
        enabled=settings.brand_alias_expansion_enabled,
        cap=settings.brand_alias_max_values,
    )
    params = _search_query_params(filters, color_values=color_values, brand_values=brand_values)
    attempts = settings.spring_max_retries + 1
    # [#132] 검색 1회의 **총시간** 상한. `spring_search_timeout_s` 는 httpx 에 스칼라로 주입돼
    # connect/read/write/pool 네 시계가 되는데 `read` 는 **청크 사이 간격** 상한이라, 바디가
    # 끊기지 않고 계속 오면 한 번도 물리지 않는다 — `size` 제거(전량 반환, §4.6)로 바디가 커진
    # 뒤로는 "3s 안에 끝난다"가 보장이 아니다. config 는 이미 `spring_search_timeout_s ×
    # (재시도+1)` 을 검색 예산으로 **가정**하고 스트림 상한을 기동 검증하는데, 그 가정을
    # 집행하는 코드가 없었다. 새 튜너블을 만들지 않고 같은 식을 쓴다 — 검증과 집행이 갈라지면
    # 한쪽만 고쳐 놓고 지켜진다고 믿게 된다.
    # [#277 병합 → #306] 곱하는 값은 상수가 아니라 **실제 시도 수**(`attempts`)다 — 예산과
    # 시도 수가 갈리면 한쪽만 고쳐 놓고 지켜진다고 믿게 된다. #277 때는 미룬 턴만 시도 수가
    # 1 로 갈려 이 구분이 실제 분기였고, #306 이 그 억제를 없애 지금은 설정 하나에서 나온다.
    # `graph.py::_stage_budget` 의 `stage_cap` 산출이 **글자 그대로 같은 식**을 쓴다(D7).
    # [#427, DESIGN-SHARED-BUDGET-384 §3 D4] `narrow_search_budget()` 로 잔여 예산이 주입돼
    # 있으면(구제 체인 좁히기) 그 값을 **총시간(재시도 포함)의 상한**으로 쓴다 — 재시도 루프는
    # 그 안에서 돈다. 주입이 없으면(`observe` 모드·`"full"` 판정 단·비-검색 단계) 종전대로
    # 계산한다 — 기본 모드는 #406 이후 `narrow` 라 구제 체인 단은 대개 주입을 받는다.
    override = _search_budget_override.get()
    budget_s = override if override is not None else settings.spring_search_timeout_s * attempts

    async def _fetch_and_parse(span: TraceNode | None) -> ProductSearchResult:
        for attempt in range(1, attempts + 1):
            try:
                # [#427] search_products 만 검색 전용 타임아웃을 넘긴다 — 다른 호출부는
                # 손대지 않는다(`_client` docstring 참조).
                async with _client(timeout=settings.spring_search_timeout_s) as client:
                    resp = await client.get("/internal/products/search", params=params)
                    _record_spring_status(span, resp)
                    resp.raise_for_status()
                    # [#132] 역직렬화를 이벤트루프 밖으로 — 전량 반환이라 바디 크기에 상한이
                    # 없고, 루프 위에서 돌면 그 시간만큼 **같은 워커의 다른 SSE 스트림이 전부
                    # 멈춘다**(실 응답 13.33MB/7,245건 실측: 5 동시 992ms 정지 → 424ms,
                    # MEASURE-I1-RESPONSE-132 §4). 전용 풀인 이유는 `_get_parse_executor` 참조.
                    data = await _run_in_parse_executor(resp.json)
                break
            except (httpx.HTTPError, ValueError) as exc:
                if attempt >= attempts or not _is_retryable(exc):
                    raise
                # 예외 원문은 남기지 않는다 — 업스트림 상태 유출 방지(#141), 유계 라벨만.
                _log.warning(
                    "spring_search_retry",
                    extra={
                        "attempt": attempt,
                        "maxAttempts": attempts,
                        "statusClass": _failure_status_class(exc),
                    },
                )
                notify_search_retry()
        # 응답 파싱·검증도 같은 경계 안 — 200 이지만 스키마 불일치인 malformed 응답도
        # SEARCH_FAILED degrade(§7)로 흐르게 한다(ValidationError 가 그대로 새어 500 되지 않게).
        # 역직렬화와 같은 이유로 스레드에 넘긴다 — N× model_validate 가 파싱 비용의 본체다.
        return await _run_in_parse_executor(_parse_search_response, data)

    try:
        with _spring_span("search_products", "GET") as span:
            try:
                # ⚠️ `wait_for` 는 **await 를 취소할 뿐 스레드를 죽이지 못한다** — 초과 시 파싱
                # 스레드는 전용 풀에서 끝까지 돈다. 그래도 턴은 즉시 degrade 로 풀려나 사용자
                # 지연과 스트림 예산은 유계가 된다(스레드는 곧 스스로 끝난다).
                return await asyncio.wait_for(_fetch_and_parse(span), timeout=budget_s)
            except TimeoutError as exc:
                # 가드 전용 타입으로 좁혀 던진다 — 분류(`_transport_status_class`)가 이 경로만
                # timeout 으로 보게 하려는 것이다(PR #293 리뷰).
                # **조용히 자르지 않는다**: 이 가드가 자주 트립하면 전용 풀에 버려진 파싱
                # 스레드가 쌓인다는 신호라, 트립 자체가 운영 관측 대상이다.
                _log.warning(
                    "spring_search_budget_exceeded",
                    extra={"budgetS": budget_s, "attempts": attempts},
                )
                raise SearchBudgetExceeded(f"검색 총시간 예산 초과({budget_s}s)") from exc
    except (httpx.HTTPError, ValueError, ValidationError, TimeoutError) as exc:
        raise SpringUnavailableError(f"search_products 실패: {exc}") from exc


async def get_popular_products(size: int) -> ProductSearchResult:
    """인기 상품 후보 조회 — I-3 (api-spec §4.17, 이슈 #162).

    GET {spring_base_url}/internal/products/popular?size= + X-Internal-Token.
    조건이 하나도 없는 발화("아무거나 추천해줘")의 후보 소스다 — I-1 은 필터가 전부 비면
    매칭 전량(실측 7,245건·13.33MB)을 돌려주고 그 상위가 사용자 의도와 무관하다.

    **응답이 I-1 과 동일 DTO** 라 `_parse_search_response` 를 그대로 재사용한다(정본 I-1:
    "같은 DTO 를 쓰는 I-3 도 동일하게 나간다"). 하류(dedup·rerank·I-21 push)도 그대로다.

    **0건은 성공이다** — 정본 §4.17: "빈 배열도 정상 결과다. 카드 없이 텍스트만 답하면 된다".
    여기서 예외를 던지면 상위가 degrade 로 오인해 무필터 I-1 폴백을 태우는데, 그게 바로 이
    함수가 없애려는 호출이다.

    **재시도하지 않는다** — §2.9(c) 의 재시도 1회는 `search_products`(I-1) 전용 예외이고 그
    예산은 이미 first-token 상한을 압박한다(#277 실측: 재시도가 이벤트 0건·504 를 8/8 재현,
    #288). 일관성 명목으로 `attempts` 루프를 옮겨 오지 말 것.

    `size` 는 호출부가 config(`popular_candidate_size`)에서 주입한다 — BE 에 범위 검증이 없어
    음수·0 이 400 이 아니라 빈 배열로 오므로 양수 보장은 AI 쪽 책임이다(Settings 가 `gt=0`).
    """
    try:
        with _spring_span("get_popular_products", "GET") as span:
            async with _client() as client:
                resp = await client.get("/internal/products/popular", params={"size": size})
                _record_spring_status(span, resp)
                resp.raise_for_status()
                data = resp.json()
        return _parse_search_response(data)
    except (httpx.HTTPError, ValueError, ValidationError, TimeoutError) as exc:
        raise SpringUnavailableError(f"get_popular_products 실패: {exc}") from exc


async def get_recent_purchases(user_id: int, status: str | None = None) -> RecentPurchases:
    """구매 이력 질의 시점 조회 — I-19 (스텁, api-spec §4.7 / C-6).

    GET {spring_base_url}/internal/members/{user_id}/orders + X-Internal-Token.
    userId 는 AI-검증 JWT sub 유래(신원 도출, 요청 본문 불신). 응답은 주문 상세 배열(OrderHistory) —
    ⚠️ items 에 category 없음: dedup 은 exact productId 제외만, 카테고리 억제 불가(§4.7 갭).
    소비처: 추천 dedup(결정 14-F) + 프로필 sleep-time 구매 소스(결정 16).
    게스트는 호출 스킵(이력 없음, 호출 측 책임). 실패/타임아웃 시 dedup 없이 추천 진행(degrade, §4.7).
    [배선 완료]
    """
    params: dict[str, object] = {}
    if status is not None:
        params["status"] = status
    try:
        with _spring_span("get_recent_purchases", "GET") as span:
            async with _client() as client:
                resp = await client.get(f"/internal/members/{user_id}/orders", params=params)
                _record_spring_status(span, resp)
                resp.raise_for_status()
                data = resp.json()
        payload = data.get("data") if isinstance(data, dict) else None
        orders = payload.get("orders") if isinstance(payload, dict) else None
        return RecentPurchases.model_validate({"orders": orders or []})
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        raise SpringUnavailableError(f"get_recent_purchases 실패: {exc}") from exc


async def get_order_status(user_id: int) -> OrderStatusSummary:
    """회원의 최근 I-4 주문 상태를 strict envelope/schema로 검증한다."""
    try:
        with _spring_span("get_order_status", "GET") as span:
            async with _client() as client:
                response = await client.get(
                    f"/internal/members/{user_id}/orders/status",
                    params={"recent": ORDER_STATUS_RECENT},
                )
                _record_spring_status(span, response)
                response.raise_for_status()
    except httpx.HTTPError:
        raise OrderStatusUnavailableError("upstream_unavailable") from None

    try:
        body = response.json()
    except ValueError:
        raise OrderStatusUnavailableError("malformed_response") from None

    if (
        type(body) is not dict
        or body.get("success") is not True
        or "data" not in body
        or type(body["data"]) is not dict
        or "orders" not in body["data"]
        or type(body["data"]["orders"]) is not list
    ):
        raise OrderStatusUnavailableError("malformed_response") from None

    try:
        return OrderStatusSummary.model_validate({"orders": body["data"]["orders"]})
    except ValidationError:
        raise OrderStatusUnavailableError("malformed_response") from None


async def add_to_cart(request: AddToCartRequest) -> AddToCartResult:
    """장바구니 담기 — BE I-2 문서 채택 (api-spec §4.1). [배선 완료]

    POST {spring_base_url}/internal/cart/items + X-Internal-Token. 본문 신원(userId|guestId)은
    AI-검증 JWT sub 유래(요청 본문 불신, §2.3). 담기 시 BE 재고검증 있음(합산 수량 > 재고 →
    CART_STOCK_INSUFFICIENT + availableStock, 2026-07-22). 실패 코드는 typed 예외로 전파:
    CART_OPTION_REQUIRED→CartOptionRequired(options), CART_OPTION_INVALID→CartOptionInvalid(options),
    CART_STOCK_INSUFFICIENT→CartStockInsufficient(availableStock), VALIDATION_ERROR(합산>99)→
    CartQuantityExceeded, 404→CartProductNotFound, 그 외→CartError.
    """
    if (request.user_id is None) == (request.guest_id is None):
        raise CartError("add_to_cart 신원 body 는 정확히 하나여야 함")
    try:
        with _spring_span("add_to_cart", "POST") as span:
            async with _client() as client:
                resp = await client.post(
                    "/internal/cart/items", json=request.model_dump(by_alias=True)
                )
                _record_spring_status(span, resp)
    except httpx.HTTPError as exc:
        raise CartError(f"add_to_cart 도달 실패: {exc}") from exc

    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError as exc:
            raise CartError(f"add_to_cart 응답 파싱 실패: {exc}") from exc
        payload = data.get("data") if isinstance(data, dict) else None
        cart_item_id = payload.get("cartItemId") if isinstance(payload, dict) else None
        return AddToCartResult(success=True, cart_item_id=cart_item_id)

    code, options, available_stock = _parse_cart_error(resp)
    if resp.status_code == 400 and code == "CART_OPTION_REQUIRED":
        raise CartOptionRequired(options)
    if resp.status_code == 400 and code == "CART_OPTION_INVALID":
        raise CartOptionInvalid(options)
    if resp.status_code == 400 and code == "CART_STOCK_INSUFFICIENT":
        raise CartStockInsufficient(available_stock)
    if resp.status_code == 400 and code == "VALIDATION_ERROR":
        # 현 계약(api-spec §4.1)상 이 엔드포인트의 VALIDATION_ERROR 는 "합산 수량 > 99"뿐 → 수량 상한으로 매핑.
        # BE 가 다른 검증 실패를 같은 코드로 재사용하면 계약 드리프트이므로 message 를 남겨 관측 가능하게 한다.
        try:
            body = resp.json()
        except ValueError:
            body = None
        err = body.get("error") if isinstance(body, dict) else None
        be_message = err.get("message") if isinstance(err, dict) else None
        _log.warning(
            "cart VALIDATION_ERROR → 수량초과로 매핑(드리프트 관측): message=%r", be_message
        )
        raise CartQuantityExceeded(f"add_to_cart 수량 상한 초과: {code}")
    if resp.status_code == 404:
        raise CartProductNotFound()
    # 401 INTERNAL_TOKEN_INVALID·미상 코드 → 운영 오류
    raise CartError(f"add_to_cart 실패: {resp.status_code} {code}")


async def get_cart(user_id: int | None = None, guest_id: str | None = None) -> CartView:
    """장바구니 조회 — I-18 (api-spec §4.9 / C-16). [배선 완료]

    GET {spring_base_url}/internal/cart?userId=|guestId= + X-Internal-Token.
    빈 장바구니는 items=[] 정상 200. 도달 불가/오류/스키마 불일치는 SpringUnavailableError —
    조회는 안내용이라 상위(담기)는 조회 실패 시에도 진행한다(degrade, §4.9).
    """
    params: dict[str, object] = {}
    if user_id is not None:
        params["userId"] = user_id
    if guest_id is not None:
        params["guestId"] = guest_id
    try:
        with _spring_span("get_cart", "GET") as span:
            async with _client() as client:
                resp = await client.get("/internal/cart", params=params)
                _record_spring_status(span, resp)
                resp.raise_for_status()
                data = resp.json()
        payload = data.get("data") if isinstance(data, dict) else None
        items = payload.get("items") if isinstance(payload, dict) else None
        return CartView.model_validate({"items": items or []})
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        raise SpringUnavailableError(f"get_cart 실패: {exc}") from exc


# ── 장바구니 삭제 · 찜 (이슈 #116·#117, I-24~I-28 — 확정 2026-08-05, Spring 구현됨) ──
# [#285] BE `jarvis-backend` main 실측(2026-08-08, BE PR #92·#93) — api-spec §4.12~4.16 v0.31.3.


def _envelope_success_false(resp: httpx.Response) -> bool:
    """200 이지만 공통 실패 봉투(`{success:false, ...}`)로 왔는지만 본다(2차 리뷰 지적 5).

    신설한 delete_cart_item·add_wishlist·remove_wishlist·get_wishlist 는 HTTP 상태만 보고
    200 이면 성공으로 처리했다 — 정본 공통 봉투는 성공이 `{success:true, …}` 인데, Spring 이
    200 + `{success:false}` 를 낼 가능성을 놓치고 있었다.

    `success` 키가 **없으면** false 로 간주하지 않는다 — "명시 안 됨"과 "명시적 false"는 다른
    사실이다(`app/agents/buyer/graph.py::_relaxed_filters_from_offer` 근방의 "없음"과 "null 로
    해제"를 가르는 것과 같은 구분). 이 함수는 이번에 신설한 4개 함수에만 쓴다 — `add_to_cart`·
    `get_cart` 는 같은 성질의 선행 구멍이 있지만 이 PR 범위가 아니고 다른 레인이 그 경로에
    의존해 별도 이슈로 남긴다.
    """
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("success") is False


async def delete_cart_item(
    cart_item_id: int,
    *,
    user_id: int | None = None,
    guest_id: str | None = None,
    chat_session_id: str | None = None,
) -> None:
    """장바구니 삭제 — I-24(확정 2026-08-05) DELETE /internal/cart/items/{cartItemId}.

    게스트 허용. 신원 query 는 userId 또는 guestId 정확히 하나만 싣는다 — 호출부(cart 그래프)가
    이미 신원을 확인하지만 어댑터도 방어한다. 둘 다 None/둘 다 not None 이면 호출 자체를 하지
    않고 CartError 로 낙성한다(ValueError 가 아니라 이 계열 오류 예외로 — 호출부가 typed 예외
    하나만 캐치하면 되게 하기 위해).
    삭제는 재고·상품 상태를 보지 않는다 — HIDDEN·품절도 성공한다(PRODUCT_NOT_FOUND 없음).
    복수 삭제는 항목별 반복 호출(bulk 없음, 호출부 책임).
    실패 매핑: 404 이고 `error.code == "CART_ITEM_NOT_FOUND"` 일 때만 → CartItemNotFound(비멱등,
    두 번째 호출도 404). **[라운드 23]** code 가 다르거나 본문을 못 읽는 404(엔드포인트 미배포로
    나는 라우트 없음 404 포함)는 CartItemNotFound 가 아니라 CartError 다 — 그렇지 않으면 상위가
    "이미 빠져 있어요"(성공 안내)로 오인해 거짓 성공을 낸다.
    chat_session_id는 선택 query이며, 있으면 Spring이 `chat:{sessionId}` sentinel로 챗봇 삭제
    이벤트를 server-side 적재한다.
    400(path 숫자 아님·신원 query 오류)·403 AUTH_FORBIDDEN(소유자 불일치, 전용 예외 없음)·500·
    도달 불가·미상 코드 → CartError(기존 담기와 같은 낙성처).
    """
    if (user_id is None) == (guest_id is None):
        # [확정 2026-08-05] 신원 query 0개/2개일 때 BE 응답 code 는 자원별 신규 code 를 신설하지
        # 않고 기존 VALIDATION_ERROR 를 재사용한다 — 어느 쪽이든 이 어댑터는 CartError 로 수렴시킨다.
        raise CartError("delete_cart_item 신원 query 는 정확히 하나여야 함")

    params: dict[str, object] = {}
    if user_id is not None:
        params["userId"] = user_id
    if guest_id is not None:
        params["guestId"] = guest_id
    if chat_session_id is not None:
        params["chatSessionId"] = chat_session_id

    try:
        with _spring_span("delete_cart_item", "DELETE") as span:
            async with _client() as client:
                resp = await client.delete(f"/internal/cart/items/{cart_item_id}", params=params)
                _record_spring_status(span, resp)
    except httpx.HTTPError as exc:
        raise CartError(f"delete_cart_item 도달 실패: {exc}") from exc

    if resp.status_code == 200:
        if _envelope_success_false(resp):
            # [#285] BE `ApiResponse`(`global/response/ApiResponse.java`)는 success:false 를
            # error{code,...} 와 함께만 만들고 GlobalExceptionHandler 가 전부 상태 코드로 낸다
            # — 200 + success:false 경로 자체가 없다. 계약상 오지 않는 조합이라 방어적으로
            # 실패 처리한다(fail-closed). 이 방어 분기는 지우지 않는다.
            raise CartError("delete_cart_item 실패: 200 success=false")
        return
    if resp.status_code == 404:
        code = _parse_error_code(resp)
        if code == "CART_ITEM_NOT_FOUND":
            raise CartItemNotFound()
        # [라운드 23] Spring 에 엔드포인트가 아직 없어서 나는 404(라우트 없음, code 없음/다름)를
        # CartItemNotFound 로 낙성하면 상위가 "이미 빠져 있어요"(성공 안내)로 끝낸다 — 배포 전
        # 호출이 조용히 거짓 성공을 낸다. code 가 계약과 정확히 일치할 때만 비멱등 낙성이다.
        raise CartError(f"delete_cart_item 실패: 404 {code}")
    code = _parse_error_code(resp)
    raise CartError(f"delete_cart_item 실패: {resp.status_code} {code}")


async def change_cart_quantity(
    cart_item_id: int, quantity: int, *, user_id: int | None = None, guest_id: str | None = None
) -> ChangeCartQuantityResult:
    """장바구니 수량 변경 — I-25(확정 2026-08-05, §4.13) PATCH /internal/cart/items/{cartItemId}.

    `delete_cart_item`(I-24)과 대칭 시그니처 — 신원 query 는 userId 또는 guestId 정확히 하나만
    싣는다(방어는 동일 규약). **I-24 와의 차이**: 삭제는 재고·상품 상태를 안 보지만 **수량
    변경은 재고를 본다**(치환값 > 재고면 400 CART_STOCK_INSUFFICIENT). 그리고 **합산이 아니라
    치환**이라 "하나 더 담아줘"류는 이 계약이 아니라 I-2(§4.1) 재호출이다 — 어댑터가 그 구분을
    하지 않으니 호출부가 올바른 함수를 골라야 한다.

    실패 매핑(§4.13): 400 이고 `error.code == "CART_STOCK_INSUFFICIENT"` 면 →
    CartStockInsufficient(available_stock)(I-2 담기와 같은 예외·같은 파서 `_parse_cart_error`
    재사용). 404 이고 code 가 정확히 `CART_ITEM_NOT_FOUND` 일 때만 → CartItemNotFound(I-24 와
    같은 예외). **[라운드 23 규약 계승]** code 가 다르거나 본문을 못 읽는 400/404(엔드포인트
    미배포 포함)는 typed 예외가 아니라 CartError 다 — status 만 보고 낙성하면 라우트 부재
    404 를 "그 항목이 없다"로, 임의의 400 을 "재고 부족"으로 오인한다.
    400 VALIDATION_ERROR(신원 query 이상·quantity 범위 밖)·403 AUTH_FORBIDDEN(소유자 불일치,
    전용 예외 없음 — BE `CartService#findOwnedItem` 실측, I-24 와 동일 규약)·500·도달 불가·
    미상 코드 → CartError(형제 어댑터와 같은 낙성처).
    """
    if (user_id is None) == (guest_id is None):
        # [확정 2026-08-05] delete_cart_item 과 같은 방어 — 신원 query 는 정확히 하나.
        raise CartError("change_cart_quantity 신원 query 는 정확히 하나여야 함")

    params: dict[str, object] = {}
    if user_id is not None:
        params["userId"] = user_id
    if guest_id is not None:
        params["guestId"] = guest_id
    try:
        # 스키마(`ChangeCartQuantityRequest.quantity: Field(ge=1, le=99)`)가 범위를 강제한다 —
        # `decompose` 가 이미 클램프하지만 이 어댑터에 들어오는 값이 그 경로만은 아니라서다.
        # 호출부가 typed 예외 하나만 캐치하면 되도록 `ValidationError` 를 그대로 새게 두지
        # 않는다(delete_cart_item 의 신원 XOR 방어와 같은 이유).
        body = ChangeCartQuantityRequest(quantity=quantity)
    except ValidationError as exc:
        raise CartError(f"change_cart_quantity quantity 범위 밖: {exc}") from exc

    try:
        with _spring_span("change_cart_quantity", "PATCH") as span:
            async with _client() as client:
                resp = await client.patch(
                    f"/internal/cart/items/{cart_item_id}",
                    params=params,
                    json=body.model_dump(by_alias=True),
                )
                _record_spring_status(span, resp)
    except httpx.HTTPError as exc:
        raise CartError(f"change_cart_quantity 도달 실패: {exc}") from exc

    if resp.status_code == 200:
        if _envelope_success_false(resp):
            # [#285] 형제 어댑터(delete_cart_item 등)와 같은 fail-closed 근거 — BE `ApiResponse`
            # 는 success:false 를 error{code,...} 와 함께만 만들고 GlobalExceptionHandler 가
            # 전부 상태 코드로 낸다. 200 + success:false 경로 자체가 없다. 계약상 오지 않는
            # 조합이라 방어적으로 실패 처리한다(fail-closed). 이 방어 분기는 지우지 않는다.
            raise CartError("change_cart_quantity 실패: 200 success=false")
        try:
            data = resp.json()
        except ValueError as exc:
            raise CartError(f"change_cart_quantity 응답 파싱 실패: {exc}") from exc
        payload = data.get("data") if isinstance(data, dict) else None
        try:
            return ChangeCartQuantityResult(
                success=True,
                cart_item_id=(payload or {}).get("cartItemId"),
                quantity=(payload or {}).get("quantity"),
            )
        except ValidationError as exc:
            raise CartError(f"change_cart_quantity 응답 스키마 이상: {exc}") from exc
    if resp.status_code == 400:
        code, _options, available_stock = _parse_cart_error(resp)
        if code == "CART_STOCK_INSUFFICIENT":
            raise CartStockInsufficient(available_stock)
        # VALIDATION_ERROR(신원 query 이상·quantity 범위 밖) 및 그 밖의 code 는 CartError.
        raise CartError(f"change_cart_quantity 실패: 400 {code}")
    if resp.status_code == 404:
        code = _parse_error_code(resp)
        if code == "CART_ITEM_NOT_FOUND":
            raise CartItemNotFound()
        # [라운드 23 규약 계승] 엔드포인트 미배포로 오는 다른/없는 code 의 404 를 "그 항목이
        # 없다"로 오인하면 상위가 거짓 성공/거짓 실패 안내를 낸다 — code 정확 일치일 때만 낙성.
        raise CartError(f"change_cart_quantity 실패: 404 {code}")
    code = _parse_error_code(resp)
    raise CartError(f"change_cart_quantity 실패: {resp.status_code} {code}")


async def add_wishlist(request: AddWishlistRequest) -> WishlistAddResult:
    """찜 추가 — I-26(확정 2026-08-05) POST /internal/wishlist.

    회원 전용(USER). 게스트 찜은 없다 — 호출부가 게스트를 걸러 이 함수 자체를 부르지 않는다
    (internal 호출 없이 degrade, 이 어댑터는 그 판단을 하지 않는다).
    성공 200 {success, data:{productId}}(wishlistId 없음).
    실패: 404 이고 code 가 정확히 PRODUCT_NOT_FOUND(없는 상품만 — HIDDEN·품절은 찜 가능)일 때만
    → WishlistProductNotFound. 409 이고 code 가 정확히 WISHLIST_DUPLICATE·RESOURCE_CONFLICT
    (UNIQUE 경합, 둘 다 동일 취급) 중 하나일 때만 → WishlistDuplicate. **[라운드 23]** code 가
    다르거나 본문을 못 읽는 404/409(엔드포인트 미배포 포함)는 WishlistError 다.
    400·500·도달 불가·미상 코드 → WishlistError. [#285] 찜 API 에는 403 이 없다(§4.14~4.16 +
    BE `InternalWishlistController` 실측 — 역할 검사 부재). 계약상 오지 않지만 오더라도 위
    "미상 코드" 분기로 `WishlistError` 에 수렴한다.
    """
    try:
        with _spring_span("add_wishlist", "POST") as span:
            async with _client() as client:
                resp = await client.post(
                    "/internal/wishlist", json=request.model_dump(by_alias=True)
                )
                _record_spring_status(span, resp)
    except httpx.HTTPError as exc:
        raise WishlistError(f"add_wishlist 도달 실패: {exc}") from exc

    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError as exc:
            raise WishlistError(f"add_wishlist 응답 파싱 실패: {exc}") from exc
        if isinstance(data, dict) and data.get("success") is False:
            # [#285] BE `ApiResponse` 는 success:false 를 error{code,...} 와 함께만 만들고
            # GlobalExceptionHandler 가 전부 상태 코드로 낸다 — 200 + success:false 경로 자체가
            # 없다. 계약상 오지 않는 조합이라 방어적으로 실패 처리한다(fail-closed).
            raise WishlistError("add_wishlist 실패: 200 success=false")
        payload = data.get("data") if isinstance(data, dict) else None
        product_id = payload.get("productId") if isinstance(payload, dict) else None
        try:
            return WishlistAddResult(success=True, product_id=product_id)
        except ValidationError as exc:
            # productId 가 dict·문자열 등 변환 불가 값이면(스키마 이상) 다른 어댑터와 같은 규약으로
            # WishlistError 로 낙성한다(라운드 15 — get_wishlist 는 이미 이렇게 하는데 이 함수만
            # 빠져 있었다. ValidationError 가 그대로 새면 stream_wishlist_add 의 `except
            # WishlistError` 에 안 걸려 우아한 degrade 가 무너지고 상위 스트림의 범용 오류로 샌다).
            raise WishlistError(f"add_wishlist 응답 스키마 이상: {exc}") from exc

    if resp.status_code == 404:
        code = _parse_error_code(resp)
        if code == "PRODUCT_NOT_FOUND":
            raise WishlistProductNotFound()
        # [라운드 23] 엔드포인트 미배포로 오는 빈/다른 code 의 404 를 "없는 상품"으로 오인하면
        # 상위가 그 조건에 맞는 안내로 잘못 종료한다 — code 가 계약과 일치할 때만 typed 예외다.
        raise WishlistError(f"add_wishlist 실패: 404 {code}")
    if resp.status_code == 409:
        code = _parse_error_code(resp)
        if code in ("WISHLIST_DUPLICATE", "RESOURCE_CONFLICT"):
            # WISHLIST_DUPLICATE·RESOURCE_CONFLICT(UNIQUE 경합) 둘 다 "이미 찜함"으로 동일 처리.
            raise WishlistDuplicate()
        raise WishlistError(f"add_wishlist 실패: 409 {code}")
    code = _parse_error_code(resp)
    raise WishlistError(f"add_wishlist 실패: {resp.status_code} {code}")


async def remove_wishlist(product_id: int, *, user_id: int) -> None:
    """찜 해제 — I-27(확정 2026-08-05) DELETE /internal/wishlist/{productId}.

    회원 전용(USER). path 는 productId(wishlistId 아님), query 는 userId 하나뿐(guestId 없음).
    실패: 404 이고 code 가 정확히 WISHLIST_NOT_FOUND(찜 안 한 상품 = 이미 해제 = 없는 상품도
    동일 코드, 구별 불가)일 때만 → WishlistNotFound(비멱등). **[라운드 23]** code 가 다르거나
    본문을 못 읽는 404(엔드포인트 미배포 포함)는 WishlistError 다. 400·500·도달 불가·미상 코드
    → WishlistError. [#285] 찜 API 에는 403 이 없다(§4.14~4.16 + BE 역할 검사 부재). 계약상
    오지 않지만 오더라도 위 "미상 코드" 분기로 `WishlistError` 에 수렴한다.
    """
    try:
        with _spring_span("remove_wishlist", "DELETE") as span:
            async with _client() as client:
                resp = await client.delete(
                    f"/internal/wishlist/{product_id}", params={"userId": user_id}
                )
                _record_spring_status(span, resp)
    except httpx.HTTPError as exc:
        raise WishlistError(f"remove_wishlist 도달 실패: {exc}") from exc

    if resp.status_code == 200:
        if _envelope_success_false(resp):
            # [#285] BE `ApiResponse` 는 success:false 를 error{code,...} 와 함께만 만들고
            # GlobalExceptionHandler 가 전부 상태 코드로 낸다 — 200 + success:false 경로 자체가
            # 없다. 계약상 오지 않는 조합이라 방어적으로 실패 처리한다(fail-closed).
            raise WishlistError("remove_wishlist 실패: 200 success=false")
        return
    if resp.status_code == 404:
        code = _parse_error_code(resp)
        if code == "WISHLIST_NOT_FOUND":
            raise WishlistNotFound()
        # [라운드 23] 엔드포인트 미배포로 오는 빈/다른 code 의 404 를 "이미 해제됨"으로 오인하면
        # 상위가 거짓 성공 안내로 끝낸다 — code 가 계약과 일치할 때만 비멱등 낙성이다.
        raise WishlistError(f"remove_wishlist 실패: 404 {code}")
    code = _parse_error_code(resp)
    raise WishlistError(f"remove_wishlist 실패: {resp.status_code} {code}")


def _parse_wishlist_items(raw: list) -> list[WishlistItem]:
    """I-28 응답 `items` 를 **항목 단위로** 파싱한다.

    배열 전체를 `WishlistView.model_validate({"items": raw})` 로 한 번에 검증하면, BE 가
    `purchaseState` enum 에 새 값 하나만 추가해도 그 값을 가진 항목 1건 때문에 `ValidationError`
    가 배열 전체를 죽여 `get_wishlist` 가 `SpringUnavailableError` 로 낙성하고, 그 사용자의 찜
    해제(#116·#117)가 통째로 막힌다 — 한 항목의 검증 실패가 전체를 죽이는 구조 자체가 문제다.

    이 저장소의 기존 관행을 따른다 — `_parse_cart_error` 가 "형식 이상 옵션은 건너뜀 — 되물음
    흐름 전체가 죽지 않게" 로 옵션 배열을 항목 단위로 방어하는 것과 같은 처리다. 검증에 실패한
    항목만 건너뛰고 나머지는 살리며, 건너뛴 수는 BE 계약 드리프트 관측을 위해 warning 으로
    남긴다(조용히 사라지면 드리프트를 알 방법이 없다).

    **[#310] 미수신 기본값은 `None`(모름)으로 바뀌었다** — 종전 `"AVAILABLE"` 은 소비자가 없던
    시절의 값이고, 이제 이 값을 읽어 안내 문구를 가르므로 기본값이 주장으로 승격된다.
    **장바구니(`CartViewItem`)는 이 함수와 처방이 다르다** — 거기선 미지 값이 와도 항목을
    skip 하지 않고 필드만 `None` 으로 강등한다(`app/schemas/spring.py::_degrade_unknown_purchase_state`).
    찜은 항목이 목록에서 빠져도 파괴적 후속 동작이 없지만, 장바구니 항목은 "전부 빼줘"의
    입력이라 사라지면 일부만 지우고 성공을 보고하게 된다. 원칙("거짓 안내를 막는다")은 같고
    최적 수단만 다르다.

    응답 구조 자체가 깨진 경우(`items` 가 list 가 아님)는 이 함수가 보지 않는다 — 호출부가 그
    형태 붕괴는 종전대로 fail-closed(`SpringUnavailableError`)로 처리한다. **원본이 비어 있지
    않은데 이 함수의 반환이 빈 리스트인 경우(전 항목 스키마 붕괴)도 이 함수는 판단하지 않는다**
    — "항목 하나의 이상"과 "계약 전면 드리프트"는 다른 문제라 그 구분은 호출부(`get_wishlist`)
    책임이다. 여기서 다루는 건 순수하게 "배열은 정상인데 그 안의 항목 하나가 이상한" 경우뿐이다.
    """
    parsed: list[WishlistItem] = []
    invalid = 0
    for it in raw:
        if not isinstance(it, dict):
            invalid += 1
            _log.warning("찜 목록 항목이 object 아님(skip) — type=%s", type(it).__name__)
            continue
        try:
            parsed.append(WishlistItem.model_validate(it))
        except ValidationError as exc:
            # 항목 1건의 스키마 이상(예: 미지의 purchaseState 값)은 그 항목만 skip — 전체 실패로
            # 번지지 않게. BE 계약 드리프트 관측을 위해 남긴다.
            invalid += 1
            _log.warning("찜 목록 항목 파싱 실패로 skip(전체 실패 아님) — %s", exc)
    if invalid:
        _log.warning("찜 목록 항목 %d건 skip(스키마 이상) / 정상 %d건", invalid, len(parsed))
    return parsed


async def get_wishlist(user_id: int) -> WishlistView:
    """찜 목록 조회 — I-28(확정 2026-08-05) GET /internal/wishlist?userId=.

    페이징 없음, MVP 전량 반환. 찜 0건도 200 + items:[](404 아님). get_cart 와 같은 degrade
    규약 — 도달 불가/오류/응답 최상위 구조 불일치는 SpringUnavailableError. 항목 단위 스키마
    이상(BE `purchaseState` enum 드리프트 등)은 `_parse_wishlist_items` 가 그 항목만 skip
    한다 — 한 항목이 찜 목록 전체를, 나아가 찜 해제 기능 전체를 죽이지 않는다.

    **원본 `items` 가 비어 있지 않은데 파싱 결과가 0건이면 SpringUnavailableError 로
    fail-closed 한다** — "항목 하나의 이상"이 아니라 BE 가 값 체계를 통째로 바꾼 계약 전면
    드리프트로 본다. 이 구분이 없으면 찜을 여러 개 해 둔 사용자가 파싱 실패로 빈 목록을
    받고 "찜한 상품이 없어요."(`app/agents/buyer/cart/wishlist.py` 의 빈 목록 분기)라는
    안내를 듣는다 — 실제로는 데이터가 있는데 없다고 확언하는 것이라, 조회 실패를 정직하게
    알리는 쪽(`SpringUnavailableError` → "찜 목록을 확인하지 못했어요")보다 나쁘다. 원본이
    애초에 빈 배열(`items: []`, 찜 0건)인 경우는 이 판정 대상이 아니다 — 그건 I-28 의 정상
    응답이라 그대로 빈 `WishlistView` 를 돌려준다.
    """
    # [#285] `GET /internal/wishlist` 신설 자체는 수용됐다(§4.16 "페이징 없음 — MVP 전량
    # 반환"으로 등재). 🔶 이슈 #285 코멘트 Q11 이 물었던 "전량 반환 응답의 크기 상한"은 아직
    # 답을 받지 못했다 — 진짜로 열려 있다. 상한이 없으므로 클라 측 절단은 두지 않는다.
    try:
        with _spring_span("get_wishlist", "GET") as span:
            async with _client() as client:
                resp = await client.get("/internal/wishlist", params={"userId": user_id})
                _record_spring_status(span, resp)
                resp.raise_for_status()
                data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            # 200 + success:false 를 "찜 0건"으로 위장시키지 않는다 — 이 ValueError 는 아래
            # except 가 잡아 SpringUnavailableError 로 낙성한다(get_cart 와 같은 degrade 규약).
            raise ValueError("get_wishlist 응답 success=false")
        payload = data.get("data") if isinstance(data, dict) else None
        items = payload.get("items") if isinstance(payload, dict) else None
        if items is not None and not isinstance(items, list):
            # items 자체가 list 가 아니다 — 항목 단위로 볼 수조차 없는 최상위 envelope drift 라
            # 종전대로 fail-closed 한다(§7 과 같은 구분 — 개별 항목 이상과는 다른 문제).
            raise ValueError(f"get_wishlist items 형태 미인식: {type(items).__name__}")
        raw_items = items or []
        parsed = _parse_wishlist_items(raw_items)
        if raw_items and not parsed:
            # 원본은 비어 있지 않은데 파싱 결과가 0건 — 개별 항목 이상이 아니라 계약 전면
            # 드리프트다(위 docstring 참조). 빈 목록으로 위장하면 실제 데이터를 가진 사용자에게
            # "찜한 상품이 없어요."라는 확신에 찬 거짓 안내가 나간다.
            raise ValueError(
                f"get_wishlist 원본 {len(raw_items)}건이 전부 파싱 실패(계약 드리프트 의심)"
            )
        return WishlistView(items=parsed)
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        raise SpringUnavailableError(f"get_wishlist 실패: {exc}") from exc


async def push_recommendations(push: RecommendationPush) -> bool:
    """최종 랭크 id 를 Spring 에 push — I-21 (경로 B, api-spec §4.2 / C-9). [배선 완료]

    POST {spring_base_url}/internal/recommendations 로 {sessionId, recommendationRequestId,
    listType, totalBudget?, lists[{listId, label?, productIds[숫자], reasons}]} 를 보낸다
    (표시 필드 없음 — Spring enrich·CH-5 조회, §4.3). listId 는 FastAPI 가 생성.
    [v0.17.1, 이슈 #209] 최상위는 lists 배열이며 구 평평 3필드는 보내지 않는다.
    미지정 선택 필드(totalBudget·label)는 exclude_none 으로 생략한다 — 계약상 "…일 때만" 싣는
    필드라 null 을 실어 보내지 않는다. reasons 는 빈 배열로 남는다(선택이되 존재하는 필드).
    콜백 성공 시에만 상위가 SSE products.ready 를 emit 한다 — 실패 시 미emit·done 종료(§3.3).
    도달 불가/오류 응답은 SpringUnavailableError 로 전파(상위가 products.ready 스킵).
    """
    try:
        with _spring_span("push_recommendations", "POST") as span:
            async with _client() as client:
                resp = await client.post(
                    "/internal/recommendations",
                    json=push.model_dump(by_alias=True, exclude_none=True),
                )
                _record_spring_status(span, resp)
                resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise SpringUnavailableError(f"push_recommendations 실패: {exc}") from exc
    return True


async def fetch_product_changes(cursor: str | None, limit: int = 500) -> ProductChangesPage:
    """상품 변경분 pull — I-17, AI 생성물 갱신 배치 (api-spec §4.8 / C-4). [배선 완료]

    GET {spring_base_url}/internal/products/changes?since={cursor}&limit={limit} + X-Internal-Token.
    응답 공통 envelope {success, data:{items, nextCursor, hasMore}}(BE 2026-07-18 확정). 도달 불가/오류/
    스키마 불일치는 SpringUnavailableError. INVALID_CURSOR만 전용 예외로 분류해 배치가
    since="0" 전체 재구축으로 복구하고, 나머지는 커서 미전진 상태로 다음 주기에 재개한다(§4.8).
    """
    params: dict[str, object] = {"since": cursor or "0", "limit": limit}
    try:
        with _spring_span("fetch_product_changes", "GET") as span:
            async with _client() as client:
                resp = await client.get("/internal/products/changes", params=params)
                _record_spring_status(span, resp)
                if resp.status_code == 400:
                    try:
                        error_body = resp.json()
                    except ValueError:
                        error_body = None
                    if isinstance(error_body, dict):
                        error = error_body.get("error")
                        code = (
                            error.get("code") if isinstance(error, dict) else error_body.get("code")
                        )
                        if code == "INVALID_CURSOR":
                            raise InvalidCursorError("fetch_product_changes: INVALID_CURSOR")
                resp.raise_for_status()
                data = resp.json()
        # 200 이어도 success=false / data=null 은 실패 envelope — 빈 페이지로 오인해 배치가
        # 조기 종료(정합성 손상)되지 않게 명시 검증한다(리뷰 반영).
        if (
            not isinstance(data, dict)
            or data.get("success") is not True
            or not isinstance(data.get("data"), dict)
        ):
            raise SpringUnavailableError(
                f"fetch_product_changes 비정상 envelope: {repr(data)[:200]}"
            )
        return ProductChangesPage.model_validate(data["data"])
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        raise SpringUnavailableError(f"fetch_product_changes 실패: {exc}") from exc


class SpringClient:
    """판매자 internal API 콜백 클라이언트 (api-spec §4.4/§4.5, DESIGN-SELLER-TOOLS-STAGE1 §2).

    X-Internal-Token 서비스 토큰 + `{brandId}` path + 3s 타임아웃(§2.3 v0.13.0·§2.9 c).
    brand_id 는 메서드 인자로 명시 전달한다 — 호출자(app/agents/seller/tools.py 클로저)가
    검증된 Identity.brand_id 를 넣는다. 이 클래스는 신원을 스스로 판단하지 않는다(IDOR 방지).

    조회 실패는 SpringUnavailableError 로 변환해 raise 한다(타임아웃·4xx/5xx·연결 실패 공통).
    "Error: ..." 문자열 degrade 로의 변환은 상위 도구 계층(app/agents/seller/tools.py)의
    책임이다 — 이 클래스는 관심사 분리를 위해 예외만 던진다.

    테스트는 transport 인자로 httpx.MockTransport 를 주입한다(respx 미설치, §0).
    """

    def __init__(
        self,
        base_url: str,
        internal_token: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        self._base_url = base_url
        self._internal_token = internal_token
        self._transport = transport
        # timeout 미지정 시 Settings 기본값 사용 — 하드코딩 금지(§5), AI→Spring 전 구간 3s.
        self._timeout = timeout if timeout is not None else get_settings().spring_timeout_s

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: _SpringOperation,
        params: dict | None = None,
        json_body: dict | None = None,
        error_code_map: dict[str, type[Exception]] | None = None,
    ) -> dict:
        """공용 요청 헬퍼. X-Internal-Token 헤더 부착 + raise_for_status +
        예외를 SpringUnavailableError 로 통일 변환한다.

        error_code_map 이 주어지면 4xx/5xx 응답 본문의 `error.code`(§2.5 봉투)를
        `_parse_error_code` 로 뽑아, 매핑된 코드는 전용 예외로 낸다(I-30 등 코드 구분이
        계약인 쓰기용) — 매핑에 없는 코드·본문 없는 오류는 상태코드로 갈린다(#620):
        4xx 는 `SpringRejected`(영구 거부, 재시도 무의미), 5xx·그 외는 종전대로
        `SpringUnavailableError`(진짜 일시 장애) — `SpringRejected` 가 그 하위라 기존
        `except SpringUnavailableError` 호출부는 손대지 않아도 그대로 잡힌다."""
        headers = {"X-Internal-Token": self._internal_token} if self._internal_token else {}
        try:
            with _spring_span(operation, method) as span:
                async with httpx.AsyncClient(
                    base_url=self._base_url, transport=self._transport, timeout=self._timeout
                ) as client:
                    response = await client.request(
                        method, path, params=params, json=json_body, headers=headers
                    )
                    _record_spring_status(span, response)
                    response.raise_for_status()
                    payload = response.json()
                    # [변경 2026-07-19, REALIGN ②-3] BE 확정 명세(I-13 실측)가
                    # {success, data:{...}} 봉투를 쓴다 — data 만 벗겨 모델에 넘긴다.
                    # 봉투 없는 응답(과도기·타 API)은 그대로 통과(하위 호환).
                    if (
                        isinstance(payload, dict)
                        and "success" in payload
                        and isinstance(payload.get("data"), dict)
                    ):
                        return payload["data"]
                    return payload
        except httpx.TimeoutException as exc:
            raise SpringUnavailableError(
                f"Spring 콜백 타임아웃({self._timeout}s): {method} {path}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            if error_code_map:
                code = _parse_error_code(exc.response)
                mapped = error_code_map.get(code) if code else None
                if mapped is not None:
                    raise mapped(f"{code}: {method} {path}") from exc
            status = exc.response.status_code
            exc_cls = SpringRejected if 400 <= status < 500 else SpringUnavailableError
            raise exc_cls(f"Spring 콜백 오류 응답({status}): {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise SpringUnavailableError(f"Spring 콜백 실패: {method} {path} ({exc})") from exc

    def _validate(self, model: type[_ModelT], data: dict) -> _ModelT:
        """Spring 응답을 스키마로 검증하는 단일 지점(opus 리뷰 M1).

        판매자 스키마는 전부 초안(🔴 C-13/C-14)이라 실측 필드 불일치 가능성이 높다.
        pydantic.ValidationError 를 여기서 한 번에 SpringUnavailableError 로 변환해,
        도구 계층(app/agents/seller/tools.py)이 `except SpringUnavailableError` 하나만으로
        degrade 문자열("Error: ...")을 반환할 수 있게 한다(개별 도구에서 ValidationError 를
        따로 잡을 필요가 없다).
        """
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise SpringUnavailableError(
                f"Spring 응답 형식이 예상과 다릅니다({model.__name__}): {exc}"
            ) from exc

    # ── 조회 8종 (전부 GET, api-spec §4.4/§4.5) ──

    async def get_sales(
        self,
        brand_id: int,
        from_: str,
        to: str,
        granularity: str = "daily",
        *,
        retries: int = 0,
    ) -> SalesResult:
        """I-6 매출 시계열 조회 (§4.4). granularity: daily/weekly/monthly/summary.

        [#489] series[].salesCount(판매 **수량**, SUM(oi.quantity) · CANCELLED/
        RETURNED 제외)를 수신한다 — 구현에는 처음부터 있었으나 스키마에 필드가
        없어 파싱되지 않고 버려지던 드리프트 정정(2026-08-06). granularity=summary
        응답에는 수량 필드가 없어 nullable 이다.

        `retries`: 무인 배치 전용(#601, get_customer_features 와 동일 규약) — 대화형
        호출은 기본값 0(재시도 없음)을 그대로 쓴다.
        """

        async def _call() -> SalesResult:
            data = await self._request(
                "GET",
                f"/internal/seller/{brand_id}/sales",
                operation="get_sales",
                params={"from": from_, "to": to, "granularity": granularity},
            )
            return self._validate(SalesResult, data)

        return await _fetch_with_batch_retries(
            _call, operation="get_sales", brand_id=brand_id, retries=retries
        )

    async def get_funnel(
        self, brand_id: int, from_: str, to: str, *, retries: int = 0
    ) -> FunnelResult:
        """I-7 구매전환 퍼널 조회 (§4.4). view→cart→checkout→purchase 4단.

        `retries`: 무인 배치 전용(#601) — 대화형 호출은 기본값 0.
        """

        async def _call() -> FunnelResult:
            data = await self._request(
                "GET",
                f"/internal/seller/{brand_id}/funnel",
                operation="get_funnel",
                params={"from": from_, "to": to},
            )
            return self._validate(FunnelResult, data)

        return await _fetch_with_batch_retries(
            _call, operation="get_funnel", brand_id=brand_id, retries=retries
        )

    async def get_events(
        self,
        brand_id: int,
        from_: str,
        to: str,
        event_type: list[str] | None = None,
        product_id: int | None = None,
        group_by: str | None = None,
        *,
        retries: int = 0,
    ) -> BehaviorEventsResult:
        """I-13 행동 이벤트 집계 조회 (§4.4 — 07/17 BE 확정, REALIGN ②-3).

        eventType 은 상품 연계 **5종** 복수 선택(미지정 = 5종 전체) — product_view /
        add_to_cart / **remove_from_cart** / checkout_start / purchase_complete.
        [2026-08-06 개정, #489] remove_from_cart 편입으로 4종 → 5종이며, 5종 밖의
        값은 400 INVALID_GROUP_BY 다(session_start/login/search/page_view 는 상품
        귀속 경로가 없어 이 엔드포인트 스코프 밖).
        groupBy 는 product(기본)/eventType/date. 실패 코드:
        INVALID_PERIOD/INVALID_GROUP_BY(400).

        `retries`: 무인 배치 전용(#601) — 대화형 호출은 기본값 0.
        """
        params: dict = {"from": from_, "to": to}
        if event_type:
            # [#196] CSV 명시 직렬화 — 구 반복 쿼리(eventType=a&eventType=b)는
            # Spring 의 암묵 변환(반복 파라미터→콤마 문자열)에 의존했다. BE 계약은
            # String eventType + comma split(api-spec §4.4 I-13) — CSV 로 명시한다.
            params["eventType"] = ",".join(event_type)
        if product_id is not None:
            params["productId"] = product_id
        if group_by:
            params["groupBy"] = group_by

        async def _call() -> BehaviorEventsResult:
            data = await self._request(
                "GET",
                f"/internal/seller/{brand_id}/events",
                operation="get_events",
                params=params,
            )
            return self._validate(BehaviorEventsResult, data)

        return await _fetch_with_batch_retries(
            _call, operation="get_events", brand_id=brand_id, retries=retries
        )

    async def get_order_events(
        self,
        brand_id: int,
        from_: str,
        to: str,
        to_status: list[str] | None = None,
        actor_type: str | None = None,
        group_by: str | None = None,
        stats: bool | None = None,
        *,
        retries: int = 0,
    ) -> OrderEventsResult:
        """I-14 주문 상태 전이/조회 (§4.4). toStatus 는 복수 허용, stats 는 집계 모드 플래그.

        [혼동 금지] 구매자 get_recent_purchases(GET /orders/recent, §4.7)와 다른 계약이다.

        `retries`: 무인 배치 전용(#601) — 대화형 호출은 기본값 0.
        """
        params: dict = {"from": from_, "to": to}
        if to_status:
            params["toStatus"] = to_status
        if actor_type:
            params["actorType"] = actor_type
        if group_by:
            params["groupBy"] = group_by
        if stats is not None:
            params["stats"] = stats

        async def _call() -> OrderEventsResult:
            data = await self._request(
                "GET",
                f"/internal/seller/{brand_id}/order-events",
                operation="get_order_events",
                params=params,
            )
            return self._validate(OrderEventsResult, data)

        return await _fetch_with_batch_retries(
            _call, operation="get_order_events", brand_id=brand_id, retries=retries
        )

    async def get_product_changes(
        self,
        brand_id: int,
        from_: str,
        to: str,
        change_type: str | None = None,
        product_id: int | None = None,
    ) -> ProductChangeLogResult:
        """I-15 상품 변경 이력(판매자 감사 로그) 조회 (§4.4). changeType: PRICE/STOCK/STATUS.

        [혼동 금지] 구매자 fetch_product_changes(I-8, §4.8 AI 생성물 배치)와 다른 계약이다.
        """
        params: dict = {"from": from_, "to": to}
        if change_type:
            params["changeType"] = change_type
        if product_id:
            params["productId"] = product_id
        data = await self._request(
            "GET",
            f"/internal/seller/{brand_id}/product-changes",
            operation="get_product_changes",
            params=params,
        )
        return self._validate(ProductChangeLogResult, data)

    async def get_churn(
        self, brand_id: int, from_: str, to: str, inactive_days: int
    ) -> ChurnResult:
        """I-16 이탈 코호트 조회 (§4.4). 코호트 = from~to 에 자사 상품과 상호작용한
        회원, 이탈 = 최근 inactiveDays 무활동(BE SellerAnalyticsService.churn 실측).

        [#197] from/to 는 필수다 — Spring AnalysisPeriod.of 가 누락·형식 오류·역전을
        400 INVALID_PERIOD 로 거부한다. 종전에는 inactiveDays 만 보내 이 조회가
        무조건 400 → degrade 로 새던 버그(주 소스 전면 불능)의 원인이었다.
        """
        data = await self._request(
            "GET",
            f"/internal/seller/{brand_id}/churn",
            operation="get_churn",
            params={"from": from_, "to": to, "inactiveDays": inactive_days},
        )
        return self._validate(ChurnResult, data)

    async def get_account_events(
        self,
        brand_id: int,
        from_: str,
        to: str,
        event_type: str | None = None,
        group_by: str | None = None,
        *,
        retries: int = 0,
    ) -> AccountEventsResult:
        """I-8 계정 이벤트 집계 조회 (§4.4) — 자사 코호트 스코프.

        [#481, 노션 2026-08-06 개정] 전역 `/internal/account-events` →
        `/internal/seller/{brandId}/account-events` 전환. admin 부재로 판매자
        워커가 자사와 무관한 플랫폼 전체 신호를 소비하던 문제를 자사 코호트
        (I-16 churn 과 동일 조인)로 해소했다 — 전역 구경로는 admin 용으로 BE 존치.
        미존재 brandId 는 404 BRAND_NOT_FOUND(다른 판매자 집계 API 와 동일) —
        전용 매핑 없이 SpringUnavailableError 로 degrade 한다(I-6·I-16 과 동일 취급).

        [#197] from/to 는 필수다(AnalysisPeriod.of — 누락 시 400 INVALID_PERIOD).
        groupBy 는 BE 화이트리스트 eventType(기본)|hour|ip 만 허용 — 그 외 400
        INVALID_GROUP_BY (AccountEventAggregateResponse 실측).

        `retries`: 무인 배치 전용(#601) — 대화형 호출은 기본값 0.
        """
        params: dict = {"from": from_, "to": to}
        if event_type:
            params["eventType"] = event_type
        if group_by:
            params["groupBy"] = group_by

        async def _call() -> AccountEventsResult:
            data = await self._request(
                "GET",
                f"/internal/seller/{brand_id}/account-events",
                operation="get_account_events",
                params=params,
            )
            return self._validate(AccountEventsResult, data)

        return await _fetch_with_batch_retries(
            _call, operation="get_account_events", brand_id=brand_id, retries=retries
        )

    async def get_customer_features(
        self,
        brand_id: int,
        from_: str,
        to: str,
        *,
        retries: int = 0,
    ) -> SellerCustomerFeaturesResult:
        """I-38 고객 행동 피처 집계 조회 (노션 2026-08-10 확정, BE #582 실장 — 이슈 #592).

        `from`/`to`는 필수다(BE `AnalysisPeriod.of` — 누락·형식 오류·역전은 400
        INVALID_PERIOD, 다른 seller GET 과 동일 계약). 코호트가 30명(`MIN_COHORT_SIZE`)
        미만이면 BE가 `insufficientCohort=true` + `rows=[]`로 단락 응답한다 — "고객 없음"이
        아니라 표본 부족으로 인한 판정 보류다(노션 규약). 1,000명(`CUSTOMER_ROW_LIMIT`) 초과는
        활동량 내림차순으로 절단하고 `truncated=true`를 세운다.

        `retries`: 무인 배치 경로 전용(OPS-RUNTIME 결정 R-1) — 대화형 호출은 기본값
        0(재시도 없음, 조회 1회)을 그대로 쓴다. 배치 호출부만 `retries=1`을 넘긴다.
        재시도 판정은 기존 `_is_retryable`(타임아웃·연결 오류·5xx·408·429)을 그대로 쓰되,
        `_request`가 감싸 던진 `SpringUnavailableError.__cause__`(원본 httpx 예외)를
        본다 — `_request` 자체는 손대지 않으므로 다른 판매자 GET 10여 종은 이 변경으로
        영향받지 않는다(무재시도 그대로, #592 범위 밖).
        """
        settings = get_settings()
        attempts = retries + 1
        for attempt in range(1, attempts + 1):
            try:
                data = await self._request(
                    "GET",
                    f"/internal/seller/{brand_id}/customer-features",
                    operation="get_customer_features",
                    params={"from": from_, "to": to},
                )
                return self._validate(SellerCustomerFeaturesResult, data)
            except SpringUnavailableError as exc:
                cause = exc.__cause__ or exc
                if attempt >= attempts or not _is_retryable(cause):
                    raise
                _log.warning(
                    "spring_customer_features_retry",
                    extra={
                        "attempt": attempt,
                        "maxAttempts": attempts,
                        "brandId": brand_id,
                        "statusClass": _failure_status_class(cause),
                    },
                )
                await asyncio.sleep(settings.seller_customer_features_retry_backoff_s * attempt)
        raise SpringUnavailableError(  # pragma: no cover — 루프가 항상 return/raise 함
            f"get_customer_features: retries 소진(brand_id={brand_id})"
        )

    async def list_products(
        self,
        brand_id: int,
        status: str | None = None,
        q: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> SellerProductList:
        """I-9 자사 상품 목록 조회 (§4.5). status: ON_SALE/HIDDEN. draft/product 도구의 before 소스.

        **`DELETED` 는 여기 나오지 않는다** — 판매자에게 보이지 않는 것이 삭제의 정의라
        BE 가 status 미지정(전량)에서도 빼고, `status=DELETED` 를 명시해도 400 이 아니라
        빈 `rows` 다(존재 비노출, §4.5). 삭제한 상품이 목록에 없는 것은 정상이다.
        """
        params: dict = {}
        if status:
            params["status"] = status
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        data = await self._request(
            "GET",
            f"/internal/seller/{brand_id}/products",
            operation="list_products",
            params=params,
        )
        return self._validate(SellerProductList, data)

    # ── 쓰기 3종 (product_agent 전용, HITL 승인 후에만 호출, api-spec §4.5) ──

    async def create_product(self, brand_id: int, payload: ProductCreate) -> ProductCreateResult:
        """I-10 상품 등록 (§4.5). name/price/stockQuantity 필수(price ≤ originalPrice).

        미설정 옵션 필드(originalPrice/category/description/imageUrl 등)는 exclude_none 으로
        본문에서 제외한다(opus 리뷰 m4) — null 전송 대신 필드 자체를 생략한다.

        [#524] 422 `INVALID_STOCK` 은 전용 예외다 — 등록 시점엔 옵션이 없어 `optionId` 는
        항상 null 이므로 여기 오는 건 수량 문제뿐이지만, 코드 구분을 I-11 과 대칭으로 둔다.

        [#541] 400 `PRODUCT_CATEGORY_INVALID`·422 `MISSING_FIELD` 도 전용 예외다. 둘 다
        **재시도로 풀리지 않는** 실패(스냅샷 낡음 / 와이어 형식 불일치)인데 매핑이 없어
        `SpringUnavailableError` 로 낙성돼 판매자에게 "일시적인 오류(재시도 가능)" 로
        보였다 — 등록이 계속 실패하는데 원인은 화면 어디에도 없었다.
        """
        data = await self._request(
            "POST",
            f"/internal/seller/{brand_id}/products",
            operation="create_product",
            json_body=payload.model_dump(by_alias=True, exclude_none=True),
            error_code_map={
                "INVALID_STOCK": InvalidStock,
                "PRODUCT_CATEGORY_INVALID": ProductCategoryInvalid,
                "MISSING_FIELD": ProductFieldMissing,
                "INVALID_PRICE": InvalidPrice,
            },
        )
        return self._validate(ProductCreateResult, data)

    async def update_product(
        self, brand_id: int, product_id: int, patch: ProductUpdate
    ) -> ProductUpdateResult:
        """I-11 상품 수정 (§4.5). 바꿀 필드만 전송 — 재고도 이 API로 통합(별도 재고 API 없음).

        삭제된 상품 수정은 409 `PRODUCT_DELETED` 전용 예외 — 재시도 대상이 아니다(§4.5).
        `status` 로 `DELETED` 를 보내는 것도 BE 가 거부한다(삭제는 I-12 전용 전이).

        [#524] 422 `INVALID_STOCK` 도 전용 예외다 — hitl 이 선차단하므로 정상 경로에서는
        나지 않고, 초안과 실행 사이 옵션 변경 레이스만 여기로 온다(InvalidStock docstring).

        [#620] 422 `INVALID_PRICE` 도 전용 예외다 — hitl 이 row-aware 로 선차단하므로
        정상 경로에서는 나지 않고, draft 표시와 confirm 사이 가격이 바뀐 레이스만
        여기로 온다(InvalidPrice docstring).
        """
        data = await self._request(
            "PATCH",
            f"/internal/seller/{brand_id}/products/{product_id}",
            operation="update_product",
            json_body=patch.model_dump(by_alias=True, exclude_none=True),
            error_code_map={
                "PRODUCT_DELETED": ProductDeletedNotEditable,
                "INVALID_STOCK": InvalidStock,
                "INVALID_PRICE": InvalidPrice,
            },
        )
        return self._validate(ProductUpdateResult, data)

    async def delete_product(self, brand_id: int, product_id: int) -> ProductDeleteResult:
        """I-12 상품 삭제(soft) (§4.5). 물리 삭제 없음 — **status=DELETED** 전환.

        `HIDDEN`(숨김·판매정지)과 다른 상태다 — 숨김은 판매자 목록에 남지만 삭제는
        목록에서도 빠지고 되돌릴 수 없다. `HIDDEN`→`DELETED` 는 정상 전이이며, 이미
        `DELETED` 면 409 `ALREADY_DELETED` 전용 예외(멱등 200 아님).
        """
        data = await self._request(
            "DELETE",
            f"/internal/seller/{brand_id}/products/{product_id}",
            operation="delete_product",
            error_code_map={"ALREADY_DELETED": ProductAlreadyDeleted},
        )
        return self._validate(ProductDeleteResult, data)

    # ── 주문·리뷰 3종 (이슈 #297, I-29~I-31 — api-spec §4.18~§4.20, 🔶 초안 BE 협의 전) ──

    async def get_orders(
        self,
        brand_id: int,
        *,
        status: str | None = None,
        order_id: int | None = None,
        from_: str | None = None,
        to: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> SellerOrderList:
        """I-29 자사 주문 조회 — 현재 상태 스냅샷 (§4.18). S-2 의 internal 판.

        기간(from/to)은 선택 — 생략 시 전체 주문(확정 2026-08-04). status 는 S-2 탭
        어휘(ORDERED/SHIPPING/DELIVERED/CLAIM). orderId 미존재·타사는 404 가 아니라
        200 + 빈 rows(존재 은닉) — 예외 매핑이 필요 없다.
        """
        params: dict = {}
        if status:
            params["status"] = status
        if order_id is not None:
            params["orderId"] = order_id
        if from_:
            params["from"] = from_
        if to:
            params["to"] = to
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        data = await self._request(
            "GET",
            f"/internal/seller/{brand_id}/orders",
            operation="get_orders",
            params=params,
        )
        return self._validate(SellerOrderList, data)

    async def update_order_item_status(
        self, brand_id: int, order_item_id: int, payload: OrderItemStatusUpdate
    ) -> OrderItemStatusResult:
        """I-30 주문 아이템 상태 전이 — 발송 처리 (§4.19). HITL 승인 후에만 호출.

        MVP 허용 전이는 ORDERED→SHIPPING 하나뿐. 실패 코드 구분이 계약이다:
        404 ORDER_ITEM_NOT_FOUND(타사 포함 존재 은닉) / 409 ORDER_ALREADY_SHIPPED
        (멱등 200 금지 — 구 ALREADY_SHIPPED 도 과도기 낙성) / 400
        ORDER_INVALID_TRANSITION(클레임·역전이 포함). 그 외(401·500·타임아웃)는
        SpringUnavailableError — 호출부는 성공 보고 금지 + 재시도 안내(§4.19).
        """
        data = await self._request(
            "PATCH",
            f"/internal/seller/{brand_id}/order-items/{order_item_id}/status",
            operation="update_order_status",
            json_body=payload.model_dump(by_alias=True),
            error_code_map={
                "ORDER_ITEM_NOT_FOUND": OrderItemNotFound,
                "ORDER_ALREADY_SHIPPED": OrderAlreadyShipped,
                "ALREADY_SHIPPED": OrderAlreadyShipped,  # 과도기 — 2026-08-05 개명 전 코드
                "ORDER_INVALID_TRANSITION": OrderInvalidTransition,
            },
        )
        return self._validate(OrderItemStatusResult, data)

    async def get_reviews(
        self,
        brand_id: int,
        *,
        from_: str | None = None,
        to: str | None = None,
        product_id: int | None = None,
        rating: str | None = None,
        sort: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> SellerReviewList:
        """I-31 자사 상품 리뷰 목록 조회 — VISIBLE 만 (§4.20).

        기간 생략 시 서버가 최근 7일 기본 적용(확정 2026-08-04 — 누락은
        INVALID_PERIOD 아님). rating 은 "1,2" 콤마 CSV, sort 는 latest(기본)|ratingAsc
        (낮은 별점부터 — 2026-08-06 개명, 구 `rating` 은 폐기돼 400 VALIDATION_ERROR 다.
        ratingDesc 는 없다). 리뷰 원문은 AI DB에 저장하지 않는다 — 질의 시점 조회(I-19 원칙).
        """
        params: dict = {}
        if from_:
            params["from"] = from_
        if to:
            params["to"] = to
        if product_id is not None:
            params["productId"] = product_id
        if rating:
            params["rating"] = rating
        if sort:
            params["sort"] = sort
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        data = await self._request(
            "GET",
            f"/internal/seller/{brand_id}/reviews",
            operation="get_reviews",
            params=params,
        )
        return self._validate(SellerReviewList, data)

    async def get_review_stats(
        self,
        brand_id: int,
        *,
        from_: str | None = None,
        to: str | None = None,
        product_id: int | None = None,
        rating: str | None = None,
    ) -> SellerReviewStats:
        """I-31 리뷰 집계 조회 — stats=true 모드 (§4.20).

        totalCount/averageRating/distribution(P-3 형태)/byProduct. 0건이면
        averageRating 은 null(I-16 churnRate 규칙과 동일 — 평점 0점 오독 금지).

        [#494] from/to·rating·productId 는 **집계에도 전부 적용**된다(I-31 확정) —
        rating 을 안 실으면 전 별점 합산 순위가 200 으로 조용히 돌아와, 명세가 대표
        사용례로 든 "1–2점이 어느 상품에 몰렸어?"가 '그냥 리뷰가 가장 많은 상품'을
        지목한다. sort/limit/offset 만 미전송이 맞다(집계 모드에는 rows 가 없다).
        """
        params: dict = {"stats": "true"}
        if from_:
            params["from"] = from_
        if to:
            params["to"] = to
        if product_id is not None:
            params["productId"] = product_id
        if rating:
            params["rating"] = rating
        data = await self._request(
            "GET",
            f"/internal/seller/{brand_id}/reviews",
            operation="get_review_stats",
            params=params,
        )
        return self._validate(SellerReviewStats, data)


# ── 싱글턴 (2026-07-18 ToolRuntime 전환 — 신원은 SellerContext, 클라이언트는 앱 소유) ──

_default_client: SpringClient | None = None


def get_spring_client() -> SpringClient:
    """앱 수명주기 공유 SpringClient 를 반환한다(커넥션 풀 재사용).

    도구(app/agents/seller/tools.py)는 이 싱글턴을 쓰고, 신원(brand_id)은
    ToolRuntime[SellerContext] 로 요청마다 주입받는다. 테스트는 set_spring_client()
    로 이중(double)을 끼운다.
    """
    global _default_client
    if _default_client is None:
        # [수정 2026-07-20 rebase 합류] 빈 생성자 호출(잠재 TypeError — 테스트는 전부
        # 주입식이라 미노출) 정정 + 토큰 키를 팀 규약 internal_api_token 으로 통일.
        settings = get_settings()
        _default_client = SpringClient(
            settings.spring_base_url, settings.internal_api_token or None
        )
    return _default_client


def set_spring_client(client: SpringClient | None) -> None:
    """싱글턴 교체(테스트 주입)·해제(None). 앱 종료 훅에서도 사용한다."""
    global _default_client
    _default_client = client
