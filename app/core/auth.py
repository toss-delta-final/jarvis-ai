"""JWT 디코드/검증 헬퍼 (api-spec §2.3, RS256 + JWKS 확정 · v0.10.0 스트림 티켓 · #34 실배선).

인증 모드 2종:
  - "dev"  : 서명 검증 없이 디코드. 헤더 없으면 게스트로 취급.
             (로컬 개발 편의 — Spring 토큰 없이도 동작)
  - "jwks" : Spring 이 서빙하는 GET /.well-known/jwks.json 공개키로 RS256 검증.
             토큰 헤더의 kid → 공개키 매핑. 검증 항목은 §2.3 확정 5종 —
             signature / exp / iss / aud / scope. kid miss 시에만 JWKS refetch
             (PyJWKClient 내장: 캐시 miss → 1회 재조회 후 재시도).

[변경] 기존 "secret"(HS256 공유 시크릿) 모드는 제거했다 — Spring 이 RS256+JWKS 로 확정.

스트림 티켓 클레임 (§2.3 v0.28.0, #439 — CH-6 정본 2026-07-18):
  - sub      : 사용자 식별자 (회원/판매자=숫자 문자열, 게스트=UUID, §2.6)
  - sub_type : member | guest — **모든 티켓의 필수 클레임**이며 구매자 신원 유형의 유일한
               정본이다. 그 외 값·누락은 fail-closed 거부.
  - scope    : 용도 검증 (확정값 chat:stream, config 주입)
  - role     : 선택적 권한 클레임. 있으면 exact lowercase "seller"이고 그 티켓의
               sub_type은 반드시 "member"여야 한다(판매자 티켓은 sub_type="member"를
               항상 동반 — BE StreamTicketProvider 실측). buyer role 대체 금지.
  - brandId  : 판매자(role="seller") 브랜드 id — {brandId} path용, 요청 본문 불신(§2.6).
  - sessionId: 구매자 티켓이 증명한 Spring 접속 id — /chat body와 일치해야 한다.

[보안] 신원(user_id)·게스트 여부·판매자 스코프는 오직 토큰 클레임에서만 도출한다.
요청 본문의 식별자는 절대 신뢰하지 않는다 (사칭 방지, api-spec §2.3 a / §2.5 / §3.1 / §3.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import jwt
from jwt import PyJWKClient

# 클레임 키
CLAIM_SUBJECT = "sub"
CLAIM_SUB_TYPE = "sub_type"
CLAIM_SCOPE = "scope"
CLAIM_ROLE = "role"
CLAIM_BRAND_ID = "brandId"
CLAIM_SESSION_ID = "sessionId"

# sub_type 값 (§2.3 v0.10.0 확정 — member|guest 두 값만 정본)
SUB_TYPE_MEMBER = "member"
SUB_TYPE_GUEST = "guest"

# dev legacy buyer role과 JWKS exact seller role.
ROLE_USER = "USER"
ROLE_GUEST = "GUEST"  # dev legacy compatibility only; JWKS guest는 sub_type="guest"
ROLE_SELLER = "seller"
STREAM_SCOPE = "chat:stream"
_BIGINT_MAX = 2**63 - 1


@dataclass(frozen=True)
class Identity:
    """토큰에서 도출한 호출자 신원. 요청 본문이 아니라 오직 토큰이 근거다.

    brand_id 는 role=="seller" 토큰의 `brandId` 클레임 — 판매자 역호출(§4.4/§4.5)의
    `{brandId}` path 에 쓴다. 요청 본문/발화에서 받지 않는다 (IDOR 방지, §2.6).
    """

    user_id: str | None
    is_guest: bool
    seller_id: str | None
    # JWKS seller는 decode 경계에서 JSON int 양의 BIGINT로 검증한다. dev legacy decode는
    # 기존 로컬 토큰 호환을 위해 문자열을 보존할 수 있다.
    brand_id: str | int | None = None
    # subject: 검증된 raw `sub` 클레임 — 게스트 UUID 포함 모든 역할에 보존한다.
    # 레이트 리밋·동시성 레지스트리의 신원 스코프 키로 일관되게 쓴다(§2.8/§2.9).
    subject: str | None = None
    # 구매자 티켓이 증명한 Spring 접속 id. 판매자 티켓과 dev 무토큰에는 없을 수 있다.
    session_id: str | None = None


class AuthError(Exception):
    """토큰 없음/무효. 라우터에서 401 TOKEN_INVALID 로 매핑한다 (api-spec §2.5)."""


class TokenExpiredError(AuthError):
    """토큰 exp 경과. 라우터에서 401 TOKEN_EXPIRED 로 매핑한다 (api-spec §2.3/§2.5).

    FE 는 이 코드를 받으면 CH-1b 재발급 후 원 요청을 1회 재시도한다 — 문자열 스니핑이
    아니라 예외 타입으로 구분해 매핑이 깨지지 않게 한다.
    """


def _norm_role(role: object) -> str | None:
    """dev 레거시 호환에만 쓰는 role 대문자 정규화."""
    if not isinstance(role, str):
        return None
    normalized = role.strip().upper()
    # 빈/공백 문자열은 "role 없음"으로 취급 — "" 가 None 이 아니라서 fail-closed
    # 가드(require_identity_claim)를 우회해 회원으로 승인되는 구멍 방지 (리뷰 3R 반영).
    return normalized or None


def _claims_to_identity(claims: dict, *, require_identity_claim: bool = False) -> Identity:
    """검증된 클레임 dict → Identity 매핑.

    §2.3 v0.28.0(#439, XOR 폐지 — CH-6 정본 2026-07-18 실측 반영) 판정 순서:
      1. require_identity_claim=True(JWKS 실배선 레인)는 sub_type이 모든 티켓의 필수
         클레임이다 — member|guest exact 문자열이 아니면 즉시 거부.
      2. role은 선택적 권한 클레임 — 있으면 정확히 "seller"이고 그 티켓의 sub_type이
         "member"여야 한다(판매자 티켓은 sub_type="member"를 항상 동반, BE
         StreamTicketProvider 실측). role이 판정의 우선 축이다: role="seller"+
         sub_type="member"는 판매자로 판정한다(sub_type="member"가 판매자 신원을
         빼앗지 않는다).
      3. dev의 구 role 폴백(GUEST/USER 등) → require_identity_claim=False 로컬 호환 유지.

    dev 모드(require_identity_claim=False)만 legacy role을 관용한다 — 이 분기는 이번
    개정 대상이 아니다.
    """
    subject = claims.get(CLAIM_SUBJECT)
    raw_role = claims.get(CLAIM_ROLE)
    role = _norm_role(raw_role)
    sub_type = claims.get(CLAIM_SUB_TYPE)
    session_id = claims.get(CLAIM_SESSION_ID)

    if require_identity_claim:
        # sub_type 은 모든 티켓의 필수 클레임 (CH-6 정본, 2026-07-18).
        if not isinstance(sub_type, str) or sub_type not in (SUB_TYPE_MEMBER, SUB_TYPE_GUEST):
            raise AuthError("invalid sub_type claim")
        # role 은 선택적 권한 클레임 — 있으면 정확히 "seller"이고 회원이어야 한다.
        if CLAIM_ROLE in claims:
            if raw_role != ROLE_SELLER or sub_type != SUB_TYPE_MEMBER:
                raise AuthError("invalid seller role claim")
    if (require_identity_claim and CLAIM_ROLE in claims) or (
        not require_identity_claim and role == ROLE_SELLER.upper()
    ):
        if require_identity_claim:
            brand_id = claims.get(CLAIM_BRAND_ID)
            if (
                not isinstance(subject, str)
                or not subject.isascii()
                or not subject.isdigit()
                or len(subject) > 19
                or not 1 <= int(subject) <= _BIGINT_MAX
            ):
                raise AuthError("invalid seller subject claim")
            if type(brand_id) is not int or not 1 <= brand_id <= _BIGINT_MAX:
                raise AuthError("invalid seller brandId claim")
        else:
            brand_id = claims.get(CLAIM_BRAND_ID)
        # 판매자는 sub 를 판매자 식별자로도 사용한다 (스코프 근거는 role 클레임).
        return Identity(
            user_id=subject,
            is_guest=False,
            seller_id=subject,
            brand_id=brand_id,
            subject=subject,
            session_id=session_id,
        )
    if sub_type is not None:
        if sub_type == SUB_TYPE_GUEST:
            return Identity(
                user_id=None,
                is_guest=True,
                seller_id=None,
                subject=subject,
                session_id=session_id,
            )
        if sub_type == SUB_TYPE_MEMBER:
            # 타입만 정규화하고 값 형식은 검증하지 않는다.
            # 정규화 이유: 발급자가 sub 를 JSON 숫자로 실으면 int 가 들어오는데
            # (66행 brand_id 주석의 동일 사례), /events/* 는 str(event.user_id) 로
            # 정규화하므로 맞춰두지 않으면 같은 사용자의 owner 비교가 42 != "42" 로 깨진다.
            # 형식 검증을 하지 않는 이유: 잘못된 member sub 는 401 로 채팅 전체를 막지 않고
            # 신원이 필요한 하위 능력(주문조회)에서만 차단하는 것이 계약이다
            # (test_order_status_invalid_member_identity_is_blocked_before_spring).
            member_subject = (
                str(subject)
                if isinstance(subject, int) and not isinstance(subject, bool)
                else subject
            )
            return Identity(
                user_id=member_subject,
                is_guest=False,
                seller_id=None,
                subject=member_subject,
                session_id=session_id,
            )
        # 미지 sub_type — 정본 값 집합(member|guest) 밖은 신원 판정 불가로 거부.
        # (require_identity_claim=True는 위 필수 클레임 검사에서 이미 걸러져 도달하지
        # 않는다 — dev 레인 전용 도달 경로다.)
        raise AuthError(f"unknown sub_type: {sub_type}")
    if role == ROLE_GUEST:
        return Identity(
            user_id=None,
            is_guest=True,
            seller_id=None,
            subject=subject,
            session_id=session_id,
        )
    # dev 전용 legacy role 폴백: 로컬 기존 토큰은 미지 role도 회원으로 관용한다.
    return Identity(
        user_id=subject,
        is_guest=False,
        seller_id=None,
        subject=subject,
        session_id=session_id,
    )


def _verify_scope(claims: dict, required: str) -> None:
    """scope 클레임은 확정된 단일 문자열과 정확히 일치해야 한다."""
    if claims.get(CLAIM_SCOPE) != required:
        raise AuthError("missing or mismatched scope")


@lru_cache
def _jwk_client(
    jwks_url: str, timeout_s: float | None = None, cache_ttl_s: float | None = None
) -> PyJWKClient:
    """JWKS 클라이언트 캐시. kid→공개키 조회를 재사용한다 (요청마다 재페치 방지).

    - timeout_s: JWKS fetch HTTP 타임아웃 — AI→Spring 전 구간 3s 기준(§2.9 c) config 주입.
    - cache_ttl_s: tier-1 JWKS 캐시 TTL — 만료 전에는 kid miss 시에만 refetch(§2.3).
    미지정(None)이면 PyJWKClient 기본값을 쓴다 (dev 등 비주입 경로 호환).
    """
    kwargs: dict = {}
    if timeout_s is not None:
        kwargs["timeout"] = timeout_s
    if cache_ttl_s is not None:
        kwargs["lifespan"] = cache_ttl_s
    return PyJWKClient(jwks_url, **kwargs)


def decode_token(
    token: str | None,
    *,
    auth_mode: str,
    jwks_url: str | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    scope: str | None = None,
    jwks_timeout_s: float | None = None,
    jwks_cache_ttl_s: float | None = None,
) -> Identity:
    """Bearer 토큰을 인증 모드에 따라 디코드/검증하고 Identity 를 반환한다.

    dev 모드에서 토큰이 없으면 게스트 Identity 를 돌려준다 (헤더 없는 로컬 호출 편의).
    그 외에는 토큰이 없거나 검증 실패 시 AuthError (만료는 TokenExpiredError).

    jwks 모드 검증 항목(§2.3 확정): signature / exp / iss / aud / scope.
    JWKS scope는 호출자 설정과 무관하게 exact ``chat:stream``을 항상 검증한다.
    """
    if auth_mode == "dev":
        if not token:
            # dev 전용 편의: 헤더 없으면 게스트로 취급. subject 는 의도적으로 None 이다 —
            # 무토큰 익명은 식별 근거가 없어 registry_key owner 가 "anon" 으로 공유된다.
            # (프로덕션 jwks 모드는 무토큰이 401 이라 이 경로에 도달하지 않고, 실제 게스트는
            #  익명 JWT 의 sub 로 개별 스코프된다.) 여기에 요청마다 고유 subject 를 부여하면
            # 방당 1스트림 제한(§2.9 a)이 익명에서 무력화되므로 그렇게 하지 않는다.
            return Identity(user_id=None, is_guest=True, seller_id=None)
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise AuthError("invalid token") from exc
        return _claims_to_identity(claims)

    if auth_mode == "jwks":
        if not token:
            raise AuthError("missing token")
        if not jwks_url:
            raise AuthError("server misconfigured: JWKS_URL unset")
        try:
            client = _jwk_client(jwks_url, jwks_timeout_s, jwks_cache_ttl_s)
            # kid→공개키 매칭. kid 가 캐시된 JWKS 에 없으면 PyJWKClient 가 1회 refetch
            # 후 재시도한다(§2.3 "kid miss 시에만 refetch"). JWKS 도달 불가/kid 부재는
            # PyJWKClientError(PyJWTError 하위) → AuthError → 401 (fail-closed).
            signing_key = client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=issuer,
                audience=audience,
                options={
                    # sub 부재 시 신원 도출 자체가 불가 — 필수 클레임(§2.3)로 강제.
                    "require": ["exp", "sub"],
                    "verify_iss": issuer is not None,
                    "verify_aud": audience is not None,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError("token expired") from exc
        except jwt.PyJWTError as exc:
            raise AuthError("invalid token") from exc
        _verify_scope(claims, STREAM_SCOPE)
        # 실배선 레인 — 신원 유형 클레임(sub_type·role) 전무 토큰은 fail-closed.
        return _claims_to_identity(claims, require_identity_claim=True)

    raise AuthError(f"unknown auth_mode: {auth_mode}")
