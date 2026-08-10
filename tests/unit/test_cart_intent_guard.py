"""찜 오담기 방어 (이슈 #116·#117, 패킷 §4) — `classify_cart_utterance` 단위 + `stream_cart_add` 배선.

전부 **기본 설정**(`get_settings()`, override 없음)으로 돈다 — 배포되는 기본 조합(표지 목록)이
실제로 이 테이블을 통과하는지가 이 테스트들의 존재 이유다. **[라운드 23]** 삭제·찜 흐름의
온/오프를 가리던 설정 필드는 삭제됐다 — 판정이 나오면 항상 해당 흐름으로 위임한다.
"""

from __future__ import annotations

import pytest

from app.agents.buyer.cart.intent_guard import classify_cart_utterance
from app.agents.buyer.cart.graph import stream_cart_add
from app.agents.buyer.cart.state import CartStateStore
from app.agents.buyer.recommendation.state import CartIntent
from app.core.auth import Identity
from app.core.config import get_settings
from app.schemas.spring import (
    AddToCartResult,
    CartView,
    CartViewItem,
    WishlistAddResult,
)


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


# ─────────── classify_cart_utterance — 2차 리뷰(Codex) 지적 1·3·8: 부정·유보 표지 ───────────


@pytest.mark.parametrize(
    "message,expected",
    [
        # 지적 1 — 부정된 담기 표지는 무효화되고, 찜 표지가 살아 있으면 그쪽으로 간다.
        ("이건 찜해줘, 장바구니에 넣지는 마", "wishlist_add"),
        # 지적 1 — "장바구니" 억제도 부정 문맥을 본다: 위와 같은 문장에서 "장바구니" 자체가
        # 부정된 절 안에 있으므로 찜 추가 억제 근거로 쓰지 않는다(별도 케이스로도 고정).
        ("장바구니에 넣지는 마, 찜해줘", "wishlist_add"),
        # 지적 1 — 찜 추가·해제 표지도 부정되면 기본값(cart_add)으로 되돌아간다.
        ("찜 목록에 추가하지 마", "cart_add"),
        ("찜 취소하지 마", "cart_add"),
        # 지적 3 — "야 할"류 유보 표지는 삭제 표지를 무효화한다(질문이지 지시가 아니다).
        ("장바구니에서 빼줘야 할까?", "cart_add"),
        # 부정 표지가 없는 정상 발화는 지금까지와 동일해야 한다(회귀 방지).
        ("장바구니에 넣어줘", "cart_add"),
        ("찜해줘", "wishlist_add"),
        ("찜 취소해줘", "wishlist_remove"),
        ("장바구니에서 빼줘", "cart_remove"),
    ],
)
def test_classify_cart_utterance_negation_layer(message: str, expected: str) -> None:
    assert classify_cart_utterance(message, get_settings()) == expected


def test_classify_wishlist_remove_suppression_does_not_apply_to_remove_markers() -> None:
    """지적 8 — "장바구니" 억제는 wishlist_add 판정에만 걸린다. wishlist_remove_markers 는 전부
    "찜"이 붙은 명시적 동작 구라 "장바구니"가 같이 나와도 혼동 여지가 없다."""
    assert (
        classify_cart_utterance("찜 취소해줘, 장바구니는 그대로 두고", get_settings())
        == "wishlist_remove"
    )


def test_classify_negation_only_suppresses_the_negated_occurrence() -> None:
    """같은 표지가 여러 번 나오면 부정되지 않은 출현이 하나라도 있어야 매칭이다 — 첫 출현만
    보고 과소 매칭하지 않는다."""
    assert (
        classify_cart_utterance("장바구니에 넣지는 마, 그래도 장바구니에 넣어줘", get_settings())
        == "cart_add"
    )


# ─────────── classify_cart_utterance — 라운드 7: 명시적 찜 동작 표지가 지시 수식어 양보보다 우선 ───────────


