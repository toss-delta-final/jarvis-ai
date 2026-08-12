"""장바구니 서브그래프 (이슈 #3) — 담기·옵션 되물음·조회·오류 매핑·라우팅·배선 회귀.

stream_cart_add/view 는 add_fn/get_cart_fn 주입으로, 라우팅은 run_buyer_turn + FakeLLM 으로,
spring_client 배선은 _client 몽키패치로 라이브 Spring 없이 구동한다.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agents.buyer.cart import graph as cart_graph
from app.agents.buyer.cart.graph import (
    _options_prompt,
    _options_text,
    stream_cart_add,
    stream_cart_view,
)
from app.agents.buyer.cart.options import OptionHint
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
    # 이슈 #570 — 옵션 줄은 이제 "\n" 으로 정당하게 나뉘므로, 옵션명에 실려온 원시 "\n"(레\n드)이
    # 별도 줄을 만들지 않고 한 줄로 접혔는지(= "레 드")까지 리터럴로 확인한다.
    assert token == ("옵션을 선택해 주세요:\n1. **블[31m루**\n2. **레 드**\n어떤 걸로 담을까요?")
    assert all(ch not in token for ch in ("\x1b", "\u200b", "\u202e"))


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


def test_purchase_state_label_covers_every_literal_value() -> None:
    """`PURCHASE_STATE_LABEL` 은 `PurchaseState` 전 값을 덮어야 한다(PR #400 리뷰).

    **이 테스트가 전사성을 강제하는 유일한 장치다.** 타입 어노테이션
    (`Mapping[PurchaseState, str]`)은 부분 매핑을 막지 못하고(`TypedDict` 가 아니다), 이 리포 CI 는
    `ruff check` + `pytest` 만 돌려 타입체커가 없다. 게다가 소비 측이 조용히 넘어간다 —
    `state_suffix` 는 `.get(state, "")`, `state_advice_lines` 는 `_ADVICE_ORDER` 에 없으면 skip.
    그래서 BE 가 상태를 추가하고 라벨 갱신을 빠뜨리면 **예외 없이 라벨만 소리 없이 사라진다**.
    """
    from typing import get_args

    from app.schemas.spring import PURCHASE_STATE_LABEL, PurchaseState

    assert set(get_args(PurchaseState)) == set(PURCHASE_STATE_LABEL)


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

    async def fake_delete(cart_item_id, *, user_id=None, guest_id=None, chat_session_id=None):
        assert cart_item_id == 1
        assert chat_session_id == "s1"
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


async def test_route_cart_add_unresolved_notice_reflects_has_last_reco() -> None:
    """[#435 리뷰 C4] `app/agents/buyer/graph.py` 의 **직접 분기**(cart_add)가 `has_last_reco`
    를 `stream_cart_add` 에 실제로 전달하는지 그래프 레벨로 고정한다. `cart/graph.py` 2선 위임
    경로는 이미 테스트로 박혀 있었지만(`test_stream_cart_add_wishlist_add_delegation_forwards_
    has_last_reco`), 주 경로인 이 직접 호출부는 인자를 지워도 잡히는 테스트가 없었다 —
    `run_buyer_turn` 의 `stream_cart_add(...)` 호출에서 `has_last_reco=has_last_reco` 를 지우면
    이 테스트가 옛 기본 문구로 실패한다(변이 시험으로 실측 확인, 보고 참조)."""
    from app.agents.buyer.cart.state import get_cart_store
    from tests._fakes import FakeLLM

    request = _req(message="음... 아무거나", thread_id="t-435-c4-cart-add")
    store = await get_cart_store()
    key = await _thread_key(request, _member())
    # last_reco 를 비어 있지 않게 시드 — `has_last_reco=True` 조건.
    await store.set_last_reco(key, [(101, "이어폰")])
    # productId=null 이면 담기 가드가 항상 미해소로 떨어진다(allowed 와 무관).
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": None, "quantity": 1}})
    events = await _collect(run_buyer_turn(request, _member(), llm=llm))
    text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert (
        text == "어떤 상품을 담을까요? 추천해 드린 상품 중에서 이름을 말씀해 주시면 담아드릴게요."
    )


async def test_route_wishlist_add_unresolved_notice_reflects_has_last_reco() -> None:
    """[#435 리뷰 C4] 같은 배선을 wishlist_add 직접 분기에도 고정한다."""
    from app.agents.buyer.cart.state import get_cart_store
    from tests._fakes import FakeLLM

    request = _req(message="음... 아무거나 찜해줘", thread_id="t-435-c4-wishlist-add")
    store = await get_cart_store()
    key = await _thread_key(request, _member())
    await store.set_last_reco(key, [(101, "이어폰")])
    llm = FakeLLM(decompose={"intent": "wishlist_add", "cart": {"productId": None}})
    events = await _collect(run_buyer_turn(request, _member(), llm=llm))
    text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert (
        text == "어떤 상품을 찜할까요? 추천해 드린 상품 중에서 이름을 말씀해 주시면 찜해 드릴게요."
    )


@pytest.mark.parametrize(
    ("intent", "message", "expected"),
    [
        (
            "cart_add",
            "음... 아무거나",
            "어떤 상품을 담을까요? 추천 목록 전달에 문제가 있었어요. 다시 추천을 요청해 주시면 도와드릴게요.",
        ),
        (
            "wishlist_add",
            "음... 아무거나 찜해줘",
            "어떤 상품을 찜할까요? 추천 목록 전달에 문제가 있었어요. 다시 추천을 요청해 주시면 도와드릴게요.",
        ),
    ],
)
async def test_unresolved_add_after_push_failure_asks_for_a_new_recommendation(
    intent: str, message: str, expected: str
) -> None:
    """[#468 I-21] push 실패 뒤에는 "추천을 먼저"라는 거짓 전제가 아닌 재요청 안내를 낸다.

    `has_push_failed`를 문구 함수로 넘기지 않거나 기본 문구를 유지하면 이 테스트가 기존의
    "추천을 먼저 받아보시면" 문구로 실패한다.
    """
    from app.agents.buyer.cart.state import get_cart_store
    from tests._fakes import FakeLLM

    request = _req(message=message, thread_id=f"t-468-push-failed-{intent}")
    store = await get_cart_store()
    key = await _thread_key(request, _member())
    await store.set_push_failed(key)
    llm = FakeLLM(decompose={"intent": intent, "cart": {"productId": None, "quantity": 1}})

    events = await _collect(run_buyer_turn(request, _member(), llm=llm))

    text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert text == expected


async def test_last_reco_clears_push_failed_marker_without_breaking_recommendation_state() -> None:
    """[#468 I-21] 다음 추천 push 성공은 실패 마커를 지워 기존 last_reco 문구가 우선하게 한다."""
    from app.agents.buyer.cart.state import CartStateStore

    store = CartStateStore()
    await store.set_push_failed("k")
    await store.set_last_reco("k", [(101, "이어폰")])

    assert await store.get_push_failed("k") is False
    assert await store.get_last_reco("k") == [(101, "이어폰")]


async def test_push_failed_marker_read_failure_keeps_unresolved_add_turn_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#468 I-21] 실패 마커 조회가 깨지면 오늘 기본 문구로 degrade하고 담기 턴은 끝까지 진행한다."""
    from app.agents.buyer.cart.state import CartStateStore
    from tests._fakes import FakeLLM

    async def fail_get_push_failed(self, key):  # noqa: ANN001
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(CartStateStore, "get_push_failed", fail_get_push_failed)
    request = _req(message="음... 아무거나", thread_id="t-468-push-failed-read")
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": None, "quantity": 1}})

    events = await _collect(run_buyer_turn(request, _member(), llm=llm))

    text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert text == "어떤 상품을 담을까요? 추천을 먼저 받아보시면 담아드릴게요."
    assert _types(events)[-1] == "done"


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

    async def patch(self, url, json=None, params=None):
        self.calls.append(("PATCH", url, {"params": params, "json": json}))
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


async def test_add_to_cart_serializes_chat_and_recommendation_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I-2는 챗 sentinel과 추천→담기 귀속을 같은 요청에 전달한다."""
    import app.services.spring_client as sc
    from app.schemas.spring import AddToCartRequest, RecommendationContext

    client = _CartClient(_CartResp(200, {"success": True, "data": {"cartItemId": 55}}))
    monkeypatch.setattr(sc, "_client", lambda: client)

    await sc.add_to_cart(
        AddToCartRequest(
            user_id=1,
            product_id=7,
            chat_session_id="chat-session-1",
            recommendation_context=RecommendationContext(
                recommendation_request_id="request-1", list_id="list-1"
            ),
        )
    )

    assert client.calls == [
        (
            "POST",
            "/internal/cart/items",
            {
                "userId": 1,
                "guestId": None,
                "productId": 7,
                "optionId": None,
                "quantity": 1,
                "chatSessionId": "chat-session-1",
                "recommendationContext": {
                    "recommendationRequestId": "request-1",
                    "listId": "list-1",
                },
            },
        )
    ]


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


@pytest.mark.parametrize("bad", [{}, [], {"kind": "SOLD_OUT"}, ["SOLD_OUT"]])
async def test_get_cart_unhashable_purchase_state_degrades_without_raising(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """unhashable 값(dict·list)이 와도 `TypeError` 가 아니라 `None` 강등이어야 한다(PR #400 리뷰).

    가드가 없으면 `value in frozenset` 이 `hash(value)` 에서 `TypeError` 를 내고, pydantic v2 는
    `BeforeValidator` 의 `TypeError` 를 `ValidationError` 로 감싸지 않아 그대로 올린다 —
    `get_cart` 의 `except (httpx.HTTPError, ValueError, ValidationError)` 를 빠져나가
    **degrade 조차 못 하는** 최악의 실패가 된다. 이 함수의 존재 이유가 드리프트 방어인데
    특정 드리프트 형태에서 더 크게 터지면 안 만든 것만 못하다."""
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [
                {"cartItemId": 55, "productId": 1, "productName": "파우치", "purchaseState": bad}
            ]
        },
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(200, body)))
    view = await sc.get_cart(user_id=1)
    assert len(view.items) == 1  # 항목이 사라지지 않는다
    assert view.items[0].purchase_state is None
    assert view.items[0].product_name == "파우치"  # 나머지 필드는 온전


@pytest.mark.parametrize("bad", [123, True, 1.5])
async def test_get_cart_non_string_scalar_purchase_state_degrades(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """문자열이 아닌 스칼라도 `None` 으로 강등된다 — `purchaseState` 는 문자열 enum 이다."""
    import app.services.spring_client as sc

    body = {
        "success": True,
        "data": {
            "items": [
                {"cartItemId": 55, "productId": 1, "productName": "파우치", "purchaseState": bad}
            ]
        },
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(200, body)))
    view = await sc.get_cart(user_id=1)
    assert view.items[0].purchase_state is None


# ─────────── spring_client 배선 (I-24 삭제, 이슈 #116, 확정 2026-08-05) ───────────


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


async def test_delete_cart_item_serializes_chat_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    client = _CartClient(_CartResp(200, {"success": True, "data": None}))
    monkeypatch.setattr(sc, "_client", lambda: client)

    await sc.delete_cart_item(55, user_id=1, chat_session_id="chat-session-1")

    assert client.calls == [
        (
            "DELETE",
            "/internal/cart/items/55",
            {"userId": 1, "chatSessionId": "chat-session-1"},
        )
    ]


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


# ─────────── spring_client 배선 (I-25 수량 변경, §4.13 — 확정 2026-08-05, #285 1단계) ───────────


async def test_change_cart_quantity_success_returns_final_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.spring_client as sc

    client = _CartClient(
        _CartResp(200, {"success": True, "data": {"cartItemId": 55, "quantity": 3}})
    )
    monkeypatch.setattr(sc, "_client", lambda: client)
    result = await sc.change_cart_quantity(55, 3, user_id=1)
    assert result.success and result.cart_item_id == 55 and result.quantity == 3
    assert client.calls == [
        ("PATCH", "/internal/cart/items/55", {"params": {"userId": 1}, "json": {"quantity": 3}})
    ]


async def test_change_cart_quantity_success_guest_id_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    client = _CartClient(
        _CartResp(200, {"success": True, "data": {"cartItemId": 55, "quantity": 5}})
    )
    monkeypatch.setattr(sc, "_client", lambda: client)
    result = await sc.change_cart_quantity(55, 5, guest_id="guest-uuid-1")
    assert result.quantity == 5
    assert client.calls == [
        (
            "PATCH",
            "/internal/cart/items/55",
            {"params": {"guestId": "guest-uuid-1"}, "json": {"quantity": 5}},
        )
    ]


async def test_change_cart_quantity_stock_insufficient_carries_available_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """400 CART_STOCK_INSUFFICIENT → CartStockInsufficient, available_stock 값이 실린다."""
    import app.services.spring_client as sc

    body = {
        "error": {"code": "CART_STOCK_INSUFFICIENT", "detail": {"availableStock": 2}},
    }
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartStockInsufficient) as exc_info:
        await sc.change_cart_quantity(55, 10, user_id=1)
    assert exc_info.value.available_stock == 2


async def test_change_cart_quantity_stock_insufficient_without_available_stock_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """availableStock 이 없으면 available_stock 은 None(값을 지어내지 않는다)."""
    import app.services.spring_client as sc

    body = {"error": {"code": "CART_STOCK_INSUFFICIENT"}}
    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(400, body)))
    with pytest.raises(sc.CartStockInsufficient) as exc_info:
        await sc.change_cart_quantity(55, 10, user_id=1)
    assert exc_info.value.available_stock is None


async def test_change_cart_quantity_validation_error_is_not_stock_insufficient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """400 이어도 code 가 VALIDATION_ERROR 면 재고 부족이 아니라 CartError."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _CartClient(_CartResp(400, {"error": {"code": "VALIDATION_ERROR"}})),
    )
    with pytest.raises(sc.CartError) as exc_info:
        await sc.change_cart_quantity(55, 3, user_id=1)
    assert not isinstance(exc_info.value, sc.CartStockInsufficient)


async def test_change_cart_quantity_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc,
        "_client",
        lambda: _CartClient(_CartResp(404, {"error": {"code": "CART_ITEM_NOT_FOUND"}})),
    )
    with pytest.raises(sc.CartItemNotFound):
        await sc.change_cart_quantity(999, 3, user_id=1)


async def test_change_cart_quantity_404_with_wrong_code_raises_cart_error_not_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[라운드 23 규약 계승] code 가 계약과 다른 404(엔드포인트 미배포 포함)를 "그 항목이 없다"
    로 오인하면 안 된다."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc, "_client", lambda: _CartClient(_CartResp(404, {"error": {"code": "NOT_FOUND"}}))
    )
    with pytest.raises(sc.CartError) as exc_info:
        await sc.change_cart_quantity(999, 3, user_id=1)
    assert not isinstance(exc_info.value, sc.CartItemNotFound)


async def test_change_cart_quantity_404_empty_body_raises_cart_error_not_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.spring_client as sc

    monkeypatch.setattr(sc, "_client", lambda: _CartClient(_CartResp(404, {})))
    with pytest.raises(sc.CartError) as exc_info:
        await sc.change_cart_quantity(999, 3, user_id=1)
    assert not isinstance(exc_info.value, sc.CartItemNotFound)


async def test_change_cart_quantity_forbidden_maps_to_cart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 AUTH_FORBIDDEN(소유자 불일치) 은 전용 예외 없이 CartError 로 낙성한다(I-24 와 동일)."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc, "_client", lambda: _CartClient(_CartResp(403, {"error": {"code": "AUTH_FORBIDDEN"}}))
    )
    with pytest.raises(sc.CartError):
        await sc.change_cart_quantity(55, 3, user_id=1)


