"""장바구니 삭제 서브그래프 (이슈 #116, 🔶 I-24 초안 — BE 협의 전, `cart_remove_enabled` on 경로).

`stream_cart_add` 가 `classify_cart_utterance` 로 "cart_remove" 로 판정하고 플래그가 켜져 있을
때만 위임받는다(패킷 §5.3·§5.4). 대상 해소는 결정론적이다 — LLM 을 새로 부르지 않는다.
복수 삭제는 항목별 반복 호출이다(I-24 에 bulk 가 없다).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import logging

from app.agents.buyer._frames import sse
from app.agents.buyer.cart.identity import cart_identity
from app.agents.buyer.cart.negation import has_any_negation, matches_unnegated
from app.agents.buyer.cart.state import CartStateStore, LastAdd
from app.core.text import _strip_unsafe
from app.schemas.chat import ActionData, DoneData, TokenData
from app.schemas.spring import CartViewItem
from app.services import spring_client
from app.services.spring_client import CartError, CartItemNotFound, SpringUnavailableError

_log = logging.getLogger(__name__)


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

    순서(패킷 §5.3, 2차 리뷰 지적 2·3 + 라운드 10 이후):
      1. 발화에 부정·대조 표지(`negation.has_any_negation` — 어미형(`utterance_negation_markers`)
         과 접두형(`utterance_prefix_negation_markers`, "안"·"못") 둘 다 본다, **문장 어디든** —
         이 층은 위치 창이 아니라 전체 검사다, `intent_guard.py` 의 근처 창 검사와 다르다)가
         **없을 때만** 전체 표지(`cart_remove_all_markers`) → 전 항목. "전체 삭제"·"방금 담은 거"는 사용자가
         이름을 대지 않은 대상을 **코드가 고르는** 규칙이라 다른 규칙보다 엄격해야 한다 — "전부
         빼지는 말고 이어폰만 빼줘"에서 "전부 빼"만 보고 확정하면 사용자가 명시적으로 배제한
         전체 삭제가 실행된다. 대조어("말고")가 표지에서 여러 글자 떨어져 나오므로 위치 창이
         아니라 문장 전체를 본다.
      2. 상품명이 발화에 **부분 문자열로**, 그리고 **부정되지 않은 출현으로** 등장 → 정확히
         1건이면 그 항목. 2건 이상이면 어느 쪽인지 결정할 수 없어 **그 자리에서 되물음**(3·4번
         으로 넘어가지 않는다 — 이미 "이름을 지목했다"는 강한 신호가 확보됐는데 모호하면, 더
         약한 신호로 임의로 골라잡는 것이 더 위험하다). 이름은 `_strip_unsafe` 로 정제한 값으로
         매칭한다 — product_name 은 판매자 입력이라 제어·zero-width·bidi 문자가 섞일 수 있고,
         원문 그대로 매칭하면 (a) 육안상 똑같아 보이는 이름이 안 보이는 문자 때문에 조용히
         매칭 실패하거나 (b) 반대로 그 안 보이는 문자를 이용해 매칭을 조작하는 표면이 생긴다.
         발화(`message`) 쪽은 사용자 입력이고 이미 API 경계에서 정제된다는 전제라 여기서 다시
         정제하지 않는다(`_pending_switch_signals` 와 같은 전제). **[라운드 11]** 부정 판정은
         1·3번의 문장 전체 가드(`has_any_negation`)가 아니라 `negation.matches_unnegated` 의
         **출현 단위** 판정을 쓴다 — "이어폰은 빼지 말고 케이스 빼줘"에서 문장 전체 가드를
         쓰면 정상 삭제 대상인 "케이스"까지 함께 죽어 되물음이 된다. 이름별로 발화에 나온 각
         출현의 앞뒤(접두·어미 부정)를 보고, **부정되지 않은 출현이 하나라도 있는 이름만**
         매칭으로 친다 — "이어폰"은 뒤에 "말고"가 붙어 그 출현이 무효화되지만 "케이스"는
         부정 없이 살아남는다.
      3. 부정·대조 표지가 **없을 때만** "방금 담은 거" 표지(`cart_remove_recent_markers`) →
         `last_add.cart_item_id`. "방금 담은 거 말고 다른 거 빼줘"처럼 사용자가 **제외한** 바로
         그 상품을 코드가 고르면 안 된다 — 이름이 따로 없으므로 안전한 동작은 되물음이다(1번과
         같은 이유로 문장 전체 검사). 표지가 있고 부정도 없는데 그 항목이 지금 장바구니에
         없으면(이미 빠졌거나 다른 세션에서 지워짐) **그 자리에서 되물음**(4번 "단건 자동"으로
         넘어가지 않는다 — 사용자가 명시적으로 "방금 그거"를 지목했는데 실패하면, 장바구니에
         다른 게 1건 있다고 그걸 대신 지우는 것은 사용자 의도와 다를 수 있다).
      4. 부정·대조 표지가 **없을 때만**(위 규칙들과 같은 이유 — 이름 없이 코드가 고르는
         규칙이다) 장바구니에 항목이 정확히 1건이면 그 1건(표지 없는 발화에서만 적용 — 표지가
         있었는데 해소에 실패한 경우는 2·3번이 이미 되물음으로 종결했다).
      5. 그 외 → `None`.

    **[라운드 10]** 이 함수의 부정 판정은 원래 어미형(`utterance_negation_markers`)만 봤다 —
    `intent_guard.py` 가 라운드 9 에서 접두형("안"·"못")을 배울 때 이 함수는 그대로 남아,
    "방금 담은 건 안 빼도 되고, 저번에 산 것도 빼줘"류가 `cart_remove` 로 라우팅된 뒤 여기서
    사용자가 명시적으로 "빼지 말라"고 한 항목이 실제로 삭제됐다(플래그 on 시 데이터 손실).
    같은 부정 개념이 두 파일에 각자 구현돼 한쪽만 고쳐진 것이 원인이라, 판정을
    `negation.has_any_negation` 공용 함수로 옮겨 `intent_guard.py` 와 같은 함수를 쓰게 했다.

    **[라운드 11]** 라운드 10 은 1·3·4번(전체 삭제·방금 담은 거·단건 자동)에 문장 전체 가드를
    붙였지만 **가장 강한 신호인 2번(이름 매칭)은 그대로 남겨** "이어폰은 빼지 말고 케이스
    빼줘"에서 이름이 지목된 이어폰이 그대로 매칭돼 삭제됐다. 2번은 `negation.matches_unnegated`
    (출현 단위 — `negation.py` 에 이미 있던 함수, 새로 만들지 않았다)를 쓴다.
    """
    has_negation = has_any_negation(
        message, settings.utterance_negation_markers, settings.utterance_prefix_negation_markers
    )

    if not has_negation and any(marker in message for marker in settings.cart_remove_all_markers):
        return list(items)

    name_matches = [
        item
        for item in items
        if (name := _strip_unsafe(item.product_name or ""))
        and matches_unnegated(
            message,
            [name],
            settings.utterance_negation_markers,
            settings.utterance_negation_window,
            settings.utterance_prefix_negation_markers,
        )
    ]
    if name_matches:
        return name_matches if len(name_matches) == 1 else None

    if not has_negation and any(
        marker in message for marker in settings.cart_remove_recent_markers
    ):
        if last_add is not None:
            recent_match = [item for item in items if item.cart_item_id == last_add.cart_item_id]
            if recent_match:
                return recent_match
        return None

    if not has_negation and len(items) == 1:
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

    try:
        last_add = await cart_store.get_last_add(thread_key)
    except Exception as exc:  # noqa: BLE001 - "방금 담은 거" 표지 없는 평범한 삭제도 이 읽기를
        # 항상 먼저 거친다 — 실패가 새면 첫 SSE 프레임도 나가기 전에 삭제 기능이 통째로
        # 죽는다(2차 리뷰 지적 6·2번). None 으로 degrade하면 _resolve_remove_targets 의
        # "방금 담은 거" 분기가 표지는 있어도 대상을 못 찾은 것과 같은 경로(되물음)로 자연히
        # 떨어진다 — 임의로 다른 상품을 고르지 않는다. CancelledError 는 BaseException 이라
        # 전파된다.
        _log.warning("last_add_read_failed", extra={"reason": str(exc)})
        last_add = None
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
