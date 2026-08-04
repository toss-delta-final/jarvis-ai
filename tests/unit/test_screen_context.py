"""화면 맥락 `screen` 필드 — 관대 유효성 · 담기 가드 합집합 회귀 (이슈 #118)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents.buyer.cart.state import get_cart_store
from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.session_state import context_thread_key
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.session_context import BuyerSessionInput
from app.schemas.chat import SCREEN_PAGE_TYPES, BuyerChatRequest
from app.schemas.seller import SellerChatRequest
from app.schemas.spring import AddToCartResult, CartView
from tests._fakes import FakeLLM


def _buyer_payload(**updates):
    payload = {"sessionId": "s1", "threadId": "t1", "message": "추천해줘"}
    payload.update(updates)
    return payload


def _member() -> Identity:
    # user_id 는 cart_identity 가 int() 로 파싱한다(§4.1) — 비숫자면 익명 취급되어 담기가
    # "로그인이 필요해요"로 조기 실패하므로 숫자 문자열을 쓴다(test_cart.py 와 동일 관례).
    return Identity(user_id="123", is_guest=False, seller_id=None, subject="123")


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
        request_id="screen-context-test",
        context_id=context.context_id,
        record_model_call=lambda *_: None,
    )


async def _run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    async for frame in _production_run_buyer_turn(
        request,
        identity,
        observer=observer,
        **kwargs,
    ):
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


def _empty_cart_view(**_):
    async def _get(*, user_id=None, guest_id=None):
        return CartView(items=[])

    return _get


# ─────────── 스키마·유효성 (400 이 아님을 증명) ───────────


def test_screen_context_round_trips_page_type_filters_products_columns() -> None:
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "chat",
                "filters": {"status": "전체", "sort": "최신순", "page": "1"},
                "products": [
                    {"productId": 101, "name": "이어폰A"},
                    {"productId": 102, "name": "이어폰B"},
                ],
                "columns": 3,
            }
        )
    )
    assert parsed.screen is not None
    assert parsed.screen.page_type == "chat"
    assert parsed.screen.filters == {"status": "전체", "sort": "최신순", "page": "1"}
    assert [(p.product_id, p.name) for p in parsed.screen.products] == [
        (101, "이어폰A"),
        (102, "이어폰B"),
    ]
    assert parsed.screen.columns == 3


def test_screen_missing_page_type_is_ignored_without_validation_error() -> None:
    parsed = BuyerChatRequest.model_validate(_buyer_payload(screen={"filters": {"status": "전체"}}))
    assert parsed.screen is None


def test_screen_unknown_page_type_is_ignored_without_validation_error() -> None:
    assert "popular_v2" not in SCREEN_PAGE_TYPES
    parsed = BuyerChatRequest.model_validate(_buyer_payload(screen={"pageType": "popular_v2"}))
    assert parsed.screen is None


@pytest.mark.parametrize("bad_screen", ["chat", ["chat"], 123])
def test_screen_non_object_is_ignored_without_validation_error(bad_screen) -> None:  # noqa: ANN001
    parsed = BuyerChatRequest.model_validate(_buyer_payload(screen=bad_screen))
    assert parsed.screen is None


def test_screen_products_truncated_to_default_max_preserving_order() -> None:
    """기본 설정(screen_products_max=20) 그대로 돌려 기본값 조합 자체를 검증한다."""
    assert get_settings().screen_products_max == 20
    products = [{"productId": i, "name": f"p{i}"} for i in range(1, 26)]
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(screen={"pageType": "chat", "products": products})
    )
    assert parsed.screen is not None
    assert [p.product_id for p in parsed.screen.products] == list(range(1, 21))


def test_screen_product_missing_name_defaults_to_empty_string_but_keeps_id() -> None:
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(screen={"pageType": "chat", "products": [{"productId": 101}]})
    )
    assert parsed.screen is not None
    assert [(p.product_id, p.name) for p in parsed.screen.products] == [(101, "")]


def test_screen_products_drops_only_the_garbage_item() -> None:
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "chat",
                "products": [
                    {"productId": 101, "name": "이어폰A"},
                    {"foo": 1},
                    "garbage",
                    {"productId": 103, "name": "이어폰C"},
                ],
            }
        )
    )
    assert parsed.screen is not None
    assert [p.product_id for p in parsed.screen.products] == [101, 103]


@pytest.mark.parametrize("columns", [None, 0, "3"])
def test_screen_invalid_columns_is_ignored_but_rest_survives(columns) -> None:  # noqa: ANN001
    screen: dict[str, object] = {
        "pageType": "chat",
        "products": [{"productId": 101, "name": "A"}],
    }
    if columns is not None:
        screen["columns"] = columns
    parsed = BuyerChatRequest.model_validate(_buyer_payload(screen=screen))
    assert parsed.screen is not None
    assert parsed.screen.columns is None
    assert parsed.screen.products[0].product_id == 101


def test_screen_filters_unknown_key_and_non_string_value_are_dropped() -> None:
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "seller_products",
                "filters": {
                    "status": "판매중",
                    "sort": "최신순",
                    "page": "1",
                    "category": "가전",  # 허용 3종 밖 — 그 키만 버림
                    "extra": 123,  # 비문자열 값 — 그 키만 버림
                },
            }
        )
    )
    assert parsed.screen is not None
    assert parsed.screen.filters == {"status": "판매중", "sort": "최신순", "page": "1"}


def test_seller_chat_request_accepts_screen_but_not_condition_actions() -> None:
    # 공용 필드 회귀 가드 — screen 은 구매자·판매자 공용이지만 conditionActions 는 구매자 전용이다.
    assert "condition_actions" not in SellerChatRequest.model_fields
    seller = SellerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "message": "주문 확인해줘",
            "screen": {"pageType": "seller_orders", "filters": {"status": "신규주문"}},
        }
    )
    assert seller.screen is not None
    assert seller.screen.page_type == "seller_orders"


def test_screen_absent_key_defaults_to_none_and_legacy_request_is_unaffected() -> None:
    parsed = BuyerChatRequest.model_validate(_buyer_payload())
    assert parsed.screen is None
    assert parsed.message == "추천해줘"


def test_screen_products_max_is_config_driven_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "screen_products_max", 3)
    products = [{"productId": i, "name": f"p{i}"} for i in range(1, 6)]
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(screen={"pageType": "chat", "products": products})
    )
    assert parsed.screen is not None
    assert [p.product_id for p in parsed.screen.products] == [1, 2, 3]


# ─────────── 담기 가드 (합집합이지 프리패스가 아니다) ───────────


async def test_screen_products_extend_cart_add_allowlist_beyond_last_reco(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """screen.products 의 productId 는 직전 추천에 없어도 담을 수 있다(합집합)."""
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        return AddToCartResult(success=True, cart_item_id=77)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="이거 담아줘",
            threadId="t-screen-union",
            screen={
                "pageType": "chat",
                "products": [{"productId": 555, "name": "신상품"}],
            },
        )
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 555, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert action["cartItemId"] == 77


async def test_ids_outside_both_lists_are_still_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[회귀·최중요] 직전 추천에도 screen.products 에도 없는 id 는 여전히 차단된다.

    LLM 이 발화 속 임의 숫자(999)를 오추출한 상황을 재현한다 — 101(직전 추천)도
    555(screen.products)도 아닌 값이 decompose 산출물로 온다.
    """
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"두 목록 밖 id 가 Spring 담기까지 도달하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="저거 담아줘",
            threadId="t-screen-outside",
            screen={
                "pageType": "chat",
                "products": [{"productId": 555, "name": "신상품"}],
            },
        )
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(await _thread_key(request, _member()), [(101, "이어폰")])
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 999, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert "action" not in [e["type"] for e in events]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "어떤 상품을 담을까요" in token_text


async def test_cart_add_without_screen_uses_last_reco_only_like_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """screen 이 없는 담기 요청은 오늘과 동일하게 직전 추천 목록만으로 판정한다(회귀 0)."""
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        return AddToCartResult(success=True, cart_item_id=88)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="담아줘", threadId="t-no-screen")
    )
    assert request.screen is None
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(await _thread_key(request, _member()), [(101, "이어폰")])
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 101, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert action["cartItemId"] == 88


async def test_ignored_screen_products_do_not_enter_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """관대 무시(미지의 pageType)로 사라진 screen 의 products 는 allowed 에 들어가지 않는다.

    즉 관대 무시가 가드 우회 경로로 쓰이지 않음을 증명한다.
    """
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"무시된 screen 의 productId 가 담기에 도달하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="이거 담아줘",
            threadId="t-screen-ignored",
            screen={
                "pageType": "popular_v2",  # 14종 밖 — screen 전체가 무시된다
                "products": [{"productId": 777, "name": "신상품"}],
            },
        )
    )
    assert request.screen is None  # 무시가 실제로 일어났는지 사전 확인
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 777, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert "action" not in [e["type"] for e in events]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "어떤 상품을 담을까요" in token_text
