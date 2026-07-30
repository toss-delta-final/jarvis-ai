"""Spring → AI 이벤트 수신 스키마 — session-end와 session-claim.

  - session-end : MVP 유지 — SessionEndEvent {userId(number), sessionId, reason} · 멱등키=(userId,sessionId) 파생(§2.7, 이슈 #62)
  - session-claim: 로그인 완료 시 guestId 소유 세션 전체를 userId 회원 문맥으로 원자 전이
  - catalog     : [영구 미채택] I-17 pull 배치로 대체 (schemas.spring.ProductChangesPage, §4.8)
  - order       : [영구 미채택] GET /orders/recent 로 대체 (schemas.spring.RecentPurchases, §4.7)

SessionEndEvent 실제 모델은 ``app.schemas.profile.SessionEndEvent``가 소유하고,
SessionClaimEvent는 이 모듈이 소유한다.
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.chat import CamelModel


class SessionClaimEvent(CamelModel):
    """Spring login completion event that transfers one guest session to a member."""

    session_id: str
    guest_id: str
    user_id: int = Field(gt=0)
