"""JWKS(RS256) 인증 모드 단위 테스트 (api-spec §2.3, #34 실배선).

RSA 키페어로 실 JWKS dict 를 구성하고 PyJWKClient 의 HTTP fetch 계층만 패치한다 —
kid→공개키 매칭·JWK 파싱·kid miss refetch 가 실제 라이브러리 경로로 돈다(tests/unit/_jwks.py).

검증 항목(§2.3 확정): signature / exp / iss / aud / scope.
클레임 매핑: JWKS buyer exact sub_type(member|guest), seller exact lowercase role="seller".
legacy role 폴백은 dev decode 전용이며 이 JWKS 모듈에서는 허용하지 않는다.
"""

from __future__ import annotations

import datetime as dt

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core import auth
from app.core.auth import AuthError, TokenExpiredError, decode_token
from tests.unit._jwks import (
    AUDIENCE,
    ISSUER,
    JWKS_URL,
    KID,
    SCOPE,
    install_jwks_fetch,
    jwks_of,
    make_rsa_key,
    seller_ticket_claims,
    sign_ticket,
    ticket_claims,
)


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    """테스트용 RSA 개인키 (모듈 공유 — 키 생성 비용 절약)."""
    return make_rsa_key()


@pytest.fixture(autouse=True)
def _fresh_jwk_client_cache():
    """_jwk_client lru_cache 를 테스트마다 비워 JWKS 캐시 상태 누수를 막는다."""
    auth._jwk_client.cache_clear()
    yield
    auth._jwk_client.cache_clear()


@pytest.fixture
def jwks_calls(monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey) -> dict:
    """기본 JWKS(단일 kid)를 서빙하고 fetch 횟수를 센다."""
    return install_jwks_fetch(monkeypatch, lambda: jwks_of((rsa_key, KID)))


def _decode(token: str | None, *, scope: str | None = SCOPE):
    return decode_token(
        token,
        auth_mode="jwks",
        jwks_url=JWKS_URL,
        issuer=ISSUER,
        audience=AUDIENCE,
        scope=scope,
        jwks_timeout_s=3.0,
        jwks_cache_ttl_s=300.0,
    )


# ── 티켓 클레임 매핑 (§2.3: buyer exact sub_type, seller exact lowercase role) ──


def test_member_ticket_maps_to_member(rsa_key, jwks_calls) -> None:
    """sub_type=member 티켓 → 회원 Identity (user_id=sub, 숫자 문자열)."""
    identity = _decode(sign_ticket(rsa_key, KID, ticket_claims(sub="42")))
    assert identity.user_id == "42"
    assert identity.is_guest is False
    assert identity.seller_id is None
    assert identity.subject == "42"


def test_guest_ticket_maps_to_guest(rsa_key, jwks_calls) -> None:
    """sub_type=guest 티켓(sub=UUID) → 게스트 Identity (user_id 없음, subject 보존)."""
    guest_uuid = "3f2b8a54-8f2e-4b1a-9c60-000000000001"
    identity = _decode(sign_ticket(rsa_key, KID, ticket_claims(sub=guest_uuid, sub_type="guest")))
    assert identity.user_id is None
    assert identity.is_guest is True
    assert identity.subject == guest_uuid


def test_unknown_sub_type_rejected(rsa_key, jwks_calls) -> None:
    """미지 sub_type 값은 fail-closed 거부 (member|guest 만 정본, §2.3)."""
    token = sign_ticket(rsa_key, KID, ticket_claims(sub_type="admin"))
    with pytest.raises(AuthError):
        _decode(token)


@pytest.mark.parametrize("sub_type", [[], {}])
def test_non_string_sub_type_is_rejected_as_auth_error(rsa_key, jwks_calls, sub_type) -> None:
    """JSON 배열·객체 discriminator도 500이 아니라 인증 오류로 fail-closed 한다."""
    token = sign_ticket(rsa_key, KID, ticket_claims(sub_type=sub_type))
    with pytest.raises(AuthError):
        _decode(token)


