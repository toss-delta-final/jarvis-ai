"""찜 오담기 방어 (이슈 #116·#117, 패킷 §4) — `classify_cart_utterance` 단위 + `stream_cart_add` 배선.

전부 **기본 설정**(`get_settings()`, override 없음)으로 돈다 — 배포되는 기본 조합(표지 목록·
플래그 off)이 실제로 이 테이블을 통과하는지가 이 테스트들의 존재 이유다.
"""

from __future__ import annotations

import pytest

from app.agents.buyer.cart.intent_guard import classify_cart_utterance
from app.agents.buyer.cart.graph import stream_cart_add
from app.agents.buyer.cart.state import CartStateStore
from app.agents.buyer.recommendation.state import CartIntent
from app.core.auth import Identity
from app.core.config import get_settings
from app.schemas.spring import AddToCartResult, CartView


def _member() -> Identity:
    return Identity(user_id="123", is_guest=False, seller_id=None, subject="123")


async def _collect(gen) -> list[dict]:
    import json

    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:") :].strip()))
    return events


def _types(events) -> list[str]:
    return [e["type"] for e in events]


async def _empty_cart(*, user_id=None, guest_id=None):
    return CartView(items=[])


# ─────────── classify_cart_utterance — 패킷 §4.1 적대적 입력표 전 항목 ───────────


@pytest.mark.parametrize(
    "message,expected",
    [
        # 찜 추가
        ("찜해줘", "wishlist_add"),
        ("이거 찜해줘", "wishlist_add"),
        ("찜 해주세요", "wishlist_add"),
        ("찜 목록에 넣어줘", "wishlist_add"),
        ("위시리스트에 추가해줘", "wishlist_add"),
        # 찜은 지시 대상 수식일 뿐 — 담기
        ("찜한 거 장바구니에 담아줘", "cart_add"),
        ("찜해둔 이어폰 담아줘", "cart_add"),
        # 표지 없음 — 담기
        ("장바구니에 담아줘", "cart_add"),
        # 찜 해제
        ("찜 빼줘", "wishlist_remove"),
        ("찜 해제해줘", "wishlist_remove"),
        ("찜에서 빼줘", "wishlist_remove"),
        # 삭제
        ("장바구니에서 빼줘", "cart_remove"),
        ("그거 지워줘", "cart_remove"),
        # "빼고"는 삭제 표지가 아니고 담기 표지("담아")가 강한 신호로 우선한다
        ("하나 빼고 담아줘", "cart_add"),
        # 애매한 질문 — 개입 금지, 오늘 동작(담기) 유지
        ("빼줄 수도 있어?", "cart_add"),
    ],
)
def test_classify_cart_utterance_adversarial_table(message: str, expected: str) -> None:
    assert classify_cart_utterance(message, get_settings()) == expected


def test_classify_wishlist_remove_does_not_leak_to_cart_remove() -> None:
    """ "찜 빼줘"는 cart_remove_markers 의 "빼줘"도 부분 문자열로 동시에 매칭한다 — 찜 해제를
    삭제보다 먼저 판정해 cart_remove 로 새지 않아야 한다(라운드 1 리뷰 지적, ⚠️)."""
    assert classify_cart_utterance("찜 빼줘", get_settings()) == "wishlist_remove"


# ─────────── classify_cart_utterance — 라운드 2 보강 표지 ───────────


@pytest.mark.parametrize(
    "message,expected",
    [
        ("찜해주세요", "wishlist_add"),
        ("찜해 줘", "wishlist_add"),
        ("찜 목록에 추가해줘", "wishlist_add"),
        ("위시리스트에 넣어줘", "wishlist_add"),
        ("제거해줘", "cart_remove"),
        ("빼 주세요", "cart_remove"),
        ("지워 주세요", "cart_remove"),
        ("찜 취소해줘", "wishlist_remove"),
        ("찜에서 지워줘", "wishlist_remove"),
    ],
)
def test_classify_cart_utterance_round2_markers(message: str, expected: str) -> None:
    assert classify_cart_utterance(message, get_settings()) == expected


