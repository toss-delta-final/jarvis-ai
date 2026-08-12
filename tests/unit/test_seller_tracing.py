"""Seller route root-trace integration for the full SSE lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import types
from time import perf_counter

import jwt
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk
from langchain_core.runnables import RunnableLambda

from app.agents.seller.context import SellerContext
from app.core.auth import Identity
from app.core.tracing import (
    FakeTraceExporter,
    LangSmithTraceExporter,
    SELLER_DEGRADE_REASONS,
    TraceFactory,
    bind_request_trace,
    set_trace_factory,
)
from app.main import app
from app.schemas.seller import SellerChatRequest

client = TestClient(app)


class CapturingTraceFactory(TraceFactory):
    def __init__(self) -> None:
        self.exporter = FakeTraceExporter()
        super().__init__(exporter=self.exporter, enabled=True, sampling_rate=1.0)


@pytest.fixture
def fake_trace_factory(monkeypatch: pytest.MonkeyPatch) -> CapturingTraceFactory:
    monkeypatch.setattr("app.core.errors.new_request_id", lambda: "req-seller-tracing")
    factory = CapturingTraceFactory()
    set_trace_factory(factory)
    yield factory
    set_trace_factory(None)


def _seller_headers(
    seller_id: str = "7",
    brand_id: str = "3",
) -> dict[str, str]:
    token = jwt.encode(
        {"sub": seller_id, "role": "SELLER", "brandId": brand_id},
        "unused-dev-secret-at-least-32-bytes",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _response_events(response) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def _start_seller_trace(factory: CapturingTraceFactory):
    return factory.start_request(
        name="seller_chat_turn",
        request_id="req-safe",
        conversation_id="conversation-safe",
        thread_id="thread-safe",
        lane="seller",
        environment="test",
    )


async def _finish(trace) -> None:
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")


class _CapturingLangSmithClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def batch_ingest_runs(self, *, create) -> None:
        self.payloads.extend(create)


def _start_seller_langsmith_trace():
    client = _CapturingLangSmithClient()
    trace = TraceFactory(
        exporter=LangSmithTraceExporter(client, "test", 1.0),
        enabled=True,
        sampling_rate=1.0,
    ).start_request(
        name="seller_chat_turn",
        request_id="req-safe",
        conversation_id="conversation-safe",
        thread_id="thread-safe",
        lane="seller",
        environment="test",
    )
    return trace, client


def _spring_payloads(payloads: list[dict[str, object]]) -> list[dict[str, object]]:
    return [payload for payload in payloads if str(payload["name"]).startswith("spring.")]


def test_seller_request_exports_one_correlated_root(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    from app.agents.seller.schemas import RouteDecision
    from app.api import seller as seller_api

    async def route(*_args, **_kwargs):
        return RouteDecision(category="general", reason="bounded", confidence=1)

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield AIMessageChunk(content="bounded")

    monkeypatch.setattr(seller_api, "route_question", route)
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: Agent())
    # [#346] 발화는 **기간 표현이 없어야** 한다. general 레인은 해석 불가 기간을 모델 호출
    # 앞에서 되묻기로 끊으므로("오늘"), 그런 발화를 쓰면 graph/llm span 이 아예 안 생겨
    # 트레이스 상관관계를 재려는 이 테스트가 엉뚱한 이유로 깨진다.
    response = client.post(
        "/seller/chat",
        json={"sessionId": "s1", "threadId": "t1", "message": "매출 알려줘"},
        headers=_seller_headers(),
    )

    assert response.status_code == 200
    events = _response_events(response)
    assert [event["type"] for event in events] == ["meta", "token", "done"]
    assert not any(
        event["type"] == "error" and event["data"]["code"] == "INTERNAL" for event in events
    )
    assert len(fake_trace_factory.exporter.exported) == 1
    roots = [node for node in fake_trace_factory.exporter.exported[0] if node.parent_id is None]
    assert len(roots) == 1
    assert roots[0].name == "seller_chat_turn"
    assert roots[0].metadata["requestId"] == response.headers["x-request-id"]
    assert roots[0].metadata["lane"] == "general"
    by_name = {node.name: node for node in fake_trace_factory.exporter.exported[0]}
    assert by_name["seller.routing"].parent_id == roots[0].id
    assert by_name["seller.graph.general"].parent_id == roots[0].id
    assert by_name["llm.seller.general"].parent_id == by_name["seller.graph.general"].id


def test_seller_request_disables_implicit_langchain_tracing_in_child_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langsmith import Client
    from langchain_core.tracers import context as tracer_context
    from langchain_core.tracers import langchain as tracer_module

    from app.agents.seller.schemas import RouteDecision
    from app.api import seller as seller_api
    from tests.unit.test_tracing import (
        PRIVACY_CANARIES,
        _assert_canaries_absent,
        _serialized_operation_content,
    )

    original_tracer = tracer_module.LangChainTracer
    default_tracers: list[object] = []
    alternate_operations: list[object] = []
    serialized_operations: list[object] = []

    class CapturingDefaultTracer(original_tracer):
        def __init__(self, *args, **kwargs) -> None:
            kwargs["client"] = types.SimpleNamespace()
            super().__init__(*args, **kwargs)
            default_tracers.append(self)

        def _persist_run_single(self, run) -> None:
            alternate_operations.append(run)

        @staticmethod
        def _update_run_single(run) -> None:
            alternate_operations.append(run)

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(tracer_context, "LangChainTracer", CapturingDefaultTracer)
    monkeypatch.setattr(tracer_module, "LangChainTracer", CapturingDefaultTracer)

    def capture_after_sdk_serialization(self, operations, **kwargs) -> None:
        del self, kwargs
        serialized_operations.extend(operations)

    monkeypatch.setattr(Client, "_batch_ingest_run_ops", capture_after_sdk_serialization)

    async def route(*_args, **_kwargs):
        return RouteDecision(category="general", reason="bounded", confidence=1)

    canary = PRIVACY_CANARIES["seller_message"]

    async def child_runnable(value):
        return AIMessageChunk(content=f"safe response for {value['messages'][0].content}")

    async def parent_runnable(value):
        return await asyncio.create_task(RunnableLambda(child_runnable).ainvoke(value))

    class Agent:
        async def astream(self, value, **_kwargs):
            yield await RunnableLambda(parent_runnable).ainvoke(value)

    monkeypatch.setattr(seller_api, "route_question", route)
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: Agent())
    manual_client = Client(
        api_key="lsv2_pt_abcdefghijklmnop1234",
        auto_batch_tracing=False,
        omit_traced_runtime_info=True,
        tracing_sampling_rate=1.0,
    )
    trace = TraceFactory(
        exporter=LangSmithTraceExporter(manual_client, "test", 1.0),
        enabled=True,
        sampling_rate=1.0,
    )
    set_trace_factory(trace)
    try:
        response = client.post(
            "/seller/chat",
            json={"sessionId": "implicit-tracing", "threadId": "child-task", "message": canary},
            headers=_seller_headers(),
        )
    finally:
        set_trace_factory(None)

    assert response.status_code == 200
    assert default_tracers == []
    assert alternate_operations == []
    assert serialized_operations
    roots = [
        operation.deserialize_run_info()
        for operation in serialized_operations
        if operation.deserialize_run_info()["parent_run_id"] is None
    ]
    assert [payload["name"] for payload in roots] == ["seller_chat_turn"]
    _assert_canaries_absent(
        [
            [_serialized_operation_content(operation) for operation in serialized_operations],
            alternate_operations,
        ]
    )


def test_seller_sse_is_identical_with_tracing_disabled(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    from app.agents.seller.schemas import RouteDecision
    from app.api import seller as seller_api

    async def route(*_args, **_kwargs):
        return RouteDecision(category="general", reason="bounded", confidence=1)

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield AIMessageChunk(content="tracing-independent seller response")

    monkeypatch.setattr(seller_api, "route_question", route)
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: Agent())
    request = {
        "sessionId": "tracing-off-seller",
        "threadId": "tracing-off-seller",
        "message": "seller response equivalence",
    }

    enabled_response = client.post("/seller/chat", json=request, headers=_seller_headers())
    disabled_exporter = FakeTraceExporter()
    set_trace_factory(TraceFactory(exporter=disabled_exporter, enabled=False, sampling_rate=1.0))
    disabled_response = client.post("/seller/chat", json=request, headers=_seller_headers())

    expected = [
        {"type": "meta", "data": {"lane": "general"}},
        {
            "type": "token",
            "data": {"text": "tracing-independent seller response"},
        },
        {
            "type": "done",
            "data": {"finishReason": "stop", "panel": "keep"},
        },
    ]
    assert enabled_response.status_code == disabled_response.status_code == 200
    assert _response_events(enabled_response) == _response_events(disabled_response) == expected
    assert len(fake_trace_factory.exporter.exported) == 1
    assert disabled_exporter.exported == []


def test_seller_endpoint_canaries_reach_provider_seams_but_not_trace_payloads(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from langsmith import Client

    caplog.set_level(logging.INFO)
    from app.agents.seller.schemas import RouteDecision
    from app.api import seller as seller_api
    from tests.unit.test_tracing import (
        PRIVACY_CANARIES,
        _assert_canaries_absent,
        _serialized_operation_content,
    )

    provider_inputs: list[object] = []
    provider_results: list[str] = []
    provider_exceptions: list[str] = []

    async def route(*_args, **_kwargs):
        return RouteDecision(category="general", reason="bounded", confidence=1)

    class Agent:
        async def astream(self, *args, **kwargs):
            provider_inputs.append((args, kwargs))
            serialized = repr((args, kwargs))
            if PRIVACY_CANARIES["provider_exception"] in serialized:
                provider_exceptions.append(PRIVACY_CANARIES["provider_exception"])
                raise RuntimeError(PRIVACY_CANARIES["provider_exception"])
            result = " ".join(
                PRIVACY_CANARIES[key]
                for key in (
                    "tool_argument",
                    "tool_result",
                    "customer_name",
                    "customer_email",
                    "customer_phone",
                    "customer_address",
                    "nested_metadata",
                )
            )
            provider_results.append(result)
            yield AIMessageChunk(content=result)

    monkeypatch.setattr(seller_api, "route_question", route)
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: Agent())
    monkeypatch.setattr("app.core.errors.new_request_id", lambda: "req-seller-canary-141")
    raw_seller = "700000000007"
    raw_brand = "300000000003"
    raw_threads = (
        "seller-thread-private-normal",
        "seller-thread-private-error",
        "seller-thread-private-sdk-normal",
        "seller-thread-private-sdk-error",
    )
    headers = {
        **_seller_headers(raw_seller, raw_brand),
        "X-Authorization-Canary": PRIVACY_CANARIES["authorization"],
        "X-Cookie-Canary": PRIVACY_CANARIES["cookie"],
    }
    message = " ".join(
        (
            PRIVACY_CANARIES["seller_message"],
            PRIVACY_CANARIES["customer_name"],
            PRIVACY_CANARIES["customer_email"],
            PRIVACY_CANARIES["customer_phone"],
        )
    )

    fake_exporter = FakeTraceExporter()
    set_trace_factory(TraceFactory(exporter=fake_exporter, enabled=True, sampling_rate=1.0))
    response = client.post(
        "/seller/chat",
        json={
            "sessionId": "seller-canary-141",
            "threadId": raw_threads[0],
            "message": message,
        },
        headers=headers,
    )
    exception_response = client.post(
        "/seller/chat",
        json={
            "sessionId": "seller-exception-141",
            "threadId": raw_threads[1],
            "message": PRIVACY_CANARIES["provider_exception"],
        },
        headers=headers,
    )

    assert response.status_code == exception_response.status_code == 200
    assert any(message in repr(value) for value in provider_inputs)
    assert PRIVACY_CANARIES["tool_result"] in provider_results[0]
    assert provider_exceptions == [PRIVACY_CANARIES["provider_exception"]]
    assert response.request.headers["X-Authorization-Canary"] == PRIVACY_CANARIES["authorization"]
    assert response.request.headers["X-Cookie-Canary"] == PRIVACY_CANARIES["cookie"]
    _assert_canaries_absent(fake_exporter.exported)

    serialized_operations = []

    def capture_after_sdk_serialization(self, operations, **kwargs) -> None:
        del self, kwargs
        serialized_operations.extend(operations)

    monkeypatch.setattr(Client, "_batch_ingest_run_ops", capture_after_sdk_serialization)
    langsmith_client = Client(
        api_key="lsv2_pt_abcdefghijklmnop1234",
        auto_batch_tracing=False,
        omit_traced_runtime_info=True,
        tracing_sampling_rate=1.0,
    )
    set_trace_factory(
        TraceFactory(
            exporter=LangSmithTraceExporter(langsmith_client, "jarvis-ai-test", 1.0),
            enabled=True,
            sampling_rate=1.0,
        )
    )
    second = client.post(
        "/seller/chat",
        json={
            "sessionId": "seller-sdk-canary-141",
            "threadId": raw_threads[2],
            "message": message,
        },
        headers=headers,
    )
    sdk_exception = client.post(
        "/seller/chat",
        json={
            "sessionId": "seller-sdk-exception-141",
            "threadId": raw_threads[3],
            "message": PRIVACY_CANARIES["provider_exception"],
        },
        headers=headers,
    )
    sdk_exception_events = _response_events(sdk_exception)

    assert second.status_code == sdk_exception.status_code == 200
    assert [event["type"] for event in sdk_exception_events] == ["meta", "error"]
    assert sdk_exception_events[0] == {"type": "meta", "data": {"lane": "general"}}
    assert sdk_exception_events[1]["data"] | {"requestId": None} == {
        "code": "INTERNAL",
        "message": "일시적인 오류가 발생했습니다.",
        "requestId": None,
        "retryable": True,
    }
    assert sdk_exception_events[1]["data"]["requestId"]
    assert PRIVACY_CANARIES["provider_exception"] not in sdk_exception.text
    assert serialized_operations
    _assert_canaries_absent(
        [_serialized_operation_content(operation) for operation in serialized_operations]
    )
    log_text = caplog.text
    for raw in (
        raw_seller,
        raw_brand,
        *raw_threads,
        PRIVACY_CANARIES["seller_message"],
        PRIVACY_CANARIES["provider_exception"],
    ):
        assert raw not in log_text
    from app.core.logging import safe_fingerprint

    assert safe_fingerprint(raw_seller) in log_text
    assert safe_fingerprint(raw_brand) in log_text
    assert safe_fingerprint(raw_threads[0]) in log_text


async def test_invalid_seller_identity_log_contains_only_fingerprints(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.api.seller import _seller_stream
    from app.core.logging import safe_fingerprint

    raw_seller = "invalid-seller-private"
    raw_brand = "invalid-brand-private"
    raw_thread = "invalid-thread-private"
    request = SellerChatRequest(
        session_id="invalid-session-private",
        thread_id=raw_thread,
        message="invalid-message-private",
    )
    identity = Identity(
        user_id=raw_seller,
        is_guest=False,
        seller_id=raw_seller,
        brand_id=raw_brand,
        subject=raw_seller,
    )

    with caplog.at_level(logging.WARNING, logger="app.api.seller"):
        events = [event async for event in _seller_stream(request, identity)]

    assert len(events) == 1 and '"code": "INTERNAL"' in events[0]
    for raw in (raw_seller, raw_brand, raw_thread, request.message):
        assert raw not in caplog.text
    assert safe_fingerprint(raw_seller) in caplog.text
    assert safe_fingerprint(raw_brand) in caplog.text


def test_analysis_request_through_open_stream_finishes_done_with_intact_tree(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    from app.agents.seller.orchestrator import PipelineResult
    from app.agents.seller.schemas import RouteDecision
    from app.api import seller as seller_api
    from app.core.tracing import trace_span

    async def route(*_args, **_kwargs):
        return RouteDecision(category="analysis", reason="bounded", confidence=1)

    async def pipeline(*_args, emit, **_kwargs):
        with trace_span("llm.seller.planner", "llm"):
            await emit("분석 중…")
        return PipelineResult(kind="report", text="bounded report")

    monkeypatch.setattr(seller_api, "route_question", route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", pipeline)
    response = client.post(
        "/seller/chat",
        json={
            "sessionId": "analysis-open-stream",
            "threadId": "analysis-open-stream",
            # [#591] 5단 파이프라인에 닿는 채팅 경로는 게이트 ②.5(차트 어휘)뿐이다.
            "message": "지난달 매출 그래프",
        },
        headers=_seller_headers(),
    )

    assert response.status_code == 200
    events = _response_events(response)
    # report 는 kind=="report" 최종 산출에 1회 동반된다(이슈 #296, api-spec §3.2 v0.24.0).
    assert [event["type"] for event in events] == ["meta", "progress", "token", "report", "done"]
    assert not any(
        event["type"] == "error" and event["data"]["code"] == "INTERNAL" for event in events
    )
    nodes = fake_trace_factory.exporter.exported[0]
    by_name = {node.name: node for node in nodes}
    root = by_name["seller_chat_turn"]
    analysis = by_name["seller.graph.analysis"]
    assert analysis.parent_id == root.id
    assert by_name["llm.seller.planner"].parent_id == analysis.id
    assert root.metadata["terminalReason"] == "done"


class _DisconnectRequest:
    state = types.SimpleNamespace()

    async def is_disconnected(self) -> bool:
        return True


async def test_general_disconnect_cancels_and_awaits_provider_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import seller as seller_api
    from app.agents.seller.schemas import RouteDecision
    from app.core.config import get_settings
    from app.core.stream import open_stream

    started = asyncio.Event()
    cancelled = asyncio.Event()
    finished = asyncio.Event()

    class Agent:
        async def astream(self, *_args, **_kwargs):
            try:
                started.set()
                await asyncio.Event().wait()
                yield AIMessageChunk(content="unreachable")
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                finished.set()

    async def route(*_args, **_kwargs):
        return RouteDecision(category="general", reason="bounded", confidence=1)

    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: Agent())
    monkeypatch.setattr(seller_api, "route_question", route)
    monkeypatch.setattr(get_settings(), "stream_disconnect_poll_s", 0.005)
    request = SellerChatRequest(
        session_id="general-disconnect",
        thread_id="general-disconnect",
        message="question",
    )
    response = await open_stream(
        _DisconnectRequest(),
        "seller:general-disconnect",
        lambda _turn_started_at: seller_api._seller_stream(
            request,
            Identity(
                user_id="7",
                is_guest=False,
                seller_id="7",
                brand_id="3",
                subject="7",
            ),
        ),
    )

    frames = [frame async for frame in response.body_iterator]

    assert len(frames) == 1
    assert json.loads(frames[0].removeprefix("data: "))["type"] == "meta"
    assert started.is_set()
    assert cancelled.is_set()
    assert finished.is_set()


async def test_analysis_disconnect_cancels_and_awaits_pipeline_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import seller as seller_api
    from app.agents.seller.schemas import RouteDecision
    from app.core.config import get_settings
    from app.core.stream import open_stream

    started = asyncio.Event()
    cancelled = asyncio.Event()
    finished = asyncio.Event()

    async def pipeline(*_args, **_kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            finished.set()

    async def route(*_args, **_kwargs):
        return RouteDecision(category="analysis", reason="bounded", confidence=1)

    monkeypatch.setattr(seller_api, "run_analysis_pipeline", pipeline)
    monkeypatch.setattr(seller_api, "route_question", route)
    monkeypatch.setattr(get_settings(), "stream_disconnect_poll_s", 0.005)
    request = SellerChatRequest(
        session_id="analysis-disconnect",
        thread_id="analysis-disconnect",
        message="매출 그래프 보여줘",  # [#591] 게이트 ②.5 로 분석 파이프라인 진입
    )
    response = await open_stream(
        _DisconnectRequest(),
        "seller:analysis-disconnect",
        lambda _turn_started_at: seller_api._seller_stream(
            request,
            Identity(
                user_id="7",
                is_guest=False,
                seller_id="7",
                brand_id="3",
                subject="7",
            ),
        ),
    )

    frames = [frame async for frame in response.body_iterator]

    assert len(frames) == 1
    assert json.loads(frames[0].removeprefix("data: "))["type"] == "meta"
    assert started.is_set()
    assert cancelled.is_set()
    assert finished.is_set()


async def test_parallel_workers_are_sibling_spans_with_llm_children(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    from app.agents.seller import orchestrator
    from app.agents.seller.pipeline import ResolvedPlan
    from app.agents.seller.schemas import AnalysisFinding
    from app.agents.seller.tools import calculate

    class Agent:
        def __init__(self, kind: str) -> None:
            self.kind = kind

        async def ainvoke(self, _input, context=None):
            await asyncio.sleep(0)
            assert await calculate.coroutine(expression="1+1") == "계산 결과: 2"
            return {
                "structured_response": AnalysisFinding(
                    analysis_type=self.kind,
                    summary="bounded",
                    evidence=[],
                    severity="info",
                )
            }

    from app.agents.seller.schemas import AnalysisScore

    class JudgeAgent:
        async def ainvoke(self, _input, context=None):
            return {
                "structured_response": AnalysisScore(
                    grounding=8, sufficiency=8, relevance=8, feedback=""
                )
            }

    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS, "sales_anomaly", lambda: Agent("sales_anomaly")
    )
    monkeypatch.setitem(orchestrator.WORKER_BUILDERS, "churn", lambda: Agent("churn"))
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: JudgeAgent())
    monkeypatch.setattr(
        orchestrator,
        "get_settings",
        lambda: types.SimpleNamespace(
            seller_worker_timeout_s=1,
            seller_worker_max_retries=1,
            seller_analysis_score_threshold=21,
            seller_analysis_judge_timeout_s=1,
            seller_branch_deadline_s=120.0,
        ),
    )
    plan = ResolvedPlan(
        analyses=("sales_anomaly", "churn"),
        date_from=__import__("datetime").date(2026, 7, 1),
        date_to=__import__("datetime").date(2026, 7, 2),
    )

    async def emit(_text: str) -> None:
        return None

    trace = _start_seller_trace(fake_trace_factory)
    with bind_request_trace(trace):
        from app.core.tracing import trace_span

        with trace_span("seller.graph.analysis", "chain"):
            await orchestrator.run_branches(
                "private question", plan, SellerContext(seller_id=7, brand_id=3), emit=emit
            )
    await _finish(trace)

    nodes = fake_trace_factory.exporter.exported[0]
    by_name = {node.name: node for node in nodes}
    analysis = by_name["seller.graph.analysis"]
    workers = [node for node in nodes if node.name.startswith("seller.worker.")]
    assert {node.name for node in workers} == {
        "seller.worker.sales_anomaly",
        "seller.worker.churn",
    }
    assert {node.parent_id for node in workers} == {analysis.id}
    llms = [node for node in nodes if node.name == "llm.seller.worker"]
    assert len(llms) == 2
    assert {node.parent_id for node in llms} == {node.id for node in workers}
    tools = [node for node in nodes if node.name == "tool.calculate"]
    assert len(tools) == 2
    assert {node.parent_id for node in tools} == {node.id for node in llms}
    assert all(node.parent_id is not None for node in nodes[1:])


async def test_analysis_request_exports_complete_bounded_tree(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    from app.agents.seller import orchestrator
    from app.agents.seller.schemas import (
        AnalysisFinding,
        AnalysisPlan,
        AnalysisScore,
        RecommendationSet,
        ReportScore,
        RouteDecision,
    )
    from app.api import seller as seller_api

    class Agent:
        def __init__(self, response: dict) -> None:
            self.response = response

        async def ainvoke(self, _input, context=None):
            return self.response

    async def route(*_args, **_kwargs):
        return RouteDecision(category="analysis", reason="bounded", confidence=1)

    async def load_history(_seller_id):
        return []

    async def save_history(*_args, **_kwargs):
        return None

    async def load_turns(*_args, **_kwargs):
        return []

    async def record_turn(*_args, **_kwargs):
        return None

    finding = AnalysisFinding(
        analysis_type="sales_anomaly",
        summary="bounded",
        evidence=[],
        severity="info",
    )
    monkeypatch.setattr(seller_api, "route_question", route)
    monkeypatch.setattr(seller_api.seller_thread, "load_recent_turns", load_turns)
    monkeypatch.setattr(seller_api.seller_thread, "record_turn", record_turn)
    monkeypatch.setattr(orchestrator.history, "load_recent", load_history)
    monkeypatch.setattr(orchestrator.history, "save_history", save_history)
    monkeypatch.setattr(
        orchestrator,
        "seller_trace_model_metadata",
        lambda *_args, **_kwargs: {"model": "bounded-model"},
    )
    monkeypatch.setattr(
        orchestrator,
        "build_analysis_planner",
        lambda: Agent(
            {
                "structured_response": AnalysisPlan(
                    analyses=["sales_anomaly"], period_expr="지난달", reason="bounded"
                )
            }
        ),
    )
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: Agent({"structured_response": finding}),
    )
    monkeypatch.setattr(
        orchestrator,
        "build_report_agent",
        lambda: Agent({"messages": [types.SimpleNamespace(content="bounded")]}),
    )
    monkeypatch.setattr(
        orchestrator,
        "build_report_judge",
        lambda: Agent(
            {
                "structured_response": ReportScore(
                    accuracy=10, completeness=10, clarity=10, feedback=""
                )
            }
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "build_recommend_agent",
        lambda: Agent({"structured_response": RecommendationSet(recommendations=[], summary="")}),
    )
    monkeypatch.setattr(
        orchestrator,
        "build_analysis_judge",
        lambda: Agent(
            {
                "structured_response": AnalysisScore(
                    grounding=8, sufficiency=8, relevance=8, feedback=""
                )
            }
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "get_settings",
        lambda: types.SimpleNamespace(
            seller_worker_timeout_s=1,
            seller_report_score_threshold=21,
            seller_report_max_retries=1,
            seller_recent_days_default=7,
            seller_period_max_days=731,  # #269 — resolve_plan 인자로 읽힌다
            seller_worker_max_retries=1,
            seller_analysis_score_threshold=21,
            seller_analysis_judge_timeout_s=1,
            seller_branch_deadline_s=120.0,
        ),
    )
    trace = _start_seller_trace(fake_trace_factory)
    request = SellerChatRequest(session_id="s", thread_id="t", message="지난달 매출 그래프")
    identity = Identity(
        user_id="7",
        is_guest=False,
        seller_id="7",
        brand_id="3",
        subject="7",
    )
    with bind_request_trace(trace):
        assert [line async for line in seller_api._seller_stream(request, identity)]
    await _finish(trace)

    nodes = fake_trace_factory.exporter.exported[0]
    by_name = {node.name: node for node in nodes}
    root = by_name["seller_chat_turn"]
    analysis = by_name["seller.graph.analysis"]
    assert root.metadata["lane"] == "analysis"
    # [#591] 차트 게이트(②.5)는 라우팅 LLM 을 부르지 않는다 — seller.routing 노드가 없는 것이
    # 정상이고, 있으면 선판정이 뚫려 supervisor 를 한 번 더 태운 것이다.
    assert "seller.routing" not in by_name
    assert analysis.parent_id == root.id
    for name in (
        "llm.seller.planner",
        "seller.worker.sales_anomaly",
        "llm.seller.report",
        "llm.seller.judge",
        "llm.seller.recommend",
    ):
        assert name in by_name
        if name.startswith("llm."):
            assert by_name[name].metadata == {"model": "bounded-model"}
    assert by_name["llm.seller.planner"].parent_id == analysis.id
    assert by_name["seller.worker.sales_anomaly"].parent_id == analysis.id
    assert "provider_ttft_ms" not in root.metadata
    assert all(node.parent_id is not None for node in nodes[1:])


async def test_general_provider_ttft_uses_first_non_empty_chunk(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    from app.api import seller as seller_api

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield AIMessageChunk(content="")
            await asyncio.sleep(0.01)
            yield AIMessageChunk(content="visible")

    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: Agent())
    monkeypatch.setattr(
        seller_api,
        "seller_trace_model_metadata",
        lambda *_args, **_kwargs: {"model": "bounded-model"},
    )
    trace = _start_seller_trace(fake_trace_factory)
    request = SellerChatRequest(session_id="s", thread_id="t", message="question")
    started = perf_counter()
    with bind_request_trace(trace):
        assert [
            line
            async for line in seller_api._general_stream(
                request, SellerContext(seller_id=7, brand_id=3)
            )
        ]
    await _finish(trace)

    nodes = fake_trace_factory.exporter.exported[0]
    root = nodes[0]
    llm = next(node for node in nodes if node.name == "llm.seller.general")
    assert llm.parent_id == next(node.id for node in nodes if node.name == "seller.graph.general")
    assert llm.metadata == {"model": "bounded-model"}
    assert root.metadata["provider_ttft_ms"] >= 5
    assert root.metadata["provider_ttft_ms"] <= int((perf_counter() - started) * 1000)


def test_seller_telemetry_model_metadata_uses_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bypassing the configured resolver must omit the expected model and fail this test."""
    from app.agents.seller import models
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        openai_api_key="test-key",
        openai_smart_model_id="configured-seller-model",
    )
    monkeypatch.setattr(models, "get_settings", lambda: settings)

    assert models.seller_trace_model_metadata("planner") == {"model": "configured-seller-model"}


