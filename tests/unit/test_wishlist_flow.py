"""찜 추가·해제 흐름 (이슈 #117, 패킷 §5.4) — `stream_wishlist_add`/`stream_wishlist_remove`
대상 해소·오류 매핑·경로 B 가드·배선.

주입 fn 으로 단위 테스트한다(`test_cart_remove.py` 와 같은 패턴) — 스트림 한 갈래를 그 갈래만
태워 보는 데는 이게 가장 싸다.

**[#386]** "Spring 이 아직 구현하지 않아 통합 테스트를 못 한다"는 이 파일의 옛 전제는 이제
사실이 아니다(#436 실측 — BE main 에 I-26/I-27/I-28 구현됨). 찜 조회는
`tests/integration/_stubs.py` 의 `GET /internal/wishlist` 라우트로 **실 HTTP 경계 e2e 도**
갖췄다(`test_degrade_e2e.py`) — 여기 단위 테스트와 역할이 갈린다: 이 파일은 갈래별 판정을,
저쪽은 어댑터·스트림·라우팅을 관통하는 경로를 잰다.
"""

from __future__ import annotations

import json

from app.agents.buyer.cart.graph import stream_cart_add
from app.agents.buyer.cart.state import CartStateStore, PendingAdd
from app.agents.buyer.cart.wishlist import (
    stream_wishlist_add,
    stream_wishlist_remove,
    stream_wishlist_view,
)
from app.agents.buyer.recommendation.state import CartIntent
from app.core.auth import Identity
from app.core.config import get_settings
from app.schemas.spring import CartOption, WishlistAddResult, WishlistItem, WishlistView
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
    return WishlistItem(product_id=product_id, name=name, purchase_state="AVAILABLE")


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


async def test_wishlist_add_spring_unavailable_maps_to_wishlist_error() -> None:
    """주입된 add_wishlist_fn 이 SpringUnavailableError 를 내도 INTERNAL 로 죽지 않고 형제와 같은
    WISHLIST_ADD_FAILED/WISHLIST_ERROR degrade 로 끝난다(이슈 #368). 기본 어댑터
    spring_client.add_wishlist(I-26)는 이 예외를 내지 않는다 — 이 테스트는 "어댑터가 이 예외를
    낸다"는 증거가 아니라 주입 fn 이 낼 때의 방어를 검증한다.

    게스트는 로그인 게이트(user_id is None)가 Spring 호출 자체를 선행 차단하므로 이 갭은
    member 경로에서만 밟힌다(이슈 #368 실측, combo-0057 member×spring_timeout) — 그래서
    member identity(_member(), 숫자 user_id "123")로 테스트한다.
    """

    async def add_wishlist_fn(request):
        raise SpringUnavailableError("boom")

    events = await _collect(
        stream_wishlist_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            settings=get_settings(),
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    assert _types(events) == ["action", "done"]
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


async def test_wishlist_add_unresolved_notice_is_byte_identical_without_last_reco() -> None:
    """[#435 W4] `has_last_reco` 기본값(False)은 오늘 문구와 바이트 동일하다."""
    events = await _collect(
        stream_wishlist_add(
            identity=_member(),
            cart=CartIntent(product_id=None, quantity=1),
            settings=get_settings(),
        )
    )
    text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert text == "어떤 상품을 찜할까요? 추천을 먼저 받아보시면 찜해 드릴게요."


async def test_wishlist_add_unresolved_notice_mentions_naming_when_last_reco_present() -> None:
    """[#435 W4] `has_last_reco=True` 면 "이미 추천을 받았다"는 사실을 반영한 문구로 바뀐다."""
    events = await _collect(
        stream_wishlist_add(
            identity=_member(),
            cart=CartIntent(product_id=None, quantity=1),
            settings=get_settings(),
            has_last_reco=True,
        )
    )
    text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert (
        text == "어떤 상품을 찜할까요? 추천해 드린 상품 중에서 이름을 말씀해 주시면 찜해 드릴게요."
    )


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


# ─── 라운드 16(head `3ca0f40` 리뷰): 문맥 productId 규칙(2번)도 문장 전체 부정 가드 ───


def test_resolve_wishlist_remove_target_negated_context_id_asks() -> None:
    """재현(라운드 16 패킷) — 이름을 대지 않고 어미형 부정만 있는 발화는 문맥 id 를 무시해야
    한다. 고치기 전엔 세 규칙 중 2번(문맥 id)에만 가드가 없어 "그건 빼지 말고 찜 빼줘"가
    사용자가 배제하려던 항목(20=케이스)을 그대로 반환했다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=20), "그건 빼지 말고 찜 빼줘", items, get_settings()
    )
    assert result is None


def test_resolve_wishlist_remove_target_prefix_negated_context_id_asks() -> None:
    """재현 2 — 접두형 부정("안")도 같은 이유로 문맥 id 를 무시해야 한다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=20), "안 빼도 되고 찜 빼줘", items, get_settings()
    )
    assert result is None


