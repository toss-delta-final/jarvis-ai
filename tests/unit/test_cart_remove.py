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


def _item(
    cart_item_id: int, product_id: int, name: str, option_name: str | None = None
) -> CartViewItem:
    return CartViewItem(
        cart_item_id=cart_item_id, product_id=product_id, product_name=name, option_name=option_name
    )


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


async def test_remove_with_chat_session_keeps_legacy_delete_test_double_compatible() -> None:
    """새 chatSessionId 배선이 기존 delete_fn 주입 시그니처를 깨지 않는다."""
    store = CartStateStore()
    deleted: list[int] = []

    async def legacy_delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰 빼줘",
            cart_store=store,
            thread_key="m:t-legacy-delete",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            delete_fn=legacy_delete_fn,
            chat_session_id="chat-session-1",
        )
    )

    assert deleted == [1]
    assert _actions(events)[0]["type"] == "CART_REMOVED"


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


def test_resolve_remove_targets_recent_marker_yields_to_unrelated_named_product() -> None:
    """재현·수정 확인 — "이어폰케이스 방금 담은 거 빼줘"에서 "방금"이 매칭돼도, 사용자가 댄
    이름("이어폰케이스")과 무관한 최근 담은 항목(파우치)을 대신 지우면 안 된다. 1번(전체
    삭제)·4번(단건 자동)이 이미 받는 `name_mentioned` 가드를 3번("방금 담은 거")도 받아야
    한다 — 빠져 있으면 사용자가 말하지 않은 상품이 삭제된다(재현 확인)."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets
    from app.agents.buyer.cart.state import LastAdd

    items = [_item(1, 10, "이어폰"), _item(2, 20, "파우치")]
    result = _resolve_remove_targets(
        "이어폰케이스 방금 담은 거 빼줘",
        items,
        get_settings(),
        LastAdd(cart_item_id=2, product_id=20),
    )
    assert result is None


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


async def test_remove_endpoint_not_deployed_404_does_not_yield_false_success() -> None:
    """[라운드 23] Spring 에 I-24 엔드포인트가 아직 없어서 나는 404(라우트 없음, `error.code`
    가 계약(`CART_ITEM_NOT_FOUND`)과 다르거나 없음)를 `CartItemNotFound` 로 낙성하면 이 그래프가
    `CART_REMOVED` + "이미 빠져 있어요"(성공 안내)로 끝낸다 — 배포 전 호출이 조용히 거짓 성공을
    낸다. `spring_client.delete_cart_item` 이 code 를 확인해 이 경우 `CartError` 로 낙성하도록
    고쳤으니, 이 그래프도 성공 안내가 아니라 `CART_REMOVE_FAILED` 로 끝나야 한다."""
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        # spring_client.delete_cart_item 이 code 불일치 404 에서 실제로 내는 예외
        # (CartItemNotFound 가 아니라 CartError, 위 delete_cart_item 구현 참조).
        raise CartError("delete_cart_item 실패: 404 None")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰 빼줘",
            cart_store=store,
            thread_key="m:t-remove-endpoint-not-deployed",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            delete_fn=delete_fn,
        )
    )
    action = _actions(events)[0]
    assert action["type"] == "CART_REMOVE_FAILED"
    assert "이미 빠져" not in action["message"]


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


# ─── 대상 해소 — 라운드 15(head `0b33e06` 리뷰 B): 이름 매칭 경계(조사 허용) ───


def test_resolve_remove_targets_name_inside_word_does_not_match() -> None:
    """재현(라운드 15 패킷) — 장바구니에 "이어폰"만 있을 때 "이어폰케이스 빼줘"는 다른 낱말
    ("이어폰케이스") 안에 파묻힌 "이어폰"을 매칭해선 안 된다(뒤 문자 "케"가 공백·문장부호·
    조사 어느 것도 아니므로 경계 위반). 이름 매칭이 비어도 단건 자동(4번)이 뒷문이 되지
    않도록(이름을 대려는 시도가 있었으므로) 되물음이어야 한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰")]
    result = _resolve_remove_targets("이어폰케이스 빼줘", items, get_settings(), None)
    assert result is None