@pytest.mark.parametrize(
    "message,expected",
    [
        # 명시적 찜 동작 표지(1·2번)가 지시 수식어("찜한")보다 강한 신호 — 이제 새지 않는다.
        ("찜한 거 찜 취소해줘", "wishlist_remove"),
        # 0-a(cart_add_markers)는 순서 변경과 무관하게 여전히 맨 앞이다(회귀 방지).
        ("찜해둔 이어폰 담아줘", "cart_add"),
        ("찜한 거 장바구니에 담아줘", "cart_add"),
        # [#440] 예전엔 "명시적 찜 동작 표지가 없으면(순수 지시 수식어만) 알려진 거짓음성"으로
        # cart_add 에 떨어졌다 — 이제 사다리 1-b(인접 결합)가 이 발화를 직접 잡는다. 전체 표는
        # tests/unit/test_wishlist_remove_resolution.py §4-A 참조.
        ("찜한 거 빼줘", "wishlist_remove"),
        # 부정 검사는 순서를 옮긴 뒤에도 그대로 적용된다.
        ("찜 취소하지 마", "cart_add"),
    ],
)
def test_classify_explicit_wishlist_action_marker_wins_over_reference_marker(
    message: str, expected: str
) -> None:
    assert classify_cart_utterance(message, get_settings()) == expected


# ─────────── classify_cart_utterance — 2차 리뷰(Claude) N-1: 담기 표지의 과거 참조형 ───────────


@pytest.mark.parametrize(
    "message,expected",
    [
        # 재현 — "담아"가 과거 참조형("담아뒀던"·"담아둔")에 부분 문자열로 걸려 뒤의 삭제·찜
        # 해제 판정까지 못 내려갔다.
        ("담아뒀던 거 다 빼줘", "cart_remove"),
        ("담아둔 이어폰 찜 취소해줘", "wishlist_remove"),
        # "장바구니에 넣" 표지도 같은 방식으로 걸려야 한다("어"가 하나 끼어도 창 검사로 잡힌다).
        ("장바구니에 넣어뒀던 거 빼줘", "cart_remove"),
        # ⚠️ 회귀 금지 — 참조 꼬리가 없는 정상 담기 요청은 전부 cart_add 그대로.
        ("장바구니에 담아줘", "cart_add"),
        ("찜한 거 장바구니에 담아줘", "cart_add"),
        ("찜해둔 이어폰 담아줘", "cart_add"),
        ("하나 빼고 담아줘", "cart_add"),
    ],
)
def test_classify_cart_add_marker_excludes_past_reference_tail(message: str, expected: str) -> None:
    assert classify_cart_utterance(message, get_settings()) == expected


# ─────────── classify_cart_utterance — 라운드 18 F2: 찜 추가 표지의 과거 참조형 ───────────


@pytest.mark.parametrize(
    "message,expected",
    [
        # 재현 — "위시리스트에 넣어"가 과거 참조형("넣어놓은")에 부분 문자열로 걸려 질문/과거
        # 참조가 찜 추가로 오분류됐다. 라운드 8 의 `_matches_cart_add_marker` 창 기계를
        # `wishlist_add_markers` 에도 재사용해(`cart_add_reference_markers` 그대로) 배제한다.
        ("위시리스트에 넣어놓은 거 있어요?", "cart_add"),
        # 대조군 — 담기 쪽은 이미 고쳐진 같은 클래스 문제(참조용, 회귀 방지).
        ("담아뒀던 거 다 빼줘", "cart_remove"),
        # ⚠️ 회귀 금지 — 참조 꼬리가 없는 정상 찜 추가 요청은 전부 wishlist_add 그대로.
        ("위시리스트에 넣어줘", "wishlist_add"),
        ("찜해줘", "wishlist_add"),
        ("찜 목록에 추가해줘", "wishlist_add"),
    ],
)
def test_classify_wishlist_add_marker_excludes_past_reference_tail(
    message: str, expected: str
) -> None:
    assert classify_cart_utterance(message, get_settings()) == expected


# ─────────── classify_cart_utterance — 라운드 9: 접두 부정("안"/"못") ───────────


@pytest.mark.parametrize(
    "message,expected",
    [
        # 재현 — 부정 검사가 어미형(뒤쪽, "-지 마")만 보고 접두형(안·못, 앞쪽)을 놓쳐, 삭제·찜
        # 하지 말라는 발화가 그대로 실행됐다.
        ("안 빼줘도 돼", "cart_add"),
        ("안 지워줘도 돼", "cart_add"),
        ("안 찜해줘도 돼", "cart_add"),
        ("못 빼줘도 괜찮아", "cart_add"),
        # 이 검사는 모든 표지 계열(담기·삭제·찜 추가·찜 해제)에 동일하게 적용된다 — 담기 표지만
        # 예외로 남으면 이 발화가 다시 어긋난다.
        ("안 담아도 되고 그냥 빼줘", "cart_remove"),
    ],
)
def test_classify_prefix_negation_suppresses_marker(message: str, expected: str) -> None:
    assert classify_cart_utterance(message, get_settings()) == expected