def test_resolve_wishlist_remove_target_context_id_still_works_without_negation() -> None:
    """회귀 — 부정 신호가 없으면 문맥 id 규칙은 그대로 동작한다(기존 동작 불변)."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=20), "찜 빼줘", items, get_settings()
    )
    assert result is not None
    assert result.product_id == 20


def test_resolve_wishlist_remove_target_unmatched_name_does_not_fall_back_to_context_id() -> None:
    """재현·수정 확인 — "이어폰케이스 찜 빼줘"는 사용자가 이름을 대려는 시도를 했지만(경계
    검사로 어느 항목과도 정확히 일치하지 않는다), 문맥 id(`cart.product_id`)가 그 시도와
    무관한 항목을 대신 확정해선 안 된다. 사용자가 입으로 말한 이름은 LLM 이 문맥에서 고른 id
    보다 강한 신호라는 이 파일의 원칙이 지금은 1번(이름 매칭)에만 적용되고 2번(문맥 id)에는
    안 걸려 있었다 — 이름 매칭이 실패로 끝나면 곧장 문맥 id 로 폴백해 무관한 항목이 해제됐다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=20), "이어폰케이스 찜 빼줘", items, get_settings()
    )
    assert result is None


async def test_wishlist_remove_negated_context_id_asks_via_stream() -> None:
    """`stream_wishlist_remove` 수준에서도 같은 사실 — remove_wishlist_fn 이 한 번도 안 불린다."""

    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("부정된 문맥 id 인데 remove_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=20),
            message="그건 빼지 말고 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]


async def test_wishlist_remove_spoken_name_wins_over_stale_context_product_id() -> None:
    """2차 리뷰 지적 4 — 발화의 명시적 상품명이 문맥 `cart.product_id` 보다 강한 신호다.

    재현: 찜 목록 10=이어폰/20=케이스, 발화 "이어폰 찜 빼줘", `cart.product_id=20`(LLM 오추출·
    stale). 문맥 id 를 먼저 보면 사용자가 말하지 않은 케이스(20)가 해제된다 — 이전 순서로
    재현하면 [20]이 나왔다. 이름 지목이 있으면 이어폰(10)이어야 한다."""
    remove_calls: list[int] = []

    async def remove_wishlist_fn(product_id, *, user_id):
        remove_calls.append(product_id)

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=20),
            message="이어폰 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert remove_calls == [10]
    assert _actions(events)[0]["type"] == "WISHLIST_REMOVED"


