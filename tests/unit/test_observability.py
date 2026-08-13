"""대화 저장 & 관측 (이슈 #8) — api-spec §6.3 (대화 저장 + 구조화 로그·PII)."""

from __future__ import annotations

import asyncio
import json
import logging
import types

import jwt
import psycopg

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core import observability
from app.core.auth import Identity
from app.core.conversation import (
    CommittedTurn,
    PgConversationStore,
    TurnStatus,
    conversation_key,
    get_conversation_store,
)
from app.core.config import get_settings
from app.core.observability import (
    emit_rejection,
    identifier_fingerprint,
    message_fingerprint,
    start_observation,
)
from app.core.session_context import BuyerSessionInput, SessionFinalizing, SessionForbidden
from app.core.stream import get_registry
from app.core.stream import open_stream
from app.core.tracing import FakeTraceExporter, RequestTrace, TraceFactory
from app.main import app

client = TestClient(app)


class _FakeRequest:
    def __init__(self, disconnected: bool = False) -> None:
        self._disc = disconnected
        self.state = types.SimpleNamespace()

    async def is_disconnected(self) -> bool:
        return self._disc


async def _obs(
    conversation_id: str,
    message: str = "질문",
    *,
    trace: RequestTrace | None = None,
):
    identity = Identity(user_id="u1", is_guest=False, seller_id=None, subject="u1")
    return start_observation(
        request_id="req-1",
        identity=identity,
        conversation_id=conversation_id,
        message=message,
        store=await get_conversation_store(),
        now=asyncio.get_event_loop().time(),
        trace=trace,
    )


def _trace(exporter: FakeTraceExporter) -> RequestTrace:
    return TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0).start_request(
        name="buyer_chat_turn",
        request_id="req-1",
        conversation_id="session-1",
        thread_id="thread-1",
        lane="buyer",
        environment="test",
    )


def _bearer(sub: str, sub_type: str | None = None) -> dict:
    claims = {"sub": sub}
    if sub_type is not None:
        claims["sub_type"] = sub_type
    token = jwt.encode(claims, "test-secret-key-0123456789abcdef", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


# ─────────── §6.3 (a) 대화 저장 ───────────


async def test_chat_records_completed_turn(buyer_fakes) -> None:
    """정상 스트림 완료 후 턴이 COMPLETED + user 원문 + assistant 부분 누적으로 저장된다."""
    msg = "여행용 방수 케이스 추천해줘"
    r = client.post("/chat", json={"sessionId": "c1", "threadId": "room-c1", "message": msg})
    assert r.status_code == 200
    _ = r.text  # 스트림 소비 → finalize
    store = await get_conversation_store()
    turns = await store.turns_for(conversation_key(None, "c1"))
    assert len(turns) == 1
    assert turns[0].user_text == msg
    assert getattr(turns[0], "thread_id", None) == "room-c1"
    assert turns[0].status == TurnStatus.COMPLETED
    assert turns[0].assistant_text  # assistant 부분 텍스트 누적 저장


async def test_partial_text_preserved_on_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """클라이언트 취소 시 상태 CANCELLED + 부분 생성 텍스트를 보존한다(§6.3 a)."""
    monkeypatch.setattr(get_settings(), "stream_disconnect_poll_s", 0.02)
    obs = await _obs("cx")

    async def token_then_idle(_turn_started_at=None):
        yield 'data: {"type":"token","data":{"text":"부분응답"}}\n\n'
        await asyncio.sleep(2.0)
        yield "data: never\n\n"

    resp = await open_stream(
        _FakeRequest(disconnected=True), "member:cx", token_then_idle, observer=obs
    )
    _ = [c async for c in resp.body_iterator]
    store = await get_conversation_store()
    turn = await store.get_turn(obs.turn_id)
    assert turn is not None
    assert turn.status == TurnStatus.CANCELLED
    assert "부분응답" in turn.assistant_text


async def test_partial_text_preserved_on_error() -> None:
    """스트림 중 상류 오류 시 상태 FAILED + 부분 텍스트를 보존한다(§6.3 a)."""
    obs = await _obs("ce")

    async def token_then_boom(_turn_started_at=None):
        yield 'data: {"type":"token","data":{"text":"조금"}}\n\n'
        raise RuntimeError("mid-stream boom")

    resp = await open_stream(_FakeRequest(), "member:ce", token_then_boom, observer=obs)
    _ = [c async for c in resp.body_iterator]
    store = await get_conversation_store()
    turn = await store.get_turn(obs.turn_id)
    assert turn is not None
    assert turn.status == TurnStatus.FAILED
    assert "조금" in turn.assistant_text


async def test_stream_completes_when_finalize_assistant_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """finish()(대화 저장 DB I/O)가 실패해도 (a) 스트림 소비가 예외 없이 끝나고,
    (b) §6.3 b 구조화 로그(chat_request)는 그대로 남는다(PR #48 후속 리뷰).

    _wrapped() 의 finally 는 이미 SSE 헤더/프레임이 전송된 뒤 실행된다 — 여기서 finish() 가
    막히지 않고 예외를 던지면 done/error 종결 프레임 없이 스트림이 끊기거나(§2.9/§3.1 위반),
    CancelledError 취소 전파 중이었다면 그 취소가 이 새 예외로 대체된다. 또한 대화 저장(§6.3 a)
    실패가 관측 로그(§6.3 b) emit 까지 막으면 안 된다(별개 계약) — finalize_assistant 가
    raise 해도 스트림 소비가 끝나고 chat_request 로그가 남는지 함께 검증한다.
    """
    obs = await _obs("finalize-boom")

    async def fail_finalize(*args, **kwargs):
        raise RuntimeError("conversation store 일시 장애")

    obs.store.finalize_assistant = fail_finalize

    async def token_then_done(_turn_started_at=None):
        yield 'data: {"type":"token","data":{"text":"응답"}}\n\n'

    with caplog.at_level(logging.INFO, logger="observability"):
        resp = await open_stream(
            _FakeRequest(), "member:finalize-boom", token_then_done, observer=obs
        )
        chunks = [c async for c in resp.body_iterator]  # 예외 없이 끝나야 한다
    assert any("응답" in c for c in chunks)
    # §6.3 b 구조화 로그가 finalize 실패와 무관하게 남았는지(계약 분리 검증)
    records = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "observability" and r.getMessage().startswith("{")
    ]
    has_chat_request = any(rec.get("event") == "chat_request" for rec in records)
    assert has_chat_request, "finalize 실패 시 §6.3 b 구조화 로그가 유실됨"


async def test_buyer_logs_use_fingerprints_without_raw_identifiers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """구매자 구조화/stream 로그에는 raw 신원·세션·thread·stream key가 남지 않는다."""
    owner = "member-raw-owner"
    session = "session-raw-secret"
    thread = "thread-raw-secret"
    identity = Identity(user_id=owner, is_guest=False, seller_id=None, subject=owner)
    obs = start_observation(
        request_id="req-safe",
        identity=identity,
        conversation_id=session,
        thread_id=thread,
        message="질문",
        store=await get_conversation_store(),
        now=asyncio.get_running_loop().time(),
        buyer_session=BuyerSessionInput(session, thread, "member", owner),
    )

    async def done(_turn_started_at=None):
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    with caplog.at_level(logging.INFO):
        response = await open_stream(
            _FakeRequest(disconnected=True),
            f"{owner}:{thread}",
            done,
            observer=obs,
        )
        _ = [chunk async for chunk in response.body_iterator]

    text = caplog.text
    assert owner not in text
    assert session not in text
    assert thread not in text
    assert f"{owner}:{thread}" not in text
    records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "observability" and record.getMessage().startswith("{")
    ]
    [chat_record] = [record for record in records if record.get("event") == "chat_request"]
    assert chat_record["ownerFp"]
    assert chat_record["sessionFp"]
    assert chat_record["threadFp"]
    assert "userId" not in chat_record
    assert "conversationId" not in chat_record
    assert "threadId" not in chat_record


async def test_slot_released_when_commit_user_message_cancelled() -> None:
    """commit_user_message(pg-profile write) 중 disconnect 로 취소돼도 슬롯이 해제된다.

    CancelledError(BaseException)가 except Exception 을 뚫고 release 를 스킵하면 해당
    session_id 가 재시작 전까지 영구히 409 를 반환한다(§2.9 a 슬롯 누수, PR #48 후속 리뷰).
    """
    obs = await _obs("cancel-commit")

    async def cancel_commit() -> None:
        raise asyncio.CancelledError

    obs.commit_user_message = cancel_commit
    registry = get_registry()

    async def gen(_turn_started_at=None):
        yield 'data: {"type":"token","data":{"text":"x"}}\n\n'

    with pytest.raises(asyncio.CancelledError):
        await open_stream(_FakeRequest(), "member:cancel-commit", gen, observer=obs)
    assert not registry.is_active("member:cancel-commit")  # 슬롯 해제됨(영구 409 방지)


