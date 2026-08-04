"""장바구니 삭제 흐름 (이슈 #116, 패킷 §5.3) — `stream_cart_remove` 대상 해소·오류 매핑·배선.

Spring 이 아직 I-24 를 구현하지 않아 실호출 통합 테스트는 하지 않는다(상대가 없다). `get_cart_fn`/
`delete_fn` 주입으로 단위 테스트한다(`stream_cart_add` 의 add_fn/get_cart_fn 주입 패턴과 동일).
"""

from __future__ import annotations

import json

import pytest

from app.agents.buyer.cart.graph import stream_cart_add
from app.agents.buyer.cart.remove import stream_cart_remove
from app.agents.buyer.cart.state import CartStateStore, PendingAdd
from app.agents.buyer.recommendation.state import CartIntent
from app.core.auth import Identity
from app.core.config import get_settings
from app.schemas.spring import AddToCartResult, CartOption, CartView, CartViewItem
from app.services.spring_client import CartError, CartItemNotFound, SpringUnavailableError


def _member() -> Identity:
    return Identity(user_id="123", is_guest=False, seller_id=None, subject="123")


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


def _item(cart_item_id: int, product_id: int, name: str) -> CartViewItem:
    return CartViewItem(cart_item_id=cart_item_id, product_id=product_id, product_name=name)


# ─────────── 대상 해소 ───────────


async def test_remove_all_marker_deletes_every_item() -> None:
    store = CartStateStore()
    deleted: list[int] = []

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="전부 빼줘",
            cart_store=store,
            thread_key="m:t-remove-all",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "케이스")),
            delete_fn=delete_fn,
        )
    )
    assert sorted(deleted) == [1, 2]
    actions = _actions(events)
    assert len(actions) == 2
    assert all(a["type"] == "CART_REMOVED" for a in actions)
    assert _types(events)[-1] == "done"


@pytest.mark.parametrize("message", ["전부 빼줘", "다 빼줘", "모두 빼줘"])
async def test_remove_all_marker_variants_still_delete_every_item(message: str) -> None:
    """ "전부"·"다"·"모두" 를 동작 구로 좁힌 뒤에도(라운드 3 리뷰 F-1) 기존 전체 삭제 표지는
    그대로 전체 삭제로 동작해야 한다(회귀 방지)."""
    store = CartStateStore()
    deleted: list[int] = []

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message=message,
            cart_store=store,
            thread_key=f"m:t-remove-all-{message}",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "케이스")),
            delete_fn=delete_fn,
        )
    )
    assert sorted(deleted) == [1, 2]
    assert _types(events)[-1] == "done"


async def test_remove_all_marker_does_not_match_jeonbuteo_substring() -> None:
    """ "전부"는 "전부터"의 부분 문자열이라 "전부터 쓰던 거 빼줘"가 장바구니 전체를 지우는
    사고가 재현됐다(라운드 3 리뷰 F-1). 최종 판정이 무엇이든(되물음이든 이름 매칭이든) 전 항목
    삭제만 아니면 된다 — 이 발화는 이름 매칭도 "방금" 표지도 없고 항목이 2건이라 되물음으로
    끝난다."""
    store = CartStateStore()
    deleted: list[int] = []

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="전부터 쓰던 거 빼줘",
            cart_store=store,
            thread_key="m:t-remove-not-all",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "세제")),
            delete_fn=delete_fn,
        )
    )
    assert sorted(deleted) != [1, 2]
    assert deleted == []
    assert _types(events) == ["token", "done"]


