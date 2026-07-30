"""Buyer route root-trace integration for the full SSE lifecycle."""

from __future__ import annotations

import asyncio
import types
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agents.buyer.cart.graph import stream_cart_add
from app.agents.buyer.cart.state import CartStateStore
from app.agents.buyer.graph import run_buyer_turn
from app.agents.buyer.recommendation.state import CartIntent
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.tracing import (
    FakeTraceExporter,
    TraceFactory,
    bind_request_trace,
    set_trace_factory,
    trace_span,
)
from app.main import app
from app.schemas.chat import ChatRequest
from app.schemas.spring import AddToCartResult, ProductSearchResult
from app.services.spring_client import SpringUnavailableError
from tests._fakes import DEFAULT_PRODUCTS, FakeLLM

client = TestClient(app)


class CapturingTraceFactory(TraceFactory):
    def __init__(self) -> None:
        self.exporter = FakeTraceExporter()
        super().__init__(exporter=self.exporter, enabled=True, sampling_rate=1.0)


BUYER_DEGRADE_REASONS = {
    "search_failed",
    "rerank_fallback",
    "push_skipped",
    "dedup_skipped",
    "cart_merge_skipped",
    "fanout_partial",
}


def _request(message: str = "민감한-사용자-메시지") -> SimpleNamespace:
    return SimpleNamespace(session_id="session-trace", thread_id="thread-trace", message=message)


def _member() -> Identity:
    return Identity(user_id="123", is_guest=False, seller_id=None, subject="123")


async def _collect(stream) -> None:
    async for _ in stream:
        pass


async def _run_with_trace(driver) -> tuple:
    factory = CapturingTraceFactory()
    trace = factory.start_request(
        name="buyer_chat_turn",
        request_id="req-trace",
        conversation_id="conversation-trace",
        thread_id="thread-trace",
        lane="buyer",
        environment="test",
    )
    with bind_request_trace(trace):
        await driver()
    await trace.finish(status="COMPLETED", error_type=None, terminal_reason="done")
    return factory.exporter.exported[0]


def assert_expected_tree(exported, *, root: str, required: set[str]) -> None:
    roots = [node for node in exported if node.parent_id is None]
    assert [node.name for node in roots] == [root]
    assert required <= {node.name for node in exported}
    assert all(node.trace_id == roots[0].trace_id for node in exported)


async def _search_with_span(filters, exclude_product_ids=None):
    del filters, exclude_product_ids
    with trace_span("spring.search_products", "tool"):
        return ProductSearchResult(
            products=list(DEFAULT_PRODUCTS), total_count=len(DEFAULT_PRODUCTS)
        )


async def _push_with_span(push) -> bool:
    del push
    with trace_span("spring.push_recommendations", "tool"):
        return True


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


async def test_recommendation_exports_bounded_buyer_tree() -> None:
    async def driver() -> None:
        await _collect(
            run_buyer_turn(
                _request(),
                _member(),
                llm=FakeLLM(),
                search=_search_with_span,
                push_fn=_push_with_span,
            )
        )

    exported = await _run_with_trace(driver)

    assert_expected_tree(
        exported,
        root="buyer_chat_turn",
        required={
            "buyer.routing",
            "llm.decompose",
            "buyer.graph.recommendation",
            "spring.search_products",
            "llm.rerank",
            "spring.push_recommendations",
        },
    )
    by_name = {node.name: node for node in exported}
    assert by_name["llm.decompose"].parent_id == by_name["buyer.routing"].id
    for name in ("spring.search_products", "llm.rerank", "spring.push_recommendations"):
        assert by_name[name].parent_id == by_name["buyer.graph.recommendation"].id
    serialized_names = " ".join(node.name for node in exported)
    assert "민감한-사용자-메시지" not in serialized_names
    assert "무선이어폰" not in serialized_names
    assert "101" not in serialized_names
    assert "https://" not in serialized_names


