"""세션 취향 버퍼 적재가 intent 별로 옳게 걸러지는지 회귀(이슈 #116·#117·#119).

`app/agents/buyer/graph.py` 의 세션 버퍼 적재 블록(`decision.intent not in
settings.profile_buffer_excluded_intents`)이 `cart_remove`/`wishlist_remove` 발화를 걸러내고
`cart_add`/`wishlist_add` 발화는 그대로 태우는지, **`get_settings()` 기본값 그대로** 검증한다.
설정을 테스트에서 덮어써 놓고 통과시키면 정작 기본값 조합이 깨져도 잡지 못한다.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.agents.buyer import graph as buyer_graph
from app.agents.buyer.cart.state import get_cart_store
from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.session_state import context_thread_key
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.session_context import BuyerSessionInput
from app.schemas.spring import AddToCartResult, CartView, CartViewItem, WishlistAddResult
from tests._fakes import FakeLLM


def _member() -> Identity:
    return Identity(user_id="123", is_guest=False, seller_id=None, subject="123")


def _req(message: str, thread_id: str) -> SimpleNamespace:
    return SimpleNamespace(session_id="s1", thread_id=thread_id, message=message)


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
        request_id="profile-buffer-test",
        record_model_call=lambda *_: None,
    )


async def run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    async for frame in _production_run_buyer_turn(request, identity, observer=observer, **kwargs):
        yield frame


async def _thread_key(request, identity) -> str:  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    return context_thread_key(observer.context_id, request.thread_id)


async def _collect(gen) -> list[dict]:  # noqa: ANN001
    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line.removeprefix("data:").strip()))
    return events


class _FakeProfileStore:
    """`get_profile_store()` 대체 — `append_session_ctx` 호출 여부만 기록한다(실 pg-profile 무관)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def append_session_ctx(self, key, message, *, cap, repeat_cap):  # noqa: ANN001
        self.calls.append((key, message))


def _patch_profile_store(monkeypatch) -> _FakeProfileStore:  # noqa: ANN001
    fake = _FakeProfileStore()

    async def fake_get_profile_store():
        return fake

    # graph.py 는 `from app.agents.profile.store import get_profile_store` 로 이름을 들여왔으므로
    # 그 모듈(app.agents.buyer.graph)의 바인딩을 패치해야 실제로 가로채진다.
    monkeypatch.setattr(buyer_graph, "get_profile_store", fake_get_profile_store)
    return fake


async def test_cart_remove_does_not_append_to_profile_buffer(monkeypatch) -> None:  # noqa: ANN001
    """재현·수정 확인 — "이어폰 빼줘"(cart_remove)는 기본 설정에서 세션 취향 버퍼에 쌓이면
    안 된다. 삭제 발화가 버퍼에 남으면, 방금 치운 상품이 그대로 선호로 학습될 수 있다."""
    import app.services.spring_client as sc

    async def fake_get_cart(*, user_id=None, guest_id=None):
        return CartView(
            items=[CartViewItem(cart_item_id=1, product_id=10, product_name="이어폰", quantity=1)]
        )

    async def fake_delete(cart_item_id, *, user_id=None, guest_id=None):
        return None

    monkeypatch.setattr(sc, "get_cart", fake_get_cart)
    monkeypatch.setattr(sc, "delete_cart_item", fake_delete)
    fake_store = _patch_profile_store(monkeypatch)

    llm = FakeLLM(decompose={"intent": "cart_remove", "cart": {}})
    request = _req("이어폰 빼줘", "t-buffer-cart-remove")
    await _collect(run_buyer_turn(request, _member(), llm=llm))

    assert fake_store.calls == []