def test_resolve_remove_targets_glued_name_still_none_with_unrelated_second_item() -> None:
    """장바구니에 무관한 2번째 항목이 있어도(경계 위반은 항목 수와 무관) 결과는 여전히
    None 이어야 한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "파우치")]
    result = _resolve_remove_targets("이어폰케이스 빼줘", items, get_settings(), None)
    assert result is None


def test_resolve_remove_targets_glued_name_does_not_leak_via_other_item_name() -> None:
    """재현·수정 확인 — 장바구니에 "이어폰"과 우연히 "케이스"가 **같이** 있으면, 이전에는
    `_has_valid_name_trailing` 의 `other_names` 종결(조사 없이도 다른 항목 이름으로 시작하면
    유효)이 "이어폰케이스"의 "케이스" 부분까지 다른 상품명의 시작으로 오인해 "이어폰"이
    사용자가 말하지 않은 상품인데도 삭제됐다(장바구니 구성에 따라 결과가 갈리는 데이터 의존적
    오삭제). 조사·filler 를 실제로 소비하지 않은 종결은 무효로 처리해 이 경로를 막는다 —
    결과는 다른 조합과 마찬가지로 되물음(None)이어야 한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "케이스")]
    result = _resolve_remove_targets("이어폰케이스 빼줘", items, get_settings(), None)
    assert result is None


def test_resolve_remove_targets_name_followed_by_particle_still_matches() -> None:
    """회귀(라운드 15 패킷 핵심) — 순진한 경계 검사는 "그 세제를 빼줘"(목적격 조사 "를"이
    이름 바로 뒤에 붙는 정상 발화)를 깨뜨린다. 오른쪽 경계에 조사를 허용해야 계속 매칭된다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 20, "세제")]
    result = _resolve_remove_targets("그 세제를 빼줘", items, get_settings(), None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [1]


def test_resolve_remove_targets_bare_name_still_matches() -> None:
    """회귀 — 뒤가 공백인 온전한 이름 매칭은 그대로 유지된다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰")]
    result = _resolve_remove_targets("이어폰 빼줘", items, get_settings(), None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [1]


def test_resolve_remove_targets_spaced_longer_name_now_asks_instead_of_matching() -> None:
    """라운드 15 는 이 케이스("이어폰 케이스 빼줘"에서 "이어폰" 뒤가 공백이라는 이유만으로
    장바구니의 "이어폰"이 매칭됨)를 "덜 친절한 문구"급 알려진 한계로 미뤘는데, 라운드 17(head
    `6ab47c9` 리뷰)에서 그 판단이 뒤집혔다 — 사용자가 요청하지 않은 "이어폰"이 확인 없이
    삭제되는 파괴적 동작이었기 때문이다. 오른쪽 경계가 "이름 + (조사) + (filler) + 표지"
    형태만 인정하도록 바뀌어, 이제 이 발화는 되물음이어야 한다(이 테스트는 라운드 15 의
    `test_resolve_remove_targets_spaced_longer_name_still_false_matches_known_limitation`
    을 그 자신의 docstring 예고("한계가 없어지면 갱신할 것")대로 뒤집은 것이다)."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰")]
    result = _resolve_remove_targets("이어폰 케이스 빼줘", items, get_settings(), None)
    assert result is None


def test_resolve_remove_targets_boundary_violation_does_not_fall_back_to_single_auto() -> None:
    """단건 자동(4번) 뒷문 방지 재확인 — 이름을 대려는 시도(경계 위반이라도 원문 부분 문자열
    겹침)가 있으면, 장바구니가 1건뿐이고 다른 표지가 전혀 없어도 단건 자동으로 새지 않는다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰")]
    result = _resolve_remove_targets("이어폰케이스 빼줘", items, get_settings(), None)
    assert result is None
    # 대조 — 이름이 전혀 없으면(무신호) 단건 자동은 그대로 동작한다(회귀 없음).
    result_no_name = _resolve_remove_targets("그거 지워줘", items, get_settings(), None)
    assert result_no_name is not None
    assert [item.cart_item_id for item in result_no_name] == [1]


async def test_remove_name_inside_word_asks_via_stream() -> None:
    """`stream_cart_remove` 수준에서도 같은 사실 — delete_fn 이 한 번도 안 불린다."""
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("경계 위반 매칭인데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰케이스 빼줘",
            cart_store=store,
            thread_key="m:t-remove-boundary",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]


# ─── 라운드 17(head `6ab47c9` 리뷰): 이름 뒤 "조사+filler+표지" 만 유효 매칭으로 인정 ───


def test_resolve_remove_targets_spaced_name_followed_by_other_word_asks() -> None:
    """고쳐지는 것(라운드 17 패킷 핵심) — "이어폰 케이스 빼줘"(장바구니에 이어폰만 있음)는
    사용자가 요청하지 않은 "이어폰"을 확인 없이 삭제하는 파괴적 동작이었다(재현). 이제
    "케이스"가 조사도 filler 도 표지도 아니라 "이어폰"의 그 출현은 무효 → 이름 매칭 0건 →
    (라운드15의 단건 자동 뒷문 가드까지 이어져) 되물음."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰")]
    result = _resolve_remove_targets("이어폰 케이스 빼줘", items, get_settings(), None)
    assert result is None


def test_resolve_remove_targets_name_followed_by_filler_then_marker_still_matches() -> None:
    """깨지면 안 되는 것 — "이어폰 좀 빼줘"는 "좀"이 filler 낱말이라 소비되고 뒤에 표지
    "빼줘"가 오므로 여전히 매칭돼야 한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰")]
    result = _resolve_remove_targets("이어폰 좀 빼줘", items, get_settings(), None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [1]


def test_resolve_remove_targets_particle_then_marker_still_matches() -> None:
    """회귀(라운드 15) — 목적격 조사 "를"이 이름 바로 뒤에 붙고 곧장 표지가 오는 정상 발화는
    라운드 17 의 새 오른쪽 경계 검사로도 계속 매칭돼야 한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 20, "세제")]
    result = _resolve_remove_targets("그 세제를 빼줘", items, get_settings(), None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [1]


def test_resolve_remove_targets_ambiguous_listing_with_particle_still_asks() -> None:
    """깨지면 안 되는 것(핵심 회귀) — "파우치 블루랑 파우치 레드 빼줘"는 "파우치 블루" 뒤가
    조사("랑") 다음 다른 실제 항목 이름("파우치 레드")으로 이어진다. 이 다른-항목-이름 허용이
    없으면 "파우치 블루"만 무효로 걸러지고 "파우치 레드"만 단독 매칭돼 모호 판정이 깨진다
    (직접 검증한 시나리오) — 둘 다 유효해야 모호(되물음)로 남는다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "파우치 블루"), _item(2, 20, "파우치 레드")]
    result = _resolve_remove_targets("파우치 블루랑 파우치 레드 빼줘", items, get_settings(), None)
    assert result is None


