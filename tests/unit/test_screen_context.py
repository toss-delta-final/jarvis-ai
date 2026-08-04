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

    발화는 **이름 지목**("신상품 담아줘")이다. 맨 지시대명사("저거")를 쓰면 화면 후보가 1건일 때
    `screen_reference` 가 그 1건으로 확정해 버려(정본 "후보 1건이면 확정") LLM 의 999 가 애초에
    폐기되고, 정작 이 테스트가 지키려는 **가드**를 지나가지 않는다. 오버라이드 쪽은
    `test_sole_screen_candidate_overrides_a_hallucinated_product_id` 가 따로 고정한다.
    """
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"두 목록 밖 id 가 Spring 담기까지 도달하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="신상품 담아줘",
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


# ─────────── 라운드 2 — 프로그램 구성 경로 · 프롬프트 신뢰경계 ───────────


def test_screen_survives_being_constructed_as_a_model_instance() -> None:
    """[#118 라운드 1 리뷰] `ScreenContext` 인스턴스로 넘겨도 screen 이 사라지면 안 된다.

    before-validator 가 `isinstance(v, dict)` 만 보던 동안, 와이어(JSON→dict) 경로는 멀쩡한데
    **요청을 코드로 구성**하면 screen 이 소리 없이 None 이 됐다(테스트·스크립트·내부 호출 경로).
    """
    from app.schemas.chat import ScreenContext, ScreenProduct

    request = BuyerChatRequest(
        sessionId="s1",
        threadId="t1",
        message="이거 담아줘",
        screen=ScreenContext(
            page_type="chat",
            products=[ScreenProduct(product_id=501, name="러그")],
            columns=3,
        ),
    )
    assert request.screen is not None
    assert [p.product_id for p in request.screen.products] == [501]
    assert request.screen.columns == 3


def test_screen_instance_path_goes_through_the_same_sanitizing() -> None:
    """인스턴스 입력도 dict 로 되돌려 같은 정제를 태운다 — 구성 경로에 따라 정제가 갈리면 안 된다."""
    from app.schemas.chat import ScreenContext, ScreenProduct

    request = BuyerChatRequest(
        sessionId="s1",
        threadId="t1",
        message="m",
        screen=ScreenContext(
            page_type="chat",
            products=[ScreenProduct(product_id=501, name="러​그" + "가" * 500)],
        ),
    )
    assert request.screen is not None
    name = request.screen.products[0].name
    assert "​" not in name
    assert len(name) == get_settings().screen_text_max_chars


def test_screen_accepts_any_mapping_not_only_dict() -> None:
    """dict 서브클래스가 아닌 Mapping(MappingProxyType)도 dict 와 같게 다룬다."""
    from types import MappingProxyType

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen=MappingProxyType(
                {
                    "pageType": "chat",
                    "products": [MappingProxyType({"productId": 501, "name": "러그"})],
                }
            )
        )
    )
    assert request.screen is not None
    assert [p.product_id for p in request.screen.products] == [501]


def test_screen_product_names_are_sanitized_and_truncated() -> None:
    """FE 문자열이 그대로 LLM 프롬프트로 간다 — 제어·zero-width·bidi 를 없애고 항목당 절단한다."""
    cap = get_settings().screen_text_max_chars
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "chat",
                "products": [
                    {"productId": 1, "name": "무선‮이어폰"},
                    {"productId": 2, "name": "가" * (cap + 50)},
                    {"productId": 3, "name": "줄바꿈\n포함\t이름"},
                ],
            }
        )
    )
    assert request.screen is not None
    names = [p.name for p in request.screen.products]
    assert names[0] == "무선이어폰"
    assert len(names[1]) == cap
    assert names[2] == "줄바꿈 포함 이름"  # 공백류는 단일 공백으로 접힌다


def test_screen_filter_values_are_sanitized_and_blank_results_dropped() -> None:
    cap = get_settings().screen_text_max_chars
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "seller_orders",
                "filters": {"status": "배​송중", "sort": "​", "page": "1" * (cap + 10)},
            }
        )
    )
    assert request.screen is not None
    assert request.screen.filters["status"] == "배송중"
    assert "sort" not in request.screen.filters  # 정제 결과가 비면 키째 버린다
    assert len(request.screen.filters["page"]) == cap


def test_oversized_screen_text_is_truncated_not_rejected() -> None:
    """상한 초과는 400 이 아니라 **절단**이다 — screen 은 맥락 힌트라 플로우를 막지 않는다(§3.1)."""
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={"pageType": "chat", "products": [{"productId": 1, "name": "가" * 10_000}]}
        )
    )
    assert request.screen is not None
    assert len(request.screen.products[0].name) == get_settings().screen_text_max_chars


def test_screen_page_type_labels_cover_the_three_page_types_that_actually_arrive() -> None:
    """정본: "현재 UI 로 실제 오는 값은 3종" — 나머지 11종은 매핑이 없어 화면명이 생략된다."""
    labels = get_settings().screen_page_type_labels
    assert set(labels) == {"chat", "seller_orders", "seller_products"}
    assert set(labels) <= SCREEN_PAGE_TYPES


# ─────────── 라운드 2 — 화면 지시어의 코드 해소 (screen_reference) ───────────


async def test_sole_screen_candidate_overrides_a_hallucinated_product_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """맨 지시대명사 + 화면 후보 1건이면 **코드가 그 1건으로 확정**한다 (정본 §3.1).

    실 LLM N=8 프로브에서 이 셀은 2/8 이었고, 실패 6건이 전부 "화면에 없는 직전 추천 상품을
    확정"이었다 — 가드가 못 막는 오담기다(목록 **안**이라 통과한다). 결정적 규칙이라 코드가 정한다.
    """
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=91)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="이거 담아줘",
            threadId="t-screen-sole",
            screen={"pageType": "chat", "products": [{"productId": 555, "name": "러그"}]},
        )
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(await _thread_key(request, _member()), [(101, "이어폰")])
    # LLM 은 화면에 없는 101(직전 추천)을 골랐다 — 프로브에서 실제로 6/8 이 이 모양이었다.
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 101, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 555  # LLM 산출(101) 이 아니라 화면의 유일 후보


async def test_ambiguous_screen_candidates_force_a_reask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[안전 셀] 후보가 여러 건이면 임의 확정 금지 — 되물음이다 (정본 §3.1).

    프로브에서 이 셀은 **0/8**(8회 모두 자신 있게 하나를 골랐다)이었다. 여기서 고르는 것은
    정확도 문제가 아니라 사용자가 말하지 않은 상품이 담기는 것이라, 코드가 되물음으로 강제한다.
    """
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"후보 다건에서 임의 확정이 담기까지 도달하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="이거 담아줘",
            threadId="t-screen-ambiguous",
            screen={
                "pageType": "chat",
                "columns": 3,
                "products": [
                    {"productId": 501, "name": "러그"},
                    {"productId": 502, "name": "바구니"},
                    {"productId": 503, "name": "가습기"},
                ],
            },
        )
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 501, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert "action" not in [e["type"] for e in events]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    # 되물음 문구가 상황에 맞아야 한다 — 화면에 상품이 보이는데 "추천을 먼저 받아보시면"이라고
    # 답하면 사용자는 무엇을 물어야 할지 알 수 없다(라운드 2 리뷰 0-a).
    assert "화면에 보이는 상품 중" in token_text
    assert "추천을 먼저 받아보시면" not in token_text