async def test_wishlist_remove_ambiguous_name_does_not_fall_back_to_context_id() -> None:
    """이름이 2건 이상 매칭되면 문맥 id 로 내려가지 않고 그 자리에서 되물음이어야 한다 —
    이름을 말했는데 모호하면 더 약한 신호로 골라잡지 않는다는 같은 원칙(지적 4 순서 변경의
    부작용이 없는지 고정)."""

    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("이름이 모호한데 remove_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=10),
            message="파우치 블루나 파우치 레드 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(
                _wishlist_item(10, "파우치 블루"), _wishlist_item(20, "파우치 레드")
            ),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]


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
    """이름이 **실제로 부분 문자열 매칭**으로 2건 걸리면 되물음(라운드 6 리뷰 — 매칭은
    `이름 in 발화` 방향이라 발화가 두 이름을 모두 포함해야 이 분기가 실제로 실행된다. 이전
    발화 "파우치 찜 빼줘"는 어느 이름도 포함하지 않아 이름 매칭이 0건이었다 — 그 무신호
    경로는 아래 `test_wishlist_remove_no_name_match_with_multiple_items_asks` 로 분리했다)."""

    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("모호한데 remove_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="파우치 블루랑 파우치 레드 찜 빼줘",
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


async def test_wishlist_remove_no_name_match_with_multiple_items_asks() -> None:
    """표지·이름 매칭이 전혀 없고(무신호) 목록이 2건 이상이면 되물음 — 이것도 유효한 계약이라
    지우지 않고 정직한 이름으로 남긴다(라운드 6 리뷰, 위 함수와 분리)."""

    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("무신호인데 remove_wishlist_fn 이 호출됐다")

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


# ─── 라운드 19(head `26f5596` 리뷰): 되물음 문구가 동작 표지를 유도한다 ───


def test_wishlist_unresolved_notice_lists_names_and_guides_action_marker() -> None:
    """`remove.py::_unresolved_notice` 와 같은 사안·같은 해법(라운드 19 패킷) — 찜한 상품명을
    모두 나열하면서(기존 단언 유지) 찜 해제 동작 표지를 포함한 예시로 다음 답을 유도해야 한다."""
    from app.agents.buyer.cart.wishlist import _wishlist_unresolved_notice

    text = _wishlist_unresolved_notice(
        [_wishlist_item(10, "파우치 블루"), _wishlist_item(20, "파우치 레드")]
    )
    assert "파우치 블루" in text
    assert "파우치 레드" in text
    assert "찜 빼줘" in text


def test_wishlist_unresolved_notice_marks_purchase_state() -> None:
    """찜 목록에도 조회·삭제와 **같은 라벨**을 붙인다(#310, AC③).

    찜은 "나중에 사려고 담아둔" 목록이라 시간이 지나며 상태가 바뀌기 쉽고, 그래서 못 사게
    됐다는 사실을 알려줄 값어치가 가장 큰 자리다. 안내 문장은 더하지 않는다 — 이 문구의 목적은
    "어느 걸 뺄지 묻기"라 문장을 더 얹으면 초점이 흐려진다."""
    from app.agents.buyer.cart.wishlist import _wishlist_unresolved_notice

    sold_out = _wishlist_item(20, "린넨 셔츠")
    sold_out.purchase_state = "SOLD_OUT"
    hidden = _wishlist_item(30, "가죽 지갑")
    hidden.purchase_state = "HIDDEN"

    text = _wishlist_unresolved_notice([_wishlist_item(10, "이어폰"), sold_out, hidden])
    assert "린넨 셔츠 (품절)" in text
    assert "가죽 지갑 (판매 종료)" in text
    assert "'이어폰 찜 빼줘'" in text  # 예시는 살 수 있는 항목 우선


async def test_wishlist_remove_no_name_match_asks_with_action_marker_guidance_via_stream() -> None:
    """`stream_wishlist_remove` 수준에서도 같은 사실 — 무신호 되물음 문구가 두 상품명과 찜 해제
    동작 표지 예시를 모두 담는다."""

    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("무신호인데 remove_wishlist_fn 이 호출됐다")

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
    assert "찜 빼줘" in token_text


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


# ─────────── stream_wishlist_remove — 라운드 10: 접두 부정이 단건 자동 규칙에도 적용된다 ───────────


def test_resolve_wishlist_remove_target_prefix_negated_single_item_asks() -> None:
    """직접 호출로 재현 확인(추측 아님) — 찜이 1건뿐일 때 "방금 찜한 건 안 빼도 되고 저건
    찜 빼줘"(라우팅은 뒤쪽 "찜 빼줘"가 그 자체로 안 부정돼 `wishlist_remove` 로 들어온다)가
    이름도 문맥 id 도 안 잡혀 "목록 1건 자동" 규칙까지 내려가면, 사용자가 "빼지 말라"고 명시한
    바로 그 항목이 삭제됐다. 이제 되물음이어야 한다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None),
        "방금 찜한 건 안 빼도 되고 저건 찜 빼줘",
        items,
        get_settings(),
    )
    assert result is None


async def test_wishlist_remove_prefix_negated_single_item_asks_via_stream() -> None:
    """`stream_wishlist_remove` 수준에서도 같은 사실 — remove_wishlist_fn 이 한 번도 안 불린다."""

    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("부정된 발화인데 remove_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="방금 찜한 건 안 빼도 되고 저건 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]


def test_resolve_wishlist_remove_target_single_item_still_works_without_negation() -> None:
    """부정 신호가 없으면 "목록 1건 자동" 규칙은 그대로 동작한다(회귀 방지)."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "찜 빼줘", items, get_settings()
    )
    assert result is not None
    assert result.product_id == 10


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