async def test_pg_conversation_store_query_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """쓰기·읽기 쿼리 모두 응답 없이 멈추면 실행 상한 초과로 종료된다.

    타임아웃이 없으면 commit_user_message 가 영영 안 끝나 동시 스트림 슬롯이 영구히 잠기고,
    읽기도 연결을 문 채 풀 고갈로 쓰기 경로까지 연쇄로 막힌다(§2.9 a, PR #48 후속 리뷰)."""
    monkeypatch.setattr(get_settings(), "state_store_query_timeout_s", 0.05)

    class _HangConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> bool:
            return False

        async def execute(self, *a, **k) -> None:
            await asyncio.sleep(10)  # 응답 없이 멈춘 pg 재현

    class _HangPool:
        def connection(self):
            return _HangConn()

    store = PgConversationStore(_HangPool())
    with pytest.raises(TimeoutError):
        await store.save_user_message("c", "u", "user", "hi")
    with pytest.raises(TimeoutError):
        await store.get_turn("t")
    with pytest.raises(TimeoutError):
        await store.turns_for("c")


@pytest.mark.parametrize("failure", [TimeoutError("pool"), psycopg.OperationalError("down")])
async def test_buyer_chat_pg_operational_failure_maps_to_state_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """buyer atomic turn 경계의 연결/timeout 장애는 중앙 503 봉투로 변환한다."""

    class _FailConnection:
        async def __aenter__(self):
            raise failure

        async def __aexit__(self, *_args):
            return False

    class _FailPool:
        def connection(self):
            return _FailConnection()

    store = PgConversationStore(_FailPool())

    async def get_store():
        return store

    monkeypatch.setattr("app.api.chat.get_conversation_store", get_store)
    response = client.post(
        "/chat",
        json={"sessionId": "db-fail-session", "threadId": "db-fail-thread", "message": "질문"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STATE_UNAVAILABLE"


async def test_buyer_chat_programming_error_is_not_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQL programming 결함은 STATE_UNAVAILABLE로 위장하지 않는다."""

    class _FailConnection:
        async def __aenter__(self):
            raise psycopg.ProgrammingError("bad sql")

        async def __aexit__(self, *_args):
            return False

    class _FailPool:
        def connection(self):
            return _FailConnection()

    store = PgConversationStore(_FailPool())
    with pytest.raises(psycopg.ProgrammingError):
        await store.save_user_message(
            "db-code-session",
            "owner",
            "member",
            "질문",
            thread_id="db-code-thread",
            buyer_session=BuyerSessionInput("db-code-session", "db-code-thread", "member", "owner"),
        )


async def test_pg_conversation_finalize_missing_turn_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Cursor:
        rowcount = 0

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, *args, **kwargs):
            return _Cursor()

    class _Pool:
        def connection(self):
            return _Conn()

    store = PgConversationStore(_Pool())
    with caplog.at_level(logging.WARNING, logger="app.core.conversation"):
        await store.finalize_assistant("missing", "text", TurnStatus.COMPLETED)
    assert any("missing" in record.getMessage() for record in caplog.records)


async def test_pg_conversation_turns_for_uses_deterministic_order() -> None:
    captured = {"sql": ""}

    class _Cursor:
        async def fetchall(self):
            return []

    class _Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql, *args, **kwargs):
            captured["sql"] = sql
            return _Cursor()

    class _Pool:
        def connection(self):
            return _Conn()

    await PgConversationStore(_Pool()).turns_for("c")
    assert "ORDER BY sequence_id" in captured["sql"]


async def test_get_conversation_store_closes_pool_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pool.open() 도중 취소(클라이언트 disconnect 등)돼도 방금 만든 풀을 닫고 취소를 전파한다.

    get_conversation_store()는 open_stream 진입 전 호출돼 취소가 실제로 도달하는데, except
    Exception 은 CancelledError(BaseException)를 못 잡아 풀(+워커)이 새던 문제(PR #48 후속 리뷰).
    """
    import app.core.conversation as conv
    import psycopg_pool

    closed = {"called": False}

    class _FakePool:
        def __init__(self, *a, **k) -> None:
            pass

        async def open(self, wait: bool = True) -> None:
            raise asyncio.CancelledError

        async def close(self) -> None:
            closed["called"] = True

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _FakePool)
    conv.set_store(None)  # 초기화 경로 강제
    try:
        with pytest.raises(asyncio.CancelledError):
            await conv.get_conversation_store()
        assert closed["called"]  # 취소돼도 방금 연 풀이 닫혔다(누수 방지)
    finally:
        conv.set_store(conv.ConversationStore())


async def test_get_conversation_store_init_lock_wait_has_query_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.conversation as conv

    init_lock = asyncio.Lock()
    await init_lock.acquire()
    monkeypatch.setattr(get_settings(), "state_store_query_timeout_s", 0.01)
    monkeypatch.setattr(conv, "_init_lock", init_lock)
    conv.set_store(None)
    try:
        with pytest.raises(TimeoutError):
            await conv.get_conversation_store()
    finally:
        init_lock.release()
        conv.set_store(conv.ConversationStore())


def test_chat_emits_rejection_log_when_conversation_store_fails(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_conversation_store()(pg-profile 지연 연결) 실패가 open_stream 안전망 밖이라도
    §6.3 b chat_request 로그(errorType)를 남긴다(PR #48 후속 리뷰)."""
    import app.api.chat as chat_mod

    async def boom() -> None:
        raise RuntimeError("pg-profile down")

    monkeypatch.setattr(chat_mod, "get_conversation_store", boom)
    with caplog.at_level(logging.INFO, logger="observability"):
        try:
            client.post("/chat", json={"sessionId": "cfail", "threadId": "t", "message": "hi"})
        except RuntimeError:
            pass  # 전역 500 핸들러가 §2.5 봉투를 낸 뒤 재전파(emit_rejection 은 그 전에 실행됨)
    logs = [json.loads(r.getMessage()) for r in caplog.records if r.name == "observability"]
    hits = [
        e for e in logs if e.get("event") == "chat_request" and e.get("errorType") == "INTERNAL"
    ]
    assert hits, "pg-profile 장애 시 chat_request errorType 로그 누락"
    assert hits[0]["streamStatus"] is None
    assert hits[0].get("sessionFp") == identifier_fingerprint("cfail")
    assert "conversationId" not in hits[0]


@pytest.mark.parametrize("failure", [TimeoutError("pool"), psycopg.OperationalError("down")])
def test_chat_store_initialization_failure_maps_to_state_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    async def fail_store():
        raise failure

    monkeypatch.setattr("app.api.chat.get_conversation_store", fail_store)
    response = client.post(
        "/chat",
        json={"sessionId": "init-fail", "threadId": "thread", "message": "질문"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STATE_UNAVAILABLE"


# ─────────── §6.3 (b) 구조화 로그 + PII ───────────


async def test_conditions_frame_does_not_end_text_ttft() -> None:
    obs = await _obs("timing")
    obs.started = 10.0
    obs.record_frame(
        'data: {"type":"conditions","data":{"chips":[]}}\n\n',
        now=10.100,
    )
    obs.record_frame(
        'data: {"type":"token","data":{"text":"첫 토큰"}}\n\n',
        now=10.350,
    )

    assert obs.server_first_event_ms == 100
    assert obs.server_first_text_token_ms == 350


@pytest.mark.parametrize(
    ("case", "frames", "expected_status", "expected_reason"),
    (
        (
            "done",
            ('data: {"type":"done","data":{"finishReason":"stop"}}\n\n',),
            TurnStatus.COMPLETED,
            "done",
        ),
        (
            "error",
            ('data: {"type":"error","data":{"code":"LLM_UNAVAILABLE"}}\n\n',),
            TurnStatus.FAILED,
            "error_frame",
        ),
    ),
)
async def test_tokenless_terminal_frame_records_reason_without_text_ttft(
    case: str,
    frames: tuple[str, ...],
    expected_status: TurnStatus,
    expected_reason: str,
) -> None:
    exporter = FakeTraceExporter()
    obs = await _obs(f"tokenless-{case}", trace=_trace(exporter))

    async def terminal_only(_turn_started_at=None):
        for frame in frames:
            yield frame

    response = await open_stream(
        _FakeRequest(),
        f"member:tokenless-{case}",
        terminal_only,
        observer=obs,
    )
    _ = [chunk async for chunk in response.body_iterator]

    root = exporter.exported[0][0]
    assert obs.server_first_text_token_ms is None
    assert root.metadata["server_first_text_token_ms"] is None
    assert root.metadata["terminalReason"] == expected_reason
    turn = await obs.store.get_turn(obs.turn_id)
    assert turn is not None and turn.status == expected_status


async def test_tokenless_timeout_records_reason_without_text_ttft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "stream_first_token_timeout_s", 0.01)
    exporter = FakeTraceExporter()
    obs = await _obs("tokenless-timeout", trace=_trace(exporter))

    async def no_first_event(_turn_started_at=None):
        await asyncio.sleep(1)
        yield "data: never\n\n"

    with pytest.raises(HTTPException) as exc_info:
        await open_stream(
            _FakeRequest(),
            "member:tokenless-timeout",
            no_first_event,
            observer=obs,
        )
    assert exc_info.value.status_code == 504

    root = exporter.exported[0][0]
    assert obs.server_first_text_token_ms is None
    assert root.metadata["server_first_text_token_ms"] is None
    assert root.metadata["terminalReason"] == "first_event_timeout"


async def test_tokenless_cancellation_records_reason_without_text_ttft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "stream_disconnect_poll_s", 0.01)
    exporter = FakeTraceExporter()
    obs = await _obs("tokenless-cancel", trace=_trace(exporter))

    async def conditions_then_idle(_turn_started_at=None):
        yield 'data: {"type":"conditions","data":{"chips":[]}}\n\n'
        await asyncio.sleep(1)
        yield "data: never\n\n"

    response = await open_stream(
        _FakeRequest(disconnected=True),
        "member:tokenless-cancel",
        conditions_then_idle,
        observer=obs,
    )
    _ = [chunk async for chunk in response.body_iterator]

    root = exporter.exported[0][0]
    assert obs.server_first_text_token_ms is None
    assert root.metadata["server_first_text_token_ms"] is None
    assert root.metadata["terminalReason"] == "client_disconnect"


async def test_terminal_done_stops_before_later_token_and_keeps_text_ttft_empty() -> None:
    exporter = FakeTraceExporter()
    obs = await _obs("terminal-done", trace=_trace(exporter))

    async def done_then_token(_turn_started_at=None):
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'
        yield 'data: {"type":"token","data":{"text":"MUST_NOT_ESCAPE"}}\n\n'

    response = await open_stream(
        _FakeRequest(),
        "member:terminal-done",
        done_then_token,
        observer=obs,
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert len(chunks) == 1
    assert '"type":"done"' in chunks[0]
    assert "MUST_NOT_ESCAPE" not in chunks[0]
    assert obs.server_first_text_token_ms is None
    turn = await obs.store.get_turn(obs.turn_id)
    assert turn is not None
    assert turn.assistant_text == ""
    root = exporter.exported[0][0]
    assert root.metadata["server_first_text_token_ms"] is None
    assert root.metadata["terminalReason"] == "done"


async def test_terminal_error_commits_failure_before_consumer_closes_iterator() -> None:
    exporter = FakeTraceExporter()
    obs = await _obs("terminal-error-close", trace=_trace(exporter))

    async def error_then_token(_turn_started_at=None):
        yield 'data: {"type":"error","data":{"code":"LLM_UNAVAILABLE"}}\n\n'
        yield 'data: {"type":"token","data":{"text":"MUST_NOT_PULL"}}\n\n'

    response = await open_stream(
        _FakeRequest(),
        "member:terminal-error-close",
        error_then_token,
        observer=obs,
    )
    iterator = response.body_iterator
    first = await anext(iterator)
    assert '"type":"error"' in first
    await iterator.aclose()

    turn = await obs.store.get_turn(obs.turn_id)
    assert turn is not None and turn.status == TurnStatus.FAILED
    assert turn.assistant_text == ""
    root = exporter.exported[0][0]
    assert root.error_type == "LLM_UNAVAILABLE"
    assert root.metadata["errorType"] == "LLM_UNAVAILABLE"
    assert root.metadata["terminalReason"] == "error_frame"
    assert not get_registry().is_active("member:terminal-error-close")


async def test_closing_body_before_first_pull_cleans_prefetched_stream() -> None:
    exporter = FakeTraceExporter()
    obs = await _obs("close-before-first-pull", trace=_trace(exporter))
    closed = asyncio.Event()

    async def prefetched_then_idle(_turn_started_at=None):
        try:
            yield 'data: {"type":"meta","data":{"lane":"test"}}\n\n'
            await asyncio.Event().wait()
        finally:
            closed.set()

    key = "member:close-before-first-pull"
    response = await open_stream(_FakeRequest(), key, prefetched_then_idle, observer=obs)
    await response.body_iterator.aclose()
    await asyncio.sleep(0)

    assert closed.is_set()
    assert not get_registry().is_active(key)
    assert not [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_coro().__qualname__ == "_IteratorPump._run"
    ]
    assert len(exporter.exported) == 1
    (root, *_) = exporter.exported[0]
    assert root.metadata["terminalReason"] == "client_disconnect"
    turn = await obs.store.get_turn(obs.turn_id)
    assert turn is not None and turn.status == TurnStatus.CANCELLED


async def test_asgi_cancellation_after_headers_before_first_pull_cleans_stream() -> None:
    exporter = FakeTraceExporter()
    obs = await _obs("cancel-header-handoff", trace=_trace(exporter))
    closed = asyncio.Event()
    headers_sent = asyncio.Event()

    async def prefetched_then_idle(_turn_started_at=None):
        try:
            yield 'data: {"type":"meta","data":{"lane":"test"}}\n\n'
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if message["type"] == "http.response.start":
            headers_sent.set()
            await asyncio.Event().wait()

    key = "member:cancel-header-handoff"
    response = await open_stream(_FakeRequest(), key, prefetched_then_idle, observer=obs)
    response_task = asyncio.create_task(
        response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            receive,
            send,
        )
    )
    await headers_sent.wait()
    response_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response_task
    await asyncio.sleep(0)

    assert closed.is_set()
    assert not get_registry().is_active(key)
    assert not [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_coro().__qualname__ == "_IteratorPump._run"
    ]
    assert len(exporter.exported) == 1
    (root, *_) = exporter.exported[0]
    assert root.metadata["terminalReason"] == "client_disconnect"
    turn = await obs.store.get_turn(obs.turn_id)
    assert turn is not None and turn.status == TurnStatus.CANCELLED


@pytest.mark.parametrize("blocked_body_number", [1, 2])
async def test_asgi_cancellation_during_body_send_cleans_started_stream(
    blocked_body_number: int,
) -> None:
    exporter = FakeTraceExporter()
    obs = await _obs("cancel-first-body-send", trace=_trace(exporter))
    closed = asyncio.Event()
    body_send_started = asyncio.Event()

    async def prefetched_then_idle(_turn_started_at=None):
        try:
            yield 'data: {"type":"meta","data":{"lane":"test"}}\n\n'
            yield 'data: {"type":"token","data":{"text":"hello"}}\n\n'
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def receive():
        await asyncio.Event().wait()

    body_number = 0

    async def send(message):
        nonlocal body_number
        if message["type"] == "http.response.body" and message.get("more_body"):
            body_number += 1
            if body_number == blocked_body_number:
                body_send_started.set()
                await asyncio.Event().wait()

    key = f"member:cancel-body-send:{blocked_body_number}"
    response = await open_stream(_FakeRequest(), key, prefetched_then_idle, observer=obs)
    response_task = asyncio.create_task(
        response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            receive,
            send,
        )
    )
    await body_send_started.wait()
    response_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response_task
    await asyncio.sleep(0)

    assert closed.is_set()
    assert not get_registry().is_active(key)
    assert not [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_coro().__qualname__ == "_IteratorPump._run"
    ]
    assert len(exporter.exported) == 1
    (root, *_) = exporter.exported[0]
    assert root.metadata["terminalReason"] == "client_disconnect"
    turn = await obs.store.get_turn(obs.turn_id)
    assert turn is not None and turn.status == TurnStatus.CANCELLED


def test_structured_log_has_fields_and_hides_raw_message(
    caplog: pytest.LogCaptureFixture, buyer_fakes
) -> None:
    """요청 구조화 로그가 §6.3 b 필드를 담되, 사용자 message 원문은 남기지 않는다(길이/해시만)."""
    msg = "SECRET_QUERY_비밀질의_XYZ"
    with caplog.at_level(logging.INFO, logger="observability"):
        r = client.post("/chat", json={"sessionId": "c2", "threadId": "t", "message": msg})
        _ = r.text
    logs = [rec.getMessage() for rec in caplog.records if rec.name == "observability"]
    assert logs, "관측 로그가 없음"
    record = json.loads(logs[-1])
    for key in (
        "requestId",
        "ownerFp",
        "sessionFp",
        "threadFp",
        "latencyTotal",
        "streamStatus",
        "messageLength",
        "messageHash",
        "model",
        "promptTokens",
        "completionTokens",
        "lane",
        "degraded",
        "degradeReason",
        "costUsd",
        "toolCalls",
    ):
        assert key in record, f"필드 누락: {key}"
    assert record["streamStatus"] == "COMPLETED"
    assert record["sessionFp"] == identifier_fingerprint("c2")
    assert record["threadFp"] == identifier_fingerprint("t")
    assert record["messageLength"] == len(msg)
    # [PII] 원문은 로그 어디에도 없고, 해시만 있다.
    assert msg not in logs[-1]
    _, digest = message_fingerprint(msg)
    assert record["messageHash"] == digest


async def test_observation_logs_lane_degrade_cost_and_tool_calls(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """추적 컨텍스트의 레인·degrade와 모델 단가·도구 호출 수를 요청 로그로 합친다."""
    monkeypatch.setattr(
        observability,
        "get_settings",
        lambda: types.SimpleNamespace(
            pii_hash_pepper="test-pepper",
            model_price_in_per_1k={"priced-model": 0.01},
            model_price_out_per_1k={"priced-model": 0.02},
        ),
    )
    exporter = FakeTraceExporter()
    trace = _trace(exporter)
    observation = await _obs("cost-dimensions", trace=trace)
    observation.record_model_call(
        "priced-model",
        prompt_tokens=2_000,
        completion_tokens=500,
    )
    trace.set_lane("recommend")
    trace.mark_degraded("rerank_fallback")
    trace.record_tool_call()
    trace.record_tool_call()

    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(1.0, TurnStatus.COMPLETED)

    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and item.getMessage().startswith("{")
    )
    assert record["lane"] == "recommend"
    assert record["degraded"] is True
    assert record["degradeReason"] == "rerank_fallback"
    assert record["costUsd"] == pytest.approx(0.03)
    assert record["toolCalls"] == 2


async def test_observation_prices_and_logs_cached_input_and_cache_writes(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """캐시 읽기·쓰기는 전체 입력에서 분리 과금되고 원문 없이 숫자만 로그에 남는다."""
    monkeypatch.setattr(
        observability,
        "get_settings",
        lambda: types.SimpleNamespace(
            pii_hash_pepper="test-pepper",
            model_price_in_per_1k={"priced-model": 0.01},
            model_price_cached_in_per_1k={"priced-model": 0.001},
            model_price_cache_write_per_1k={"priced-model": 0.0125},
            model_price_out_per_1k={"priced-model": 0.02},
        ),
    )
    observation = await _obs("cache-cost")
    observation.record_model_call(
        "priced-model",
        prompt_tokens=1_000,
        completion_tokens=500,
        cached_input_tokens=400,
        cache_write_tokens=100,
    )

    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(1.0, TurnStatus.COMPLETED)

    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and item.getMessage().startswith("{")
    )
    assert record["promptTokens"] == 1_000
    assert record["cachedInputTokens"] == 400
    assert record["cacheWriteTokens"] == 100
    assert record["costUsd"] == pytest.approx(0.01665)


async def test_bound_model_call_receives_usage_without_touching_same_model_placeholder() -> None:
    """동시 fast 호출도 예약 call ID를 쓰면 같은 모델의 일반 placeholder와 섞이지 않는다."""
    from app.core import llm as llm_mod
    from app.core.tracing import bind_model_call_usage, bind_request_trace

    trace = _trace(FakeTraceExporter())
    observation = await _obs("bound-call", trace=trace)
    reserved_id = observation.record_model_call("fast-model", usage_reserved=True)
    ordinary_id = observation.record_model_call("fast-model")

    with bind_request_trace(trace), bind_model_call_usage(reserved_id):
        llm_mod._record_usage(
            types.SimpleNamespace(
                usage_metadata={"input_tokens": 12, "output_tokens": 4},
                response_metadata={},
            ),
            "fast-model",
        )

    assert observation.model_calls[reserved_id].prompt_tokens == 12
    assert observation.model_calls[reserved_id].completion_tokens == 4
    assert observation.model_calls[ordinary_id].prompt_tokens == 0
    assert observation.model_calls[ordinary_id].completion_tokens == 0


async def test_memory_context_and_compaction_cost_are_logged_without_content(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """메모리 로그는 숫자·boolean만 담고 예약 압축 호출 비용을 정확히 분리한다."""
    monkeypatch.setattr(
        observability,
        "get_settings",
        lambda: types.SimpleNamespace(
            pii_hash_pepper="test-pepper",
            model_price_in_per_1k={"fast-model": 0.01},
            model_price_cached_in_per_1k={"fast-model": 0.001},
            model_price_cache_write_per_1k={"fast-model": 0.0125},
            model_price_out_per_1k={"fast-model": 0.02},
        ),
    )
    observation = await _obs("memory-metrics")
    observation.record_memory_context(
        recent_tokens=120,
        situation_tokens=80,
        evicted_tokens=1_300,
        compaction_triggered=True,
    )
    call_id = observation.record_model_call(
        "fast-model",
        usage_reserved=True,
        purpose="memory_compaction",
    )
    observation.record_model_usage(
        "fast-model",
        prompt_tokens=1_000,
        completion_tokens=100,
        call_id=call_id,
        cached_input_tokens=400,
        cache_write_tokens=100,
    )

    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(1.0, TurnStatus.COMPLETED)

    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and item.getMessage().startswith("{")
    )
    assert record["recentHistoryTokens"] == 120
    assert record["situationMemoryTokens"] == 80
    assert record["evictedHistoryTokens"] == 1_300
    assert record["memoryCompactionTriggered"] is True
    assert record["memoryCompactionPromptTokens"] == 1_000
    assert record["memoryCompactionCompletionTokens"] == 100
    assert record["memoryCompactionCostUsd"] == pytest.approx(0.00865)
    assert record["costUsd"] == record["memoryCompactionCostUsd"]
    assert "memory-metrics" not in json.dumps(record, ensure_ascii=False)


async def test_unregistered_model_costs_zero_and_warns(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """단가가 없는 모델 비용은 0이지만 누락을 숨기지 않고 경고한다."""
    monkeypatch.setattr(
        observability,
        "get_settings",
        lambda: types.SimpleNamespace(
            pii_hash_pepper="test-pepper",
            model_price_in_per_1k={},
            model_price_out_per_1k={},
        ),
    )
    observation = await _obs("unknown-price")
    observation.record_model_call("unregistered-model", 1_000, 1_000)

    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(1.0, TurnStatus.COMPLETED)

    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and item.getMessage().startswith("{")
    )
    assert record["costUsd"] == 0
    assert "MODEL_PRICE_MISSING" in caplog.text
    assert "unregistered-model" in caplog.text


@pytest.mark.parametrize(
    ("input_prices", "output_prices"),
    [
        ({"partial-model": 0.01}, {}),
        ({}, {"partial-model": 0.02}),
    ],
)
async def test_partially_registered_model_costs_zero(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    input_prices: dict[str, float],
    output_prices: dict[str, float],
) -> None:
    """입출력 단가 중 하나라도 빠지면 그럴듯한 부분 비용 대신 호출 전체를 0 처리한다."""
    monkeypatch.setattr(
        observability,
        "get_settings",
        lambda: types.SimpleNamespace(
            pii_hash_pepper="test-pepper",
            model_price_in_per_1k=input_prices,
            model_price_out_per_1k=output_prices,
        ),
    )
    observation = await _obs("partial-price")
    observation.record_model_call("partial-model", 1_000, 1_000)

    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(1.0, TurnStatus.COMPLETED)

    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and item.getMessage().startswith("{")
    )
    assert record["costUsd"] == 0
    assert "MODEL_PRICE_MISSING" in caplog.text


async def test_cost_failure_does_not_drop_chat_request(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = await _obs("cost-failure")

    def fail_cost() -> float:
        raise RuntimeError("PRIVATE-COST-CANARY")

    monkeypatch.setattr(observation, "_cost_usd", fail_cost)

    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(1.0, TurnStatus.COMPLETED)

    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and item.getMessage().startswith("{")
    )
    assert record["costUsd"] == 0
    assert "MODEL_COST_CALCULATION_FAILED" in caplog.text
    assert "PRIVATE-COST-CANARY" not in caplog.text


async def test_settings_failure_does_not_drop_chat_request(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """단가 설정 전체 조회 실패도 압축 비용 단계에서 요청 로그를 유실시키지 않는다."""
    observation = await _obs("settings-failure")

    def fail_settings():
        raise RuntimeError("PRIVATE-SETTINGS-CANARY")

    monkeypatch.setattr(observability, "get_settings", fail_settings)

    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(1.0, TurnStatus.COMPLETED)

    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and item.getMessage().startswith("{")
    )
    assert record["costUsd"] == 0
    assert record["memoryCompactionCostUsd"] == 0
    assert "MODEL_COST_CALCULATION_FAILED" in caplog.text
    assert "PRIVATE-SETTINGS-CANARY" not in caplog.text


def test_message_fingerprint_is_not_raw() -> None:
    """지문은 (길이, 해시)이며 원문을 그대로 노출하지 않는다."""
    length, digest = message_fingerprint("hello")
    assert length == 5
    assert digest != "hello"
    assert len(digest) == 16


async def test_graph_error_frame_marks_failed() -> None:
    """그래프가 자체 in-stream error 프레임을 emit하면 저장/로그가 FAILED 로 마감된다(§6.3)."""
    obs = await _obs("ge")

    async def token_then_error(_turn_started_at=None):
        yield 'data: {"type":"token","data":{"text":"부분"}}\n\n'
        yield 'data: {"type":"error","data":{"code":"LLM_UNAVAILABLE","message":"x"}}\n\n'

    resp = await open_stream(_FakeRequest(), "member:ge", token_then_error, observer=obs)
    _ = [c async for c in resp.body_iterator]
    store = await get_conversation_store()
    turn = await store.get_turn(obs.turn_id)
    assert turn is not None
    assert turn.status == TurnStatus.FAILED


async def test_conversation_scoped_by_identity() -> None:
    """서로 다른 신원이 같은 session_id 를 써도 대화가 섞이지 않는다(IDOR 방지)."""
    store = await get_conversation_store()
    id_a = Identity(user_id="A", is_guest=False, seller_id=None, subject="A")
    id_b = Identity(user_id="B", is_guest=False, seller_id=None, subject="B")
    await start_observation(
        request_id="r", identity=id_a, conversation_id="s1", message="a", store=store, now=0.0
    ).commit_user_message()
    await start_observation(
        request_id="r", identity=id_b, conversation_id="s1", message="b", store=store, now=0.0
    ).commit_user_message()
    turns_a = await store.turns_for(conversation_key("A", "s1"))
    turns_b = await store.turns_for(conversation_key("B", "s1"))
    assert len(turns_a) == 1 and turns_a[0].user_text == "a"
    assert len(turns_b) == 1 and turns_b[0].user_text == "b"


async def test_inner_factory_sync_error_releases_and_marks_failed() -> None:
    """inner_factory 동기 예외 시 슬롯 해제 + 턴 FAILED 마감(PENDING 영구 잔존 방지)."""
    obs = await _obs("if1")

    def bad_factory(_turn_started_at=None):
        raise RuntimeError("factory boom")

    with pytest.raises(RuntimeError):
        await open_stream(_FakeRequest(), "member:if1", bad_factory, observer=obs)
    assert not get_registry().is_active("member:if1")
    store = await get_conversation_store()
    turn = await store.get_turn(obs.turn_id)
    assert turn is not None and turn.status == TurnStatus.FAILED


async def test_commit_user_message_failure_releases_slot() -> None:
    """대화 저장(commit_user_message) 실패 시에도 스트림 슬롯이 해제된다(영구 누수 방지, PR #48 리뷰).

    이전(인메모리 dict) 구현은 저장이 예외를 던질 수 없었지만, pg-profile 이관 후
    실제 DB 오류로 던질 수 있다 — registry.acquire() 이후 release 담당 try/except
    이전에 있으면 예외가 그대로 전파돼 슬롯이 프로세스 재시작까지 영구히 잠긴다.
    """
    obs = await _obs("slotfail")

    class _FailingStore:
        async def save_user_message(self, *a, **k):
            raise RuntimeError("db down")

        async def finalize_assistant(self, *a, **k):
            return None

        async def get_turn(self, *a, **k):
            return None

        async def turns_for(self, *a, **k):
            return []

    obs.store = _FailingStore()

    async def unreachable(_turn_started_at=None):
        yield "data: never\n\n"

    with pytest.raises(RuntimeError):
        await open_stream(_FakeRequest(), "member:slotfail", unreachable, observer=obs)
    assert not get_registry().is_active("member:slotfail")


@pytest.mark.parametrize("error", [SessionForbidden(), SessionFinalizing(), RuntimeError("boom")])
async def test_commit_user_message_any_failure_releases_slot(error: Exception) -> None:
    obs = await _obs("slot-domain-fail")

    class _FailingStore:
        async def save_user_message(self, *args, **kwargs):
            raise error

        async def finalize_assistant(self, *args, **kwargs):
            return None

        async def get_turn(self, *args, **kwargs):
            return None

        async def turns_for(self, *args, **kwargs):
            return []

    obs.store = _FailingStore()

    async def unreachable(_turn_started_at=None):
        yield "data: never\n\n"

    with pytest.raises(type(error)):
        await open_stream(
            _FakeRequest(),
            "member:slot-domain-fail",
            unreachable,
            observer=obs,
        )
    assert not get_registry().is_active("member:slot-domain-fail")


async def test_observation_stores_and_logs_committed_buyer_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = await get_conversation_store()
    observation = start_observation(
        request_id="buyer-context",
        identity=Identity(
            user_id="7",
            is_guest=False,
            seller_id=None,
            subject="7",
            session_id="buyer-session",
        ),
        conversation_id="buyer-session",
        thread_id="buyer-thread",
        message="질문",
        store=store,
        now=0.0,
        buyer_session=BuyerSessionInput(
            session_id="buyer-session",
            thread_id="buyer-thread",
            owner_type="member",
            owner_id="7",
        ),
    )

    await observation.commit_user_message()
    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(1.0, TurnStatus.COMPLETED)

    assert observation.turn_id is not None
    assert observation.context_id is not None
    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and item.getMessage().startswith("{")
    )
    assert record["contextFp"] == identifier_fingerprint(observation.context_id)