def test_resolve_remove_targets_jeonbuteo_does_not_resolve_to_all() -> None:
    """`_resolve_remove_targets` 직접 호출로도 같은 사실을 고정한다(리뷰가 재현한 그 호출)."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "세제")]
    result = _resolve_remove_targets("전부터 쓰던 거 빼줘", items, get_settings(), None)
    assert result != items
    assert result is None


# ─────────── 대상 해소 — 2차 리뷰(Codex) 지적 2·3: 부정·대조 표지(문장 전체 검사) ───────────


def test_resolve_remove_targets_excluded_all_marker_falls_through_to_name_match() -> None:
    """ "전부 빼지는 말고 이어폰만 빼줘" — "전부 빼"가 매칭돼도 문장에 대조 표지("말고")가
    있으면 전체 삭제 규칙을 건너뛰고 이름 매칭으로 내려가 이어폰만 골라야 한다(2차 리뷰
    지적 2, 재현: 고치기 전엔 [1, 2] 전체 삭제)."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "세제")]
    result = _resolve_remove_targets("전부 빼지는 말고 이어폰만 빼줘", items, get_settings(), None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [1]


def test_resolve_remove_targets_excluded_recent_item_asks_instead_of_deleting_it() -> None:
    """ "방금 담은 거 말고 다른 거 빼줘" — "방금"이 매칭돼도 대조 표지("말고")가 있으면
    "방금 담은 거" 규칙을 건너뛴다. 이름도 없고 항목이 2건이라 되물음(None)이어야 한다
    (2차 리뷰 지적 3, 재현: 고치기 전엔 사용자가 제외한 바로 그 상품 [2]가 삭제됨)."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets
    from app.agents.buyer.cart.state import LastAdd

    items = [_item(1, 10, "이어폰"), _item(2, 20, "세제")]
    result = _resolve_remove_targets(
        "방금 담은 거 말고 다른 거 빼줘",
        items,
        get_settings(),
        LastAdd(cart_item_id=2, product_id=20),
    )
    assert result is None


async def test_remove_excluded_all_marker_deletes_only_named_item() -> None:
    """`stream_cart_remove` 수준에서도 같은 사실 — 전체가 아니라 이름이 지목된 1건만 지운다."""
    store = CartStateStore()
    deleted: list[int] = []

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="전부 빼지는 말고 이어폰만 빼줘",
            cart_store=store,
            thread_key="m:t-remove-excluded-all",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "세제")),
            delete_fn=delete_fn,
        )
    )
    assert deleted == [1]
    assert _actions(events)[0]["type"] == "CART_REMOVED"


def test_resolve_remove_targets_all_marker_still_works_without_negation() -> None:
    """부정·대조 표지가 없으면 전체 삭제 규칙은 그대로 동작한다(회귀 방지)."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "세제")]
    result = _resolve_remove_targets("전부 빼줘", items, get_settings(), None)
    assert result is not None
    assert sorted(item.cart_item_id for item in result) == [1, 2]


def test_resolve_remove_targets_recent_marker_still_works_without_negation() -> None:
    """부정·대조 표지가 없으면 "방금 담은 거" 규칙은 그대로 동작한다(회귀 방지)."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets
    from app.agents.buyer.cart.state import LastAdd

    items = [_item(1, 10, "이어폰"), _item(2, 20, "세제")]
    result = _resolve_remove_targets(
        "방금 담은 거 빼줘", items, get_settings(), LastAdd(cart_item_id=2, product_id=20)
    )
    assert result is not None
    assert [item.cart_item_id for item in result] == [2]


# ─────────── 대상 해소 — 라운드 10: 접두 부정("안"/"못")이 remove.py 안전장치에도 적용된다 ───────────


def test_resolve_remove_targets_prefix_negated_recent_item_asks_instead_of_deleting_it() -> None:
    """재현 1 — "방금 담은 건 안 빼도 되고, 저번에 산 것도 빼줘": 라운드 9 는 접두 부정을
    `intent_guard.py` 에만 넣어 `remove.py` 의 "방금 담은 거" 가드는 여전히 어미형만 봤다.
    사용자가 "빼지 말라"고 명시한 바로 그 항목([2])이 삭제되던 사고였다 — 이제 되물음이어야
    한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets
    from app.agents.buyer.cart.state import LastAdd

    items = [_item(1, 10, "이어폰"), _item(2, 20, "세제")]
    result = _resolve_remove_targets(
        "방금 담은 건 안 빼도 되고, 저번에 산 것도 빼줘",
        items,
        get_settings(),
        LastAdd(cart_item_id=2, product_id=20),
    )
    assert result is None


def test_resolve_remove_targets_prefix_negated_all_marker_falls_through_to_name_match() -> None:
    """재현 2 — "안 전부 빼줘도 되고 이어폰만 빼줘": 전체 삭제([1, 2])가 아니라 이름이 지목된
    이어폰만([1]) 해소돼야 한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "세제")]
    result = _resolve_remove_targets(
        "안 전부 빼줘도 되고 이어폰만 빼줘", items, get_settings(), None
    )
    assert result is not None
    assert [item.cart_item_id for item in result] == [1]


def test_resolve_remove_targets_prefix_negated_all_marker_without_name_asks() -> None:
    """재현 3 — "못 전부 빼줘": 전체 삭제([1, 2])가 아니라 이름이 없으므로 되물음이어야 한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "세제")]
    result = _resolve_remove_targets("못 전부 빼줘", items, get_settings(), None)
    assert result is None


