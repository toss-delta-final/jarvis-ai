"""장바구니 서브그래프 (이슈 #3) — 담기·옵션 되물음·조회·오류 매핑·라우팅·배선 회귀.

stream_cart_add/view 는 add_fn/get_cart_fn 주입으로, 라우팅은 run_buyer_turn + FakeLLM 으로,
spring_client 배선은 _client 몽키패치로 라이브 Spring 없이 구동한다.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agents.buyer.cart.graph import stream_cart_add, stream_cart_view
from app.agents.buyer.cart.state import CartStateStore, PendingAdd
from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.recommendation.state import CartIntent
from app.agents.buyer.session_state import context_thread_key
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.session_context import BuyerSessionInput
from app.schemas.spring import (
    AddToCartResult,
    CartOption,
    CartView,
    CartViewItem,
    ProductSearchResult,
)
from app.services.spring_client import (
    CartError,
    CartOptionInvalid,
    CartOptionRequired,
    CartProductNotFound,
    CartQuantityExceeded,
    CartStockInsufficient,
    SpringUnavailableError,
)


def _member() -> Identity:
    return Identity(user_id="123", is_guest=False, seller_id=None, subject="123")


def _guest() -> Identity:
    return Identity(user_id=None, is_guest=True, seller_id=None, subject="guest-uuid-1")


def _anon() -> Identity:
    return Identity(user_id=None, is_guest=True, seller_id=None, subject=None)


async def _committed_observer(request, identity):  # noqa: ANN001
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "guest" if identity.is_guest else "member",
            buyer_owner_id(identity, get_settings()),
        )
    )
    return SimpleNamespace(
        context_id=context.context_id,
        request_id="unit-request",
        record_model_call=lambda *_: None,
    )


async def run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    async for frame in _production_run_buyer_turn(
        request,
        identity,
        observer=observer,
        **kwargs,
    ):
        yield frame


async def _thread_key(request, identity) -> str:  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    return context_thread_key(observer.context_id, request.thread_id)


async def _collect(gen) -> list[dict]:
    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:") :].strip()))
    return events


def _types(events) -> list[str]:
    return [e["type"] for e in events]


def _empty_cart(**_):
    async def _get(*, user_id=None, guest_id=None):
        return CartView(items=[])

    return _get


# ─────────── 담기 성공 / 합산 ───────────


async def test_cart_add_success() -> None:
    store = CartStateStore()

    async def add_fn(req):
        return AddToCartResult(success=True, cart_item_id=55)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert action["cartItemId"] == 55
    assert _types(events)[-1] == "done"


async def test_cart_add_merge_notice_when_existing() -> None:
    store = CartStateStore()

    async def add_fn(req):
        return AddToCartResult(success=True, cart_item_id=56)

    async def get_cart_fn(*, user_id=None, guest_id=None):
        return CartView(
            items=[CartViewItem(cart_item_id=9, product_id=1, option_id=None, quantity=2)]
        )

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=get_cart_fn,
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert "더했" in action["message"]  # 합산 안내


# ─────────── 옵션 되물음 멀티턴 ───────────


async def test_cart_add_option_required_reasks_and_sets_pending() -> None:
    store = CartStateStore()

    async def add_fn(req):
        raise CartOptionRequired(
            [CartOption(option_id=3, name="블루"), CartOption(option_id=4, name="레드")]
        )

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    types = _types(events)
    assert "action" not in types  # 되물음은 실패 action 이 아니다(§4.1)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "블루" in token and "레드" in token
    pending = await store.get_pending("m:t")
    assert pending is not None and pending.product_id == 1


async def test_cart_option_reask_strips_seller_text() -> None:
    """Spring 옵션명(판매자 입력 영향)은 token 조립 후 위험 문자가 제거된다."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartOptionRequired(
            [
                CartOption(option_id=3, name="블\x1b[31m루\u200b\u202e"),
                CartOption(option_id=4, name="레\n드"),
            ]
        )

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="unsafe-option",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "블[31m루" in token and "레 드" in token
    assert all(ch not in token for ch in ("\x1b", "\u200b", "\u202e", "\n"))


async def test_cart_add_reask_then_success_clears_pending() -> None:
    store = CartStateStore()
    await store.set_pending(
        "m:t", PendingAdd(product_id=1, quantity=2, options=[CartOption(option_id=3, name="블루")])
    )

    async def add_fn(req):
        assert (
            req.product_id == 1 and req.option_id == 3 and req.quantity == 2
        )  # pending 상품/수량 + 이번 optionId
        return AddToCartResult(success=True, cart_item_id=77)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, option_id=3, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED" and action["cartItemId"] == 77
    assert await store.get_pending("m:t") is None  # 성공 후 정리


async def test_cart_add_option_invalid_exhausts_to_cart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "cart_option_reask_max", 1)
    store = CartStateStore()
    # 이미 1회 재질문한 상태(attempts=1) → 다음 INVALID 는 상한 초과 → CART_ERROR
    await store.set_pending(
        "m:t",
        PendingAdd(
            product_id=1, quantity=1, options=[CartOption(option_id=3, name="블루")], attempts=1
        ),
    )

    async def add_fn(req):
        raise CartOptionInvalid([CartOption(option_id=3, name="블루")])

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, option_id=9, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=settings,
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADD_FAILED" and action["reason"] == "CART_ERROR"
    assert await store.get_pending("m:t") is None


async def test_cart_add_option_invalid_reasks_within_limit() -> None:
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(
            product_id=1, quantity=1, options=[CartOption(option_id=3, name="블루")], attempts=0
        ),
    )

    async def add_fn(req):
        raise CartOptionInvalid([CartOption(option_id=3, name="블루")])

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, option_id=9, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    assert "action" not in _types(events)  # 아직 상한 내 → 재질문
    assert (await store.get_pending("m:t")).attempts == 1


# ─────────── 담기 오류 매핑 ───────────


async def test_cart_add_product_not_found() -> None:
    store = CartStateStore()

    async def add_fn(req):
        raise CartProductNotFound()

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=999, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADD_FAILED" and action["reason"] == "PRODUCT_NOT_FOUND"


async def test_cart_add_stock_insufficient_exposes_remaining() -> None:
    """재고 부족 → reason STOCK_INSUFFICIENT + 남은 재고 수 노출(2026-07-22)."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartStockInsufficient(3)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=5),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADD_FAILED" and action["reason"] == "STOCK_INSUFFICIENT"
    assert "3" in action["message"]


async def test_cart_add_stock_insufficient_without_count_falls_back() -> None:
    """남은 재고 수 미상(None) → 일반 재고부족 안내(reason 은 여전히 STOCK_INSUFFICIENT)."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartStockInsufficient(None)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=5),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADD_FAILED" and action["reason"] == "STOCK_INSUFFICIENT"


async def test_cart_add_stock_zero_says_soldout() -> None:
    """재고 0(품절, BE ON_SALE+stock 0) → "품절된 상품이에요"(reason 은 STOCK_INSUFFICIENT 유지)."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartStockInsufficient(0)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADD_FAILED" and action["reason"] == "STOCK_INSUFFICIENT"
    assert action["message"] == "품절된 상품이에요."


async def test_cart_add_quantity_exceeded_uses_be_message() -> None:
    """수량 상한 초과(합산 > 99, BE VALIDATION_ERROR) → CART_ERROR + BE 동일 문구."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartQuantityExceeded("합산 초과")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=5),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADD_FAILED" and action["reason"] == "CART_ERROR"
    assert action["message"] == "수량은 최대 99개까지 담을 수 있습니다."


async def test_cart_add_error_maps_to_cart_error() -> None:
    store = CartStateStore()

    async def add_fn(req):
        raise CartError("token invalid")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADD_FAILED" and action["reason"] == "CART_ERROR"


async def test_cart_add_degrades_when_get_cart_fails() -> None:
    """조회 실패해도 담기는 진행한다(§4.9 degrade)."""
    store = CartStateStore()

    async def add_fn(req):
        return AddToCartResult(success=True, cart_item_id=1)

    async def get_cart_fn(*, user_id=None, guest_id=None):
        raise SpringUnavailableError("down")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=get_cart_fn,
        )
    )
    assert next(e for e in events if e["type"] == "action")["data"]["type"] == "CART_ADDED"


async def test_cart_add_no_product_asks_clarify() -> None:
    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("productId 없으면 add 호출 금지")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=None, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    assert "action" not in _types(events)
    assert "어떤 상품" in next(e for e in events if e["type"] == "token")["data"]["text"]


