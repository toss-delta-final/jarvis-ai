import asyncio

from app.core.tracing import (
    FakeTraceExporter,
    NoopRequestTrace,
    TraceFactory,
    bind_request_trace,
    current_request_trace,
    trace_span,
)


def _start_trace(factory: TraceFactory):
    return factory.start_request(
        name="buyer_chat_turn",
        request_id="req-1",
        conversation_id="session-1",
        thread_id="thread-1",
        lane="buyer",
        environment="test",
    )


async def test_fake_exporter_receives_one_root_with_nested_children() -> None:
    exporter = FakeTraceExporter()
    factory = TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0)
    trace = _start_trace(factory)

    with bind_request_trace(trace):
        assert current_request_trace() is trace
        with trace_span("buyer.routing", "chain"):
            with trace_span("llm.decompose", "llm"):
                pass

    assert current_request_trace() is None
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")
    await trace.finish(status="FAILED", error_type="INTERNAL", terminal_reason="duplicate")

    nodes = exporter.exported[0]
    roots = [node for node in nodes if node.parent_id is None]
    assert [node.name for node in roots] == ["buyer_chat_turn"]
    assert {node.name for node in nodes} == {
        "buyer_chat_turn",
        "buyer.routing",
        "llm.decompose",
    }
    assert len({node.trace_id for node in nodes}) == 1
    assert len(exporter.exported) == 1

    by_name = {node.name: node for node in nodes}
    assert by_name["buyer.routing"].parent_id == by_name["buyer_chat_turn"].id
    assert by_name["llm.decompose"].parent_id == by_name["buyer.routing"].id
    assert all(node.ended_at is not None for node in nodes)
    assert by_name["buyer_chat_turn"].metadata["terminalReason"] == "done"


async def test_disabled_tracing_exports_nothing_and_allocates_no_nodes() -> None:
    exporter = FakeTraceExporter()
    factory = TraceFactory(exporter=exporter, enabled=False, sampling_rate=1.0)

    trace = _start_trace(factory)

    assert isinstance(trace, NoopRequestTrace)
    with bind_request_trace(trace):
        with trace_span("buyer.routing", "chain"):
            pass
        trace.record_provider_ttft(23)
        trace.mark_degraded("fallback")
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    assert exporter.exported == []


async def test_async_children_create_sibling_spans() -> None:
    exporter = FakeTraceExporter()
    trace = _start_trace(TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0))

    async def create_span(name: str) -> None:
        with trace_span(name, "chain"):
            await asyncio.sleep(0)

    with bind_request_trace(trace):
        with trace_span("buyer.fanout", "chain"):
            await asyncio.gather(create_span("buyer.cart"), create_span("buyer.recommendation"))

    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    nodes = exporter.exported[0]
    by_name = {node.name: node for node in nodes}
    fanout = by_name["buyer.fanout"]
    assert by_name["buyer.cart"].parent_id == fanout.id
    assert by_name["buyer.recommendation"].parent_id == fanout.id


async def test_trace_records_bounded_root_observability_metadata() -> None:
    exporter = FakeTraceExporter()
    trace = _start_trace(TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0))

    trace.record_provider_ttft(42)
    trace.mark_degraded("provider_fallback")
    await trace.finish(status="FAILED", error_type="UPSTREAM", terminal_reason="error")

    root = exporter.exported[0][0]
    assert root.metadata == {
        "requestId": "req-1",
        "conversationId": "session-1",
        "threadId": "thread-1",
        "lane": "buyer",
        "environment": "test",
        "provider_ttft_ms": 42,
        "degraded": True,
        "degradeReason": "provider_fallback",
        "errorType": "UPSTREAM",
        "terminalReason": "error",
    }
    assert root.error_type == "UPSTREAM"


async def test_span_marks_safe_error_type_and_restores_parentage() -> None:
    class StreamCancelled(BaseException):
        pass

    exporter = FakeTraceExporter()
    trace = _start_trace(TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0))

    with bind_request_trace(trace):
        try:
            with trace_span("buyer.streaming", "chain"):
                raise StreamCancelled("sensitive detail")
        except StreamCancelled:
            pass
        with trace_span("buyer.cleanup", "chain"):
            pass

    await trace.finish(status="CANCELLED", error_type="CANCELLED", terminal_reason="cancelled")

    by_name = {node.name: node for node in exporter.exported[0]}
    assert by_name["buyer.streaming"].error_type == "StreamCancelled"
    assert by_name["buyer.cleanup"].parent_id == by_name["buyer_chat_turn"].id


async def test_exporter_failure_is_isolated_and_finish_remains_idempotent(caplog) -> None:
    class FailingExporter:
        def __init__(self) -> None:
            self.calls = 0

        async def export(self, nodes) -> None:
            self.calls += 1
            raise RuntimeError("customer@example.com must not reach logs")

    exporter = FailingExporter()
    trace = _start_trace(TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0))

    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")
    await trace.finish(status="FAILED", error_type="INTERNAL", terminal_reason="duplicate")

    assert exporter.calls == 1
    assert "TELEMETRY_EXPORT_FAILED" in caplog.text
    assert "customer@example.com" not in caplog.text