async def test_change_cart_quantity_500_maps_to_cart_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc, "_client", lambda: _CartClient(_CartResp(500, {"error": {"code": "INTERNAL_ERROR"}}))
    )
    with pytest.raises(sc.CartError):
        await sc.change_cart_quantity(55, 3, user_id=1)


async def test_change_cart_quantity_200_success_false_raises_cart_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 이지만 공통 봉투 success:false 면 성공으로 처리하지 않는다(형제 어댑터와 같은 방어)."""
    import app.services.spring_client as sc

    monkeypatch.setattr(
        sc, "_client", lambda: _CartClient(_CartResp(200, {"success": False, "data": None}))
    )
    with pytest.raises(sc.CartError):
        await sc.change_cart_quantity(55, 3, user_id=1)


async def test_change_cart_quantity_rejects_zero_identity_queries() -> None:
    """신원 query 0개(둘 다 None) — 어댑터가 방어적으로 호출 자체를 막는다."""
    import app.services.spring_client as sc

    with pytest.raises(sc.CartError):
        await sc.change_cart_quantity(55, 3)


async def test_change_cart_quantity_rejects_two_identity_queries() -> None:
    """신원 query 2개(둘 다 not None) — "정확히 하나" 계약을 어댑터가 방어한다."""
    import app.services.spring_client as sc

    with pytest.raises(sc.CartError):
        await sc.change_cart_quantity(55, 3, user_id=1, guest_id="guest-uuid-1")


# ─────────── spring_client 배선 (I-26/I-27/I-28 찜, 이슈 #117, 확정 2026-08-05) 은 tests/unit/test_wishlist.py ───────────


# ─────────── 리뷰 수정 회귀 (Fix 1~4) ───────────


def test_parse_cart_clamps_quantity() -> None:
    """수량 상한(99) 초과 발화가 파싱 시점에 클램프된다(Fix1 — ValidationError 스트림 중단 방지)."""
    from app.agents.buyer.recommendation.decompose import _parse_cart

    assert _parse_cart({"productId": 1, "quantity": 1000}).quantity == 99
    assert _parse_cart({"productId": 1, "quantity": 0}).quantity == 1
    assert _parse_cart({"productId": 1, "quantity": 3}).quantity == 3


def test_parse_cart_target_quantity_does_not_default_to_one() -> None:
    """[#285, 함정 3 회귀 고정] `targetQuantity` 는 `quantity`(담기, 기본값 1)와 달리 추출
    실패·미기재를 **조용히 1로 메우지 않는다** — 변이 시험: `_parse_target_quantity` 가
    `quantity` 처럼 `min(max(qty, 1), 99) if qty is not None else 1` 로 바뀌면 이 테스트의
    `is None` 단정이 깨진다(1이 나온다)."""
    from app.agents.buyer.recommendation.decompose import _parse_cart

    assert _parse_cart({"quantity": 1}).target_quantity is None  # 키 자체가 없음
    assert _parse_cart({"quantity": 1, "targetQuantity": None}).target_quantity is None
    assert _parse_cart({"quantity": 1, "targetQuantity": 3}).target_quantity == 3


def test_parse_cart_target_quantity_out_of_range_asks_instead_of_clamping() -> None:
    """[#285, 함정 3] 범위(1~99) 밖 값은 `quantity` 처럼 잘라 보내지 않고 미해소(None)로
    되돌린다 — 클램프해서 보내면 사용자가 말한 값과 실제 전송값이 달라진다(BE 도 §4.13
    VALIDATION_ERROR 로 거부한다). 변이 시험: 클램프로 바뀌면 150 이 99 로 나와 이 단정이
    깨진다."""
    from app.agents.buyer.recommendation.decompose import _parse_cart

    assert _parse_cart({"quantity": 1, "targetQuantity": 150}).target_quantity is None
    assert _parse_cart({"quantity": 1, "targetQuantity": 0}).target_quantity is None
    assert _parse_cart({"quantity": 1, "targetQuantity": 1}).target_quantity == 1
    assert _parse_cart({"quantity": 1, "targetQuantity": 99}).target_quantity == 99


def test_parse_cart_target_quantity_coerces_float_and_string() -> None:
    """`_as_int` 관례를 그대로 따른다(새 파서를 만들지 않는다) — float·문자열 숫자도 받는다."""
    from app.agents.buyer.recommendation.decompose import _parse_cart

    assert _parse_cart({"quantity": 1, "targetQuantity": 3.0}).target_quantity == 3
    assert _parse_cart({"quantity": 1, "targetQuantity": "5"}).target_quantity == 5
    assert _parse_cart({"quantity": 1, "targetQuantity": "abc"}).target_quantity is None


async def test_route_cart_quantity(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#285] decompose 가 직접 `cart_quantity` 를 산출하면 `stream_cart_quantity_change` 로
    위임된다(`test_route_cart_remove` 와 같은 패턴) — `intent` 허용 목록에 `cart_quantity` 가
    빠지면 이 테스트가 `"recommend"` 로 떨어져 change_cart_quantity 가 호출되지 않아 깨진다
    (실제 runtime 파싱 분기를 exercise — 정적 타입 대조가 아니다)."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc

    async def fake_get(*, user_id=None, guest_id=None):
        return CartView(
            items=[CartViewItem(cart_item_id=1, product_id=1, product_name="키보드", quantity=1)]
        )

    async def fake_change(cart_item_id, quantity, *, user_id=None, guest_id=None):
        assert cart_item_id == 1 and quantity == 3
        return sc.ChangeCartQuantityResult(success=True, cart_item_id=1, quantity=3)

    monkeypatch.setattr(sc, "get_cart", fake_get)
    monkeypatch.setattr(sc, "change_cart_quantity", fake_change)
    llm = FakeLLM(decompose={"intent": "cart_quantity", "cart": {"targetQuantity": 3}})
    events = await _collect(run_buyer_turn(_req(message="키보드 3개로 바꿔줘"), _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_QUANTITY_CHANGED"
    assert action["quantity"] == 3


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


async def test_route_cart_add_forwards_saved_recommendation_context() -> None:
    """추천 카드에서 해소한 담기는 현재 세션과 그 카드의 귀속 키를 함께 보낸다."""
    from tests._fakes import FakeLLM
    import app.services.spring_client as sc
    from app.agents.buyer.cart.state import get_cart_store
    from app.schemas.spring import RecommendationContext

    captured = []

    async def fake_add(request):
        captured.append(request)
        return AddToCartResult(success=True, cart_item_id=42)

    async def fake_get(*, user_id=None, guest_id=None):
        return CartView(items=[])

    request = _req(thread_id="attribution")
    request.session_id = "chat-session-1"
    store = await get_cart_store()
    key = await _thread_key(request, _member())
    await store.set_last_reco(
        key,
        [(101, "이어폰")],
        recommendation_contexts={
            101: RecommendationContext(recommendation_request_id="request-1", list_id="list-1")
        },
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", fake_get)
    try:
        events = await _collect(
            run_buyer_turn(
                request,
                _member(),
                llm=FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 101}}),
            )
        )
    finally:
        monkeypatch.undo()

    assert (
        next(event for event in events if event["type"] == "action")["data"]["type"] == "CART_ADDED"
    )
    assert captured[0].chat_session_id == "chat-session-1"
    assert captured[0].recommendation_context == RecommendationContext(
        recommendation_request_id="request-1", list_id="list-1"
    )


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


async def _run_add(
    store,
    cart,
    add_fn,
    *,
    get_cart_fn=None,
    thread_key="m:t",
    message="",
    condition_terms=(),
    product_names=None,
):
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
            message=message,
            condition_terms=condition_terms,
            product_names=product_names,
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


# ─────────── I-1 options·optionCount 소비 — 되물음 좁히기 (이슈 #455) ───────────


async def test_cart_add_single_option_still_autoselects_with_hint_present() -> None:
    """(#114 회귀) 힌트 optionCount==1 + 400 목록 1개여도 좁히기 로직이 개입하지 않는다.

    유일 옵션 자동 선택(#114)은 힌트로 게이팅하지 않는다 — 힌트가 있어도 없어도 동작은 같다.
    """
    store = CartStateStore()
    calls: list[int | None] = []

    async def add_fn(req):
        calls.append(req.option_id)
        if req.option_id is None:
            raise CartOptionRequired([CartOption(option_id=7, name="단일 사이즈")])
        return AddToCartResult(success=True, cart_item_id=70)

    await store.set_last_reco("m:t", [(1, "상품")], option_hints={1: OptionHint(names=(), total=1)})

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    assert calls == [None, 7]
    assert "token" not in _types(events)
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED" and action["cartItemId"] == 70
    assert await store.get_pending("m:t") is None


async def test_cart_add_narrows_by_message_to_single_candidate() -> None:
    """(핵심 이득) 이번 발화가 후보 3개 중 1개만 매칭하면 되묻지 않고 같은 턴에 담는다."""
    store = CartStateStore()
    calls: list[int | None] = []

    async def add_fn(req):
        calls.append(req.option_id)
        if req.option_id is None:
            raise CartOptionRequired(
                [
                    CartOption(option_id=5, name="레드"),
                    CartOption(option_id=6, name="블루"),
                    CartOption(option_id=7, name="그린"),
                ]
            )
        return AddToCartResult(success=True, cart_item_id=80)

    events = await _run_add(
        store, CartIntent(product_id=1, quantity=1), add_fn, message="레드로 담아줘"
    )

    assert calls == [None, 5]  # 되물음 왕복 없이 I-2 는 2회 호출
    assert "token" not in _types(events)
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED" and action["cartItemId"] == 80
    assert "레드" in action["message"]
    assert await store.get_pending("m:t") is None


async def test_cart_add_narrowed_reask_hides_unmatched_options() -> None:
    """(좁힌 되물음) 조건에 맞는 2개만 문구에 실리고 나머지 2개는 문구에 없다 — pending 은 전체 4개."""
    store = CartStateStore()
    all_options = [
        CartOption(option_id=1, name="코튼 화이트"),
        CartOption(option_id=2, name="코튼 블랙"),
        CartOption(option_id=3, name="울 그레이"),
        CartOption(option_id=4, name="폴리 네이비"),
    ]

    async def add_fn(req):
        raise CartOptionRequired(all_options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="아무거나 담아줘",
        condition_terms=("코튼",),
    )

    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "코튼 화이트" in token and "코튼 블랙" in token
    assert "울 그레이" not in token and "폴리 네이비" not in token

    pending = await store.get_pending("m:t")
    assert pending is not None
    assert [o.option_id for o in pending.options] == [1, 2, 3, 4]  # 전체 저장


async def test_cart_add_narrowing_no_match_degrades_to_today_text() -> None:
    """(degrade — 못 좁힘) 조건이 아무것도 매칭 안 되면 문구가 오늘과 바이트 동일."""
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블루"), CartOption(option_id=2, name="그린")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store, CartIntent(product_id=1, quantity=1), add_fn, message="레드로 주세요"
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == _options_prompt("옵션을 선택해 주세요:", options, "어떤 걸로 담을까요?")


async def test_cart_add_narrowing_all_match_degrades_to_today_text() -> None:
    """(degrade — 전부 매칭) 모든 옵션이 매칭되면 좁힌 게 아니므로 문구가 오늘과 동일."""
    store = CartStateStore()
    options = [CartOption(option_id=1, name="레드"), CartOption(option_id=2, name="레드/M")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store, CartIntent(product_id=1, quantity=1), add_fn, message="레드 담아줘"
    )

    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == _options_prompt("옵션을 선택해 주세요:", options, "어떤 걸로 담을까요?")


async def test_cart_add_accumulated_condition_does_not_autoselect() -> None:
    """(누적 조건은 자동 선택하지 않는다) 이번 발화에 없는 조건으로 1개만 매칭돼도 자동 선택 안 함."""
    store = CartStateStore()
    options = [
        CartOption(option_id=1, name="레드"),
        CartOption(option_id=2, name="블루"),
        CartOption(option_id=3, name="그린"),
    ]
    calls: list[int | None] = []

    async def add_fn(req):
        calls.append(req.option_id)
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="아무거나 담아줘",
        condition_terms=("레드",),
    )

    assert calls == [None]  # 재호출 없음 — 자동 선택 안 됨
    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "레드" in token
    assert "블루" not in token and "그린" not in token
    pending = await store.get_pending("m:t")
    assert pending is not None and len(pending.options) == 3


async def test_cart_add_term_substring_of_name_does_not_autoselect() -> None:
    """(#455 리뷰 F-5) 조건어가 옵션 이름의 부분 문자열일 뿐이면(세그먼트 정확 일치 아님) 자동
    선택하지 않고 되묻는다 — "그레이"라고만 말했는데 확인 없이 "그레이라이트"가 담기면 안 된다."""
    store = CartStateStore()
    calls: list[int | None] = []
    options = [CartOption(option_id=1, name="그레이라이트"), CartOption(option_id=2, name="네이비")]

    async def add_fn(req):
        calls.append(req.option_id)
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="그레이로 담아줘",
        condition_terms=("그레이",),
    )

    assert calls == [None]  # 재호출 없음 — 자동 선택 안 됨
    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "그레이라이트" in token  # 조건에 맞는 옵션으로 문구는 좁혀진다(R2)
    pending = await store.get_pending("m:t")
    assert pending is not None and len(pending.options) == 2


async def test_cart_add_narrows_without_any_hint_present() -> None:
    """(미전송) 힌트가 아예 없어도(재추천 없이 바로 담기 등) 좁히기 동작은 그대로다."""
    store = CartStateStore()
    assert await store.get_option_hint("m:t", 1) is None
    calls: list[int | None] = []

    async def add_fn(req):
        calls.append(req.option_id)
        if req.option_id is None:
            raise CartOptionRequired(
                [CartOption(option_id=5, name="레드"), CartOption(option_id=6, name="블루")]
            )
        return AddToCartResult(success=True, cart_item_id=81)

    events = await _run_add(
        store, CartIntent(product_id=1, quantity=1), add_fn, message="레드로 담아줘"
    )

    assert calls == [None, 5]
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"


async def test_cart_add_option_count_guard_passes_when_hint_matches_filtered_options() -> None:
    """(#508 신 계약) 품절 제외 후에도 정합 가드가 자동 선택을 막지 않는다 — I-1 힌트 `total`
    과 I-2 400 목록이 이미 같은 기준(구매 가능)으로 필터돼 있어 개수가 자연히 일치한다. 아래
    `..._mismatch_skips_autoselect` 와 반대 방향(가드 통과 vs 가드 차단)을 잡는 짝 테스트다."""
    store = CartStateStore()
    options = [
        CartOption(option_id=1, name="레드"),
        CartOption(option_id=2, name="블루"),
        CartOption(option_id=3, name="그린"),
    ]
    calls: list[int | None] = []

    async def add_fn(req):
        calls.append(req.option_id)
        if req.option_id is None:
            raise CartOptionRequired(options)
        return AddToCartResult(success=True, cart_item_id=91)

    # total=3 == len(options)=3 — 품절 제외로 이미 같은 기준으로 걸러진 상태를 흉내낸다.
    await store.set_last_reco("m:t", [(1, "상품")], option_hints={1: OptionHint(names=(), total=3)})

    events = await _run_add(
        store, CartIntent(product_id=1, quantity=1), add_fn, message="레드로 담아줘"
    )

    assert calls == [None, 1]  # 자동 선택 발동 — 되물음 왕복 없이 담긴다
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED" and action["cartItemId"] == 91


async def test_cart_add_option_count_mismatch_skips_autoselect() -> None:
    """(`optionCount` 불일치) 힌트 total=5 인데 400 목록이 3개면 자동 선택하지 않고 되묻는다.
    (#508 신 계약에서도 유효) — 신 계약에서는 힌트·400 목록이 보통 같은 기준(구매 가능)으로
    맞춰지지만(위 `..._guard_passes_...` 참조), 그럼에도 어긋나면(드리프트·계약 위반) 가드는
    여전히 발동해 자동 선택을 막는다 — "품절 제외로 항상 일치한다"고 가드를 없애면 안 되는 이유."""
    store = CartStateStore()
    options = [
        CartOption(option_id=1, name="레드"),
        CartOption(option_id=2, name="블루"),
        CartOption(option_id=3, name="그린"),
    ]
    calls: list[int | None] = []

    async def add_fn(req):
        calls.append(req.option_id)
        raise CartOptionRequired(options)

    await store.set_last_reco("m:t", [(1, "상품")], option_hints={1: OptionHint(names=(), total=5)})

    events = await _run_add(
        store, CartIntent(product_id=1, quantity=1), add_fn, message="레드로 담아줘"
    )

    assert calls == [None]  # 재호출(자동 선택) 없음 — 정합 가드가 막는다
    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "레드" in token


async def test_cart_add_truncated_hint_names_do_not_crash_narrowing() -> None:
    """(절단 수신) 힌트 이름 20개·optionCount 25 를 받아도 좁히기 대상은 400 목록이고, 길이 불일치가
    예외를 만들지 않는다."""
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블루"), CartOption(option_id=2, name="그린")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    truncated_names = tuple(f"색상{i}" for i in range(20))
    await store.set_last_reco(
        "m:t", [(1, "상품")], option_hints={1: OptionHint(names=truncated_names, total=25)}
    )

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn, message="아무거나")

    assert "action" not in _types(events)
    assert "token" in _types(events)  # 예외 없이 되물음으로 degrade


async def test_cart_add_empty_options_falls_back_to_hint_names() -> None:
    """(400 목록 비었을 때 I-1 이름 폴백) 힌트 이름 + total 로 되묻고, 위험 문자는 제거된다."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartOptionRequired([])

    await store.set_last_reco(
        "m:t",
        [(1, "상품")],
        option_hints={
            1: OptionHint(names=("블\x1b[31m랙​", "화이트", "레드"), total=5),
        },
    )

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "블[31m랙" in token
    assert "화이트" in token and "레드" in token
    assert "외 2개" in token
    assert all(ch not in token for ch in ("\x1b", "​"))


async def test_cart_add_empty_options_without_hint_degrades_to_stock_message() -> None:
    """(이슈 #508) 400 목록이 비었고 I-1 힌트 이름도 없으면 품절 안내로 degrade한다 — 신
    계약에서는 이 경로가 남은 옵션이 없다는 뜻이라(BE 는 보통 CART_STOCK_INSUFFICIENT 로 내려야
    한다) 무의미한 "옵션을 선택해 주세요: 옵션." 문구 대신 재고를 단정해도 근거가 있다."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartOptionRequired([])

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == "지금은 고를 수 있는 옵션이 없어요. 품절된 것 같아요. 다른 상품을 보여드릴까요?"


async def test_cart_add_empty_options_with_sanitized_empty_hint_degrades_to_stock_message() -> None:
    """I-1 힌트가 있어도 모든 이름이 정제 뒤 비면 품절 안내로 degrade한다."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartOptionRequired([])

    await store.set_last_reco(
        "m:t",
        [(1, "상품")],
        option_hints={1: OptionHint(names=("\x1b", "\u200b", "\u202e"), total=3)},
    )

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == "지금은 고를 수 있는 옵션이 없어요. 품절된 것 같아요. 다른 상품을 보여드릴까요?"
    assert all(marker not in token for marker in ("1.", "2.", "3.", "**"))


# ─────────── 색상 동의어 등가·조건 미충족 고지 (이슈 #454) ───────────
#
# 사전 적재는 `_cart_option_required_text` 호출 전에 `spring_client._load_color_synonym_map` 을
# 거친다(graph.py::_load_cart_color_synonyms) — 그 함수를 몽키패치해 라이브 DB 없이 구동한다.

_BLACK_SYNONYMS = {"검정": ["블랙", "검정", "흑색"], "블랙": ["블랙", "검정", "흑색"]}


def _mock_synonym_map(mapping: dict) -> object:
    async def _load(settings):
        return mapping

    return _load


async def test_cart_add_color_equivalence_narrows_reask_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2-A 이득) 조건어 "검정" + 옵션명 "블랙" — 사전이 있으면 되물음 문구가 블랙 두 건으로 좁혀진다."""
    monkeypatch.setattr(
        "app.services.spring_client._load_color_synonym_map", _mock_synonym_map(_BLACK_SYNONYMS)
    )
    store = CartStateStore()
    options = [
        CartOption(option_id=1, name="블랙 / M"),
        CartOption(option_id=2, name="블랙 / L"),
        CartOption(option_id=3, name="화이트 / M"),
    ]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="아무거나 담아줘",
        condition_terms=("검정",),
    )

    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "말씀하신 조건에 맞는 옵션이에요" in token
    assert "블랙 / M" in token and "블랙 / L" in token
    assert "화이트 / M" not in token


async def test_cart_add_color_equivalence_does_not_feed_autoselect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(R1 불변, §454) 조건어·발화가 색상 동의어 표기라도 자동 선택 근거(R1)는 등가를 보지 않는다
    — 등가는 되물음 문구 좁히기(R2)에만 적용된다. 단일 "블랙" 후보 하나뿐이라, R1 이 등가를
    (버그로) 봤다면 후보가 정확히 1개로 좁혀져 되묻지 않고 자동 담겼을 것이다."""
    monkeypatch.setattr(
        "app.services.spring_client._load_color_synonym_map", _mock_synonym_map(_BLACK_SYNONYMS)
    )
    store = CartStateStore()
    calls: list[int | None] = []
    options = [CartOption(option_id=1, name="블랙"), CartOption(option_id=2, name="화이트")]

    async def add_fn(req):
        calls.append(req.option_id)
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="검정 담아줘",
        condition_terms=("검정",),
    )

    assert calls == [None]  # 재호출(자동 선택) 없음 — R1 은 등가를 몰라 후보를 못 좁힌다
    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    # 되물음 문구(R2)는 등가로 좁혀진다 — 자동 선택만 안 될 뿐 문구는 여전히 2-A 이득을 본다.
    assert "블랙" in token and "화이트" not in token


async def test_cart_add_no_synonym_dictionary_degrades_to_today_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(사전 없음 degrade) 사전이 `None` 이면(적재 결과 없음) 결과가 오늘과 완전히 동일하다."""
    monkeypatch.setattr(
        "app.services.spring_client._load_color_synonym_map", _mock_synonym_map(None)
    )
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙"), CartOption(option_id=2, name="화이트")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="아무거나 담아줘",
        condition_terms=("검정",),
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == _options_prompt("옵션을 선택해 주세요:", options, "어떤 걸로 담을까요?")


async def test_cart_add_unresolved_color_condition_reports_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2-B 발동) 조건어 "빨강" — 색상 축은 있지만(블랙/화이트) 매칭이 하나도 없으면 "없다"고
    단정하지 않고 "찾지 못했어요"로만 안내하고 전체 옵션을 보여준다(패킷 §3 문구 그대로)."""
    mapping = {
        "빨강": ["빨강", "레드"],
        "블랙": ["블랙", "검정"],
        "화이트": ["화이트", "흰색"],
    }
    monkeypatch.setattr(
        "app.services.spring_client._load_color_synonym_map", _mock_synonym_map(mapping)
    )
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙 / M"), CartOption(option_id=2, name="화이트 / M")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="빨강 담아줘",
        condition_terms=("빨강",),
    )

    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == _options_prompt(
        "'빨강' 조건에 맞는 옵션은 찾지 못했어요. 고를 수 있는 옵션은 이거예요:",
        options,
        "이 중에서 고르시거나 다른 상품을 말씀해 주세요.",
    )
    assert "없어요" not in token and "품절" not in token  # 단정 금지(패킷 §3)


async def test_cart_add_no_color_axis_keeps_todays_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """(2-B 미발동① — 색상 축 없음) 옵션명이 사이즈뿐이면 색상 조건이 안 맞아도 오늘 문구 그대로다."""
    mapping = {"빨강": ["빨강", "레드"]}
    monkeypatch.setattr(
        "app.services.spring_client._load_color_synonym_map", _mock_synonym_map(mapping)
    )
    store = CartStateStore()
    options = [
        CartOption(option_id=1, name="S"),
        CartOption(option_id=2, name="M"),
        CartOption(option_id=3, name="L"),
    ]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="빨강 담아줘",
        condition_terms=("빨강",),
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == _options_prompt("옵션을 선택해 주세요:", options, "어떤 걸로 담을까요?")


async def test_cart_add_no_condition_terms_keeps_todays_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2-B 미발동② — 조건어 없음) 조건어가 아예 없으면 사전이 있어도 오늘 문구 그대로다."""
    monkeypatch.setattr(
        "app.services.spring_client._load_color_synonym_map", _mock_synonym_map(_BLACK_SYNONYMS)
    )
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙"), CartOption(option_id=2, name="화이트")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store, CartIntent(product_id=1, quantity=1), add_fn, message="아무거나 담아줘"
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == _options_prompt("옵션을 선택해 주세요:", options, "어떤 걸로 담을까요?")


async def test_cart_add_color_condition_fully_matched_keeps_todays_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2-B 미발동③ — 전건 일치) 조건에 전 옵션이 맞으면(등가 포함) "찾지 못했어요" 문구를 내지
    않는다 — `condition_matched_all` 이 이 케이스를 "0건 좁힘"과 갈라주지 않으면 여기서 거짓
    안내("찾지 못했다")가 나간다."""
    monkeypatch.setattr(
        "app.services.spring_client._load_color_synonym_map", _mock_synonym_map(_BLACK_SYNONYMS)
    )
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙 / M"), CartOption(option_id=2, name="블랙 / L")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="검정 담아줘",
        condition_terms=("검정",),
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == _options_prompt("옵션을 선택해 주세요:", options, "어떤 걸로 담을까요?")
    assert "찾지 못했어요" not in token


async def test_cart_add_color_synonym_config_off_keeps_todays_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(설정 off) `cart_option_color_synonym_enabled=False` 면 사전을 아예 조회하지 않고
    오늘 동작 그대로다."""
    monkeypatch.setattr(get_settings(), "cart_option_color_synonym_enabled", False)
    monkeypatch.setattr(
        "app.services.spring_client._load_color_synonym_map",
        lambda settings: pytest.fail("설정 off 인데 사전을 조회했다"),
    )
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙"), CartOption(option_id=2, name="화이트")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="아무거나 담아줘",
        condition_terms=("검정",),
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == _options_prompt("옵션을 선택해 주세요:", options, "어떤 걸로 담을까요?")


async def test_cart_add_synonym_load_failure_degrades_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(사전 적재 실패) 적재가 예외를 던져도 담기 흐름이 죽지 않고 오늘 문구로 degrade한다."""

    async def _raise(settings):
        raise RuntimeError("catalog offline")

    monkeypatch.setattr("app.services.spring_client._load_color_synonym_map", _raise)
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙"), CartOption(option_id=2, name="화이트")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="아무거나 담아줘",
        condition_terms=("검정",),
    )

    assert "action" not in _types(events)
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == _options_prompt("옵션을 선택해 주세요:", options, "어떤 걸로 담을까요?")


# ─────────── 이슈 #570 — 되물음 줄바꿈 나열 리터럴 회귀 ───────────
#
# 아래는 패킷 §1 A-3/A-4 의 정본 출력 문자열을 그대로 리터럴로 박아 둔다 — 피검사 함수
# (_options_text/_options_prompt)를 기대값 계산에 다시 쓰지 않는다(공허성 방지, 패킷 §4-D-1).
# `_options_text` 의 "\n".join 을 " | ".join 으로 바꾸면 이 리터럴 테스트들이 모두 빨개져야 한다.


def test_option_product_heading_sanitizes_and_truncates() -> None:
    assert cart_graph._option_product_heading("짧은 상품") == "**상품:** 짧은 상품"
    assert cart_graph._option_product_heading("가" * 40) == f"**상품:** {'가' * 40}"
    assert cart_graph._option_product_heading("나" * 41) == f"**상품:** {'나' * 40}…"
    assert cart_graph._option_product_heading("시\n계\u200b\u202e") == "**상품:** 시 계"
    assert cart_graph._option_product_heading("\u200b\u202e") == ""
    assert cart_graph._option_product_heading(None) == ""


async def test_option_product_default_reask_names_and_truncates_target() -> None:
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙"), CartOption(option_id=2, name="화이트")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        product_names={1: "시" * 41},
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        f"**상품:** {'시' * 40}…\n\n"
        "옵션을 선택해 주세요:\n"
        "1. **블랙**\n"
        "2. **화이트**\n"
        "어떤 걸로 담을까요?"
    )


async def test_option_product_missing_target_name_keeps_default_reask_literal() -> None:
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙"), CartOption(option_id=2, name="화이트")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        product_names={999: "다른 상품"},
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == ("옵션을 선택해 주세요:\n1. **블랙**\n2. **화이트**\n어떤 걸로 담을까요?")


async def test_option_product_narrow_reask_names_target() -> None:
    store = CartStateStore()
    options = [
        CartOption(option_id=1, name="블랙 / M"),
        CartOption(option_id=2, name="화이트 / M"),
        CartOption(option_id=3, name="레드 / L"),
    ]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="블랙이나 화이트로 담아줘",
        condition_terms=("블랙", "화이트"),
        product_names={1: "조건 상품"},
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        "**상품:** 조건 상품\n\n"
        "말씀하신 조건에 맞는 옵션이에요:\n"
        "1. **블랙 / M**\n"
        "2. **화이트 / M**\n"
        "이 중에서 고르시거나 다른 옵션을 말씀해 주세요."
    )


