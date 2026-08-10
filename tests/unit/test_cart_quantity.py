"""장바구니 수량 변경 흐름 (이슈 #285, I-25 §4.13) — `stream_cart_quantity_change` 대상 해소·
오류 매핑·배선.

**함정 1**(404 는 I-24 와 반대로 실패) · **함정 2**(치환 vs 합산, `classify_cart_utterance` 로
고정 — `test_cart_intent_guard.py` 참조) · **함정 3**(목표 수량 미상에 1 기본값 금지)을
회귀로 고정한다. `get_cart_fn`/`change_fn` 주입으로 단위 테스트한다(`test_cart_remove.py` 와
같은 패턴).
"""

from __future__ import annotations

import json

import pytest

from app.agents.buyer.cart.quantity import _resolve_quantity_target, stream_cart_quantity_change
from app.agents.buyer.recommendation.state import CartIntent
from app.core.auth import Identity
from app.core.config import get_settings
from app.schemas.spring import CartView, CartViewItem
from app.services.spring_client import (
    CartError,
    CartItemNotFound,
    CartStockInsufficient,
    SpringUnavailableError,
)


def _member() -> Identity:
    return Identity(user_id="123", is_guest=False, seller_id=None, subject="123")


def _guest() -> Identity:
    return Identity(user_id=None, is_guest=True, seller_id=None, subject="guest-uuid-1")


def _anon() -> Identity:
    return Identity(user_id=None, is_guest=True, seller_id=None, subject=None)


async def _collect(gen) -> list[dict]:
    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:") :].strip()))
    return events


def _types(events) -> list[str]:
    return [e["type"] for e in events]


def _actions(events) -> list[dict]:
    return [e["data"] for e in events if e["type"] == "action"]


def _cart(*items: CartViewItem):
    async def _get(*, user_id=None, guest_id=None):
        return CartView(items=list(items))

    return _get


def _item(
    cart_item_id: int, product_id: int, name: str, option_name: str | None = None
) -> CartViewItem:
    return CartViewItem(
        cart_item_id=cart_item_id, product_id=product_id, product_name=name, option_name=option_name
    )


class _ChangeResult:
    def __init__(self, quantity: int | None) -> None:
        self.success = True
        self.quantity = quantity


# ─────────── 성공 ───────────


async def test_quantity_change_success_reports_final_quantity() -> None:
    calls: list[tuple[int, int]] = []

    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        calls.append((cart_item_id, quantity))
        return _ChangeResult(quantity=5)

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=5),
            message="이어폰 5개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            change_fn=change_fn,
        )
    )
    assert calls == [(1, 5)]
    action = _actions(events)[0]
    assert action["type"] == "CART_QUANTITY_CHANGED"
    assert action["cartItemId"] == 1
    assert action["quantity"] == 5
    assert "5개" in action["message"]
    assert _types(events)[-1] == "done"


async def test_quantity_change_response_missing_quantity_falls_back_to_requested_value() -> None:
    """[패킷 함정 3 인접] 성공 응답에 quantity 키가 없으면(1단계 어댑터가 None 을 담는다) 요청에
    쓴 목표 수량으로 폴백한다 — "수량을 None개로 바꿨어요" 가 나가면 안 된다."""

    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        return _ChangeResult(quantity=None)

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=7),
            message="이어폰 7개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            change_fn=change_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "CART_QUANTITY_CHANGED"
    assert action["quantity"] == 7
    assert "None" not in action["message"]
    assert "7개" in action["message"]


async def test_quantity_change_guest_identity_also_works() -> None:
    """게스트 신원으로도 동작한다(I-25 는 userId|guestId 둘 다 받는다, 삭제와 같다)."""
    calls: list[dict] = []

    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        calls.append({"user_id": user_id, "guest_id": guest_id})
        return _ChangeResult(quantity=quantity)

    events = await _collect(
        stream_cart_quantity_change(
            identity=_guest(),
            cart=CartIntent(target_quantity=2),
            message="이어폰 2개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            change_fn=change_fn,
        )
    )
    assert calls == [{"user_id": None, "guest_id": "guest-uuid-1"}]
    assert _actions(events)[0]["type"] == "CART_QUANTITY_CHANGED"


