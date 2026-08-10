import asyncio
import random
import uuid
from dataclasses import fields, is_dataclass
from collections.abc import Mapping

import pytest

from app.core.config import get_settings
from app.core.logging import safe_fingerprint
from app.core.errors import new_request_id
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
from app.core.tracing import _build_export_payloads, _is_opaque_identifier


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


@pytest.mark.parametrize("enabled", [True, False])
def test_unregistered_lane_warns_without_exposing_raw_value(
    caplog: pytest.LogCaptureFixture,
    enabled: bool,
) -> None:
    trace = _start_trace(
        TraceFactory(exporter=FakeTraceExporter(), enabled=enabled, sampling_rate=1.0)
    )
    unknown_lane = "PRIVATE-LANE-CANARY"

    with caplog.at_level("WARNING", logger="app.core.tracing"):
        trace.set_lane(unknown_lane)

    assert "OBSERVABILITY_LANE_UNKNOWN" in caplog.text
    assert unknown_lane not in caplog.text


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


async def test_cancelled_trace_finish_shields_one_export_and_reuses_it() -> None:
    """export 대기 caller가 취소돼도 동일 cleanup task가 끝나며 재호출은 중복 export하지 않는다."""

    class BlockingExporter:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.proceed = asyncio.Event()
            self.calls = 0
            self.exported = []

        async def export(self, nodes) -> None:
            self.calls += 1
            self.started.set()
            await self.proceed.wait()
            self.exported.append(nodes)

    exporter = BlockingExporter()
    trace = _start_trace(TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0))
    first = asyncio.create_task(
        trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")
    )
    await exporter.started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert trace._finished is False
    exporter.proceed.set()
    await asyncio.gather(
        trace.finish(status="FAILED", error_type="INTERNAL", terminal_reason="retry"),
        trace.finish(status="CANCELLED", error_type=None, terminal_reason="retry"),
    )

    assert trace._finished is True
    assert exporter.calls == 1
    assert len(exporter.exported) == 1
    root = exporter.exported[0][0]
    assert root.error_type is None
    assert root.metadata["terminalReason"] == "done"


# --- PII 카나리아 오탐 (#208 검증 중 발견) ---------------------------------------
#
# `dotted_order` 는 `<timestamp><uuid4>` 로 조립되는 **기계 생성** 문자열이고, 페이로드에서
# 카나리아 검사를 받는 유일한 랜덤 문자열이다(`id`/`trace_id` 는 UUID 객체라 str 검사 대상이
# 아니다). 한국 휴대폰 정규식이 UUID 의 hex 숫자열과 겹쳐 오탐하면 `validate_export_payload`
# 가 UnsafeTelemetryError 를 던지고 **트레이스가 통째로 드롭**된다 — 그때마다 exported 를
# 검증하던 아무 테스트나 하나가 깨졌다(수정 전 스팬당 약 1.7e-4, 손 안 댄 dev 에서도 재현).
# 같은 오탐은 16-hex 지문(`sessionFp`·`threadFp`)에도 성립한다.

# 실제로 오탐을 일으킨 UUID 들 — 회귀 고정용.
_UUID_FALSE_POSITIVES = [
    "6a31007e-22e1-4659-8a6a-0181980191ee",
    "f73ba1f0-b9a0-4139-8a33-f01625126608",
    "8d010084-1377-45fb-830c-603c3d1722c1",
    "da75b05d-f7ef-424a-b124-d0106877030c",
    "93e67d23-1924-40e5-a016-54952023d4cd",
]


@pytest.mark.parametrize("node_id", _UUID_FALSE_POSITIVES)
def test_machine_generated_uuid_does_not_trip_pii_canary(node_id: str) -> None:
    """랜덤 UUID hex 는 PII 가 아니다 — 오탐하면 트레이스가 통째로 드롭된다."""
    validate_export_payload({"dotted_order": f"20260731T152345123456Z{node_id}"})


