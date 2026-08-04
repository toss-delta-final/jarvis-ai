"""찜 추가·해제 서브그래프 (이슈 #117, 🔶 I-26/I-27 초안 — BE 협의 전, `wishlist_enabled` on 경로).

`stream_cart_add` 가 `classify_cart_utterance` 로 "wishlist_add"/"wishlist_remove" 로 판정하고
플래그가 켜져 있을 때만 위임받는다(패킷 §5.4). 게스트 찜은 없다(I-26) — 회원이 아니면 internal
호출 없이 degrade한다. 이벤트에 productId 를 싣지 않는다(경로 B) — `remove.py` 와 구조·어조를
맞춘 형제 모듈이다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.agents.buyer._frames import sse
from app.agents.buyer.cart.identity import cart_identity
from app.agents.buyer.recommendation.state import CartIntent
from app.core.text import _strip_unsafe
from app.schemas.chat import ActionData, DoneData, TokenData
from app.schemas.spring import AddWishlistRequest, WishlistItem
from app.services import spring_client
from app.services.spring_client import (
    SpringUnavailableError,
    WishlistDuplicate,
    WishlistError,
    WishlistNotFound,
    WishlistProductNotFound,
)


def _done() -> str:
    return sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))


def _display_wishlist_name(item: WishlistItem) -> str:
    """안내 문구용 상품명 — 반드시 `_strip_unsafe` 를 거친다(판매자 입력, `remove.py` 와 같은 규약)."""
    return _strip_unsafe(item.name or "") or "상품"


def _wishlist_unresolved_notice(items: list[WishlistItem]) -> str:
    """되물음 문구 — 지금 찜한 상품명을 나열해 무엇을 물어야 할지 알려준다(`remove.py` 와 같은 철학)."""
    names = ", ".join(_display_wishlist_name(item) for item in items)
    return f"찜한 상품: {names}. 어떤 걸 뺄까요?"


def _resolve_wishlist_remove_target(
    cart: CartIntent, message: str, items: list[WishlistItem]
) -> WishlistItem | None:
    """찜 해제 대상을 결정론적으로 해소한다(패킷 §5.4) — 강한 신호부터 순서대로 확정한다:
      1. `cart.product_id`(decompose 가 문맥에서 이미 골라 온 값)가 목록 안에 있으면 그것.
      2. 이름이 발화에 **부분 문자열로** 등장 → 정확히 1건이면 그것, 2건 이상이면 **그 자리에서
         미해소**(단건 자동으로 넘어가지 않는다 — `remove.py::_resolve_remove_targets` 와 같은
         "강한 신호가 모호하면 더 약한 신호로 임의로 고르지 않는다" 원칙).
      3. 위 두 규칙이 모두 안 잡히고 목록이 정확히 1건이면 그 1건.
      4. 그 외 → `None`.

    `remove.py::_resolve_remove_targets` 와 규칙 모양(강한 신호 우선·부분 문자열 이름 매칭·단건
    자동)이 겹치지만 **공용 헬퍼로 묶지 않았다**. 자료형이 다르고(`CartViewItem` vs
    `WishlistItem`, 필드명도 `product_name` vs `name`), 이쪽에는 "전체 삭제"·"방금 담은 거"
    같은 규칙이 아예 없다 — 억지로 하나의 함수로 묶으면 두 흐름이 서로 쓰지 않는 매개변수(상대
    쪽의 `cart_remove_all_markers`·`last_add` 같은)까지 시그니처에 끌고 다니게 되고, 한쪽 규칙을
    바꿀 때 다른 쪽 테스트까지 다시 봐야 한다. 겹치는 부분은 "이름 부분 문자열 매칭" 한 조각뿐이라
    분리해서 각자 읽기 쉽게 두는 편이 이 겹침의 크기에 비해 더 싸다.
    """
    if cart.product_id is not None:
        direct = [item for item in items if item.product_id == cart.product_id]
        if direct:
            return direct[0]

    name_matches = [
        item for item in items if (name := _strip_unsafe(item.name or "")) and name in message
    ]
    if name_matches:
        return name_matches[0] if len(name_matches) == 1 else None

    if len(items) == 1:
        return items[0]
    return None


async def stream_wishlist_add(
    *,
    identity,
    cart: CartIntent,
    settings,
    allowed_product_ids: set[int] | None = None,
    add_wishlist_fn=None,
    observer=None,
) -> AsyncIterator[str]:
    """찜 추가 서브그래프(I-26, 🔶 초안). `action`(WISHLIST_ADDED/WISHLIST_ADD_FAILED) 또는
    되물음 token 을 내고 `done` 으로 끝난다."""
    add_wishlist_fn = add_wishlist_fn or spring_client.add_wishlist

    user_id, _guest_id = cart_identity(identity)
    if user_id is None:
        # 게스트·익명 모두 여기 걸린다(cart_identity 는 게스트를 guest_id 로, 익명을 (None, None)
        # 으로 돌려주므로 둘 다 user_id 가 None) — 게스트 찜은 계약에 없다(I-26). internal 호출
        # 없이 degrade한다. 폐기된 GUEST_NOT_ALLOWED action 을 되살리지 않는다(계약에서 폐기된 값).
        yield sse("token", TokenData(text="찜에는 로그인이 필요해요.").model_dump(by_alias=True))
        yield _done()
        return

    # 경로 B 가드 — stream_cart_add 의 unresolved 판정과 같은 규칙이다. 담기 쪽은 옵션 되물음
    # 진행 중(pending)이면 이미 검증된 상품이라는 예외 분기가 있지만, 찜에는 pending 개념이
    # 없으므로 그 분기가 없다.
    product_id = cart.product_id
    if product_id is None or (
        allowed_product_ids is not None and product_id not in allowed_product_ids
    ):
        yield sse(
            "token",
            TokenData(
                text="어떤 상품을 찜할까요? 추천을 먼저 받아보시면 찜해 드릴게요."
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    try:
        await add_wishlist_fn(AddWishlistRequest(user_id=user_id, product_id=product_id))
    except WishlistDuplicate:
        # 🔶 I-26 협의 대상: 409 를 성공 안내로 종료하는 것은 정본 권고안 — 사용자가 보기엔
        # "찜하려던 게 이미 찜해 있다"는 실패가 아니라 원하는 상태에 도달한 것이다.
        yield sse(
            "action",
            ActionData(type="WISHLIST_ADDED", message="이미 찜해 두셨어요.").model_dump(
                by_alias=True
            ),
        )
    except WishlistProductNotFound:
        yield sse(
            "action",
            ActionData(
                type="WISHLIST_ADD_FAILED",
                message="해당 상품을 찾지 못했어요.",
                reason="PRODUCT_NOT_FOUND",
            ).model_dump(by_alias=True),
        )
    except WishlistError:
        yield sse(
            "action",
            ActionData(
                type="WISHLIST_ADD_FAILED",
                message="찜하지 못했어요. 잠시 후 다시 시도해 주세요.",
                reason="WISHLIST_ERROR",
            ).model_dump(by_alias=True),
        )
    else:
        # 경로 B — 이벤트에 productId 를 싣지 않는다. `ActionData.cart_item_id` 는 cart_item.id
        # 전용 필드다 — 여기 productId 를 넣으면 다른 자원의 id 를 그 필드로 흘리는 것이라
        # 절대 하지 않는다.
        yield sse(
            "action",
            ActionData(type="WISHLIST_ADDED", message="찜해 뒀어요.").model_dump(by_alias=True),
        )
    yield _done()


async def stream_wishlist_remove(
    *,
    identity,
    cart: CartIntent,
    message: str,
    settings,
    get_wishlist_fn=None,
    remove_wishlist_fn=None,
    observer=None,
) -> AsyncIterator[str]:
    """찜 해제 서브그래프(I-27, 🔶 초안). 회원 아니면 `stream_wishlist_add` 와 같은 degrade."""
    get_wishlist_fn = get_wishlist_fn or spring_client.get_wishlist
    remove_wishlist_fn = remove_wishlist_fn or spring_client.remove_wishlist

    user_id, _guest_id = cart_identity(identity)
    if user_id is None:
        yield sse(
            "token", TokenData(text="찜 해제에는 로그인이 필요해요.").model_dump(by_alias=True)
        )
        yield _done()
        return

    try:
        wishlist_view = await get_wishlist_fn(user_id)
    except SpringUnavailableError:
        # I-28 어댑터는 4xx/5xx·도달 불가·스키마 불일치를 전부 SpringUnavailableError 로 낸다
        # (get_cart 와 같은 degrade 규약) — WishlistError 만 잡으면 이 실패를 하나도 못 잡는다.
        yield sse(
            "action",
            ActionData(
                type="WISHLIST_REMOVE_FAILED",
                message="찜 목록을 확인하지 못했어요. 잠시 후 다시 시도해 주세요.",
                reason="WISHLIST_ERROR",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    items = list(wishlist_view.items)
    if not items:
        yield sse("token", TokenData(text="찜한 상품이 없어요.").model_dump(by_alias=True))
        yield _done()
        return

    target = _resolve_wishlist_remove_target(cart, message, items)
    if target is None:
        yield sse(
            "token",
            TokenData(text=_wishlist_unresolved_notice(items)).model_dump(by_alias=True),
        )
        yield _done()
        return

    name = _display_wishlist_name(target)
    try:
        await remove_wishlist_fn(target.product_id, user_id=user_id)
    except WishlistNotFound:
        # 🔶 I-27 협의 대상: 404 를 성공 안내로 종료하는 것은 정본 권고안.
        yield sse(
            "action",
            ActionData(
                type="WISHLIST_REMOVED", message=f"이미 찜 목록에 없어요: {name}"
            ).model_dump(by_alias=True),
        )
    except WishlistError:
        yield sse(
            "action",
            ActionData(
                type="WISHLIST_REMOVE_FAILED",
                message=f"빼지 못했어요: {name}. 잠시 후 다시 시도해 주세요.",
                reason="WISHLIST_ERROR",
            ).model_dump(by_alias=True),
        )
    else:
        # 어순은 라운드 3(F-3)에서 정한 "{동작}: {이름}" 규약과 통일한다(조사 없이 자연스럽게).
        yield sse(
            "action",
            ActionData(type="WISHLIST_REMOVED", message=f"찜 목록에서 뺐어요: {name}").model_dump(
                by_alias=True
            ),
        )
    yield _done()