async def test_seller_observation_fingerprints_int_brand_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """운영 JWKS 판매자 경로의 brandId 는 int 다(auth.py `type is int` 강제).

    지문 계산 전 str() 캐스팅이 빠지면 safe_fingerprint 의 `.encode` 에서 AttributeError 가
    나고, 그 예외가 chat_request 구조화 로그와 trace.finish() 앞에서 터져 판매자 채널의
    관측이 매 요청 조용히 사라진다(호출부가 예외를 삼켜 SSE 는 정상으로 보인다).
    """
    store = await get_conversation_store()
    observation = start_observation(
        request_id="seller-int-brand",
        identity=Identity(
            user_id="7",
            is_guest=False,
            seller_id="7",
            brand_id=3,
            subject="7",
        ),
        conversation_id="seller-session",
        thread_id="seller-thread",
        message="질문",
        store=store,
        now=0.0,
    )

    await observation.commit_user_message()
    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(1.0, TurnStatus.COMPLETED)

    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and item.getMessage().startswith("{")
    )
    assert record["brandFp"] == identifier_fingerprint("3")
    assert record["brandFp"] not in ("3", 3)


async def test_seller_style_observation_logs_null_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = await get_conversation_store()
    observation = start_observation(
        request_id="seller-context",
        identity=Identity(
            user_id="7",
            is_guest=False,
            seller_id="7",
            brand_id="3",
            subject="7",
        ),
        conversation_id="seller-session",
        thread_id="seller-thread",
        message="질문",
        store=store,
        now=0.0,
    )

    await observation.commit_user_message()
    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(1.0, TurnStatus.COMPLETED)

    assert isinstance(
        CommittedTurn(observation.turn_id or "", observation.context_id),
        CommittedTurn,
    )
    assert observation.context_id is None
    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and item.getMessage().startswith("{")
    )
    assert record["contextFp"] is None


