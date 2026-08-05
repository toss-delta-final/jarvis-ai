"""I-4 order-status contract, client, deterministic rendering, and route tests."""

from __future__ import annotations

import ast
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
from pydantic import ValidationError

from app.agents.buyer.order_status import (
    format_order_status,
    member_order_identity,
    stream_order_status,
)
from app.core.auth import Identity
from app.schemas.spring import (
    ORDER_ITEM_STATUS_TEXT,
    ORDER_STATUS_RECENT,
    OrderStatusItem,
    OrderStatusOrder,
    OrderStatusSummary,
)
from app.services import spring_client
from app.services.spring_client import OrderStatusUnavailableError


STATUS_PAIRS = tuple(ORDER_ITEM_STATUS_TEXT.items())
OrderStatusErrorCategory = Literal["upstream_unavailable", "malformed_response"]
ORDER_STATUS_ERROR_CATEGORIES: tuple[OrderStatusErrorCategory, OrderStatusErrorCategory] = (
    "upstream_unavailable",
    "malformed_response",
)
REPRESENTATIVE_STATUSES = (
    "결제 대기",
    "결제 실패",
    "주문 완료",
    "배송중",
    "배송 완료",
    "구매 확정",
    "취소/반품 진행중",
    "처리 완료",
)


def _item(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "productName": "러닝화",
        "status": "SHIPPING",
        "statusText": "배송중",
    }
    value.update(overrides)
    return value


def _order(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "orderId": 42,
        "orderedAt": "2026-07-29T12:30:00+09:00",
        "representativeStatus": "배송중",
        "items": [_item()],
    }
    value.update(overrides)
    return value


def _payload(*, orders: object = None) -> dict[str, object]:
    if orders is None:
        orders = [_order()]
    return {"success": True, "data": {"orders": orders}}


def _summary(*orders: dict[str, object]) -> OrderStatusSummary:
    return OrderStatusSummary.model_validate({"orders": list(orders)})


def _mock_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        spring_client,
        "_client",
        lambda: httpx.AsyncClient(
            base_url="https://spring.test",
            headers={"X-Internal-Token": "internal-secret"},
            timeout=3,
            transport=transport,
        ),
    )


async def _collect(stream) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    async for frame in stream:
        events.append(json.loads(frame.removeprefix("data: ").strip()))
    return events


# ── Schema ──


def test_schema_parses_complete_camel_case_payload_and_preserves_order() -> None:
    summary = _summary(
        _order(orderId=2, orderedAt="2026-07-30T00:00:00Z"),
        _order(orderId=1, orderedAt="2026-07-29T00:00:00-04:00"),
    )
    assert [order.order_id for order in summary.orders] == [2, 1]
    assert summary.orders[0].ordered_at.utcoffset() == timedelta(0)
    assert summary.orders[1].ordered_at.utcoffset() == timedelta(hours=-4)


def test_schema_accepts_exact_empty_orders() -> None:
    assert OrderStatusSummary.model_validate({"orders": []}).orders == []


@pytest.mark.parametrize("orders", [None, {}, "", 1])
def test_schema_rejects_missing_null_or_wrong_type_orders(orders: object) -> None:
    payload = {} if orders == "" else {"orders": orders}
    with pytest.raises(ValidationError):
        OrderStatusSummary.model_validate(payload)


@pytest.mark.parametrize("order_id", [1, 42, 2**63 - 1])
def test_order_id_accepts_strict_signed_bigint(order_id: int) -> None:
    assert OrderStatusOrder.model_validate(_order(orderId=order_id)).order_id == order_id


@pytest.mark.parametrize(
    "order_id",
    [True, False, "1", "001", "+1", " 1", 1.0, 42.0, Decimal("1"), None, 0, -1, 2**63],
)
def test_order_id_rejects_coercion_and_out_of_range(order_id: object) -> None:
    with pytest.raises(ValidationError):
        OrderStatusOrder.model_validate(_order(orderId=order_id))


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("orderId", None),
        ("orderedAt", None),
        ("orderedAt", 1_722_251_400),
        ("orderedAt", "2026-07-29T12:30:00"),
        ("orderedAt", "not-a-date"),
        ("representativeStatus", None),
        ("items", None),
        ("items", {}),
    ],
)
def test_order_rejects_missing_null_wrong_type_and_naive_datetime(field: str, bad: object) -> None:
    value = _order(**{field: bad})
    with pytest.raises(ValidationError):
        OrderStatusOrder.model_validate(value)
    value.pop(field)
    with pytest.raises(ValidationError):
        OrderStatusOrder.model_validate(value)