def test_resolve_remove_targets_prefix_negation_does_not_falsely_suppress_all_marker() -> None:
    """거짓 억제 방지(핵심) — "안경"의 "안"은 독립 어절이 아니므로(뒤에 "경"이 바로 붙는다)
    부정으로 치지 않는다. "안경 다 빼줘"는 여전히 전체 삭제([1, 2])여야 한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "세제")]
    result = _resolve_remove_targets("안경 다 빼줘", items, get_settings(), None)
    assert result is not None
    assert sorted(item.cart_item_id for item in result) == [1, 2]


async def test_remove_prefix_negated_recent_item_asks_via_stream() -> None:
    """`stream_cart_remove` 수준에서도 재현 1 과 같은 사실 — delete_fn 이 한 번도 안 불린다."""
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("부정된 '방금 담은 거'인데 delete_fn 이 호출됐다")

    thread_key = "m:t-remove-prefix-negation"
    await store.set_last_add(thread_key, cart_item_id=2, product_id=20)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="방금 담은 건 안 빼도 되고, 저번에 산 것도 빼줘",
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "세제")),
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]


async def test_remove_resolves_single_name_match() -> None:
    store = CartStateStore()
    deleted: list[int] = []

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰 빼줘",
            cart_store=store,
            thread_key="m:t-remove-name",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "케이스")),
            delete_fn=delete_fn,
        )
    )
    assert deleted == [1]
    action = _actions(events)[0]
    assert action["type"] == "CART_REMOVED"
    assert "이어폰" in action["message"]


async def test_remove_ambiguous_name_match_asks_instead_of_deleting() -> None:
    """상품명이 2건 이상에 **실제로 부분 문자열 매칭**되면 되물음 — 실패 action 없이 token +
    done. 매칭은 `상품명 in 발화` 방향이라, 발화가 두 상품명을 모두 포함해야 이 분기가 실제로
    실행된다(라운드 6 리뷰 — 이전 발화 "파우치 빼줘"는 "파우치 블루"/"파우치 레드" 어느 쪽도
    포함하지 않아 이름 매칭이 0건이었고, "신호 없음 + 목록 2건" 경로로 우연히 같은 결과에
    도달했을 뿐이었다. 그 경로는 `test_remove_ambiguous_without_any_signal_asks` 가 이미
    검증한다)."""
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("모호한데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="파우치 블루랑 파우치 레드 빼줘",
            cart_store=store,
            thread_key="m:t-remove-ambiguous",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "파우치 블루"), _item(2, 20, "파우치 레드")),
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "파우치 블루" in token_text and "파우치 레드" in token_text


async def test_remove_recent_marker_resolves_last_add() -> None:
    store = CartStateStore()
    thread_key = "m:t-remove-recent"
    await store.set_last_add(thread_key, cart_item_id=2, product_id=20)
    deleted: list[int] = []

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="방금 담은 거 빼줘",
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "케이스")),
            delete_fn=delete_fn,
        )
    )
    assert deleted == [2]
    assert _actions(events)[0]["type"] == "CART_REMOVED"


async def test_remove_recent_marker_item_missing_from_cart_asks() -> None:
    """last_add 의 항목이 지금 장바구니에 없으면(이미 빠짐) 단건 자동으로 넘어가지 않고 되물음."""
    store = CartStateStore()
    thread_key = "m:t-remove-recent-missing"
    await store.set_last_add(thread_key, cart_item_id=99, product_id=999)

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("해소 실패인데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="방금 담은 거 빼줘",
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]


async def test_remove_single_item_auto_resolves_without_markers() -> None:
    store = CartStateStore()
    deleted: list[int] = []

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="그거 지워줘",
            cart_store=store,
            thread_key="m:t-remove-single",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            delete_fn=delete_fn,
        )
    )
    assert deleted == [1]
    assert _actions(events)[0]["type"] == "CART_REMOVED"


async def test_remove_ambiguous_without_any_signal_asks() -> None:
    """표지도 없고 이름 매칭도 없고 항목이 2건 이상이면 되물음(실패 action 없음)."""
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("미해소인데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="그거 지워줘",
            cart_store=store,
            thread_key="m:t-remove-multi-unresolved",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "케이스")),
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    assert not _actions(events)


# ─────────── 오류 매핑 ───────────


async def test_remove_not_found_ends_as_removed_not_failure() -> None:
    """404 CartItemNotFound → CART_REMOVED(성공 취급) + "이미 빠져 있어요", 실패 action 아님."""
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise CartItemNotFound()

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰 빼줘",
            cart_store=store,
            thread_key="m:t-remove-404",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            delete_fn=delete_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "CART_REMOVED"
    assert "이미 빠져" in action["message"]


async def test_remove_cart_error_maps_to_remove_failed() -> None:
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise CartError("boom")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰 빼줘",
            cart_store=store,
            thread_key="m:t-remove-error",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            delete_fn=delete_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "CART_REMOVE_FAILED"
    assert action["reason"] == "CART_ERROR"


async def test_remove_get_cart_failure_maps_to_remove_failed() -> None:
    store = CartStateStore()

    async def get_cart_fn(*, user_id=None, guest_id=None):
        raise SpringUnavailableError("boom")

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("조회 실패인데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰 빼줘",
            cart_store=store,
            thread_key="m:t-remove-getcart-fail",
            settings=get_settings(),
            get_cart_fn=get_cart_fn,
            delete_fn=delete_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "CART_REMOVE_FAILED"
    assert action["reason"] == "CART_ERROR"


async def test_remove_empty_cart_says_empty() -> None:
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("빈 장바구니인데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰 빼줘",
            cart_store=store,
            thread_key="m:t-remove-empty",
            settings=get_settings(),
            get_cart_fn=_cart(),
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]


async def test_remove_anon_identity_makes_zero_internal_calls() -> None:
    store = CartStateStore()

    async def get_cart_fn(*, user_id=None, guest_id=None):
        raise AssertionError("익명인데 get_cart_fn 이 호출됐다")

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("익명인데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_anon(),
            message="이어폰 빼줘",
            cart_store=store,
            thread_key="m:t-remove-anon",
            settings=get_settings(),
            get_cart_fn=get_cart_fn,
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]


async def test_remove_partial_failure_isolated_and_done_once() -> None:
    """한 항목이 실패해도 나머지는 계속 진행하고 done 은 정확히 1회."""
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        if cart_item_id == 1:
            raise CartError("boom")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="전부 빼줘",
            cart_store=store,
            thread_key="m:t-remove-partial",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "케이스")),
            delete_fn=delete_fn,
        )
    )
    actions = _actions(events)
    assert len(actions) == 2
    types_by_item = {a.get("cartItemId"): a["type"] for a in actions}
    assert types_by_item[1] == "CART_REMOVE_FAILED"
    assert types_by_item[2] == "CART_REMOVED"
    assert _types(events).count("done") == 1
    assert _types(events)[-1] == "done"


# ─────── 대상 해소 — 라운드 11: 이름 매칭(가장 강한 신호)에도 출현 단위 부정 가드 ───────


def test_resolve_remove_targets_negated_only_name_match_asks() -> None:
    """재현(라운드 11 패킷) — "이어폰은 빼지 말고 케이스 빼줘": 장바구니에 이어폰만 있으면
    이름 매칭이 그 부정된 출현 하나뿐이라 되물음이어야 한다(고치기 전엔 [이어폰]이 삭제됐다 —
    사용자가 '빼지 말라'고 명시한 바로 그 상품)."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰")]
    result = _resolve_remove_targets("이어폰은 빼지 말고 케이스 빼줘", items, get_settings(), None)
    assert result is None