@pytest.mark.parametrize(
    ("raw_subject", "sub_type"),
    [("member-rate-limit-canary", "member"), ("guest-rate-limit-canary", "guest")],
)
def test_subject_rate_limit_logs_only_fingerprints(
    caplog: pytest.LogCaptureFixture,
    raw_subject: str,
    sub_type: str,
) -> None:
    """실 429 member/guest sub 경로는 raw subject/IP 대신 fingerprint만 기록한다."""
    headers = _bearer(raw_subject, sub_type)
    with caplog.at_level(logging.INFO, logger="observability"):
        for i in range(11):
            client.post(
                "/chat",
                json={"sessionId": f"rlo-{i}", "threadId": "t", "message": "m"},
                headers=headers,
            )
    logs = [json.loads(r.getMessage()) for r in caplog.records if r.name == "observability"]
    rate_logs = [entry for entry in logs if entry.get("errorType") == "RATE_LIMITED"]
    assert rate_logs, "429 구조화 로그 없음"
    record = rate_logs[0]
    serialized = json.dumps(record, ensure_ascii=False)
    assert record["streamStatus"] is None
    assert record["scopeFp"]
    assert record["scopeType"] == "sub"
    assert record["scopeFp"] == identifier_fingerprint(f"sub:{raw_subject}")
    assert record["ownerFp"] == identifier_fingerprint(raw_subject)
    assert record["ipFp"] == identifier_fingerprint("testclient")
    assert raw_subject not in serialized
    assert f"sub:{raw_subject}" not in serialized
    assert "testclient" not in serialized
    assert '"scope"' not in serialized
    assert raw_subject not in caplog.text
    assert "testclient" not in caplog.text
    assert record["lane"] is None  # 스트림 전 거부라 라우팅 결과가 아직 없다.
    assert record["degraded"] is False
    assert record["degradeReason"] is None
    assert record["costUsd"] == 0
    assert record["toolCalls"] == 0


