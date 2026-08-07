"""401 실패 사유 로깅 회귀 테스트 (이슈 #408, api-spec §2.3/§2.5).

운영에서 `POST /chat 401 TOKEN_INVALID` 가 전원 발생했는데 AI 로그에 사유가 없어 원인
분리가 불가능했다. 401 매핑 3곳(get_identity·require_seller 경유·verify_service_token)이
예외 타입 + 메시지 + `__cause__` 체인을 WARNING 으로 남기는지 유형별로 고정한다.

[보안] 토큰 원문·서명은 어떤 경우에도 로그에 남지 않아야 한다 — 그 불변식도 함께 검증한다.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

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

DEPS_LOGGER = "app.api.deps"


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    """테스트용 RSA 개인키 (모듈 공유)."""
    return make_rsa_key()


@pytest.fixture(scope="module")
def other_key() -> rsa.RSAPrivateKey:
    """JWKS 에 실리지 않는 키 — 서명 불일치 재현용."""
    return make_rsa_key()


def _jwks_settings(**overrides) -> Settings:
    """jwks 모드 Settings — 운영 필수값 포함, .env 미참조."""
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
def jwks_app(monkeypatch: pytest.MonkeyPatch, rsa_key: rsa.RSAPrivateKey):
    """deps 를 jwks 설정으로 패치하고 JWKS fetch 를 로컬 키페어로 고정한다."""
    settings = _jwks_settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    auth._jwk_client.cache_clear()
    install_jwks_fetch(monkeypatch, lambda: jwks_of((rsa_key, KID)))
    yield settings
    auth._jwk_client.cache_clear()


def _chat_body() -> dict:
    return {"sessionId": "s-408-1", "threadId": "t-408-1", "message": "여행용 파우치 추천해줘"}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _auth_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """deps 로거가 남긴 401 거부 레코드만 추린다."""
    return [
        record
        for record in caplog.records
        if record.name == DEPS_LOGGER and record.getMessage().startswith("auth rejected")
    ]


def _expired_claims() -> dict:
    now = dt.datetime.now(tz=dt.timezone.utc)
    return ticket_claims(exp=now - dt.timedelta(seconds=1))


# ── 사용자 티켓: 유형별 사유가 로그에 남는다 ──


@pytest.mark.parametrize(
    ("case", "code", "expected_reason"),
    [
        pytest.param("no-token", "TOKEN_INVALID", "AuthError: missing token", id="missing-token"),
        pytest.param("bad-signature", "TOKEN_INVALID", "InvalidSignatureError", id="signature"),
        pytest.param("wrong-audience", "TOKEN_INVALID", "InvalidAudienceError", id="audience"),
        pytest.param("wrong-issuer", "TOKEN_INVALID", "InvalidIssuerError", id="issuer"),
        pytest.param("missing-exp", "TOKEN_INVALID", "MissingRequiredClaimError", id="missing-exp"),
        pytest.param("unknown-kid", "TOKEN_INVALID", "PyJWKClientError", id="unknown-kid"),
        pytest.param(
            "wrong-scope", "TOKEN_INVALID", "missing or mismatched scope", id="scope-mismatch"
        ),
        pytest.param(
            "seller-with-guest-sub-type",
            "TOKEN_INVALID",
            "invalid seller role claim",
            id="seller-with-guest-sub-type",
        ),
        pytest.param(
            "seller-without-sub-type",
            "TOKEN_INVALID",
            "invalid sub_type claim",
            id="seller-without-sub-type",
        ),
        pytest.param(
            "sub-type-only-malformed",
            "TOKEN_INVALID",
            "invalid sub_type claim",
            id="sub-type-only-malformed",
        ),
        pytest.param(
            "invalid-sub-type",
            "TOKEN_INVALID",
            "invalid sub_type claim",
            id="invalid-sub-type",
        ),
        pytest.param("expired", "TOKEN_EXPIRED", "ExpiredSignatureError", id="expired"),
    ],
)
def test_chat_401_logs_reason_by_failure_type(
    jwks_app,
    rsa_key: rsa.RSAPrivateKey,
    other_key: rsa.RSAPrivateKey,
    caplog: pytest.LogCaptureFixture,
    case: str,
    code: str,
    expected_reason: str,
) -> None:
    """대표 실패 유형마다 401 코드 + 예외 사유가 WARNING 레코드로 남는다 (#408)."""
    tokens = {
        "bad-signature": lambda: sign_ticket(other_key, KID, ticket_claims()),
        "wrong-audience": lambda: sign_ticket(rsa_key, KID, ticket_claims(aud="other-audience")),
        "wrong-issuer": lambda: sign_ticket(rsa_key, KID, ticket_claims(iss="other-issuer")),
        "missing-exp": lambda: sign_ticket(
            rsa_key, KID, {k: v for k, v in ticket_claims().items() if k != "exp"}
        ),
        "unknown-kid": lambda: sign_ticket(rsa_key, "kid-unknown", ticket_claims()),
        "wrong-scope": lambda: sign_ticket(rsa_key, KID, ticket_claims(scope="profile:read")),
        # role="seller" + sub_type="guest" — 판매자는 sub_type="member"를 동반해야 한다.
        "seller-with-guest-sub-type": lambda: sign_ticket(
            rsa_key, KID, ticket_claims(role="seller", sub_type="guest")
        ),
        # role="seller"인데 sub_type 키 자체가 없다 — §2.3 v0.28.0: sub_type은 판매자 티켓도
        # 필수라 여기서 먼저 걸린다(구 XOR 하에서는 허용이었던 형태 — 실존하지 않는 형태다).
        "seller-without-sub-type": lambda: sign_ticket(
            rsa_key,
            KID,
            {k: v for k, v in ticket_claims(role="seller").items() if k != "sub_type"},
        ),
        # role 없이 sub_type만 이상값 — 정본(member|guest) 밖.
        "sub-type-only-malformed": lambda: sign_ticket(
            rsa_key, KID, ticket_claims(sub_type="ADMIN")
        ),
        # 대문자 sub_type — §2.3 정본값(member|guest) 밖이라 fail-closed.
        "invalid-sub-type": lambda: sign_ticket(rsa_key, KID, ticket_claims(sub_type="MEMBER")),
        "expired": lambda: sign_ticket(rsa_key, KID, _expired_claims()),
    }
    headers = {} if case == "no-token" else _bearer(tokens[case]())

    with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
        resp = client.post("/chat", json=_chat_body(), headers=headers)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == code

    records = _auth_records(caplog)
    assert len(records) == 1, "401 마다 정확히 한 줄"
    record = records[0]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert f"code={code}" in message
    assert "dep=get_identity" in message
    assert "path=/chat" in message
    assert expected_reason in message


def test_401_log_correlates_with_response_request_id(
    jwks_app, rsa_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
) -> None:
    """로그의 rid 는 응답 봉투/X-Request-Id 와 같은 값이다 (§2.4 상관관계)."""
    token = sign_ticket(rsa_key, KID, ticket_claims(scope="profile:read"))
    with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
        resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    request_id = resp.json()["error"]["requestId"]
    assert resp.headers["X-Request-Id"] == request_id
    assert f"rid={request_id}" in _auth_records(caplog)[0].getMessage()


def test_401_log_never_contains_token_material(
    jwks_app,
    rsa_key: rsa.RSAPrivateKey,
    other_key: rsa.RSAPrivateKey,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """토큰 원문·서명·클레임 식별자(sub/sessionId)는 로그에 남지 않는다 (PII/시크릿 금지)."""
    claims = ticket_claims(sub="1234567890", sessionId="s-secret-408")
    token = sign_ticket(other_key, KID, claims)
    header_b64, payload_b64, signature_b64 = token.split(".")

    with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
        client.post("/chat", json=_chat_body(), headers=_bearer(token))

    message = _auth_records(caplog)[0].getMessage()
    assert token not in message
    for segment in (header_b64, payload_b64, signature_b64):
        assert segment not in message
    assert "1234567890" not in message
    assert "s-secret-408" not in message


def test_401_log_escapes_attacker_controlled_control_chars(
    jwks_app, rsa_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
) -> None:
    """서명 검증 이전 값(`kid`)으로 가짜 로그 줄을 심을 수 없다 (로그 인젝션, CWE-117).

    PyJWKClientError 메시지는 JWT 헤더의 `kid` 를 그대로 싣는데, `kid` 는 서명 검증 전에
    읽히므로 공격자가 유효 서명 없이 임의 문자열을 넣을 수 있다.
    """
    forged = "auth rejected code=TOKEN_INVALID dep=get_identity path=/chat rid=deadbeef reason=ok"
    # C0 뿐 아니라 뷰어·파서가 개행으로 읽는 NEL(U+0085)·LINE/PARAGRAPH SEPARATOR
    # (U+2028/U+2029)와 양방향 재정의(U+202E)까지 같은 토큰에 심는다.
    breakers = "\n\r\t\x85\u2028\u2029\u202e"
    token = sign_ticket(rsa_key, f"kid-x{breakers}{forged}", ticket_claims())

    with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
        resp = client.post("/chat", json=_chat_body(), headers=_bearer(token))

    assert resp.status_code == 401
    records = _auth_records(caplog)
    assert len(records) == 1, "위조 줄이 별도 레코드로 갈라지지 않는다"
    message = records[0].getMessage()
    for breaker in breakers:
        assert breaker not in message, f"비출력 문자 {breaker!r} 가 그대로 실렸다"
    assert "\\n" in message and "\\r" in message and "\\t" in message
    assert "\\u0085" in message and "\\u2028" in message and "\\u2029" in message
    # 위조 문자열은 한 줄 안의 reason= 값에 갇힌다 — 새 로그 레코드로 갈라지지 않는다.
    assert message.startswith("auth rejected code=TOKEN_INVALID dep=get_identity path=/chat")
    assert forged in message.split("reason=", 1)[1]


def test_401_log_escapes_request_path(caplog: pytest.LogCaptureFixture) -> None:
    """path 도 사유와 같이 이스케이프된다 — path 파라미터 라우트에 재사용될 때를 대비한다.

    지금 이 의존성을 쓰는 라우트는 전부 고정 리터럴 경로지만, {brandId} 류 라우트에 붙는
    순간 `request.url.path` 가 외부 통제 값이 된다(PR #409 리뷰 3R). urlsplit 이 걸러주는
    것은 `\\t\\r\\n` 뿐이라 U+2028·U+0085 같은 줄바꿈 취급 문자는 그대로 통과한다.
    """
    forged = "/seller/x\u2028\x85auth rejected code=TOKEN_INVALID rid=deadbeef reason=ok"
    request = Request(
        {"type": "http", "method": "GET", "path": forged, "headers": [], "query_string": b""}
    )
    assert "\u2028" in request.url.path, "전제: urlsplit 은 이 문자를 지우지 않는다"

    with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
        deps._log_auth_rejection(
            request, code="TOKEN_INVALID", dependency="get_identity", reason="AuthError: x"
        )

    records = _auth_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "\u2028" not in message and "\x85" not in message
    assert "path=/seller/x\\u2028\\u0085auth rejected" in message


def test_reason_chain_respects_suppressed_context() -> None:
    """`raise ... from None` 으로 억제된 예외는 사유에 되살아나지 않는다 (PR #409 리뷰 4R).

    억제는 "이 예외는 노출하지 말라"는 명시적 의사표시다 — CPython 트레이스백과 같은 규칙.
    """
    try:
        try:
            raise ValueError("secret-inner-detail")
        except ValueError:
            raise deps.AuthError("invalid token") from None
    except deps.AuthError as exc:
        suppressed = deps._reason_chain(exc)

    assert suppressed == "AuthError: invalid token"
    assert "secret-inner-detail" not in suppressed

    # 억제하지 않으면(암묵 컨텍스트) 종전대로 따라간다 — 진단 능력은 유지된다.
    try:
        try:
            raise ValueError("visible-inner-detail")
        except ValueError:
            raise deps.AuthError("invalid token")
    except deps.AuthError as exc:
        implicit = deps._reason_chain(exc)

    assert "ValueError: visible-inner-detail" in implicit


def test_escape_keeps_printable_text_intact() -> None:
    """이스케이프는 비출력 문자만 건드린다 — 한글·기호는 그대로여야 진단이 읽힌다."""
    assert deps._escape_unprintable("인증 실패: scope 불일치 (a/b) 100%") == (
        "인증 실패: scope 불일치 (a/b) 100%"
    )
    assert deps._escape_unprintable("a\u2028b") == "a\\u2028b"


def test_seller_lane_401_logs_require_seller_dependency(
    jwks_app, other_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
) -> None:
    """/seller/chat 401 은 require_seller 경유로 기록된다 (레인 구분)."""
    token = sign_ticket(other_key, KID, seller_ticket_claims())
    with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
        resp = client.post(
            "/seller/chat",
            json={"threadId": "t-408-seller", "message": "상세페이지 초안 만들어줘"},
            headers=_bearer(token),
        )

    assert resp.status_code == 401
    message = _auth_records(caplog)[0].getMessage()
    assert "dep=require_seller" in message
    assert "path=/seller/chat" in message
    assert "InvalidSignatureError" in message


def test_successful_ticket_logs_nothing(
    jwks_app, rsa_key: rsa.RSAPrivateKey, caplog: pytest.LogCaptureFixture
) -> None:
    """정상 티켓은 거부 로그를 남기지 않는다 (무조건 로깅 회귀 방지)."""
    token = sign_ticket(rsa_key, KID, ticket_claims())
    with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
        deps.get_identity(authorization=f"Bearer {token}")
    assert _auth_records(caplog) == []


# ── 서비스 토큰 레인 (§3.5) ──


@pytest.mark.parametrize(
    ("header", "configured", "expected_reason"),
    [
        pytest.param(None, "svc-token", "X-Internal-Token header missing", id="header-missing"),
        pytest.param("wrong-token", "svc-token", "X-Internal-Token mismatch", id="mismatch"),
        pytest.param(
            "svc-token", "", "internal_api_token is not configured", id="server-unconfigured"
        ),
    ],
)
def test_internal_token_401_logs_reason(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    header: str | None,
    configured: str,
    expected_reason: str,
) -> None:
    """서비스 토큰 401 도 어느 조건에서 걸렸는지 남긴다 — 헤더 값·설정 토큰은 로그 금지."""
    # 미설정 케이스는 jwks 모드 기동 검증(필수값)을 통과할 수 없어 검증 후 복사로 만든다.
    settings = _jwks_settings().model_copy(update={"internal_api_token": configured})
    monkeypatch.setattr(deps, "get_settings", lambda: settings)

    with caplog.at_level(logging.WARNING, logger=DEPS_LOGGER):
        with pytest.raises(HTTPException) as excinfo:
            deps.verify_service_token(x_internal_token=header)

    assert excinfo.value.status_code == 401
    message = _auth_records(caplog)[0].getMessage()
    assert "code=INTERNAL_TOKEN_INVALID" in message
    assert "dep=verify_service_token" in message
    assert expected_reason in message
    if header is not None:
        assert header not in message
    if configured:
        assert configured not in message