def test_resolve_remove_targets_negated_name_with_valid_trailing_still_matches_other() -> None:
    """회귀(라운드 11) — "이어폰은 빼지 말고 케이스 빼줘"에서 "케이스" 뒤는 곧장 표지이므로
    라운드 17 의 오른쪽 경계 검사를 통과하고, "이어폰"은 부정으로 무효화돼 케이스만 남는다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "케이스")]
    result = _resolve_remove_targets("이어폰은 빼지 말고 케이스 빼줘", items, get_settings(), None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [2]


def test_resolve_remove_targets_single_item_no_name_still_auto_resolves() -> None:
    """깨지면 안 되는 것 — 이름을 전혀 대지 않은 "그거 빼줘"는 항목이 1건이면 그대로 단건
    자동이어야 한다(새 오른쪽 경계 검사는 이름이 실제로 매칭 시도될 때만 관여한다)."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "무선 이어폰")]
    result = _resolve_remove_targets("그거 빼줘", items, get_settings(), None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [1]


async def test_remove_spaced_name_followed_by_other_word_asks_via_stream() -> None:
    """`stream_cart_remove` 수준에서도 같은 사실 — delete_fn 이 한 번도 안 불린다."""
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("파괴적 오삭제인데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰 케이스 빼줘",
            cart_store=store,
            thread_key="m:t-remove-spaced-longer-name",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰")),
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]


# ─── 라운드 18(head `f87eb5b` 리뷰 F1): 1번(전체 삭제)에도 name_mentioned 가드 ───


def test_resolve_remove_targets_all_marker_with_named_item_does_not_delete_everything() -> None:
    """재현(라운드 18 패킷 F1) — 장바구니 [이어폰, 파우치]에서 "이어폰 전부 빼줘"는 사용자가
    이름을 댔는데도 라운드 17 이전엔 1번(전체 삭제)이 "전부 빼"만 보고 확정해 이름을 대지 않은
    "파우치"까지 함께 지웠다. `name_mentioned`(라운드 17, 4번과 공용) 가드를 1번에도 적용하면
    2번(이름 매칭)으로 내려가고, "이어폰" 뒤가 "전부"라 라운드 17 오른쪽 경계 규칙상 유효
    매칭이 아니므로(조사·filler·표지·다른 항목 이름 어느 것도 아님) 매칭 0건 → 되물음이다.
    핵심 단언은 "전체 삭제가 아니다"이지, 되물음 문구 자체를 개선하는 것이 아니다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "파우치")]
    result = _resolve_remove_targets("이어폰 전부 빼줘", items, get_settings(), None)
    assert result != items
    assert result is None


async def test_remove_all_marker_with_named_item_asks_via_stream() -> None:
    """`stream_cart_remove` 수준에서도 같은 사실 — delete_fn 이 한 번도 안 불린다(파우치가
    조용히 함께 삭제되는 사고가 재현되지 않는다)."""
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("이름을 댄 전체 삭제인데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰 전부 빼줘",
            cart_store=store,
            thread_key="m:t-remove-all-with-name",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "파우치")),
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]


@pytest.mark.parametrize("message", ["전부 빼줘", "다 빼줘", "모두 빼줘"])
def test_resolve_remove_targets_all_marker_without_any_name_still_deletes_everything(
    message: str,
) -> None:
    """회귀(라운드 18) — 어느 항목 이름도 언급되지 않은 "전부 빼줘"·"다 빼줘"·"모두 빼줘"는
    `name_mentioned` 가 거짓이라 1번(전체 삭제)이 그대로 동작해야 한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "파우치")]
    result = _resolve_remove_targets(message, items, get_settings(), None)
    assert result is not None
    assert sorted(item.cart_item_id for item in result) == [1, 2]