async def test_cart_add_anon_requires_login() -> None:
    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("익명은 add 호출 금지")

    events = await _collect(
        stream_cart_add(
            identity=_anon(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="a:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADD_FAILED" and action["reason"] == "CART_ERROR"


async def test_cart_add_guest_uses_guest_id() -> None:
    store = CartStateStore()
    captured = {}

    async def add_fn(req):
        captured["userId"] = req.user_id
        captured["guestId"] = req.guest_id
        return AddToCartResult(success=True, cart_item_id=1)

    await _collect(
        stream_cart_add(
            identity=_guest(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="g:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    assert captured["userId"] is None and captured["guestId"] == "guest-uuid-1"


# ─────────── 조회 ───────────


async def test_cart_view_lists_items() -> None:
    async def get_cart_fn(*, user_id=None, guest_id=None):
        return CartView(
            items=[
                CartViewItem(
                    cart_item_id=1,
                    product_id=1,
                    product_name="방수 파우치",
                    option_name="블루",
                    quantity=2,
                )
            ]
        )

    events = await _collect(stream_cart_view(identity=_member(), get_cart_fn=get_cart_fn))
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "방수 파우치" in token and "블루" in token and "2개" in token


async def test_cart_view_strips_seller_text() -> None:
    """장바구니 상품명·옵션명은 사용자 token 경계에서 정제된다."""

    async def get_cart_fn(*, user_id=None, guest_id=None):
        return CartView(
            items=[
                CartViewItem(
                    cart_item_id=1,
                    product_id=1,
                    product_name="방수\x1b[31m 파우치\u200b\u202e",
                    option_name="블\n루",
                    quantity=2,
                ),
                CartViewItem(
                    cart_item_id=2,
                    product_id=2,
                    product_name="정상 상품",
                    quantity=1,
                ),
            ]
        )

    events = await _collect(stream_cart_view(identity=_member(), get_cart_fn=get_cart_fn))
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == ("장바구니에 담긴 상품이에요:\n방수[31m 파우치 (블 루) · 2개\n정상 상품 · 1개")
    assert all(ch not in token for ch in ("\x1b", "\u200b", "\u202e"))


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("AVAILABLE", ""),
        (None, ""),
        ("SOLD_OUT", " (품절)"),
        ("HIDDEN", " (판매 종료)"),
    ],
)
def test_state_suffix(state, expected: str) -> None:
    """상태→라벨은 순수 함수 분기다(REQ-CART-037 — 프롬프트가 아니라 테스트 가능해야 한다).

    `AVAILABLE` 과 미수신(`None`)이 둘 다 빈 문자열인 것이 핵심이다 — 앞은 "살 수 있다"를 굳이
    말하지 않는 표현 정책이고 뒤는 모름을 주장으로 바꾸지 않겠다는 것이라, 근거는 달라도
    사용자에게 보이는 결과가 같다."""
    from app.agents.buyer.cart.purchase_state import state_suffix

    assert state_suffix(state) == expected


async def test_cart_view_marks_sold_out_and_hidden_with_advice_once() -> None:
    """목록 줄에는 짧은 라벨만, 행동 안내는 문단 끝에 상태당 **한 번만** 싣는다(#310).

    같은 상태 항목이 둘이어도 안내 문장이 두 번 나오면 안 된다 — 목록이 문장 덩어리가 된다.
    판매 종료 안내는 예시 발화(`'가죽 지갑 빼줘'`)로 유도한다: 이슈 예시의 "뺄 상품을
    추천해드릴까요?"를 그대로 쓰면 이 되물음이 상태를 저장하지 않아 사용자가 "응"이라 답해도
    삭제로 라우팅되지 않는다(`remove.py::_unresolved_notice` 가 문서화한 함정)."""

    async def get_cart_fn(*, user_id=None, guest_id=None):
        return CartView(
            items=[
                CartViewItem(cart_item_id=1, product_id=1, product_name="방수 파우치", quantity=2),
                CartViewItem(
                    cart_item_id=2,
                    product_id=2,
                    product_name="린넨 셔츠",
                    quantity=1,
                    purchase_state="SOLD_OUT",
                ),
                CartViewItem(
                    cart_item_id=3,
                    product_id=3,
                    product_name="가죽 지갑",
                    quantity=1,
                    purchase_state="HIDDEN",
                ),
                CartViewItem(
                    cart_item_id=4,
                    product_id=4,
                    product_name="양말",
                    quantity=1,
                    purchase_state="HIDDEN",
                ),
            ]
        )

    events = await _collect(stream_cart_view(identity=_member(), get_cart_fn=get_cart_fn))
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "방수 파우치 · 2개" in token  # AVAILABLE 아닌 미수신 — 라벨 없음
    assert "린넨 셔츠 · 1개 (품절)" in token
    assert "가죽 지갑 · 1개 (판매 종료)" in token
    assert token.count("재입고되면") == 1
    assert token.count("빼는 걸 추천드려요") == 1
    assert "'가죽 지갑 빼줘'" in token  # 판매 종료 항목 중 첫 번째를 예시로


async def test_cart_view_all_available_has_no_advice() -> None:
    """전부 구매 가능하면 안내 줄이 붙지 않는다 — 오늘 문구와 바이트 동일해야 한다."""

    async def get_cart_fn(*, user_id=None, guest_id=None):
        return CartView(
            items=[
                CartViewItem(
                    cart_item_id=1,
                    product_id=1,
                    product_name="방수 파우치",
                    option_name="블루",
                    quantity=2,
                    purchase_state="AVAILABLE",
                )
            ]
        )

    events = await _collect(stream_cart_view(identity=_member(), get_cart_fn=get_cart_fn))
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == "장바구니에 담긴 상품이에요:\n방수 파우치 (블루) · 2개"


async def test_cart_view_empty() -> None:
    events = await _collect(stream_cart_view(identity=_member(), get_cart_fn=_empty_cart()))
    assert "비어" in next(e for e in events if e["type"] == "token")["data"]["text"]


async def test_cart_view_unavailable() -> None:
    async def get_cart_fn(*, user_id=None, guest_id=None):
        raise SpringUnavailableError("down")

    events = await _collect(stream_cart_view(identity=_member(), get_cart_fn=get_cart_fn))
    assert "불러오지" in next(e for e in events if e["type"] == "token")["data"]["text"]


# ─────────── 라우팅 (run_buyer_turn + FakeLLM) ───────────


def _req(message="담아줘", thread_id="t1"):
    return SimpleNamespace(session_id="s1", thread_id=thread_id, message=message)


async def test_route_cart_add(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_add(req):
        return AddToCartResult(success=True, cart_item_id=42)

    async def fake_get(*, user_id=None, guest_id=None):
        return CartView(items=[])

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", fake_get)
    # 직전 추천이 있어야 담기 가능(경로 B) — last_reco 시드.
    from app.agents.buyer.cart.state import get_cart_store

    seed_store = await get_cart_store()
    request = _req()
    await seed_store.set_last_reco(await _thread_key(request, _member()), [(101, "이어폰")])
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 101, "quantity": 1}})
    events = await _collect(run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED" and action["cartItemId"] == 42


async def test_route_cart_add_forwards_message_to_pending_switch_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """호출부가 원문 발화를 넘겨 fast 에코형 전환도 옛 상품 담기 전에 차단한다."""
    from app.agents.buyer.cart.state import get_cart_store
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_add(req):
        raise AssertionError(f"해소 실패 전환은 Spring 담기에 도달하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    request = _req(message="다른 거 담아줘", thread_id="t-switch-message")
    store = await get_cart_store()
    key = await _thread_key(request, _member())
    await store.set_last_reco(key, [(101, "세탁 세제"), (201, "무선 이어폰")])
    await store.set_pending(
        key,
        PendingAdd(product_id=101, quantity=1, options=[CartOption(option_id=1001, name="일반형")]),
    )
    llm = FakeLLM(
        decompose={
            "intent": "cart_add",
            "cart": {"productId": 101, "optionId": 1002, "quantity": 1},
        }
    )

    events = await _collect(run_buyer_turn(request, _member(), llm=llm))

    # progress_events_enabled 기본 on(#396) — 스트림 맨 앞에 progress 프레임이 추가된다.
    assert _types(events) == ["progress", "token", "done"]
    assert await store.get_pending(key) is None


async def test_route_cart_view(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_get(*, user_id=None, guest_id=None):
        return CartView(
            items=[CartViewItem(cart_item_id=1, product_id=1, product_name="키보드", quantity=1)]
        )

    monkeypatch.setattr(sc, "get_cart", fake_get)
    llm = FakeLLM(decompose={"intent": "cart_view", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="장바구니 뭐 있어?"), _member(), llm=llm))
    assert "키보드" in next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "action" not in _types(events)


async def test_route_cart_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    """[라운드 24] decompose 가 직접 cart_remove 를 산출하면 stream_cart_remove 로 위임된다."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_get(*, user_id=None, guest_id=None):
        return CartView(
            items=[CartViewItem(cart_item_id=1, product_id=1, product_name="키보드", quantity=1)]
        )

    async def fake_delete(cart_item_id, *, user_id=None, guest_id=None):
        assert cart_item_id == 1
        return None

    monkeypatch.setattr(sc, "get_cart", fake_get)
    monkeypatch.setattr(sc, "delete_cart_item", fake_delete)
    llm = FakeLLM(decompose={"intent": "cart_remove", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="키보드 빼줘"), _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_REMOVED"


async def test_route_wishlist_add(monkeypatch: pytest.MonkeyPatch) -> None:
    """[라운드 24] decompose 가 직접 wishlist_add 를 산출하면 stream_wishlist_add 로 위임된다.

    찜 추가는 경로 B 가드(`allowed`)가 반드시 필요하다 — last_reco 시드가 그 가드를 통과시킨다.
    """
    from app.agents.buyer.cart.state import get_cart_store
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_add_wishlist(req):
        assert req.product_id == 101
        return None

    monkeypatch.setattr(sc, "add_wishlist", fake_add_wishlist)
    seed_store = await get_cart_store()
    request = _req(message="이거 찜해줘")
    await seed_store.set_last_reco(await _thread_key(request, _member()), [(101, "이어폰")])
    llm = FakeLLM(decompose={"intent": "wishlist_add", "cart": {"productId": 101}})
    events = await _collect(run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "WISHLIST_ADDED"


async def test_route_wishlist_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    """[라운드 24] decompose 가 직접 wishlist_remove 를 산출하면 stream_wishlist_remove 로 위임된다."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    async def fake_get_wishlist(user_id):
        return WishlistView(items=[WishlistItem(product_id=1, name="키보드")])

    async def fake_remove_wishlist(product_id, *, user_id=None):
        assert product_id == 1
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(run_buyer_turn(_req(message="키보드 찜 빼줘"), _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "WISHLIST_REMOVED"


async def test_last_reco_stored_after_recommendation() -> None:
    """추천 턴이 후보를 last_reco 로 저장해 이후 담기의 productId 해소 소스가 된다."""
    from app.agents.buyer.cart.state import get_cart_store
    from tests._fakes import DEFAULT_PRODUCTS, FakeLLM

    async def search(filters, exclude_product_ids=None):
        return ProductSearchResult(products=DEFAULT_PRODUCTS, total_count=len(DEFAULT_PRODUCTS))

    async def push(p):
        return True

    await _collect(
        run_buyer_turn(
            _req(message="무선 이어폰 추천", thread_id="t9"),
            _member(),
            llm=FakeLLM(),
            search=search,
            push_fn=push,
        )
    )
    cart_store = await get_cart_store()
    reco = await cart_store.get_last_reco(
        await _thread_key(_req(message="무선 이어폰 추천", thread_id="t9"), _member())
    )
    assert [pid for pid, _ in reco] == [101, 102, 103]


# ─────────── spring_client 배선 (I-2 담기 · I-18 조회) ───────────


class _CartResp:
    def __init__(self, status_code, data) -> None:
        self.status_code = status_code
        self._data = data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._data


class _CartClient:
    def __init__(self, resp) -> None:
        self._resp = resp
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def post(self, url, json=None):
        self.calls.append(("POST", url, json))
        return self._resp

    async def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        return self._resp

    async def delete(self, url, params=None):
        self.calls.append(("DELETE", url, params))
        return self._resp


async def test_add_to_cart_success_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _CartClient(_CartResp(200, {"success": True, "data": {"cartItemId": 55}})),
    )
    res = await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=1))
    assert res.success and res.cart_item_id == 55


async def test_add_to_cart_option_required_raises_with_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[BE 확정 2026-07-18] error.detail.options = [{optionId, name, extraPrice}] 를 파싱한다."""
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    body = {
        "error": {
            "code": "CART_OPTION_REQUIRED",
            "detail": {
                "options": [
                    {"optionId": 3, "name": "블루", "extraPrice": 0},
                    {"optionId": 4, "name": "레드", "extraPrice": 1000},
                ]
            },
        }
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartOptionRequired) as ei:
        await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=1))
    opts = ei.value.options
    assert [o.option_id for o in opts] == [3, 4]
    assert [o.name for o in opts] == ["블루", "레드"]
    assert opts[1].extra_price == 1000


async def test_add_to_cart_option_required_legacy_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """구버전 위치(error.options, optionName)도 방어적으로 파싱한다(하위호환)."""
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    body = {
        "error": {
            "code": "CART_OPTION_REQUIRED",
            "options": [{"optionId": 9, "optionName": "그린"}],
        }
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartOptionRequired) as ei:
        await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=1))
    assert ei.value.options[0].option_id == 9 and ei.value.options[0].name == "그린"


async def test_add_to_cart_product_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    monkeypatch.setattr(
        sc, "_client", lambda: _CartClient(_CartResp(404, {"error": {"code": "PRODUCT_NOT_FOUND"}}))
    )
    with pytest.raises(sc.CartProductNotFound):
        await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=999, quantity=1))


async def test_add_to_cart_stock_insufficient_parses_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """400 CART_STOCK_INSUFFICIENT + error.detail.availableStock → CartStockInsufficient(3)."""
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    body = {"error": {"code": "CART_STOCK_INSUFFICIENT", "detail": {"availableStock": 3}}}
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartStockInsufficient) as ei:
        await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=5))
    assert ei.value.available_stock == 3


async def test_add_to_cart_stock_available_float_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """availableStock 이 BE Double 직렬화로 4.9999998 처럼 오면 round → 5(절삭 아님, 리뷰 #75)."""
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    body = {"error": {"code": "CART_STOCK_INSUFFICIENT", "detail": {"availableStock": 4.9999998}}}
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartStockInsufficient) as ei:
        await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=5))
    assert ei.value.available_stock == 5


async def test_add_to_cart_stock_insufficient_missing_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """availableStock 누락 시에도 CartStockInsufficient(None) 로 전파(방어)."""
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    body = {"error": {"code": "CART_STOCK_INSUFFICIENT"}}
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartStockInsufficient) as ei:
        await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=5))
    assert ei.value.available_stock is None


