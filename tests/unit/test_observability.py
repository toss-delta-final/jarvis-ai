"""대화 저장 & 관측 (이슈 #8) — api-spec §6.3 (대화 저장 + 구조화 로그·PII)."""

from __future__ import annotations

import asyncio
import json
import logging
import types

import pytest
from fastapi.testclient import TestClient

from app.core.auth import Identity
from app.core.conversation import TurnStatus, get_conversation_store
from app.core.config import get_settings
from app.core.observability import message_fingerprint, start_observation
from app.core.stream import open_stream
from app.main import app

client = TestClient(app)


class _FakeRequest:
    def __init__(self, disconnected: bool = False) -> None:
        self._disc = disconnected
        self.state = types.SimpleNamespace()

    async def is_disconnected(self) -> bool:
        return self._disc


def _obs(conversation_id: str, message: str = "질문"):
    identity = Identity(user_id="u1", is_guest=False, seller_id=None, subject="u1")
    return start_observation(
        request_id="req-1",
        identity=identity,
        conversation_id=conversation_id,
        message=message,
        store=get_conversation_store(),
        now=asyncio.get_event_loop().time(),
    )


# ─────────── §6.3 (a) 대화 저장 ───────────


def test_chat_records_completed_turn() -> None:
    """정상 스트림 완료 후 턴이 COMPLETED + user 원문 + assistant 부분 누적으로 저장된다."""
    msg = "여행용 방수 케이스 추천해줘"
    r = client.post("/chat", json={"sessionId": "c1", "threadId": "t", "message": msg})
    assert r.status_code == 200
    _ = r.text  # 스트림 소비 → finalize
    turns = get_conversation_store().turns_for("c1")
    assert len(turns) == 1
    assert turns[0].user_text == msg
    assert turns[0].status == TurnStatus.COMPLETED
    assert "(stub)" in turns[0].assistant_text


async def test_partial_text_preserved_on_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """클라이언트 취소 시 상태 CANCELLED + 부분 생성 텍스트를 보존한다(§6.3 a)."""
    monkeypatch.setattr(get_settings(), "stream_disconnect_poll_s", 0.02)
    obs = _obs("cx")

    async def token_then_idle():
        yield 'data: {"type":"token","data":{"text":"부분응답"}}\n\n'
        await asyncio.sleep(2.0)
        yield "data: never\n\n"

    resp = await open_stream(_FakeRequest(disconnected=True), "member:cx", token_then_idle, observer=obs)
    _ = [c async for c in resp.body_iterator]
    turn = get_conversation_store().get_turn(obs.turn_id)
    assert turn is not None
    assert turn.status == TurnStatus.CANCELLED
    assert "부분응답" in turn.assistant_text


async def test_partial_text_preserved_on_error() -> None:
    """스트림 중 상류 오류 시 상태 FAILED + 부분 텍스트를 보존한다(§6.3 a)."""
    obs = _obs("ce")

    async def token_then_boom():
        yield 'data: {"type":"token","data":{"text":"조금"}}\n\n'
        raise RuntimeError("mid-stream boom")

    resp = await open_stream(_FakeRequest(), "member:ce", token_then_boom, observer=obs)
    _ = [c async for c in resp.body_iterator]
    turn = get_conversation_store().get_turn(obs.turn_id)
    assert turn is not None
    assert turn.status == TurnStatus.FAILED
    assert "조금" in turn.assistant_text


# ─────────── §6.3 (b) 구조화 로그 + PII ───────────


def test_structured_log_has_fields_and_hides_raw_message(caplog: pytest.LogCaptureFixture) -> None:
    """요청 구조화 로그가 §6.3 b 필드를 담되, 사용자 message 원문은 남기지 않는다(길이/해시만)."""
    msg = "SECRET_QUERY_비밀질의_XYZ"
    with caplog.at_level(logging.INFO, logger="observability"):
        r = client.post("/chat", json={"sessionId": "c2", "threadId": "t", "message": msg})
        _ = r.text
    logs = [rec.getMessage() for rec in caplog.records if rec.name == "observability"]
    assert logs, "관측 로그가 없음"
    record = json.loads(logs[-1])
    for key in (
        "requestId", "conversationId", "latencyTotalMs", "streamStatus",
        "messageLength", "messageHash", "model", "promptTokens", "completionTokens",
    ):
        assert key in record, f"필드 누락: {key}"
    assert record["streamStatus"] == "COMPLETED"
    assert record["conversationId"] == "c2"
    assert record["messageLength"] == len(msg)
    # [PII] 원문은 로그 어디에도 없고, 해시만 있다.
    assert msg not in logs[-1]
    _, digest = message_fingerprint(msg)
    assert record["messageHash"] == digest


def test_message_fingerprint_is_not_raw() -> None:
    """지문은 (길이, 해시)이며 원문을 그대로 노출하지 않는다."""
    length, digest = message_fingerprint("hello")
    assert length == 5
    assert digest != "hello"
    assert len(digest) == 16
