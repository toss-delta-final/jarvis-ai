"""장바구니 삭제 서브그래프 (이슈 #116, 🔶 I-24 초안 — BE 협의 전, `cart_remove_enabled` on 경로).

`stream_cart_add` 가 `classify_cart_utterance` 로 "cart_remove" 로 판정하고 플래그가 켜져 있을
때만 위임받는다(패킷 §5.3·§5.4). 대상 해소는 결정론적이다 — LLM 을 새로 부르지 않는다.
복수 삭제는 항목별 반복 호출이다(I-24 에 bulk 가 없다).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.agents.buyer._frames import sse
from app.agents.buyer.cart.identity import cart_identity
from app.agents.buyer.cart.state import CartStateStore, LastAdd
from app.core.text import _strip_unsafe
from app.schemas.chat import ActionData, DoneData, TokenData
from app.schemas.spring import CartViewItem
from app.services import spring_client
from app.services.spring_client import CartError, CartItemNotFound, SpringUnavailableError


def _done() -> str:
    return sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))


def _display_name(item: CartViewItem) -> str:
    """안내 문구용 상품명 — 반드시 `_strip_unsafe` 를 거친다(판매자 입력, §5.3 항목 6)."""
    return _strip_unsafe(item.product_name or "") or "상품"


def _resolve_remove_targets(
    message: str,
    items: list[CartViewItem],
    settings,
    last_add: LastAdd | None,
) -> list[CartViewItem] | None:
    """삭제 대상을 결정론적으로 해소한다 — 강한 신호부터 순서대로 시도하고, 결과가 나오면 그
    자리에서 확정한다(다음 규칙으로 넘어가지 않는다). 어느 규칙도 못 잡으면 `None`(호출부가
    현재 담긴 상품을 나열하며 되묻는다).

    순서(패킷 §5.3):
      1. 전체 표지(`cart_remove_all_markers`) → 전 항목. 가장 명시적인 신호라 최우선.
      2. 상품명이 발화에 **부분 문자열로** 등장 → 정확히 1건이면 그 항목. 2건 이상이면 어느
         쪽인지 결정할 수 없어 **그 자리에서 되물음**(3·4번으로 넘어가지 않는다 — 이미 "이름을
         지목했다"는 강한 신호가 확보됐는데 모호하면, 더 약한 신호로 임의로 골라잡는 것이 더
         위험하다). 이름은 `_strip_unsafe` 로 정제한 값으로 매칭한다 — product_name 은 판매자
         입력이라 제어·zero-width·bidi 문자가 섞일 수 있고, 원문 그대로 매칭하면 (a) 육안상
         똑같아 보이는 이름이 안 보이는 문자 때문에 조용히 매칭 실패하거나 (b) 반대로 그 안 보이는
         문자를 이용해 매칭을 조작하는 표면이 생긴다. 발화(`message`) 쪽은 사용자 입력이고 이미
         API 경계에서 정제된다는 전제라 여기서 다시 정제하지 않는다(`_pending_switch_signals` 와
         같은 전제).
      3. "방금 담은 거" 표지(`cart_remove_recent_markers`) → `last_add.cart_item_id`. 그 항목이
         지금 장바구니에 없으면(이미 빠졌거나 다른 세션에서 지워짐) **그 자리에서 되물음**(4번
         "단건 자동"으로 넘어가지 않는다 — 사용자가 명시적으로 "방금 그거"를 지목했는데 실패하면,
         장바구니에 다른 게 1건 있다고 그걸 대신 지우는 것은 사용자 의도와 다를 수 있다).
      4. 위 표지가 **하나도 없고** 장바구니에 항목이 정확히 1건이면 그 1건(표지 없는 발화에서만
         적용 — 표지가 있었는데 해소에 실패한 경우는 2·3번이 이미 되물음으로 종결했다).
      5. 그 외 → `None`.
    """
    if any(marker in message for marker in settings.cart_remove_all_markers):
        return list(items)

    name_matches = [
        item
        for item in items
        if (name := _strip_unsafe(item.product_name or "")) and name in message
    ]
    if name_matches:
        return name_matches if len(name_matches) == 1 else None

    if any(marker in message for marker in settings.cart_remove_recent_markers):
        if last_add is not None:
            recent_match = [item for item in items if item.cart_item_id == last_add.cart_item_id]
            if recent_match:
                return recent_match
        return None

    if len(items) == 1:
        return items
    return None


def _unresolved_notice(items: list[CartViewItem]) -> str:
    """되물음 문구 — 지금 담긴 상품명을 나열해 무엇을 물어야 할지 알려준다
    (`_UNRESOLVED_SCREEN_POSITION` 의 철학과 같음, graph.py 참조)."""
    names = ", ".join(_display_name(item) for item in items)
    return f"지금 장바구니에 있는 상품: {names}. 어떤 걸 뺄까요?"


async def stream_cart_remove(
    *,
    identity,
    message: str,
    cart_store: CartStateStore,
    thread_key: str,
    settings,
    get_cart_fn=None,
    delete_fn=None,
    observer=None,
) -> AsyncIterator[str]:
    """삭제 서브그래프. 항목마다 `action`(CART_REMOVED/CART_REMOVE_FAILED)을 내고 `done` 1회로 끝난다."""
    get_cart_fn = get_cart_fn or spring_client.get_cart
    delete_fn = delete_fn or spring_client.delete_cart_item

    user_id, guest_id = cart_identity(identity)
    if user_id is None and guest_id is None:
        yield sse(
            "token",
            TokenData(text="장바구니 삭제에는 로그인이 필요해요.").model_dump(by_alias=True),
        )
        yield _done()
        return

    try:
        cart_view = await get_cart_fn(user_id=user_id, guest_id=guest_id)
    except SpringUnavailableError:
        yield sse(
            "action",
            ActionData(
                type="CART_REMOVE_FAILED",
                message="장바구니를 확인하지 못했어요. 잠시 후 다시 시도해 주세요.",
                reason="CART_ERROR",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    items = list(cart_view.items)
    if not items:
        yield sse("token", TokenData(text="장바구니가 비어 있어요.").model_dump(by_alias=True))
        yield _done()
        return

    last_add = await cart_store.get_last_add(thread_key)
    targets = _resolve_remove_targets(message, items, settings, last_add)
    if targets is None:
        yield sse("token", TokenData(text=_unresolved_notice(items)).model_dump(by_alias=True))
        yield _done()
        return

    for item in targets:
        name = _display_name(item)
        try:
            await delete_fn(item.cart_item_id, user_id=user_id, guest_id=guest_id)
        except CartItemNotFound:
            # 🔶 I-24 협의 대상: 404 를 성공 안내로 종료하는 것은 정본 권고안 — 사용자가 보기엔
            # "빼려던 게 이미 빠져 있다"는 실패가 아니라 원하는 상태에 도달한 것이다.
            yield sse(
                "action",
                ActionData(
                    type="CART_REMOVED",
                    message=f"이미 빠져 있어요: {name}",
                    cart_item_id=item.cart_item_id,
                ).model_dump(by_alias=True),
            )
        except CartError:
            # 한 항목이 실패해도 다른 항목은 계속 진행한다(실패 격리) — 여기서 return 하지 않는다.
            # 🔶 I-24 협의 대상: 확장안은 CART_REMOVE_FAILED 에 reason 만 싣는다. 복수 삭제에서
            # 어느 항목이 실패했는지 구분하려고 cartItemId 를 함께 싣는다 — 협의 안건에 올릴 것.
            yield sse(
                "action",
                ActionData(
                    type="CART_REMOVE_FAILED",
                    message=f"빼지 못했어요: {name}. 잠시 후 다시 시도해 주세요.",
                    cart_item_id=item.cart_item_id,
                    reason="CART_ERROR",
                ).model_dump(by_alias=True),
            )
        else:
            # 조사(을/를) 대신 어순으로 자연스럽게 만든다(라운드 3 리뷰 F-3) — 받침 유무에 따른
            # 조사 선택 로직을 새로 만들지 않기 위해 "{동작}: {이름}" 형태로 통일한다.
            yield sse(
                "action",
                ActionData(
                    type="CART_REMOVED",
                    message=f"장바구니에서 뺐어요: {name}",
                    cart_item_id=item.cart_item_id,
                ).model_dump(by_alias=True),
            )

    yield _done()
