"""요청 바디 크기 상한 (이슈 #299, api-spec §2.5·§2.8) — BodySizeLimitMiddleware.

`tests/unit/test_infra.py` 는 다른 레인이 함께 건드릴 수 있어 별도 파일로 분리한다.
"""

from __future__ import annotations

import json

import jwt
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.core.body_limit import BodySizeLimitMiddleware
from app.core.config import get_settings
from app.main import app as real_app
from app.main import create_app
from app.schemas.chat import BuyerChatRequest

# 레이트 리밋 상태는 tests/conftest.py 의 전역 autouse `_reset_infra_state` 가 매 테스트 전후로
# 이미 reset_limiter() 한다 — 이 파일에서 다시 하지 않는다.


def _spy_app() -> tuple:
    """바디 크기 확인용 최소 스파이 라우트가 달린 새 앱(등록 순서는 create_app() 과 동일)."""
    app = create_app()
    calls: list[int] = []

    @app.post("/_spy")
    async def _spy(request: Request) -> dict:
        # 바디를 **완주해 읽은 뒤에만** 기록한다 — ASGI 라우팅은 바디 완주 전에 핸들러 코루틴에
        # 진입하므로, 진입 자체가 아니라 "오버사이즈 바디를 실제로 다 받았는가"를 증명해야
        # 스트리밍 거절(§2-b 4단계, http.disconnect)의 의미와 맞는다.
        body = await request.body()
        calls.append(1)
        return {"received": len(body)}

    return app, calls


