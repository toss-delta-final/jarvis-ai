import asyncio
from dataclasses import fields, is_dataclass
from collections.abc import Mapping

import pytest

from app.core.logging import safe_fingerprint
from app.core.tracing import (
    BUYER_DEGRADE_REASON_PRECEDENCE,
    FakeTraceExporter,
    LangSmithTraceExporter,
    NoopRequestTrace,
    SAFE_METADATA_KEYS,
    TraceFactory,
    UnsafeTelemetryError,
    bind_request_trace,
    current_request_trace,
    get_trace_factory,
    set_trace_factory,
    trace_span,
    validate_export_payload,
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


def _recursive_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, bytes):
        return [value.decode("utf-8", errors="replace")]
    if isinstance(value, Mapping):
        return [
            item
            for key, nested in value.items()
            for item in (*_recursive_strings(key), *_recursive_strings(nested))
        ]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for nested in value for item in _recursive_strings(nested)]
    if is_dataclass(value) and not isinstance(value, type):
        return [
            item
            for field in fields(value)
            for item in _recursive_strings(getattr(value, field.name))
        ]
    return []


PRIVACY_CANARIES = {
    "buyer_message": "BUYER-MESSAGE-CANARY-141",
    "seller_message": "SELLER-MESSAGE-CANARY-141",
    "tool_argument": "TOOL-ARGUMENT-CANARY-141",
    "tool_result": "TOOL-RESULT-CANARY-141",
    "provider_exception": "PROVIDER-EXCEPTION-CANARY-141",
    "authorization": "Bearer authorization-canary-141",
    "cookie": "session=cookie-canary-141",
    "customer_name": "Customer Name Canary 141",
    "customer_email": "customer-canary-141@example.com",
    "customer_phone": "010-1414-1414",
    "customer_address": "Customer Address Canary 141",
    "nested_metadata": "NESTED-METADATA-CANARY-141",
}


def _assert_canaries_absent(payload: object) -> None:
    serialized_strings = _recursive_strings(payload)
    for canary in PRIVACY_CANARIES.values():
        assert all(canary not in value for value in serialized_strings)


def _serialized_operation_content(operation: object) -> dict[str, object]:
    return {
        "run_info": operation.deserialize_run_info(),
        "inputs": operation.inputs,
        "outputs": operation.outputs,
        "events": operation.events,
        "attachments": operation.attachments,
    }


@pytest.mark.parametrize("field", ["inputs", "outputs"])
def test_recursive_canary_inspection_includes_serialized_input_output_bytes(
    field: str,
) -> None:
    payload = {"inputs": b"{}", "outputs": b"{}"}
    payload[field] = b'{"message":"' + PRIVACY_CANARIES["buyer_message"].encode() + b'"}'
    with pytest.raises(AssertionError):
        _assert_canaries_absent(payload)


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
        "sessionFp": safe_fingerprint("session-1"),
        "threadFp": safe_fingerprint("thread-1"),
        "lane": "buyer",
        "environment": "test",
        "provider_ttft_ms": 42,
        "degraded": True,
        "degradeReason": "provider_fallback",
        "errorType": "UPSTREAM",
        "terminalReason": "error",
    }
    assert root.error_type == "UPSTREAM"


@pytest.mark.parametrize(
    "reasons",
    [
        BUYER_DEGRADE_REASON_PRECEDENCE,
        tuple(reversed(BUYER_DEGRADE_REASON_PRECEDENCE)),
    ],
)
async def test_buyer_degrade_precedence_is_order_independent(reasons: tuple[str, ...]) -> None:
    exporter = FakeTraceExporter()
    trace = _start_trace(TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0))

    for reason in reasons:
        trace.mark_degraded(reason)
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    root = exporter.exported[0][0]
    assert root.metadata["degraded"] is True
    assert root.metadata["degradeReason"] == "search_failed"