async def test_wishlist_remove_does_not_append_to_profile_buffer(monkeypatch) -> None:  # noqa: ANN001
    """재현·수정 확인 — "이어폰 찜 빼줘"(wishlist_remove)도 기본 설정에서 버퍼에 쌓이면
    안 된다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    async def fake_get_wishlist(user_id):
        return WishlistView(items=[WishlistItem(product_id=10, name="이어폰")])

    async def fake_remove_wishlist(product_id, *, user_id=None):
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)
    fake_store = _patch_profile_store(monkeypatch)

    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    request = _req("이어폰 찜 빼줘", "t-buffer-wishlist-remove")
    await _collect(run_buyer_turn(request, _member(), llm=llm))

    assert fake_store.calls == []


async def test_wishlist_view_does_not_append_to_profile_buffer(monkeypatch) -> None:  # noqa: ANN001
    """[#386] "내가 뭐 찜했지?"(wishlist_view)도 버퍼에 쌓이면 안 된다.

    다만 제외 사유가 형제들과 다르다 — cart_remove·wishlist_remove 는 **부호가 반대인 신호**라
    빼지만, 이쪽은 cart_view·order_status 와 같은 **취향 신호 0인 상태 조회**다. 매 세션 반복되며
    슬라이딩 윈도우를 채워 정작 취향 발화를 밀어낸다(config.py 주석 참조).
    """
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    async def fake_get_wishlist(user_id):
        return WishlistView(items=[WishlistItem(product_id=10, name="이어폰")])

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    fake_store = _patch_profile_store(monkeypatch)

    llm = FakeLLM(decompose={"intent": "wishlist_view", "cart": {}})
    request = _req("내가 뭐 찜했지?", "t-buffer-wishlist-view")
    events = await _collect(run_buyer_turn(request, _member(), llm=llm))

    assert fake_store.calls == []
    # 라우팅이 실제로 조회 핸들러에 닿았는지도 함께 고정한다 — 분기가 없으면 추천 레인으로
    # 새면서도 버퍼 제외만은 통과해 이 테스트가 조용히 무의미해진다.
    # `progress` 는 상위 스트림이 모든 턴에 싣는 이벤트라(#289) 핸들러 산출과 분리해 본다.
    assert [e["type"] for e in events if e["type"] != "progress"] == ["token", "done"]
    tokens = [e for e in events if e["type"] == "token"]
    assert "이어폰" in tokens[0]["data"]["text"]


async def test_cart_add_still_appends_to_profile_buffer(monkeypatch) -> None:  # noqa: ANN001
    """회귀 방지 — `cart_add`(긍정 행동 신호)는 기존대로 버퍼에 계속 쌓여야 한다."""
    import app.services.spring_client as sc

    async def fake_add(req):
        return AddToCartResult(success=True, cart_item_id=42)

    async def fake_get_cart(*, user_id=None, guest_id=None):
        return CartView(items=[])

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", fake_get_cart)
    fake_store = _patch_profile_store(monkeypatch)

    request = _req("이어폰 담아줘", "t-buffer-cart-add")
    seed_store = await get_cart_store()
    await seed_store.set_last_reco(await _thread_key(request, _member()), [(101, "이어폰")])

    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 101, "quantity": 1}})
    await _collect(run_buyer_turn(request, _member(), llm=llm))

    assert len(fake_store.calls) == 1
    assert fake_store.calls[0][1] == "이어폰 담아줘"


async def test_wishlist_add_still_appends_to_profile_buffer(monkeypatch) -> None:  # noqa: ANN001
    """회귀 방지 — `wishlist_add`(`cart_add` 와 같은 긍정 행동 신호)도 기존대로 버퍼에 쌓여야
    한다 — 이번 변경이 실수로 함께 걸러내면 안 된다."""
    import app.services.spring_client as sc

    async def fake_add_wishlist(req):
        return WishlistAddResult(success=True, product_id=req.product_id)

    monkeypatch.setattr(sc, "add_wishlist", fake_add_wishlist)
    fake_store = _patch_profile_store(monkeypatch)

    request = _req("이어폰 찜해줘", "t-buffer-wishlist-add")
    seed_store = await get_cart_store()
    await seed_store.set_last_reco(await _thread_key(request, _member()), [(101, "이어폰")])

    llm = FakeLLM(decompose={"intent": "wishlist_add", "cart": {"productId": 101}})
    await _collect(run_buyer_turn(request, _member(), llm=llm))

    assert len(fake_store.calls) == 1
    assert fake_store.calls[0][1] == "이어폰 찜해줘"