async def test_option_product_color_unmet_reask_names_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = {"빨강": ["빨강", "레드"], "블랙": ["블랙", "검정"], "화이트": ["화이트", "흰색"]}
    monkeypatch.setattr(
        "app.services.spring_client._load_color_synonym_map", _mock_synonym_map(mapping)
    )
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙 / M"), CartOption(option_id=2, name="화이트 / M")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="빨강 담아줘",
        condition_terms=("빨강",),
        product_names={1: "색상 상품"},
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        "**상품:** 색상 상품\n\n"
        "'빨강' 조건에 맞는 옵션은 찾지 못했어요. 고를 수 있는 옵션은 이거예요:\n"
        "1. **블랙 / M**\n"
        "2. **화이트 / M**\n"
        "이 중에서 고르시거나 다른 상품을 말씀해 주세요."
    )


async def test_option_product_hint_fallback_names_target() -> None:
    store = CartStateStore()
    await store.set_last_reco(
        "m:t",
        [(1, "힌트 상품")],
        option_hints={1: OptionHint(names=("블랙", "화이트"), total=3)},
    )

    async def add_fn(req):
        raise CartOptionRequired([])

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        product_names={1: "힌트 상품"},
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        "**상품:** 힌트 상품\n\n"
        "옵션을 선택해 주세요:\n"
        "1. **블랙**\n"
        "2. **화이트**\n"
        "외 1개\n"
        "어떤 걸로 담을까요?"
    )


