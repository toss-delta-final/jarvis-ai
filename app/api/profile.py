"""프로필 조회 엔드포인트 — GET /profile/me (MVP, api-spec v0.7.0 §3.4).

[정정] 고도화가 아니라 MVP 범위다 — 마이페이지 프로필 자연어 노출(결정 4-A 항목 6,
SPEC-PROFILE-001 §5.4/§6.9). 게스트·프로필 미보유는 {exists: false, markdown: null}
정상 200 (오류 아님, REQ-PROF-081).

TODO(MVP): APIRouter + GET /profile/me (사용자 JWT, 레인 a) → main.py 라우터 등록.
응답: {exists, markdown, updatedAt} (camelCase, §3.4).
"""

from __future__ import annotations