async def test_add_to_cart_validation_error_raises_quantity_exceeded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """400 VALIDATION_ERROR(합산 > 99) → CartQuantityExceeded(CartError 하위) + 드리프트 관측 로그."""
    import logging

    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    body = {
        "error": {"code": "VALIDATION_ERROR", "message": "수량은 최대 99개까지 담을 수 있습니다."}
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with caplog.at_level(logging.WARNING, logger="app.services.spring_client"):
        with pytest.raises(sc.CartQuantityExceeded):
            # 각 요청 수량은 <=99(클라 검증), 합산 초과는 BE가 VALIDATION_ERROR 로 낸다
            await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=5))
    # CartError 하위라 일반 캐치로도 낙성(전용 핸들러 누락 시 CART_ERROR degrade)
    assert issubclass(sc.CartQuantityExceeded, sc.CartError)
    # 드리프트 관측: BE message 를 WARN 으로 남긴다(코드가 다른 사유로 재사용될 때 감지용)
    assert "VALIDATION_ERROR" in caplog.text and "수량은 최대 99개까지" in caplog.text


async def test_get_cart_parses_items(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [
                {
                    "cartItemId": 55,
                    "productId": 1,
                    "productName": "파우치",
                    "optionId": 3,
                    "optionName": "블루",
                    "quantity": 2,
                    "price": 12900,
                }
            ]
        },
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(200, body)))
    view = await sc.get_cart(user_id=1)
    assert len(view.items) == 1
    assert (
        view.items[0].product_name == "파우치"
        and view.items[0].option_name == "블루"
        and view.items[0].quantity == 2
    )


@pytest.mark.parametrize("purchase_state", ["AVAILABLE", "SOLD_OUT", "HIDDEN"])
async def test_get_cart_parses_purchase_state(
    monkeypatch: pytest.MonkeyPatch, purchase_state: str
) -> None:
    """I-18 superset 의 `purchaseState` 를 파싱한다(#310, api-spec §4.9 v0.25.1).

    BE `InternalCartResponse.Item` 에 실재하는 필드인데 종전엔 선언이 없어 `extra="ignore"` 로
    조용히 버려졌다 — 그래서 "구매 불가 상태예요"조차 말하지 못했다."""
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [
                {
                    "cartItemId": 55,
                    "productId": 1,
                    "productName": "파우치",
                    "quantity": 1,
                    "purchaseState": purchase_state,
                }
            ]
        },
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(200, body)))
    view = await sc.get_cart(user_id=1)
    assert view.items[0].purchase_state == purchase_state


async def test_get_cart_missing_purchase_state_key_defaults_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """키가 없으면 `None`(모름) — `"AVAILABLE"` 로 단정하지 않는다(#310, `WishlistItem` 과 대칭).

    BE 배포 시점에 따라 아직 안 내려올 수 있고, 그때 "구매 가능"으로 읽으면 못 사는 상품을
    살 수 있다고 안내하게 된다. 모름은 주장이 아니다."""
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {"items": [{"cartItemId": 55, "productId": 1, "productName": "파우치"}]},
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(200, body)))
    view = await sc.get_cart(user_id=1)
    assert view.items[0].purchase_state is None


async def test_get_cart_unknown_purchase_state_degrades_to_none_keeping_item(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BE 가 계약 밖 상태값을 추가해도 **항목이 사라지지 않고** 그 필드만 `None` 으로 강등된다.

    찜(`_parse_wishlist_items`)은 항목 단위 skip 이지만 장바구니는 같은 처방을 쓰면 안 된다 —
    스킵된 항목이 목록에서 조용히 사라지면 "전부 빼줘"(`cart_remove_all_markers`)가 일부만
    지우고 성공을 보고하고, 수량 합산 안내도 어긋난다. 장바구니 항목은 사용자 소유물이고
    파괴적 후속 동작의 입력이라 **관대 강등**이 맞다. 드리프트 관측용 warning 은 남긴다."""
    import logging

    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [
                {
                    "cartItemId": 55,
                    "productId": 1,
                    "productName": "파우치",
                    "quantity": 1,
                    "purchaseState": "DISCONTINUED",
                },
                {
                    "cartItemId": 56,
                    "productId": 2,
                    "productName": "지갑",
                    "quantity": 1,
                    "purchaseState": "SOLD_OUT",
                },
            ]
        },
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(200, body)))
    with caplog.at_level(logging.WARNING, logger="app.schemas.spring"):
        view = await sc.get_cart(user_id=1)
    assert len(view.items) == 2  # 항목이 사라지지 않는다 — 이것이 이 테스트의 요지
    assert view.items[0].purchase_state is None
    assert view.items[1].purchase_state == "SOLD_OUT"
    assert "DISCONTINUED" in caplog.text


# ─────────── spring_client 배선 (I-24 삭제, 이슈 #116, 🔶 초안) ───────────


async def test_delete_cart_item_success_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    client = _CartClient(_CartResp(200, {"success": True, "data": None}))
    monkeypatch.setattr(sc, "_client", lambda: client)
    result = await sc.delete_cart_item(55, user_id=1)
    assert result is None
    assert client.calls == [("DELETE", "/internal/cart/items/55", {"userId": 1})]


async def test_delete_cart_item_uses_guest_id_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    client = _CartClient(_CartResp(200, {"success": True, "data": None}))
    monkeypatch.setattr(sc, "_client", lambda: client)
    await sc.delete_cart_item(55, guest_id="guest-uuid-1")
    assert client.calls == [("DELETE", "/internal/cart/items/55", {"guestId": "guest-uuid-1"})]


async def test_delete_cart_item_200_success_false_raises_cart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 이지만 공통 봉투 success:false 면 성공으로 처리하지 않는다(2차 리뷰 지적 5)."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc, "_client", lambda: _CartClient(_CartResp(200, {"success": False, "data": None}))
    )
    with pytest.raises(sc.CartError):
        await sc.delete_cart_item(55, user_id=1)


async def test_delete_cart_item_200_missing_success_key_is_not_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """success 키가 없는 것과 명시적 false 는 다른 사실이다 — 없으면 실패로 보지 않는다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(200, {"data": None})))
    result = await sc.delete_cart_item(55, user_id=1)
    assert result is None