def _bearer(sub: str) -> dict:
    """dev 디코드는 서명 검증을 안 하므로 임의 서명 JWT 로 sub 만 실어 보낸다(test_infra.py 규약)."""
    token = jwt.encode({"sub": sub}, "test-secret-key-0123456789abcdef", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


# ─────────── 1. Content-Length 사전 거절 ───────────


def test_content_length_over_limit_returns_400_without_calling_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content-Length 가 상한을 넘으면 다운스트림 핸들러 호출 없이 400 봉투를 낸다."""
    settings = get_settings()
    monkeypatch.setattr(settings, "request_body_max_bytes", 10)
    app, calls = _spy_app()
    client = TestClient(app)

    r = client.post("/_spy", content=b"x" * 100)

    assert r.status_code == 400
    env = r.json()["error"]
    assert env["code"] == "BAD_REQUEST"
    assert env["requestId"]
    assert r.headers.get("x-request-id") == env["requestId"]
    assert calls == []  # 라우트 핸들러가 전혀 호출되지 않았다


# ─────────── 2. chunked(Content-Length 없음) 스트리밍 누적 카운트 ───────────


def test_chunked_without_content_length_over_limit_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Content-Length 위조/부재(chunked) 경로도 실수신 바이트 누적으로 걸러진다.

    httpx TestClient 는 제너레이터 body 를 주면 Content-Length 없이 chunked 전송한다(실측 확인) —
    이 경로가 없으면 Content-Length 위조로 상한이 뚫린다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "request_body_max_bytes", 10)
    app, calls = _spy_app()
    client = TestClient(app)

    def _chunks():
        yield b"a" * 6
        yield b"b" * 6  # 누적 12 > 10

    r = client.post("/_spy", content=_chunks())

    assert r.status_code == 400
    env = r.json()["error"]
    assert env["code"] == "BAD_REQUEST"
    assert env["requestId"]
    assert calls == []  # 핸들러가 오버사이즈 바디를 끝까지 받지 못했다(disconnect 로 끊김)


def test_chunked_send_actually_omits_content_length() -> None:
    """위 테스트가 실제로 chunked 경로(Content-Length 없음)를 타는지 별도로 실측 고정한다."""
    app, _ = _spy_app()
    client = TestClient(app)

    captured: dict = {}

    @app.post("/_echo_headers")
    async def _echo_headers(request: Request) -> dict:
        captured["content_length"] = request.headers.get("content-length")
        captured["transfer_encoding"] = request.headers.get("transfer-encoding")
        await request.body()
        return {"ok": True}

    def _chunks():
        yield b"a" * 4
        yield b"b" * 4

    r = client.post("/_echo_headers", content=_chunks())
    assert r.status_code == 200
    assert captured["content_length"] is None
    assert captured["transfer_encoding"] == "chunked"


# ─────────── 2b. 리뷰 1차 F-1 · 거절 응답 멱등성 (raw ASGI) ───────────


async def test_reject_is_idempotent_when_downstream_ignores_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """다운스트림이 http.disconnect 를 무시하고 receive() 를 계속 불러도 http.response.start 는
    정확히 1개만 나간다.

    TestClient(httpx)로는 이 경로가 안 나온다 — Starlette 의 실제 Request.stream() 은
    http.disconnect 를 받으면 ClientDisconnect 를 던져 즉시 멈추므로, 지금 스택에서는 버그가
    재현되지 않는다(패킷 F-1 원문의 "실측: 현재 스택에서는 재현되지 않는다"). 그 안전성은
    전적으로 다운스트림 구현이 disconnect 를 존중한다는 가정에 기대고 있어, disconnect 를
    무시하는(혹은 무시하는 버그가 있는) ASGI 앱이 뒤에 오면 조용히 깨진다 — 그래서 scope dict 를
    직접 만들어 그런 다운스트림을 흉내내는 raw ASGI 호출로 증명한다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "request_body_max_bytes", 10)

    # 매 청크가 6바이트라 두 번째부터 계속 상한(10)을 넘는다 — 수정 전 코드라면 매 청크마다
    # _reject 가 다시 불려 http.response.start 가 반복 전송된다.
    chunks = [b"a" * 6, b"b" * 6, b"c" * 6, b"d" * 6]
    chunk_iter = iter(chunks)

    async def fake_raw_receive() -> dict:
        body = next(chunk_iter, None)
        if body is None:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": body, "more_body": True}

    sent_messages: list[dict] = []

    async def fake_send(message: dict) -> None:
        sent_messages.append(message)

    async def stubborn_downstream(scope: dict, receive, send) -> None:
        """disconnect 를 무시하고 계속 receive() 를 부르는 다운스트림(버그 재현용)."""
        for _ in range(len(chunks) + 2):
            message = await receive()
            if message["type"] == "http.disconnect":
                continue  # 정상 구현(Starlette)이라면 여기서 멈추지만, 이 더미는 무시한다.

    middleware = BodySizeLimitMiddleware(stubborn_downstream)
    scope = {"type": "http", "method": "POST", "path": "/whatever", "headers": []}

    await middleware(scope, fake_raw_receive, fake_send)

    starts = [m for m in sent_messages if m["type"] == "http.response.start"]
    assert len(starts) == 1
    assert starts[0]["status"] == 400
    bodies = [m for m in sent_messages if m["type"] == "http.response.body"]
    assert len(bodies) == 1