# ─── 라운드 19(head `26f5596` 리뷰): 되물음 문구가 동작 표지를 유도한다 ───


def test_unresolved_notice_lists_names_and_guides_action_marker() -> None:
    """재현(라운드 19 패킷) — 기존 문구("어떤 걸 뺄까요?")는 사용자가 상품명만("이어폰") 답해도
    괜찮다고 오인시킨다. 새 문구는 여전히 담긴 상품명을 모두 나열하면서(기존 단언 유지), 동작
    표지("빼줘")를 포함한 예시로 다음 답을 유도해야 한다 — 그래야 다음 턴에
    `classify_cart_utterance` 가 그 답을 다시 `cart_remove` 로 잡을 수 있다."""
    from app.agents.buyer.cart.remove import _unresolved_notice

    text = _unresolved_notice([_item(1, 10, "이어폰"), _item(2, 20, "케이스")])
    assert "이어폰" in text
    assert "케이스" in text
    assert "빼줘" in text


def test_unresolved_notice_marks_purchase_state() -> None:
    """삭제 되물음 목록도 조회와 **같은 라벨**을 붙인다(#310, REQ-CART-037).

    안 붙이면 같은 장바구니가 질문 방식에 따라 다르게 보인다 — "뭐 있어?"에는 (판매 종료)가
    보이는데 "빼줘"에는 이름만 나온다. 게다가 삭제 문맥은 판매 종료 항목을 빼도록 돕는
    자리라 라벨이 가장 쓸모 있는 곳이다."""
    from app.agents.buyer.cart.remove import _unresolved_notice

    sold_out = _item(2, 20, "린넨 셔츠")
    sold_out.purchase_state = "SOLD_OUT"
    hidden = _item(3, 30, "가죽 지갑")
    hidden.purchase_state = "HIDDEN"

    text = _unresolved_notice([_item(1, 10, "이어폰"), sold_out, hidden])
    assert "린넨 셔츠 (품절)" in text
    assert "가죽 지갑 (판매 종료)" in text
    assert "이어폰," in text  # 상태 없는 항목은 라벨 없이 그대로


def test_unresolved_notice_example_avoids_unavailable_item() -> None:
    """예시로 드는 상품은 **살 수 있는 항목**을 우선한다.

    예시는 "이렇게 답하세요"라고 권하는 자리인데 첫 항목이 품절이면 못 사는 상품을 예시로 들게
    된다. 다만 이 되물음의 목적은 삭제 대상 좁히기라 못 사는 항목도 삭제 후보로는 유효하므로,
    전부 못 사는 경우엔 그대로 첫 항목을 쓴다(예시가 사라지는 게 더 나쁘다)."""
    from app.agents.buyer.cart.remove import _unresolved_notice

    sold_out = _item(1, 10, "린넨 셔츠")
    sold_out.purchase_state = "SOLD_OUT"
    text = _unresolved_notice([sold_out, _item(2, 20, "이어폰")])
    assert "'이어폰 빼줘'" in text