async def test_delete_cart_item_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """I-24 는 비멱등 — 이미 지워진 항목을 다시 지워도(두 번째 호출) 404 그대로다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _CartClient(_CartResp(404, {"error": {"code": "CART_ITEM_NOT_FOUND"}})),
    )
    with pytest.raises(sc.CartItemNotFound):
        await sc.delete_cart_item(999, user_id=1)


async def test_delete_cart_item_404_with_wrong_code_raises_cart_error_not_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[라운드 23] Spring 에 엔드포인트가 아직 없어서 나는 404(라우트 없음)도 body 만 보면
    똑같은 404 다 — code 가 계약(`CART_ITEM_NOT_FOUND`)과 다르면 `CartItemNotFound` 로 낙성하지
    않는다. 이걸 성공(`CartItemNotFound` → "이미 빠져 있어요")으로 오인하면 배포 전 호출이
    거짓 성공 안내를 낸다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _CartClient(_CartResp(404, {"error": {"code": "NOT_FOUND"}})),
    )
    with pytest.raises(sc.CartError):
        await sc.delete_cart_item(999, user_id=1)


async def test_delete_cart_item_404_empty_body_raises_cart_error_not_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[라운드 23] 라우트 자체가 없어 본문이 아예 비거나 계약 봉투가 아닌 404(code 를 못
    읽음)도 같은 이유로 `CartError` 여야 한다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(404, {})))
    with pytest.raises(sc.CartError):
        await sc.delete_cart_item(999, user_id=1)


async def test_delete_cart_item_forbidden_maps_to_cart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 AUTH_FORBIDDEN(소유자 불일치) 은 전용 예외 없이 CartError 로 낙성한다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc, "_client", lambda: _CartClient(_CartResp(403, {"error": {"code": "AUTH_FORBIDDEN"}}))
    )
    with pytest.raises(sc.CartError):
        await sc.delete_cart_item(55, user_id=1)


async def test_delete_cart_item_500_maps_to_cart_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc, "_client", lambda: _CartClient(_CartResp(500, {"error": {"code": "INTERNAL_ERROR"}}))
    )
    with pytest.raises(sc.CartError):
        await sc.delete_cart_item(55, user_id=1)


async def test_delete_cart_item_rejects_zero_identity_queries() -> None:
    """신원 query 0개(둘 다 None) — 어댑터가 방어적으로 호출 자체를 막는다."""
    import app.services.spring_client as sc

    with pytest.raises(sc.CartError):
        await sc.delete_cart_item(55)


async def test_delete_cart_item_rejects_two_identity_queries() -> None:
    """신원 query 2개(둘 다 not None) — "정확히 하나" 계약을 어댑터가 방어한다."""
    import app.services.spring_client as sc

    with pytest.raises(sc.CartError):
        await sc.delete_cart_item(55, user_id=1, guest_id="guest-uuid-1")


# ─────────── spring_client 배선 (I-26/I-27/I-28 찜, 이슈 #117, 🔶 초안) 은 tests/unit/test_wishlist.py ───────────


# ─────────── 리뷰 수정 회귀 (Fix 1~4) ───────────


def test_parse_cart_clamps_quantity() -> None:
    """수량 상한(99) 초과 발화가 파싱 시점에 클램프된다(Fix1 — ValidationError 스트림 중단 방지)."""
    from app.agents.buyer.recommendation.decompose import _parse_cart

    assert _parse_cart({"productId": 1, "quantity": 1000}).quantity == 99
    assert _parse_cart({"productId": 1, "quantity": 0}).quantity == 1
    assert _parse_cart({"productId": 1, "quantity": 3}).quantity == 3


async def test_cart_add_rejects_out_of_context_product() -> None:
    """last_reco 밖 productId(LLM 오추출)는 담지 않고 안내 token(Fix4)."""
    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("문맥 밖 상품은 add 호출 금지")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=777, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            allowed_product_ids={101, 102},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    assert "action" not in _types(events)
    assert "어떤 상품" in next(e for e in events if e["type"] == "token")["data"]["text"]


async def test_cart_add_allows_in_context_product() -> None:
    """last_reco 안 productId 는 정상 담기(Fix4 — pending 아닌 신규)."""
    store = CartStateStore()

    async def add_fn(req):
        return AddToCartResult(success=True, cart_item_id=5)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=101, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            allowed_product_ids={101, 102},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    assert next(e for e in events if e["type"] == "action")["data"]["type"] == "CART_ADDED"


async def test_cart_add_invalid_quantity_maps_cart_error() -> None:
    """req 생성이 try 안이라 quantity 스펙 위반도 CART_ERROR 로 degrade(Fix2)."""
    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("검증 실패 시 add 미도달")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1000),  # 클램프 우회(직접 주입)
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADD_FAILED" and action["reason"] == "CART_ERROR"


async def test_last_reco_stored_in_ranked_display_order() -> None:
    """last_reco 는 검색순서가 아니라 노출(rerank) 순서로 저장된다(Codex P1, Fix3)."""
    from app.agents.buyer.cart.state import get_cart_store
    from tests._fakes import DEFAULT_PRODUCTS, FakeLLM

    async def search(filters, exclude_product_ids=None):
        return ProductSearchResult(products=DEFAULT_PRODUCTS, total_count=len(DEFAULT_PRODUCTS))

    async def push(p):
        return True

    # rerank 가 검색순서(101,102,103)와 다르게 재정렬(103 먼저).
    llm = FakeLLM(
        rerank={
            "ranked": [{"productId": 103, "rationale": "a"}, {"productId": 101, "rationale": "b"}],
            "overallComment": "c",
        }
    )
    await _collect(
        run_buyer_turn(
            _req(message="추천", thread_id="tR"), _member(), llm=llm, search=search, push_fn=push
        )
    )
    cart_store = await get_cart_store()
    reco = await cart_store.get_last_reco(await _thread_key(_req(thread_id="tR"), _member()))
    # 노출 순서: rerank [103,101] + expose_min 보충 102 → [103,101,102] (검색순서 아님)
    assert [pid for pid, _ in reco][:2] == [103, 101]


# ─────────── 리뷰 라운드 2 회귀 (R1·R2) ───────────


async def test_last_reco_not_stored_when_push_fails() -> None:
    """push 실패로 카드가 노출되지 않으면 last_reco 를 저장하지 않는다(R1 — 경로 B 불변식)."""
    from app.agents.buyer.cart.state import get_cart_store
    from tests._fakes import DEFAULT_PRODUCTS, FakeLLM

    async def search(filters, exclude_product_ids=None):
        return ProductSearchResult(products=DEFAULT_PRODUCTS, total_count=len(DEFAULT_PRODUCTS))

    async def failing_push(p):
        from app.services.spring_client import SpringUnavailableError

        raise SpringUnavailableError("push down")

    await _collect(
        run_buyer_turn(
            _req(message="추천", thread_id="tNo"),
            _member(),
            llm=FakeLLM(),
            search=search,
            push_fn=failing_push,
        )
    )
    cart_store = await get_cart_store()
    reco = await cart_store.get_last_reco(await _thread_key(_req(thread_id="tNo"), _member()))
    assert reco == []  # 저장 안 됨 → 다음 턴 "그거 담아줘"가 미노출 상품을 담지 못함


async def test_cart_add_option_required_is_uncapped() -> None:
    """api-spec §4.1 — REQUIRED 는 상한 없는 되물음 멀티턴(INVALID 상한과 분리). 반복돼도 재질문."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(
            product_id=1, quantity=1, options=[CartOption(option_id=3, name="블루")], attempts=1
        ),
    )

    async def add_fn(req):
        raise CartOptionRequired([CartOption(option_id=3, name="블루")])

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, option_id=None, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    assert "action" not in _types(events)  # CART_ERROR 아님 — 계속 재질문(§4.1)
    pending = await store.get_pending("m:t")
    assert pending is not None and pending.attempts == 1  # INVALID 카운터 보존(리셋 안 함)


async def test_cart_add_reask_prefers_new_quantity() -> None:
    """옵션 답변과 함께 수량을 다시 말하면("레드로 5개") 새 수량을 우선한다(라운드5)."""
    store = CartStateStore()
    await store.set_pending(
        "m:t", PendingAdd(product_id=1, quantity=1, options=[CartOption(option_id=4, name="레드")])
    )
    captured = {}

    async def add_fn(req):
        captured["quantity"] = req.quantity
        return AddToCartResult(success=True, cart_item_id=1)

    await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, option_id=4, quantity=5),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    assert captured["quantity"] == 5  # pending 의 1 이 아니라 이번 턴 5


async def test_cart_add_reask_ignores_quantity_for_other_target() -> None:
    """전환이 성립 안 한(미추천 상품 언급) 턴의 수량은 옛 pending 상품에 적용하지 않는다(라운드6)."""
    store = CartStateStore()
    await store.set_pending(
        "m:t", PendingAdd(product_id=1, quantity=2, options=[CartOption(option_id=4, name="레드")])
    )
    captured = {}

    async def add_fn(req):
        captured["productId"] = req.product_id
        captured["quantity"] = req.quantity
        return AddToCartResult(success=True, cart_item_id=1)

    # cart.product_id=99(미추천 → allowed 밖, 전환 미성립), quantity=5 는 옛 상품(1)에 적용 금지.
    await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=99, option_id=4, quantity=5),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            allowed_product_ids={1, 2},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    assert captured["productId"] == 1  # 옛 pending 상품
    assert captured["quantity"] == 2  # 이번 턴 5 가 아니라 pending 의 2


# ─────────── 리뷰 라운드 3 회귀 ───────────


def test_parse_cart_coerces_float_and_string() -> None:
    """LLM JSON 변형(float·숫자문자열)도 조용한 폴백 없이 int 로 해석한다."""
    from app.agents.buyer.recommendation.decompose import _parse_cart

    assert _parse_cart({"productId": 101.0, "quantity": 2.0}).product_id == 101
    assert _parse_cart({"productId": 101.0, "quantity": 2.0}).quantity == 2
    assert _parse_cart({"productId": "101", "quantity": "3"}).product_id == 101
    assert _parse_cart({"productId": "101", "quantity": "3"}).quantity == 3
    # bool 은 제외(수량 True 오해석 방지)
    assert _parse_cart({"quantity": True}).quantity == 1


async def test_cart_add_switches_product_during_pending() -> None:
    """되물음 중 다른 추천 상품으로 전환하면 pending 을 버리고 새 상품을 담는다(라운드3)."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(product_id=101, quantity=1, options=[CartOption(option_id=1001, name="일반형")]),
    )
    captured = {}

    async def add_fn(req):
        captured["productId"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=8)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=201, quantity=1),  # 다른 상품으로 전환
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            message="아니 이어폰 담아줘",
            allowed_product_ids={101, 201},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    assert captured["productId"] == 201  # 옛 상품(101) 아닌 새 상품(201)
    assert next(e for e in events if e["type"] == "action")["data"]["type"] == "CART_ADDED"
    assert await store.get_pending("m:t") is None


@pytest.mark.parametrize(
    ("message", "cart"),
    [
        pytest.param(
            "다른 거 담아줘",
            CartIntent(product_id=101, option_id=1002, quantity=1),
            id="fast-echo",
        ),
        pytest.param(
            "이거 말고 다른 거 담아줘",
            CartIntent(product_id=None, option_id=None, quantity=1),
            id="smart-null",
        ),
    ],
)
async def test_cart_add_unresolved_switch_during_pending_does_not_add_old_product(
    message: str, cart: CartIntent
) -> None:
    """전환을 해소 못한 두 티어 출력은 옛 상품·임의 옵션을 쓰지 않고 pending 을 해제한다."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(product_id=101, quantity=1, options=[CartOption(option_id=1001, name="일반형")]),
    )

    async def add_fn(req):
        raise AssertionError(
            f"해소 실패 전환은 담기에 도달하면 안 됨: product={req.product_id}, option={req.option_id}"
        )

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=cart,
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            message=message,
            allowed_product_ids={101, 201},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )

    assert _types(events) == ["token", "done"]
    assert events[0]["data"]["text"] == (
        "어떤 상품을 담을까요? 추천을 먼저 받아보시면 담아드릴게요."
    )
    assert await store.get_pending("m:t") is None