async def test_reject_skips_envelope_when_downstream_already_started_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """다운스트림이 **자기 응답(200)을 먼저 시작한 뒤** 바디를 마저 읽다가 상한을 넘기면, 그
    위에 우리 400 http.response.start 를 겹쳐 보내지 않고(이미 나간 응답 위에 끼워 넣을 자리가
    없다) disconnect 만 돌려준다 — http.response.start 는 다운스트림의 200 하나만 나가야
    한다(리뷰 2차 R-2, F-1 과 같은 종류의 잠복 결함).

    현재 스택(FastAPI/Starlette)은 바디를 다 읽은 뒤에야 응답을 시작하므로 이 경로는 실제로
    도달하지 않는다(리뷰어 실측도 raw ASGI 로만 재현됨) — 그 순서를 지키지 않는 다운스트림을
    raw ASGI 로 직접 흉내내 증명한다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "request_body_max_bytes", 10)

    chunks = [b"a" * 6, b"b" * 6]  # 두 번째 청크에서 누적 12 > 10
    chunk_iter = iter(chunks)

    async def fake_raw_receive() -> dict:
        body = next(chunk_iter, None)
        if body is None:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": body, "more_body": True}

    sent_messages: list[dict] = []

    async def fake_send(message: dict) -> None:
        sent_messages.append(message)

    async def starts_response_then_reads_body(scope: dict, receive, send) -> None:
        """자기 응답(200)을 먼저 통째로 보낸 뒤에야 바디를 읽는 이례적인 다운스트림."""
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"early ok"})
        await receive()  # 첫 청크(6B, 아직 상한 이내)
        await receive()  # 두 번째 청크(누적 12B > 10) — 여기서 미들웨어가 상한 초과를 감지한다

    middleware = BodySizeLimitMiddleware(starts_response_then_reads_body)
    scope = {"type": "http", "method": "POST", "path": "/whatever", "headers": []}

    await middleware(scope, fake_raw_receive, fake_send)

    starts = [m for m in sent_messages if m["type"] == "http.response.start"]
    assert len(starts) == 1  # 다운스트림의 200 하나만 — 우리 400 이 겹쳐 나가지 않는다
    assert starts[0]["status"] == 200


# ─────────── 2c. 리뷰 1차 F-2/3차 C-1 · 거절 후 다운스트림 예외를 무음이 아니라 warning 으로 남긴다 ───────────


async def test_downstream_exception_after_reject_is_logged_not_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """거절(rejected=True) 후 다운스트림이 던지는 예외는 삼키되, 타입 이름을 warning 으로 남긴다.

    [리뷰 3차 C-1] 앱의 실제 루트 로거 레벨은 INFO 다(configure_logging() 기본값, debug 는
    운영에서 한 줄도 안 나온다) — caplog 를 INFO 로 잡아 이 레코드가 그 레벨에서도 실제로
    남는지 증명한다. DEBUG 로 억지로 낮춰 놓고 통과시키던 이전 판은 이 결함(운영 완전 무음)을
    못 잡았다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "request_body_max_bytes", 10)

    async def fake_raw_receive() -> dict:
        return {"type": "http.request", "body": b"x" * 100, "more_body": False}

    async def fake_send(message: dict) -> None:
        return None

    async def raising_downstream(scope: dict, receive, send) -> None:
        await receive()  # 거절을 유발하고 http.disconnect 를 받는다
        raise RuntimeError("simulated ClientDisconnect-like failure")

    middleware = BodySizeLimitMiddleware(raising_downstream)
    scope = {"type": "http", "method": "POST", "path": "/whatever", "headers": []}

    import logging

    with caplog.at_level(logging.INFO, logger="app.core.body_limit"):
        await middleware(scope, fake_raw_receive, fake_send)  # 예외를 전파하지 않아야 한다(삼킴)

    assert any(
        "RuntimeError" in record.message
        and "swallowed" in record.message
        and record.levelno == logging.WARNING
        for record in caplog.records
    )
    # 본문 원문("x"*100)이나 예외 메시지 문자열은 로그에 실리지 않는다(§6.3 b) — 타입 이름만.
    assert not any("simulated ClientDisconnect" in record.message for record in caplog.records)


# ─────────── 3. 경계 (== 통과, +1 거절) ───────────


def test_boundary_exact_limit_passes_and_plus_one_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "request_body_max_bytes", 16)
    app, calls = _spy_app()
    client = TestClient(app)

    r_ok = client.post("/_spy", content=b"x" * 16)
    assert r_ok.status_code == 200
    assert r_ok.json()["received"] == 16

    r_over = client.post("/_spy", content=b"x" * 17)
    assert r_over.status_code == 400

    assert calls == [1]  # 통과한 요청만 핸들러에 도달했다


# ─────────── 4. 정상 요청 회귀 0 ───────────