def test_jwks_legacy_role_user_is_rejected(rsa_key, jwks_calls) -> None:
    """JWKS에서 sub_type 없는 구 role=USER 토큰은 fail-closed 한다."""
    claims = ticket_claims(sub="7")
    del claims["sub_type"]
    claims["role"] = auth.ROLE_USER
    with pytest.raises(AuthError):
        _decode(sign_ticket(rsa_key, KID, claims))


def test_jwks_legacy_role_guest_is_rejected(rsa_key, jwks_calls) -> None:
    """JWKS에서 sub_type 없는 구 role=GUEST 토큰은 fail-closed 한다."""
    claims = ticket_claims()
    del claims["sub_type"]
    claims["role"] = auth.ROLE_GUEST
    with pytest.raises(AuthError):
        _decode(sign_ticket(rsa_key, KID, claims))


def test_seller_role_with_brand_id(rsa_key, jwks_calls) -> None:
    """확정 role="seller" + brandId → 판매자 스코프와 brand_id를 보존한다."""
    claims = seller_ticket_claims(sub="9", brandId=77)
    identity = _decode(sign_ticket(rsa_key, KID, claims))
    assert identity.seller_id == "9"
    assert identity.brand_id == 77
    assert identity.is_guest is False


def test_exact_lowercase_seller_role_accepted(rsa_key, jwks_calls) -> None:
    """확정값 role="seller" 소문자 정확 일치만 판매자 스코프를 연다."""
    claims = seller_ticket_claims(sub="9", brandId=77)
    identity = _decode(sign_ticket(rsa_key, KID, claims))
    assert identity.seller_id == "9"
    assert identity.brand_id == 77


@pytest.mark.parametrize(
    "brand_id",
    [None, "77", 77.0, True, False, [], {}, 0, -1, 2**63],
)
def test_seller_brand_id_must_be_positive_bigint_integer(
    rsa_key,
    jwks_calls,
    brand_id: object,
) -> None:
    """seller brandId는 bool/coercion 없이 양의 PostgreSQL BIGINT 정수만 허용한다."""
    token = sign_ticket(
        rsa_key,
        KID,
        seller_ticket_claims(sub="9", brandId=brand_id),
    )

    with pytest.raises(AuthError):
        _decode(token)


@pytest.mark.parametrize("subject", ["", "0", "-1", "seller-9", str(2**63)])
def test_seller_subject_must_be_positive_bigint_string(
    rsa_key,
    jwks_calls,
    subject: str,
) -> None:
    """seller sub는 seller_id로 쓰이므로 양의 BIGINT 숫자 문자열이어야 한다."""
    token = sign_ticket(
        rsa_key,
        KID,
        seller_ticket_claims(sub=subject, brandId=77),
    )

    with pytest.raises(AuthError):
        _decode(token)


@pytest.mark.parametrize("sub_type", ["member", "guest", "admin"])
def test_seller_role_rejects_any_buyer_sub_type(rsa_key, jwks_calls, sub_type: str) -> None:
    """판매자 discriminator와 buyer discriminator가 함께 있으면 값과 무관하게 거부한다."""
    claims = ticket_claims(sub="9", sub_type=sub_type)
    claims.update({"role": auth.ROLE_SELLER, "brandId": "77"})

    with pytest.raises(AuthError):
        _decode(sign_ticket(rsa_key, KID, claims))


@pytest.mark.parametrize("sub_type", ["member", "guest"])
def test_buyer_sub_type_rejects_any_role_claim(
    rsa_key,
    jwks_calls,
    sub_type: str,
) -> None:
    """buyer 티켓은 role 클레임 자체가 없어야 한다."""
    claims = ticket_claims(sub_type=sub_type)
    claims["role"] = auth.ROLE_USER

    with pytest.raises(AuthError):
        _decode(sign_ticket(rsa_key, KID, claims))


@pytest.mark.parametrize(
    ("role_present", "role", "sub_type_present", "sub_type"),
    [
        (True, None, True, "member"),
        (True, "seller", True, None),
        (True, None, True, None),
        (True, None, False, None),
        (False, None, True, None),
    ],
)
def test_jwks_discriminator_null_presence_is_rejected(
    rsa_key,
    jwks_calls,
    role_present: bool,
    role: str | None,
    sub_type_present: bool,
    sub_type: str | None,
) -> None:
    """운영 discriminator는 값뿐 아니라 key presence도 정확히 한 쪽이어야 한다."""
    claims = ticket_claims()
    claims.pop("role", None)
    claims.pop("sub_type", None)
    if role_present:
        claims["role"] = role
    if sub_type_present:
        claims["sub_type"] = sub_type

    with pytest.raises(AuthError):
        _decode(sign_ticket(rsa_key, KID, claims))


