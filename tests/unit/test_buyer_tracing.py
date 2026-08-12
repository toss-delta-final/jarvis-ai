"""Buyer route root-trace integration for the full SSE lifecycle."""

from __future__ import annotations

import asyncio
import json
import types
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.buyer.cart.state import get_cart_store
from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.recommendation.category_mapping import CategoryMapping
from app.agents.buyer.session_state import context_thread_key
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.session_context import BuyerSessionInput
from app.core.tracing import (
    FakeTraceExporter,
    LangSmithTraceExporter,
    TraceFactory,
    bind_request_trace,
    set_trace_factory,
    trace_span,
)
from app.main import app
from app.schemas.chat import ChatRequest
from app.schemas.spring import (
    AddToCartRequest,
    AddToCartResult,
    ProductSearchFilters,
    ProductSearchResult,
    RecommendationListEntry,
    RecommendationPush,
)
from app.services.spring_client import (
    CartError,
    OrderStatusUnavailableError,
    SpringUnavailableError,
    get_order_status as _real_get_order_status,
    get_recent_purchases as _real_get_recent_purchases,
)
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


async def run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    """Tracing unit calls still cross the same committed lifecycle boundary as /chat."""
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "guest" if identity.is_guest else "member",
            buyer_owner_id(identity, get_settings()),
        )
    )
    observer = kwargs.pop("observer", None) or SimpleNamespace(
        request_id="buyer-trace-unit",
        record_model_call=lambda *_: None,
    )
    observer.context_id = context.context_id
    async for frame in _production_run_buyer_turn(
        request,
        identity,
        observer=observer,
        **kwargs,
    ):
        yield frame


async def _collect(stream) -> list[str]:
    return [frame async for frame in stream]


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


class _CapturingLangSmithClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def batch_ingest_runs(self, *, create) -> None:
        self.payloads.extend(create)