def test_opaque_identifiers_never_trip_the_pii_canary() -> None:
    """고정 시드 corpus — UUID·16-hex 지문 어느 쪽도 오탐이 0 이어야 한다.

    표본 5개만 고정하면 정규식을 그 5개에만 맞춰 깎는 수정도 통과한다. 클래스 전체가
    닫혔는지 보려면 corpus 가 필요하고, 시드를 고정해야 CI 가 흔들리지 않는다.
    """
    rnd = random.Random(208)
    for _ in range(20_000):
        node_id = uuid.UUID(int=rnd.getrandbits(128), version=4)
        validate_export_payload({"dotted_order": f"20260731T152345123456Z{node_id}"})
        validate_export_payload({"metadata": {"sessionFp": f"{rnd.getrandbits(64):016x}"}})


@pytest.mark.parametrize(
    "value",
    [
        "01012345678",
        "010-1234-5678",
        "연락처 010 1234 5678 입니다",
        "900101-1234567",
        "주민 900101-1234567 확인",
        # PR #218 리뷰 — 오탐을 "hex 문자 인접"으로 막으면 hex 로 끝나는 흔한 영단어 뒤에
        # 구분자 없이 붙은 진짜 PII 가 통째로 탐지를 피한다. 그 회피 경로를 여기서 막는다.
        "face01012345678",
        "userid01012345678",
        "cafe9001011234567",
    ],
)
def test_real_pii_is_still_rejected(value: str) -> None:
    """오탐을 줄이려다 진짜 PII 를 놓치면 안 된다 — 카나리아의 존재 이유다."""
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload({"metadata": {"model": value}})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        # 값이 그 생성기의 산출물 모양이 아니면 면제가 붙으면 안 된다.
        ("requestId", "010-1234-5678"),
        ("sessionFp", "01012345678"),
        ("threadFp", "900101-1234567"),
        ("sessionFp", "연락처 010 1234 5678"),
    ],
)
def test_opaque_key_with_non_identifier_value_is_still_scanned(key: str, value: str) -> None:
    """면제는 **키 이름이 아니라 값의 모양**으로 결정된다 (PR #218 리뷰).

    키 이름만 보면 트리 어디서든 같은 이름을 쓰는 dict 가 생기는 순간 면제가 따라붙는다.
    """
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload({"metadata": {key: value}})


@pytest.mark.parametrize("key", ["sessionFp", "threadFp", "requestId"])
def test_opaque_metadata_keys_are_not_exempt_outside_metadata(key: str) -> None:
    """메타데이터 문맥 밖(예: 예외 `vars()`)의 동명 키는 면제 대상이 아니다."""
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload({"extra": {key: "01012345678"}})


def test_dotted_order_outside_its_shape_is_still_scanned() -> None:
    """`dotted_order` 도 정렬 키 모양일 때만 면제된다."""
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload({"dotted_order": "01012345678"})


@pytest.mark.parametrize("key", ["requestId", "sessionFp", "threadFp"])
@pytest.mark.parametrize(
    "canary",
    ["Bearer abcdefghijklmnop", "person@example.com", "sk-abcdefghijklmnop1234"],
)
def test_opaque_identifier_fields_still_reject_non_numeric_canaries(key: str, canary: str) -> None:
    """불투명 식별자 필드의 면제는 **숫자열 카나리아에만** 적용된다.

    면제가 넓어져 그 필드가 검사에서 통째로 빠지면, 값 생성 경로가 바뀌었을 때 토큰·이메일이
    조용히 나간다. 면제 범위를 좁게 고정한다.
    """
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload({"metadata": {key: canary}})