def test_buyer_degrade_precedence_documents_all_six_bounded_reasons() -> None:
    assert BUYER_DEGRADE_REASON_PRECEDENCE == (
        "search_failed",
        "push_skipped",
        "rerank_fallback",
        "fanout_partial",
        "dedup_skipped",
        "cart_merge_skipped",
    )


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


async def test_sensitive_nested_payload_drops_entire_trace(caplog) -> None:
    payload = {
        "inputs": {"nested": [{"authorization": "Bearer abcdefghijklmnop"}]},
        "outputs": {"email": "person@example.com"},
        "exception": {"message": "sk-abcdefghijklmnop1234"},
        "headers": {"cookie": "sid=secret"},
    }
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload(payload)

    exporter = FakeTraceExporter()
    factory = TraceFactory(
        exporter=exporter,
        enabled=True,
        sampling_rate=1.0,
        payload_validator=lambda _payload: (_ for _ in ()).throw(UnsafeTelemetryError("canary")),
    )
    trace = factory.start_request(
        name="buyer_chat_turn",
        request_id="req-secret",
        conversation_id="session-secret",
        thread_id="thread-secret",
        lane="buyer",
        environment="test",
    )
    await trace.finish(status="FAILED", error_type="INTERNAL", terminal_reason="error")

    assert exporter.exported == []
    assert "TELEMETRY_REDACTION_FAILED" in caplog.text
    assert "req-secret" in caplog.text
    assert "buyer_chat_turn" in caplog.text
    assert "canary" not in caplog.text


async def test_default_validator_drops_built_trace_with_unsafe_allowlisted_value(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from langsmith import Client

    unsafe = "attached-canary-141@example.com"
    fake_exporter = FakeTraceExporter()
    fake_trace = _start_trace(TraceFactory(exporter=fake_exporter, enabled=True, sampling_rate=1.0))
    with bind_request_trace(fake_trace):
        with trace_span("llm.decompose", "llm", {"model": unsafe}):
            pass
    await fake_trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    serialized_operations = []

    def capture_after_sdk_serialization(self, operations, **kwargs) -> None:
        del self, kwargs
        serialized_operations.extend(operations)

    monkeypatch.setattr(Client, "_batch_ingest_run_ops", capture_after_sdk_serialization)
    client = Client(
        api_key="lsv2_pt_abcdefghijklmnop1234",
        auto_batch_tracing=False,
        omit_traced_runtime_info=True,
        tracing_sampling_rate=1.0,
    )
    langsmith_trace = _start_trace(
        TraceFactory(
            exporter=LangSmithTraceExporter(client, "jarvis-ai-test", 1.0),
            enabled=True,
            sampling_rate=1.0,
        )
    )
    with bind_request_trace(langsmith_trace):
        with trace_span("llm.decompose", "llm", {"model": unsafe}):
            pass
    await langsmith_trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    assert fake_exporter.exported == []
    assert serialized_operations == []
    assert (
        caplog.messages.count(
            "trace dropped requestId=req-1 root=buyer_chat_turn code=TELEMETRY_REDACTION_FAILED"
        )
        == 2
    )
    assert unsafe not in caplog.text


def test_sampling_zero_always_returns_noop() -> None:
    factory = TraceFactory(exporter=FakeTraceExporter(), enabled=True, sampling_rate=0.0)

    assert isinstance(_start_trace(factory), NoopRequestTrace)


def test_sampling_one_always_traces() -> None:
    factory = TraceFactory(exporter=FakeTraceExporter(), enabled=True, sampling_rate=1.0)

    assert not isinstance(_start_trace(factory), NoopRequestTrace)


def test_sampling_is_deterministic_for_same_request_id() -> None:
    decisions = []
    for _ in range(10):
        factory = TraceFactory(exporter=FakeTraceExporter(), enabled=True, sampling_rate=0.5)
        decisions.append(isinstance(_start_trace(factory), NoopRequestTrace))

    assert len(set(decisions)) == 1


async def test_metadata_keys_outside_allowlist_are_rejected(caplog) -> None:
    exporter = FakeTraceExporter()
    trace = _start_trace(TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0))

    with bind_request_trace(trace):
        with trace_span("buyer.routing", "chain", {"unexpectedMetadata": "safe-looking"}):
            pass
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    assert exporter.exported == []
    assert "TELEMETRY_REDACTION_FAILED" in caplog.text