async def test_remove_ambiguous_asks_with_action_marker_guidance_via_stream() -> None:
    """`stream_cart_remove` 수준에서도 같은 사실 — 장바구니 [이어폰, 케이스]에서 표지 없는
    "빼줘"는 되물음이고, 그 문구가 두 상품명과 동작 표지 예시를 모두 담는다(라운드 19 패킷
    재현 1)."""
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("미해소인데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="빼줘",
            cart_store=store,
            thread_key="m:t-remove-dead-end-guidance",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "케이스")),
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "이어폰" in token_text and "케이스" in token_text
    assert "빼줘" in token_text


# ─── 옵션만 다른 동명 상품에서 되물음이 막다른 루프가 되지 않는다 ───


def test_unresolved_notice_disambiguates_same_name_different_option() -> None:
    """재현 — product_name 이 같고 option_name 만 다르면(이어폰(블랙)·이어폰(화이트))
    `_display_name` 만으로는 문구가 "이어폰, 이어폰"으로 나와 사용자가 어느 쪽인지 구분할 수
    없다. 옵션 구분자가 목록·예시 둘 다에 보여야 한다."""
    from app.agents.buyer.cart.remove import _unresolved_notice

    text = _unresolved_notice([_item(1, 10, "이어폰", "블랙"), _item(2, 10, "이어폰", "화이트")])
    assert "블랙" in text
    assert "화이트" in text


def test_resolve_remove_targets_same_name_bare_repeat_still_ambiguous() -> None:
    """`_unresolved_notice` 가 예시로 준 맨이름을 그대로 반복하면(옵션 없이) 여전히 모호하다 —
    이건 회귀가 아니라 의도된 동작이다(단건으로 임의로 좁히면 사용자가 답하지 않은 옵션이
    삭제된다)."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰", "블랙"), _item(2, 10, "이어폰", "화이트")]
    result = _resolve_remove_targets("이어폰 빼줘", items, get_settings(), None)
    assert result is None


def test_resolve_remove_targets_option_qualified_name_breaks_the_loop() -> None:
    """루프 탈출이 실제로 되는지가 이 테스트의 요점 — 되물음 문구가 예시로 든 구분자 형태
    ("이어폰 (블랙)")로 답하면 1건으로 좁혀져야 한다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰", "블랙"), _item(2, 10, "이어폰", "화이트")]
    result = _resolve_remove_targets("이어폰 (블랙) 빼줘", items, get_settings(), None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [1]


def test_resolve_remove_targets_option_name_without_parens_also_breaks_the_loop() -> None:
    """괄호 없이 자연스럽게 답해도("이어폰 블랙 빼줘") 좁혀져야 한다 — 안내가 제시하는 형식
    그대로가 아니어도 흔한 답변 형태는 받아준다."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰", "블랙"), _item(2, 10, "이어폰", "화이트")]
    result = _resolve_remove_targets("이어폰 블랙 빼줘", items, get_settings(), None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [1]


async def test_remove_option_qualified_answer_resolves_via_stream() -> None:
    """`stream_cart_remove` 수준의 end-to-end 루프 탈출 재현 — 1턴째 모호 → 2턴째(옵션 구분자
    포함 답) 실제 삭제까지 완료된다."""
    store = CartStateStore()
    deleted: list[int] = []

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰 (블랙) 빼줘",
            cart_store=store,
            thread_key="m:t-remove-option-loop-break",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰", "블랙"), _item(2, 10, "이어폰", "화이트")),
            delete_fn=delete_fn,
        )
    )
    assert deleted == [1]
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_REMOVED" and action["cartItemId"] == 1


def test_resolve_remove_targets_option_present_but_unmentioned_item_unaffected() -> None:
    """옵션이 있는 항목이라도 사용자가 옵션을 말하지 않았고 이름이 유일하면(다른 항목과 겹치지
    않으면) 기존처럼 맨이름만으로 매칭된다 — 회귀 없음."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰", "블랙"), _item(2, 20, "케이스")]
    result = _resolve_remove_targets("케이스 빼줘", items, get_settings(), None)
    assert result is not None
    assert [item.cart_item_id for item in result] == [2]