async def test_option_product_invalid_reask_uses_pending_target_name() -> None:
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(
            product_id=1,
            quantity=1,
            options=[CartOption(option_id=3, name="블루")],
            attempts=0,
        ),
    )
    options = [CartOption(option_id=1, name="블랙"), CartOption(option_id=2, name="화이트")]

    async def add_fn(req):
        raise CartOptionInvalid(options)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=None, option_id=9, quantity=1),
            cart_store=store,
            thread_key="m:t",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_empty_cart(),
            product_names={1: "대기 상품"},
        )
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        "**상품:** 대기 상품\n\n"
        "그 옵션을 찾지 못했어요. 다시 골라 주세요:\n"
        "1. **블랙**\n"
        "2. **화이트**"
    )


def test_numbered_option_rows_bolds_complete_labels_in_order() -> None:
    labels = ["블랙 / M", "화이트 / L(+1,000원)"]

    assert cart_graph._numbered_option_rows(labels) == (
        "1. **블랙 / M**\n2. **화이트 / L(+1,000원)**"
    )


def test_options_text_numbers_only_sanitized_displayable_labels_contiguously() -> None:
    options = [
        CartOption(option_id=1, name="블\x1b[31m랙\u200b"),
        CartOption(option_id=2, name="\u200b\u202e"),
        CartOption(option_id=3, name="화\n이트"),
    ]

    assert _options_text(options) == "1. **블[31m랙**\n2. **화 이트**"


