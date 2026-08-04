"""장바구니 서브그래프 스트리밍 (결정 7 / api-spec §4.1 담기·§4.9 조회, 이슈 #3).

담기: (상품·옵션·수량) 의도 확정 → add_to_cart(I-2, 단건) → SSE action.
      옵션 필수(CART_OPTION_REQUIRED)면 실패 action 없이 token 되물음 → pending 저장 →
      다음 턴 사용자 답을 optionId 로 해석해 재담기(§4.1 멀티턴). 단 후보가 1개뿐이면 되묻지
      않고 그 옵션으로 즉시 재담기한다(이슈 #114). 담기 전 get_cart(§4.9)로
      기존 보유를 확인해 합산 안내(조회 실패 시에도 담기 진행, degrade).
조회: get_cart(I-18) → token 텍스트 답변(별도 이벤트 없음, §3.1).
게스트 담기 허용(userId|guestId, §4.1) — 신원은 JWT sub 유래(요청 본문 불신).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import logging

from pydantic import ValidationError

from app.agents.buyer._frames import sse
from app.agents.buyer.cart.identity import cart_identity
from app.agents.buyer.cart.intent_guard import classify_cart_utterance
from app.agents.buyer.cart.remove import stream_cart_remove
from app.agents.buyer.cart.state import CartStateStore, PendingAdd
from app.agents.buyer.cart.wishlist import stream_wishlist_add, stream_wishlist_remove
from app.agents.buyer.recommendation.state import CartIntent
from app.core.text import _strip_unsafe
from app.core.tracing import current_request_trace
from app.schemas.chat import ActionData, DoneData, TokenData
from app.schemas.spring import AddToCartRequest, AddToCartResult, CartOption, CartViewItem
from app.services import spring_client
from app.services.spring_client import (
    CartError,
    CartOptionInvalid,
    CartOptionRequired,
    CartProductNotFound,
    CartQuantityExceeded,
    CartStockInsufficient,
    SpringUnavailableError,
)

__all__ = ["cart_identity", "stream_cart_add", "stream_cart_view"]

_log = logging.getLogger(__name__)


def _option_label(option: CartOption) -> str:
    """옵션 표시 라벨 — 표시명(판매자 입력이라 정제)에 추가금이 양수면 함께 붙인다. 이름 없으면 "".

    extraPrice 는 api-spec §4.1 상 surcharge(≥0) — 양수만 표시. 0/음수(계약 미정의)는 미표시.
    되물음 문구(_options_text)와 자동 선택 안내(#114)가 같은 규칙을 쓰도록 한 곳에 둔다.
    """
    name = _strip_unsafe(option.name)
    if not name:
        return ""
    return f"{name}(+{option.extra_price:,}원)" if _has_surcharge(option) else name


def _has_surcharge(option: CartOption) -> bool:
    return bool(option.extra_price and option.extra_price > 0)


def _options_text(options: list[CartOption]) -> str:
    """옵션 목록을 되물음 문구로 나열한다 — 추가금(extraPrice)이 있으면 함께 표시."""
    parts = [label for opt in options if (label := _option_label(opt))]
    return " / ".join(parts) if parts else "옵션"


def _all_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """겹치는 경우까지 needle 의 모든 [start, end) 출현 구간을 돌려준다."""
    if not needle:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while (index := text.find(needle, start)) >= 0:
        spans.append((index, index + len(needle)))
        start = index + 1
    return spans


def _pending_switch_signals(
    message: str, pending: PendingAdd | None, markers: list[str]
) -> tuple[bool, bool]:
    """(독립 전환 마커 있음, 독립 pending 옵션명 있음).

    겹친 문자열은 위치로 판정한다. `대` ⊂ `대신`이면 옵션 언급이 아니고,
    `말고` ⊂ `말고기`이면 전환 마커가 아니다.
    """
    if pending is not None and any(not option.name.strip() for option in pending.options):
        # 옵션명을 하나라도 모르면 전환과 옵션 답변을 구별할 근거가 없다. 이 구성에서는
        # 정밀도를 우선해 휴리스틱을 끄므로 #253 의 옛 상품 오담기 보호가 의도적으로 적용되지 않는다.
        return False, False
    option_spans = (
        [
            span
            for option in pending.options
            if (name := option.name.strip())
            for span in _all_spans(message, name)
        ]
        if pending is not None
        else []
    )
    marker_spans = [span for marker in markers if marker for span in _all_spans(message, marker)]
    markers_present = any(
        not any(
            option_start <= marker_start and marker_end <= option_end
            for option_start, option_end in option_spans
        )
        for marker_start, marker_end in marker_spans
    )
    option_mentioned = any(
        not any(
            marker_start <= option_start and option_end <= marker_end
            for marker_start, marker_end in marker_spans
        )
        for option_start, option_end in option_spans
    )
    return markers_present, option_mentioned


def _existing_quantity(items: list[CartViewItem], product_id: int, option_id: int | None) -> int:
    """담기 전 보유 수량(합산 안내용) — 동일 상품·옵션 합계. optionId 미상이면 그 상품 전체를 센다."""
    return sum(
        item.quantity
        for item in items
        if item.product_id == product_id and (option_id is None or item.option_id == option_id)
    )


async def _add_with_single_option(
    add_fn, req: AddToCartRequest
) -> tuple[AddToCartResult, CartOption | None]:
    """I-2 담기 — 옵션 후보가 1개뿐이면 되묻지 않고 그 optionId 로 즉시 재담기한다(이슈 #114).

    선택지가 하나면 되물어도 답이 정해져 있어 왕복만 늘어난다. 계약 변경은 없다 — AI 가 유일
    옵션의 optionId 로 I-2 를 재호출할 뿐(api-spec §4.1). 자동 선택 재시도는 **1회**로 고정한다:
    자동 선택한 옵션에도 REQUIRED 가 또 오면 계약 이상이므로 예외를 그대로 올려 기존 되물음
    멀티턴으로 degrade 한다(무한 재시도 금지). 후보가 여럿이면 임의로 고르지 않고, 이미 보낸
    optionId 와 같으면 같은 요청을 되풀이하지 않는다. 나머지 오류(INVALID·재고·수량 등)는 그대로
    상위로 올려 기존 action 매핑을 탄다.

    반환값 두 번째는 자동 선택한 옵션(없었으면 None) — AI 가 대신 골랐음을 안내 문구에 밝히기 위함.
    """
    try:
        return await add_fn(req), None
    except CartOptionRequired as exc:
        if len(exc.options) != 1 or exc.options[0].option_id == req.option_id:
            raise
        option = exc.options[0]
        return await add_fn(req.model_copy(update={"option_id": option.option_id})), option


def _done() -> str:
    return sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))


# 담기 대상을 확정하지 못했을 때의 되물음 문구 (#118).
#
# 기본 문구는 **한 글자도 바꾸지 않는다** — 화면 맥락이 없는 경로(FE 가 `screen` 을 보내지 않는
# 현재의 절대다수)는 오늘과 바이트 동일해야 한다. 화면 후보가 있는데 "추천을 먼저 받아보시면"
# 이라고 답하면 사용자는 눈앞의 상품을 두고 엉뚱한 안내를 받는다 — 정본 §3.1 의 "여러 건이면
# 되물음"은 되묻는 것만이 아니라 **무엇을 물어야 할지 알려주는 것**까지다.
_UNRESOLVED_DEFAULT = "어떤 상품을 담을까요? 추천을 먼저 받아보시면 담아드릴게요."
# 화면을 가리켰지만 **어느 것인지** 특정되지 않은 경우. 후보 다건(`ambiguous_screen_candidates`)과
# 순번·좌표가 화면 범위를 벗어난 경우(`*_out_of_range`), 좌표를 풀 `columns` 가 없는 경우를 **한
# 문구로 묶는다** — 사유는 다르지만 사용자가 취해야 할 다음 행동이 "위치를 다시 말한다"로 같기
# 때문이다. 범위 밖에 "N개까지만 있어요"처럼 개수를 알려주는 안내는 화면에 이미 보이는 정보를
# 되읊는 것이라 이득이 없다고 판단했다.
_UNRESOLVED_SCREEN_POSITION = (
    "화면에 보이는 상품 중 어떤 걸 담을까요? 왼쪽부터 몇 번째인지 말씀해 주시면 담아드릴게요."
)
# 사용자가 말한 상품 id 를 두 목록 어디에서도 찾지 못한 경우. 위와 **묶지 않는다** — 여기서
# "몇 번째인지 말해 달라"고 하면 못 찾았다는 사실 자체가 전달되지 않아 같은 말을 반복하게 된다.
_UNRESOLVED_SCREEN_NOT_FOUND = (
    "말씀하신 상품을 화면에서 찾지 못했어요. 화면에 보이는 상품 중에서 골라 주시겠어요?"
)
_SCREEN_POSITION_REASONS = frozenset(
    {
        "ambiguous_screen_candidates",
        "ordinal_out_of_range",
        "coordinate_out_of_range",
        "coordinate_without_columns",
    }
)

# 찜 오담기 방어(이슈 #117, 패킷 §4) — wishlist_enabled=False 인 동안 찜 판정이 나오면 이 문구로
# degrade 한다. 실패 action(WISHLIST_ADD_FAILED 등)이 아니라 token 인 이유: 이건 오류가 아니라
# "아직 지원 안 하는 기능"에 대한 안내라 되물음(옵션 되물음 등)과 같은 취급이 맞다 — 사용자가
# 다시 말할 대상도, 재시도할 오류도 아니다.
# intent 별로 문구를 가른다(2차 리뷰 N-3) — 찜 추가 요청에는 담기를 대안으로 제안해도 자연스럽지만
# ("찜 대신 담아 드릴까요?"), 찜 해제 요청에 같은 제안을 하면 빼 달라는 사용자에게 담아 주겠다고
# 답하는 꼴이 된다. 해제 쪽은 대안 제안 없이 상태만 안내한다.
_WISHLIST_ADD_DISABLED_NOTICE = "찜 기능은 아직 준비 중이에요. 장바구니에 담아 드릴까요?"
_WISHLIST_REMOVE_DISABLED_NOTICE = "찜 기능은 아직 준비 중이에요."


def _unresolved_notice(screen_reason: str | None) -> str:
    """되물음 문구를 화면 해소 사유로 가른다. 사유가 없으면 오늘 문구 그대로."""
    if screen_reason in _SCREEN_POSITION_REASONS:
        return _UNRESOLVED_SCREEN_POSITION
    if screen_reason == "unknown_product_id_spoken":
        return _UNRESOLVED_SCREEN_NOT_FOUND
    return _UNRESOLVED_DEFAULT


async def stream_cart_add(
    *,
    identity,
    cart: CartIntent,
    cart_store: CartStateStore,
    thread_key: str,
    settings,
    message: str = "",
    allowed_product_ids: set[int] | None = None,
    screen_reason: str | None = None,
    add_fn=None,
    get_cart_fn=None,
    delete_fn=None,
    add_wishlist_fn=None,
    get_wishlist_fn=None,
    remove_wishlist_fn=None,
    observer=None,
) -> AsyncIterator[str]:
    """담기 서브그래프. action(CART_ADDED/CART_ADD_FAILED) 또는 옵션 되물음 token 을 낸다.

    `screen_reason` 은 화면 지시어 해소기(`screen_reference`)가 담기 대상을 **일부러 비운** 사유다
    (#118). 되물음 문구를 상황에 맞게 가르는 데만 쓰고 판정에는 관여하지 않는다 — `None` 이면
    (= FE 가 `screen` 을 안 보냈거나 해소기가 개입하지 않은 절대다수 경로) 문구는 오늘과 같다.

    **[라운드 14, head `0a53ffc` 리뷰]** 아래 삭제·찜 세 위임 분기(`stream_cart_remove`/
    `stream_wishlist_add`/`stream_wishlist_remove`)는 이 `screen_reason` 을 넘기지 않는다 —
    누락이 아니라 **의도적 축소**다. 그 세 흐름은 화면 해소 사유별 문구(`_UNRESOLVED_SCREEN_POSITION`·
    `_UNRESOLVED_SCREEN_NOT_FOUND`) 대신 각자의 범용 되물음 문구를 쓴다. 잘못된 동작이 아니라
    "덜 친절한 문구"일 뿐이고(되물음 자체는 정상 발생), #118 의 세 문구는 실 LLM 프로브(N=8)와
    여러 라운드 리뷰로 고른 것이라 이 레인에 측정 없이 같은 급의 찜·삭제용 화면 문구를 지어내
    끼워 넣지 않는다. 화면 맥락(#118)과 삭제·찜(#116·#117)의 통합은 이 레인 범위 밖(핸드오버의
    찜 해소는 "추천 목록·문맥에서 productId 해소"까지)이며, 플래그가 꺼진 지금은 사용자에게
    도달하지도 않는다 — 플래그를 켜기 전에 다시 볼 항목이다.
    """
    # 찜 오담기 방어(이슈 #117, 패킷 §4) — 신원 도출·pending 조회보다 앞에서 판별한다. LLM 을
    # 새로 부르지 않는 결정론적 판정이라 순서가 앞이어도 비용이 없고, wishlist_enabled=False 인
    # 동안은 이 턴이 어차피 담기로 흘러가면 안 되므로 다른 상태를 건드리기 전에 빠져야 한다.
    intent = classify_cart_utterance(message, settings)
    if intent in ("wishlist_add", "wishlist_remove") and not settings.wishlist_enabled:
        # pending 은 지우지 않는다 — 이 턴은 담기 흐름에 개입하지 않고 그냥 빠지는 것이라, 옵션
        # 되물음 중이던 사용자가 "찜해줘"를 말했다고 진행 중이던 담기를 버릴 이유가 없다.
        notice = (
            _WISHLIST_ADD_DISABLED_NOTICE
            if intent == "wishlist_add"
            else _WISHLIST_REMOVE_DISABLED_NOTICE
        )
        yield sse("token", TokenData(text=notice).model_dump(by_alias=True))
        yield _done()
        return
    if intent == "wishlist_add" and settings.wishlist_enabled:
        # 위 flag-off 분기와 반대로 이 턴은 **실제로 다른 동작을 수행하고 빠진다** — 담기 대기와
        # 무관한 흐름으로 위임하므로, `graph.py` 665~668행과 같은 취지로 stale pending 을 정리한다
        # (다음 턴이 옛 상품의 옵션 답변으로 오해석되지 않게).
        await cart_store.clear_pending(thread_key)
        async for frame in stream_wishlist_add(
            identity=identity,
            cart=cart,
            settings=settings,
            allowed_product_ids=allowed_product_ids,
            add_wishlist_fn=add_wishlist_fn,
            observer=observer,
        ):
            yield frame
        return
    if intent == "wishlist_remove" and settings.wishlist_enabled:
        await cart_store.clear_pending(thread_key)
        async for frame in stream_wishlist_remove(
            identity=identity,
            cart=cart,
            message=message,
            settings=settings,
            get_wishlist_fn=get_wishlist_fn,
            remove_wishlist_fn=remove_wishlist_fn,
            observer=observer,
        ):
            yield frame
        return
    if intent == "cart_remove" and settings.cart_remove_enabled:
        await cart_store.clear_pending(thread_key)
        async for frame in stream_cart_remove(
            identity=identity,
            message=message,
            cart_store=cart_store,
            thread_key=thread_key,
            settings=settings,
            get_cart_fn=get_cart_fn,
            delete_fn=delete_fn,
            observer=observer,
        ):
            yield frame
        return
    # cart_remove_enabled=False 면 cart_remove 판정이어도 **오늘 동작 그대로**(담기로 흘러간다) —
    # 안내를 내지 않는다. 찜과 다르게 취급하는 이유: 찜은 "담지 말아야 할" 오담기라 개입해야
    # 안전하지만, 삭제 발화가 담기로 잘못 오는 경우는 드물고 여기서 안내를 내면 정상 담기가 더
    # 자주 깨질 위험이 있다(패킷 §5 라운드 3).

    add_fn = add_fn or spring_client.add_to_cart
    get_cart_fn = get_cart_fn or spring_client.get_cart

    user_id, guest_id = cart_identity(identity)
    if user_id is None and guest_id is None:
        yield sse(
            "action",
            ActionData(
                type="CART_ADD_FAILED", message="담기에는 로그인이 필요해요.", reason="CART_ERROR"
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    # 되물음 진행 중이라도 사용자가 **다른 추천 상품**으로 전환하면 pending 을 버리고 새 담기로 처리한다
    # (옛 상품 옵션 되물음에 갇히지 않게 — decompose 가 전환을 새 productId 로 신호).
    pending = await cart_store.get_pending(thread_key)
    if (
        pending is not None
        and cart.product_id is not None
        and cart.product_id != pending.product_id
        and (allowed_product_ids is None or cart.product_id in allowed_product_ids)
    ):
        await cart_store.clear_pending(thread_key)
        pending = None
    markers_present, pending_option_mentioned = _pending_switch_signals(
        message, pending, settings.cart_pending_switch_markers
    )
    # 해소된 전환은 위 분기가 pending 을 지웠다. 여기 남은 pending + 전환 표지는 productId 가
    # 에코/null/미추천 값 중 무엇이든 해소 실패이므로 옛 상품에 적용하지 않는다.
    unresolved_switch = pending is not None and markers_present and not pending_option_mentioned
    if unresolved_switch:
        await cart_store.clear_pending(thread_key)
        pending = None
    if pending is not None:
        product_id: int | None = pending.product_id
        # 옵션 답변과 함께 수량을 다시 말하면("레드로 5개") 새 수량을 우선한다(기본 1이면 pending 유지).
        # 단 이번 턴이 pending 상품을 겨냥할 때만 — 순수 옵션 답변(productId=None)이거나 같은 상품일 때.
        # (다른/미추천 상품을 가리켜 전환이 성립 안 한 경우의 수량을 옛 상품에 잘못 적용하지 않게.)
        same_target = cart.product_id is None or cart.product_id == pending.product_id
        quantity = cart.quantity if (cart.quantity != 1 and same_target) else pending.quantity
        attempts = pending.attempts
    else:
        product_id = None if unresolved_switch else cart.product_id
        quantity = cart.quantity
        attempts = 0
    option_id = None if unresolved_switch else cart.option_id

    # 경로 B — SSE에 카드가 없어 문맥으로 상품을 확정한다. 신규 담기는 직전 추천(last_reco)에 있는
    # productId 만 허용(LLM 이 발화 속 임의 숫자를 오추출해 추천 안 된 상품을 담는 것 차단). 되물음
    # 진행(pending) 중이면 이미 검증된 상품이므로 예외.
    unresolved = product_id is None or (
        pending is None
        and allowed_product_ids is not None
        and product_id not in allowed_product_ids
    )
    if unresolved:
        yield sse(
            "token",
            TokenData(text=_unresolved_notice(screen_reason)).model_dump(by_alias=True),
        )
        yield _done()
        return

    # 담기 전 기존 보유 확인(안내용, degrade) — 동일 상품·옵션 보유 수량. 조회 결과 자체를 들고
    # 있다가 유일 옵션 자동 선택(#114)으로 옵션이 확정되면 아래에서 그 옵션 기준으로 다시 센다.
    existing_items: list[CartViewItem] = []
    try:
        cart_view = await get_cart_fn(user_id=user_id, guest_id=guest_id)
        existing_items = list(cart_view.items)
    except SpringUnavailableError:
        if trace := current_request_trace():
            trace.mark_degraded("cart_merge_skipped")
        pass  # 조회 실패해도 담기는 진행(§4.9)
    existing = _existing_quantity(existing_items, product_id, option_id)

    try:
        req = AddToCartRequest(
            user_id=user_id,
            guest_id=guest_id,
            product_id=product_id,
            option_id=option_id,
            quantity=quantity,
        )
        result, auto_option = await _add_with_single_option(add_fn, req)
    except CartOptionRequired as exc:
        # api-spec §4.1 — REQUIRED 는 **상한 없는 되물음 멀티턴**(사용자가 옵션을 아직 안 준 정상 흐름).
        # 각 되물음은 사용자 입력을 요구하므로 서버 무한 루프가 아니다. INVALID 카운터(attempts)는
        # 리셋하지 않고 보존해 사이에 끼어도 INVALID 상한이 유지되게 한다.
        await cart_store.set_pending(
            thread_key,
            PendingAdd(
                product_id=product_id, quantity=quantity, options=exc.options, attempts=attempts
            ),
        )
        yield sse(
            "token",
            TokenData(
                text=f"옵션을 선택해 주세요: {_options_text(exc.options)}. 어떤 걸로 담을까요?"
            ).model_dump(by_alias=True),
        )
        yield _done()
        return
    except CartOptionInvalid as exc:
        # api-spec §4.1 — INVALID 는 재시도 상한(config cart_option_reask_max, 기본 1) 후 CART_ERROR.
        new_attempts = attempts + 1
        if new_attempts > settings.cart_option_reask_max:
            await cart_store.clear_pending(thread_key)
            yield sse(
                "action",
                ActionData(
                    type="CART_ADD_FAILED",
                    message="옵션을 확인하지 못했어요. 다시 시도해 주세요.",
                    reason="CART_ERROR",
                ).model_dump(by_alias=True),
            )
            yield _done()
            return
        await cart_store.set_pending(
            thread_key,
            PendingAdd(
                product_id=product_id, quantity=quantity, options=exc.options, attempts=new_attempts
            ),
        )
        yield sse(
            "token",
            TokenData(
                text=f"그 옵션을 찾지 못했어요. 다시 골라 주세요: {_options_text(exc.options)}"
            ).model_dump(by_alias=True),
        )
        yield _done()
        return
    except CartProductNotFound:
        await cart_store.clear_pending(thread_key)
        yield sse(
            "action",
            ActionData(
                type="CART_ADD_FAILED",
                message="해당 상품을 찾지 못했어요.",
                reason="PRODUCT_NOT_FOUND",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return
    except CartStockInsufficient as exc:
        # I-2 재고 부족 — 남은 재고 노출("재고가 N개뿐이에요"). 재고 0(품절, BE ON_SALE+stock 0)은
        # "품절된 상품이에요", availableStock 미상 시 일반 안내(§4.1, 2026-07-22).
        await cart_store.clear_pending(thread_key)
        if exc.available_stock is None:
            message = "재고가 부족해 담지 못했어요."
        elif exc.available_stock == 0:
            message = "품절된 상품이에요."
        else:
            message = f"재고가 {exc.available_stock}개뿐이에요."
        yield sse(
            "action",
            ActionData(
                type="CART_ADD_FAILED",
                message=message,
                reason="STOCK_INSUFFICIENT",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return
    except CartQuantityExceeded:
        # I-2 수량 상한 초과(합산 > 99, BE VALIDATION_ERROR) — BE와 동일 문구, reason 은 CART_ERROR 유지.
        # 문구의 99 는 BE CartItem.MAX_QUANTITY 와 결합(변경 시 동기화 필요).
        await cart_store.clear_pending(thread_key)
        yield sse(
            "action",
            ActionData(
                type="CART_ADD_FAILED",
                message="수량은 최대 99개까지 담을 수 있습니다.",
                reason="CART_ERROR",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return
    except (CartError, SpringUnavailableError, ValidationError):
        await cart_store.clear_pending(thread_key)
        yield sse(
            "action",
            ActionData(
                type="CART_ADD_FAILED",
                message="장바구니에 담지 못했어요. 잠시 후 다시 시도해 주세요.",
                reason="CART_ERROR",
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    # 성공 — 되물음 상태 정리 + 합산 안내.
    await cart_store.clear_pending(thread_key)
    # "방금 담은 거" 삭제 해소 소스(이슈 #116) — 담기 **성공** 경로에서만 기록한다(실패·되물음은
    # 갱신하지 않음). cart_item_id 가 없으면(계약상 있어야 하지만 방어) 해소에 쓸 수 없어 저장하지
    # 않는다.
    # [라운드 3 리뷰 F-2] 이 값을 읽는 곳은 삭제 흐름뿐이라 cart_remove_enabled=False 인 기본
    # 배포에서는 아무도 읽지 않는 쓰기다 — 플래그로 가두지 않으면 (1) "플래그 off 면 기존 동작과
    # 동일"이라는 이 레인의 수용 기준에 저장소 부작용이 하나 더 생기고, (2) 이 자리는 Spring 담기가
    # 이미 성공한 뒤 CART_ADDED 를 내기 전이라, 쓰지도 않을 값 때문에 스토어 실패가 "상품은 담겼는데
    # 사용자는 깨진 턴을 본다"는 새 실패 모드를 성공 경로에 얹게 된다.
    if settings.cart_remove_enabled and result.cart_item_id is not None:
        try:
            await cart_store.set_last_add(thread_key, result.cart_item_id, product_id)
        except Exception as exc:  # noqa: BLE001 - Spring 담기가 이미 성공한 뒤라 이 쓰기 실패로
            # CART_ADDED/done 을 죽이면 안 된다(2차 리뷰 지적 6·1번) — 상품은 담겼는데 사용자는
            # 실패를 보는 게 더 나쁘다. "방금 담은 거" 해소만 다음 턴에 모르는 상태로 degrade될
            # 뿐이라 로그만 남기고 성공 흐름은 그대로 진행한다(`ThreadFilterStore.get` 과 같은
            # 취지). CancelledError 는 BaseException 이라 전파된다.
            _log.warning("last_add_write_failed", extra={"reason": str(exc)})
    if auto_option is not None:
        # 담길 옵션이 확정됐으니 그 옵션 기준으로 다시 센다 — 담기 전 계산은 optionId 미상이라
        # 지금 후보에 없는 옛 옵션(단종·품절)의 보유까지 합산할 수 있고, 그러면 Spring 은 새 줄로
        # 담았는데 "수량을 더했어요"라고 말하게 된다(PR #211 리뷰 / REQ-CART-031 합산 권위=Spring).
        existing = _existing_quantity(existing_items, product_id, auto_option.option_id)
    if existing > 0:
        message = "이미 담겨 있던 상품이라 수량을 더했어요."
    else:
        message = "장바구니에 담았어요."
    # 유일 옵션을 AI 가 대신 골랐으면 무엇으로 담았는지 밝힌다(이슈 #114). 라벨은 되물음 문구와
    # 같은 _option_label — 추가금도 함께 알린다(자동 선택은 사용자가 고를 기회가 없었으므로 숨기면
    # 결제 단계에서야 알게 된다, PR #211 리뷰). 이름이 비면 기본 문구를 유지한다.
    if auto_option is not None and (option_label := _option_label(auto_option)):
        message = (
            f"{option_label} 옵션으로 담아 수량을 더했어요."
            if existing > 0
            else f"{option_label} 옵션으로 담았어요."
        )
    yield sse(
        "action",
        ActionData(type="CART_ADDED", message=message, cart_item_id=result.cart_item_id).model_dump(
            by_alias=True
        ),
    )
    yield _done()


async def stream_cart_view(*, identity, get_cart_fn=None, observer=None) -> AsyncIterator[str]:
    """조회 서브그래프. 장바구니 내용을 token 텍스트로 답한다(§4.9, 별도 이벤트 없음)."""
    get_cart_fn = get_cart_fn or spring_client.get_cart
    user_id, guest_id = cart_identity(identity)
    if user_id is None and guest_id is None:
        yield sse(
            "token",
            TokenData(text="장바구니를 보려면 로그인이 필요해요.").model_dump(by_alias=True),
        )
        yield _done()
        return

    try:
        cart_view = await get_cart_fn(user_id=user_id, guest_id=guest_id)
    except SpringUnavailableError:
        yield sse(
            "token",
            TokenData(text="장바구니를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.").model_dump(
                by_alias=True
            ),
        )
        yield _done()
        return

    if not cart_view.items:
        yield sse("token", TokenData(text="장바구니가 비어 있어요.").model_dump(by_alias=True))
        yield _done()
        return

    lines = []
    for item in cart_view.items:
        product_name = _strip_unsafe(item.product_name or "상품")
        option_name = _strip_unsafe(item.option_name) if item.option_name else ""
        opt = f" ({option_name})" if option_name else ""
        lines.append(f"{product_name}{opt} · {item.quantity}개")
    text = "장바구니에 담긴 상품이에요:\n" + "\n".join(lines)
    yield sse("token", TokenData(text=text).model_dump(by_alias=True))
    yield _done()