async def test_real_export_payload_is_recognized_as_opaque() -> None:
    """면제 모양 정규식을 **실제 생성기 산출물에 묶는다**.

    모양 기반 면제의 유일한 회귀 위험은 `_build_export_payloads`·`new_request_id`·
    `safe_fingerprint` 가 형식을 바꿨는데 정규식이 따라가지 못하는 것이다. 그러면 면제가 조용히
    풀려 오탐 flake 가 되돌아오는데, 확률적이라 다른 테스트로는 안 잡힌다.
    """
    exporter = FakeTraceExporter()
    trace = TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0).start_request(
        name="buyer_chat_turn",
        request_id=new_request_id(),
        conversation_id="session-opaque",
        thread_id="thread-opaque",
        lane="buyer",
        environment="test",
    )
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    payloads = _build_export_payloads(exporter.exported[0], project_name=None)
    assert payloads
    for payload in payloads:
        dotted = payload["dotted_order"]
        assert _is_opaque_identifier("dotted_order", dotted, metadata=False), dotted
        metadata = payload["extra"]["metadata"]
        for key in ("requestId", "sessionFp", "threadFp"):
            assert _is_opaque_identifier(key, metadata[key], metadata=True), (key, metadata[key])


# ── [#326] 콘텐츠 추적 모드 ─────────────────────────────────────────────────────


async def test_content_mode_off_keeps_inputs_outputs_empty_even_if_record_called() -> None:
    """기본(off)에서는 기록 API 가 no-op — 기존 #141 비유출 동작을 문자 그대로 고정한다."""
    exporter = FakeTraceExporter()
    factory = TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0)
    trace = _start_trace(factory)

    assert trace.captures_content is False
    trace.record_request_content(input_text=PRIVACY_CANARIES["buyer_message"])
    with bind_request_trace(trace):
        with trace_span("llm.decompose", "llm") as node:
            trace.record_llm_content(
                system="sys", user=PRIVACY_CANARIES["buyer_message"], output="out"
            )
            assert node is not None
            trace.record_span_content(node, inputs={"url": "http://spring/internal"})
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    payloads = _build_export_payloads(exporter.exported[0], project_name=None)
    assert all(p["inputs"] == {} and p["outputs"] == {} for p in payloads)
    _assert_canaries_absent(payloads)


async def test_content_mode_records_root_llm_and_span_content_with_clipping() -> None:
    exporter = FakeTraceExporter()
    factory = TraceFactory(
        exporter=exporter,
        enabled=True,
        sampling_rate=1.0,
        payload_validator=lambda p: validate_export_payload(p, allow_content=True),
        capture_content=True,
        content_max_chars=10,
    )
    trace = _start_trace(factory)

    assert trace.captures_content is True
    trace.record_request_content(input_text="파란 바지 추천해줘 예산은 오만원")
    with bind_request_trace(trace):
        with trace_span("spring.search_products", "tool") as spring_node:
            assert spring_node is not None
            trace.record_span_content(
                spring_node,
                inputs={"url": "http://spring/internal/products/search?keyword=바지"},
                outputs={"responseBody": '{"rows": []}'},
            )
        with trace_span("llm.rerank", "llm"):
            trace.record_llm_content(system="시스템 프롬프트", user="유저 프롬프트", output="응답")
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    nodes = exporter.exported[0]
    by_name = {node.name: node for node in nodes}
    # 루트 발화 — max_chars=10 절단 표식 확인.
    root_message = by_name["buyer_chat_turn"].inputs["message"]
    assert root_message.startswith("파란 바지 추천해줘")
    assert "truncated" in root_message
    # LLM span 원문.
    assert by_name["llm.rerank"].inputs == {"system": "시스템 프롬프트", "user": "유저 프롬프트"}
    assert by_name["llm.rerank"].outputs == {"content": "응답"}
    # Spring span 페이로드 (절단 적용).
    assert "url" in by_name["spring.search_products"].inputs
    assert "responseBody" in by_name["spring.search_products"].outputs
    # export payload 에 실리고, allow_content 검증을 통과한다.
    payloads = _build_export_payloads(nodes, project_name=None)
    validate_export_payload(payloads, allow_content=True)
    assert any(p["inputs"] for p in payloads)