async def test_cart_option_narrow_reask_literal_matches_issue_570() -> None:
    """(1) #582 조건 좁힘 — 옵션 두 개가 번호·굵은 글씨로 나열된다."""
    store = CartStateStore()
    options = [
        CartOption(option_id=1, name="블랙 / M"),
        CartOption(option_id=2, name="화이트 / M"),
        CartOption(option_id=3, name="레드 / L"),
    ]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="아무거나 담아줘",
        condition_terms=("블랙", "화이트"),
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        "말씀하신 조건에 맞는 옵션이에요:\n"
        "1. **블랙 / M**\n"
        "2. **화이트 / M**\n"
        "이 중에서 고르시거나 다른 옵션을 말씀해 주세요."
    )


async def test_cart_option_color_unmet_reask_literal_matches_issue_570(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2) #582 색상 미충족 고지 — 첫 줄은 종전처럼 두 문장이 한 줄이고, 옵션은 번호·굵은 글씨로 나열된다."""
    mapping = {"빨강": ["빨강", "레드"], "블랙": ["블랙", "검정"], "화이트": ["화이트", "흰색"]}
    monkeypatch.setattr(
        "app.services.spring_client._load_color_synonym_map", _mock_synonym_map(mapping)
    )
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙 / M"), CartOption(option_id=2, name="화이트 / M")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(
        store,
        CartIntent(product_id=1, quantity=1),
        add_fn,
        message="빨강 담아줘",
        condition_terms=("빨강",),
    )

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        "'빨강' 조건에 맞는 옵션은 찾지 못했어요. 고를 수 있는 옵션은 이거예요:\n"
        "1. **블랙 / M**\n"
        "2. **화이트 / M**\n"
        "이 중에서 고르시거나 다른 상품을 말씀해 주세요."
    )