async def test_cart_add_unresolved_switch_rejects_product_outside_allowed_recommendations() -> None:
    """전환 표지가 있는 미추천 productId도 옛 pending 상품·그 턴 옵션으로 담지 않는다."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(product_id=101, quantity=1, options=[CartOption(option_id=1001, name="일반형")]),
    )

    async def add_fn(req):
        raise AssertionError(
            f"해소 실패 전환은 담기에 도달하면 안 됨: product={req.product_id}, option={req.option_id}"
        )

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=999, option_id=1002, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            message="다른 거 담아줘",
            allowed_product_ids={101, 201},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )

    assert _types(events) == ["token", "done"]
    assert events[0]["data"]["text"] == (
        "어떤 상품을 담을까요? 추천을 먼저 받아보시면 담아드릴게요."
    )
    assert await store.get_pending("m:t") is None


@pytest.mark.parametrize("message", ["일반형", "드럼형으로", "2번으로"])
async def test_cart_add_option_answer_during_pending_still_adds_pending_product(
    message: str,
) -> None:
    """전환 표지가 없는 옵션 답변은 productId=null 이어도 기존 pending 상품에 정상 적용한다."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(product_id=101, quantity=1, options=[CartOption(option_id=1001, name="일반형")]),
    )
    captured = {}

    async def add_fn(req):
        captured["productId"] = req.product_id
        captured["optionId"] = req.option_id
        return AddToCartResult(success=True, cart_item_id=8)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=None, option_id=1001, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            message=message,
            allowed_product_ids={101, 201},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )

    assert captured == {"productId": 101, "optionId": 1001}
    assert next(e for e in events if e["type"] == "action")["data"]["type"] == "CART_ADDED"
    assert await store.get_pending("m:t") is None


async def test_cart_add_discourse_interjection_before_number_option_still_adds() -> None:
    """기본 전환 마커가 아닌 '아니' 뒤 번호 옵션 답변은 pending 상품에 정상 적용한다."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(
            product_id=101,
            quantity=1,
            options=[
                CartOption(option_id=1001, name="일반형"),
                CartOption(option_id=1002, name="드럼형"),
            ],
        ),
    )
    captured = {}

    async def add_fn(req):
        captured["productId"] = req.product_id
        captured["optionId"] = req.option_id
        captured["pendingKeptUntilAdd"] = await store.get_pending("m:t") is not None
        return AddToCartResult(success=True, cart_item_id=8)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=101, option_id=1002, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            message="아니 2번이요",
            allowed_product_ids={101, 201},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )

    assert captured == {
        "productId": 101,
        "optionId": 1002,
        "pendingKeptUntilAdd": True,
    }
    assert _types(events) == ["action", "done"]
    assert await store.get_pending("m:t") is None


@pytest.mark.parametrize(
    ("message", "option"),
    [
        pytest.param("아니 파란색이요", CartOption(option_id=1001, name="파란색"), id="blue"),
        pytest.param("아니 그냥 레드로 주세요", CartOption(option_id=1002, name="레드"), id="red"),
    ],
)
async def test_cart_add_option_correction_interjection_still_uses_named_pending_option(
    message: str, option: CartOption
) -> None:
    """기본 전환 마커가 아닌 '아니' 뒤 옵션명 정정은 정상 옵션 답변으로 처리한다."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(
            product_id=101,
            quantity=1,
            options=[
                CartOption(option_id=1001, name="파란색"),
                CartOption(option_id=1002, name="레드"),
            ],
        ),
    )
    captured = {}

    async def add_fn(req):
        captured["productId"] = req.product_id
        captured["optionId"] = req.option_id
        captured["pendingKeptUntilAdd"] = await store.get_pending("m:t") is not None
        return AddToCartResult(success=True, cart_item_id=8)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=101, option_id=option.option_id, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            message=message,
            allowed_product_ids={101, 201},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )

    assert captured == {
        "productId": 101,
        "optionId": option.option_id,
        "pendingKeptUntilAdd": True,
    }
    assert _types(events) == ["action", "done"]
    assert await store.get_pending("m:t") is None


async def test_cart_add_switch_marker_substring_does_not_count_as_pending_option() -> None:
    """옵션명 '대'가 전환 마커 '대신' 안에만 있어도 실제 옵션 답변으로 오인하지 않는다."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(
            product_id=101,
            quantity=1,
            options=[
                CartOption(option_id=1001, name="대"),
                CartOption(option_id=1002, name="중"),
                CartOption(option_id=1003, name="소"),
            ],
        ),
    )

    async def add_fn(req):
        raise AssertionError(f"마커 안 옵션명은 옛 상품 담기에 쓰면 안 됨: {req}")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=101, option_id=1001, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            message="대신 다른 상품 담아줘",
            allowed_product_ids={101, 201},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )

    assert _types(events) == ["token", "done"]
    assert events[0]["data"]["text"] == (
        "어떤 상품을 담을까요? 추천을 먼저 받아보시면 담아드릴게요."
    )
    assert await store.get_pending("m:t") is None


async def test_cart_add_short_option_after_removed_interjection_still_counts_as_answer() -> None:
    """기본 마커에서 빠진 '아니' 뒤 짧은 옵션명 '대'도 정상 옵션 답변으로 담는다."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(
            product_id=101,
            quantity=1,
            options=[
                CartOption(option_id=1001, name="대"),
                CartOption(option_id=1002, name="중"),
                CartOption(option_id=1003, name="소"),
            ],
        ),
    )
    captured = {}

    async def add_fn(req):
        captured["productId"] = req.product_id
        captured["optionId"] = req.option_id
        captured["pendingKeptUntilAdd"] = await store.get_pending("m:t") is not None
        return AddToCartResult(success=True, cart_item_id=8)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=101, option_id=1001, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            message="아니 대로 주세요",
            allowed_product_ids={101, 201},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )

    assert captured == {
        "productId": 101,
        "optionId": 1001,
        "pendingKeptUntilAdd": True,
    }
    assert _types(events) == ["action", "done"]
    assert await store.get_pending("m:t") is None


async def test_cart_add_marker_substring_inside_option_name_still_counts_as_option_answer() -> None:
    """전환 마커 '말고'가 옵션명 '말고기' 안에만 있으면 정상 옵션 답변으로 담는다."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(
            product_id=101,
            quantity=1,
            options=[
                CartOption(option_id=1001, name="말고기"),
                CartOption(option_id=1002, name="소고기"),
            ],
        ),
    )
    captured = {}

    async def add_fn(req):
        captured["productId"] = req.product_id
        captured["optionId"] = req.option_id
        captured["pendingKeptUntilAdd"] = await store.get_pending("m:t") is not None
        return AddToCartResult(success=True, cart_item_id=8)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=101, option_id=1001, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            message="말고기로 주세요",
            allowed_product_ids={101, 201},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )

    assert captured == {
        "productId": 101,
        "optionId": 1001,
        "pendingKeptUntilAdd": True,
    }
    assert _types(events) == ["action", "done"]
    assert await store.get_pending("m:t") is None


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(
            [CartOption(option_id=1001, name=""), CartOption(option_id=1002, name="")],
            id="all-empty",
        ),
        pytest.param(
            [CartOption(option_id=1001, name=""), CartOption(option_id=1002, name="드럼형")],
            id="partly-empty",
        ),
    ],
)
async def test_cart_add_unnamed_pending_option_skips_switch_heuristic(
    options: list[CartOption],
) -> None:
    """옵션명 하나라도 비면 보수적으로 휴리스틱을 끈다 — 이 구성에서는 #253 보호가 적용되지 않는다."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(
            product_id=101,
            quantity=1,
            options=options,
        ),
    )
    captured = {}

    async def add_fn(req):
        captured["productId"] = req.product_id
        captured["optionId"] = req.option_id
        captured["pendingKeptUntilAdd"] = await store.get_pending("m:t") is not None
        return AddToCartResult(success=True, cart_item_id=8)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=101, option_id=1001, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            message="다른 거 담아줘",
            allowed_product_ids={101, 201},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )

    assert captured == {
        "productId": 101,
        "optionId": 1001,
        "pendingKeptUntilAdd": True,
    }
    assert _types(events) == ["action", "done"]
    assert await store.get_pending("m:t") is None


