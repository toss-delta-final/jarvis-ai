"""FastAPI 애플리케이션 엔트리포인트.

CORS 미들웨어(오리진은 설정 주입), MVP 라우터(chat/seller), GET /health 를 구성한다.
FE 가 AI 서버를 다른 오리진에서 직접 호출하므로 CORS 가 앞단으로 이동했다 (api-spec §2.7 / C-11).

[변경 2026-07-15] MVP 표면은 /chat, /seller/chat, /health 로 축소.
  - [TODO MVP] GET /profile/me(§3.4)·POST /events/session-end(§3.5) 라우터 등록 필요 —
    현재 플레이스홀더(app/api/{profile,events}.py). catalog/order 이벤트는 영구 미채택.
  - [TODO MVP] §2.9 스트림 수명주기(409·취소·타임아웃)·§2.8 레이트 리밋 미들웨어 — app/api/chat.py TODO 참고.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, seller
from app.core.config import get_settings
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    """FastAPI 앱을 생성·구성해 반환한다 (앱 팩토리)."""
    configure_logging()
    settings = get_settings()

    app = FastAPI(
        title="Jarvis AI Server",
        version="0.1.0",
        description="agentic commerce AI server (FastAPI + LangGraph)",
    )

    # CORS: 허용 오리진은 설정에서 주입 (api-spec §2.7). Authorization 헤더 → preflight 발생.
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

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        """헬스 체크. 컨테이너 healthcheck·부팅 확인용."""
        return {"status": "ok"}

    return app


app = create_app()
