"""Buyer route root-trace integration for the full SSE lifecycle."""

from __future__ import annotations

import asyncio
import types

import pytest
from fastapi.testclient import TestClient

from app.core.auth import Identity
from app.core.tracing import FakeTraceExporter, TraceFactory, set_trace_factory
from app.main import app
from app.schemas.chat import ChatRequest

client = TestClient(app)


class CapturingTraceFactory(TraceFactory):
    def __init__(self) -> None:
        self.exporter = FakeTraceExporter()
        super().__init__(exporter=self.exporter, enabled=True, sampling_rate=1.0)


@pytest.fixture
def fake_trace_factory(monkeypatch: pytest.MonkeyPatch) -> CapturingTraceFactory:
    monkeypatch.setattr("app.core.errors.new_request_id", lambda: "req-buyer-tracing")
    factory = CapturingTraceFactory()
    set_trace_factory(factory)
    yield factory
    set_trace_factory(None)


def test_buyer_request_exports_one_correlated_root(
    buyer_fakes, fake_trace_factory: CapturingTraceFactory
) -> None:
    response = client.post(
        "/chat",
        json={"sessionId": "s1", "threadId": "t1", "message": "추천해줘"},
    )

    assert response.status_code == 200
    assert len(fake_trace_factory.exporter.exported) == 1
    roots = [node for node in fake_trace_factory.exporter.exported[0] if node.parent_id is None]
    assert len(roots) == 1
    assert roots[0].name == "buyer_chat_turn"
    assert roots[0].metadata["requestId"] == response.headers["x-request-id"]


def test_buyer_store_acquisition_failure_finishes_root(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    import app.api.chat as chat_api

    async def fail_store():
        raise RuntimeError("pg-profile down")

    monkeypatch.setattr(chat_api, "get_conversation_store", fail_store)

    with pytest.raises(RuntimeError, match="pg-profile down"):
        client.post(
            "/chat",
            json={"sessionId": "buyer-store", "threadId": "t-store", "message": "질문"},
        )

    assert len(fake_trace_factory.exporter.exported) == 1
    (root,) = fake_trace_factory.exporter.exported[0]
    assert root.name == "buyer_chat_turn"
    assert root.error_type == "INTERNAL"
    assert root.metadata["terminalReason"] == "store_unavailable"


async def test_buyer_store_acquisition_cancellation_finishes_root(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    import app.api.chat as chat_api

    async def cancel_store():
        raise asyncio.CancelledError

    monkeypatch.setattr(chat_api, "get_conversation_store", cancel_store)
    request = ChatRequest(session_id="buyer-cancel", thread_id="t-cancel", message="질문")
    http_request = types.SimpleNamespace(state=types.SimpleNamespace(request_id="req-cancel"))
    identity = Identity(user_id="1", is_guest=False, seller_id=None, subject="1")

    with pytest.raises(asyncio.CancelledError):
        await chat_api.chat(request, http_request, identity)

    assert len(fake_trace_factory.exporter.exported) == 1
    (root,) = fake_trace_factory.exporter.exported[0]
    assert root.error_type is None
    assert root.metadata["terminalReason"] == "client_disconnect"


@pytest.mark.parametrize("failure_point", ("factory", "start_request"))
def test_buyer_telemetry_start_failure_preserves_route(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    buyer_fakes,
    failure_point: str,
) -> None:
    import app.core.tracing as tracing

    tracing.set_trace_factory(None)
    if failure_point == "factory":
        monkeypatch.setattr(
            tracing,
            "TraceFactory",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("telemetry factory failed")),
        )
    else:

        class RaisingFactory:
            def start_request(self, **kwargs):
                raise RuntimeError("telemetry start failed")

        tracing.set_trace_factory(RaisingFactory())

    try:
        response = client.post(
            "/chat",
            json={
                "sessionId": f"buyer-{failure_point}",
                "threadId": f"thread-{failure_point}",
                "message": "추천해줘",
            },
        )
    finally:
        tracing.set_trace_factory(None)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type": "done"' in response.text
    assert caplog.messages.count("trace start failed code=TELEMETRY_START_FAILED") == 1
    assert "telemetry factory failed" not in caplog.text
    assert "telemetry start failed" not in caplog.text