@pytest.mark.parametrize(
    ("decompose", "expected"),
    [
        (
            {
                "intent": "cart_add",
                "reply": "",
                "case": 2,
                "semanticQuery": "",
                "categoryQueries": [],
                "filters": {},
                "cart": {"productId": None, "quantity": 1},
            },
            {"buyer.routing", "llm.decompose", "buyer.graph.cart"},
        ),
        (
            {
                "intent": "general",
                "reply": "안녕하세요",
                "case": 2,
                "semanticQuery": "",
                "categoryQueries": [],
                "filters": {},
            },
            {"buyer.routing", "llm.decompose", "buyer.graph.fallback", "llm.fallback"},
        ),
    ],
)
async def test_cart_and_fallback_export_bounded_graphs(decompose: dict, expected: set[str]) -> None:
    async def driver() -> None:
        await _collect(run_buyer_turn(_request(), _member(), llm=FakeLLM(decompose=decompose)))

    exported = await _run_with_trace(driver)

    assert_expected_tree(exported, root="buyer_chat_turn", required=expected)
    if "llm.fallback" in expected:
        by_name = {node.name: node for node in exported}
        assert by_name["llm.fallback"].parent_id == by_name["buyer.graph.fallback"].id


async def test_search_failure_marks_bounded_degrade() -> None:
    async def search(filters, exclude_product_ids=None):
        del filters, exclude_product_ids
        raise SpringUnavailableError("customer@example.com search exploded")

    async def driver() -> None:
        await _collect(
            run_buyer_turn(
                _request(), _member(), llm=FakeLLM(), search=search, push_fn=_push_with_span
            )
        )

    await _assert_degrade(driver, "search_failed")


async def test_rerank_failure_marks_bounded_degrade() -> None:
    async def driver() -> None:
        await _collect(
            run_buyer_turn(
                _request(),
                _member(),
                llm=FakeLLM(rerank_error=True),
                search=_search_with_span,
                push_fn=_push_with_span,
            )
        )

    await _assert_degrade(driver, "rerank_fallback")


async def test_push_failure_marks_bounded_degrade() -> None:
    async def push(push) -> bool:
        del push
        raise SpringUnavailableError("customer@example.com push exploded")

    async def driver() -> None:
        await _collect(
            run_buyer_turn(
                _request(), _member(), llm=FakeLLM(), search=_search_with_span, push_fn=push
            )
        )

    await _assert_degrade(driver, "push_skipped")


async def test_dedup_failure_marks_bounded_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    async def purchases(user_id, status=None):
        del user_id, status
        raise SpringUnavailableError("customer@example.com dedup exploded")

    monkeypatch.setattr("app.services.spring_client.get_recent_purchases", purchases)

    async def driver() -> None:
        await _collect(
            run_buyer_turn(
                _request(),
                _member(),
                llm=FakeLLM(),
                search=_search_with_span,
                push_fn=_push_with_span,
            )
        )

    await _assert_degrade(driver, "dedup_skipped")


async def test_cart_merge_failure_marks_bounded_degrade() -> None:
    async def get_cart(*, user_id=None, guest_id=None):
        del user_id, guest_id
        raise SpringUnavailableError("customer@example.com cart exploded")

    async def add_cart(request):
        del request
        return AddToCartResult(success=True, cart_item_id=1)

    async def driver() -> None:
        await _collect(
            stream_cart_add(
                identity=_member(),
                cart=CartIntent(product_id=101),
                cart_store=CartStateStore(),
                thread_key="member:thread",
                settings=get_settings(),
                add_fn=add_cart,
                get_cart_fn=get_cart,
            )
        )

    await _assert_degrade(driver, "cart_merge_skipped")


async def test_partial_fanout_marks_bounded_degrade() -> None:
    async def mapper(*, category_queries, utterance, settings):
        del category_queries, utterance, settings
        return [("카테고리-A", "A"), ("카테고리-B", "B")]

    async def search(filters, exclude_product_ids=None):
        del exclude_product_ids
        if filters.category == "카테고리-B":
            raise SpringUnavailableError("customer@example.com fanout exploded")
        return ProductSearchResult(
            products=[DEFAULT_PRODUCTS[0]],
            total_count=1,
        )

    async def driver() -> None:
        await _collect(
            run_buyer_turn(
                _request(),
                _member(),
                llm=FakeLLM(
                    rerank={
                        "ranked": [{"productId": 101, "rationale": "적합"}],
                        "overallComment": "추천",
                    }
                ),
                search=search,
                push_fn=_push_with_span,
                map_categories=mapper,
            )
        )

    await _assert_degrade(driver, "fanout_partial")


async def _assert_degrade(driver, reason: str) -> None:
    exported = await _run_with_trace(driver)
    root = next(node for node in exported if node.parent_id is None)
    assert reason in BUYER_DEGRADE_REASONS
    assert root.metadata["degraded"] is True
    assert root.metadata["degradeReason"] == reason
    assert "customer@example.com" not in repr(root.metadata)


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