async def test_content_mode_redacts_hard_pii_from_request_and_llm_content() -> None:
    """[이슈 #321] 콘텐츠 트레이스는 원문을 싣기 전 하드 PII 를 치환한다(기본 on)."""
    exporter = FakeTraceExporter()
    factory = TraceFactory(
        exporter=exporter,
        enabled=True,
        sampling_rate=1.0,
        payload_validator=lambda p: validate_export_payload(p, allow_content=True),
        capture_content=True,
        content_max_chars=0,
    )
    trace = _start_trace(factory)

    trace.record_request_content(
        input_text="제 번호는 010-0000-0000 이고 예산은 오만원",
        output_text="tester@example.com 로 안내드렸습니다",
    )
    with bind_request_trace(trace):
        with trace_span("llm.rerank", "llm"):
            trace.record_llm_content(user="연락처는 010-0000-0000 입니다", output="응답")
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    nodes = exporter.exported[0]
    by_name = {node.name: node for node in nodes}
    root_input = by_name["buyer_chat_turn"].inputs["message"]
    root_output = by_name["buyer_chat_turn"].outputs["message"]
    llm_input = by_name["llm.rerank"].inputs["user"]
    assert "0000" not in root_input
    assert "[전화번호]" in root_input
    assert "example.com" not in root_output
    assert "[이메일]" in root_output
    assert "0000" not in llm_input
    assert "[전화번호]" in llm_input