async def test_cart_add_other_color_during_pending_is_documented_safe_false_positive() -> None:
    """알려진 한계: '다른 색'도 상품 전환으로 감지되지만 오담기 없이 해제 후 되묻는다."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(product_id=101, quantity=1, options=[CartOption(option_id=1001, name="일반형")]),
    )

    async def add_fn(req):
        raise AssertionError(f"안전한 오탐은 담기에 도달하면 안 됨: {req}")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=101, option_id=1002, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            message="다른 색으로 해줘",
            allowed_product_ids={101, 201},
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )

    assert _types(events) == ["token", "done"]
    assert await store.get_pending("m:t") is None


# ─────────── 리뷰 라운드 4 회귀 ───────────


def test_cart_identity_non_numeric_sub_is_anon() -> None:
    """비숫자 sub(dev 미검증 토큰)는 익명 취급 — int 변환 실패로 죽지 않는다."""
    from app.agents.buyer.cart.graph import cart_identity

    assert cart_identity(_member()) == (123, None)
    assert cart_identity(_guest()) == (None, "guest-uuid-1")
    bad = Identity(user_id="abc", is_guest=False, seller_id=None, subject="abc")
    assert cart_identity(bad) == (None, None)


async def test_cart_add_non_numeric_member_maps_cart_error() -> None:
    """비숫자 user_id 회원은 예외로 죽지 않고 CART_ERROR 로 낙성한다."""
    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("익명 취급 → add 미도달")

    bad = Identity(user_id="abc", is_guest=False, seller_id=None, subject="abc")
    events = await _collect(
        stream_cart_add(
            identity=bad,
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="b:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADD_FAILED" and action["reason"] == "CART_ERROR"


async def test_general_intent_clears_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """되물음 중 취소(general 전환)하면 stale pending 이 정리된다(라운드4)."""
    from app.agents.buyer.cart.state import PendingAdd, get_cart_store
    from tests._fakes import FakeLLM

    key = await _thread_key(_req(), _member())
    cart_store = await get_cart_store()
    await cart_store.set_pending(
        key, PendingAdd(product_id=1, quantity=1, options=[CartOption(option_id=3, name="블루")])
    )
    llm = FakeLLM(decompose={"intent": "general", "reply": "네, 취소할게요."})
    await _collect(run_buyer_turn(_req(message="그만할래"), _member(), llm=llm))
    assert await cart_store.get_pending(key) is None  # 정리됨


def test_local_recommendation_cache_pop_removes_only_requested_key() -> None:
    from app.core.pg_resilience import BoundedLRUCache

    cache = BoundedLRUCache[str, str](max_entries=2)
    cache["context-a:thread"] = "a"
    cache["context-b:thread"] = "b"

    assert cache.pop("context-a:thread") == "a"
    assert cache.pop("missing") is None
    assert cache.get("context-b:thread") == "b"


# ─────────── #18 리뷰 수정 회귀 ───────────


async def test_cart_add_reask_shows_option_surcharge() -> None:
    """되물음 문구에 옵션 추가금(extraPrice)을 표시한다(Codex #18)."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartOptionRequired(
            [
                CartOption(option_id=3, name="블루", extra_price=0),
                CartOption(option_id=4, name="레드", extra_price=1000),
            ]
        )

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "레드(+1,000원)" in token and "블루" in token


async def test_add_to_cart_empty_detail_options_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """detail.options 가 빈 배열이면 구버전 위치의 잔재 options 로 폴백하지 않는다(Claude #18)."""
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    body = {
        "error": {
            "code": "CART_OPTION_REQUIRED",
            "detail": {"options": []},
            "options": [{"optionId": 99, "name": "stale"}],
        }
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartOptionRequired) as ei:
        await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=1))
    assert ei.value.options == []  # 빈 배열 신뢰 — 99(잔재) 안 고름


async def test_add_to_cart_malformed_option_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """형식 이상 옵션 항목은 건너뛰고 정상 항목만 파싱한다(되물음 흐름 보호, Claude #18)."""
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    body = {
        "error": {
            "code": "CART_OPTION_REQUIRED",
            "detail": {
                "options": [
                    {"optionId": "not-int", "name": "깨짐"},  # optionId 변환 불가 → 건너뜀
                    {"optionId": 3, "name": "블루", "extraPrice": 0},  # 정상
                ]
            },
        }
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartOptionRequired) as ei:
        await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=1))
    assert [o.option_id for o in ei.value.options] == [3]  # 깨진 항목 제외, 정상만


async def test_cart_add_reask_formats_surcharge_by_sign() -> None:
    """추가금은 부호별로: 양수=+, 음수=할인(-), 0/None=미표시('(+-)' 깨짐 없이, Claude #18)."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartOptionRequired(
            [
                CartOption(option_id=4, name="레드", extra_price=-1000),  # 할인
                CartOption(option_id=5, name="블랙", extra_price=0),  # 추가금 없음
                CartOption(option_id=6, name="화이트", extra_price=2000),  # 추가금
            ]
        )

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
        )
    )
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "화이트(+2,000원)" in token  # 양수 추가금만 표시
    assert "레드" in token and "레드(" not in token  # 음수(계약 미정의) 미표시
    assert "블랙" in token and "블랙(" not in token  # 0 미표시
    assert "+-" not in token and "-1,000" not in token


def test_parse_cart_error_logs_when_all_options_dropped(caplog: pytest.LogCaptureFixture) -> None:
    """옵션이 전부 파싱 실패하면 계약 위반 신호로 경고 로그를 남긴다(Claude #18)."""
    import logging
    import app.services.spring_client as sc

    class _R:
        status_code = 400

        def json(self):
            return {
                "error": {
                    "code": "CART_OPTION_REQUIRED",
                    "detail": {"options": [{"optionId": "bad"}]},
                }
            }

    with caplog.at_level(logging.WARNING, logger="app.services.spring_client"):
        code, options, _stock = sc._parse_cart_error(_R())
    assert options == [] and code == "CART_OPTION_REQUIRED"
    assert any("전부 파싱 실패" in r.getMessage() for r in caplog.records)


async def test_add_to_cart_bad_extra_price_keeps_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """extraPrice(표시용)가 이상해도 옵션 자체는 버리지 않는다(extra_price=None, Claude #18)."""
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    body = {
        "error": {
            "code": "CART_OPTION_REQUIRED",
            "detail": {
                "options": [
                    {
                        "optionId": 3,
                        "name": "블루",
                        "extraPrice": "weird",
                    },  # extraPrice 이상 → None 으로, 옵션 유지
                ]
            },
        }
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartOptionRequired) as ei:
        await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=1))
    assert [o.option_id for o in ei.value.options] == [3]
    assert ei.value.options[0].extra_price is None


async def test_add_to_cart_float_extra_price_coerced(monkeypatch: pytest.MonkeyPatch) -> None:
    """BE 가 정수 금액을 float(1500.0)로 내려도 int 로 수용한다(Claude #18)."""
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    body = {
        "error": {
            "code": "CART_OPTION_REQUIRED",
            "detail": {
                "options": [
                    {"optionId": 7, "name": "골드", "extraPrice": 1500.0},
                    {
                        "optionId": 8,
                        "name": "실버",
                        "extraPrice": 999.9999999998,
                    },  # BigDecimal.doubleValue 오차
                ]
            },
        }
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartOptionRequired) as ei:
        await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=1))
    assert ei.value.options[0].extra_price == 1500
    assert ei.value.options[1].extra_price == 1000  # 반올림


async def test_add_to_cart_naninf_extra_price_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """NaN/Infinity extraPrice 여도 스트림이 죽지 않고 옵션은 유지된다(extra_price None, Claude #18)."""
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest

    body = {
        "error": {
            "code": "CART_OPTION_REQUIRED",
            "detail": {
                "options": [
                    {"optionId": 9, "name": "네온", "extraPrice": float("nan")},
                    {"optionId": 10, "name": "무한", "extraPrice": float("inf")},
                    {
                        "optionId": 11,
                        "name": "초대형",
                        "extraPrice": 10**400,
                    },  # float 변환 OverflowError
                ]
            },
        }
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartOptionRequired) as ei:
        await sc.add_to_cart(AddToCartRequest(user_id=1, product_id=1, quantity=1))
    assert [o.option_id for o in ei.value.options] == [9, 10, 11]
    assert all(o.extra_price is None for o in ei.value.options)


async def test_cart_state_store_all_operations_have_query_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """buyer cart BaseStore I/O 전 구간에 pg-profile query deadline을 적용한다."""

    class _HangStore:
        async def aget(self, *args, **kwargs):
            await asyncio.sleep(10)

        async def aput(self, *args, **kwargs):
            await asyncio.sleep(10)

        async def adelete(self, *args, **kwargs):
            await asyncio.sleep(10)

    monkeypatch.setattr(get_settings(), "state_store_query_timeout_s", 0.01)
    store = CartStateStore(_HangStore())
    pending = PendingAdd(product_id=1, quantity=1)
    operations = [
        lambda: store.set_last_reco("k", [(1, "상품")]),
        lambda: store.get_last_reco("k"),
        lambda: store.set_pending("k", pending),
        lambda: store.get_pending("k"),
        lambda: store.clear_pending("k"),
    ]
    for operation in operations:
        with pytest.raises(TimeoutError):
            await operation()


# ─────────── 유일 옵션 자동 선택 (이슈 #114) ───────────


async def _run_add(store, cart, add_fn, *, get_cart_fn=None, thread_key="m:t"):
    """자동 선택 테스트 공용 구동 — 담기 스트림 이벤트 목록을 돌려준다."""
    return await _collect(
        stream_cart_add(
            identity=_member(),
            cart=cart,
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=get_cart_fn or _empty_cart(),
        )
    )


async def test_cart_add_single_option_autoselected() -> None:
    """옵션 후보가 1개뿐이면 되묻지 않고 그 optionId 로 즉시 재담기한다(#114)."""
    store = CartStateStore()
    calls: list[int | None] = []

    async def add_fn(req):
        calls.append(req.option_id)
        if req.option_id is None:
            raise CartOptionRequired([CartOption(option_id=7, name="단일 사이즈")])
        return AddToCartResult(success=True, cart_item_id=70)

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    assert calls == [None, 7]  # 되물음 없이 유일 옵션으로 재호출
    assert "token" not in _types(events)  # 되묻지 않는다
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED" and action["cartItemId"] == 70
    assert "단일 사이즈 옵션으로" in action["message"]  # 대신 고른 옵션을 밝힌다
    assert await store.get_pending("m:t") is None


async def test_cart_add_autoselect_message_strips_seller_text() -> None:
    """자동 선택 안내에 실리는 옵션명(판매자 입력)도 위험 문자를 제거한다(#114)."""
    store = CartStateStore()

    async def add_fn(req):
        if req.option_id is None:
            raise CartOptionRequired([CartOption(option_id=7, name="블\x1b[31m랙​")])
        return AddToCartResult(success=True, cart_item_id=71)

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    message = next(e for e in events if e["type"] == "action")["data"]["message"]
    assert "블[31m랙 옵션으로" in message
    assert all(ch not in message for ch in ("\x1b", "​"))


