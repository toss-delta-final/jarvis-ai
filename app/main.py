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
from app.core.conversation import close_store as close_conversation_store
from app.core.config import get_settings
from app.core.errors import install_error_handling
from app.core.logging import configure_logging, get_logger
from app.core.pg_store import close_store as close_pg_store
from app.core.pg_resilience import close_advisory_pool
from app.core.session_context import close_session_lifecycle, initialize_session_lifecycle
from app.core.ratelimit import rate_limit_middleware
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
    failed = 0
    cancellation: asyncio.CancelledError | None = None
    for name, close in resources:
        try:
            async with asyncio.timeout(timeout_s):
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
            logger.warning(
                "lifespan resource cleanup timed out resource=%s timeout_s=%s",
                name,
                timeout_s,
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


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifecycle migration 뒤 scheduler를 시작하고 owned resources를 역순 종료한다."""
    scheduler_started = False
    try:
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

    # CORS 는 최외곽(가장 마지막 등록)에 둬 오류·429 응답에도 헤더가 실리게 한다 (api-spec §2.7).
    # Authorization 헤더 사용 → 브라우저 preflight(OPTIONS) 발생.
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