async def test_content_mode_keeps_raw_text_when_redaction_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pii_redact_trace_content=False` 면 종전 동작(무치환)이다."""
    monkeypatch.setattr(get_settings(), "pii_redact_trace_content", False)
    exporter = FakeTraceExporter()
    factory = TraceFactory(
        exporter=exporter,
        enabled=True,
        sampling_rate=1.0,
        payload_validator=lambda p: validate_export_payload(p, allow_content=True),
        capture_content=True,
        content_max_chars=0,
    )
    trace = _start_trace(factory)

    trace.record_request_content(input_text="제 번호는 010-0000-0000 입니다")
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    nodes = exporter.exported[0]
    by_name = {node.name: node for node in nodes}
    assert "010-0000-0000" in by_name["buyer_chat_turn"].inputs["message"]


async def test_content_mode_redaction_preserves_non_string_extra_inputs() -> None:
    """구조화 `extra_inputs`(예: conditionActions)는 문자열이 아니면 치환 대상에서 제외한다."""
    exporter = FakeTraceExporter()
    factory = TraceFactory(
        exporter=exporter,
        enabled=True,
        sampling_rate=1.0,
        payload_validator=lambda p: validate_export_payload(p, allow_content=True),
        capture_content=True,
        content_max_chars=0,
    )
    trace = _start_trace(factory)

    trace.record_request_content(
        input_text="발화", extra_inputs={"conditionActions": [{"field": "priceMax"}]}
    )
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")

    nodes = exporter.exported[0]
    by_name = {node.name: node for node in nodes}
    assert "priceMax" in by_name["buyer_chat_turn"].inputs["conditionActions"]


def test_content_payload_is_rejected_without_allow_content() -> None:
    """allow_content 없이 콘텐츠가 실린 payload 는 기존 fail-closed 검증이 그대로 막는다."""
    payload = [{"inputs": {"message": "원문"}, "outputs": {}, "extra": {"metadata": {}}}]
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload(payload)


def test_allow_content_keeps_metadata_allowlist_enforced() -> None:
    payload = [
        {
            "inputs": {"message": PRIVACY_CANARIES["buyer_message"]},
            "outputs": {},
            "extra": {"metadata": {"rawCustomerField": "leak"}},
        }
    ]
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload(payload, allow_content=True)


def test_allow_content_still_rejects_canary_outside_content_fields() -> None:
    payload = [
        {
            "inputs": {},
            "outputs": {},
            "name": PRIVACY_CANARIES["customer_email"],
            "extra": {"metadata": {}},
        }
    ]
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload(payload, allow_content=True)


@pytest.mark.parametrize(
    "leaked",
    [
        "여기 제 토큰이요 Bearer abc.def.ghi",
        "키는 sk-proj-abcdefghijklmn 입니다",
        "lsv2_pt_0123456789abcdef 로 접속하세요",
        "제 메일은 customer-326@example.com 이에요",
    ],
)
def test_allow_content_still_applies_text_canaries_inside_content(leaked: str) -> None:
    """콘텐츠 모드여도 credential·이메일 카나리아는 inputs/outputs 안에서 계속 잡는다."""
    payload = [{"inputs": {"message": leaked}, "outputs": {}, "extra": {"metadata": {}}}]
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload(payload, allow_content=True)


def test_allow_content_permits_normal_utterance_with_numbers() -> None:
    """정상 발화(가격 등 숫자 포함)는 콘텐츠 서브트리에서 숫자열 카나리아에 걸리지 않는다."""
    payload = [
        {
            "inputs": {"message": "5만원 이하 파란 바지 추천해줘, productId 1006987247"},
            "outputs": {"content": '{"rows": [{"price": 49900}]}'},
            "extra": {"metadata": {}},
        }
    ]
    validate_export_payload(payload, allow_content=True)


def test_spring_payload_content_keeps_numeric_canaries() -> None:
    """[#326] Spring 원본(`responseBody` 등)은 콘텐츠여도 휴대폰·주민번호 카나리아를 유지한다."""
    payload = [
        {
            "inputs": {},
            "outputs": {"responseBody": '{"receiverPhone": "010-1234-5678"}'},
            "extra": {"metadata": {}},
        }
    ]
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload(payload, allow_content=True)


def test_llm_input_keys_stay_lenient_for_numeric_shapes() -> None:
    """입력 방향 발화·prompt 키는 숫자열 카나리아 면제 — 전화번호를 직접 말한 발화는 통과한다."""
    payload = [
        {
            "inputs": {"message": "010-1234-5678 로 배송 문자 줘"},
            "outputs": {},
            "extra": {"metadata": {}},
        }
    ]
    validate_export_payload(payload, allow_content=True)


def test_llm_output_content_keeps_numeric_canaries() -> None:
    """출력은 모델 생성물 — 백엔드 데이터를 옮겨 적을 수 있어 숫자열 카나리아를 유지한다."""
    payload = [
        {
            "inputs": {},
            "outputs": {"content": "고객 연락처는 010-1234-5678 입니다"},
            "extra": {"metadata": {}},
        }
    ]
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload(payload, allow_content=True)


def test_unknown_content_key_defaults_to_strict() -> None:
    """새 콘텐츠 키는 등록 없이 lenient 를 얻지 못한다 — 기본 strict(fail-closed)."""
    payload = [
        {
            "inputs": {"somethingNew": "010-1234-5678"},
            "outputs": {},
            "extra": {"metadata": {}},
        }
    ]
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload(payload, allow_content=True)


def test_agent_transcript_keeps_numeric_canaries() -> None:
    """[#326] agent 히스토리(transcript)는 tool 결과가 섞여 strict — 전화번호 형태가 잡힌다."""
    payload = [
        {
            "inputs": {"transcript": 'tool: {"receiverPhone": "010-1234-5678"}'},
            "outputs": {},
            "extra": {"metadata": {}},
        }
    ]
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload(payload, allow_content=True)


def test_extra_inputs_are_strict_content() -> None:
    """[#469] conditionActions·signals 등 extra_inputs 키는 lenient 를 얻지 못한다."""
    payload = [
        {
            "inputs": {"conditionActions": '[{"action":"remove","value":"010-1234-5678"}]'},
            "outputs": {},
            "extra": {"metadata": {}},
        }
    ]
    with pytest.raises(UnsafeTelemetryError):
        validate_export_payload(payload, allow_content=True)


async def test_record_request_content_extra_inputs_lands_on_root() -> None:
    exporter = FakeTraceExporter()
    factory = TraceFactory(
        exporter=exporter,
        enabled=True,
        sampling_rate=1.0,
        payload_validator=lambda p: validate_export_payload(p, allow_content=True),
        capture_content=True,
        content_max_chars=20000,
    )
    trace = _start_trace(factory)
    trace.record_request_content(
        input_text="", extra_inputs={"conditionActions": '[{"action":"remove","field":"price"}]'}
    )
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")
    root = exporter.exported[0][0]
    assert "remove" in root.inputs["conditionActions"]