async def _run_with_langsmith_payload(driver) -> list[dict[str, object]]:
    client = _CapturingLangSmithClient()
    trace = TraceFactory(
        exporter=LangSmithTraceExporter(client, "test", 1.0),
        enabled=True,
        sampling_rate=1.0,
    ).start_request(
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
    return client.payloads


def _spring_payloads(payloads: list[dict[str, object]]) -> list[dict[str, object]]:
    return [payload for payload in payloads if str(payload["name"]).startswith("spring.")]


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


def test_buyer_sse_is_identical_with_tracing_disabled(
    buyer_fakes: FakeLLM,
    fake_trace_factory: CapturingTraceFactory,
) -> None:
    buyer_fakes._decompose = {
        "intent": "general",
        "reply": "tracing-independent buyer response",
        "filters": {},
    }
    request = {
        "sessionId": "tracing-off-buyer",
        "threadId": "tracing-off-buyer",
        "message": "buyer response equivalence",
    }

    enabled_response = client.post("/chat", json=request)
    disabled_exporter = FakeTraceExporter()
    set_trace_factory(TraceFactory(exporter=disabled_exporter, enabled=False, sampling_rate=1.0))
    disabled_response = client.post("/chat", json=request)

    enabled_events = [
        json.loads(line.removeprefix("data: "))
        for line in enabled_response.text.splitlines()
        if line.startswith("data: ")
    ]
    disabled_events = [
        json.loads(line.removeprefix("data: "))
        for line in disabled_response.text.splitlines()
        if line.startswith("data: ")
    ]
    expected = [
        # progress_events_enabled 기본 on(#396) — 스트림 맨 앞에 progress 프레임이 추가된다.
        {
            "type": "progress",
            "data": {"stage": "analyzing", "message": get_settings().progress_analyzing_message},
        },
        {
            "type": "token",
            "data": {"text": "tracing-independent buyer response"},
        },
        {
            "type": "done",
            "data": {"finishReason": "stop"},
        },
    ]
    assert enabled_response.status_code == disabled_response.status_code == 200
    assert enabled_events == disabled_events == expected
    assert len(fake_trace_factory.exporter.exported) == 1
    assert disabled_exporter.exported == []


def test_buyer_endpoint_canaries_reach_production_seams_but_not_trace_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langsmith import Client

    from app.agents.buyer.recommendation.rerank_grounding import NEUTRAL_RATIONALE
    from app.core.llm import LLMError
    from app.schemas.spring import SpringProduct
    from app.services import search_service
    from tests.unit.test_tracing import (
        PRIVACY_CANARIES,
        _assert_canaries_absent,
        _serialized_operation_content,
    )

    tool_calls: list[object] = []
    pushes: list[object] = []
    provider_inputs: list[str] = []

    class CanaryBackend:
        async def search(self, filters):
            tool_calls.append(filters)
            return ProductSearchResult(
                products=[
                    SpringProduct(
                        product_id=product_id,
                        name=f"{PRIVACY_CANARIES['tool_result']}-{product_id}",
                        summary=PRIVACY_CANARIES["customer_address"],
                        attributes={
                            "customer": {
                                "name": PRIVACY_CANARIES["customer_name"],
                                "email": PRIVACY_CANARIES["customer_email"],
                                "phone": PRIVACY_CANARIES["customer_phone"],
                            }
                        },
                        price=1000,
                    )
                    for product_id in (14101, 14102, 14103)
                ],
                total_count=3,
            )

    async def capture_push(push) -> bool:
        pushes.append(push)
        return True

    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "reply": "",
            "case": 2,
            "semanticQuery": PRIVACY_CANARIES["tool_argument"],
            "categoryQueries": [],
            "filters": {"keyword": PRIVACY_CANARIES["tool_argument"]},
        },
        rerank={
            "ranked": [
                {
                    "productId": product_id,
                    "rationale": PRIVACY_CANARIES["nested_metadata"],
                }
                for product_id in (14101, 14102, 14103)
            ],
            "overallComment": "bounded",
        },
    )
    original_complete = llm.complete

    async def complete(**kwargs):
        provider_inputs.append(kwargs["user"])
        if PRIVACY_CANARIES["provider_exception"] in kwargs["user"]:
            raise LLMError(PRIVACY_CANARIES["provider_exception"])
        return await original_complete(**kwargs)

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr("app.agents.buyer.graph.get_llm", lambda: llm)
    monkeypatch.setattr(search_service, "default_backend", CanaryBackend())
    monkeypatch.setattr("app.services.spring_client.push_recommendations", capture_push)
    monkeypatch.setattr("app.core.errors.new_request_id", lambda: "req-buyer-canary-141")

    fake_exporter = FakeTraceExporter()
    set_trace_factory(TraceFactory(exporter=fake_exporter, enabled=True, sampling_rate=1.0))
    headers = {
        "X-Authorization-Canary": PRIVACY_CANARIES["authorization"],
        "X-Cookie-Canary": PRIVACY_CANARIES["cookie"],
    }
    message = " ".join(
        (
            PRIVACY_CANARIES["buyer_message"],
            PRIVACY_CANARIES["customer_name"],
            PRIVACY_CANARIES["customer_email"],
            PRIVACY_CANARIES["customer_phone"],
        )
    )
    response = client.post(
        "/chat",
        json={"sessionId": "buyer-canary-141", "threadId": "buyer-canary-141", "message": message},
        headers=headers,
    )
    exception_response = client.post(
        "/chat",
        json={
            "sessionId": "buyer-exception-141",
            "threadId": "buyer-exception-141",
            "message": PRIVACY_CANARIES["provider_exception"],
        },
        headers=headers,
    )

    assert response.status_code == exception_response.status_code == 200
    assert any(message in user for user in provider_inputs)
    assert tool_calls[0].keyword == PRIVACY_CANARIES["tool_argument"]
    # The validated production arm must not forward an ungrounded model rationale.
    assert PRIVACY_CANARIES["nested_metadata"] not in repr(pushes[0])
    assert NEUTRAL_RATIONALE in repr(pushes[0])
    assert response.request.headers["X-Authorization-Canary"] == PRIVACY_CANARIES["authorization"]
    assert response.request.headers["X-Cookie-Canary"] == PRIVACY_CANARIES["cookie"]
    assert any(PRIVACY_CANARIES["provider_exception"] in user for user in provider_inputs)
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
        "/chat",
        json={
            "sessionId": "buyer-sdk-canary-141",
            "threadId": "buyer-sdk-canary-141",
            "message": message,
        },
        headers=headers,
    )
    sdk_exception = client.post(
        "/chat",
        json={
            "sessionId": "buyer-sdk-exception-141",
            "threadId": "buyer-sdk-exception-141",
            "message": PRIVACY_CANARIES["provider_exception"],
        },
        headers=headers,
    )
    sdk_exception_events = [
        json.loads(line.removeprefix("data: "))
        for line in sdk_exception.text.splitlines()
        if line.startswith("data: ")
    ]

    assert second.status_code == sdk_exception.status_code == 200
    # progress_events_enabled 기본 on(#396) — provider 예외는 progress emit(decompose 직전)
    # 이후에 발생하므로 progress 가 error 앞에 먼저 나간다.
    assert [event["type"] for event in sdk_exception_events] == ["progress", "error"]
    assert sdk_exception_events[1]["data"] | {"requestId": None} == {
        "code": "LLM_UNAVAILABLE",
        "message": "질의를 이해하지 못했어요.",
        "requestId": None,
        "retryable": True,
    }
    assert sdk_exception_events[1]["data"]["requestId"]
    assert PRIVACY_CANARIES["provider_exception"] not in sdk_exception.text
    assert serialized_operations
    _assert_canaries_absent(
        [_serialized_operation_content(operation) for operation in serialized_operations]
    )


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