@pytest.mark.parametrize("field", ["productName", "status", "statusText"])
def test_item_rejects_missing_and_null_fields(field: str) -> None:
    value = _item(**{field: None})
    with pytest.raises(ValidationError):
        OrderStatusItem.model_validate(value)
    value.pop(field)
    with pytest.raises(ValidationError):
        OrderStatusItem.model_validate(value)


def test_product_name_has_strict_200_character_boundary() -> None:
    assert len(OrderStatusItem.model_validate(_item(productName="가" * 200)).product_name) == 200
    for invalid in ("가" * 201, 123, None):
        with pytest.raises(ValidationError):
            OrderStatusItem.model_validate(_item(productName=invalid))


@pytest.mark.parametrize(("status", "status_text"), STATUS_PAIRS)
def test_each_canonical_status_pair_is_accepted(status: str, status_text: str) -> None:
    item = OrderStatusItem.model_validate(_item(status=status, statusText=status_text))
    assert (item.status, item.status_text) == (status, status_text)


def test_status_mapping_is_immutable_and_mismatched_pairs_fail() -> None:
    with pytest.raises(TypeError):
        ORDER_ITEM_STATUS_TEXT["SHIPPING"] = "변조"  # type: ignore[index]
    with pytest.raises(ValidationError):
        OrderStatusItem.model_validate(_item(status="SHIPPING", statusText="배송 완료"))
    with pytest.raises(ValidationError):
        OrderStatusItem.model_validate(_item(status="DELIVERED", statusText="배송중"))


@pytest.mark.parametrize("field", ["status", "statusText"])
@pytest.mark.parametrize("suffix", ["\n", "\x00", "\u202e"])
def test_status_fields_reject_control_or_format_characters(field: str, suffix: str) -> None:
    with pytest.raises(ValidationError):
        OrderStatusItem.model_validate(_item(**{field: str(_item()[field]) + suffix}))


@pytest.mark.parametrize("representative_status", REPRESENTATIVE_STATUSES)
def test_representative_status_exact_vocabulary(representative_status: str) -> None:
    order = OrderStatusOrder.model_validate(_order(representativeStatus=representative_status))
    assert order.representative_status == representative_status


@pytest.mark.parametrize(
    "mutator",
    [
        lambda: _item(status="IN_TRANSIT"),
        lambda: _item(statusText="운송중"),
        lambda: _order(representativeStatus="완료됨"),
    ],
)
def test_vocabulary_drift_fails(mutator) -> None:
    value = mutator()
    model = OrderStatusItem if "productName" in value else OrderStatusOrder
    with pytest.raises(ValidationError):
        model.model_validate(value)


def test_all_items_are_validated_but_orders_are_capped_at_three() -> None:
    many_items = [_item(productName=f"상품 {index}") for index in range(20)]
    assert len(OrderStatusOrder.model_validate(_order(items=many_items)).items) == 20
    assert len(_summary(*[_order(orderId=index + 1) for index in range(3)]).orders) == 3
    with pytest.raises(ValidationError):
        _summary(*[_order(orderId=index + 1) for index in range(4)])
    many_items[-1] = _item(status="UNKNOWN")
    with pytest.raises(ValidationError):
        OrderStatusOrder.model_validate(_order(items=many_items))


# ── Client ──


async def test_client_uses_exact_method_path_query_header_and_parses_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json=_payload(), request=request)

    _mock_client(monkeypatch, handler)
    summary = await spring_client.get_order_status(42)
    assert len(summary.orders) == 1
    assert len(observed) == 1
    request = observed[0]
    assert request.method == "GET"
    assert request.url.path == "/internal/members/42/orders/status"
    assert request.url.query == b"recent=3"
    assert request.headers["X-Internal-Token"] == "internal-secret"
    assert request.extensions["timeout"] == {
        "connect": 3,
        "read": 3,
        "write": 3,
        "pool": 3,
    }


async def test_client_returns_exact_empty_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_client(monkeypatch, lambda request: httpx.Response(200, json=_payload(orders=[])))
    assert (await spring_client.get_order_status(42)).orders == []