@pytest.mark.parametrize(
    "message,expected",
    [
        # 거짓 억제 방지(핵심) — "안"은 흔한 조각이라 부분 문자열로 보면 정상 삭제 요청까지
        # 죽는다. 어절 경계 판정이라 "안경"의 "안"·발화 앞쪽의 "안"은 표지를 무효화하지 않는다.
        ("안경 빼줘", "cart_remove"),
        ("가방 안에 있는 거 빼줘", "cart_remove"),
    ],
)
def test_classify_prefix_negation_does_not_falsely_suppress(message: str, expected: str) -> None:
    assert classify_cart_utterance(message, get_settings()) == expected


def test_has_prefix_negation_word_boundary_rules() -> None:
    """`negation.has_prefix_negation` 직접 호출로 어절 경계 규칙을 고정한다 — "안 빼줘"·
    "안빼줘"(공백 0~1개)는 잡고, "안경"의 "안"(뒤에 다른 글자가 붙어 토큰이 아님)과 표지에서
    먼 "안"은 안 잡는다.

    [라운드 10] 이 함수는 원래 `intent_guard.py` 안에 있었는데 부정 판정을 공용 모듈
    `app/agents/buyer/cart/negation.py` 로 뽑으면서 그쪽으로 옮겼다 — import 경로만 바뀌었고
    동작은 그대로다(이 테스트가 그 사실을 고정한다)."""
    from app.agents.buyer.cart.negation import has_prefix_negation

    prefix_markers = ["안", "못"]
    # "안 빼줘" — marker "빼줘" starts at index 1 (공백 1개 뒤).
    assert has_prefix_negation("안 빼줘", 1, prefix_markers) is True
    # "안빼줘" — marker "빼줘" starts at index 1 (공백 없음).
    assert has_prefix_negation("안빼줘", 1, prefix_markers) is True
    # "안경 빼줘" — marker "빼줘" starts at index 3; 직전 토큰은 "안경"이지 "안" 단독이 아니다.
    assert has_prefix_negation("안경 빼줘", 3, prefix_markers) is False
    # "가방 안에 있는 거 빼줘" — marker 직전 토큰은 "거"; "안"은 문장 앞쪽에 멀리 있다.
    message = "가방 안에 있는 거 빼줘"
    assert has_prefix_negation(message, message.index("빼줘"), prefix_markers) is False


# ─────────── stream_cart_add 배선 — 찜 오담기 방어 ───────────


async def test_stream_cart_add_wishlist_add_intent_delegates_to_wishlist_flow() -> None:
    """찜 추가 판정 → `stream_wishlist_add` 로 위임돼 실제로 찜이 담긴다(라운드 23 — 이전엔
    온/오프 설정 필드가 꺼져 있으면 "찜 기능은 아직 준비 중이에요" 로 degrade 했지만, 그 필드가
    삭제돼 이제 항상 위임한다). `add_fn`(담기)은 한 번도 안 불린다."""

    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("wishlist 판정인데 add_fn(담기) 이 호출됐다")

    async def add_wishlist_fn(req):
        return WishlistAddResult(success=True, product_id=req.product_id)

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
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "WISHLIST_ADDED"


async def test_stream_cart_add_wishlist_add_delegation_forwards_has_last_reco() -> None:
    """[#435 W4] `stream_cart_add` 의 2선 방어(찜 위임)도 `has_last_reco` 를 전달한다 — 빠뜨리면
    이 경로만 옛 문구로 새어 절반만 고친 것이다(패킷 §4 W4)."""
    store = CartStateStore()

    async def add_wishlist_fn(req):
        raise AssertionError("productId 미해소인데 add_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=None, quantity=1),
            cart_store=store,
            thread_key="m:t-wishlist-add-reco",
            settings=get_settings(),
            message="이거 찜해줘",
            add_wishlist_fn=add_wishlist_fn,
            has_last_reco=True,
        )
    )
    text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert (
        text == "어떤 상품을 찜할까요? 추천해 드린 상품 중에서 이름을 말씀해 주시면 찜해 드릴게요."
    )