def test_token_without_identity_claims_rejected(rsa_key, jwks_calls) -> None:
    """sub_type·role 둘 다 없는 서명 유효 토큰 → 거부 (jwks 레인 fail-closed, 리뷰 반영).

    §2.3 은 sub_type 을 티켓 필수 클레임으로 확정 — 신원 유형 클레임이 전무한 토큰을
    회원으로 기본 승인하지 않는다 (미지 sub_type 거부와 방어 원칙 일관).
    """
    claims = ticket_claims()
    del claims["sub_type"]
    token = sign_ticket(rsa_key, KID, claims)
    with pytest.raises(AuthError):
        _decode(token)


@pytest.mark.parametrize("empty_role", ["", "   "])
def test_empty_role_without_sub_type_rejected(rsa_key, jwks_calls, empty_role) -> None:
    """role 이 빈/공백 문자열이고 sub_type 없음 → 거부 (fail-closed 가드 우회 방지, 리뷰 3R).

    ""(빈 문자열)는 None 이 아니라 `role is None` 가드를 지나칠 수 있다 — _norm_role 이
    빈/공백을 "role 없음"(None)으로 정규화해 회원 기본 승인 구멍을 막는다.
    """
    claims = ticket_claims()
    del claims["sub_type"]
    claims["role"] = empty_role
    token = sign_ticket(rsa_key, KID, claims)
    with pytest.raises(AuthError):
        _decode(token)


def test_jwks_unrecognized_role_is_rejected(rsa_key, jwks_calls) -> None:
    """미지 role은 buyer sub_type 정본을 대신할 수 없다."""
    claims = ticket_claims(sub="42")
    del claims["sub_type"]
    claims["role"] = "MEMBER"
    with pytest.raises(AuthError):
        _decode(sign_ticket(rsa_key, KID, claims))


# ── 검증 항목: signature / exp / iss / aud / scope (§2.3 확정) ──


def test_expired_ticket_raises_typed_error(rsa_key, jwks_calls) -> None:
    """만료 티켓 → TokenExpiredError (deps 가 401 TOKEN_EXPIRED 로 매핑, §2.5)."""
    now = dt.datetime.now(tz=dt.timezone.utc)
    token = sign_ticket(rsa_key, KID, ticket_claims(exp=now - dt.timedelta(seconds=1)))
    with pytest.raises(TokenExpiredError):
        _decode(token)


def test_expired_error_is_auth_error(rsa_key, jwks_calls) -> None:
    """TokenExpiredError 는 AuthError 하위 타입 — 기존 except AuthError 경로 호환."""
    assert issubclass(TokenExpiredError, AuthError)


def test_wrong_audience_rejected(rsa_key, jwks_calls) -> None:
    """aud 불일치(로그인 AT 혼용 방지) → AuthError."""
    token = sign_ticket(rsa_key, KID, ticket_claims(aud="shopping-spring-api"))
    with pytest.raises(AuthError):
        _decode(token)


def test_wrong_issuer_rejected(rsa_key, jwks_calls) -> None:
    """iss 불일치 → AuthError."""
    token = sign_ticket(rsa_key, KID, ticket_claims(iss="evil-issuer"))
    with pytest.raises(AuthError):
        _decode(token)


def test_wrong_signature_rejected(jwks_calls) -> None:
    """JWKS 에 없는 키로 서명(같은 kid 참칭) → 서명 검증 실패 AuthError."""
    other_key = make_rsa_key()
    token = sign_ticket(other_key, KID, ticket_claims())
    with pytest.raises(AuthError):
        _decode(token)


def test_scope_mismatch_rejected(rsa_key, jwks_calls) -> None:
    """scope 불일치(다른 용도 토큰 혼용) → AuthError (§2.3 검증 항목)."""
    token = sign_ticket(rsa_key, KID, ticket_claims(scope="profile:read"))
    with pytest.raises(AuthError):
        _decode(token)