async def test_quantity_change_anon_identity_asks_login_without_any_call() -> None:
    async def get_cart_fn(*, user_id=None, guest_id=None):
        raise AssertionError("익명인데 get_cart_fn 이 호출됐다")

    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        raise AssertionError("익명인데 change_fn 이 호출됐다")

    events = await _collect(
        stream_cart_quantity_change(
            identity=_anon(),
            cart=CartIntent(target_quantity=3),
            message="3개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=get_cart_fn,
            change_fn=change_fn,
        )
    )
    assert _types(events) == ["token", "done"]


# ─────────── 함정 1 — 404 는 실패다(I-24 와 정반대) ───────────


async def test_quantity_change_item_not_found_is_a_failure_not_success() -> None:
    """[함정 1 회귀 고정] I-24 의 CartItemNotFound 는 CART_REMOVED(성공)로 종료하지만, I-25 는
    CART_QUANTITY_CHANGE_FAILED 여야 한다 — remove.py 를 베껴 성공으로 옮기면 이 테스트가
    깨진다(변이 시험: type 매핑을 반대로 바꾸면 즉시 실패)."""

    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        raise CartItemNotFound()

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=3),
            message="이어폰 3개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            change_fn=change_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "CART_QUANTITY_CHANGE_FAILED"
    assert action["reason"] == "CART_ERROR"
    assert action["type"] != "CART_QUANTITY_CHANGED"


# ─────────── 함정 3 — 목표 수량 미상에 1 기본값 금지 ───────────


async def test_quantity_change_unresolved_target_quantity_asks_without_calling_adapter() -> None:
    """[함정 3 회귀 고정] `cart.target_quantity` 가 None 이면 get_cart_fn·change_fn 어느 것도
    호출하지 않고 곧장 되물음이다 — "수량 바꿔줘"(수량 미상)가 조용히 1개로 치환되면 안 된다."""

    async def get_cart_fn(*, user_id=None, guest_id=None):
        raise AssertionError("목표 수량 미상인데 get_cart_fn 이 호출됐다")

    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        raise AssertionError("목표 수량 미상인데 change_fn 이 호출됐다")

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=None),
            message="수량 바꿔줘",
            settings=get_settings(),
            get_cart_fn=get_cart_fn,
            change_fn=change_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    assert not _actions(events)


# ─────────── 재고 부족 3분기 ───────────


async def test_quantity_change_stock_insufficient_unknown_amount() -> None:
    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        raise CartStockInsufficient(None)

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=50),
            message="이어폰 50개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            change_fn=change_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "CART_QUANTITY_CHANGE_FAILED"
    assert action["reason"] == "STOCK_INSUFFICIENT"
    assert action["message"] == "재고가 부족해 담지 못했어요."


async def test_quantity_change_stock_insufficient_zero_says_sold_out() -> None:
    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        raise CartStockInsufficient(0)

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=5),
            message="이어폰 5개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            change_fn=change_fn,
        )
    )
    action = _actions(events)[0]
    assert action["message"] == "품절된 상품이에요."
    assert action["reason"] == "STOCK_INSUFFICIENT"


async def test_quantity_change_stock_insufficient_reports_remaining_count() -> None:
    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        raise CartStockInsufficient(3)

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=10),
            message="이어폰 10개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            change_fn=change_fn,
        )
    )
    action = _actions(events)[0]
    assert action["message"] == "재고가 3개뿐이에요."


# ─────────── 그 외 오류 매핑 ───────────


async def test_quantity_change_cart_error_maps_to_failed() -> None:
    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        raise CartError("boom")

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=3),
            message="이어폰 3개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            change_fn=change_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "CART_QUANTITY_CHANGE_FAILED"
    assert action["reason"] == "CART_ERROR"


async def test_quantity_change_get_cart_failure_maps_to_failed() -> None:
    async def get_cart_fn(*, user_id=None, guest_id=None):
        raise SpringUnavailableError("boom")

    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        raise AssertionError("조회 실패인데 change_fn 이 호출됐다")

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=3),
            message="이어폰 3개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=get_cart_fn,
            change_fn=change_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "CART_QUANTITY_CHANGE_FAILED"
    assert action["reason"] == "CART_ERROR"


async def test_quantity_change_empty_cart_says_empty() -> None:
    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        raise AssertionError("빈 장바구니인데 change_fn 이 호출됐다")

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=3),
            message="3개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(),
            change_fn=change_fn,
        )
    )
    assert _types(events) == ["token", "done"]


