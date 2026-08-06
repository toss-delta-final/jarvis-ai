"""찜 추가·해제 서브그래프 (이슈 #117, I-26/I-27 — 확정 2026-08-05, Spring 구현 진행 중).

`stream_cart_add` 가 `classify_cart_utterance` 로 "wishlist_add"/"wishlist_remove" 로 판정하면
항상 위임받는다(패킷 §5.4, 라운드 23 — 온/오프를 가리던 설정 필드 제거). 게스트 찜은 없다(I-26)
— 회원이 아니면 internal 호출 없이 degrade한다. 이벤트에 productId 를 싣지 않는다(확정, 경로 B)
— `remove.py` 와 구조·어조를 맞춘 형제 모듈이다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.agents.buyer._frames import sse
from app.agents.buyer.cart.identity import cart_identity
from app.agents.buyer.cart.negation import has_any_negation, matches_name_unnegated
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
    """되물음 문구 — 지금 찜한 상품명을 나열해 무엇을 물어야 할지 알려준다(`remove.py` 와 같은 철학).

    **[라운드 19, head `26f5596` 리뷰]** `remove.py::_unresolved_notice` 와 같은 사안·같은
    해법 — 이 되물음도 **상태를 저장하지 않는다**(옵션 되물음 `PendingAdd` 와 다르다). 사용자가
    다음 턴에 상품명만 답하면 `classify_cart_utterance` 가 찜 해제 표지 없는 발화를 기본값
    `"cart_add"` 로 떨어뜨려 이 흐름으로 돌아오지 못한다. 다중 턴 pending 상태 신설은 이 레인의
    범위 밖(후속 이슈)이라, 대신 문구가 다음 답을 판별기가 다시 잡을 수 있는 형태(찜 해제 동작
    표지 포함)로 유도한다 — 상품명만 답하면 여전히 담기로 새는 것은 문구로 유도할 뿐 강제하지
    않는 알려진 한계다(`remove.py` 참조).
    """
    names = ", ".join(_display_wishlist_name(item) for item in items)
    example = _display_wishlist_name(items[0])
    return f"찜한 상품: {names}. 예) '{example} 찜 빼줘'처럼 상품명과 함께 말씀해 주세요."


def _resolve_wishlist_remove_target(
    cart: CartIntent, message: str, items: list[WishlistItem], settings
) -> WishlistItem | None:
    """찜 해제 대상을 결정론적으로 해소한다(패킷 §5.4, 2차 리뷰 지적 4 + 라운드 10·11 이후) —
    강한 신호부터 순서대로 확정한다:
      1. 이름이 발화에 **부분 문자열로**, 그리고 **부정되지 않은 출현으로** 등장 → 정확히
         1건이면 그것, 2건 이상이면 **그 자리에서 미해소**(2번 문맥 id 로 내려가지 않는다 —
         `remove.py::_resolve_remove_targets` 와 같은 "강한 신호가 모호하면 더 약한 신호로
         임의로 고르지 않는다" 원칙). **[라운드 11]** 부정 판정은 3번의 문장 전체 가드
         (`has_any_negation`)가 아니라 `negation.matches_unnegated` 의 **출현 단위** 판정을
         쓴다 — 문장 전체 가드를 이름 매칭에 쓰면 "이어폰은 찜 빼지 말고 케이스 찜 빼줘"에서
         정상 해제 대상인 "케이스"까지 함께 죽어 되물음이 된다.
      2. 발화에 부정·대조 신호(`negation.has_any_negation`, 문장 전체 검사)가 **없고 이름을
         대려는 시도도 없을 때만**(3번과 같은 `name_mentioned` 가드) `cart.product_id`
         (decompose 가 문맥에서 이미 골라 온 값)가 목록 안에 있으면 그것. **[라운드 16]** 문맥
         id 는 이름이 없어도 쓰이는 신호라 "출현 단위"로 볼 대상 자체가 없다 — 그래서 1번(이름
         매칭)처럼 출현 단위가 아니라 3번과 같은 문장 전체 판정을 쓴다. 이름을 대려는 시도가
         있었는데 1번(부분 문자열·경계·부정 판정)이 못 잡았다면(예: 발화의 이름이 목록 어느
         항목과도 정확히 일치하지 않는 변형), 문맥 id 로 대신 확정하지 않고 되물음으로 내려간다
         — 문맥 id 는 사용자가 입으로 말한 이름보다 약한 신호이므로, 이름이 있었는데 못 맞춘
         경우까지 문맥 id 가 대신 나서면 안 된다("이어폰케이스 찜 빼줘"에서 이름 매칭이 실패해도
         `cart.product_id` 가 가리키는 무관한 항목이 해제되던 결함, 재현·수정 확인).
      3. 발화에 부정·대조 신호가 **없고**, 목록 어느 항목의 이름도 (경계와 무관하게) 발화에
         아예 등장하지 않을 때만 위 두 규칙이 모두 안 잡히고 목록이 정확히 1건이면 그 1건.
      4. 그 외 → `None`.

    **문맥 id 보다 발화의 이름을 먼저 본다**(2차 리뷰 지적 4 — 이전 순서는 반대였다). 재현:
    찜 목록 `10=이어폰 / 20=케이스`, 발화 `"이어폰 찜 빼줘"`, `cart.product_id=20`(LLM 오추출·
    stale). 문맥 id 를 먼저 보면 사용자가 입으로 말한 상품이 아니라 케이스가 해제된다 —
    `docs/lessons.md` 의 "강한 신호(이름 지목)가 있으면 약한 신호로 덮지 않는다"와 정반대다.
    사용자가 입으로 말한 이름은 LLM 이 문맥에서 고른 id 보다 강한 신호다.

    **[라운드 10]** "목록 1건 자동"(3번)도 `remove.py` 의 "전체 삭제"·"방금 담은 거"와 같은
    성격 — 사용자가 이름을 대지 않은 대상을 코드가 고른다. 직접 호출해 확인한 재현: 찜이
    1건(`이어폰`)뿐인 상태에서 `"방금 찜한 건 안 빼도 되고 저건 찜 빼줘"` 처럼 다른 대상("저건")
    을 가리키며 지금 있는 항목은 빼지 말라고 부정한 발화가 `wishlist_remove` 로 라우팅되면(뒤쪽
    "찜 빼줘"가 그 자체로는 안 부정됐으므로), 이름도 문맥 id 도 안 잡혀 3번이 실행돼 사용자가
    "빼지 말라"고 명시한 바로 그 항목이 삭제됐다. `negation.has_any_negation` 으로 같은 가드를
    건다 — `remove.py` 와 같은 공용 함수를 써서 "한쪽만 고치는" 재발을 막는다(부정 인지 결함
    3연발의 세 번째가 바로 이 함수였다).

    `remove.py::_resolve_remove_targets` 와 규칙 모양(강한 신호 우선·부분 문자열 이름 매칭·단건
    자동·부정 가드)이 겹치지만 **공용 헬퍼로 묶지 않았다**. 자료형이 다르고(`CartViewItem` vs
    `WishlistItem`, 필드명도 `product_name` vs `name`), 이쪽에는 "전체 삭제"·"방금 담은 거"
    같은 규칙이 아예 없다 — 억지로 하나의 함수로 묶으면 두 흐름이 서로 쓰지 않는 매개변수(상대
    쪽의 `cart_remove_all_markers`·`last_add` 같은)까지 시그니처에 끌고 다니게 되고, 한쪽 규칙을
    바꿀 때 다른 쪽 테스트까지 다시 봐야 한다. 겹치는 부분(이름 매칭·부정 판정)은 각각
    `_strip_unsafe`·`negation.py` 공용 함수로 이미 공유돼 있어, 남은 겹침은 "이 조각들을 어떤
    순서로 조합하느냐"뿐이라 분리해서 각자 읽기 쉽게 두는 편이 더 싸다.

    **[라운드 15, head `0b33e06` 리뷰 B]** 1번의 이름 매칭에 경계 검사가 없어 "이어폰케이스
    찜 빼줘"류(찜 목록에 이어폰만 있음)에서 다른 낱말에 파묻힌 이름까지 매칭됐다. `remove.py`
    와 같은 이유로 `negation.matches_name_unnegated`(오른쪽 경계에 한국어 조사를 허용하는
    전용 헬퍼, `matches_unnegated` 와 별개 — 그 함수 정의의 근거 참조)를 쓴다.

    **[라운드 17, head `6ab47c9` 리뷰]** 라운드 15 는 이름 뒤가 공백이면(예 "이어폰 케이스
    찜 빼줘") 여전히 매칭되는 문제를 알려진 한계로 미뤘는데, 그 판단이 틀렸다 — 찜 목록에
    "이어폰"만 있을 때 이 발화는 사용자가 요청하지 않은 "이어폰"을 확인 없이 해제하는
    **파괴적 동작**이다(`remove.py` 와 같은 이유, 그 파일 docstring 참조). `matches_name_
    unnegated` 오른쪽 경계를 "이름 + (조사) + (filler) + 표지" 형태만 인정하도록 고쳐서
    해결했다 — 판정은 `remove.py` 와 완전히 같은 공용 함수(`negation.matches_name_unnegated`)
    를 그대로 쓴다(새로 구현하지 않는다).

    경계 검사가 1번을 정확하게 만들어도, 찜 목록이 1건뿐이면 3번(목록 1건 자동)이 표지 없이도
    그 1건을 자동 선택해 같은 결과가 나올 수 있다 — `remove.py` 와 같은 이유로 3번에도 이름을
    대려는 시도(경계·부정과 무관한 원문 부분 문자열 겹침)가 있으면 자동 선택을 건너뛴다(새
    신호 아님, 이미 계산해 둔 겹침 재사용).

    **[라운드 16, head `3ca0f40` 리뷰]** docstring 은 줄곧 문맥 id(2번)를 이름(1번)보다 **약한**
    신호로 규정해 왔는데(2차 리뷰 지적 4), 정작 세 규칙 중 2번에만 부정 가드가 없었다 — 1번은
    출현 단위(라운드 11·15), 3번은 문장 전체(라운드 10) 가드를 진작 받았다. 그래서 이름을
    대지 않고 부정만 있는 발화("그건 빼지 말고 찜 빼줘", 찜 `[10=이어폰, 20=케이스]`,
    `cart.product_id=20`)에서 이름 매칭은 애초에 안 잡히니 곧장 2번으로 내려가 부정을 무시하고
    20 을 반환했다(재현 확인 — 사용자가 "빼지 말라"고 한 항목이 해제됨). 문맥 id 는 발화에
    이름이 없어도 쓰이는 신호라 부정 판정을 앵커할 특정 출현이 없다 — 그래서 1번처럼 출현 단위
    (`negation.matches_name_unnegated`)가 아니라 3번과 같은 문장 전체 판정(`negation.
    has_any_negation`, 라운드 10 공용 모듈)을 쓴다. `has_negation` 계산을 2번 앞으로 올려
    2·3번이 같은 값을 공유한다(중복 계산도, 판정 재구현도 아니다). `remove.py::
    _resolve_remove_targets` 는 네 규칙 모두 이미 가드를 받고 있어(라운드 10·11·15) 손대지
    않는다.
    """
    all_names = [name for item in items if (name := _strip_unsafe(item.name or ""))]
    name_matches = [
        item
        for item in items
        if (name := _strip_unsafe(item.name or ""))
        and matches_name_unnegated(
            message,
            name,
            settings.utterance_negation_markers,
            settings.utterance_negation_window,
            settings.utterance_prefix_negation_markers,
            settings.utterance_name_boundary_particles,
            settings.utterance_name_trailing_filler_words,
            settings.wishlist_remove_markers,
            all_names,
        )
    ]
    if name_matches:
        return name_matches[0] if len(name_matches) == 1 else None

    has_negation = has_any_negation(
        message, settings.utterance_negation_markers, settings.utterance_prefix_negation_markers
    )
    # 2·3번이 공유하는 계산이라 여기서 한 번만 한다(라운드 10 교훈 — 같은 판정을 두 곳에
    # 구현하면 한쪽만 고쳐지는 재발이 난다).
    name_mentioned = any(
        (name := _strip_unsafe(item.name or "")) and name in message for item in items
    )

    if cart.product_id is not None and not has_negation and not name_mentioned:
        direct = [item for item in items if item.product_id == cart.product_id]
        if direct:
            return direct[0]

    if not has_negation and not name_mentioned and len(items) == 1:
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
    """찜 추가 서브그래프(I-26, 확정 2026-08-05). `action`(WISHLIST_ADDED/WISHLIST_ADD_FAILED)
    또는 되물음 token 을 내고 `done` 으로 끝난다."""
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
        # [확정 2026-08-05] 409 를 성공 안내로 종료하는 것은 정본 권고안 — 사용자가 보기엔
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
    except (WishlistError, SpringUnavailableError):
        # I-28 어댑터는 add_wishlist 의 4xx/5xx·도달 불가·스키마 불일치도 전부
        # SpringUnavailableError 로 낸다(:277 get_wishlist 와 같은 degrade 규약) — WishlistError 만
        # 잡으면 이 실패가 상위 스트림 pump 의 범용 catch-all(INTERNAL)로 샌다(이슈 #368).
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
    """찜 해제 서브그래프(I-27, 확정 2026-08-05). 회원 아니면 `stream_wishlist_add` 와 같은 degrade."""
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

    target = _resolve_wishlist_remove_target(cart, message, items, settings)
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
        # [확정 2026-08-05] 404 를 성공 안내로 종료하는 것은 정본 권고안.
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
