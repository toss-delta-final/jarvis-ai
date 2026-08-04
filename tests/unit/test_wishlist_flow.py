"""찜 추가·해제 흐름 (이슈 #117, 패킷 §5.4) — `stream_wishlist_add`/`stream_wishlist_remove`
대상 해소·오류 매핑·경로 B 가드·배선.

Spring 이 아직 I-26/I-27/I-28 을 구현하지 않아 실호출 통합 테스트는 하지 않는다(상대가 없다).
주입 fn 으로 단위 테스트한다(`test_cart_remove.py` 와 같은 패턴).
"""

from __future__ import annotations

import json

import pytest

from app.agents.buyer.cart.graph import stream_cart_add
from app.agents.buyer.cart.state import CartStateStore, PendingAdd
from app.agents.buyer.cart.wishlist import stream_wishlist_add, stream_wishlist_remove
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
    return WishlistItem(product_id=product_id, name=name, purchasable=True)


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


# ─────────── stream_cart_add 배선 ───────────


async def test_stream_cart_add_delegates_to_wishlist_add_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "wishlist_enabled", True)
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


async def test_stream_cart_add_delegates_to_wishlist_remove_when_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "wishlist_enabled", True)
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


async def test_stream_cart_add_wishlist_degrade_notice_unchanged_when_flag_off() -> None:
    """wishlist_enabled=False(기본) 면 여전히 라운드 2 의 degrade 안내(찜 추가 문구)가 그대로
    나간다(2차 리뷰 N-3 이후에도 찜 추가는 담기 대안 제안 문구를 유지한다)."""
    from app.agents.buyer.cart.graph import _WISHLIST_ADD_DISABLED_NOTICE

    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("degrade 인데 add_fn 이 호출됐다")

    async def add_wishlist_fn(request):
        raise AssertionError("플래그 off 인데 add_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-add-wishlist-degrade",
            settings=get_settings(),
            message="이거 찜해줘",
            add_fn=add_fn,
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    assert _types(events) == ["token", "done"]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token_text == _WISHLIST_ADD_DISABLED_NOTICE
    assert not _actions(events)


# ─── stream_cart_add 배선 — 라운드 13(head `14aa26b` 리뷰): 위임 시 stale pending 정리 ───


def _pending_options() -> list[CartOption]:
    return [CartOption(option_id=1, name="레드", extra_price=0)]


async def test_stream_cart_add_delegates_to_wishlist_add_clears_stale_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """옵션 되물음(pending) 진행 중에 찜 추가 판정 턴이 끼면 `stream_wishlist_add` 로 위임하기
    전에 stale pending 을 지워야 한다(`graph.py` 665~668행과 같은 취지 — 담기가 아닌 의도로
    전환되면 옛 상품 되물음에 갇히지 않게)."""
    monkeypatch.setattr(get_settings(), "wishlist_enabled", True)
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


async def test_stream_cart_add_delegates_to_wishlist_remove_clears_stale_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """찜 해제 위임도 동일 — pending 이 지워진다."""
    monkeypatch.setattr(get_settings(), "wishlist_enabled", True)
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


async def test_stream_cart_add_wishlist_disabled_notice_keeps_pending() -> None:
    """플래그 off(라운드 2 결정, 회귀 고정) — 안내만 하고 빠지는 턴은 아무 동작도 수행하지
    않으므로 진행 중이던 담기 pending 을 버릴 이유가 없다. 위 "위임해서 실제로 다른 동작을
    수행하는" 세 테스트와 대비되는 경우다."""
    store = CartStateStore()
    thread_key = "m:t-wishlist-flag-off-keeps-pending"
    await store.set_pending(
        thread_key, PendingAdd(product_id=99, quantity=1, options=_pending_options())
    )

    async def add_fn(req):
        raise AssertionError("degrade 인데 add_fn 이 호출됐다")

    async def add_wishlist_fn(request):
        raise AssertionError("플래그 off 인데 add_wishlist_fn 이 호출됐다")

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
    assert await store.get_pending(thread_key) is not None