def test_realistic_max_field_bounded_payload_passes_default_limit() -> None:
    """현실적 최대 페이로드(필드별 상한 가득 채움)는 그대로 통과하고, 기본 상한보다 훨씬 작다.

    [리뷰 1차 F-3] payload 는 **스키마상 유효**해야 한다 — 이전 판은 `screen.filters` 를 list 로,
    `conditionActions` 항목을 `{"type":"confirm"}` 으로 썼는데 실제 계약은 `filters:
    dict[str,str]`(허용 키 3종 `status`/`sort`/`page`, `SCREEN_FILTER_KEYS`)이고
    `ConditionAction` 은 `{op: Literal["remove"], field: Literal[...]}` 이라 둘 다 존재하지 않는
    모양이었다. `pageType` 도 필수인데 빠져 있었다. 스키마상 무효한 바이트열이 상한보다
    작다는 것만으로는 "정상 요청 회귀 0"을 증명하지 못한다 — 그 바이트열 자체가 실제 FE 가
    보낼 수 없는 것이면 무엇의 회귀도 아니기 때문이다. `/chat` 라우트로 직접 쏘지 않는 이유는
    세션 lifecycle(동시 스트림 잠금)이 얽혀 409 로 불안정해지기 때문이다(실측) — 대신
    `/_spy`(미들웨어 통과만 확인)와 `BuyerChatRequest.model_validate_json`(계약 통과 확인)을
    각각 별개로 증명한다.

    실측(2026-08-05, 이 payload 그대로): **21,499B = 기본 상한의 약 2.05%** — #118 이 절단 없이
    받아주는 현실적 최대 구간(개수는 `screen_products_max`=20, 항목 길이는 표시 상한
    `screen_text_max_chars`=120자)이 이 층에서도 넉넉히 통과한다.
    """
    settings = get_settings()
    payload = {
        "sessionId": "s" * settings.chat_key_max_chars,
        "threadId": "t" * settings.chat_key_max_chars,
        "message": "가" * settings.chat_message_max_chars,
        "screen": {
            "pageType": "search",  # BuyerChatRequest 허용 10종 중 하나(필수 필드)
            "products": [
                # productId 는 양의 정수만 유효하다(_coerce_positive_int) — 0부터 시작하지 않는다.
                {"productId": i + 1, "name": "상" * settings.screen_text_max_chars}
                for i in range(settings.screen_products_max)
            ],
            # filters 는 dict[str, str] 이고 허용 키는 SCREEN_FILTER_KEYS 3종뿐이다.
            "filters": {
                key: "필" * settings.screen_text_max_chars for key in ("status", "sort", "page")
            },
        },
        "conditionActions": [{"op": "remove", "field": "priceMax"}],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    assert len(body) < settings.request_body_max_bytes // 2  # 기본 상한보다 훨씬 작다(실측 ~2%)

    # (c) 계약 검증: 이 바이트열이 실제로 BuyerChatRequest 모델을 통과하는 "정상 요청"이다.
    BuyerChatRequest.model_validate_json(body)

    # (a)+(b): 이 층(BodySizeLimitMiddleware)을 그대로 통과한다.
    app, calls = _spy_app()
    client = TestClient(app)
    r = client.post("/_spy", content=body)

    assert r.status_code == 200
    assert calls == [1]


# ─────────── 5. 기본값 정합 (config 파생, 하드코딩 금지) ───────────


def test_default_limit_covers_config_derived_worst_case_normal_payload() -> None:
    """2-d 표의 산정을 config 값에서 파생시켜 고정한다 — 필드 상한이 오르면 이 테스트가 깨진다."""
    settings = get_settings()
    bytes_per_char = 3  # 한국어 UTF-8 가정
    per_item_overhead = 400  # productId + JSON 구두점 등(고정 추정치)
    filters_count = 10  # 표시값 필터 건수(고정 추정치)
    envelope_bytes = 500  # conditionActions + 봉투 키(고정 추정치)

    message_bytes = settings.chat_message_max_chars * bytes_per_char
    key_bytes = settings.chat_key_max_chars * bytes_per_char * 2  # sessionId + threadId
    products_bytes = settings.screen_products_raw_scan_max * (
        settings.screen_text_max_chars * bytes_per_char + per_item_overhead
    )
    filters_bytes = filters_count * settings.screen_text_max_chars * bytes_per_char

    max_normal_payload_bytes = (
        message_bytes + key_bytes + products_bytes + filters_bytes + envelope_bytes
    )

    assert max_normal_payload_bytes < settings.request_body_max_bytes


# ─────────── 6. 바디 없는 요청 무영향 ───────────


def test_health_check_unaffected() -> None:
    client = TestClient(real_app)
    r = client.get("/health")
    assert r.status_code == 200


def test_body_reading_route_rejects_oversized_content_length_regardless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[리뷰 2차 R-1(a)] Content-Length 사전 거절은 라우트가 바디를 **전혀 읽지 않아도**
    경로 무관하게 발동한다 — "상한 우회"가 아니라 이 층이 실제로 보장하는 바를 못박는
    회귀 테스트다(모듈 docstring (a) 참조).

    `GET /health`(app/main.py)의 핸들러는 `async def health() -> dict: return {...}` 로 `Request`
    인자 자체를 받지 않는다 — FastAPI 의존성 주입 대상에 없으니 ASGI 레벨에서 바디를 읽을
    방법이 그 핸들러엔 **구조적으로 없다**(호출 스택 어디에도 `request.body()`/`Request` 획득
    경로가 없다, "우연히 안 읽었다"가 아니라 코드로 확인 가능한 사실). 그런데도 거절되는 것이
    "다운스트림 호출 **전에** Content-Length 만으로 판단한다"는 (a) 의 증거다.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "request_body_max_bytes", 10)
    client = TestClient(real_app)

    r = client.request("GET", "/health", content=b"x" * 100)

    assert r.status_code == 400
    env = r.json()["error"]
    assert env["code"] == "BAD_REQUEST"
    assert env["requestId"]