# ─── stream_wishlist_remove — 라운드 11·12: 이름 매칭(가장 강한 신호)에도 출현 단위 부정 가드 ───
#
# 라운드 11 은 패킷의 원문 "이어폰은 찜 빼지 말고 케이스 찜 빼줘"가 공용 부정 창(8자)에서
# "말고"를 못 담는다는 이유로 대체 문구로 바꿔 테스트했는데, 그건 절차 위반이었다 — 리뷰어가
# 실제로 재현해 신고한 문장을 통과하는 문장으로 갈아끼우면 테스트는 초록이 돼도 신고된 결함은
# 남는다(실제로 남아 있었다: 이어폰만 있을 때 여전히 이어폰이 해제 대상으로 확정됐다). 라운드
# 12 는 원인을 "창이 한 글자 모자란 것"이 아니라 "표지 목록이 '말' 활용형(-지 말고 등)을 못
# 잡는 것"으로 지목해 `utterance_negation_markers`에 "지 말"을 추가했다(창 크기는 그대로 8).
# 아래 원문 케이스가 그 수정의 회귀 테스트다. 라운드 11 의 대체 문구 테스트는 다른 회귀
# 조합(조사 생략형)으로 남긴다.


def test_resolve_wishlist_remove_target_negated_only_name_match_asks() -> None:
    """재현(라운드 11 패킷, 구조 동일) — 찜 목록에 이어폰만 있으면 그 부정된 출현 하나뿐이라
    되물음이어야 한다(고치기 전엔 이어폰이 해제됐다 — 사용자가 '빼지 말라'고 명시한 그 상품)."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰 찜 빼지 말고 케이스 찜 빼줘", items, get_settings()
    )
    assert result is None


def test_resolve_wishlist_remove_target_negated_name_still_resolves_other_name() -> None:
    """같은 발화라도 케이스가 실제로 찜 목록에 있으면 케이스만 해제 대상이어야 한다 — 문장
    전체 가드를 이름 매칭에 쓰면 케이스까지 함께 죽어 되물음이 되는데, 그건 틀렸다(라운드 11
    패킷 핵심)."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰 찜 빼지 말고 케이스 찜 빼줘", items, get_settings()
    )
    assert result is not None
    assert result.product_id == 20


async def test_wishlist_remove_negated_name_match_removes_only_unnegated_item() -> None:
    """`stream_wishlist_remove` 수준에서도 같은 사실 — remove_wishlist_fn 이 케이스(20)에만
    불린다."""
    remove_calls: list[int] = []

    async def remove_wishlist_fn(product_id, *, user_id):
        remove_calls.append(product_id)

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="이어폰 찜 빼지 말고 케이스 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert remove_calls == [20]
    action = _actions(events)[0]
    assert action["type"] == "WISHLIST_REMOVED"
    assert "케이스" in action["message"]