async def test_buyer_spring_success_exports_only_bounded_transport_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import spring_client

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/products/search"
        assert request.url.params["keyword"] == "private-product-901"
        assert request.headers["X-Internal-Token"] == "private-token-901"
        return httpx.Response(
            200,
            json={"success": True, "data": [], "privateResponse": "private-body-901"},
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        spring_client,
        "_client",
        lambda *, timeout=None: httpx.AsyncClient(
            base_url="https://spring.private.test",
            headers={"X-Internal-Token": "private-token-901"},
            transport=transport,
        ),
    )

    async def driver() -> None:
        result = await spring_client.search_products(
            ProductSearchFilters(keyword="private-product-901")
        )
        assert result.products == []

    payloads = await _run_with_langsmith_payload(driver)
    spring_payloads = _spring_payloads(payloads)

    assert len(spring_payloads) == 1
    assert spring_payloads[0]["extra"]["metadata"] == {
        "httpMethod": "GET",
        "upstream": "spring",
        "statusClass": "2xx",
    }
    serialized = repr(payloads)
    for secret in (
        "spring.private.test",
        "/internal/products/search",
        "private-product-901",
        "private-token-901",
        "private-body-901",
    ):
        assert secret not in serialized


@pytest.mark.parametrize(("status_code", "status_class"), [(404, "4xx"), (503, "5xx")])
async def test_buyer_spring_http_failure_preserves_mapping_and_records_only_status_class(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    status_class: str,
) -> None:
    from app.services import spring_client

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": {"message": "private-error-905", "productId": 91929394959697}},
        )

    monkeypatch.setattr(
        spring_client,
        "_client",
        lambda *, timeout=None: httpx.AsyncClient(
            base_url="https://spring.private.test",
            headers={"X-Internal-Token": "private-token-905"},
            transport=httpx.MockTransport(handler),
        ),
    )

    async def driver() -> None:
        with pytest.raises(SpringUnavailableError):
            await spring_client.search_products(ProductSearchFilters(keyword="private-query-905"))

    payloads = await _run_with_langsmith_payload(driver)
    spring_payloads = _spring_payloads(payloads)

    assert len(spring_payloads) == 1
    assert spring_payloads[0]["extra"]["metadata"] == {
        "httpMethod": "GET",
        "upstream": "spring",
        "statusClass": status_class,
    }
    serialized = repr(payloads)
    for secret in (
        "private-error-905",
        "91929394959697",
        "private-query-905",
        "private-token-905",
    ):
        assert secret not in serialized