def test_scope_missing_rejected(rsa_key, jwks_calls) -> None:
    """scope 클레임 누락 → AuthError (검증 요구 시 필수)."""
    claims = ticket_claims()
    del claims["scope"]
    token = sign_ticket(rsa_key, KID, claims)
    with pytest.raises(AuthError):
        _decode(token)


@pytest.mark.parametrize(
    "scope",
    ["chat:stream other", ["chat:stream"], ["chat:stream", "other"], 1, True, None, {}],
)
def test_scope_must_be_exact_string(rsa_key, jwks_calls, scope: object) -> None:
    """scope는 복합/비문자 표현을 허용하지 않고 exact chat:stream 문자열만 받는다."""
    token = sign_ticket(rsa_key, KID, ticket_claims(scope=scope))
    with pytest.raises(AuthError):
        _decode(token)


def test_scope_check_cannot_be_disabled_by_none(rsa_key, jwks_calls) -> None:
    """JWKS 호출자가 scope=None을 넘겨도 exact 운영 scope 검증은 비활성화되지 않는다."""
    claims = ticket_claims()
    del claims["scope"]
    token = sign_ticket(rsa_key, KID, claims)
    with pytest.raises(AuthError):
        _decode(token, scope=None)


def test_missing_sub_rejected(rsa_key, jwks_calls) -> None:
    """sub 누락 → AuthError (신원 도출 불가 — 필수 클레임, §2.3)."""
    claims = ticket_claims()
    del claims["sub"]
    token = sign_ticket(rsa_key, KID, claims)
    with pytest.raises(AuthError):
        _decode(token)


def test_missing_token_rejected(jwks_calls) -> None:
    """jwks 모드에서 토큰 없음 → AuthError."""
    with pytest.raises(AuthError):
        _decode(None)


# ── JWKS 캐시·refetch (§2.3: kid miss 시에만 refetch, 요청마다 왕복 금지) ──


def test_jwks_cache_reused_between_decodes(rsa_key, jwks_calls) -> None:
    """같은 kid 반복 검증은 최초 1회만 fetch — 캐시 재사용."""
    _decode(sign_ticket(rsa_key, KID, ticket_claims()))
    _decode(sign_ticket(rsa_key, KID, ticket_claims(sub="43")))
    assert jwks_calls["count"] == 1


def test_kid_miss_triggers_refetch(monkeypatch, rsa_key) -> None:
    """키 회전으로 새 kid 도착 → 캐시 miss → refetch 1회 후 성공 (§2.3)."""
    key_b = make_rsa_key()
    state = {"jwks": jwks_of((rsa_key, KID))}
    calls = install_jwks_fetch(monkeypatch, lambda: state["jwks"])

    assert _decode(sign_ticket(rsa_key, KID, ticket_claims())).user_id == "42"
    assert calls["count"] == 1

    # Spring 키 회전: JWKS 에 kid-b 추가 후 kid-b 서명 티켓 도착.
    state["jwks"] = jwks_of((rsa_key, KID), (key_b, "kid-2026-b"))
    identity = _decode(sign_ticket(key_b, "kid-2026-b", ticket_claims(sub="77")))
    assert identity.user_id == "77"
    assert calls["count"] == 2


def test_unknown_kid_rejected_after_refetch(rsa_key, jwks_calls) -> None:
    """JWKS 에 끝내 없는 kid → refetch 후에도 실패 → AuthError (401 매핑)."""
    other_key = make_rsa_key()
    token = sign_ticket(other_key, "kid-unknown", ticket_claims())
    with pytest.raises(AuthError):
        _decode(token)


# ── 클라이언트 튜너블 주입 (§2.9 c: AI→Spring 3s, 캐시 TTL config) ──


def test_jwk_client_injects_timeout_and_ttl() -> None:
    """_jwk_client 가 JWKS fetch 타임아웃·캐시 TTL 을 config 값으로 주입한다."""
    client = auth._jwk_client(JWKS_URL, timeout_s=3.0, cache_ttl_s=120.0)
    assert client.timeout == pytest.approx(3.0)
    assert client.jwk_set_cache is not None
    assert client.jwk_set_cache.lifespan == pytest.approx(120.0)