def test_ip_fallback_rate_limit_logs_only_fingerprint(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """무토큰 IP fallback의 실 429도 raw IP를 남기지 않는다."""
    import app.core.ratelimit as ratelimit

    settings = get_settings().model_copy(
        update={"rate_limit_per_min": 1, "rate_limit_per_hour": 1, "rate_limit_host_multiplier": 1}
    )
    monkeypatch.setattr(ratelimit, "get_settings", lambda: settings)
    with caplog.at_level(logging.INFO, logger="observability"):
        client.post("/chat", json={"sessionId": "ip-1", "threadId": "t", "message": "m"})
        response = client.post(
            "/chat",
            json={
                "sessionId": "ip-secret-session",
                "threadId": "secret-thread",
                "message": "secret",
            },
        )

    assert response.status_code == 429
    record = next(
        json.loads(item.getMessage())
        for item in caplog.records
        if item.name == "observability" and '"errorType": "RATE_LIMITED"' in item.getMessage()
    )
    serialized = json.dumps(record, ensure_ascii=False)
    assert record["scopeType"] == "ip"
    assert record["scopeFp"] == identifier_fingerprint("ip:testclient")
    assert record["ipFp"] == identifier_fingerprint("testclient")
    for raw in ("testclient", "ip:testclient", "ip-secret-session", "secret-thread", "secret"):
        assert raw not in serialized
        assert raw not in caplog.text


def test_rejection_sanitizer_absorbs_guest_and_drops_unknown_identifier_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="observability"):
        emit_rejection(
            "req-sanitize",
            "RATE_LIMITED",
            guestId="guest-raw",
            scope="sub:guest-raw",
            ip="203.0.113.10",
            path="/chat",
            arbitraryIdentifier="must-not-leak",
            ownerFp="raw-owner-disguised-as-fingerprint",
            token="raw-token",
            message="raw-message",
            Authorization="Bearer raw-token",
        )

    record = json.loads(caplog.records[-1].getMessage())
    serialized = json.dumps(record, ensure_ascii=False)
    assert record["ownerFp"] == identifier_fingerprint("guest-raw")
    assert record["scopeFp"] == identifier_fingerprint("sub:guest-raw")
    assert record["scopeType"] == "sub"
    assert record["ipFp"] == identifier_fingerprint("203.0.113.10")
    assert record["path"] == "/chat"
    for raw in (
        "guest-raw",
        "sub:guest-raw",
        "203.0.113.10",
        "must-not-leak",
        "raw-owner-disguised-as-fingerprint",
        "raw-token",
        "raw-message",
    ):
        assert raw not in serialized
        assert raw not in caplog.text
    for key in ("guestId", "scope", "ip", "arbitraryIdentifier"):
        assert key not in record