def test_classify_wishlist_add_suppressed_when_message_mentions_cart() -> None:
    """ "장바구니"가 있으면 찜으로 갈라내지 않는다(삭제 판정에는 영향 없음, §4)."""
    assert classify_cart_utterance("장바구니에 있는 거 찜해줘", get_settings()) == "cart_add"


def test_classify_cart_remove_unaffected_by_cart_mention_suppression() -> None:
    """ "장바구니" 억제는 찜 판정에만 걸리고 삭제 판정은 그대로 통과한다."""
    assert classify_cart_utterance("장바구니에서 빼줘", get_settings()) == "cart_remove"


# ─────────── stream_cart_add 배선 — 찜 오담기 방어 ───────────


async def test_stream_cart_add_wishlist_intent_degrades_without_calling_add_fn() -> None:
    """찜 판정 + wishlist_enabled=False(기본) → token 안내 + done 만. add_fn 은 한 번도 안 불린다."""
    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("wishlist 판정인데 add_fn 이 호출됐다")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-wishlist-add",
            settings=get_settings(),
            message="이거 찜해줘",
            add_fn=add_fn,
            get_cart_fn=_empty_cart,
        )
    )
    assert _types(events) == ["token", "done"]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "찜" in token_text
    assert not any(e["type"] == "action" for e in events)


async def test_stream_cart_add_wishlist_remove_intent_also_degrades() -> None:
    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("wishlist 판정인데 add_fn 이 호출됐다")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-wishlist-remove",
            settings=get_settings(),
            message="찜 빼줘",
            add_fn=add_fn,
            get_cart_fn=_empty_cart,
        )
    )
    assert _types(events) == ["token", "done"]
    assert not any(e["type"] == "action" for e in events)


async def test_stream_cart_add_wishlist_reference_marker_still_adds() -> None:
    """ "찜한 거 담아줘" — 오늘과 동일하게 담긴다(CART_ADDED)."""
    store = CartStateStore()

    async def add_fn(req):
        return AddToCartResult(success=True, cart_item_id=77)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-wishlist-ref",
            settings=get_settings(),
            message="찜한 거 담아줘",
            add_fn=add_fn,
            get_cart_fn=_empty_cart,
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert action["cartItemId"] == 77


async def test_stream_cart_add_remove_phrase_with_add_marker_still_adds() -> None:
    """ "하나 빼고 담아줘" — 삭제 표지처럼 보이지만 담기 표지가 강한 신호라 담긴다(CART_ADDED)."""
    store = CartStateStore()

    async def add_fn(req):
        return AddToCartResult(success=True, cart_item_id=78)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-remove-add-marker",
            settings=get_settings(),
            message="하나 빼고 담아줘",
            add_fn=add_fn,
            get_cart_fn=_empty_cart,
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert action["cartItemId"] == 78


async def test_stream_cart_add_remove_intent_is_noop_this_round() -> None:
    """cart_remove 판정은 이번 라운드에서 아무 것도 하지 않는다 — 오늘 동작(담기)이 그대로 돈다."""
    store = CartStateStore()

    async def add_fn(req):
        return AddToCartResult(success=True, cart_item_id=79)

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-remove-noop",
            settings=get_settings(),
            message="장바구니에서 빼줘",
            add_fn=add_fn,
            get_cart_fn=_empty_cart,
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"


async def test_stream_cart_add_wishlist_intent_does_not_clear_pending() -> None:
    """옵션 되물음 진행 중(pending) 이 사용자가 "찜해줘"를 말해도 pending 은 지워지지 않는다."""
    from app.agents.buyer.cart.state import PendingAdd
    from app.schemas.spring import CartOption

    store = CartStateStore()
    thread_key = "m:t-wishlist-keeps-pending"
    await store.set_pending(
        thread_key,
        PendingAdd(
            product_id=1,
            quantity=1,
            options=[CartOption(option_id=3, name="블루")],
            attempts=1,
        ),
    )

    async def add_fn(req):
        raise AssertionError("wishlist 판정인데 add_fn 이 호출됐다")

    await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=None, quantity=1),
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            message="이거 찜해줘",
            add_fn=add_fn,
            get_cart_fn=_empty_cart,
        )
    )
    assert await store.get_pending(thread_key) is not None