async def test_safe_token_count_metadata_is_exported() -> None:
    exporter = FakeTraceExporter()
    trace = _start_trace(TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0))

    with bind_request_trace(trace):
        with trace_span("llm.decompose", "llm", {"promptTokens": 12}):
            pass
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    assert exporter.exported[0][1].metadata["promptTokens"] == 12


@pytest.mark.parametrize(
    "canary",
    [
        "sk-abcdefghijklmnop1234",
        "sk-proj-abcdefghijklmnop1234",
        "sk-ant-abcdefghijklmnop1234",
        "lsv2_pt_abcdefghijklmnop1234",
    ],
)
def test_api_key_canary_is_rejected_as_sole_allowlisted_metadata_value(canary) -> None:
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload({"metadata": {"model": canary}})


async def test_langsmith_batch_export_uses_safe_explicit_run_payloads() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.create = None

        def batch_ingest_runs(self, *, create=None, update=None) -> None:
            assert update is None
            self.create = create

    client = FakeClient()
    exporter = LangSmithTraceExporter(client, "jarvis-ai-test", 0.5)
    trace = _start_trace(TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0))
    with bind_request_trace(trace):
        with trace_span("buyer.routing", "chain"):
            pass
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    assert client.create is not None
    assert len(client.create) == 2
    roots = [payload for payload in client.create if payload["parent_run_id"] is None]
    assert len(roots) == 1
    by_name = {payload["name"]: payload for payload in client.create}
    assert by_name["buyer.routing"]["parent_run_id"] == by_name["buyer_chat_turn"]["id"]
    assert all(payload["inputs"] == {} for payload in client.create)
    assert all(payload["outputs"] == {} for payload in client.create)
    assert all(payload["session_name"] == "jarvis-ai-test" for payload in client.create)
    assert by_name["buyer_chat_turn"]["dotted_order"].endswith(
        str(by_name["buyer_chat_turn"]["id"])
    )
    assert by_name["buyer.routing"]["dotted_order"].startswith(
        f"{by_name['buyer_chat_turn']['dotted_order']}."
    )
    assert by_name["buyer.routing"]["dotted_order"].endswith(str(by_name["buyer.routing"]["id"]))
    assert "person@example.com" not in repr(client.create)


async def test_pinned_sdk_serializes_only_validated_safe_trace_fields(monkeypatch) -> None:
    from langsmith import Client
    from langsmith import env as langsmith_env

    from app.core.config import get_settings

    serialized_operations = []

    def capture_after_sdk_serialization(self, operations, **kwargs) -> None:
        del self, kwargs
        serialized_operations.extend(operations)

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_abcdefghijklmnop1234")
    monkeypatch.setenv("LANGCHAIN_UNSAFE_METADATA", "person@example.com")
    monkeypatch.setattr(Client, "_batch_ingest_run_ops", capture_after_sdk_serialization)
    langsmith_env.get_langchain_env_var_metadata.cache_clear()
    get_settings.cache_clear()
    set_trace_factory(None)
    try:
        trace = _start_trace(get_trace_factory())
        with bind_request_trace(trace):
            with trace_span("buyer.routing", "chain"):
                pass
        await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")
    finally:
        set_trace_factory(None)
        get_settings.cache_clear()
        langsmith_env.get_langchain_env_var_metadata.cache_clear()

    assert len(serialized_operations) == 2
    run_info = [operation.deserialize_run_info() for operation in serialized_operations]
    by_name = {run["name"]: run for run in run_info}
    assert by_name["buyer.routing"]["parent_run_id"] == by_name["buyer_chat_turn"]["id"]
    assert by_name["buyer.routing"]["dotted_order"].startswith(
        f"{by_name['buyer_chat_turn']['dotted_order']}."
    )
    assert all(operation.inputs == b"{}" for operation in serialized_operations)
    assert all(operation.outputs == b"{}" for operation in serialized_operations)
    assert all(set(run["extra"]) == {"metadata"} for run in run_info)
    assert all(set(run["extra"]["metadata"]) <= SAFE_METADATA_KEYS for run in run_info)
    assert "LANGCHAIN_UNSAFE_METADATA" not in repr(run_info)
    assert "person@example.com" not in repr(run_info)