async def test_stream_cart_add_wishlist_remove_intent_delegates_to_wishlist_flow() -> None:
    """찜 해제 판정 → `stream_wishlist_remove` 로 위임된다(라운드 23, 위 함수와 같은 사실).
    `add_fn`(담기)은 한 번도 안 불린다.

    **[#440 라운드 9 리뷰 F24]** 이 위임은 이제 `has_wishlist_remove_evidence`(발화 **전체**
    앵커, F22)를 요구한다 — "이어폰 찜 빼줘"는 상품명("이어폰")이 `wishlist_remove_prefix_
    words` 같은 닫힌 어휘가 아니라서 그 근거를 통과하지 못한다(규칙 1 전용 라우팅급 게이트
    `is_wishlist_remove_command_context` 는 통과하지만, 그건 `_resolve_wishlist_remove_
    target` **안에서** 이름이 매칭됐을 때만 쓰는 게이트지 이 위임 자체의 게이트가 아니다).
    위임하지 않으면 결정론 규칙이 LLM 의 `cart_add` 판단을 확신 없이 덮어쓰지 않는다는 뜻이라
    담기 흐름의 안전한 기본 동작(상품을 특정 못 했다는 재질문)으로 남는다 — 파괴적이지 않다."""

    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("담기 대상이 없어 add_fn(담기) 자체가 호출되면 안 된다")

    async def get_wishlist_fn(user_id):
        raise AssertionError("근거가 없어 위임되지 않아야 하는데 get_wishlist_fn 이 호출됐다")

    async def remove_wishlist_fn(product_id, *, user_id):
        raise AssertionError("근거가 없어 위임되지 않아야 하는데 remove_wishlist_fn 이 호출됐다")

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=None),
            cart_store=store,
            thread_key="m:t-wishlist-remove",
            settings=get_settings(),
            message="이어폰 찜 빼줘",
            add_fn=add_fn,
            get_cart_fn=_empty_cart,
            get_wishlist_fn=get_wishlist_fn,
            remove_wishlist_fn=remove_wishlist_fn,
        )
    )
    assert "action" not in [e["type"] for e in events]
    assert [e["type"] for e in events] == ["token", "done"]


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


async def test_stream_cart_add_remove_intent_delegates_to_remove_flow() -> None:
    """cart_remove 판정 → `stream_cart_remove` 로 위임된다(라운드 23 — 이전엔 온/오프 설정
    필드가 꺼져 있으면 이 판정을 무시하고 오늘 동작(담기)이 그대로 돌았지만, 그 필드가 삭제돼
    이제 항상 위임한다). `add_fn`(담기)은 한 번도 안 불린다."""
    store = CartStateStore()

    async def add_fn(req):
        raise AssertionError("cart_remove 판정인데 add_fn(담기) 이 호출됐다")

    deleted: list[int] = []

    async def delete_fn(cart_item_id, *, user_id=None, guest_id=None):
        deleted.append(cart_item_id)

    async def get_cart_fn(*, user_id=None, guest_id=None):
        return CartView(items=[CartViewItem(cart_item_id=55, product_id=1, product_name="이어폰")])

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key="m:t-remove-delegates",
            settings=get_settings(),
            message="이어폰 빼줘",
            add_fn=add_fn,
            get_cart_fn=get_cart_fn,
            delete_fn=delete_fn,
        )
    )
    assert deleted == [55]
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_REMOVED"


async def test_stream_cart_add_wishlist_intent_clears_pending() -> None:
    """옵션 되물음 진행 중(pending) 이 사용자가 "찜해줘"를 말하면 pending 이 정리된다(라운드 23
    — 이전엔 온/오프 설정 필드가 꺼져 있어 이 턴이 담기 흐름에 개입하지 않고 그냥 빠졌으므로
    pending 을 건드리지 않았지만, 이제 실제로 찜 흐름으로 위임하므로 다음 턴이 옛 상품의 옵션
    답변으로 오해석되지 않게 정리해야 한다)."""
    from app.agents.buyer.cart.state import PendingAdd
    from app.schemas.spring import CartOption

    store = CartStateStore()
    thread_key = "m:t-wishlist-clears-pending"
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
        raise AssertionError("wishlist 판정인데 add_fn(담기) 이 호출됐다")

    async def add_wishlist_fn(req):
        return WishlistAddResult(success=True, product_id=req.product_id)

    await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(product_id=1, quantity=1),
            cart_store=store,
            thread_key=thread_key,
            settings=get_settings(),
            message="이거 찜해줘",
            add_fn=add_fn,
            get_cart_fn=_empty_cart,
            add_wishlist_fn=add_wishlist_fn,
        )
    )
    assert await store.get_pending(thread_key) is None