def test_resolve_remove_targets_negated_name_still_resolves_other_name() -> None:
    """같은 발화라도 케이스가 실제로 장바구니에 있으면 케이스만 삭제돼야 한다 — 문장 전체
    가드를 이름 매칭에 쓰면 케이스까지 함께 죽어 되물음이 되는데, 그건 틀렸다(라운드 11 패킷
    핵심). 출현 단위 판정이라 이어폰의 부정된 출현은 제외되고 케이스의 부정 없는 출현만
    남는다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "케이스")]
    result = _resolve_remove_targets("이어폰은 빼지 말고 케이스 빼줘", items, get_settings(), None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [2]


async def test_remove_negated_name_match_deletes_only_unnegated_item() -> None:
    """`stream_cart_remove` 수준에서도 같은 사실 — delete_fn 이 케이스(2)에만 불린다."""
    store = CartStateStore()
    deleted: list[int] = []

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰은 빼지 말고 케이스 빼줘",
            cart_store=store,
            thread_key="m:t-remove-negated-name",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "케이스")),
            delete_fn=delete_fn,
        )
    )
    assert deleted == [2]
    action = _actions(events)[0]
    assert action["type"] == "CART_REMOVED"
    assert "케이스" in action["message"]


def test_resolve_remove_targets_single_item_negated_name_asks_not_auto_resolve() -> None:
    """단건 자동 규칙(4번)이 이름 매칭 실패의 뒷문이 되면 안 된다 — 이어폰 1건뿐인 장바구니에서
    "이어폰은 빼지 말고 케이스 빼줘"는 이름 매칭이 부정으로 비어도, 부정 표지가 있으므로 단건
    자동으로 새지 않고 되물음이어야 한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰")]
    result = _resolve_remove_targets("이어폰은 빼지 말고 케이스 빼줘", items, get_settings(), None)
    assert result is None