async def test_ordinal_and_coordinate_references_are_resolved_by_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """순번·좌표는 산술이라 코드가 푼다 — LLM 은 이웃 칸을 고르는 오답을 3/8 냈다."""
    import app.services.spring_client as sc

    added: list[int] = []

    async def fake_add(req):  # noqa: ANN001
        added.append(req.product_id)
        return AddToCartResult(success=True, cart_item_id=92)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    products = [{"productId": 500 + i, "name": f"상품{i}"} for i in range(1, 10)]
    for message, expected in (("3번째 거 담아줘", 503), ("3번째 줄 2번째 담아줘", 508)):
        request = BuyerChatRequest.model_validate(
            _buyer_payload(
                message=message,
                threadId=f"t-screen-{expected}",
                screen={"pageType": "chat", "columns": 3, "products": products},
            )
        )
        # LLM 은 매번 엉뚱한 이웃(509)을 골랐다고 가정한다.
        llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 509, "quantity": 1}})
        await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert added == [503, 508]


async def test_spoken_product_id_outside_both_lists_forces_a_reask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사용자가 말한 id 를 못 들어줄 때 **조용히 다른 상품을 담지 않는다**.

    프로브에서 `301 담아줘`(두 목록 밖)는 LLM 이 6/8 을 화면의 다른 상품으로 대체했다.
    가드는 301 만 막을 뿐 대체된 상품은 목록 안이라 그대로 담긴다.
    """
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"말하지 않은 상품이 대신 담기면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="301 담아줘",
            threadId="t-screen-unknown-id",
            screen={
                "pageType": "chat",
                "columns": 3,
                "products": [
                    {"productId": 501, "name": "러그"},
                    {"productId": 502, "name": "바구니"},
                ],
            },
        )
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 501, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert "action" not in [e["type"] for e in events]


async def test_screen_reference_never_fires_without_screen_products() -> None:
    """[안전 논거] 화면 상품이 없으면 이 해소기는 **한 번도 돌지 않는다**.

    #234/#239/#240 회귀 대조군은 전부 `screen` 이 없는 요청이므로, 이 규칙이 그 기준선에
    구조적으로 닿을 수 없다는 것이 이 모듈을 도입한 안전 논거다.
    """
    from app.agents.buyer.screen_reference import resolve_screen_reference

    for message in ("이거 담아줘", "3번째 거 담아줘", "3번째 줄 2번째 담아줘", "301 담아줘"):
        assert (
            resolve_screen_reference(
                message,
                products=[],
                columns=3,
                allowed_product_ids={101},
                deictic_markers=get_settings().screen_deictic_markers,
                context_reference_markers=get_settings().screen_context_reference_markers,
            )
            is None
        )


def test_screen_reference_leaves_name_and_unmatched_utterances_to_the_llm() -> None:
    """이름 매칭은 LLM 이 8/8 로 잘한다 — 코드가 가로채지 않는다."""
    from app.agents.buyer.screen_reference import resolve_screen_reference

    products = [(501, "코튼 러그"), (502, "라탄 바구니")]
    kwargs = {
        "products": products,
        "columns": 2,
        "allowed_product_ids": {501, 502},
        "deictic_markers": get_settings().screen_deictic_markers,
        "context_reference_markers": get_settings().screen_context_reference_markers,
    }
    assert resolve_screen_reference("라탄 바구니 담아줘", **kwargs) is None
    assert resolve_screen_reference("이 라탄 바구니 담아줘", **kwargs) is None  # 이름이 있으면 양보
    assert resolve_screen_reference("추천해줘", **kwargs) is None


def test_out_of_range_positions_reask_instead_of_guessing() -> None:
    """범위를 벗어난 순번·좌표, columns 없는 좌표 지시는 **되물음**이다(§3.1 "좌표 지시만 불가")."""
    from app.agents.buyer.screen_reference import resolve_screen_reference

    products = [(500 + i, f"상품{i}") for i in range(1, 4)]
    settings = get_settings()
    markers = settings.screen_deictic_markers
    context_markers = settings.screen_context_reference_markers
    out_of_range = resolve_screen_reference(
        "9번째 거 담아줘",
        products=products,
        columns=3,
        allowed_product_ids=set(),
        deictic_markers=markers,
        context_reference_markers=context_markers,
    )
    assert out_of_range is not None and out_of_range.product_id is None
    no_columns = resolve_screen_reference(
        "2번째 줄 1번째 담아줘",
        products=products,
        columns=None,
        allowed_product_ids=set(),
        deictic_markers=markers,
        context_reference_markers=context_markers,
    )
    assert no_columns is not None and no_columns.product_id is None


# ─────────── 라운드 3 — 리뷰 지적 회귀 가드 ───────────


def _resolve(message: str, products, columns=3, allowed=None):
    """프로덕션 해소기를 config 기본값 그대로 호출한다(기본값 자체가 이번 수정의 일부다)."""
    from app.agents.buyer.screen_reference import resolve_screen_reference

    settings = get_settings()
    return resolve_screen_reference(
        message,
        products=products,
        columns=columns,
        allowed_product_ids=allowed if allowed is not None else {pid for pid, _ in products},
        deictic_markers=settings.screen_deictic_markers,
        context_reference_markers=settings.screen_context_reference_markers,
    )


def test_conversation_deictic_is_not_forced_onto_the_screen_product() -> None:
    """[리뷰 F-1] `"아까 추천해준 그거 담아줘"` 가 화면 상품으로 확정되면 **오담기**다.

    직전 추천이 (101,"이어폰")이고 화면이 (555,"러그") 1건일 때, 사용자는 명시적으로 대화 맥락을
    참조했는데 해소기가 러그로 확정했다(실제 재현). 이 저장소에서 `"그거"`·`"아까"` 류는 직전
    추천 맥락으로 확립돼 있고(decompose `_SYSTEM` 하중 문구·#234 프로브), 정본 §3.1 지시어 해소
    표가 든 예는 `"이거"` 다. 두 방향 모두로 막는다 — 대화 참조 표지, 그리고 근칭만 남긴 기본값.
    """
    products = [(555, "러그")]
    assert _resolve("아까 추천해준 그거 담아줘", products, columns=1, allowed={101, 555}) is None
    assert _resolve("저번에 본 그거 담아줘", products, columns=1, allowed={101, 555}) is None
    # 대화 참조 표지가 없어도 `"그거"` 자체가 화면 지시어 기본값에서 빠져 있다(막는 축 ①).
    assert _resolve("그거 담아줘", products, columns=1, allowed={101, 555}) is None
    # 막는 축 ② — **근칭을 써도** 대화 참조가 있으면 화면으로 확정하지 않는다. 축 ①만으로는
    # 이 두 발화가 각각 화면 1건 확정·순번 확정으로 새므로, 축 ②를 독립적으로 고정한다.
    assert _resolve("아까 추천해준 이거 담아줘", products, columns=1, allowed={101, 555}) is None
    five = [(500 + i, f"상품{i}") for i in range(1, 6)]
    assert _resolve("저번에 말한 3번째 거 담아줘", five, columns=3) is None
    # 근칭 + 대화 참조 없음은 그대로 화면을 가리킨다 — 라운드 2가 되찾은 동작이 살아 있어야 한다.
    resolved = _resolve("이거 담아줘", products, columns=1, allowed={101, 555})
    assert resolved is not None and resolved.product_id == 555


def test_conversation_reference_markers_are_configured_and_narrow() -> None:
    """대화 참조 표지는 좁게 유지한다 — 넓히면 정상적인 화면 지시까지 LLM 으로 넘어간다."""
    settings = get_settings()
    assert "아까" in settings.screen_context_reference_markers
    assert "저번" in settings.screen_context_reference_markers
    # `"그거"`·`"그것"` 은 대화 지시어라 화면 지시어 기본값에서 빠져 있어야 한다.
    assert "그거" not in settings.screen_deictic_markers
    assert "그것" not in settings.screen_deictic_markers
    assert "이거" in settings.screen_deictic_markers


def test_named_product_beats_a_positional_number() -> None:
    """[리뷰 F-2] 이름을 지목했는데 순번이 이기면 **엉뚱한 상품이 담긴다**.

    `"무선 이어폰 2번째 옵션으로 담아줘"` 의 `"2번째"` 는 **옵션**을 수식하는데 화면 순번으로
    읽혀 러그(501)가 확정됐다(실제 재현). 이름 매칭은 프로브에서 LLM 이 8/8 로 가장 잘하는
    신호이고 순번은 그보다 약하다 — 강한 신호가 있으면 약한 신호로 덮지 않는다.
    """
    products = [(502, "무선 이어폰"), (501, "러그")]
    assert _resolve("무선 이어폰 2번째 옵션으로 담아줘", products, columns=2) is None
    assert _resolve("무선 이어폰 2번째 줄 1번째로 담아줘", products, columns=2) is None
    # 이름이 없으면 순번은 그대로 발동한다.
    resolved = _resolve("2번째 거 담아줘", products, columns=2)
    assert resolved is not None and resolved.product_id == 501


@pytest.mark.parametrize(
    "message",
    [
        "10만원대 무선 이어폰 담아줘",  # 숫자 바로 뒤가 `만` 이라 접미 목록으로는 못 막았다
        "2026년형 TV 담아줘",
        "128GB 모델 담아줘",
        "2024년형 담아줘",
        "55인치 담아줘",
        "500ml 담아줘",
    ],
)
def test_measurements_and_years_are_not_mistaken_for_product_ids(message: str) -> None:
    """[리뷰 F-3] 가격·연도·용량의 숫자를 상품 id 로 오인해 **정상 발화가 되물음으로 막혔다**.

    방향은 안전(오담기가 아니라 되물음)하지만 `"10만원대 무선 이어폰 담아줘"` 는 흔한 발화다.
    단위·수식이 붙은 숫자는 문자와 맞닿아 있다는 **구조적 성질**로 배제한다 — 접미 목록을 늘리는
    땜질은 새 단위가 나올 때마다 다시 뚫린다.
    """
    products = [(501, "러그"), (502, "바구니")]
    assert _resolve(message, products, columns=2) is None


def test_a_standalone_unknown_id_still_forces_a_reask() -> None:
    """F-3 을 좁히면서 원래 목적은 지켜야 한다 — 두 목록 밖 id 는 여전히 되물음이다."""
    products = [(501, "러그"), (502, "바구니")]
    resolved = _resolve("301 담아줘", products, columns=2)
    assert resolved is not None and resolved.product_id is None
    assert resolved.reason == "unknown_product_id_spoken"
    # 목록 안 id 를 말했으면 막지 않는다.
    assert _resolve("501 담아줘", products, columns=2) is None


async def test_screenless_reask_wording_is_byte_identical_to_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[회귀 0] `screen` 이 없는 담기 되물음 문구는 오늘과 **바이트 동일**해야 한다.

    라운드 3이 추가한 화면용 문구가 기존 경로로 새면, FE 가 `screen` 을 보내지 않는 절대다수
    경로의 사용자 경험이 조용히 바뀐다.
    """
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"해소 실패가 담기까지 도달하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="그거 담아줘", threadId="t-screenless-wording")
    )
    assert request.screen is None
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 999, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token_text == "어떤 상품을 담을까요? 추천을 먼저 받아보시면 담아드릴게요."


async def test_spoken_unknown_id_reask_says_it_could_not_be_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """미지 id 는 위치를 되묻는 것이 답이 아니다 — 못 찾았다는 사실을 먼저 알린다."""
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"말하지 않은 상품이 대신 담기면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="301 담아줘",
            threadId="t-screen-notfound-wording",
            screen={
                "pageType": "chat",
                "columns": 2,
                "products": [
                    {"productId": 501, "name": "러그"},
                    {"productId": 502, "name": "바구니"},
                ],
            },
        )
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 501, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "화면에서 찾지 못했어요" in token_text


def test_unresolved_notice_falls_back_to_the_default_for_unknown_reasons() -> None:
    """새 사유가 생겨도 문구 분기가 조용히 빈 문자열을 내지 않는다(기본 문구로 degrade)."""
    from app.agents.buyer.cart.graph import _unresolved_notice

    assert _unresolved_notice(None) == "어떤 상품을 담을까요? 추천을 먼저 받아보시면 담아드릴게요."
    assert _unresolved_notice("some_future_reason") == _unresolved_notice(None)
    assert _unresolved_notice("ambiguous_screen_candidates") != _unresolved_notice(None)