# ─────────── 7. 순서 증명 — 레이트 리밋 바깥 ───────────


def test_body_limit_rejection_does_not_consume_rate_limit_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """바디 초과 요청은 레이트 리밋보다 앞에서 걸려 슬롯을 소모하지 않는다(2-c 순서 증명)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "request_body_max_bytes", 10)
    client = TestClient(real_app)
    headers = _bearer("order-proof-user")

    over_limit_count = settings.rate_limit_per_min + 5  # 분당 상한(기본 10)보다 많이 보낸다
    for _ in range(over_limit_count):
        r = client.post("/chat", content=b"x" * 1000, headers=headers)
        assert r.status_code == 400

    # 상한을 원복하고 정상 요청을 보내면 레이트 리밋 슬롯이 아직 남아 있어 200 이어야 한다.
    monkeypatch.setattr(settings, "request_body_max_bytes", 1_048_576)
    r_ok = client.post(
        "/chat",
        json={"sessionId": "order-ok", "threadId": "order-ok-t", "message": "m"},
        headers=headers,
    )
    assert r_ok.status_code == 200


# ─────────── 8. 순서 증명 — CORS 안쪽 ───────────


def test_cors_header_present_on_reject_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """바디 초과 400 응답에도 CORS 헤더가 실린다(api-spec §2.7, 기존 429 와 같은 이유)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "request_body_max_bytes", 10)
    client = TestClient(real_app)
    origin = settings.cors_origins[0]

    r = client.post("/chat", content=b"x" * 1000, headers={"Origin": origin})

    assert r.status_code == 400
    assert r.headers.get("access-control-allow-origin") == origin


# ─────────── 9. env 로 상한 조정 가능 ───────────


def test_limit_configurable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQUEST_BODY_MAX_BYTES env 로 상한을 낮추면 그 값에서 거절이 발동한다.

    env 복원과 캐시 무효화를 테스트 본문 안에서 직접, 순서대로 처리한다 —
    tests/conftest.py 의 전역 autouse `_reset_infra_state` 가 teardown 에서 reset_cart_store()
    를 통해 get_settings() 를 다시 부르는데, 그 호출이 monkeypatch 의 자동 env 복원보다 먼저
    돌면(픽스처 teardown 은 이 파일 fixture 보다 나중에 실행되는 상위 conftest 라 실제로
    그렇다) 그 시점의 낮춰진 env 값으로 캐시가 재구성되어 버려 다음 테스트로 샌다(실측 확인).
    monkeypatch.undo() 를 여기서 먼저 불러 env 복원을 강제한 뒤 캐시를 비우면, 그 뒤에 도는
    어떤 픽스처의 get_settings() 호출도 항상 올바른(복원된) env 로 재구성된다.
    """
    monkeypatch.setenv("REQUEST_BODY_MAX_BYTES", "20")
    get_settings.cache_clear()
    try:
        app, calls = _spy_app()
        client = TestClient(app)

        r_ok = client.post("/_spy", content=b"x" * 20)
        assert r_ok.status_code == 200

        r_over = client.post("/_spy", content=b"x" * 21)
        assert r_over.status_code == 400

        assert calls == [1]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