@pytest.mark.parametrize("success", [False, None, 1, "true"])
async def test_client_requires_literal_success_true(
    monkeypatch: pytest.MonkeyPatch, success: object
) -> None:
    payload = _payload()
    payload["success"] = success
    _mock_client(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(OrderStatusUnavailableError) as caught:
        await spring_client.get_order_status(42)
    assert caught.value.category == "malformed_response"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {},
        {"data": {"orders": []}},
        {"success": True},
        {"success": True, "data": None},
        {"success": True, "data": []},
        {"success": True, "data": {}},
        {"success": True, "data": {"orders": None}},
        {"success": True, "data": {"orders": {}}},
        _payload(orders=[_order(orderId="42")]),
    ],
)
async def test_client_maps_envelope_and_schema_drift_to_malformed(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    _mock_client(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(OrderStatusUnavailableError) as caught:
        await spring_client.get_order_status(42)
    assert caught.value.category == "malformed_response"
    assert "42" not in str(caught.value)
    assert caught.value.__cause__ is None


async def test_client_maps_invalid_json_to_malformed_without_leaking_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "raw-order-999-product-secret"
    _mock_client(monkeypatch, lambda request: httpx.Response(200, text=secret))
    with pytest.raises(OrderStatusUnavailableError) as caught:
        await spring_client.get_order_status(42)
    assert caught.value.category == "malformed_response"
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("status_code", [404, 500])
async def test_client_maps_http_status_to_upstream_unavailable(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    _mock_client(
        monkeypatch,
        lambda request: httpx.Response(status_code, text="private response", request=request),
    )
    with pytest.raises(OrderStatusUnavailableError) as caught:
        await spring_client.get_order_status(42)
    assert caught.value.category == "upstream_unavailable"
    assert "private" not in str(caught.value)
    assert "/internal/" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_client_maps_network_and_timeout_to_upstream_unavailable(
    monkeypatch: pytest.MonkeyPatch, error_type
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error_type("sensitive transport detail", request=request)

    _mock_client(monkeypatch, handler)
    with pytest.raises(OrderStatusUnavailableError) as caught:
        await spring_client.get_order_status(42)
    assert caught.value.category == "upstream_unavailable"
    assert "sensitive" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_order_status_error_accepts_only_finite_categories() -> None:
    for category in ORDER_STATUS_ERROR_CATEGORIES:
        assert OrderStatusUnavailableError(category).category == category
    with pytest.raises(ValueError):
        OrderStatusUnavailableError("other")  # type: ignore[arg-type]


# ── Lane 2 public contract: identity, formatter, stream, fixed JSON log ──


@pytest.mark.parametrize("user_id", [1, "1", 2**63 - 1, str(2**63 - 1)])
def test_member_order_identity_accepts_only_canonical_member_ids(user_id: int | str) -> None:
    identity = SimpleNamespace(user_id=user_id, is_guest=False, seller_id=None, subject="spoof")
    assert member_order_identity(identity) == (int(user_id), "none")


@pytest.mark.parametrize(
    "user_id",
    [
        True,
        False,
        1.0,
        b"1",
        None,
        "",
        " ",
        " 1",
        "+1",
        "-1",
        "１",
        "1_0",
        "1.0",
        "0",
        0,
        -1,
        2**63,
    ],
)
def test_member_order_identity_rejects_noncanonical_ids_without_subject_fallback(
    user_id: object,
) -> None:
    identity = SimpleNamespace(user_id=user_id, is_guest=False, seller_id=None, subject="42")
    result, category = member_order_identity(identity)
    assert result is None
    assert category in {"missing_user_id", "invalid_user_id"}


def test_member_order_identity_rejects_guest_and_seller_first() -> None:
    guest = Identity(user_id="42", is_guest=True, seller_id=None, subject="42")
    seller = Identity(user_id="42", is_guest=False, seller_id="7", subject="42")
    assert member_order_identity(guest) == (None, "guest")
    assert member_order_identity(seller) == (None, "seller")


def test_member_order_identity_rejects_extremely_long_decimal_without_raising() -> None:
    identity = SimpleNamespace(
        user_id="9" * 5000,
        is_guest=False,
        seller_id=None,
        subject="42",
    )
    assert member_order_identity(identity) == (None, "invalid_user_id")


def test_member_order_identity_accepts_long_leading_zero_decimal_with_valid_suffix() -> None:
    identity = SimpleNamespace(
        user_id=("0" * 5000) + "42",
        is_guest=False,
        seller_id=None,
        subject="spoof",
    )
    assert member_order_identity(identity) == (42, "none")


def test_formatter_preserves_order_and_bounds_items_with_exact_remainder() -> None:
    orders = [
        _order(
            orderId=9,
            orderedAt="2026-07-30T00:30:00Z",
            items=[_item(productName=f"상품{index}") for index in range(1, 6)],
        ),
        _order(orderId=8, orderedAt="2026-07-28T23:00:00Z"),
    ]
    text = format_order_status(_summary(*orders))
    assert text.index("9") < text.index("8")
    assert "7월 30일" in text
    assert all(f"상품{index}" in text for index in range(1, 4))
    assert "상품4" not in text and "상품5" not in text
    assert "외 2개" in text
    assert "SHIPPING" not in text


def test_formatter_sanitizes_product_name_without_silent_valid_length_truncation() -> None:
    dirty = "앞\r\n중간\x00\u202e\u200b뒤"
    text = format_order_status(_summary(_order(items=[_item(productName=dirty)])))
    assert "앞 중간뒤" in text
    assert not any(char in text for char in ("\r", "\x00", "\u202e", "\u200b"))
    boundary = "가" * 200
    assert boundary in format_order_status(_summary(_order(items=[_item(productName=boundary)])))


async def test_stream_success_logs_before_token_and_emits_only_token_done(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ordering: list[str] = []

    async def fetch_order_status(user_id: int) -> OrderStatusSummary:
        assert user_id == 42
        return _summary(_order())

    caplog.set_level("INFO", logger="app.agents.buyer.order_status")
    stream = stream_order_status(
        identity=Identity(user_id="42", is_guest=False, seller_id=None, subject="42"),
        fetch_order_status=fetch_order_status,
        request_id="req-i4-correlation",
    )
    events: list[dict[str, object]] = []
    async for frame in stream:
        if not ordering:
            ordering.extend(record.message for record in caplog.records)
        events.append(json.loads(frame.removeprefix("data: ").strip()))
    assert ordering and json.loads(ordering[0])["event"] == "order_status_route"
    assert [event["type"] for event in events] == ["token", "done"]
    assert events[-1]["data"] == {"finishReason": "stop"}
    record = json.loads(caplog.records[0].message)
    assert set(record) == {
        "event",
        "requestId",
        "outcome",
        "errorCategory",
        "orderCount",
        "elapsedMs",
    }
    assert record["requestId"] == "req-i4-correlation"
    assert record["outcome"] == "success"
    assert record["errorCategory"] == "none"
    assert record["orderCount"] == 1
    assert isinstance(record["elapsedMs"], (int, float))


@pytest.mark.parametrize(
    ("identity", "expected_category"),
    [
        (Identity(user_id=None, is_guest=True, seller_id=None), "guest"),
        (Identity(user_id="42", is_guest=False, seller_id="7"), "seller"),
        (Identity(user_id=None, is_guest=False, seller_id=None, subject="42"), "missing_user_id"),
        (Identity(user_id="bad", is_guest=False, seller_id=None, subject="42"), "invalid_user_id"),
    ],
)
async def test_stream_identity_blocks_without_fetch_and_logs_safe_fixed_record(
    identity: Identity, expected_category: str, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    async def fetch_order_status(user_id: int) -> OrderStatusSummary:
        nonlocal calls
        calls += 1
        raise AssertionError("blocked identities must not fetch")

    caplog.set_level("INFO", logger="app.agents.buyer.order_status")
    events = await _collect(
        stream_order_status(
            identity=identity,
            fetch_order_status=fetch_order_status,
            request_id="req-blocked",
        )
    )
    assert calls == 0
    assert [event["type"] for event in events] == ["token", "done"]
    record = json.loads(caplog.records[0].message)
    assert (record["outcome"], record["errorCategory"], record["orderCount"]) == (
        "identity_blocked",
        expected_category,
        0,
    )


@pytest.mark.parametrize("category", ORDER_STATUS_ERROR_CATEGORIES)
async def test_stream_degrades_upstream_failures_without_pii_or_error_event(
    category: OrderStatusErrorCategory, caplog: pytest.LogCaptureFixture
) -> None:
    async def fetch_order_status(user_id: int) -> OrderStatusSummary:
        raise OrderStatusUnavailableError(category)

    caplog.set_level("INFO", logger="app.agents.buyer.order_status")
    events = await _collect(
        stream_order_status(
            identity=Identity(
                user_id="9223372036854775806",
                is_guest=False,
                seller_id=None,
                subject="9223372036854775806",
            ),
            fetch_order_status=fetch_order_status,
            request_id="req-degraded",
        )
    )
    assert [event["type"] for event in events] == ["token", "done"]
    record = json.loads(caplog.records[0].message)
    assert (record["outcome"], record["errorCategory"], record["orderCount"]) == (
        "upstream_degraded",
        category,
        0,
    )
    logged = caplog.records[0].message
    for forbidden in (
        "9223372036854775806",
        "/internal/members",
        "러닝화",
        "배송중",
        "internal-secret",
    ):
        assert forbidden not in logged


async def test_stream_empty_is_not_failure(caplog: pytest.LogCaptureFixture) -> None:
    async def fetch_order_status(user_id: int) -> OrderStatusSummary:
        return OrderStatusSummary(orders=[])

    caplog.set_level("INFO", logger="app.agents.buyer.order_status")
    events = await _collect(
        stream_order_status(
            identity=Identity(user_id="42", is_guest=False, seller_id=None),
            fetch_order_status=fetch_order_status,
            request_id="req-empty",
        )
    )
    assert [event["type"] for event in events] == ["token", "done"]
    assert events[0]["data"] == {"text": "최근 주문 내역이 없어요."}
    assert len(caplog.records) == 1
    record = json.loads(caplog.records[0].message)
    assert (record["outcome"], record["errorCategory"], record["orderCount"]) == (
        "empty",
        "none",
        0,
    )


# ── Lane 4 bounded documentation contract ──


def test_lane_c_documents_exact_eighteen_call_contract_and_i4_section() -> None:
    """[#162] I-3(§4.17) 등재로 레인 (c)는 17→18건이다."""
    text = Path("docs/api-spec.md").read_text(encoding="utf-8")
    lane = re.search(r"^- 레인 \(c\):[^\n]*", text, re.MULTILINE)
    assert lane is not None
    paragraph = lane.group(0)
    assert "18건" in paragraph
    assert re.search(r"(?<!\d)8건", paragraph) is None
    assert set(re.findall(r"I-\d+", paragraph)) == {
        "I-1",
        "I-3",
        "I-19",
        "I-4",
        "I-2",
        "I-18",
        "I-21",
        "I-6",
        "I-7",
        "I-13",
        "I-14",
        "I-15",
        "I-16",
        "I-9",
        "I-10",
        "I-11",
        "I-12",
        "I-17",
    }
    assert "### 4.10 주문 상태 요약 API (I-4)" in text


def test_recent_order_limit_has_one_contract_source() -> None:
    """Schema, client, and route must not carry independent copies of recent=3."""
    root = Path(__file__).parents[2]
    files = {
        # encoding 명시 — 미지정이면 로케일 기본(Windows 한국어는 cp949)이라 소스의 한국어 주석에서
        # UnicodeDecodeError 가 난다. 저장소 소스는 전부 UTF-8 이다.
        path: ast.parse((root / path).read_text(encoding="utf-8"))
        for path in (
            "app/schemas/spring.py",
            "app/services/spring_client.py",
            "app/agents/buyer/order_status.py",
        )
    }
    assignments = [
        path
        for path, tree in files.items()
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "ORDER_STATUS_RECENT"
            for target in node.targets
        )
    ]
    assert assignments == ["app/schemas/spring.py"]
    client_function = next(
        node
        for node in files["app/services/spring_client.py"].body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_order_status"
    )
    assert [argument.arg for argument in client_function.args.args] == ["user_id"]
    route_calls = [
        node
        for node in ast.walk(files["app/agents/buyer/order_status.py"])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "fetch_order_status"
    ]
    assert len(route_calls) == 1
    assert len(route_calls[0].args) == 1
    assert route_calls[0].keywords == []
    assert any(
        getattr(metadata, "max_length", None) == ORDER_STATUS_RECENT
        for metadata in OrderStatusSummary.model_fields["orders"].metadata
    )