# ─────────── stream_cart_add 배선 (플래그·last_add) ───────────


async def test_stream_cart_add_delegates_to_remove_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "cart_remove_enabled", True)
    store = CartStateStore()
    deleted: list[int] = []

    async def add_fn(req):
        raise AssertionError("삭제 판정인데 add_fn 이 호출됐다")

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-add-delegates-remove",
            settings=get_settings(),
            message="이어폰 빼줘",
            add_fn=add_fn,
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            delete_fn=delete_fn,
        )
    )
    assert deleted == [1]
    assert _actions(events)[0]["type"] == "CART_REMOVED"


async def test_stream_cart_add_delegates_to_remove_clears_stale_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """라운드 13(head `14aa26b` 리뷰) — 옵션 되물음(pending) 진행 중에 삭제 판정 턴이 끼면
    `stream_cart_remove` 로 위임하기 전에 stale pending 을 지워야 한다. 안 지우면 다음 턴 발화가
    옛 상품의 옵션 답변으로 오해석될 수 있다(`graph.py` 665~668행과 같은 취지)."""
    monkeypatch.setattr(get_settings(), "cart_remove_enabled", True)
    store = CartStateStore()
    thread_key = "m:t-remove-clears-pending"
    await store.set_pending(
        thread_key,
        PendingAdd(
            product_id=99, quantity=1, options=[CartOption(option_id=1, name="레드", extra_price=0)]
        ),
    )

    async def add_fn(req):
        raise AssertionError("삭제 판정인데 add_fn 이 호출됐다")

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        pass

    await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            message="이어폰 빼줘",
            add_fn=add_fn,
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            delete_fn=delete_fn,
        )
    )
    assert await store.get_pending(thread_key) is None