def test_resolve_wishlist_remove_target_single_item_negated_name_asks_not_auto_resolve() -> None:
    """단건 자동 규칙(3번)이 이름 매칭 실패의 뒷문이 되면 안 된다 — 이어폰 1건뿐인 찜 목록에서
    부정 표지가 있으면 단건 자동으로 새지 않고 되물음이어야 한다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰 찜 빼지 말고 케이스 찜 빼줘", items, get_settings()
    )
    assert result is None


# ─── 라운드 12 — 리뷰어 원문 그대로("이어폰은 찜 빼지 말고 케이스 찜 빼줘", 조사 "은" 포함) ───


def test_resolve_wishlist_remove_target_reviewer_original_phrasing_only_earphone_asks() -> None:
    """리뷰어가 실제로 재현해 신고한 문장(라운드 12 패킷) — 대체 문구가 아니라 이 문장 자체가
    통과해야 결함이 실제로 고쳐진 것이다. 고치기 전(라운드 11 상태)엔 이 문장에서 "이어폰"
    뒤 8자 창이 "은 찜 빼지 말"로 끊겨 "말고"를 못 담았고, "지 말"이 표지에 없어 그 출현이
    부정으로 안 잡혀 이어폰이 그대로 해제 대상으로 확정됐다(직접 재현 확인). "지 말" 표지
    추가로 같은 창(8자) 안에서 "지 말"이 잡혀 되물음이 된다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰은 찜 빼지 말고 케이스 찜 빼줘", items, get_settings()
    )
    assert result is None


def test_resolve_wishlist_remove_target_reviewer_original_phrasing_resolves_case() -> None:
    """같은 원문 문장에서 케이스가 실제로 찜 목록에 있으면 케이스만 해제 대상이어야 한다 —
    출현 단위 판정이라 이어폰의 부정된 출현만 제외되고 케이스는 살아남는다(문장 전체 가드였다면
    케이스까지 죽어 되물음이 됐을 것)."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰은 찜 빼지 말고 케이스 찜 빼줘", items, get_settings()
    )
    assert result is not None
    assert result.product_id == 20


async def test_wishlist_remove_reviewer_original_phrasing_removes_only_case_via_stream() -> None:
    """`stream_wishlist_remove` 수준에서도 리뷰어 원문으로 같은 사실 — remove_wishlist_fn 이
    케이스(20)에만 불린다."""
    remove_calls: list[int] = []

    async def remove_wishlist_fn(product_id, *, user_id):
        remove_calls.append(product_id)

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="이어폰은 찜 빼지 말고 케이스 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert remove_calls == [20]
    action = _actions(events)[0]
    assert action["type"] == "WISHLIST_REMOVED"
    assert "케이스" in action["message"]


def test_resolve_wishlist_remove_target_ji_mal_marker_does_not_swallow_normal_remove() -> None:
    """ "지 말" 표지 추가가 정상 발화를 삼키지 않는지 확인 — "말"이 들어간 무관한 단어("말투"
    등)가 아니라 실제 부정 활용형만 잡아야 한다. 부정 신호 없는 평범한 단건 해제는 그대로
    동작해야 한다(회귀 방지)."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰 찜 빼줘", items, get_settings()
    )
    assert result is not None
    assert result.product_id == 10


# ─── 라운드 15(head `0b33e06` 리뷰 B): 이름 매칭 경계(조사 허용) — 찜 동형 ───


def test_resolve_wishlist_remove_target_name_inside_word_does_not_match() -> None:
    """재현(라운드 15 패킷, 찜 동형) — 찜 목록에 "이어폰"만 있을 때 "이어폰케이스 찜 빼줘"는
    다른 낱말 안에 파묻힌 "이어폰"을 매칭해선 안 된다. 단건 자동(3번) 뒷문도 막혀야 하므로
    되물음이어야 한다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰케이스 찜 빼줘", items, get_settings()
    )
    assert result is None


def test_resolve_wishlist_remove_target_name_followed_by_particle_still_matches() -> None:
    """회귀 — 목적격 조사 "를"이 이름 바로 뒤에 붙는 정상 발화는 계속 매칭돼야 한다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(20, "세제")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "그 세제를 찜 빼줘", items, get_settings()
    )
    assert result is not None
    assert result.product_id == 20