def test_rejection_accepts_only_branded_fingerprints_and_validated_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    branded_owner = observability.fingerprint_log_value("member-42")
    branded_scope = observability.fingerprint_log_value("sub:member-42")
    branded_ip = observability.fingerprint_log_value("203.0.113.10")
    with caplog.at_level(logging.INFO, logger="observability"):
        emit_rejection(
            "req-branded",
            "RATE_LIMITED",
            ownerFp=branded_owner,
            scopeFp=branded_scope,
            ipFp=branded_ip,
            scopeType="sub",
            path="/chat",
            role="member",
            status="FAILED",
            action="confirm",
        )

    record = json.loads(caplog.records[-1].getMessage())
    assert record["ownerFp"] == str(branded_owner)
    assert record["scopeFp"] == str(branded_scope)
    assert record["ipFp"] == str(branded_ip)
    assert record["scopeType"] == "sub"
    assert record["path"] == "/chat"
    assert record["role"] == "member"
    assert record["status"] == "FAILED"
    assert record["action"] == "confirm"
    assert json.loads(json.dumps(record)) == record


def test_rejection_drops_plain_hex_fingerprints_and_untrusted_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_values = (
        "deadbeefdeadbeef",
        "0123456789abcdef",
        "/chat?token=raw-token",
        "seller raw-subject",
        "FAILED raw-exception",
        "confirm raw-message",
        "raw-token",
        "raw-message",
    )
    with caplog.at_level(logging.INFO, logger="observability"):
        emit_rejection(
            "req-untrusted",
            "RATE_LIMITED",
            ownerFp=raw_values[0],
            scopeFp=raw_values[1],
            ipFp=raw_values[0],
            scopeType="sub:raw-subject",
            path=raw_values[2],
            role=raw_values[3],
            status=raw_values[4],
            action=raw_values[5],
            token=raw_values[6],
            message=raw_values[7],
            Authorization="Bearer raw-token",
        )

    record = json.loads(caplog.records[-1].getMessage())
    serialized = json.dumps(record, ensure_ascii=False)
    assert record["ownerFp"] is None
    assert record["scopeFp"] is None
    assert record["ipFp"] is None
    assert record["scopeType"] is None
    assert "path" not in record
    assert "role" not in record
    assert "status" not in record
    assert "action" not in record
    for raw in raw_values:
        assert raw not in serialized
        assert raw not in caplog.text


def test_rejection_fingerprints_raw_sixteen_digit_identifiers_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = "1234567890123456"
    raw_scope = f"sub:{raw}"
    with caplog.at_level(logging.INFO, logger="observability"):
        emit_rejection(
            "req-raw-sixteen",
            "RATE_LIMITED",
            ownerId=raw,
            scope=raw_scope,
            ip=raw,
            ownerFp=raw,
            scopeFp=raw,
            ipFp=raw,
        )

    record = json.loads(caplog.records[-1].getMessage())
    serialized = json.dumps(record, ensure_ascii=False)
    assert record["ownerFp"] == identifier_fingerprint(raw)
    assert record["scopeFp"] == identifier_fingerprint(raw_scope)
    assert record["ipFp"] == identifier_fingerprint(raw)
    assert raw not in serialized
    assert raw_scope not in serialized
    assert raw not in caplog.text


async def test_error_frame_terminates_stream() -> None:
    """in-stream error 후 스트림을 종결 — 이후 이벤트가 응답·저장소를 오염시키지 않는다."""
    obs = await _obs("et")

    async def token_error_token(_turn_started_at=None):
        yield 'data: {"type":"token","data":{"text":"before"}}\n\n'
        yield 'data: {"type":"error","data":{"code":"LLM_UNAVAILABLE","message":"x"}}\n\n'
        yield 'data: {"type":"token","data":{"text":"AFTER"}}\n\n'

    resp = await open_stream(_FakeRequest(), "member:et", token_error_token, observer=obs)
    text = "".join([c async for c in resp.body_iterator])
    assert "before" in text and "error" in text
    assert "AFTER" not in text  # error 이후 종결
    store = await get_conversation_store()
    turn = await store.get_turn(obs.turn_id)
    assert turn is not None and turn.status == TurnStatus.FAILED
    assert "AFTER" not in turn.assistant_text  # 저장 오염 없음


def test_message_length_limit_rejected() -> None:
    """상한 초과 message 는 400(메모리·PII 방어)."""
    r = client.post("/chat", json={"sessionId": "ml", "threadId": "t", "message": "x" * 100000})
    assert r.status_code == 400


async def test_store_evicts_oldest_beyond_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """저장소가 상한을 넘으면 오래된 턴부터 축출한다(무제한 증가 방지, 인메모리 폴백 한정)."""
    store = await get_conversation_store()
    monkeypatch.setattr(type(store), "_MAX_TURNS", 2)
    for text in ("a", "b", "c"):
        tid = await store.save_user_message("k", "u", "member", text)
        await store.finalize_assistant(tid, "x", TurnStatus.COMPLETED)  # 확정 → 축출 대상
    assert len(store._turns) == 2  # 확정된 오래된 턴(a) 축출


