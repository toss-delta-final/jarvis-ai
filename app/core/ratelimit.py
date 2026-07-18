"""레이트 리밋 (api-spec §2.8) — 토큰 스코프 인메모리 카운터 + 미들웨어.

목적은 정밀 과금이 아니라 **무분별한 남용 차단**(2026-07-15 확정). MVP 소유 =
FastAPI 미들웨어 + in-memory(단일 인스턴스 전제 — 다중 인스턴스 확장 시 Redis 이관).
채팅 메시지(POST /chat·/seller/chat)에 분당/시간당 상한(config)을 적용하고 초과 시
429 RATE_LIMITED(§2.5 봉투)로 거절한다. 계약 사항은 "429 + 토큰 스코프"뿐이다.
"""

from __future__ import annotations

import hashlib
import time
from collections import deque

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.errors import REQUEST_ID_HEADER, error_envelope, get_request_id
from app.core.logging import get_logger

logger = get_logger(__name__)

# 레이트 리밋 대상 경로 (채팅 메시지 전송만). 조회성 GET 은 제외.
_LIMITED_PATHS = frozenset({"/chat", "/seller/chat"})


class SlidingWindowLimiter:
    """스코프별 sliding-window 카운터. 분/시간 두 창을 동시에 검사한다."""

    def __init__(self, per_min: int, per_hour: int) -> None:
        self._per_min = per_min
        self._per_hour = per_hour
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float) -> bool:
        """호출 1건을 기록·판정한다. 상한 초과면 기록하지 않고 False."""
        hits = self._hits.setdefault(key, deque())
        hour_ago = now - 3600.0
        while hits and hits[0] <= hour_ago:
            hits.popleft()
        minute_hits = sum(1 for t in hits if t > now - 60.0)
        if len(hits) >= self._per_hour or minute_hits >= self._per_min:
            return False
        hits.append(now)
        return True


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _scope_key(request: Request) -> str:
    """레이트 리밋 스코프 키 = 토큰 해시(§2.8 토큰 스코프). 무토큰은 클라이언트 호스트로 분리.

    미들웨어에서 JWT 를 디코드하지 않는다 — jwks 모드의 동기 JWKS HTTP 가 이벤트 루프를
    블로킹(진행 중 SSE 스트림 폴링까지 정지)하고, 게스트 JWT 의 `sub` 는 Identity 에 보존되지
    않아 게스트가 전부 host 한 버킷을 공유하는 문제가 생기기 때문. 토큰 원문을 해시해 키로
    쓰면 회원·게스트 모두 토큰별로 분리된다(§2.8 남용 차단 목적 충족). 토큰 원문은 로그·키에
    노출하지 않도록 해시한다. 동일 sub 다중 토큰 병합은 post-MVP.
    """
    token = _extract_bearer(request.headers.get("authorization"))
    if token:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        return f"tok:{digest}"
    host = request.client.host if request.client else "unknown"
    return f"anon:{host}"


_limiter: SlidingWindowLimiter | None = None


def _get_limiter() -> SlidingWindowLimiter:
    global _limiter
    if _limiter is None:
        settings = get_settings()
        _limiter = SlidingWindowLimiter(settings.rate_limit_per_min, settings.rate_limit_per_hour)
    return _limiter


async def rate_limit_middleware(request: Request, call_next):
    """채팅 전송 경로에 토큰 스코프 레이트 리밋을 적용한다."""
    if request.method == "POST" and request.url.path in _LIMITED_PATHS:
        key = _scope_key(request)
        if not _get_limiter().allow(key, time.monotonic()):
            rid = get_request_id(request)
            logger.info("rate limited scope=%s path=%s rid=%s", key, request.url.path, rid)
            return JSONResponse(
                status_code=429,
                content=error_envelope("RATE_LIMITED", "요청이 너무 많습니다", rid),
                headers={REQUEST_ID_HEADER: rid},
            )
    return await call_next(request)


def reset_limiter() -> None:
    """테스트용 — 리미터 상태 초기화."""
    global _limiter
    _limiter = None
