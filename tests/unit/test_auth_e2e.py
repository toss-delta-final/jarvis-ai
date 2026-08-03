"""인증 실배선 앱 레벨 검증 (#34, api-spec §2.3/§2.5) — jwks 모드 401/403 봉투 + 서비스 토큰 레인.

FastAPI 앱을 jwks 설정으로 구동해 실 RS256 티켓 검증 흐름을 확인한다:
  - §2.3(a) 사용자 티켓: 무토큰/무효 401 TOKEN_INVALID · 만료 401 TOKEN_EXPIRED ·
    유효 티켓 SSE 200 · /seller/chat 스코프 403 FORBIDDEN (§2.5 봉투 + requestId)
  - §2.3(b) 서비스 토큰: 인바운드 verify_service_token fail-closed ·
    아웃바운드 spring_client X-Internal-Token 부착
  - jwks 모드 기동 검증(Settings validator): jwks_url·pepper·internal 토큰 필수

tests/integration/ E2E 하니스(#35)와 별개의 순수 단위 검증 — 라이브 의존 없음.
"""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from app.api import deps
from app.core import auth
from app.core.config import Settings
from app.main import app
from tests.unit._jwks import (
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

client = TestClient(app)


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    """테스트용 RSA 개인키 (모듈 공유)."""
    return make_rsa_key()


def _jwks_settings(**overrides) -> Settings:
    """jwks 모드 Settings — 운영 필수값(pepper·internal 토큰·jwks_url·google_api_key) 포함, .env 미참조."""
    kwargs = dict(
        _env_file=None,
        auth_mode="jwks",
        jwks_url=JWKS_URL,
        jwt_scope=SCOPE,
        pii_hash_pepper="test-pepper",
        internal_api_token="svc-token",
        google_api_key="test-google-key",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.fixture
def jwks_app(monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey) -> Settings:
    """deps 를 jwks 설정으로 패치하고 JWKS fetch 를 로컬 키페어로 고정한다."""
    settings = _jwks_settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    auth._jwk_client.cache_clear()
    install_jwks_fetch(monkeypatch, lambda: jwks_of((rsa_key, KID)))
    yield settings
    auth._jwk_client.cache_clear()


def _chat_body() -> dict:
    return {"sessionId": "s-auth-1", "threadId": "t-auth-1", "message": "여행용 파우치 추천해줘"}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── §2.3(a) FE→AI 사용자 티켓: 401/403 봉투 (§2.5) ──


def test_chat_without_token_returns_401_invalid(jwks_app) -> None:
    """jwks 모드 무토큰 → 401 TOKEN_INVALID 봉투 + requestId (dev 게스트 우회 없음)."""
    resp = client.post("/chat", json=_chat_body())
    assert resp.status_code == 401
    body = resp.json()["error"]
    assert body["code"] == "TOKEN_INVALID"
    assert body["requestId"]


def test_chat_with_expired_ticket_returns_401_expired(jwks_app, rsa_key) -> None:
    """만료 티켓 → 401 TOKEN_EXPIRED — FE 는 CH-1b 재발급 후 1회 재시도 (§2.3/§2.5)."""
    now = dt.datetime.now(tz=dt.timezone.utc)
    token = sign_ticket(rsa_key, KID, ticket_claims(exp=now - dt.timedelta(seconds=1)))
    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_EXPIRED"


def test_chat_with_wrong_scope_returns_401(jwks_app, rsa_key) -> None:
    """scope 불일치 티켓(용도 혼용) → 401 TOKEN_INVALID (§2.3 검증 항목)."""
    token = sign_ticket(rsa_key, KID, ticket_claims(scope="profile:read"))
    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


@pytest.mark.parametrize(
    "scope",
    [
        pytest.param(None, id="null"),
        pytest.param("", id="empty"),
        pytest.param("chat:stream other", id="composite-string"),
        pytest.param(["chat:stream"], id="list"),
        pytest.param(1, id="number"),
        pytest.param(True, id="bool"),
        pytest.param({}, id="object"),
    ],
)
def test_chat_scope_requires_exact_signed_string(
    jwks_app,
    rsa_key,
    scope: object,
) -> None:
    """실 signed HTTP에서 exact 문자열 외 scope는 모두 401 TOKEN_INVALID다."""
    token = sign_ticket(
        rsa_key,
        KID,
        ticket_claims(scope=scope, sessionId="s-auth-1"),
    )

    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


def test_chat_missing_scope_returns_401(jwks_app, rsa_key) -> None:
    """실 signed HTTP에서 scope key 누락도 TOKEN_INVALID다."""
    claims = ticket_claims(sessionId="s-auth-1")
    claims.pop("scope")
    token = sign_ticket(rsa_key, KID, claims)

    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


def test_chat_with_garbage_token_returns_401(jwks_app) -> None:
    """형식 불량 토큰 → 401 TOKEN_INVALID."""
    resp = client.post("/chat", json=_chat_body(), headers=_bearer("not-a-jwt"))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


def test_chat_with_valid_member_ticket_streams(jwks_app, rsa_key, buyer_fakes) -> None:
    """유효 회원 티켓 → SSE 200 스트리밍 (실 JWT 검증 통과 후 그래프 구동)."""
    token = sign_ticket(rsa_key, KID, ticket_claims(sub="42", sessionId="s-auth-1"))
    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


@pytest.mark.parametrize("legacy_role", [auth.ROLE_GUEST, auth.ROLE_USER, "UNKNOWN"])
def test_jwks_buyer_ticket_requires_exact_sub_type(
    jwks_app,
    rsa_key,
    legacy_role: str,
) -> None:
    """실배선 buyer는 legacy/미지 role로 sub_type 정본을 우회할 수 없다."""
    claims = ticket_claims(sub="42", sessionId="s-auth-1")
    claims.pop("sub_type")
    claims["role"] = legacy_role
    token = sign_ticket(rsa_key, KID, claims)

    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


@pytest.mark.parametrize("malformed_sub_type", [[], {}, ["member"], 1, True, None])
def test_jwks_chat_rejects_non_string_sub_type_with_401_envelope(
    jwks_app,
    rsa_key,
    malformed_sub_type: object,
) -> None:
    """JSON composite/number/bool discriminator는 TypeError/500 없이 TOKEN_INVALID다."""
    token = sign_ticket(
        rsa_key,
        KID,
        ticket_claims(
            sub="42",
            sub_type=malformed_sub_type,
            sessionId="s-auth-1",
        ),
    )

    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


@pytest.mark.parametrize("malformed_role", [[], {}, ["seller"], 1, True, None])
def test_jwks_chat_rejects_non_string_role_with_401_envelope(
    jwks_app,
    rsa_key,
    malformed_role: object,
) -> None:
    """seller role도 non-string JSON 값이면 예외 누출 없이 fail-closed 한다."""
    claims = ticket_claims(sub="9", sessionId="s-auth-1")
    claims.pop("sub_type")
    claims["role"] = malformed_role
    token = sign_ticket(rsa_key, KID, claims)

    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


def test_dev_buyer_keeps_legacy_role_compatibility(monkeypatch, rsa_key, buyer_fakes) -> None:
    """dev 레인은 기존 GUEST/USER role 토큰 호환을 유지한다."""
    monkeypatch.setattr(deps, "get_settings", lambda: Settings(_env_file=None, auth_mode="dev"))
    claims = ticket_claims(sub="legacy-user", sessionId="s-auth-1")
    claims.pop("sub_type")
    claims["role"] = auth.ROLE_USER
    token = sign_ticket(rsa_key, KID, claims)

    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 200


def test_adoption_failure_before_first_frame_maps_to_state_unavailable(
    jwks_app,
    rsa_key,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """accepted commit 뒤 adoption 실패는 SSE 200이 아니라 중앙 503 봉투로 응답한다."""
    from app.agents.buyer import graph as buyer_graph
    from app.core.session_context import SessionStateUnavailable

    async def fail_adoption(*_args, **_kwargs):
        raise SessionStateUnavailable

    monkeypatch.setattr(buyer_graph, "ensure_thread_adopted", fail_adoption)
    token = sign_ticket(rsa_key, KID, ticket_claims(sub="42", sessionId="s-auth-1"))

    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]["code"] == "STATE_UNAVAILABLE"


def test_adoption_programming_error_before_first_frame_is_not_masked(
    jwks_app,
    rsa_key,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """public chat 경로도 adoption programming 오류를 STATE_UNAVAILABLE로 오분류하지 않는다."""
    import psycopg

    from app.agents.buyer import session_state

    async def fail_resolution(*_args, **_kwargs):
        raise psycopg.ProgrammingError("bad adoption query")

    monkeypatch.setattr(session_state, "_resolve_context_and_legacy_owner", fail_resolution)
    token = sign_ticket(rsa_key, KID, ticket_claims(sub="42", sessionId="s-auth-1"))

    with pytest.raises(psycopg.ProgrammingError):
        client.post("/chat", json=_chat_body(), headers=_bearer(token))


def test_buyer_session_claim_must_match_body(jwks_app, rsa_key) -> None:
    """구매자 티켓의 서명된 sessionId와 body sessionId가 다르면 403이다."""
    token = sign_ticket(rsa_key, KID, ticket_claims(sub="42", sessionId="S1"))
    body = {"sessionId": "S2", "threadId": "T1", "message": "hello"}

    resp = client.post("/chat", json=body, headers=_bearer(token))

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "SESSION_FORBIDDEN"


def test_buyer_session_claim_is_required_in_jwks_mode(jwks_app, rsa_key) -> None:
    """운영(jwks) 구매자 티켓에 sessionId가 없으면 fail-closed 403이다."""
    token = sign_ticket(rsa_key, KID, ticket_claims(sub="42"))

    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "SESSION_FORBIDDEN"


def test_dev_token_session_mismatch_is_rejected(monkeypatch, rsa_key) -> None:
    """dev 토큰도 sessionId를 주장했다면 body 불일치를 우회할 수 없다."""
    settings = Settings(_env_file=None, auth_mode="dev")
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    token = sign_ticket(rsa_key, KID, ticket_claims(sub="42", sessionId="other-session"))

    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "SESSION_FORBIDDEN"


def test_dev_no_token_streams_and_uses_dev_anon_owner(monkeypatch, buyer_fakes) -> None:
    """로컬 무토큰 경로는 스트리밍을 허용하고 안정적인 dev-anon 소유자를 쓴다."""
    settings = Settings(_env_file=None, auth_mode="dev")
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    identity = auth.decode_token(None, auth_mode="dev")

    assert deps.require_buyer_session(identity, "s-auth-1", settings) is None
    assert deps.buyer_owner_id(identity, settings) == "dev-anon"
    resp = client.post("/chat", json=_chat_body())
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


def test_seller_chat_with_member_ticket_returns_403(jwks_app, rsa_key) -> None:
    """판매자 스코프 없는 티켓의 /seller/chat → 403 FORBIDDEN 봉투 (§2.3)."""
    token = sign_ticket(rsa_key, KID, ticket_claims(sub="42"))
    resp = client.post("/seller/chat", json=_chat_body(), headers=_bearer(token))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_seller_chat_without_brand_id_returns_401(jwks_app, rsa_key) -> None:
    """seller brandId 누락은 decode 신원 경계에서 TOKEN_INVALID로 거부한다."""
    claims = seller_ticket_claims(sub="9")
    claims.pop("brandId")
    token = sign_ticket(rsa_key, KID, claims)
    resp = client.post("/seller/chat", json=_chat_body(), headers=_bearer(token))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


def test_seller_ticket_without_buyer_session_claim_streams(jwks_app, rsa_key) -> None:
    """판매자 티켓에는 구매자 sessionId claim을 추가로 요구하지 않는다."""
    claims = seller_ticket_claims(sub="9", brandId=3)
    token = sign_ticket(rsa_key, KID, claims)

    resp = client.post("/seller/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


@pytest.mark.parametrize(
    "brand_id",
    [None, "3", 3.0, True, False, [], {}, 0, -1, 2**63],
)
def test_seller_malformed_brand_id_returns_401_before_backend(
    jwks_app,
    rsa_key,
    monkeypatch: pytest.MonkeyPatch,
    brand_id: object,
) -> None:
    """malformed brandId는 seller coercion/backend 진입 전에 TOKEN_INVALID로 닫힌다."""
    from app.api import seller as seller_api

    async def fail_if_streamed(*_args, **_kwargs):
        raise AssertionError("malformed seller identity must not enter seller stream")
        yield  # pragma: no cover

    monkeypatch.setattr(seller_api, "_seller_stream", fail_if_streamed)
    token = sign_ticket(
        rsa_key,
        KID,
        seller_ticket_claims(sub="9", brandId=brand_id),
    )

    resp = client.post("/seller/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


@pytest.mark.parametrize("subject", ["", "0", "-1", "seller-9", str(2**63)])
def test_seller_malformed_subject_returns_401_before_backend(
    jwks_app,
    rsa_key,
    monkeypatch: pytest.MonkeyPatch,
    subject: str,
) -> None:
    """seller_id로 쓰는 sub가 양의 BIGINT 문자열이 아니면 backend 호출 없이 401이다."""
    from app.api import seller as seller_api

    async def fail_if_streamed(*_args, **_kwargs):
        raise AssertionError("malformed seller identity must not enter seller stream")
        yield  # pragma: no cover

    monkeypatch.setattr(seller_api, "_seller_stream", fail_if_streamed)
    token = sign_ticket(
        rsa_key,
        KID,
        seller_ticket_claims(sub=subject, brandId=3),
    )

    resp = client.post("/seller/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


def test_seller_ticket_is_rejected_from_buyer_chat_before_state_access(
    jwks_app,
    rsa_key,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """서명 sessionId가 있어도 seller는 buyer 저장소/수명주기에 닿기 전에 403이다."""
    from app.api import chat as chat_api

    async def fail_if_state_accessed():
        raise AssertionError("seller must not access buyer conversation state")

    monkeypatch.setattr(chat_api, "get_conversation_store", fail_if_state_accessed)
    claims = seller_ticket_claims(sub="9", sessionId="s-auth-1", brandId=3)
    token = sign_ticket(rsa_key, KID, claims)

    resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("pg-profile deadline"),
        PoolTimeout("pg-profile pool exhausted"),
        psycopg.OperationalError("pg-profile connection reset"),
    ],
)
def test_seller_store_io_failure_maps_to_state_unavailable(
    jwks_app,
    rsa_key,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """대화 store I/O 장애는 구매자 /chat 과 같은 503 STATE_UNAVAILABLE 봉투로 나간다."""
    from app.api import seller as seller_api

    async def fail_store():
        raise failure

    monkeypatch.setattr(seller_api, "get_conversation_store", fail_store)
    token = sign_ticket(rsa_key, KID, seller_ticket_claims(sub="9", brandId=3))

    resp = client.post("/seller/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]["code"] == "STATE_UNAVAILABLE"


def test_seller_store_programming_error_is_not_masked_as_state_unavailable(
    jwks_app,
    rsa_key,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """코드 버그는 503 으로 마스킹하지 않는다 — 실 I/O 장애만 STATE_UNAVAILABLE 이다."""
    from app.api import seller as seller_api

    async def fail_store():
        raise psycopg.ProgrammingError("bad conversation query")

    monkeypatch.setattr(seller_api, "get_conversation_store", fail_store)
    token = sign_ticket(rsa_key, KID, seller_ticket_claims(sub="9", brandId=3))

    with pytest.raises(psycopg.ProgrammingError):
        client.post("/seller/chat", json=_chat_body(), headers=_bearer(token))


def test_jwks_uppercase_seller_role_is_not_accepted(jwks_app, rsa_key) -> None:
    """BE 확정값과 다른 대문자 SELLER는 판매자 경로를 열지 않는다."""
    claims = seller_ticket_claims(sub="9", role="SELLER", brandId=3)
    token = sign_ticket(rsa_key, KID, claims)

    resp = client.post("/seller/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


# ── §2.3(b) 인바운드 서비스 토큰 (Spring→AI) — fail-closed ──


def test_service_token_match_passes(monkeypatch) -> None:
    """jwks 모드 + 토큰 일치 → 통과."""
    monkeypatch.setattr(deps, "get_settings", lambda: _jwks_settings())
    assert deps.verify_service_token(x_internal_token="svc-token") is None


def test_service_token_mismatch_401(monkeypatch) -> None:
    """jwks 모드 + 토큰 불일치 → 401 INTERNAL_TOKEN_INVALID."""
    monkeypatch.setattr(deps, "get_settings", lambda: _jwks_settings())
    with pytest.raises(HTTPException) as exc:
        deps.verify_service_token(x_internal_token="wrong")
    assert exc.value.status_code == 401


def test_service_token_missing_401(monkeypatch) -> None:
    """jwks 모드 + 헤더 누락 → 401 (fail-closed)."""
    monkeypatch.setattr(deps, "get_settings", lambda: _jwks_settings())
    with pytest.raises(HTTPException) as exc:
        deps.verify_service_token(x_internal_token=None)
    assert exc.value.status_code == 401


def test_service_token_dev_mode_bypasses(monkeypatch) -> None:
    """dev 모드는 미검증 편의 허용 (로컬 전용)."""
    monkeypatch.setattr(deps, "get_settings", lambda: Settings(_env_file=None, auth_mode="dev"))
    assert deps.verify_service_token(x_internal_token=None) is None


# ── §2.3(b) 아웃바운드 서비스 토큰 (AI→Spring) — X-Internal-Token 부착 ──


def test_module_client_attaches_internal_token(monkeypatch) -> None:
    """spring_client._client() 가 X-Internal-Token 헤더 + 3s 타임아웃을 부착한다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc, "get_settings", lambda: Settings(_env_file=None, internal_api_token="svc-token")
    )
    http_client = sc._client()
    assert http_client.headers["X-Internal-Token"] == "svc-token"
    assert http_client.timeout.read == pytest.approx(3.0)


# ── jwks 모드 기동 검증 (Settings validator — 조용한 미설정 방지) ──


def test_settings_jwks_requires_jwks_url() -> None:
    """auth_mode=jwks 인데 JWKS_URL 미설정 → 기동 실패 (런타임 401 폭주 대신 fail-fast)."""
    with pytest.raises(ValueError):
        _jwks_settings(jwks_url=None)


def test_settings_jwks_requires_internal_token() -> None:
    """auth_mode=jwks 인데 INTERNAL_API_TOKEN 미설정 → 기동 실패 (기존 규칙 회귀 가드)."""
    with pytest.raises(ValueError):
        _jwks_settings(internal_api_token="")


def test_settings_jwks_requires_google_api_key() -> None:
    """auth_mode=jwks 인데 GOOGLE_API_KEY 미설정 → 기동 실패 (PR #42 리뷰 — 배치가 조용히
    영원히 실패하는 대신 fail-fast, 이슈 #31)."""
    with pytest.raises(ValueError):
        _jwks_settings(google_api_key="")


def test_settings_jwt_scope_defaults_to_exact_stream_scope() -> None:
    """운영 스트림 scope 확정값은 설정 누락으로 비활성화될 수 없다."""
    assert Settings(_env_file=None).jwt_scope == "chat:stream"


@pytest.mark.parametrize("scope", [None, "", "profile:read", "chat:stream other"])
def test_settings_jwks_rejects_non_exact_scope(scope: str | None) -> None:
    """JWKS 설정은 exact chat:stream 외 값으로 기동하지 않는다."""
    with pytest.raises(ValueError):
        _jwks_settings(jwt_scope=scope)
