"""FastAPI 인증 의존성 (api-spec §2.2, RS256 + JWKS 확정 2026-07-15).

사용자 대면 API(/chat·/seller/chat)는 사용자 JWT 를 검증해 Identity 를 만든다.

[I-20] /events/session-end는 MVP inbound 채널로 유지하며 X-Internal-Token을 검증한다.

[보안] Identity 는 오직 토큰에서 도출된다 — 요청 본문의 식별자는 신뢰하지 않는다.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException, Request, status

from app.core.auth import AuthError, Identity, TokenExpiredError, decode_token
from app.core.config import Settings, get_settings
from app.core.errors import get_request_id

logger = logging.getLogger(__name__)

# 사유 문자열 상한 — 예외 메시지가 길어도 로그 한 줄이 넘치지 않게 자른다.
_REASON_MAX_CHARS = 200
# __cause__/__context__ 체인 추적 상한 (순환/과다 중첩 방어).
_REASON_MAX_DEPTH = 5
# 흔한 제어문자는 읽기 좋은 형태로 남긴다 (아래 _escape_unprintable).
_SHORT_ESCAPES = {"\t": "\\t", "\n": "\\n", "\r": "\\r"}


def _escape_unprintable(text: str) -> str:
    """로그 한 줄의 구조를 바꿀 수 있는 문자를 전부 이스케이프한다 (로그 인젝션, CWE-117).

    401 사유에 섞이는 값은 **서명 검증 이전** 입력이다 — PyJWKClientError 는 JWT 헤더의
    `kid` 를 메시지에 그대로 싣고, dev 모드 decode 는 서명을 아예 보지 않는다. 개행이 그대로
    나가면 공격자가 유효 서명 없이도 가짜 "auth rejected ..." 줄을 로그에 심을 수 있다.

    이스케이프 대상을 손으로 열거하지 않는다 — C0/DEL 만 막으면 NEL(U+0085)·LINE
    SEPARATOR(U+2028)처럼 뷰어·파서가 개행으로 읽는 문자가 빠진다(PR #409 리뷰 2R).
    `str.isprintable()` 은 제어(Cc)·형식(Cf, 예: 양방향 재정의 U+202E)·줄/문단 구분자
    (Zl/Zp)를 모두 비출력으로 판정하므로 그 판정을 그대로 신뢰한다.
    """
    if text.isprintable():
        return text
    return "".join(
        _SHORT_ESCAPES.get(char) or (char if char.isprintable() else f"\\u{ord(char):04x}")
        for char in text
    )


def _reason_chain(exc: BaseException) -> str:
    """예외 타입명 + 메시지를 __cause__ 체인으로 이어붙인 401 진단 문자열 (이슈 #408).

    PyJWT 는 실패 사유를 원 예외(InvalidSignatureError/InvalidAudienceError/
    InvalidIssuerError/MissingRequiredClaimError/PyJWKClientError 등)에 담고
    core.auth 가 그것을 AuthError 로 감싸므로, 사유는 __cause__ 쪽에만 남는다.

    [보안] 토큰 원문·서명·클레임 식별자(sub/sessionId/brandId)는 싣지 않는다 — 예외 타입과
    라이브러리/자체 예외 메시지만 남긴다. 자체 메시지 중 값을 끼우는 것은 신원 판별자
    `sub_type`(member|guest 집합) 하나뿐이며, 이는 식별자가 아니라 유형 값이고 §2.3 클레임
    변경을 가리는 것이 이 로그의 목적이다.

    [보안] 메시지에 섞이는 토큰 유래 값(`kid`·`sub_type`)은 **서명 검증 이전** 값이라 공격자
    제어다. 비출력 문자를 이스케이프해 로그 인젝션(CWE-117)을 막는다(_escape_unprintable).

    [보안] `raise ... from None` 으로 억제된 컨텍스트는 따라가지 않는다 — CPython 트레이스백과
    같은 규칙이다. 억제는 "이 예외는 노출하지 말라"는 명시적 의사표시이므로, 나중에 누가
    토큰 조각이 섞인 하위 예외를 그렇게 숨겨도 이 경로가 되살리지 않는다(PR #409 리뷰 4R).
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < _REASON_MAX_DEPTH:
        seen.add(id(current))
        message = _escape_unprintable(str(current))[:_REASON_MAX_CHARS]
        parts.append(f"{type(current).__name__}: {message}" if message else type(current).__name__)
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
    return " <- ".join(parts)


def _log_auth_rejection(
    request: Request | None,
    *,
    code: str,
    dependency: str,
    reason: str,
) -> None:
    """401 매핑 지점의 실패 사유를 WARNING 으로 남긴다 (이슈 #408).

    운영 401 은 응답 본문에 사유를 싣지 않으므로(§2.5 고정 메시지), 진단 근거는 이 로그뿐이다.
    requestId 는 errors.request_context_middleware 가 부여한 값과 동일해 §2.4 오류 봉투·
    응답 헤더(X-Request-Id)와 상관된다.

    [보안] path 도 사유와 같이 이스케이프한다. 지금 이 의존성을 쓰는 라우트는 전부 고정 리터럴
    경로라 라우팅에서 걸러지지만, path 파라미터가 있는 라우트(§4.4/§4.5 {brandId} 류)에
    재사용되는 순간 `request.url.path` 는 외부 통제 값이 된다 — 위협 모델을 "로그 한 줄에
    들어가는 모든 외부 통제 값"으로 일관되게 둔다(PR #409 리뷰 3R).
    """
    logger.warning(
        "auth rejected code=%s dep=%s path=%s rid=%s reason=%s",
        code,
        dependency,
        _escape_unprintable(request.url.path) if request is not None else None,
        get_request_id(request) if request is not None else None,
        reason,
    )


def _extract_bearer(authorization: str | None) -> str | None:
    """`Authorization: Bearer <token>` 헤더에서 토큰만 추출한다."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _identity_or_401(
    request: Request | None,
    authorization: str | None,
    *,
    dependency: str,
) -> Identity:
    """토큰 검증 → Identity. 401 매핑 두 갈래 모두 실패 사유를 WARNING 으로 남긴다 (#408).

    응답에는 §2.5 고정 메시지만 나가므로(사유 미노출), 운영 진단 근거는 이 로그가 유일하다.
    검증 로직 자체는 core.auth 소관 — 여기서는 사유를 관측 가능하게 만들기만 한다.
    """
    settings: Settings = get_settings()
    token = _extract_bearer(authorization)
    try:
        return decode_token(
            token,
            auth_mode=settings.auth_mode,
            jwks_url=settings.jwks_url,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            scope=settings.jwt_scope,
            jwks_timeout_s=settings.spring_timeout_s,
            jwks_cache_ttl_s=settings.jwks_cache_ttl_s,
        )
    except TokenExpiredError as exc:
        # §2.5: 만료는 TOKEN_EXPIRED — FE 가 CH-1b 재발급 후 1회 재시도하는 신호.
        _log_auth_rejection(
            request, code="TOKEN_EXPIRED", dependency=dependency, reason=_reason_chain(exc)
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_EXPIRED", "message": "인증 실패"},
        ) from exc
    except AuthError as exc:
        # §2.5: 그 외(없음/서명·형식·scope 불일치)는 TOKEN_INVALID.
        _log_auth_rejection(
            request, code="TOKEN_INVALID", dependency=dependency, reason=_reason_chain(exc)
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_INVALID", "message": "인증 실패"},
        ) from exc


def get_identity(
    # FastAPI 가 주입한다. 기본 None 은 의존성 밖 직접 호출(단위 테스트)용 — 그때는 requestId·
    # path 없이 사유만 남는다.
    request: Request = None,  # type: ignore[assignment]
    authorization: str | None = Header(default=None),
) -> Identity:
    """사용자 JWT → Identity 의존성.

    dev 모드에서 헤더가 없으면 게스트 Identity 를 반환한다 (core.auth 참고).
    무효/만료 토큰은 401 로 매핑한다 (api-spec §2.4).
    """
    return _identity_or_401(request, authorization, dependency="get_identity")


def require_seller(
    request: Request = None,  # type: ignore[assignment]
    authorization: str | None = Header(default=None),
) -> Identity:
    """판매자 스코프 필수 의존성 (api-spec §3.2).

    판매자 스코프(seller_id)가 없는 토큰의 /seller/chat 호출은 403 으로 거부한다.
    반환 Identity 의 brand_id(§4.4/§4.5 {brandId} path용)는 검증된 토큰 클레임 유래다.
    """
    identity = _identity_or_401(request, authorization, dependency="require_seller")
    if not identity.seller_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "seller scope required"},
        )
    if not identity.brand_id:
        # §2.3: 판매자 토큰엔 brandId 클레임 필수 — 없으면 판매자 역호출(§4.4/§4.5) 불가.
        # 요청 본문/발화로 우회하지 않도록 검증된 클레임 부재 시 거부한다(IDOR 방지).
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "seller token missing brandId claim"},
        )
    return identity


def require_buyer_session(
    identity: Identity,
    session_id: str,
    settings: Settings,
) -> None:
    """구매자 body sessionId를 서명된 스트림 티켓의 접속에 바인딩한다."""
    if identity.seller_id is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "buyer scope required"},
        )
    if settings.auth_mode == "dev" and identity.session_id is None:
        return
    if not identity.session_id or identity.session_id != session_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "SESSION_FORBIDDEN", "message": "session access denied"},
        )


def buyer_owner_id(identity: Identity, settings: Settings) -> str:
    """구매자 세션 상태의 소유자 키를 검증된 subject에서 도출한다."""
    if identity.subject:
        return identity.subject
    if settings.auth_mode == "dev":
        return "dev-anon"
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "SESSION_FORBIDDEN", "message": "session access denied"},
    )


def verify_service_token(
    request: Request = None,  # type: ignore[assignment]
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """Spring → AI inbound(레인 b) 서비스 토큰 검증 (api-spec §3.5).

    config internal_api_token 이 설정돼 있으면 헤더 일치를 요구하고, 비어 있으면(dev) 허용한다.
    """
    settings: Settings = get_settings()
    # dev(로컬)만 미검증 편의 허용. 운영(jwks)은 inbound write 엔드포인트라 **fail-closed** —
    # 토큰 미설정·불일치 모두 401(프로필 오염 IDOR 방지, 리뷰 반영).
    if settings.auth_mode == "dev":
        return
    if (
        not settings.internal_api_token
        or x_internal_token is None
        or not hmac.compare_digest(x_internal_token, settings.internal_api_token)
    ):
        # [보안] 사유는 어느 조건에서 걸렸는지만 남긴다 — 헤더 값·설정 토큰은 로그 금지(#408).
        if not settings.internal_api_token:
            reason = "internal_api_token is not configured"
        elif x_internal_token is None:
            reason = "X-Internal-Token header missing"
        else:
            reason = "X-Internal-Token mismatch"
        _log_auth_rejection(
            request,
            code="INTERNAL_TOKEN_INVALID",
            dependency="verify_service_token",
            reason=reason,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INTERNAL_TOKEN_INVALID", "message": "서비스 토큰 필요/불일치"},
        )
