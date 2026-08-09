"""FastAPI 애플리케이션 엔트리포인트.

CORS 미들웨어(오리진은 설정 주입), MVP 라우터(chat/seller), GET /health 를 구성한다.
FE 가 AI 서버를 다른 오리진에서 직접 호출하므로 CORS 가 앞단으로 이동했다 (api-spec §2.7 / C-11).

[변경 2026-07-15] MVP 표면은 /chat, /seller/chat, /profile/me,
  /events/session-end, /health 로 확정. catalog/order 이벤트는 영구 미채택.
  - [완료] §2.9 스트림 수명주기(app/core/stream.py)·§2.8 레이트 리밋(app/core/ratelimit.py)·§2.5 오류 봉투(app/core/errors.py).

[추가 2026-07-20/23, 이슈 #31/#79] lifespan 에서 I-17 증분 pull과 프로필 inactivity
스케줄러(app/pipelines/scheduler.py)를
기동/종료한다 — TestClient(app) 를 `with` 없이 쓰는 기존 테스트들은 lifespan 이 발동하지 않아
영향이 없다(경험적으로 확인).

[추가 2026-08-06, 이슈 #401] lifespan 에서 categories 0행/0임베딩 가드
(app/pipelines/category_seed.py check_category_dictionary)를 검사한다. 기본값(log)·off 는
DB 연결 실패로 기동을 막지 않는다. category_dictionary_startup_check="fail" 로 강한 검증을
opt-in 하면 사전 상태를 확인 못하는 모든 경우(도달 불가 포함)에 기동을 거부한다(라운드 7 F8).

[추가 2026-08-08, 이슈 #437] lifespan 에서 모델 단가표 상태(app/core/model_pricing.py
log_model_price_table_status)를 1회 로그한다 — I/O 가 없어 항상 실행되고, category
dictionary 점검이 실패해도 이 신호는 남도록 그 앞에 둔다.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# [이슈 #33] Windows 기본 ProactorEventLoop 는 psycopg async 연결을 지원하지 않아
# AsyncPostgresStore/AsyncPostgresSaver(pg-profile, seller history.py·hitl.py 포함)가
# 조용히 실패 → dev 폴백(InMemoryStore/Saver)으로 전락한다(docs/lessons.md 2026-07-20).
# uvicorn 이 이벤트 루프를 만들기 전에(모듈 임포트 시점) 정책을 교체해야 한다.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.agents.profile.processed_events import close_pool as close_processed_events_pool
from app.agents.profile.session_activity import close_pool as close_session_activity_pool
from app.agents.profile.store import close_store as close_profile_store
from app.agents.seller.checkpoint import close_checkpointer as close_seller_checkpointer
from app.agents.seller.history import close_store as close_seller_history_store
from app.api import chat, events, internal, profile, seller
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.conversation import close_store as close_conversation_store
from app.core.config import get_settings
from app.core.errors import install_error_handling
from app.core.logging import configure_logging, get_logger
from app.core.model_pricing import log_model_price_table_status
from app.core.pg_store import close_store as close_pg_store
from app.core.pg_resilience import close_advisory_pool
from app.core.session_context import close_session_lifecycle, initialize_session_lifecycle
from app.core.ratelimit import rate_limit_middleware
from app.pipelines.category_seed import (
    CategoryDictionaryError,
    check_category_dictionary,
    unreachable_db_error_types,
)
from app.pipelines.scheduler import start_scheduler, stop_scheduler

logger = get_logger(__name__)


async def _close_owned_resources() -> None:
    """소유한 리소스를 의존성 역순으로 닫고 개별 실패를 격리한다."""
    resources = (
        ("session_lifecycle", close_session_lifecycle),
        ("seller_history_store", close_seller_history_store),
        ("seller_checkpointer", close_seller_checkpointer),
        ("profile_store", close_profile_store),
        ("session_activity_pool", close_session_activity_pool),
        ("processed_events_pool", close_processed_events_pool),
        ("conversation_store", close_conversation_store),
        ("pg_store", close_pg_store),
        ("advisory_pool", close_advisory_pool),
    )
    settings = get_settings()
    timeout_s = settings.lifespan_resource_close_timeout_s
    floor_s = settings.lifespan_resource_close_floor_s
    loop = asyncio.get_running_loop()
    cleanup_budget_s = settings.lifespan_cleanup_budget_s
    deadline = loop.time() + cleanup_budget_s
    required_floor_s = len(resources) * floor_s
    if cleanup_budget_s < required_floor_s:
        logger.warning(
            "lifespan cleanup budget cannot reserve resource floor "
            "budget_s=%s floor_s=%s resources=%d required_s=%s",
            cleanup_budget_s,
            floor_s,
            len(resources),
            required_floor_s,
        )
    failed = 0
    cancellation: asyncio.CancelledError | None = None
    for index, (name, close) in enumerate(resources):
        remaining_budget_s = max(deadline - loop.time(), 0.0)
        remaining_count = len(resources) - index
        reserved_for_rest_s = (remaining_count - 1) * floor_s
        allowance_s = remaining_budget_s - reserved_for_rest_s
        resource_timeout_s = min(timeout_s, max(allowance_s, 0.0))
        budget_limited = allowance_s < timeout_s
        try:
            async with asyncio.timeout(resource_timeout_s):
                await close()
        except asyncio.CancelledError as exc:
            failed += 1
            cancellation = exc
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            logger.warning("lifespan resource cleanup cancelled resource=%s", name)
        except TimeoutError:
            failed += 1
            if budget_limited:
                logger.warning(
                    "lifespan resource cleanup budget exhausted resource=%s "
                    "timeout_s=%s budget_s=%s",
                    name,
                    resource_timeout_s,
                    cleanup_budget_s,
                )
            else:
                logger.warning(
                    "lifespan resource cleanup timed out resource=%s timeout_s=%s",
                    name,
                    resource_timeout_s,
                )
        except Exception:
            failed += 1
            logger.exception("lifespan resource cleanup failed resource=%s", name)
    logger.info(
        "lifespan resource cleanup complete succeeded=%d failed=%d",
        len(resources) - failed,
        failed,
    )
    if cancellation is not None:
        raise cancellation


async def _check_category_dictionary_startup() -> None:
    """categories 0행/0임베딩 가드(이슈 #401) — 기동 시 1회.

    세 갈래로 원인을 구분해 로그는 정직하게 남기되(진단 정보는 뭉개지 않는다), **거부 여부는
    `fail` 모드에서 하나로 통일**한다(#401 라운드 7 리뷰 F8). 라운드 5(F7)의 "도달 불가는 모든
    모드에서 WARNING"은 리뷰가 실측으로 반박했다 — `psycopg.OperationalError` 는 일시적 도달
    불가(연결 거부·타임아웃)와 **영구적 구성 오류**(비밀번호·dbname 오타)를 **같은 타입**으로
    낸다. libpq 는 연결 수립 실패에 `sqlstate` 등 구조화된 판별자를 주지 않고 차이는 지역화되는
    메시지 문자열뿐이라(실측 확인) 예외 타입으로 "진짜 구성 오류"만 골라내는 건 불가능하다.

    그래서 분류 대신 `fail` 의 **의미**를 바꾼다 — `fail` 은 "강한 검증을 opt-in 한 모드"이므로
    계약은 "**사전이 건강함을 확인하지 못하면 기동하지 않는다**"다(원인이 오타든 일시적 불통이든
    가드 자체의 버그든 상관없이). `log`(기본값)·`off` 의 동작은 이전과 동일하다:

    1. `CategoryDictionaryError`(0행/0임베딩·비연결 DB 오류 — 모드는 `check_category_dictionary`
       안에서 이미 적용됨) — 그대로 전파해 기동을 거부한다.
    2. **도달 불가 계열**(`OSError`·`psycopg.OperationalError` —
       `category_seed.unreachable_db_error_types()` 로 얻는다. `app/main.py` 는 psycopg 를
       import 하지 않는다는 lazy-import 관례를 지키려고 `category_seed.py` 가 타입을 대신
       노출한다) — `log`/`off` 는 WARNING 후 계속(부팅 순간 DB 흔들림으로 서버 전체를 못 뜨게
       하는 건 과하다). **`fail` 은 ERROR 로그 후 전파**한다 — 잘못된 DSN(비밀번호·dbname
       오타)도 이 타입으로 나오므로, `fail` 을 건 운영자가 가장 잡고 싶은 실수를 잡으려면
       도달 불가도 "확인 실패"로 다뤄야 한다.
    3. **그 밖의 예상 못 한 예외**(가드 코드 자체 버그) — ERROR 로그("도달 불가"가 아니라
       "예상 못 한 실패"임을 정직하게 남긴다, `exc_info=True`). `log`/`off` 는 계속하고
       `fail` 은 전파한다 — 같은 "확인 실패면 거부" 원칙의 연장이다.
    """
    settings = get_settings()
    mode = settings.category_dictionary_startup_check
    try:
        await asyncio.to_thread(
            check_category_dictionary,
            settings.catalog_db_url,
            mode=mode,
        )
    except CategoryDictionaryError:
        raise
    except unreachable_db_error_types():
        if mode == "fail":
            logger.error(
                "category dictionary startup check could not reach the database — "
                "failing startup because fail mode requires confirmed dictionary health "
                "(cause may be transient unreachability OR a permanent misconfiguration "
                "such as a wrong password/dbname — psycopg cannot reliably distinguish them)",
                exc_info=True,
            )
            raise
        logger.warning(
            "category dictionary startup check could not reach the database", exc_info=True
        )
    except Exception:
        logger.error(
            "category dictionary startup check failed unexpectedly (guard bug, not a reachability issue)",
            exc_info=True,
        )
        if mode == "fail":
            raise


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifecycle migration 뒤 scheduler를 시작하고 owned resources를 역순 종료한다."""
    scheduler_started = False
    try:
        log_model_price_table_status(get_settings())
        await _check_category_dictionary_startup()
        await initialize_session_lifecycle()
        start_scheduler()
        scheduler_started = True
        yield
    finally:
        try:
            if scheduler_started:
                stop_scheduler()
        finally:
            await _close_owned_resources()


def create_app() -> FastAPI:
    """FastAPI 앱을 생성·구성해 반환한다 (앱 팩토리)."""
    configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="Jarvis AI Server",
        version="0.1.0",
        description="agentic commerce AI server (FastAPI + LangGraph)",
        lifespan=_lifespan,
    )

    # 오류 봉투(§2.5) 예외 핸들러 + requestId 미들웨어. 스트림 전 거부/검증 실패를
    # {"error":{code,message,requestId}} 봉투로 낸다.
    install_error_handling(app)

    # 레이트 리밋(§2.8, 채팅 전송 경로) — requestId 미들웨어보다 안쪽에 둔다.
    app.middleware("http")(rate_limit_middleware)

    # 요청 바디 크기 상한(이슈 #299) — 레이트 리밋보다 **바깥**에 둔다. rate_limit_middleware 는
    # _verified_sub_scope() 에서 JWT 서명 검증(필요 시 JWKS HTTP)을 돌리는데, 거대 바디를 그
    # 비용 뒤에 거절하면 방어 목적이 반감된다. 또 거대 바디 요청이 레이트 리밋 슬롯을 소모하지
    # 않게 한다. Starlette 은 add_middleware 가 user_middleware.insert(0, ...) 이고 스택을
    # reversed() 로 감싸므로 **나중에 등록한 것이 바깥**이다 — 아래 CORSMiddleware(이미 이
    # 규약으로 최외곽에 등록돼 있던 것) 보다 먼저 등록해 그 안쪽(= rate_limit_middleware 바깥)에
    # 둔다. 실측: app.user_middleware 등록 순서와 TestClient 왕복(순서 증명 테스트,
    # tests/unit/test_body_limit.py) 로 확인 — 바디 초과 요청은 레이트 리밋 상한 횟수를 넘겨
    # 보내도 슬롯을 소모하지 않는다.
    app.add_middleware(BodySizeLimitMiddleware)

    # CORS 는 최외곽(가장 마지막 등록)에 둬 오류·429·바디초과 400 응답에도 헤더가 실리게 한다
    # (api-spec §2.7, 기존 429 와 같은 이유). Authorization 헤더 사용 → 브라우저 preflight(OPTIONS) 발생.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # MVP 라우터: 사용자 대면 chat / seller 만 등록한다.
    app.include_router(chat.router)
    app.include_router(seller.router)
    app.include_router(profile.router)
    app.include_router(events.router)
    # Spring → AI 위임(레인 b) — I-22 홈 추천 랭킹(§3.7, #148)
    app.include_router(internal.router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        """헬스 체크. 컨테이너 healthcheck·부팅 확인용."""
        return {"status": "ok"}

    return app


app = create_app()