def test_resolve_wishlist_remove_target_boundary_violation_blocks_single_auto() -> None:
    """단건 자동(3번) 뒷문 방지 — 이름을 대려는 시도(경계 위반)가 있으면 목록이 1건뿐이고
    다른 표지가 없어도 단건 자동으로 새지 않는다. 대조로 이름이 전혀 없으면(무신호) 그대로
    동작한다(회귀 없음)."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰케이스 찜 빼줘", items, get_settings()
    )
    assert result is None
    result_no_name = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "찜 빼줘", items, get_settings()
    )
    assert result_no_name is not None
    assert result_no_name.product_id == 10


# ─── 라운드 17(head `6ab47c9` 리뷰): 이름 뒤 "조사+filler+표지" 만 유효 매칭으로 인정 — 찜 동형 ───


def test_resolve_wishlist_remove_target_spaced_name_followed_by_other_word_asks() -> None:
    """고쳐지는 것(라운드 17 패킷 핵심, 찜 동형) — "이어폰 케이스 찜 빼줘"(찜 목록에 이어폰만
    있음)는 사용자가 요청하지 않은 "이어폰"을 확인 없이 해제하는 파괴적 동작이었다(재현).
    "케이스"가 조사·filler·표지 어느 것도 아니라 이제 되물음이어야 한다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰 케이스 찜 빼줘", items, get_settings()
    )
    assert result is None


def test_resolve_wishlist_remove_target_name_followed_by_filler_then_marker_still_matches() -> None:
    """깨지면 안 되는 것 — "이어폰 좀 찜 빼줘"는 "좀"이 filler 라 소비되고 뒤에 표지("찜 빼줘")
    가 오므로 여전히 매칭돼야 한다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰 좀 찜 빼줘", items, get_settings()
    )
    assert result is not None
    assert result.product_id == 10


def test_resolve_wishlist_remove_target_particle_then_marker_still_matches() -> None:
    """회귀 — 목적격 조사가 이름 바로 뒤에 붙고 곧장 표지가 오는 정상 발화는 계속 매칭돼야
    한다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(20, "세제")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "그 세제를 찜 빼줘", items, get_settings()
    )
    assert result is not None
    assert result.product_id == 20


def test_resolve_wishlist_remove_target_ambiguous_listing_with_particle_still_asks() -> None:
    """깨지면 안 되는 것(핵심 회귀, 찜 동형) — "파우치 블루랑 파우치 레드 찜 빼줘"는 다른
    항목 이름 허용이 없으면 "파우치 블루"만 무효로 걸러지고 모호 판정이 깨진다. 둘 다 유효해야
    모호(되물음)로 남는다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "파우치 블루"), _wishlist_item(20, "파우치 레드")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "파우치 블루랑 파우치 레드 찜 빼줘", items, get_settings()
    )
    assert result is None


def test_resolve_wishlist_remove_target_listed_names_with_batchim_ask_like_without_batchim() -> (
    None
):
    """찜 동형(라운드 20 패킷, `remove.py` 와 같은 원인) — `_consume_prefix` 가 첫 매칭("이")만
    소비하면 받침 있는 "이어폰" 뒤 "이랑"에서 "랑"이 남아 오른쪽 경계 검사가 깨지고, 지목된
    "이어폰"이 조용히 빠진다. 최장 일치로 고치면 둘 다 매칭돼 모호(되물음) — 받침 없는 "파우치
    랑 세제 찜 빼줘"와 같은 결과여야 한다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰이랑 케이스 찜 빼줘", items, get_settings()
    )
    assert result is None


def test_resolve_wishlist_remove_target_negated_name_with_valid_trailing_still_matches_other() -> (
    None
):
    """회귀(라운드 11) — "이어폰은 찜 빼지 말고 케이스 찜 빼줘"에서 "케이스"는 새 오른쪽
    경계 검사를 통과하고, "이어폰"은 부정으로 무효화돼 케이스만 남는다."""
    from app.agents.buyer.cart.wishlist import _resolve_wishlist_remove_target

    items = [_wishlist_item(10, "이어폰"), _wishlist_item(20, "케이스")]
    result = _resolve_wishlist_remove_target(
        CartIntent(product_id=None), "이어폰은 찜 빼지 말고 케이스 찜 빼줘", items, get_settings()
    )
    assert result is not None
    assert result.product_id == 20


