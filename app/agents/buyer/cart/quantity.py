"""장바구니 수량 변경 서브그래프 (이슈 #285, I-25 — 확정 2026-08-05, §4.13).

`remove.py` 와 구조·어조를 맞춘 형제 모듈이지만 **실패 처분이 정반대다** — I-24(삭제)의 404
`CART_ITEM_NOT_FOUND` 는 "이미 빠져 있어요"(성공 안내)로 종료하지만, I-25 의 같은 404 는
**실패**다(패킷 함정 1). "수량을 3개로 바꿔 달라"는데 대상 항목이 없으면 목표에 도달하지
못한 것이지 원하는 상태에 이른 것이 아니다.

`stream_cart_add`(`cart/graph.py`)가 `classify_cart_utterance` 로 `"cart_quantity"` 로 판정하면
2선 방어로 위임받고, `decompose` 가 직접 `cart_quantity` intent 를 낸 발화는 `buyer/graph.py` 가
곧바로 위임한다(`cart_remove` 와 대칭 구조). 대상 해소는 결정론적이다 — LLM 을 새로 부르지
않는다. I-25 는 단건이다(path 에 cartItemId 하나) — bulk 가 없다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.agents.buyer._frames import sse
from app.agents.buyer.cart.identity import cart_identity
from app.agents.buyer.cart.negation import has_any_negation, matches_name_unnegated
from app.agents.buyer.cart.purchase_state import state_suffix
from app.agents.buyer.recommendation.state import CartIntent
from app.core.text import _strip_unsafe
from app.schemas.chat import ActionData, DoneData, TokenData
from app.schemas.spring import CartViewItem
from app.services import spring_client
from app.services.spring_client import (
    CartError,
    CartItemNotFound,
    CartStockInsufficient,
    SpringUnavailableError,
)


def _done() -> str:
    return sse("done", DoneData(finish_reason="stop").model_dump(by_alias=True))


def _display_name(item: CartViewItem) -> str:
    """안내 문구용 상품명 — 반드시 `_strip_unsafe` 를 거친다(판매자 입력, `remove.py` 와 같은 규약)."""
    return _strip_unsafe(item.product_name or "") or "상품"


def _resolve_quantity_target(
    message: str, items: list[CartViewItem], settings, target_quantity: int
) -> CartViewItem | None:
    """수량 변경 대상을 결정론적으로 해소한다(패킷 §5.5) — `remove.py::_resolve_remove_targets`
    와 같은 부품(`negation.matches_name_unnegated`·`_strip_unsafe`·옵션 구분자 후보)을 쓰되
    동작 표지는 `settings.cart_quantity_markers` 를 넘긴다.

    I-25 는 **단건**이다(path 에 cartItemId 하나) — 이름이 2건 이상에 걸리면 임의로 하나를
    고르지 않고 그 자리에서 되물음이다(`remove.py` 의 "강한 신호가 모호하면 더 약한 신호로
    내려가지 않는다" 원칙과 동일).

    `remove.py` 의 "전체 삭제"(`cart_remove_all_markers`)·"방금 담은 거"(`last_add`) 규칙은
    옮기지 않는다 — 전 항목 수량을 일괄 치환하는 계약이 없고(§4.13 은 단건 PATCH 만 정의),
    "방금 담은 거"도 이 계약엔 대응 개념이 없다(패킷이 명시적으로 제외했다). 남는 것은 "이름
    매칭"과 "이름·부정 언급이 전혀 없고 장바구니가 1건뿐일 때의 단건 자동"뿐이다 — 둘 다
    `remove.py` 규칙 2·4 와 같은 원리(강한 신호 우선, 코드가 고르는 자리는 더 엄격해야 한다).

    **동적 수량 표지(실측으로 발견한 보강)** — `matches_name_unnegated` 의 오른쪽 경계는
    "이름 + (조사) + (filler) + 표지"만 인정하는데, `utterance_name_trailing_filler_words`
    는 숫자를 모르는 정적 목록이라 "이어폰 **3개로** 바꿔줘"처럼 이름과 표지 사이에 사용자가
    말한 숫자가 끼면(가장 자연스러운 실제 phrasing) 정적 `cart_quantity_markers` 만으로는
    표지가 이름 바로 뒤에서 시작하지 않아 매칭에 실패한다(장바구니가 1건뿐이어도 `name_
    mentioned` 가드에 걸려 단건 자동으로도 못 새 — 실측 재현, 이 함수의 인자로 `target_
    quantity` 를 받게 된 이유). 하지만 그 숫자는 이미 **확정된 값**이다 — 이 함수가 불릴
    시점엔 `target_quantity` 가 `None` 이 아님이 상위(`stream_cart_quantity_change`)에서
    보장된다(함정 3 가드를 통과한 값). 그래서 `f"{target_quantity}개로 …"` 형태의 표지를
    정적 목록에 **더해** 넘긴다 — 추측이 아니라 이미 아는 숫자를 리터럴로 재사용하는 것이라
    새 판정 로직이 아니다.
    """
    has_negation = has_any_negation(
        message, settings.utterance_negation_markers, settings.utterance_prefix_negation_markers
    )
    name_mentioned = any(
        (name := _strip_unsafe(item.product_name or "")) and name in message for item in items
    )
    all_names = [name for item in items if (name := _strip_unsafe(item.product_name or ""))]
    action_markers = settings.cart_quantity_markers + [
        f"{target_quantity}개로 바꿔",
        f"{target_quantity}개로 변경",
        f"{target_quantity}개로 해줘",
        f"{target_quantity}개로",
        f"{target_quantity}개",
    ]

    def _matches_any(candidates: list[str]) -> bool:
        return any(
            matches_name_unnegated(
                message,
                candidate,
                settings.utterance_negation_markers,
                settings.utterance_negation_window,
                settings.utterance_prefix_negation_markers,
                settings.utterance_name_boundary_particles,
                settings.utterance_name_trailing_filler_words,
                action_markers,
                all_names,
            )
            for candidate in candidates
        )

    # product_name 이 같고 option_name 만 다른 항목은 맨이름만으로 영원히 모호하다 —
    # `remove.py` 와 같은 이유로 옵션 구분자 형태를 이름 매칭 후보에 추가한다(새 판정 로직을
    # 만들지 않고 후보 문자열만 늘린다).
    candidates_by_item: list[tuple[CartViewItem, list[str], list[str]]] = []
    for item in items:
        base = _strip_unsafe(item.product_name or "")
        if not base:
            continue
        option = _strip_unsafe(item.option_name) if item.option_name else ""
        qualified = (
            [f"{base} ({option})", f"{base}({option})", f"{base} {option}"] if option else []
        )
        candidates_by_item.append((item, [base, *qualified], qualified))

    qualified_matches = [
        item for item, _, qualified in candidates_by_item if qualified and _matches_any(qualified)
    ]
    if len(qualified_matches) == 1:
        return qualified_matches[0]

    name_matches = [item for item, candidates, _ in candidates_by_item if _matches_any(candidates)]
    if name_matches:
        return name_matches[0] if len(name_matches) == 1 else None

    # 이름·부정 언급이 전혀 없고 장바구니가 1건뿐일 때만 그 1건(`remove.py` 규칙 4 와 동일 원리
    # — 표지도 이름도 없는 발화에서만 적용, 이름을 대려다 실패한 경우는 위에서 이미 되물음으로
    # 끝난다).
    if not has_negation and not name_mentioned and len(items) == 1:
        return items[0]
    return None


def _quantity_unresolved_notice(items: list[CartViewItem]) -> str:
    """대상 미해소 되물음 문구 — `remove.py::_unresolved_notice` 와 같은 철학(상태 저장 없음,
    다음 답을 판별기가 다시 잡을 수 있는 형태로 유도, #310 구매 가능 상태 라벨)이지만 예시는
    수량 변경 어휘("N개로 바꿔줘")로 쓴다."""
    names = ", ".join(f"{_display_name(item)}{state_suffix(item.purchase_state)}" for item in items)
    example_item = next(
        (item for item in items if item.purchase_state in (None, "AVAILABLE")), items[0]
    )
    example = _display_name(example_item)
    return (
        f"지금 장바구니에 있는 상품: {names}. 예) '{example} 3개로 바꿔줘'처럼 "
        "상품명과 수량을 함께 말씀해 주세요."
    )


async def stream_cart_quantity_change(
    *,
    identity,
    cart: CartIntent,
    message: str,
    settings,
    get_cart_fn=None,
    change_fn=None,
    observer=None,
) -> AsyncIterator[str]:
    """수량 변경 서브그래프. `action`(CART_QUANTITY_CHANGED/CART_QUANTITY_CHANGE_FAILED) 또는
    되물음 `token` 을 내고 `done` 1회로 끝난다.

    **패킷 함정 1** — I-24 와 달리 404(`CartItemNotFound`)는 실패다. **패킷 함정 3** —
    `cart.target_quantity` 가 `None`(목표 수량 미상)이면 어댑터를 호출하지 않고 곧장 되물음으로
    끝낸다(장바구니 조회조차 하지 않는다 — 수량을 모르면 어차피 진행할 수 없다).
    """
    get_cart_fn = get_cart_fn or spring_client.get_cart
    change_fn = change_fn or spring_client.change_cart_quantity

    user_id, guest_id = cart_identity(identity)
    if user_id is None and guest_id is None:
        yield sse(
            "token",
            TokenData(text="장바구니 수량 변경에는 로그인이 필요해요.").model_dump(by_alias=True),
        )
        yield _done()
        return

    # [패킷 함정 3] 목표 수량이 없으면(추출 실패·범위 밖) 조용히 1개로 치환하지 않는다 — 되물어야
    # 한다. 장바구니 조회조차 하지 않는다: 어차피 얼마로 바꿀지 모르면 대상을 해소해도 진행할
    # 수 없고, 불필요한 Spring 왕복을 만들 이유가 없다.
    target_quantity = cart.target_quantity
    if target_quantity is None:
        yield sse(
            "token",
            TokenData(
                text="몇 개로 바꿔드릴까요? '3개로 바꿔줘'처럼 수량을 말씀해 주세요."
            ).model_dump(by_alias=True),
        )
        yield _done()
        return

    try:
        cart_view = await get_cart_fn(user_id=user_id, guest_id=guest_id)
    except SpringUnavailableError:
        yield sse(
            "action",
            ActionData(
                type="CART_QUANTITY_CHANGE_FAILED",
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

    target = _resolve_quantity_target(message, items, settings, target_quantity)
    if target is None:
        yield sse(
            "token",
            TokenData(text=_quantity_unresolved_notice(items)).model_dump(by_alias=True),
        )
        yield _done()
        return

    name = _display_name(target)
    try:
        result = await change_fn(
            target.cart_item_id, target_quantity, user_id=user_id, guest_id=guest_id
        )
    except CartStockInsufficient as exc:
        # 문구는 `cart/graph.py` 의 I-2 담기 재고 부족 분기를 그대로 따른다(패킷 T4 — "새 문구를
        # 발명하지 마라"). available_stock 미상은 일반 안내, 0 은 품절, 그 외는 잔여 재고 수.
        if exc.available_stock is None:
            message_text = "재고가 부족해 담지 못했어요."
        elif exc.available_stock == 0:
            message_text = "품절된 상품이에요."
        else:
            message_text = f"재고가 {exc.available_stock}개뿐이에요."
        yield sse(
            "action",
            ActionData(
                type="CART_QUANTITY_CHANGE_FAILED",
                message=message_text,
                cart_item_id=target.cart_item_id,
                reason="STOCK_INSUFFICIENT",
            ).model_dump(by_alias=True),
        )
    except CartItemNotFound:
        # [패킷 함정 1] I-25 의 404 는 I-24 와 정반대로 **실패**다 — "이미 빠져 있어요"류 성공
        # 안내로 옮기면 계약 위반이자 거짓 안내다(§4.13 실패표). 재시도를 유도하지 않는다 —
        # 항목 자체가 없어서 다시 시도해도 같은 결과다(비멱등, delete_cart_item 과 같은 근거).
        yield sse(
            "action",
            ActionData(
                type="CART_QUANTITY_CHANGE_FAILED",
                message=f"장바구니에 없는 상품이에요: {name}",
                cart_item_id=target.cart_item_id,
                reason="CART_ERROR",
            ).model_dump(by_alias=True),
        )
    except CartError:
        yield sse(
            "action",
            ActionData(
                type="CART_QUANTITY_CHANGE_FAILED",
                message=f"수량을 바꾸지 못했어요: {name}. 잠시 후 다시 시도해 주세요.",
                cart_item_id=target.cart_item_id,
                reason="CART_ERROR",
            ).model_dump(by_alias=True),
        )
    else:
        # [패킷 T4 §5] 1단계 어댑터의 결과 모델은 응답 payload 에 quantity 키가 없으면 None 을
        # 담는다(§4.13 은 항상 싣는다고 규정하지만 방어가 필요하다) — "수량을 None개로 바꿨어요"
        # 가 나가면 안 된다. 요청에 쓴 목표 수량(target_quantity)으로 폴백한다 — 그 값은 우리가
        # 보낸 치환 요청값이라, 호출이 성공했다면(예외 없이 여기 도달했다면) 최종 수량과 같다.
        final_quantity = result.quantity if result.quantity is not None else target_quantity
        yield sse(
            "action",
            ActionData(
                type="CART_QUANTITY_CHANGED",
                message=f"수량을 {final_quantity}개로 바꿨어요",
                cart_item_id=target.cart_item_id,
                quantity=final_quantity,
            ).model_dump(by_alias=True),
        )
    yield _done()
