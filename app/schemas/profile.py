"""프로필 조회 응답 스키마 (api-spec §3.3).

GET /profile/me 응답. SPEC-PROFILE-001 §5.4 ProfileViewResponse 와 일치한다.
게스트/신규 회원은 exists=false, markdown=null 을 정상 응답(200)으로 반환한다
(api-spec §3.3, REQ-PROF-081).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProfileViewResponse(BaseModel):
    """마이페이지 프로필 조회 응답 (api-spec §3.3, SPEC-PROFILE-001 §5.4)."""

    user_id: str = Field(..., description="요청 대상 식별자 (토큰 subject 도출)")
    exists: bool = Field(..., description="프로필 존재 여부. 게스트·신규 false")
    markdown: str | None = Field(default=None, description="사람이 읽는 프로필 마크다운")
    generated_at: str | None = Field(
        default=None, description="요약 생성 시각 (sleep-time consolidation, ISO-8601)"
    )
