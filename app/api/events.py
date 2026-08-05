"""이벤트 수신 엔드포인트 — POST /events/session-end (I-20, api-spec §3.5).

Spring → AI inbound(우리가 호스팅). 세션 종료 통지를 프로필 파이프라인 조기 트리거로 받는다
(결정 12/16). best-effort·멱등((userId, sessionId) 고정키, §2.7) — 유실돼도 AI inactivity sweep이 회수.
서비스 토큰(레인 b) 검증. catalog/order 이벤트는 영구 미채택(§3.6).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.api.deps import verify_service_token
from app.core import session_lifecycle
from app.core.llm import get_llm  # noqa: F401 - integration injection compatibility
from app.core.pg_resilience import is_state_store_unavailable
from app.core.session_context import SessionStateUnavailable
from app.schemas.events import SessionClaimEvent
from app.schemas.profile import SessionEndEvent

router = APIRouter(tags=["events"])
logger = logging.getLogger(__name__)


@router.post("/events/session-claim", status_code=202)
async def session_claim(
    event: SessionClaimEvent,
    _token: None = Depends(verify_service_token),
) -> dict[str, str]:
    """Transfer a whole guest session to the authenticated member selected by Spring."""
    try:
        outcome = await session_lifecycle.claim_owner(event)
    except Exception as exc:
        if is_state_store_unavailable(exc):
            raise SessionStateUnavailable from exc
        raise
    return {"status": "accepted" if outcome.claimed else "duplicate"}


@router.post("/events/session-end", status_code=202)
async def session_end(event: SessionEndEvent, _token: None = Depends(verify_service_token)) -> dict:
    """세션 종료 → 프로필 델타 추출 + consolidation(best-effort·멱등, 202 Accepted)."""
    # [신뢰경계] session-end 는 Spring→AI(레인 b) — 신원(userId/sessionId)은 §3.5 계약상 본문으로
    # 오며, 호출 인가는 **서비스 토큰**(verify_service_token)이 담당한다(Spring 은 인증된 호출자).
    # sessionId 길이·userId(BIGINT) 범위 상한은 SessionEndEvent 가 강제(스토어 키 남용 방어).
    # best-effort 프로필 갱신 — LLM 미구성/버퍼 없음/오류는 no-op degrade. 어떤 오류도 202 를 막지 않는다(§3.5).
    # store/builder 는 문자열 신원 키를 쓰므로 int userId 를 문자열화(JWT sub·conversation_key 와 정합).
    try:
        result = await session_lifecycle.SessionLifecycleCoordinator().begin_terminal(
            event.user_id,
            event.session_id,
        )
    except SessionStateUnavailable:
        raise
    except Exception as exc:
        if is_state_store_unavailable(exc):
            raise SessionStateUnavailable from exc
        raise
    status = "duplicate" if result.duplicate else "accepted"
    return {"status": status}