def test_fingerprint_uses_pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    """pepper 가 바뀌면 지문이 달라진다(HMAC — salt 없는 sha256 역산 방어)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "pii_hash_pepper", "pep1")
    _, d1 = message_fingerprint("hello")
    monkeypatch.setattr(settings, "pii_hash_pepper", "pep2")
    _, d2 = message_fingerprint("hello")
    assert d1 != d2


def test_pepper_required_in_jwks_mode() -> None:
    """운영(jwks)에서 pii_hash_pepper 미주입이면 Settings 기동이 실패한다."""
    from app.core.config import Settings

    with pytest.raises(Exception):
        Settings(auth_mode="jwks", pii_hash_pepper="", jwks_url="http://x")
    # dev 모드는 빈 pepper 허용
    Settings(auth_mode="dev", pii_hash_pepper="")


async def test_eviction_skips_pending_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """진행 중(PENDING) 턴은 상한을 넘겨도 축출되지 않는다(응답 유실 방지, 인메모리 폴백 한정)."""
    store = await get_conversation_store()
    monkeypatch.setattr(type(store), "_MAX_TURNS", 1)
    pending = await store.save_user_message("k", "u", "member", "in-flight")  # 미완(PENDING)
    for i in range(3):
        tid = await store.save_user_message("k", "u", "member", f"m{i}")
        await store.finalize_assistant(tid, "done", TurnStatus.COMPLETED)
    turn = await store.get_turn(pending)
    assert turn is not None and turn.status == TurnStatus.PENDING


async def test_409_does_not_store_ghost_turn() -> None:
    """409(동시 스트림 거절) 요청은 대화 저장소에 유령 턴을 남기지 않는다(save-after-acquire)."""
    from app.core.conversation import conversation_key
    from app.core.stream import get_registry

    store = await get_conversation_store()
    # dev 게스트 → registry_key/conversation_key owner="anon"
    await get_registry().acquire("anon:t")  # 동일 threadId 슬롯 선점 → 다음 요청은 409
    try:
        r = client.post("/chat", json={"sessionId": "dup", "threadId": "t", "message": "중복요청"})
        assert r.status_code == 409
    finally:
        await get_registry().release("anon:t")
    assert await store.turns_for(conversation_key(None, "dup")) == []  # 유령 턴 없음


def test_identifier_length_limit_rejected() -> None:
    """상한 초과 sessionId/threadId 는 400(불투명 키 남용 방어)."""
    r = client.post("/chat", json={"sessionId": "s" * 10000, "threadId": "t", "message": "m"})
    assert r.status_code == 400


@pytest.mark.parametrize(
    ("scenario", "expected_status", "expected_error", "expected_reason"),
    (
        ("normal_done", TurnStatus.COMPLETED, None, "done"),
        ("first_event_timeout", TurnStatus.FAILED, "UPSTREAM_TIMEOUT", "first_event_timeout"),
        ("graph_error_frame", TurnStatus.FAILED, "GRAPH_FAILED", "error_frame"),
        ("disconnect", TurnStatus.CANCELLED, None, "client_disconnect"),
        ("total_cap", TurnStatus.COMPLETED, None, "total_timeout_stop"),
        ("tool_exception", TurnStatus.FAILED, "INTERNAL", "tool_error"),
    ),
)
async def test_open_stream_exports_one_root_for_each_terminal_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_status: TurnStatus,
    expected_error: str | None,
    expected_reason: str,
) -> None:
    from app.core.tracing import trace_span

    settings = get_settings()
    monkeypatch.setattr(settings, "stream_disconnect_poll_s", 0.005)
    if scenario == "first_event_timeout":
        monkeypatch.setattr(settings, "stream_first_token_timeout_s", 0.01)
    if scenario == "total_cap":
        monkeypatch.setattr(settings, "stream_total_timeout_s", 0.02)

    exporter = FakeTraceExporter()
    obs = await _obs(f"matrix-{scenario}", trace=_trace(exporter))

    async def stream(_turn_started_at=None):
        with trace_span(f"{scenario}_first_pull", "chain"):
            pass
        if scenario == "first_event_timeout":
            await asyncio.sleep(1)
            yield "data: never\n\n"
            return
        if scenario == "normal_done":
            yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'
            return
        if scenario == "graph_error_frame":
            yield 'data: {"type":"error","data":{"code":"GRAPH_FAILED"}}\n\n'
            return

        yield 'data: {"type":"meta","data":{"lane":"test"}}\n\n'
        with trace_span(f"{scenario}_second_pull", "tool"):
            if scenario == "tool_exception":
                raise RuntimeError("tool failed")
        await asyncio.sleep(1)
        yield "data: never\n\n"

    request = _FakeRequest(disconnected=scenario == "disconnect")
    if scenario == "first_event_timeout":
        with pytest.raises(HTTPException) as exc_info:
            await open_stream(request, f"member:matrix-{scenario}", stream, observer=obs)
        assert exc_info.value.status_code == 504
    else:
        response = await open_stream(request, f"member:matrix-{scenario}", stream, observer=obs)
        _ = [chunk async for chunk in response.body_iterator]

    assert len(exporter.exported) == 1
    nodes = exporter.exported[0]
    roots = [node for node in nodes if node.parent_id is None]
    assert len(roots) == 1
    root = roots[0]
    assert root.error_type == expected_error
    assert root.metadata["terminalReason"] == expected_reason
    expected_node_count = (
        2
        if scenario
        in {
            "normal_done",
            "first_event_timeout",
            "graph_error_frame",
        }
        else 3
    )
    assert len(nodes) == expected_node_count
    node_ids = {node.id for node in nodes}
    assert all(node.parent_id in node_ids for node in nodes if node.parent_id is not None)
    turn = await obs.store.get_turn(obs.turn_id)
    assert turn is not None and turn.status == expected_status


async def test_request_observation_isolates_trace_finalization_failure() -> None:
    exporter = FakeTraceExporter()

    def fail_validation(_payload: object) -> None:
        raise RuntimeError("telemetry validator unavailable")

    trace = TraceFactory(
        exporter=exporter,
        enabled=True,
        sampling_rate=1.0,
        payload_validator=fail_validation,
    ).start_request(
        name="buyer_chat_turn",
        request_id="req-1",
        conversation_id="telemetry-safe",
        thread_id="thread-1",
        lane="buyer",
        environment="test",
    )
    obs = await _obs("telemetry-safe", trace=trace)
    await obs.commit_user_message()

    await obs.finish(
        asyncio.get_running_loop().time(),
        TurnStatus.COMPLETED,
        None,
        "done",
    )

    turn = await obs.store.get_turn(obs.turn_id)
    assert turn is not None and turn.status == TurnStatus.COMPLETED
    assert exporter.exported == []


async def test_outer_cancellation_during_first_pull_owns_prestream_cleanup() -> None:
    exporter = FakeTraceExporter()
    obs = await _obs("outer-cancel-first-pull", trace=_trace(exporter))
    started = asyncio.Event()
    closed = asyncio.Event()
    inner_task: asyncio.Task | None = None

    async def sleeping_first_pull(_turn_started_at=None):
        nonlocal inner_task
        inner_task = asyncio.current_task()
        try:
            started.set()
            await asyncio.sleep(10)
            yield "data: never\n\n"
        finally:
            closed.set()

    stream_key = "member:outer-cancel-first-pull"
    outer_task = asyncio.create_task(
        open_stream(_FakeRequest(), stream_key, sleeping_first_pull, observer=obs)
    )
    await started.wait()
    outer_task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await outer_task
        await asyncio.sleep(0)
        snapshot = (
            closed.is_set(),
            inner_task is not None and inner_task.done(),
            not get_registry().is_active(stream_key),
            len(exporter.exported),
        )
    finally:
        if inner_task is not None and not inner_task.done():
            inner_task.cancel()
            await asyncio.gather(inner_task, return_exceptions=True)
        await get_registry().release(stream_key)

    assert snapshot == (True, True, True, 1)
    (root,) = exporter.exported[0]
    assert root.error_type is None
    assert root.metadata["terminalReason"] == "client_disconnect"


async def test_close_failure_after_first_frame_preserves_cancel_outcome(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(get_settings(), "stream_disconnect_poll_s", 0.005)
    exporter = FakeTraceExporter()
    obs = await _obs("close-failure-stream", trace=_trace(exporter))

    class CloseFailIterator:
        def __init__(self, _turn_started_at=None) -> None:
            self.pulls = 0

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            self.pulls += 1
            if self.pulls == 1:
                return 'data: {"type":"meta","data":{"lane":"test"}}\n\n'
            await asyncio.sleep(10)
            raise StopAsyncIteration

        async def aclose(self) -> None:
            raise RuntimeError("sensitive close detail")

    stream_key = "member:close-failure-stream"
    response = await open_stream(
        _FakeRequest(disconnected=True),
        stream_key,
        CloseFailIterator,
        observer=obs,
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert len(chunks) == 1
    assert not get_registry().is_active(stream_key)
    assert len(exporter.exported) == 1
    (root, *_) = exporter.exported[0]
    assert root.error_type is None
    assert root.metadata["terminalReason"] == "client_disconnect"
    assert "stream iterator close failed code=STREAM_CLOSE_FAILED" in caplog.messages
    assert "sensitive close detail" not in caplog.text


async def test_close_failure_during_prestream_abort_preserves_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(get_settings(), "stream_first_token_timeout_s", 0.01)
    monkeypatch.setattr(get_settings(), "stream_disconnect_poll_s", 0.005)
    exporter = FakeTraceExporter()
    obs = await _obs("close-failure-prestream", trace=_trace(exporter))

    class CloseFailIterator:
        def __init__(self, _turn_started_at=None) -> None:
            pass

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            await asyncio.sleep(10)
            raise StopAsyncIteration

        async def aclose(self) -> None:
            raise RuntimeError("sensitive close detail")

    stream_key = "member:close-failure-prestream"
    with pytest.raises(HTTPException) as exc_info:
        await open_stream(
            _FakeRequest(),
            stream_key,
            CloseFailIterator,
            observer=obs,
        )

    assert exc_info.value.status_code == 504
    assert not get_registry().is_active(stream_key)
    assert len(exporter.exported) == 1
    (root, *_) = exporter.exported[0]
    assert root.error_type == "UPSTREAM_TIMEOUT"
    assert root.metadata["terminalReason"] == "first_event_timeout"
    assert "stream iterator close failed code=STREAM_CLOSE_FAILED" in caplog.messages
    assert "sensitive close detail" not in caplog.text


async def test_cancellation_during_terminal_close_propagates_after_finalization() -> None:
    exporter = FakeTraceExporter()
    obs = await _obs("cancel-during-terminal-close", trace=_trace(exporter))
    close_started = asyncio.Event()

    class BlockingCloseIterator:
        def __init__(self, _turn_started_at=None) -> None:
            self.pulled = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            if self.pulled:
                raise StopAsyncIteration
            self.pulled = True
            return 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

        async def aclose(self) -> None:
            close_started.set()
            await asyncio.Event().wait()

    stream_key = "member:cancel-during-terminal-close"
    response = await open_stream(
        _FakeRequest(),
        stream_key,
        BlockingCloseIterator,
        observer=obs,
    )

    async def consume() -> list[str]:
        return [chunk async for chunk in response.body_iterator]

    consumer = asyncio.create_task(consume())
    await close_started.wait()
    consumer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert not get_registry().is_active(stream_key)
    assert len(exporter.exported) == 1
    (root, *_) = exporter.exported[0]
    assert root.error_type is None
    assert root.metadata["terminalReason"] == "done"
    turn = await obs.store.get_turn(obs.turn_id)
    assert turn is not None and turn.status == TurnStatus.COMPLETED


async def test_trace_spans_across_yields_keep_parentage_without_clobbering_host_stack() -> None:
    from app.core.tracing import bind_request_trace, trace_span

    streamed_exporter = FakeTraceExporter()
    streamed_trace = _trace(streamed_exporter)
    obs = await _obs("cross-yield-parentage", trace=streamed_trace)
    host_exporter = FakeTraceExporter()
    host_trace = _trace(host_exporter)

    async def stream(_turn_started_at=None):
        with trace_span("stream.outer", "chain"):
            yield 'data: {"type":"meta","data":{"lane":"test"}}\n\n'
            with trace_span("stream.inner", "tool"):
                yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    with bind_request_trace(host_trace):
        with trace_span("host", "chain"):
            response = await open_stream(
                _FakeRequest(),
                "member:cross-yield-parentage",
                stream,
                observer=obs,
            )
            assert len([chunk async for chunk in response.body_iterator]) == 2
            with trace_span("after_close", "chain"):
                pass
    await host_trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    streamed = {node.name: node for node in streamed_exporter.exported[0]}
    assert streamed["stream.outer"].parent_id == streamed["buyer_chat_turn"].id
    assert streamed["stream.inner"].parent_id == streamed["stream.outer"].id
    host = {node.name: node for node in host_exporter.exported[0]}
    assert host["after_close"].parent_id == host["host"].id


async def test_open_stream_uses_one_task_for_every_pull_and_close() -> None:
    tasks: list[asyncio.Task | None] = []

    class RecordingIterator:
        def __init__(self, _turn_started_at=None) -> None:
            self.pulls = 0

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            tasks.append(asyncio.current_task())
            self.pulls += 1
            if self.pulls == 1:
                return 'data: {"type":"meta","data":{"lane":"test"}}\n\n'
            return 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

        async def aclose(self) -> None:
            tasks.append(asyncio.current_task())

    response = await open_stream(
        _FakeRequest(),
        "member:single-producer-task",
        RecordingIterator,
    )
    assert len([chunk async for chunk in response.body_iterator]) == 2
    assert len(tasks) == 3
    assert len(set(tasks)) == 1
    assert not get_registry().is_active("member:single-producer-task")


async def test_total_timeout_cancels_and_awaits_same_task_pump_without_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "stream_total_timeout_s", 0.02)
    monkeypatch.setattr(get_settings(), "stream_disconnect_poll_s", 0.005)
    exporter = FakeTraceExporter()
    obs = await _obs("forced-stop-pump", trace=_trace(exporter))
    tasks: list[asyncio.Task | None] = []
    closed = asyncio.Event()

    class BlockingIterator:
        def __init__(self, _turn_started_at=None) -> None:
            self.pulls = 0

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            tasks.append(asyncio.current_task())
            self.pulls += 1
            if self.pulls == 1:
                return 'data: {"type":"meta","data":{"lane":"test"}}\n\n'
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            tasks.append(asyncio.current_task())
            closed.set()

    key = "member:forced-stop-pump"
    response = await open_stream(_FakeRequest(), key, BlockingIterator, observer=obs)
    chunks = [chunk async for chunk in response.body_iterator]

    assert len(chunks) == 2
    assert '"type": "done"' in chunks[-1]
    assert closed.is_set()
    assert len(set(tasks)) == 1
    assert tasks[0] is not None and tasks[0].done()
    assert not get_registry().is_active(key)
    assert len(exporter.exported) == 1
    assert exporter.exported[0][0].metadata["terminalReason"] == "total_timeout_stop"


async def test_cancelled_finish_keeps_one_cleanup_until_turn_log_and_trace_complete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """finish 대기자가 취소돼도 공유 cleanup은 계속되고 재호출과 합쳐 정확히 1회 실행된다."""
    exporter = FakeTraceExporter()
    obs = await _obs("cancel-safe-finish", trace=_trace(exporter))
    await obs.commit_user_message()
    finalize_started = asyncio.Event()
    finalize_proceed = asyncio.Event()
    finalize_calls = 0
    original_finalize = obs.store.finalize_assistant

    async def blocking_finalize(turn_id, assistant_text, status):
        nonlocal finalize_calls
        finalize_calls += 1
        finalize_started.set()
        await finalize_proceed.wait()
        await original_finalize(turn_id, assistant_text, status)

    obs.store.finalize_assistant = blocking_finalize
    stream_key = "member:cancel-safe-finish"

    async def done(_turn_started_at=None):
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    with caplog.at_level(logging.INFO, logger="observability"):
        response = await open_stream(_FakeRequest(), stream_key, done, observer=obs)
        consumer = asyncio.create_task(anext(response.body_iterator))
        assert '"type":"done"' in await consumer
        closing = asyncio.create_task(anext(response.body_iterator))
        await finalize_started.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

        assert not get_registry().is_active(stream_key)
        assert obs.finished is False
        finalize_proceed.set()
        await asyncio.gather(
            obs.finish(
                asyncio.get_running_loop().time(),
                TurnStatus.COMPLETED,
                None,
                "done",
            ),
            obs.finish(
                asyncio.get_running_loop().time(),
                TurnStatus.FAILED,
                "INTERNAL",
                "retry_must_not_replace",
            ),
        )

    assert finalize_calls == 1
    assert obs.finished is True
    turn = await obs.store.get_turn(obs.turn_id)
    assert turn is not None and turn.status == TurnStatus.COMPLETED
    assert len(exporter.exported) == 1
    records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "observability" and record.getMessage().startswith("{")
    ]
    assert sum(record.get("event") == "chat_request" for record in records) == 1