# ─────────── classify_cart_utterance — #285 I-25 §4.13 수량 변경(사다리 4-a) ───────────


@pytest.mark.parametrize(
    "message,expected",
    [
        # 함정 2 — 치환(cart_quantity) vs 합산(cart_add). 합산 표지가 있으면 cart_quantity 로
        # 가지 않는다(대개 "담아"를 포함해 0-a 에서 이미 걸리지만, 표지 자체를 직접 확인한다).
        ("3개로 바꿔줘", "cart_quantity"),
        ("수량 2개로 변경해줘", "cart_quantity"),
        ("수량 바꿔줘", "cart_quantity"),
        ("하나 더 담아줘", "cart_add"),
        ("추가로 담아줘", "cart_add"),
        # 0-a(담기)가 여전히 4-a 보다 강하다 — "담아"가 걸리면 그 자리에서 cart_add 로 확정되고
        # cart_quantity 후보로도 내려가지 않는다(intent_guard.py 사다리 4-a 문단 참조).
        ("3개로 바꿔서 담아줘", "cart_add"),
        # 회귀 없음 — 기존 삭제·찜 판정은 그대로.
        ("빼줘", "cart_remove"),
        ("찜해줘", "wishlist_add"),
    ],
)
def test_classify_cart_utterance_quantity_ladder(message: str, expected: str) -> None:
    assert classify_cart_utterance(message, get_settings()) == expected


def test_classify_cart_utterance_quantity_negation_suppresses_marker() -> None:
    """[함정 2 회귀 — 부정] 표지가 실제로 매칭된 뒤에도(정적 목록 "수량 바꿔") 짧은 창 안의
    부정 표지("하지 마")가 뒤따르면 무효화된다 — `"수량 바꾸지 마"` 는 애초에 정적 표지("수량
    바꿔")와 문자열이 달라 무신호로 빠지므로, 실제로 표지가 매칭된 경우로 검증한다(변이 시험:
    `_matches_unnegated` 를 단순 `in` 검사로 바꾸면 이 테스트가 깨진다)."""
    settings = get_settings()
    assert classify_cart_utterance("수량 바꿔달라고 하지 마", settings) == "cart_add"
    # 대조 — 같은 표지가 부정 없이 오면 그대로 cart_quantity.
    assert classify_cart_utterance("수량 바꿔줘", settings) == "cart_quantity"


def test_classify_cart_utterance_quantity_literal_negation_phrase_stays_cart_add() -> None:
    """패킷이 명시한 회귀 문구 그대로 — "수량 바꾸지 마"는 cart_add(부정)여야 한다."""
    assert classify_cart_utterance("수량 바꾸지 마", get_settings()) == "cart_add"


async def test_stream_cart_add_quantity_intent_delegates_to_quantity_change() -> None:
    """`stream_cart_add` 의 `classify_cart_utterance` 2선 방어가 `cart_quantity` 를 돌려주면
    `stream_cart_quantity_change` 로 위임돼 CART_QUANTITY_CHANGED 가 나가야 한다(cart_remove
    2선 경로와 대칭 — `cart/graph.py` 위임 분기 참조)."""
    store = CartStateStore()

    async def get_cart_fn(*, user_id=None, guest_id=None):
        return CartView(items=[CartViewItem(cart_item_id=1, product_id=10, product_name="이어폰")])

    class _Result:
        success = True
        quantity = 3

    async def change_quantity_fn(cart_item_id, quantity, *, user_id=None, guest_id=None):
        return _Result()

    events = await _collect(
        stream_cart_add(
            identity=_member(),
            cart=CartIntent(target_quantity=3),
            cart_store=store,
            thread_key="m:t-2nd-line-quantity",
            settings=get_settings(),
            message="이어폰 3개로 바꿔줘",
            get_cart_fn=get_cart_fn,
            change_quantity_fn=change_quantity_fn,
        )
    )
    actions = [e["data"] for e in events if e["type"] == "action"]
    assert actions[0]["type"] == "CART_QUANTITY_CHANGED"
    assert actions[0]["quantity"] == 3