async def test_wishlist_remove_spaced_name_followed_by_other_word_asks_via_stream() -> None:
    """`stream_wishlist_remove` 수준에서도 같은 사실 — remove_wishlist_fn 이 한 번도 안 불린다."""

    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("파괴적 오해제인데 remove_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_remove(
            identity=_member(),
            cart=CartIntent(product_id=None),
            message="이어폰 케이스 찜 빼줘",
            settings=get_settings(),
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]


# ─────────── stream_cart_add 배선 ───────────
# [라운드 23] 찜 흐름의 온/오프를 가리던 설정 필드를 삭제했다(사용자 지시) — 판정이 나오면 항상 위임한다.
# "플래그 off" 류 테스트는 삭제하고, 아래는 전부 "항상 위임" 동작을 검증한다.


async def test_stream_cart_add_delegates_to_wishlist_add() -> None:
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


async def test_stream_cart_add_delegates_to_wishlist_remove() -> None:
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


# ─── stream_cart_add 배선 — 라운드 13(head `14aa26b` 리뷰): 위임 시 stale pending 정리 ───


def _pending_options() -> list[CartOption]:
    return [CartOption(option_id=1, name="레드", extra_price=0)]


async def test_stream_cart_add_delegates_to_wishlist_add_clears_stale_pending() -> None:
    """옵션 되물음(pending) 진행 중에 찜 추가 판정 턴이 끼면 `stream_wishlist_add` 로 위임하기
    전에 stale pending 을 지워야 한다(`graph.py` 665~668행과 같은 취지 — 담기가 아닌 의도로
    전환되면 옛 상품 되물음에 갇히지 않게)."""
    store = CartStateStore()
    thread_key = "m:t-wishlist-add-clears-pending"
    await store.set_pending(
        thread_key, PendingAdd(product_id=99, quantity=1, options=_pending_options())
    )

    async def add_fn(req):
        raise AssertionError("찜 판정인데 add_fn 이 호출됐다")

    async def add_wishlist_fn(request):
        return WishlistAddResult(success=True, product_id=request.product_id)

    await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            message="이거 찜해줘",
            add_fn=add_fn,
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    assert await store.get_pending(thread_key) is None


async def test_stream_cart_add_delegates_to_wishlist_remove_clears_stale_pending() -> None:
    """찜 해제 위임도 동일 — pending 이 지워진다."""
    store = CartStateStore()
    thread_key = "m:t-wishlist-remove-clears-pending"
    await store.set_pending(
        thread_key, PendingAdd(product_id=99, quantity=1, options=_pending_options())
    )

    async def add_fn(req):
        raise AssertionError("찜 판정인데 add_fn 이 호출됐다")

    async def remove_wishlist_fn(product_id, *, user_id):
        return None

    await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=10, quantity=1),
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            message="찜 빼줘",
            add_fn=add_fn,
            get_wishlist_fn=_wishlist(_wishlist_item(10, "이어폰")),
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert await store.get_pending(thread_key) is None


# ─────────── stream_wishlist_view (이슈 #386, I-28 §4.16) ───────────


async def test_wishlist_view_guest_makes_zero_internal_calls() -> None:
    """게스트 찜 목록 조회는 계약에 없다(회원 전용 I-26/I-27/I-28, M-4) — internal 호출 전에
    로그인 안내로 끝낸다(api-spec §3.1 게스트 문단). `action` 은 내지 않는다."""

    async def get_wishlist_fn(user_id):
        raise AssertionError("게스트인데 get_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_wishlist_view(identity=_guest(), get_wishlist_fn=get_wishlist_fn)
    )
    assert _types(events) == ["token", "done"]
    assert not _actions(events)
    assert events[0]["data"]["text"] == "찜 목록 조회에는 로그인이 필요해요."