async def test_stream_cart_add_does_not_delegate_when_flag_off() -> None:
    """cart_remove_enabled=False(기본) 면 cart_remove 판정이어도 이 경로가 절대 안 돈다 —
    delete_fn 이 안 불리고 오늘 동작(담기)이 그대로 실행된다."""
    store = CartStateStore()

    async def add_fn(req):
        return AddToCartResult(success=True, cart_item_id=55)

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("플래그 off 인데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-add-no-delegate",
            settings=get_settings(),
            message="장바구니에서 빼줘",
            add_fn=add_fn,
            get_cart_fn=_cart(),
            delete_fn=delete_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "CART_ADDED"


async def test_last_add_stored_only_on_add_success_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cart_remove_enabled=True 이면 담기 성공에서만 last_add 가 저장된다 — 실패·되물음 턴에서는
    바뀌지 않는다."""
    from app.services.spring_client import CartProductNotFound

    monkeypatch.setattr(get_settings(), "cart_remove_enabled", True)
    store = CartStateStore()
    thread_key = "m:t-last-add"

    async def failing_add_fn(req):
        raise CartProductNotFound()

    await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=999, quantity=1),
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            add_fn=failing_add_fn,
            get_cart_fn=_cart(),
        )
    )
    assert await store.get_last_add(thread_key) is None

    async def succeeding_add_fn(req):
        return AddToCartResult(success=True, cart_item_id=321)

    await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            add_fn=succeeding_add_fn,
            get_cart_fn=_cart(),
        )
    )
    last_add = await store.get_last_add(thread_key)
    assert last_add is not None
    assert last_add.cart_item_id == 321 and last_add.product_id == 1


async def test_last_add_not_written_when_flag_off() -> None:
    """cart_remove_enabled=False(기본) 이면 담기 성공에도 last_add 저장소 쓰기가 아예 안 일어난다
    (라운드 3 리뷰 F-2) — 이 값을 읽는 곳은 삭제 흐름뿐이라 기본 배포에서는 아무도 읽지 않는
    쓰기이고, 플래그 off 경로의 저장소 쓰기 횟수는 오늘(이 필드가 없던 시절)과 같아야 한다."""
    store = CartStateStore()
    thread_key = "m:t-last-add-flag-off"

    async def succeeding_add_fn(req):
        return AddToCartResult(success=True, cart_item_id=321)

    await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            add_fn=succeeding_add_fn,
            get_cart_fn=_cart(),
        )
    )
    assert await store.get_last_add(thread_key) is None


# ─────────── 예외 격리 — 2차 리뷰(Codex) 지적 6: 선택적 상태 저장소 장애가 스트림을 죽이면 안 된다 ───────────


class _FailingLastAddStore(CartStateStore):
    async def get_last_add(self, key):
        raise RuntimeError("state store down")


class _FailingSetLastAddStore(CartStateStore):
    async def set_last_add(self, key, cart_item_id, product_id):
        raise RuntimeError("state store down")


async def test_remove_survives_get_last_add_failure_and_still_deletes() -> None:
    """ "방금 담은 거" 표지가 없는 평범한 삭제는 `get_last_add` 읽기가 죽어도 계속 진행돼야
    한다(2차 리뷰 지적 6-2, 재현: 고치기 전엔 첫 SSE 프레임도 나가기 전에 예외가 샌다)."""
    store = _FailingLastAddStore()
    deleted: list[int] = []

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰 빼줘",
            cart_store=store,
            thread_key="m:t-remove-store-down",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            delete_fn=delete_fn,
        )
    )
    assert deleted == [1]
    assert _actions(events)[0]["type"] == "CART_REMOVED"


async def test_remove_get_last_add_failure_with_recent_marker_asks_instead_of_guessing() -> None:
    """읽기 실패 시 "방금 담은 거" 표지가 있었다면 임의로 다른 상품을 고르지 말고 되물음으로
    가야 한다(2차 리뷰 지적 6 — degrade 는 `last_add=None` 과 같은 경로를 탄다)."""
    store = _FailingLastAddStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("읽기 실패 + 방금 표지인데 임의로 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="방금 담은 거 빼줘",
            cart_store=store,
            thread_key="m:t-remove-store-down-recent",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "케이스")),
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]


async def test_add_success_survives_set_last_add_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spring 담기가 이미 성공한 뒤 `set_last_add` 쓰기가 죽어도 CART_ADDED/done 은 그대로
    나가야 한다(2차 리뷰 지적 6-1, 재현: 고치기 전엔 상품은 담겼는데 사용자는 실패를 본다).
    이 경로가 실제로 도는 걸 보려면 `cart_remove_enabled=True` 여야 한다(off 면 애초에
    `set_last_add` 를 안 부른다, 라운드 3 F-2)."""
    monkeypatch.setattr(get_settings(), "cart_remove_enabled", True)
    store = _FailingSetLastAddStore()

    async def add_fn(req):
        return AddToCartResult(success=True, cart_item_id=321)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-add-store-down",
            settings=get_settings(),
            add_fn=add_fn,
            get_cart_fn=_cart(),
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "CART_ADDED"
    assert _types(events)[-1] == "done"