async def test_buyer_spring_retry_still_exports_one_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#133] I-1 재시도는 **호출당 span 1개**를 깨지 않는다.

    재시도 루프를 span 바깥에 두면 시도마다 span 이 나가 관측 계약(유계 transport 메타데이터
    1건)이 깨진다. 루프를 안쪽에 두어 statusClass 는 마지막 시도의 결과를 남긴다 —
    "503 뒤 200" 은 성공한 호출이므로 2xx 다. 재시도 사실은 구조화 로그로만 관측한다.
    """
    from app.services import spring_client

    # [#394] 기본값이 0으로 바뀌어 재시도 루프 자체를 켜서 검증하려면 명시 주입이 필요하다.
    monkeypatch.setattr(get_settings(), "spring_max_retries", 1)

    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "private-transient-907"})
        return httpx.Response(200, json={"success": True, "data": []})

    monkeypatch.setattr(
        spring_client,
        "_client",
        lambda *, timeout=None: httpx.AsyncClient(
            base_url="https://spring.private.test",
            transport=httpx.MockTransport(handler),
        ),
    )

    async def driver() -> None:
        result = await spring_client.search_products(ProductSearchFilters(keyword="q-907"))
        assert result.products == []

    payloads = await _run_with_langsmith_payload(driver)
    spring_payloads = _spring_payloads(payloads)

    assert len(calls) == 2  # 재시도가 실제로 일어났다
    assert len(spring_payloads) == 1  # 그래도 span 은 1개
    assert spring_payloads[0]["extra"]["metadata"] == {
        "httpMethod": "GET",
        "upstream": "spring",
        "statusClass": "2xx",
    }
    assert "private-transient-907" not in repr(payloads)


async def test_buyer_spring_remote_disconnect_exports_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """응답 중단도 span 에 statusClass 를 남긴다 (PR #235 리뷰).

    `RemoteProtocolError` 는 `NetworkError` 의 형제라 `_spring_span` 의 isinstance 에서 빠지면
    statusClass 가 **아예 안 붙는다** — trace 에서 "실패했는데 원인 미분류"와 "기록 자체가
    없음"이 구분되지 않는다. 로그(`_failure_status_class`)는 `connection_error` 로 찍는데
    span 만 비면 로그↔trace 대조가 깨진다.

    재시도를 모두 소진한 마지막 시도가 이 실패일 때 도달하는 경로다(중간 시도는 재시도되어
    span 에 남지 않는다).
    """
    from app.services import spring_client

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("private-disconnect-908")

    monkeypatch.setattr(
        spring_client,
        "_client",
        lambda *, timeout=None: httpx.AsyncClient(
            base_url="https://spring.private.test",
            transport=httpx.MockTransport(handler),
        ),
    )

    async def driver() -> None:
        with pytest.raises(SpringUnavailableError):
            await spring_client.search_products(ProductSearchFilters(keyword="q-908"))

    payloads = await _run_with_langsmith_payload(driver)
    spring_payloads = _spring_payloads(payloads)

    assert len(spring_payloads) == 1
    assert spring_payloads[0]["extra"]["metadata"] == {
        "httpMethod": "GET",
        "upstream": "spring",
        "statusClass": "connection_error",
    }
    serialized = repr(payloads)
    for secret in ("RemoteProtocolError", "private-disconnect-908"):
        assert secret not in serialized


async def test_buyer_spring_connect_failure_exports_fixed_code_without_exception_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import spring_client

    raw_error = httpx.ConnectError(
        "private-connect-error-906",
        request=httpx.Request("GET", "https://spring.private.test/private-path-906"),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        raise raw_error

    monkeypatch.setattr(
        spring_client,
        "_client",
        lambda *, timeout=None: httpx.AsyncClient(
            base_url="https://spring.private.test",
            headers={"X-Internal-Token": "private-token-906"},
            transport=httpx.MockTransport(handler),
        ),
    )

    async def driver() -> None:
        with pytest.raises(SpringUnavailableError) as raised:
            await spring_client.search_products(ProductSearchFilters(keyword="private-query-906"))
        assert raised.value.__cause__ is raw_error
        traceback_names = []
        traceback = raw_error.__traceback__
        while traceback is not None:
            traceback_names.append(traceback.tb_frame.f_code.co_name)
            traceback = traceback.tb_next
        assert "handler" in traceback_names

    payloads = await _run_with_langsmith_payload(driver)
    spring_payloads = _spring_payloads(payloads)

    assert len(spring_payloads) == 1
    assert spring_payloads[0]["extra"]["metadata"] == {
        "httpMethod": "GET",
        "upstream": "spring",
        "statusClass": "connection_error",
    }
    serialized = repr(payloads)
    for secret in (
        "ConnectError",
        "private-connect-error-906",
        "private-query-906",
        "private-token-906",
    ):
        assert secret not in serialized


async def test_buyer_spring_cancellation_is_rethrown_unchanged_outside_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import spring_client

    cancellation = asyncio.CancelledError("private-cancellation-908")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise cancellation

    monkeypatch.setattr(
        spring_client,
        "_client",
        lambda *, timeout=None: httpx.AsyncClient(
            base_url="https://spring.private.test",
            headers={"X-Internal-Token": "private-token-908"},
            transport=httpx.MockTransport(handler),
        ),
    )

    async def driver() -> None:
        with pytest.raises(asyncio.CancelledError) as raised:
            await spring_client.search_products(ProductSearchFilters(keyword="private-query-908"))
        assert raised.value is cancellation

    payloads = await _run_with_langsmith_payload(driver)
    spring_payloads = _spring_payloads(payloads)

    assert len(spring_payloads) == 1
    assert spring_payloads[0]["extra"]["metadata"] == {
        "httpMethod": "GET",
        "upstream": "spring",
    }
    serialized = repr(payloads)
    for secret in ("CancelledError", "private-cancellation-908", "private-token-908"):
        assert secret not in serialized


async def test_all_buyer_spring_operations_trace_timeout_without_changing_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import spring_client

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private timeout detail", request=request)

    monkeypatch.setattr(
        spring_client,
        "_client",
        lambda *, timeout=None: httpx.AsyncClient(
            base_url="https://spring.private.test",
            headers={"X-Internal-Token": "private-token-902"},
            transport=httpx.MockTransport(handler),
        ),
    )
    cases = (
        (
            "spring.search_products",
            "GET",
            lambda: spring_client.search_products(ProductSearchFilters(keyword="private-1")),
            SpringUnavailableError,
        ),
        (
            "spring.get_recent_purchases",
            "GET",
            lambda: _real_get_recent_purchases(81828384858687),
            SpringUnavailableError,
        ),
        (
            "spring.get_order_status",
            "GET",
            lambda: _real_get_order_status(81828384858687),
            OrderStatusUnavailableError,
        ),
        (
            "spring.add_to_cart",
            "POST",
            lambda: spring_client.add_to_cart(
                AddToCartRequest(
                    user_id=81828384858687,
                    product_id=91929394959697,
                    quantity=1,
                )
            ),
            CartError,
        ),
        (
            "spring.get_cart",
            "GET",
            lambda: spring_client.get_cart(user_id=81828384858687),
            SpringUnavailableError,
        ),
        (
            "spring.push_recommendations",
            "POST",
            lambda: spring_client.push_recommendations(
                RecommendationPush(
                    session_id="private-session",
                    recommendation_request_id="private-request",
                    list_type="PICK_ONE",
                    lists=[
                        RecommendationListEntry(
                            list_id="private-list",
                            product_ids=[91929394959697],
                        )
                    ],
                )
            ),
            SpringUnavailableError,
        ),
        (
            "spring.fetch_product_changes",
            "GET",
            lambda: spring_client.fetch_product_changes("private-cursor"),
            SpringUnavailableError,
        ),
    )

    async def driver() -> None:
        for name, _method, invoke, expected_error in cases:
            try:
                await invoke()
            except expected_error:
                pass
            else:
                pytest.fail(f"{name} did not preserve {expected_error.__name__} mapping")

    payloads = await _run_with_langsmith_payload(driver)
    spring_payloads = _spring_payloads(payloads)

    assert len(spring_payloads) == len(cases)
    for name, method, _invoke, _error in cases:
        matches = [payload for payload in spring_payloads if payload["name"] == name]
        assert len(matches) == 1
        assert matches[0]["extra"]["metadata"] == {
            "httpMethod": method,
            "upstream": "spring",
            "statusClass": "timeout",
        }
    serialized = repr(payloads)
    for secret in (
        "ReadTimeout",
        "private timeout detail",
        "spring.private.test",
        "/internal/",
        "private-token-902",
        "81828384858687",
        "91929394959697",
        "private-session",
        "private-list",
        "private-cursor",
    ):
        assert secret not in serialized


async def test_needs_expansion_is_owned_by_recommendation_graph() -> None:
    decompose = {
        "intent": "recommend",
        "reply": "",
        "case": 3,
        "semanticQuery": "집들이 선물",
        "categoryQueries": [{"category": None, "query": "집들이 선물"}],
        "filters": {},
    }

    async def _map_unresolved(*, category_queries, utterance, settings, **_):
        """[#217] 전개 트리거가 **매핑 실패**라, 이 스팬을 태우려면 매퍼가 실패를 보고해야 한다.

        `'집들이 선물'` 은 실측 0.2904 / 마진 0.0073 으로 거리컷에 걸린다(설계 §4.5). 종전에는
        목적 marker `'선물'` 로 매핑 전에 트리거됐으므로 기본 fake 매퍼로도 스팬이 떴다.
        """
        return CategoryMapping(legs=[], unresolved=[q.query for q in category_queries if q.query])

    async def driver() -> None:
        await _collect(
            run_buyer_turn(
                _request("집들이 선물"),
                _member(),
                llm=FakeLLM(decompose=decompose),
                search=_search_with_span,
                push_fn=_push_with_span,
                map_categories=_map_unresolved,
            )
        )

    exported = await _run_with_trace(driver)
    by_name = {node.name: node for node in exported}

    assert by_name["llm.needs_expansion"].parent_id == by_name["buyer.graph.recommendation"].id


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


async def test_recommendation_route_sets_observability_lane() -> None:
    """구매자 추천 턴은 초기 buyer 표식이 아니라 집계용 recommend 레인으로 확정한다."""

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
    root = next(node for node in exported if node.parent_id is None)
    assert root.metadata["lane"] == "recommend"


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


async def test_cart_merge_failure_marks_routed_cart_without_changing_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_cart(*, user_id=None, guest_id=None):
        del user_id, guest_id
        raise SpringUnavailableError("customer@example.com cart exploded")

    async def add_cart(request):
        del request
        return AddToCartResult(success=True, cart_item_id=1)

    monkeypatch.setattr("app.services.spring_client.get_cart", get_cart)
    monkeypatch.setattr("app.services.spring_client.add_to_cart", add_cart)
    cart_store = await get_cart_store()
    request = _request()
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "member",
            buyer_owner_id(_member(), get_settings()),
        )
    )
    await cart_store.set_last_reco(
        context_thread_key(context.context_id, request.thread_id),
        [(101, "상품")],
    )
    decompose = {
        "intent": "cart_add",
        "reply": "",
        "case": 2,
        "semanticQuery": "",
        "categoryQueries": [],
        "filters": {},
        "cart": {"productId": 101, "quantity": 1},
    }
    frames: list[str] = []

    async def driver() -> None:
        frames.extend(
            await _collect(
                run_buyer_turn(
                    _request(),
                    _member(),
                    llm=FakeLLM(decompose=decompose),
                )
            )
        )

    exported = await _assert_degrade(driver, "cart_merge_skipped")
    by_name = {node.name: node for node in exported}
    root = by_name["buyer_chat_turn"]
    assert by_name["buyer.routing"].parent_id == root.id
    assert by_name["llm.decompose"].parent_id == by_name["buyer.routing"].id
    assert by_name["buyer.graph.cart"].parent_id == root.id
    events = [json.loads(frame.removeprefix("data:").strip()) for frame in frames]
    # progress_events_enabled 기본 on(#396) — 스트림 맨 앞에 progress 프레임이 추가된다.
    assert [event["type"] for event in events] == ["progress", "action", "done"]
    assert events[1]["data"] == {
        "type": "CART_ADDED",
        "message": "장바구니에 담았어요.",
        "cartItemId": 1,
        "quantity": None,  # [#285] I-25 전용 필드 — CART_ADDED 는 쓰지 않아 항상 None
        "reason": None,
    }


async def test_partial_fanout_marks_bounded_degrade() -> None:
    # [#115] 매퍼는 llm·tier 도 받는다(§4.4 마진 택일) — 빠지면 TypeError 를 그래프의 방어
    # except 가 삼켜 legs 가 비고, fan-out 이 아예 안 일어나 degrade 도 찍히지 않는다.
    async def mapper(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        del category_queries, utterance, settings, llm, tier
        return CategoryMapping(legs=[("카테고리-A", "A"), ("카테고리-B", "B")])

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


async def test_combined_fanout_and_dedup_failure_uses_stable_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def purchases(user_id, status=None):
        del user_id, status
        await asyncio.sleep(0)
        raise SpringUnavailableError("customer@example.com dedup exploded")

    # [#115] 매퍼는 llm·tier 도 받는다(§4.4 마진 택일) — 빠지면 TypeError 를 그래프의 방어
    # except 가 삼켜 legs 가 비고, fan-out 이 아예 안 일어나 degrade 도 찍히지 않는다.
    async def mapper(*, category_queries, utterance, settings, llm=None, tier="fast", **_):
        del category_queries, utterance, settings, llm, tier
        return CategoryMapping(legs=[("카테고리-A", "A"), ("카테고리-B", "B")])

    async def search(filters, exclude_product_ids=None):
        del exclude_product_ids
        if filters.category == "카테고리-B":
            raise SpringUnavailableError("customer@example.com fanout exploded")
        return ProductSearchResult(products=[DEFAULT_PRODUCTS[0]], total_count=1)

    monkeypatch.setattr("app.services.spring_client.get_recent_purchases", purchases)

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


async def _assert_degrade(driver, reason: str) -> tuple:
    exported = await _run_with_trace(driver)
    root = next(node for node in exported if node.parent_id is None)
    assert reason in BUYER_DEGRADE_REASONS
    assert root.metadata["degraded"] is True
    assert root.metadata["degradeReason"] == reason
    assert "customer@example.com" not in repr(root.metadata)
    return exported


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
