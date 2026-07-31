"""Spring → AI 이벤트 수신 스키마 — session-end와 session-claim.

  - session-end : MVP 유지 — SessionEndEvent {userId(number), sessionId, reason} · 멱등키=(userId,sessionId) 파생(§2.7, 이슈 #62)
  - session-claim: 로그인 완료 시 guestId 소유 세션 전체를 userId 회원 문맥으로 원자 전이
  - catalog     : [영구 미채택] I-17 pull 배치로 대체 (schemas.spring.ProductChangesPage, §4.8)
  - order       : [영구 미채택] GET /orders/recent 로 대체 (schemas.spring.RecentPurchases, §4.7)

SessionEndEvent 실제 모델은 ``app.schemas.profile.SessionEndEvent``가 소유하고,
SessionClaimEvent는 이 모듈이 소유한다.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.chat import to_camel

_BIGINT_MAX = 2**63 - 1


class StrictEventModel(BaseModel):
    """Spring inbound event 전용 strict camelCase wire base."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=False,
        extra="forbid",
    )


class SessionClaimEvent(StrictEventModel):
    """Spring login completion event that transfers one guest session to a member."""

    session_id: str = Field(strict=True, min_length=1)
    guest_id: str = Field(strict=True, min_length=1)
    user_id: int = Field(strict=True, gt=0, le=_BIGINT_MAX)

    @field_validator("session_id", "guest_id")
    @classmethod
    def _limit_key_length(cls, value: str) -> str:
        """공개 claim 식별자는 chat key와 같은 설정 상한을 사용한다."""
        from app.core.config import get_settings

        cap = get_settings().chat_key_max_chars
        if len(value) > cap:
            raise ValueError(f"identifier exceeds {cap} characters")
        return value