def test_tracing_false_never_constructs_langsmith_client(monkeypatch) -> None:
    from app.core.config import get_settings

    constructed = False

    def fail_if_constructed(*args, **kwargs):
        del args, kwargs
        nonlocal constructed
        constructed = True
        raise AssertionError("LangSmith client must not be constructed")

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setattr("langsmith.Client", fail_if_constructed)
    get_settings.cache_clear()
    set_trace_factory(None)
    try:
        factory = get_trace_factory()
        assert isinstance(_start_trace(factory), NoopRequestTrace)
        assert constructed is False
    finally:
        set_trace_factory(None)
        get_settings.cache_clear()


def test_enabled_factory_disables_sdk_runtime_metadata(monkeypatch) -> None:
    from app.core.config import get_settings

    captured_kwargs = {}

    class CapturingClient:
        def __init__(self, **kwargs) -> None:
            captured_kwargs.update(kwargs)

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_abcdefghijklmnop1234")
    monkeypatch.setattr("langsmith.Client", CapturingClient)
    get_settings.cache_clear()
    set_trace_factory(None)
    try:
        get_trace_factory()
        assert captured_kwargs["omit_traced_runtime_info"] is True
    finally:
        set_trace_factory(None)
        get_settings.cache_clear()


def test_enabled_factory_disables_sdk_side_sampling(monkeypatch) -> None:
    from app.core.config import get_settings

    captured_kwargs = {}

    class CapturingClient:
        def __init__(self, **kwargs) -> None:
            captured_kwargs.update(kwargs)

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_abcdefghijklmnop1234")
    monkeypatch.setenv("LANGSMITH_TRACING_SAMPLING_RATE", "0.5")
    monkeypatch.setattr("langsmith.Client", CapturingClient)
    get_settings.cache_clear()
    set_trace_factory(None)
    try:
        get_trace_factory()
        assert captured_kwargs["tracing_sampling_rate"] == 1.0
    finally:
        set_trace_factory(None)
        get_settings.cache_clear()


async def test_application_sampler_is_sole_decision_before_sdk_serialization(
    monkeypatch,
) -> None:
    from langsmith import Client

    from app.core.config import get_settings

    serialized_operations = []

    def capture_after_sdk_serialization(self, operations, **kwargs) -> None:
        del self, kwargs
        serialized_operations.extend(operations)

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_abcdefghijklmnop1234")
    monkeypatch.setenv("LANGSMITH_TRACING_SAMPLING_RATE", "0.5")
    monkeypatch.setattr("langsmith.client.random.random", lambda: 0.99)
    monkeypatch.setattr(Client, "_batch_ingest_run_ops", capture_after_sdk_serialization)
    get_settings.cache_clear()
    set_trace_factory(None)
    try:
        factory = get_trace_factory()
        for _ in range(2):
            trace = factory.start_request(
                name="buyer_chat_turn",
                request_id="same-request",
                conversation_id="session-1",
                thread_id="thread-1",
                lane="buyer",
                environment="test",
            )
            assert not isinstance(trace, NoopRequestTrace)
            await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")
    finally:
        set_trace_factory(None)
        get_settings.cache_clear()

    assert len(serialized_operations) == 2
    assert all(
        operation.deserialize_run_info()["name"] == "buyer_chat_turn"
        for operation in serialized_operations
    )