# ─── 라운드 20(head `5772021` 리뷰): 조사 소비가 리스트 순서가 아니라 최장 일치를 따른다 ───


def test_resolve_remove_targets_listed_names_with_batchim_ask_like_without_batchim() -> None:
    """재현(라운드 20 패킷) — `_consume_prefix` 가 첫 매칭("이")만 소비하면 받침 있는 이름
    ("이어폰") 뒤 "이랑"에서 "랑"이 남아 오른쪽 경계 검사가 깨지고, 사용자가 지목한 "이어폰"이
    조용히 빠진 채 "케이스"만 단독 매칭된다. 최장 일치로 고치면 "이랑"이 통째로 소비되어
    "이어폰"도 유효 매칭이 되고, 둘 다 매칭돼 모호(되물음)로 떨어진다 — 받침 없는 "파우치랑
    세제 빼줘"와 **같은 결과**여야 한다(받침 유무로 갈리지 않는다)."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    with_batchim = [_item(1, 10, "이어폰"), _item(2, 20, "케이스")]
    result_with_batchim = _resolve_remove_targets(
        "이어폰이랑 케이스 빼줘", with_batchim, get_settings(), None
    )
    assert result_with_batchim is None

    without_batchim = [_item(1, 10, "파우치"), _item(2, 20, "세제")]
    result_without_batchim = _resolve_remove_targets(
        "파우치랑 세제 빼줘", without_batchim, get_settings(), None
    )
    assert result_without_batchim is None


def test_resolve_remove_targets_ina_particle_with_batchim_also_asks() -> None:
    """ "이나"(선택 접속조사)도 "이"와 접두 관계라 같은 결함 클래스 — 최장 일치로 같은 결과."""
    from app.agents.buyer.cart.remove import _resolve_remove_targets

    items = [_item(1, 10, "이어폰"), _item(2, 20, "케이스")]
    result = _resolve_remove_targets("이어폰이나 케이스 빼줘", items, get_settings(), None)
    assert result is None


async def test_remove_listed_names_with_batchim_ask_via_stream() -> None:
    """`stream_cart_remove` 수준에서도 같은 사실 — "이어폰"이 조용히 빠진 채 "케이스"만
    삭제되는 사고가 재현되지 않는다(delete_fn 이 한 번도 안 불린다)."""
    store = CartStateStore()

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        raise AssertionError("모호한 목록 삭제인데 delete_fn 이 호출됐다")

    events = await _collect(
        stream_cart_remove(
            identity=_member(),
            message="이어폰이랑 케이스 빼줘",
            cart_store=store,
            thread_key="m:t-remove-listed-batchim",
            settings=get_settings(),
            get_cart_fn=_cart(_item(1, 10, "이어폰"), _item(2, 20, "케이스")),
            delete_fn=delete_fn,
        )
    )
    assert _types(events) == ["token", "done"]


# ─────────── stream_cart_add 배선 (last_add) ───────────
# [라운드 23] 삭제 흐름의 온/오프를 가리던 설정 필드를 삭제했다(사용자 지시) — 판정이 나오면
# 항상 위임한다. "플래그 off" 류 테스트는 삭제하고, 아래는 전부 "항상 위임" 동작을 검증한다.


async def test_stream_cart_add_delegates_to_remove() -> None:
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


async def test_stream_cart_add_delegates_to_remove_clears_stale_pending() -> None:
    """라운드 13(head `14aa26b` 리뷰) — 옵션 되물음(pending) 진행 중에 삭제 판정 턴이 끼면
    `stream_cart_remove` 로 위임하기 전에 stale pending 을 지워야 한다. 안 지우면 다음 턴 발화가
    옛 상품의 옵션 답변으로 오해석될 수 있다(`graph.py` 665~668행과 같은 취지)."""
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


async def test_last_add_stored_only_on_add_success() -> None:
    """담기 성공에서만 last_add 가 저장된다 — 실패·되물음 턴에서는 바뀌지 않는다."""
    from app.services.spring_client import CartProductNotFound

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


async def test_add_success_survives_set_last_add_failure() -> None:
    """Spring 담기가 이미 성공한 뒤 `set_last_add` 쓰기가 죽어도 CART_ADDED/done 은 그대로
    나가야 한다(2차 리뷰 지적 6-1, 재현: 고치기 전엔 상품은 담겼는데 사용자는 실패를 본다)."""
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