async def test_seller_telemetry_model_resolution_failure_isolated_with_safe_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Removing the explicit telemetry boundary must fail the request or leak diagnostics."""
    from app.agents.seller import models
    from app.api import seller as seller_api

    class Agent:
        async def astream(self, *_args, **_kwargs):
            yield AIMessageChunk(content="still available")

    def fail_resolution(*_args, **_kwargs):
        raise RuntimeError("private resolver failure customer@example.com")

    monkeypatch.setattr(models, "resolve_provider_model", fail_resolution)
    monkeypatch.setattr(seller_api, "build_general_agent", lambda **_kwargs: Agent())
    trace = _start_seller_trace(fake_trace_factory)
    request = SellerChatRequest(session_id="s", thread_id="t", message="question")

    with bind_request_trace(trace):
        frames = [
            line
            async for line in seller_api._general_stream(
                request, SellerContext(seller_id=7, brand_id=3)
            )
        ]
    await _finish(trace)

    assert any("still available" in frame for frame in frames)
    llm = next(
        node
        for node in fake_trace_factory.exporter.exported[0]
        if node.name == "llm.seller.general"
    )
    assert llm.metadata == {}
    assert "SELLER_TELEMETRY_MODEL_RESOLUTION_FAILED" in caplog.text
    assert "private resolver failure" not in caplog.text
    assert "customer@example.com" not in caplog.text


async def test_existing_seller_degrade_outcomes_use_four_bounded_reasons(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    import datetime as dt

    from app.agents.seller import orchestrator
    from app.agents.seller.pipeline import ResolvedPlan
    from app.agents.seller.schemas import AnalysisFinding
    from app.api import seller as seller_api
    from app.services.spring_client import SpringUnavailableError

    class Agent:
        def __init__(self, response=None, exc: Exception | None = None) -> None:
            self.response = response
            self.exc = exc

        async def ainvoke(self, _input, context=None):
            if self.exc:
                raise self.exc
            return self.response

    from app.agents.seller.schemas import AnalysisScore

    class JudgeAgent:
        async def ainvoke(self, _input, context=None):
            return {
                "structured_response": AnalysisScore(
                    grounding=8, sufficiency=8, relevance=8, feedback=""
                )
            }

    settings = types.SimpleNamespace(
        seller_worker_timeout_s=1,
        seller_report_score_threshold=21,
        seller_report_max_retries=1,
        seller_worker_max_retries=1,
        seller_analysis_score_threshold=21,
        seller_analysis_judge_timeout_s=1,
        seller_branch_deadline_s=120.0,
    )
    monkeypatch.setattr(orchestrator, "get_settings", lambda: settings)
    monkeypatch.setattr(orchestrator, "build_analysis_judge", lambda: JudgeAgent())
    plan = ResolvedPlan(
        analyses=("sales_anomaly", "churn"),
        date_from=dt.date(2026, 7, 1),
        date_to=dt.date(2026, 7, 2),
    )
    finding = AnalysisFinding(
        analysis_type="sales_anomaly",
        summary="bounded",
        evidence=["x=1"],
        severity="info",
    )

    async def emit(_text: str) -> None:
        return None

    async def capture(coro) -> str:
        trace = _start_seller_trace(fake_trace_factory)
        with bind_request_trace(trace):
            try:
                await coro
            except orchestrator.AllWorkersFailedError:
                pass
        await _finish(trace)
        return fake_trace_factory.exporter.exported[-1][0].metadata["degradeReason"]

    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: Agent({"structured_response": finding}),
    )
    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "churn",
        lambda: Agent(exc=RuntimeError("private worker error")),
    )
    worker_reason = await capture(
        orchestrator.run_branches("private", plan, SellerContext(7, 3), emit=emit)
    )

    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: Agent(exc=RuntimeError("private worker error")),
    )
    all_reason = await capture(
        orchestrator.run_branches("private", plan, SellerContext(7, 3), emit=emit)
    )

    monkeypatch.setattr(
        orchestrator,
        "build_report_agent",
        lambda: Agent({"messages": [types.SimpleNamespace(content="x=1")]}),
    )
    monkeypatch.setattr(
        orchestrator,
        "build_report_judge",
        lambda: Agent(exc=RuntimeError("private judge error")),
    )
    partial_reason = await capture(
        orchestrator.write_verified_report([finding], SellerContext(7, 3), emit=emit)
    )

    async def fail_confirm(*_args, **_kwargs):
        raise SpringUnavailableError("private spring error")

    monkeypatch.setattr(seller_api, "confirm_draft", fail_confirm)
    request = SellerChatRequest(
        session_id="s", thread_id="t", message="", action="confirm", draft_id="d"
    )
    trace = _start_seller_trace(fake_trace_factory)
    with bind_request_trace(trace):
        assert [
            line
            async for line in seller_api._confirm_stream(
                request, SellerContext(seller_id=7, brand_id=3)
            )
        ]
    await _finish(trace)
    spring_reason = fake_trace_factory.exporter.exported[-1][0].metadata["degradeReason"]

    assert {
        worker_reason,
        partial_reason,
        all_reason,
        spring_reason,
    } == {
        "worker_degrade",
        "partial_report",
        "all_workers_failed",
        "spring_write_failed",
    }
    assert SELLER_DEGRADE_REASONS == {
        "worker_degrade",
        "partial_report",
        "all_workers_failed",
        "spring_write_failed",
    }


@pytest.mark.parametrize("lane", ("analysis", "product", "general", "confirm", "apply", "refused"))
async def test_seller_root_lane_uses_only_six_bounded_sse_values(
    lane: str,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    from app.api import seller as seller_api

    trace = _start_seller_trace(fake_trace_factory)
    with bind_request_trace(trace):
        seller_api._set_trace_lane(lane)  # type: ignore[arg-type]
    await _finish(trace)

    assert fake_trace_factory.exporter.exported[-1][0].metadata["lane"] == lane


async def test_seller_apply_lane_chain_span_is_not_attributed_to_product_lane(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    """apply 레인의 chain span 이 product 레인 이름으로 집계되면 안 된다.

    lane 메타데이터만 검증하면 span 이름 복붙 오류를 놓친다 — 레인별 지연을 span 이름으로
    가르는 이상, 이름이 겹치는 순간 두 레인이 한 통계로 합쳐진다(PR #206 리뷰).
    """
    from app.api import seller as seller_api

    async def _no_history(n: int, context: SellerContext):
        del n, context
        return None, "적용할 추천 이력이 없습니다."

    async def _record_turn(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(seller_api, "apply_recommendation", _no_history)
    monkeypatch.setattr(seller_api.seller_thread, "record_turn", _record_turn)

    trace = _start_seller_trace(fake_trace_factory)
    request = SellerChatRequest(session_id="s-apply", thread_id="t-apply", message="1번 적용해줘")
    with bind_request_trace(trace):
        assert [
            line
            async for line in seller_api._apply_stream(
                1, request, SellerContext(seller_id=7, brand_id=3)
            )
        ]
    await _finish(trace)

    names = {node.name for node in fake_trace_factory.exporter.exported[0]}
    assert "seller.graph.apply" in names
    assert "seller.graph.product" not in names


def test_seller_store_acquisition_failure_finishes_root(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    import app.api.seller as seller_api

    async def fail_store():
        raise RuntimeError("pg-profile down")

    monkeypatch.setattr(seller_api, "get_conversation_store", fail_store)

    with pytest.raises(RuntimeError, match="pg-profile down"):
        client.post(
            "/seller/chat",
            json={"sessionId": "seller-store", "threadId": "t-store", "message": "질문"},
            headers=_seller_headers(),
        )

    assert len(fake_trace_factory.exporter.exported) == 1
    (root,) = fake_trace_factory.exporter.exported[0]
    assert root.name == "seller_chat_turn"
    assert root.error_type == "INTERNAL"
    assert root.metadata["terminalReason"] == "store_unavailable"


async def test_seller_store_acquisition_cancellation_finishes_root(
    monkeypatch: pytest.MonkeyPatch,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    import app.api.seller as seller_api

    async def cancel_store():
        raise asyncio.CancelledError

    monkeypatch.setattr(seller_api, "get_conversation_store", cancel_store)
    request = SellerChatRequest(
        session_id="seller-cancel",
        thread_id="t-cancel",
        message="질문",
    )
    http_request = types.SimpleNamespace(state=types.SimpleNamespace(request_id="req-cancel"))
    identity = Identity(
        user_id="7",
        is_guest=False,
        seller_id="7",
        brand_id="3",
        subject="7",
    )

    with pytest.raises(asyncio.CancelledError):
        await seller_api.seller_chat(request, http_request, identity)

    assert len(fake_trace_factory.exporter.exported) == 1
    (root,) = fake_trace_factory.exporter.exported[0]
    assert root.error_type is None
    assert root.metadata["terminalReason"] == "client_disconnect"


@pytest.mark.parametrize("failure_point", ("factory", "start_request"))
def test_seller_telemetry_start_failure_preserves_route(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
            "/seller/chat",
            json={
                "sessionId": f"seller-{failure_point}",
                "threadId": f"thread-{failure_point}",
                "message": "오늘 날씨 알려줘",
            },
            headers=_seller_headers(),
        )
    finally:
        tracing.set_trace_factory(None)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert caplog.messages.count("trace start failed code=TELEMETRY_START_FAILED") == 1
    assert "telemetry factory failed" not in caplog.text
    assert "telemetry start failed" not in caplog.text


@pytest.mark.parametrize(
    ("status_code", "status_class"), [(200, "2xx"), (404, "4xx"), (503, "5xx")]
)
async def test_seller_spring_status_exports_only_bounded_transport_metadata(
    status_code: int,
    status_class: str,
) -> None:
    import httpx

    from app.services.spring_client import SpringClient, SpringUnavailableError

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/seller/private-brand-903/sales"
        assert request.url.params["from"] == "private-from-903"
        assert request.headers["X-Internal-Token"] == "private-token-903"
        return httpx.Response(
            status_code,
            json=(
                {"series": [], "privateResponse": "private-body-903"}
                if status_code == 200
                else {"error": {"message": "private-error-903"}}
            ),
        )

    client = SpringClient(
        "https://spring.private.test",
        "private-token-903",
        transport=httpx.MockTransport(handler),
    )
    trace, langsmith_client = _start_seller_langsmith_trace()
    with bind_request_trace(trace):
        if status_code == 200:
            result = await client.get_sales(
                "private-brand-903", "private-from-903", "private-to-903"
            )
            assert result.series == []
        else:
            with pytest.raises(SpringUnavailableError):
                await client.get_sales("private-brand-903", "private-from-903", "private-to-903")
    await _finish(trace)

    spring_payloads = _spring_payloads(langsmith_client.payloads)
    assert len(spring_payloads) == 1
    assert spring_payloads[0]["extra"]["metadata"] == {
        "httpMethod": "GET",
        "upstream": "spring",
        "statusClass": status_class,
    }
    serialized = repr(langsmith_client.payloads)
    for secret in (
        "spring.private.test",
        "/internal/seller/private-brand-903/sales",
        "private-brand-903",
        "private-from-903",
        "private-to-903",
        "private-token-903",
        "private-body-903",
        "private-error-903",
    ):
        assert secret not in serialized


async def test_seller_spring_connect_failure_exports_fixed_code_without_exception_details() -> None:
    import httpx

    from app.services.spring_client import SpringClient, SpringUnavailableError

    raw_error = httpx.ConnectError(
        "private-connect-error-907",
        request=httpx.Request("GET", "https://spring.private.test/private-path-907"),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        raise raw_error

    client = SpringClient(
        "https://spring.private.test",
        "private-token-907",
        transport=httpx.MockTransport(handler),
    )
    trace, langsmith_client = _start_seller_langsmith_trace()
    with bind_request_trace(trace):
        with pytest.raises(SpringUnavailableError) as raised:
            await client.get_sales("private-brand-907", "private-from-907", "private-to-907")
        assert raised.value.__cause__ is raw_error
        traceback_names = []
        traceback = raw_error.__traceback__
        while traceback is not None:
            traceback_names.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        assert "handler" in traceback_names
    await _finish(trace)

    spring_payloads = _spring_payloads(langsmith_client.payloads)
    assert len(spring_payloads) == 1
    assert spring_payloads[0]["extra"]["metadata"] == {
        "httpMethod": "GET",
        "upstream": "spring",
        "statusClass": "connection_error",
    }
    serialized = repr(langsmith_client.payloads)
    for secret in (
        "ConnectError",
        "private-connect-error-907",
        "private-brand-907",
        "private-token-907",
    ):
        assert secret not in serialized


async def test_all_seller_spring_operations_trace_timeout_without_changing_mapping() -> None:
    import httpx

    from app.schemas.spring import OrderItemStatusUpdate, ProductCreate, ProductUpdate
    from app.services.spring_client import SpringClient, SpringUnavailableError

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    client = SpringClient(
        "https://spring.private.test",
        "private-token-904",
        transport=httpx.MockTransport(handler),
    )
    cases = (
        (
            "spring.get_sales",
            "GET",
            lambda: client.get_sales(71727374757677, "private-from", "private-to"),
        ),
        (
            "spring.get_funnel",
            "GET",
            lambda: client.get_funnel(71727374757677, "private-from", "private-to"),
        ),
        (
            "spring.get_events",
            "GET",
            lambda: client.get_events(
                71727374757677,
                "private-from",
                "private-to",
                product_id=81828384858687,
            ),
        ),
        (
            "spring.get_order_events",
            "GET",
            lambda: client.get_order_events(71727374757677, "private-from", "private-to"),
        ),
        (
            "spring.get_product_changes",
            "GET",
            lambda: client.get_product_changes(
                71727374757677,
                "private-from",
                "private-to",
                product_id=81828384858687,
            ),
        ),
        # [#197] I-16/I-8 은 from/to 필수(AnalysisPeriod.of 400) — 시그니처 정합.
        (
            "spring.get_churn",
            "GET",
            lambda: client.get_churn(71727374757677, "2026-06-01", "2026-07-31", 30),
        ),
        (
            "spring.get_account_events",
            "GET",
            lambda: client.get_account_events(
                71727374757677, "2026-06-01", "2026-07-31", event_type="private-event"
            ),
        ),
        (
            "spring.list_products",
            "GET",
            lambda: client.list_products(71727374757677, q="private-query"),
        ),
        (
            "spring.create_product",
            "POST",
            lambda: client.create_product(
                71727374757677,
                ProductCreate(
                    name="private-product",
                    price=1000,
                    description="private-description",
                ),
            ),
        ),
        (
            "spring.update_product",
            "PATCH",
            lambda: client.update_product(
                71727374757677,
                81828384858687,
                ProductUpdate(name="private-update"),
            ),
        ),
        (
            "spring.delete_product",
            "DELETE",
            lambda: client.delete_product(71727374757677, 81828384858687),
        ),
        # [#297] I-29~I-31 주문·리뷰 4종 — 신설 op 도 payload-free 규약을 지킨다.
        (
            "spring.get_orders",
            "GET",
            lambda: client.get_orders(71727374757677, status="ORDERED"),
        ),
        (
            "spring.update_order_status",
            "PATCH",
            lambda: client.update_order_item_status(
                71727374757677,
                81828384858687,
                OrderItemStatusUpdate(to_status="SHIPPING"),
            ),
        ),
        (
            "spring.get_reviews",
            "GET",
            lambda: client.get_reviews(71727374757677, rating="1,2"),
        ),
        (
            "spring.get_review_stats",
            "GET",
            lambda: client.get_review_stats(71727374757677, from_="private-from"),
        ),
    )

    trace, langsmith_client = _start_seller_langsmith_trace()
    with bind_request_trace(trace):
        for _name, _method, invoke in cases:
            with pytest.raises(SpringUnavailableError):
                await invoke()
    await _finish(trace)

    spring_payloads = _spring_payloads(langsmith_client.payloads)
    assert len(spring_payloads) == len(cases)
    for name, method, _invoke in cases:
        matches = [payload for payload in spring_payloads if payload["name"] == name]
        assert len(matches) == 1
        assert matches[0]["extra"]["metadata"] == {
            "httpMethod": method,
            "upstream": "spring",
            "statusClass": "timeout",
        }
    serialized = repr(langsmith_client.payloads)
    for secret in (
        "ReadTimeout",
        "private timeout detail",
        "spring.private.test",
        "/internal/",
        "71727374757677",
        "81828384858687",
        "private-from",
        "private-to",
        "private-event",
        "private-query",
        "private-product",
        "private-description",
        "private-update",
        "private-token-904",
    ):
        assert secret not in serialized


# ── [#326] 콘텐츠 추적 모드 — seller 레인 콜백 커버리지 ──────────────────────────


async def test_content_callback_records_seller_llm_span_content() -> None:
    """seller 는 app/core/llm.py 를 거치지 않으므로 모델 콜백이 원문 초크포인트다."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.outputs import Generation, LLMResult

    from app.agents.seller.models import _CONTENT_TRACE_CALLBACK
    from app.core.tracing import (
        FakeTraceExporter,
        TraceFactory,
        bind_request_trace,
        trace_span,
        validate_export_payload,
    )

    exporter = FakeTraceExporter()
    factory = TraceFactory(
        exporter=exporter,
        enabled=True,
        sampling_rate=1.0,
        payload_validator=lambda p: validate_export_payload(p, allow_content=True),
        capture_content=True,
        content_max_chars=20000,
    )
    trace = factory.start_request(
        name="seller_chat_turn",
        request_id="req-seller-326",
        conversation_id="session-1",
        thread_id="thread-1",
        lane="seller",
        environment="test",
    )
    with bind_request_trace(trace):
        with trace_span("llm.seller.worker", "llm") as node:
            await _CONTENT_TRACE_CALLBACK.on_chat_model_start(
                {}, [[SystemMessage(content="지시"), HumanMessage(content="7월 매출 분석해줘")]]
            )
            await _CONTENT_TRACE_CALLBACK.on_llm_end(
                LLMResult(generations=[[Generation(text="분석 결과")]])
            )
            assert node is not None
            # tool 결과가 섞이는 히스토리라 lenient `user` 가 아니라 strict `transcript` 다.
            assert "7월 매출 분석해줘" in node.inputs["transcript"]
            assert "user" not in node.inputs
            assert node.outputs["content"] == "분석 결과"
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")


async def test_content_callback_is_noop_when_mode_off() -> None:
    from langchain_core.messages import HumanMessage
    from langchain_core.outputs import Generation, LLMResult

    from app.agents.seller.models import _CONTENT_TRACE_CALLBACK
    from app.core.tracing import FakeTraceExporter, TraceFactory, bind_request_trace, trace_span

    exporter = FakeTraceExporter()
    factory = TraceFactory(exporter=exporter, enabled=True, sampling_rate=1.0)
    trace = factory.start_request(
        name="seller_chat_turn",
        request_id="req-seller-326-off",
        conversation_id="session-1",
        thread_id="thread-1",
        lane="seller",
        environment="test",
    )
    with bind_request_trace(trace):
        with trace_span("llm.seller.worker", "llm") as node:
            await _CONTENT_TRACE_CALLBACK.on_chat_model_start(
                {}, [[HumanMessage(content="발화 원문")]]
            )
            await _CONTENT_TRACE_CALLBACK.on_llm_end(
                LLMResult(generations=[[Generation(text="응답")]])
            )
            assert node is not None
            assert node.inputs == {} and node.outputs == {}
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")