async def test_wishlist_view_anon_makes_zero_internal_calls() -> None:
    async def get_wishlist_fn(user_id):
        raise AssertionError("익명인데 get_wishlist_fn 이 호출됐다")

    events = await _collect(stream_wishlist_view(identity=_anon(), get_wishlist_fn=get_wishlist_fn))
    assert _types(events) == ["token", "done"]
    assert not _actions(events)


async def test_wishlist_view_empty_is_guidance_not_error() -> None:
    """0건은 오류가 아니다 — 기존 찜 해제 흐름과 같은 문구를 재사용한다."""
    events = await _collect(stream_wishlist_view(identity=_member(), get_wishlist_fn=_wishlist()))
    assert _types(events) == ["token", "done"]
    assert events[0]["data"]["text"] == "찜한 상품이 없어요."


async def test_wishlist_view_lists_names_with_purchase_state_labels() -> None:
    """목록은 `token` 텍스트로만 답한다 — 상품 카드도 `action` 도 없다(경로 B, api-spec §4.16).

    항목별 구매 가능 상태 라벨은 `state_suffix` 를 재사용한다(#310, 장바구니 조회와 같은 규칙).
    다만 문단 끝 **행동 안내 문장**(`state_advice_lines`)은 붙이지 않는다 — 그 문구는 장바구니
    어휘("다시 담을 수 있어요")이고, 예시로 드는 `'{품목} 빼줘'` 는 `intent_guard` 가 의도적으로
    `cart_add` 로 떨어뜨리는 발화라(알려진 거짓음성) 답할 수 없는 안내가 된다.
    """
    items = (
        WishlistItem(product_id=10, name="무선 이어폰", purchase_state="AVAILABLE"),
        WishlistItem(product_id=20, name="가죽 크로스백", purchase_state="SOLD_OUT"),
        WishlistItem(product_id=30, name="캠핑 랜턴", purchase_state="HIDDEN"),
    )
    events = await _collect(
        stream_wishlist_view(identity=_member(), get_wishlist_fn=_wishlist(*items))
    )
    assert _types(events) == ["token", "done"]
    assert not _actions(events)
    text = events[0]["data"]["text"]
    assert text == "찜한 상품이에요:\n무선 이어폰\n가죽 크로스백 (품절)\n캠핑 랜턴 (판매 종료)"
    assert "다시 담을 수 있어요" not in text
    assert "빼는 걸 추천" not in text


async def test_wishlist_view_sanitizes_seller_supplied_names() -> None:
    """상품명은 판매자 입력이라 반드시 `_strip_unsafe` 를 거친다(`remove.py` 와 같은 규약)."""
    items = (WishlistItem(product_id=10, name="이어\n폰​", purchase_state="AVAILABLE"),)
    events = await _collect(
        stream_wishlist_view(identity=_member(), get_wishlist_fn=_wishlist(*items))
    )
    text = events[0]["data"]["text"]
    assert "이어\n폰" not in text
    assert "​" not in text


async def test_wishlist_view_spring_unavailable_degrades_to_token_not_action() -> None:
    """조회 실패는 `token` 안내 + 정상 `done` 이다 — `action` 을 내지 않는다.

    `ActionData.type` 유니온(api-spec §3.1)에 조회 실패 어휘가 아예 없다. 형제
    `stream_wishlist_remove` 가 `action(WISHLIST_REMOVE_FAILED)` 을 내는 것은 그쪽이 **변경 턴의
    선행 조회**이기 때문이고, 이 턴은 상태를 바꾸지 않는다. 개별 처리하지 않으면 상위 스트림의
    범용 catch-all 이 `error(INTERNAL)` 로 내보낸다(#368).
    """

    async def get_wishlist_fn(user_id):
        raise SpringUnavailableError("wishlist down")

    events = await _collect(
        stream_wishlist_view(identity=_member(), get_wishlist_fn=get_wishlist_fn)
    )
    assert _types(events) == ["token", "done"]
    assert not _actions(events)
    assert events[0]["data"]["text"] == "찜 목록을 불러오지 못했어요. 잠시 후 다시 시도해 주세요."
