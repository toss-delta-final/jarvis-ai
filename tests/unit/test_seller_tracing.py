"""Seller route root-trace integration for the full SSE lifecycle."""

from __future__ import annotations

import asyncio
import types
from time import perf_counter

import jwt
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk

from app.agents.seller.context import SellerContext
from app.core.auth import Identity
from app.core.tracing import (
    FakeTraceExporter,
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


def _seller_headers() -> dict[str, str]:
    token = jwt.encode(
        {"sub": "7", "role": "SELLER", "brandId": "3"},
        "unused-dev-secret-at-least-32-bytes",
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


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
    response = client.post(
        "/seller/chat",
        json={"sessionId": "s1", "threadId": "t1", "message": "오늘 날씨 알려줘"},
        headers=_seller_headers(),
    )

    assert response.status_code == 200
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

    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS, "sales_anomaly", lambda: Agent("sales_anomaly")
    )
    monkeypatch.setitem(orchestrator.WORKER_BUILDERS, "churn", lambda: Agent("churn"))
    monkeypatch.setattr(
        orchestrator,
        "get_settings",
        lambda: types.SimpleNamespace(seller_worker_timeout_s=1),
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
            await orchestrator.run_workers(
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
        "resolve_provider_model",
        lambda *_args, **_kwargs: types.SimpleNamespace(model_id="bounded-model"),
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
        "get_settings",
        lambda: types.SimpleNamespace(
            seller_worker_timeout_s=1,
            seller_report_score_threshold=21,
            seller_report_max_retries=1,
            seller_recent_days_default=7,
        ),
    )
    trace = _start_seller_trace(fake_trace_factory)
    request = SellerChatRequest(session_id="s", thread_id="t", message="지난달 매출 분석")
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
    routing = by_name["seller.routing"]
    analysis = by_name["seller.graph.analysis"]
    assert root.metadata["lane"] == "analysis"
    assert routing.parent_id == root.id
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
        "resolve_provider_model",
        lambda *_args, **_kwargs: types.SimpleNamespace(model_id="bounded-model"),
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

    settings = types.SimpleNamespace(
        seller_worker_timeout_s=1,
        seller_report_score_threshold=21,
        seller_report_max_retries=1,
    )
    monkeypatch.setattr(orchestrator, "get_settings", lambda: settings)
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
        orchestrator.run_workers("private", plan, SellerContext(7, 3), emit=emit)
    )

    monkeypatch.setitem(
        orchestrator.WORKER_BUILDERS,
        "sales_anomaly",
        lambda: Agent(exc=RuntimeError("private worker error")),
    )
    all_reason = await capture(
        orchestrator.run_workers("private", plan, SellerContext(7, 3), emit=emit)
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