async def test_cart_option_default_reask_literal_matches_issue_570() -> None:
    """(3) #582 기본 되물음 — 옵션 두 개가 번호·굵은 글씨로 나열된다."""
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙 / M"), CartOption(option_id=2, name="화이트 / M")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        "옵션을 선택해 주세요:\n1. **블랙 / M**\n2. **화이트 / M**\n어떤 걸로 담을까요?"
    )


async def test_cart_option_invalid_reask_literal_matches_issue_570() -> None:
    """(4) #582 CART_OPTION_INVALID 재질문 — 마무리 줄 없이 번호·굵은 글씨 옵션만 나열된다."""
    store = CartStateStore()
    await store.set_pending(
        "m:t",
        PendingAdd(
            product_id=1, quantity=1, options=[CartOption(option_id=3, name="블루")], attempts=0
        ),
    )
    options = [CartOption(option_id=1, name="블랙 / M"), CartOption(option_id=2, name="화이트 / M")]

    async def add_fn(req):
        raise CartOptionInvalid(options)

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
    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        "그 옵션을 찾지 못했어요. 다시 골라 주세요:\n1. **블랙 / M**\n2. **화이트 / M**"
    )


async def test_cart_option_hint_fallback_literal_matches_issue_570() -> None:
    """(hint) #582 I-1 힌트 이름 폴백 — '외 N개' 는 독립된 줄이고 마지막 이름에 붙지 않는다(패킷 §1 A-4)."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartOptionRequired([])

    await store.set_last_reco(
        "m:t",
        [(1, "상품")],
        option_hints={1: OptionHint(names=("블랙", "화이트", "레드"), total=5)},
    )

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        "옵션을 선택해 주세요:\n"
        "1. **블랙**\n"
        "2. **화이트**\n"
        "3. **레드**\n"
        "외 2개\n"
        "어떤 걸로 담을까요?"
    )


async def test_cart_option_hint_fallback_without_total_has_no_extra_line() -> None:
    """(hint) `hint.total` 이 이름 수 이하면(또는 없으면) '외 N개' 줄 자체가 없다(패킷 §1 A-4)."""
    store = CartStateStore()

    async def add_fn(req):
        raise CartOptionRequired([])

    await store.set_last_reco(
        "m:t",
        [(1, "상품")],
        option_hints={1: OptionHint(names=("블랙", "화이트"), total=None)},
    )

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == ("옵션을 선택해 주세요:\n1. **블랙**\n2. **화이트**\n어떤 걸로 담을까요?")


async def test_cart_option_reask_reproduces_issue_570_symptom() -> None:
    """[이슈 #570 재현] 옵션명 자체에 '/' 가 있어도(로컬 카탈로그 53.7% 실측) 옵션 줄이 정확히
    2줄이고, 어떤 옵션 줄도 마침표로 끝나지 않으며, 옵션명이 원문 그대로 한 줄에 온전히 들어있다."""
    store = CartStateStore()
    options = [CartOption(option_id=1, name="블랙 / M"), CartOption(option_id=2, name="화이트 / M")]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    lines = token.split("\n")
    option_lines = lines[1:-1]  # 안내 줄·마무리 줄 제외
    assert option_lines == ["1. **블랙 / M**", "2. **화이트 / M**"]
    assert all(not line.endswith(".") for line in option_lines)


async def test_cart_add_reask_surcharge_option_on_own_line() -> None:
    """[이슈 #570] extraPrice>0 인 옵션이 '레드(+1,000원)' 형태로 자기 줄에 온전히 나온다."""
    store = CartStateStore()
    options = [
        CartOption(option_id=3, name="블루", extra_price=0),
        CartOption(option_id=4, name="레드", extra_price=1000),
    ]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        "옵션을 선택해 주세요:\n1. **블루**\n2. **레드(+1,000원)**\n어떤 걸로 담을까요?"
    )


def test_options_text_empty_list_falls_back_to_default_label() -> None:
    """빈 옵션 목록이면 `_options_text` 가 여전히 '옵션'을 돌려준다(경로 (4)에서만 실제 도달)."""
    assert _options_text([]) == "옵션"


async def test_cart_option_numeric_prefix_name_not_escaped() -> None:
    """[이슈 #570] 옵션명이 "4. 얼큰한맛 92g x 30개"(실제 카탈로그 값)처럼 번호 목록 접두로
    시작해도 이스케이프하지 않고 원문 그대로 한 줄에 싣는다 — FE 마크다운 파서가 아직 배포
    전이라 어떤 이스케이프든 오늘 사용자에게 백슬래시로 그대로 보인다. 파서 도착 후(하이픈
    단계) 재검토 대상이다(api-spec §3.1 (2) 실측 근거 참조)."""
    store = CartStateStore()
    options = [
        CartOption(option_id=1, name="4. 얼큰한맛 92g x 30개"),
        CartOption(option_id=2, name="순한맛 92g x 30개"),
    ]

    async def add_fn(req):
        raise CartOptionRequired(options)

    events = await _run_add(store, CartIntent(product_id=1, quantity=1), add_fn)

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "\\" not in token  # 이스케이프하지 않는다
    assert "1. **4. 얼큰한맛 92g x 30개**" in token.split("\n")


async def test_cart_state_store_option_hint_round_trip_and_pruning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(상태) set_last_reco(option_hints=) 후 get_option_hint 가 값을 주고, 상한 프루닝으로 빠진
    pid 의 힌트는 사라지며, pg-profile 저장 값에는 옵션명이 없다."""
    store = CartStateStore()

    await store.set_last_reco(
        "s",
        [(1, "상품1"), (2, "상품2")],
        option_hints={
            1: OptionHint(names=("레드", "블루"), total=2),
            2: OptionHint(names=("M",), total=1),
        },
    )

    assert await store.get_option_hint("s", 1) == OptionHint(names=("레드", "블루"), total=2)
    assert await store.get_option_hint("s", 2) == OptionHint(names=("M",), total=1)
    assert await store.get_option_hint("s", 999) is None

    # pg-profile 에 저장된 값에는 옵션명이 없다 — 상품 원본 컬럼 사본 금지 규칙(CLAUDE.md).
    raw = await store._store.aget(store._ns("s"), "last_reco")
    dumped = repr(raw.value)
    assert "레드" not in dumped and "블루" not in dumped

    # 상한 프루닝 — product 1 을 상한 밖으로 밀어내면 힌트도 함께 사라진다.
    monkeypatch.setattr(get_settings(), "last_reco_max", 1)
    await store.set_last_reco("s", [(3, "상품3")])  # 힌트 없이 새 항목만 추가

    assert await store.get_option_hint("s", 1) is None  # capped 밖으로 밀려나 프루닝됨
    assert await store.get_option_hint("s", 2) is None


async def test_cart_state_store_option_hint_partial_update_keeps_missing_pids() -> None:
    """힌트가 안 온 pid 의 기존 힌트는 지우지 않는다(부분 갱신) — 이름 캐시와 같은 어조."""
    store = CartStateStore()
    await store.set_last_reco(
        "s", [(1, "상품1")], option_hints={1: OptionHint(names=("레드",), total=1)}
    )

    # 같은 pid 가 다시 승계되는 턴에 힌트를 안 실어도(예: 프로필 랭킹 경로) 기존 힌트가 남는다.
    await store.set_last_reco("s", [(1, "상품1")])

    assert await store.get_option_hint("s", 1) == OptionHint(names=("레드",), total=1)


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


async def test_pending_cart_option_answer_unaffected_by_has_last_reco(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#435 W6] 옵션 되물음(PENDING_CART) 진행 중 턴은 `has_last_reco`(#435 신호)가 True 여도
    상품 전환·옵션 해소 조건이 그대로다 — 새 이름 공급·새 문구 배선은 되물음 흐름에 관여하지 않는다.
    """
    from app.agents.buyer.cart.state import get_cart_store

    request = _req(thread_id="t-pending-435", message="1번이요")
    key = await _thread_key(request, _member())
    store = await get_cart_store()
    # `last_reco` 가 비어 있지 않아 `has_last_reco=True` 가 되는 조건을 만든다.
    await store.set_last_reco(key, [(101, "세탁 세제")])
    await store.set_pending(
        key,
        PendingAdd(product_id=101, quantity=1, options=[CartOption(option_id=1001, name="일반형")]),
    )

    async def add_fn(req):
        return AddToCartResult(success=True, cart_item_id=42)

    monkeypatch.setattr("app.services.spring_client.add_to_cart", add_fn)
    monkeypatch.setattr("app.services.spring_client.get_cart", _empty_cart())

    llm = _PromptCapturingLLM({"cart": {"productId": None, "optionId": 1001, "quantity": 1}})
    events = await _collect(run_buyer_turn(request, _member(), llm=llm))

    assert next(e for e in events if e["type"] == "action")["data"]["type"] == "CART_ADDED"
    assert await store.get_pending(key) is None


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


async def test_ordinal_span_round_trips_and_degrades_defensively() -> None:
    """[#571] `ordinal_span` 저장·조회 왕복 / 키 없음·`bool`·범위 밖 값이면 `0` 으로 degrade /
    기존 호출부(인자 미전달)는 `0`.

    degrade 방향이 `turn_count` 와 **반대**다 — 여기서는 "모르면 순번을 쓰지 않는다"(0)가
    안전한 쪽이다(순번을 켜는 신호를 못 믿는 값으로 그대로 쓰면 오담기로 이어진다, §2 결정 2).
    """
    from app.agents.buyer.cart import state as cart_state
    from app.agents.buyer.cart.state import CartStateStore

    cart_state.reset_cart_store()
    store = CartStateStore()

    # 왕복 — 넘긴 값이 그대로 읽힌다.
    await store.set_last_reco("k1", [(1, "a"), (2, "b")], ordinal_span=2)
    assert (await store.get_last_reco_state("k1")).ordinal_span == 2

    # 기존 호출부(인자 미전달)는 0 — `option_hints` 와 같은 어조(키워드 인자 추가만).
    await store.set_last_reco("k2", [(1, "a")])
    assert (await store.get_last_reco_state("k2")).ordinal_span == 0

    # 키 자체가 없으면(구버전 인스턴스가 쓴 행) 0 으로 degrade.
    await store._store.aput(
        ("buyer_cart_v2", "k3"), "last_reco", {"product_ids": [1], "turn_count": 1}
    )
    assert (await store.get_last_reco_state("k3")).ordinal_span == 0

    # bool 은 int 의 서브클래스라 명시적으로 배제 — 0 으로 degrade.
    await store._store.aput(
        ("buyer_cart_v2", "k4"),
        "last_reco",
        {"product_ids": [1], "turn_count": 1, "ordinal_span": True},
    )
    assert (await store.get_last_reco_state("k4")).ordinal_span == 0

    # 범위 밖 값(len(items) 초과)도 못 믿을 값이므로 0 으로 degrade.
    await store._store.aput(
        ("buyer_cart_v2", "k5"),
        "last_reco",
        {"product_ids": [1], "turn_count": 1, "ordinal_span": 99},
    )
    assert (await store.get_last_reco_state("k5")).ordinal_span == 0


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
    from app.agents.buyer.recommendation.decompose import (
        _SYSTEM_WITH_SCREEN_DEDICATED_UNDERSPECIFIED,
    )

    request = _screen_request("이거 담아줘", "t-nonpending-screen")

    llm = _PromptCapturingLLM({"cart": {"productId": 501, "quantity": 1}})
    await _collect(run_buyer_turn(request, _member(), llm=llm))

    assert "SCREEN: {" in llm.user
    assert '"순번": 1' in llm.user and '"순번": 2' in llm.user
    # #463 후보 프롬프트는 #430의 빈 semanticQuery 문장만 빼고 화면 규칙은 보존한다.
    assert llm.system == _SYSTEM_WITH_SCREEN_DEDICATED_UNDERSPECIFIED
