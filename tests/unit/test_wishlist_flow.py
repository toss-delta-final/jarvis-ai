"""찜 추가·해제 흐름 (이슈 #117, 패킷 §5.4) — `stream_wishlist_add`/`stream_wishlist_remove`
대상 해소·오류 매핑·경로 B 가드·배선.

Spring 이 아직 I-26/I-27/I-28 을 구현하지 않아 실호출 통합 테스트는 하지 않는다(상대가 없다).
주입 fn 으로 단위 테스트한다(`test_cart_remove.py` 와 같은 패턴).
"""

from __future__ import annotations

import json

import pytest

from app.agents.buyer.cart.graph import stream_cart_add
from app.agents.buyer.cart.state import CartStateStore
from app.agents.buyer.cart.wishlist import stream_wishlist_add, stream_wishlist_remove
from app.agents.buyer.recommendation.state import CartIntent
from app.core.auth import Identity
from app.core.config import get_settings
from app.schemas.spring import WishlistAddResult, WishlistItem, WishlistView
from app.services.spring_client import (
    SpringUnavailableError,
    WishlistDuplicate,
    WishlistError,
    WishlistNotFound,
    WishlistProductNotFound,
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


def _wishlist_item(product_id: int, name: str) -> WishlistItem:
    return WishlistItem(product_id=product_id, name=name, purchasable=True)


def _wishlist(*items: WishlistItem):
    async def _get(user_id):
        return WishlistView(items=list(items))

    return _get


# ─────────── stream_wishlist_add ───────────


async def test_wishlist_add_guest_makes_zero_internal_calls() -> None:
    async def add_wishlist_fn(request):
        raise AssertionError("게스트인데 add_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_add(
            identity=_guest(),
            cart=CartIntent(product_id=1, quantity=1),
            settings=get_settings(),
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    assert not _actions(events)


async def test_wishlist_add_anon_makes_zero_internal_calls() -> None:
    async def add_wishlist_fn(request):
        raise AssertionError("익명인데 add_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_add(
            identity=_anon(),
            cart=CartIntent(product_id=1, quantity=1),
            settings=get_settings(),
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]


async def test_wishlist_add_success() -> None:
    async def add_wishlist_fn(request):
        return WishlistAddResult(success=True, product_id=request.product_id)

    events = await _collect(
        stream_wishlist_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            settings=get_settings(),
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "WISHLIST_ADDED"


async def test_wishlist_add_duplicate_ends_as_added_not_failure() -> None:
    async def add_wishlist_fn(request):
        raise WishlistDuplicate()

    events = await _collect(
        stream_wishlist_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            settings=get_settings(),
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "WISHLIST_ADDED"
    assert "이미" in action["message"]


async def test_wishlist_add_product_not_found_maps_reason() -> None:
    async def add_wishlist_fn(request):
        raise WishlistProductNotFound()

    events = await _collect(
        stream_wishlist_add(
            identity=_member(),
            cart=CartIntent(product_id=999, quantity=1),
            settings=get_settings(),
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "WISHLIST_ADD_FAILED"
    assert action["reason"] == "PRODUCT_NOT_FOUND"


async def test_wishlist_add_error_maps_to_wishlist_error() -> None:
    async def add_wishlist_fn(request):
        raise WishlistError("boom")

    events = await _collect(
        stream_wishlist_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            settings=get_settings(),
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "WISHLIST_ADD_FAILED"
    assert action["reason"] == "WISHLIST_ERROR"


async def test_wishlist_add_unresolved_product_id_asks_and_skips_call() -> None:
    async def add_wishlist_fn(request):
        raise AssertionError("productId 미해소인데 add_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_add(
            identity=_member(),
            cart=CartIntent(product_id=None, quantity=1),
            settings=get_settings(),
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    assert not _actions(events)


async def test_wishlist_add_rejects_product_outside_allowed_ids() -> None:
    """경로 B 가드 — allowed_product_ids 밖의 productId 는 internal 호출 없이 되물음(보안 성격)."""
    called = False

    async def add_wishlist_fn(request):
        nonlocal called
        called = True

    events = await _collect(
        stream_wishlist_add(
            identity=_member(),
            cart=CartIntent(product_id=999, quantity=1),
            settings=get_settings(),
            allowed_product_ids={1, 2, 3},
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    assert called is False
    assert _types(events) == ["token", "done"]


async def test_wishlist_add_allows_product_inside_allowed_ids() -> None:
    async def add_wishlist_fn(request):
        return WishlistAddResult(success=True, product_id=request.product_id)

    events = await _collect(
        stream_wishlist_add(
            identity=_member(),
            cart=CartIntent(product_id=2, quantity=1),
            settings=get_settings(),
            allowed_product_ids={1, 2, 3},
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    assert _actions(events)[0]["type"] == "WISHLIST_ADDED"


async def test_wishlist_add_action_never_carries_product_id() -> None:
    """경로 B — 어떤 찜 action 에도 productId 가 실리지 않는다."""

    async def add_wishlist_fn(request):
        return WishlistAddResult(success=True, product_id=request.product_id)

    events = await _collect(
        stream_wishlist_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            settings=get_settings(),
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    action = _actions(events)[0]
    assert "productId" not in action


# ─────────── stream_wishlist_remove ───────────


async def test_wishlist_remove_guest_makes_zero_internal_calls() -> None:
    async def get_wishlist_fn(user_id):
        raise AssertionError("게스트인데 get_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_remove(
            identity=_guest(),
            cart=CartIntent(product_id=1),
            message="찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=get_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]


async def test_wishlist_remove_resolves_by_context_product_id() -> None:
    """cart.product_id 가 목록 안에 있으면(문맥에서 이미 확정) 이름 매칭 없이도 그것으로 해소."""
    remove_calls: list[int] = []

    async def remove_wishlist_fn(product_id, *, user_id):
        remove_calls.append(product_id)

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=20),
            message="이거 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert remove_calls == [20]
    assert _actions(events)[0]["type"] == "WISHLIST_REMOVED"


async def test_wishlist_remove_resolves_single_name_match() -> None:
    remove_calls: list[int] = []

    async def remove_wishlist_fn(product_id, *, user_id):
        remove_calls.append(product_id)

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="이어폰 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert remove_calls == [10]
    assert _actions(events)[0]["type"] == "WISHLIST_REMOVED"


async def test_wishlist_remove_ambiguous_name_asks() -> None:
    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("모호한데 remove_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="파우치 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(
                _wishlist_item(10, "파우치 블루"), _wishlist_item(20, "파우치 레드")
            ),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "파우치 블루" in token_text and "파우치 레드" in token_text


async def test_wishlist_remove_single_item_auto_resolves() -> None:
    remove_calls: list[int] = []

    async def remove_wishlist_fn(product_id, *, user_id):
        remove_calls.append(product_id)

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert remove_calls == [10]
    assert _actions(events)[0]["type"] == "WISHLIST_REMOVED"


async def test_wishlist_remove_empty_list_says_empty() -> None:
    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("빈 목록인데 remove_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]


async def test_wishlist_remove_not_found_ends_as_removed_not_failure() -> None:
    async def remove_wishlist_fn(product_id, *, user_id):
        raise WishlistNotFound()

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=10),
            message="이어폰 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "WISHLIST_REMOVED"
    assert "이미" in action["message"]


async def test_wishlist_remove_get_wishlist_failure_maps_to_remove_failed() -> None:
    """I-28 어댑터는 4xx/5xx/도달 불가/스키마 불일치를 전부 SpringUnavailableError 로 낸다."""

    async def get_wishlist_fn(user_id):
        raise SpringUnavailableError("boom")

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=10),
            message="이어폰 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=get_wishlist_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "WISHLIST_REMOVE_FAILED"
    assert action["reason"] == "WISHLIST_ERROR"


async def test_wishlist_remove_error_maps_to_remove_failed() -> None:
    async def remove_wishlist_fn(product_id, *, user_id):
        raise WishlistError("boom")

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=10),
            message="이어폰 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "WISHLIST_REMOVE_FAILED"
    assert action["reason"] == "WISHLIST_ERROR"


async def test_wishlist_remove_action_never_carries_product_id() -> None:
    async def remove_wishlist_fn(product_id, *, user_id):
        return None

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=10),
            message="이어폰 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    action = _actions(events)[0]
    assert "productId" not in action


# ─────────── stream_cart_add 배선 ───────────


async def test_stream_cart_add_delegates_to_wishlist_add_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "wishlist_enabled", True)
    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("찜 판정인데 add_fn 이 호출됐다")

    async def add_wishlist_fn(request):
        return WishlistAddResult(success=True, product_id=request.product_id)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-add-delegates-wishlist",
            settings=get_settings(),
            message="이거 찜해줘",
            add_fn=add_fn,
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    assert _actions(events)[0]["type"] == "WISHLIST_ADDED"


async def test_stream_cart_add_delegates_to_wishlist_remove_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "wishlist_enabled", True)
    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("찜 판정인데 add_fn 이 호출됐다")

    async def remove_wishlist_fn(product_id, *, user_id):
        return None

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=10, quantity=1),
            cart_store=store,
            thread_key="m:t-add-delegates-wishlist-remove",
            settings=get_settings(),
            message="찜 빼줘",
            add_fn=add_fn,
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert _actions(events)[0]["type"] == "WISHLIST_REMOVED"


async def test_stream_cart_add_wishlist_degrade_notice_unchanged_when_flag_off() -> None:
    """wishlist_enabled=False(기본) 면 여전히 라운드 2 의 degrade 안내 그대로 나간다."""
    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("degrade 인데 add_fn 이 호출됐다")

    async def add_wishlist_fn(request):
        raise AssertionError("플래그 off 인데 add_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-add-wishlist-degrade",
            settings=get_settings(),
            message="이거 찜해줘",
            add_fn=add_fn,
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "찜" in token_text
    assert not _actions(events)