async def test_cart_add_autoselect_message_shows_surcharge() -> None:
    """AI 가 대신 고른 옵션에 추가금이 있으면 안내에 밝힌다 — 되물음 문구와 같은 규칙(#114 PR 리뷰).

    자동 선택은 사용자가 고를 기회 자체가 없으므로, 추가금을 숨기면 결제 단계에서야 알게 된다.
    """
    store = CartStateStore()

    async def add_fn(req):
        if req.option_id is None:
            raise CartOptionRequired([CartOption(option_id=7, name="블랙", extra_price=2000)])
        return AddToCartResult(success=True, cart_item_id=74)

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    message = next(e for e in events if e["type"] == "action")["data"]["message"]
    assert message == "블랙(+2,000원) 옵션으로 담았어요."


async def test_cart_add_autoselect_message_hides_nonpositive_surcharge() -> None:
    """추가금 0·음수(계약 미정의)는 자동 선택 안내에서도 표시하지 않는다(#114 PR 리뷰)."""
    store = CartStateStore()

    async def add_fn(req):
        if req.option_id is None:
            raise CartOptionRequired([CartOption(option_id=7, name="블랙", extra_price=-1000)])
        return AddToCartResult(success=True, cart_item_id=75)

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    message = next(e for e in events if e["type"] == "action")["data"]["message"]
    assert message == "블랙 옵션으로 담았어요."
    assert "+-" not in message and "1,000" not in message


async def test_cart_add_autoselect_keeps_merge_notice() -> None:
    """자동 선택으로 담아도 기존 보유가 있으면 합산 안내를 유지한다(#114)."""
    store = CartStateStore()

    async def add_fn(req):
        if req.option_id is None:
            raise CartOptionRequired([CartOption(option_id=7, name="블랙")])
        return AddToCartResult(success=True, cart_item_id=72)

    async def get_cart_fn(*, user_id=None, guest_id=None):
        return CartView(items=[CartViewItem(cart_item_id=9, product_id=1, option_id=7, quantity=2)])

    events = await _run_add(
        store, CartIntent(product_id=1, quantity=1), add_fn, get_cart_fn=get_cart_fn
    )

    message = next(e for e in events if e["type"] == "action")["data"]["message"]
    assert "블랙 옵션으로" in message and "더했" in message


async def test_cart_add_autoselect_merge_notice_ignores_other_option() -> None:
    """자동 선택으로 담길 옵션과 다른 옵션의 보유는 합산 안내 근거가 아니다(#114 PR 리뷰).

    담기 전 조회는 optionId 미상이라 그 상품의 모든 항목을 센다. 지금 후보가 1개라는 사실이
    기존 항목도 그 옵션이라는 뜻은 아니다(단종·품절로 후보에서 빠진 옛 옵션) — Spring 은 새 줄로
    담는데 "수량을 더했어요"라고 말하면 안내가 실제 결과와 어긋난다(REQ-CART-031).
    """
    store = CartStateStore()

    async def add_fn(req):
        if req.option_id is None:
            raise CartOptionRequired([CartOption(option_id=7, name="블랙")])
        return AddToCartResult(success=True, cart_item_id=73)

    async def get_cart_fn(*, user_id=None, guest_id=None):
        # 후보에 없는 옛 옵션(3)으로 담아둔 항목 — 자동 선택될 7 과는 다른 줄이다.
        return CartView(items=[CartViewItem(cart_item_id=9, product_id=1, option_id=3, quantity=2)])

    events = await _run_add(
        store, CartIntent(product_id=1, quantity=1), add_fn, get_cart_fn=get_cart_fn
    )

    message = next(e for e in events if e["type"] == "action")["data"]["message"]
    assert message == "블랙 옵션으로 담았어요."
    assert "더했" not in message


async def test_cart_add_multiple_options_still_reasks() -> None:
    """옵션이 2개 이상이면 자동 선택하지 않고 기존 되물음 멀티턴을 유지한다(#114 회귀)."""
    store = CartStateStore()
    calls: list[int | None] = []

    async def add_fn(req):
        calls.append(req.option_id)
        raise CartOptionRequired(
            [CartOption(option_id=3, name="블루"), CartOption(option_id=4, name="레드")]
        )

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    assert calls == [None]  # 임의 선택 금지 — 재호출하지 않는다
    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "블루" in token and "레드" in token
    assert (await store.get_pending("m:t")) is not None


async def test_cart_add_autoselect_retries_only_once() -> None:
    """자동 선택한 옵션에도 REQUIRED 가 또 오면 재시도를 멈추고 되물음으로 degrade 한다(#114)."""
    store = CartStateStore()
    calls: list[int | None] = []

    async def add_fn(req):
        calls.append(req.option_id)
        raise CartOptionRequired([CartOption(option_id=7, name="블랙")])

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    assert calls == [None, 7]  # 무한 재시도 금지 — 자동 선택은 1회
    assert "action" not in _types(events)
    assert "블랙" in next(e for e in events if e["type"] == "token")["data"]["text"]
    assert (await store.get_pending("m:t")) is not None


async def test_cart_add_autoselect_skipped_when_same_option_sent() -> None:
    """이미 보낸 optionId 와 유일 후보가 같으면 같은 요청을 되풀이하지 않는다(#114)."""
    store = CartStateStore()
    calls: list[int | None] = []

    async def add_fn(req):
        calls.append(req.option_id)
        raise CartOptionRequired([CartOption(option_id=7, name="블랙")])

    events = await _run_add(store, CartIntent(product_id=1, option_id=7, quantity=1), add_fn)

    assert calls == [7]  # 동일 요청 재호출 없음
    assert "action" not in _types(events) and "token" in _types(events)


async def test_cart_add_autoselect_failure_maps_to_action() -> None:
    """자동 선택 재담기가 실패하면 기존 오류 매핑(재고 부족 등)을 그대로 탄다(#114)."""
    store = CartStateStore()

    async def add_fn(req):
        if req.option_id is None:
            raise CartOptionRequired([CartOption(option_id=7, name="블랙")])
        raise CartStockInsufficient(available_stock=2)

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADD_FAILED" and action["reason"] == "STOCK_INSUFFICIENT"
    assert "2개뿐" in action["message"]
    assert await store.get_pending("m:t") is None


async def test_cart_add_autoselect_invalid_falls_back_to_reask() -> None:
    """자동 선택한 옵션이 INVALID 면 기존 상한 있는 되물음 재시도로 이어진다(#114)."""
    store = CartStateStore()

    async def add_fn(req):
        if req.option_id is None:
            raise CartOptionRequired([CartOption(option_id=7, name="블랙")])
        raise CartOptionInvalid([CartOption(option_id=8, name="화이트")])

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    assert "action" not in _types(events)  # 상한(기본 1) 내 → 재질문
    pending = await store.get_pending("m:t")
    assert pending is not None and pending.attempts == 1