# ─────────── 대상 해소 — 단건, 2건 이상 되물음 ───────────


async def test_quantity_change_ambiguous_name_match_asks_instead_of_picking_one() -> None:
    """2건 이상 매칭되면 임의로 하나를 고르지 않고 되물음이다(패킷 T4 — "임의 선택이 아니다").
    변이 시험: 여기서 `len(qualified/name)==1` 검사를 빼고 첫 항목을 임의 반환하게 바꾸면
    change_fn 이 호출돼 이 테스트가 깨진다."""

    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        raise AssertionError("모호한데 change_fn 이 호출됐다")

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=3),
            message="파우치 블루랑 파우치 레드 3개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "파우치 블루"), _item(2, 20, "파우치 레드")),
            change_fn=change_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "파우치 블루" in token_text and "파우치 레드" in token_text


async def test_quantity_change_single_item_auto_resolves_without_name() -> None:
    calls: list[int] = []

    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        calls.append(cart_item_id)
        return _ChangeResult(quantity=quantity)

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=4),
            message="4개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            change_fn=change_fn,
        )
    )
    assert calls == [1]
    assert _actions(events)[0]["type"] == "CART_QUANTITY_CHANGED"


async def test_quantity_change_multiple_items_no_name_or_signal_asks() -> None:
    """이름도 표지도 없고 항목이 2건 이상이면 되물음 — 단건 자동 뒷문이 되지 않는다."""

    async def change_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        raise AssertionError("미해소인데 change_fn 이 호출됐다")

    events = await _collect(
        stream_cart_quantity_change(
            identity=_member(),
            cart=CartIntent(target_quantity=3),
            message="3개로 바꿔줘",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "케이스")),
            change_fn=change_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    assert not _actions(events)


def test_resolve_quantity_target_name_match_picks_named_item() -> None:
    """[동적 수량 표지 회귀 고정] 이름과 표지 사이에 사용자가 말한 숫자("3개로")가 껴도
    `target_quantity` 를 아는 동적 표지 보강으로 여전히 해소돼야 한다 — 실측으로 발견한
    "이어폰 3개로 바꿔줘"가 정적 표지만으로는 매칭 실패했던 결함의 회귀 테스트."""
    items = [_item(1, 10, "이어폰"), _item(2, 20, "케이스")]
    result = _resolve_quantity_target("이어폰 3개로 바꿔줘", items, get_settings(), 3)
    assert result is not None
    assert result.cart_item_id == 1


def test_resolve_quantity_target_no_all_or_recent_rule_exists() -> None:
    """[패킷 T4 명시 제외] `remove.py` 의 "전체 삭제"·"방금 담은 거" 규칙은 옮기지 않는다 —
    "전부"류 표지가 있어도 전 항목을 고르지 않고, 이름·표지 없는 일반 해소 규칙(단건 자동/
    되물음)만 적용된다."""
    items = [_item(1, 10, "이어폰"), _item(2, 20, "케이스")]
    result = _resolve_quantity_target("전부 3개로 바꿔줘", items, get_settings(), 3)
    # "전부"를 대상 확정으로 쓰는 규칙이 없으므로, 이름도 표지도 없는 이 발화는 항목이
    # 2건이라 되물음(None)이어야 한다 — remove.py 였다면 전체 삭제였을 자리다.
    assert result is None


@pytest.mark.parametrize(
    "message,target_quantity", [("3개로 바꿔줘", 3), ("수량 2개로 변경해줘", 2)]
)
async def test_quantity_change_negation_suppresses_target_resolution(
    message: str, target_quantity: int
) -> None:
    """이름 매칭 출현이 부정되면(`negation.matches_name_unnegated`) 그 항목은 후보에서 빠진다 —
    `_resolve_quantity_target` 이 `remove.py` 와 같은 부품을 실제로 쓰는지 고정한다."""
    items = [_item(1, 10, "이어폰")]
    negated = f"이어폰은 말고 {message}"
    result = _resolve_quantity_target(negated, items, get_settings(), target_quantity)
    # 이름이 부정된 채로만 등장하고 다른 신호도 없으므로 단건 자동으로도 못 새고 되물음이다
    # (has_negation 이 참이라 remove.py 규칙 4 와 같은 가드가 걸린다).
    assert result is None