async def test_last_reco_name_cache_is_bounded_lru(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.buyer.cart import state as cart_state

    monkeypatch.setattr(get_settings(), "state_store_local_cache_max_entries", 2)
    cart_state.reset_cart_store()
    store = CartStateStore()

    await store.set_last_reco("a", [(1, "A")])
    await store.set_last_reco("b", [(2, "B")])
    assert await store.get_last_reco("a") == [(1, "A")]  # a를 MRU로 승격
    await store.set_last_reco("c", [(3, "C")])

    assert await store.get_last_reco("b") == [(2, "")]  # id는 영속, 이름만 LRU miss degrade
    assert len(cart_state._last_reco_names) == 2


# ─────────── #118 last_reco 누적화 (담기 가드의 시간 축) ───────────


async def test_last_reco_accumulates_across_turns_in_most_recent_order() -> None:
    """[#118] 새 추천이 옛 추천을 **덮지 않는다** — 정본 §3.1 담기 가드가 "누적 추천 목록"이다.

    덮어쓰기였을 때 깨지던 시나리오: 추천 A(101·102·103) → "101 방수야?" → 추천 B(301·302)
    → "이거 담아줘". 3단계에서 101 이 사라져 담기가 차단됐다.
    """
    from app.agents.buyer.cart import state as cart_state

    cart_state.reset_cart_store()
    store = CartStateStore()
    await store.set_last_reco("k", [(101, "이어폰"), (102, "케이스"), (103, "충전기")])
    await store.set_last_reco("k", [(301, "니트"), (302, "코트")])

    reco = await store.get_last_reco("k")
    # 최근 언급 순 — 이번 턴이 앞, 그다음이 직전 턴.
    assert [pid for pid, _ in reco] == [301, 302, 101, 102, 103]
    # 이름 캐시도 병합돼야 한다(통째 교체면 승계분 이름이 사라진다).
    assert dict(reco)[101] == "이어폰"
    assert dict(reco)[301] == "니트"


async def test_last_reco_promotes_repeated_product_without_duplicating() -> None:
    from app.agents.buyer.cart import state as cart_state

    cart_state.reset_cart_store()
    store = CartStateStore()
    await store.set_last_reco("k", [(101, "이어폰"), (102, "케이스")])
    await store.set_last_reco("k", [(102, "케이스"), (301, "니트")])

    assert [pid for pid, _ in await store.get_last_reco("k")] == [102, 301, 101]


async def test_last_reco_cap_never_truncates_the_current_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[HARD] I-21 은 한 턴에 최대 10목록 × 9상품 = 90건을 민다.

    단순 `merged[:cap]` 이면 **방금 추천한 상품이 담기 차단되는 회귀**가 된다 — 상한은
    승계분에만 실효적으로 걸려야 한다.
    """
    from app.agents.buyer.cart import state as cart_state

    monkeypatch.setattr(get_settings(), "last_reco_max", 5)
    cart_state.reset_cart_store()
    store = CartStateStore()
    await store.set_last_reco("k", [(pid, f"승계{pid}") for pid in range(1, 4)])

    this_turn = [(1000 + i, f"신규{i}") for i in range(90)]
    await store.set_last_reco("k", this_turn)

    reco = await store.get_last_reco("k")
    assert reco[:90] == this_turn  # 이번 턴 90건이 하나도 잘리지 않았다
    assert len(reco) == 90  # 상한을 넘긴 승계분만 잘렸다


async def test_last_reco_cap_applies_to_carried_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """무한 증가 방지 — 이번 턴이 작으면 상한이 승계분을 실제로 자른다."""
    from app.agents.buyer.cart import state as cart_state

    monkeypatch.setattr(get_settings(), "last_reco_max", 4)
    cart_state.reset_cart_store()
    store = CartStateStore()
    await store.set_last_reco("k", [(pid, f"옛{pid}") for pid in (1, 2, 3, 4, 5)])
    await store.set_last_reco("k", [(90, "새1"), (91, "새2")])

    reco = await store.get_last_reco("k")
    assert [pid for pid, _ in reco] == [90, 91, 1, 2]
    # 잘려나간 id 의 이름은 캐시에서도 정리(prune)돼야 한다 — 스레드당 dict 가 무한히 자라지 않게.
    assert set(cart_state._last_reco_names.get("k", {})) == {90, 91, 1, 2}


async def test_last_reco_merge_does_not_erase_a_cached_name_with_a_blank_one() -> None:
    """push 가 이름을 못 실어 온 상품이 캐시의 멀쩡한 이름을 지우면 이름 지목이 이유 없이 나빠진다."""
    from app.agents.buyer.cart import state as cart_state

    cart_state.reset_cart_store()
    store = CartStateStore()
    await store.set_last_reco("k", [(101, "파란 니트")])
    await store.set_last_reco("k", [(101, "")])

    assert await store.get_last_reco("k") == [(101, "파란 니트")]


async def test_cart_add_allows_a_product_recommended_two_turns_ago(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """누적화의 목적 — 추천 A → 질문 → 추천 B 뒤에도 A 의 상품을 담을 수 있어야 한다.

    가드(`allowed`)를 실제로 태우는 `run_buyer_turn` 진입점으로 검증한다. 스토어 단언만으로는
    "그래서 담기가 되나"를 지나가지 않는다.
    """
    import app.services.spring_client as sc
    from app.agents.buyer.cart.state import get_cart_store
    from tests._fakes import FakeLLM

    async def fake_add(req):  # noqa: ANN001
        return AddToCartResult(success=True, cart_item_id=77)

    async def fake_get(*, user_id=None, guest_id=None):  # noqa: ANN001
        return CartView(items=[])

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", fake_get)

    request = _req(thread_id="t-accumulate", message="101 담아줘")
    key = await _thread_key(request, _member())
    store = await get_cart_store()
    await store.set_last_reco(key, [(101, "이어폰"), (102, "케이스")])  # 추천 A
    await store.set_last_reco(key, [(301, "니트"), (302, "코트")])  # 추천 B

    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 101, "quantity": 1}})
    events = await _collect(run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert action["cartItemId"] == 77


class _PromptCapturingLLM:
    """decompose 프롬프트를 붙잡아 두는 FakeLLM — LAST_RECOMMENDATIONS 내용을 단언한다."""

    def __init__(self, decompose: dict) -> None:
        import json as _json

        self._raw = _json.dumps({"intent": "cart_add", "filters": {}, **decompose})
        self.user = ""
        self.system = ""

    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):  # noqa: ANN001
        self.user, self.system = user, system
        return self._raw

    async def stream(self, *, system, user, tier, max_tokens=1024):  # noqa: ANN001
        yield "x"


async def test_pending_turn_prompt_excludes_carried_recommendations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#118] 옵션 되물음 중에는 프롬프트에 **승계분을 싣지 않는다** — 가드는 그대로 누적을 쓴다.

    실 LLM N=8 프로브에서 "PENDING_CART 중 상품 전환"(`이어폰으로 할래`)이
    승계 없음 6/8 · 승계 없음+screen 7/8 · 승계 11건 **1/8** · 승계 상한 6건 **2/8** 이었다.
    승계분이 2건만 붙어도 #240 이 "낮추지 말 것"으로 못박은 경로가 무너진다.
    """
    from app.agents.buyer.cart.state import get_cart_store

    request = _req(thread_id="t-pending-prompt", message="이어폰으로 할래")
    key = await _thread_key(request, _member())
    store = await get_cart_store()
    await store.set_last_reco(key, [(9001, "승계1"), (9002, "승계2")])  # 옛 턴
    await store.set_last_reco(key, [(101, "세탁 세제"), (201, "무선 이어폰")])  # 이번 턴
    await store.set_pending(
        key,
        PendingAdd(product_id=101, quantity=1, options=[CartOption(option_id=1001, name="일반형")]),
    )

    llm = _PromptCapturingLLM({"cart": {"productId": 201, "quantity": 1}})
    await _collect(run_buyer_turn(request, _member(), llm=llm))

    reco_line = next(
        line for line in llm.user.splitlines() if line.startswith("LAST_RECOMMENDATIONS:")
    )
    assert "101" in reco_line and "201" in reco_line  # 이번 턴은 그대로
    assert "9001" not in reco_line and "9002" not in reco_line  # 승계분은 빠진다
    # 가드는 여전히 누적 전체다 — 프롬프트에서 뺀 것이 allowed 를 좁히지 않는다.
    assert {pid for pid, _ in await store.get_last_reco(key)} >= {9001, 9002, 101, 201}


async def test_non_pending_turn_prompt_carries_the_accumulated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """되물음이 아닌 턴에서는 누적 전체를 싣는다 — #118 의 4단계 시나리오 해소가 여기서 일어난다."""
    from app.agents.buyer.cart.state import get_cart_store

    request = _req(thread_id="t-nonpending-prompt", message="이거 담아줘")
    key = await _thread_key(request, _member())
    store = await get_cart_store()
    await store.set_last_reco(key, [(9001, "추천A-1"), (9002, "추천A-2")])
    await store.set_last_reco(key, [(301, "추천B-1")])

    llm = _PromptCapturingLLM({"cart": {"productId": 9001, "quantity": 1}})
    await _collect(run_buyer_turn(request, _member(), llm=llm))

    reco_line = next(
        line for line in llm.user.splitlines() if line.startswith("LAST_RECOMMENDATIONS:")
    )
    assert "9001" in reco_line and "301" in reco_line


async def test_missing_turn_count_degrades_to_todays_behaviour() -> None:
    """구버전 인스턴스가 쓴 값(`turn_count` 없음)을 읽어도 KeyError 없이 오늘 동작으로 degrade 한다.

    롤링 배포 중 신구 혼재에서 담기 턴이 죽으면 안 된다 — 경계를 모르면 **전량을 이번 턴**으로 본다.
    """
    from app.agents.buyer.cart import state as cart_state
    from app.agents.buyer.cart.state import CartStateStore

    cart_state.reset_cart_store()
    store = CartStateStore()
    # 구버전 인스턴스의 쓰기를 그대로 재현한다(product_ids 만 있는 값).
    await store._store.aput(("buyer_cart_v2", "k"), "last_reco", {"product_ids": [1, 2, 3]})

    state = await store.get_last_reco_state("k")
    assert [pid for pid, _ in state.items] == [1, 2, 3]
    assert state.turn_count == 3  # 경계 불명 → 전량을 이번 턴으로


def _screen_request(message: str, thread_id: str):
    """screen 이 실린 실제 요청 — 관대 정규화를 그대로 태운다."""
    from app.schemas.chat import BuyerChatRequest

    return BuyerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": thread_id,
            "message": message,
            "screen": {
                "pageType": "chat",
                "columns": 2,
                "products": [
                    {"productId": 501, "name": "러그"},
                    {"productId": 502, "name": "바구니"},
                ],
            },
        }
    )


async def _seed_pending(key: str) -> None:
    from app.agents.buyer.cart.state import get_cart_store

    store = await get_cart_store()
    await store.set_last_reco(key, [(9001, "드럼용 세탁 세제")])
    await store.set_pending(
        key,
        PendingAdd(
            product_id=9001,
            quantity=1,
            options=[
                CartOption(option_id=1001, name="일반형"),
                CartOption(option_id=1002, name="드럼형"),
            ],
        ),
    )


async def test_pending_turn_prompt_excludes_the_screen_block_and_rule() -> None:
    """[#118 · PR 4차 리뷰] 되물음 턴에는 **screen 도** 프롬프트에서 뺀다 — `prompt_reco` 와 같은 규약.

    싣고 있었을 때 한 턴에 "options 의 번호로 골라라"(PENDING_CART)와 "화면 순번으로 골라라"
    (SCREEN.상품 + 규칙)가 동시에 주어졌다. `"2번으로"` 가 화면 순번 2로 오인되면 채워지는
    productId 는 `screen.products` 출신이라 `allowed` 에 반드시 들어 있어 cart/graph.py 의 전환
    조건을 통과한다 → 되물음이 조용히 버려지고 답한 적 없는 상품이 담긴다(end-to-end 재현:
    담긴 productId=502·pending 소멸·CART_ADDED). 코드 해소기도 `pending is None` 일 때만 도는데
    프롬프트만 화면 순번을 가르치던 비대칭이었다.
    """
    from app.agents.buyer.recommendation.decompose import _SYSTEM

    request = _screen_request("2번으로", "t-pending-screen")
    await _seed_pending(await _thread_key(request, _member()))

    llm = _PromptCapturingLLM({"cart": {"productId": 9001, "optionId": 1002, "quantity": 1}})
    await _collect(run_buyer_turn(request, _member(), llm=llm))

    # user — SCREEN 블록도, 화면 상품 id·순번 라벨도 실리지 않는다.
    assert "SCREEN" not in llm.user
    assert "501" not in llm.user and "502" not in llm.user
    assert "순번" not in llm.user
    assert "PENDING_CART:" in llm.user  # 되물음 맥락 자체는 그대로다
    # system — 화면 규칙이 붙지 않은 **원본 프롬프트와 바이트 동일**.
    assert llm.system == _SYSTEM


async def test_non_pending_turn_prompt_carries_the_screen_block_and_rule() -> None:
    """되물음이 아닌 턴에서는 종전대로 실린다 — 이번 수정이 화면 해소 자체를 끄지 않았다."""
    from app.agents.buyer.recommendation.decompose import _SYSTEM_WITH_SCREEN

    request = _screen_request("이거 담아줘", "t-nonpending-screen")

    llm = _PromptCapturingLLM({"cart": {"productId": 501, "quantity": 1}})
    await _collect(run_buyer_turn(request, _member(), llm=llm))

    assert "SCREEN: {" in llm.user
    assert '"순번": 1' in llm.user and '"순번": 2' in llm.user
    # system — 화면 규칙이 덧붙은 변형과 **바이트 동일**.
    assert llm.system == _SYSTEM_WITH_SCREEN
