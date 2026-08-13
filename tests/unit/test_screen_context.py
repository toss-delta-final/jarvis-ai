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
from app.schemas.spring import AddToCartResult, CartOption, CartView, WishlistAddResult
from app.services.spring_client import CartOptionRequired
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


# ─────────── Claude 리뷰 11차 — pageType 역할 경계(구매자/판매자) ───────────


def test_screen_page_type_from_the_other_role_is_ignored_not_rejected() -> None:
    """[Claude 리뷰 11차] 역할에 맞지 않는 pageType 은 **400 이 아니라 screen 전체를 관대 무시**한다.

    정본 §3.1 유효성 표는 "pageType 누락·미지의 값 → screen 전체 무시하고 200 진행"이고 screen 은
    **어떤 경우에도 400 을 내지 않는 것**이 이 필드의 핵심 계약이다(conditionActions 의 엄격함과
    정반대). 역할 경계는 정본이 어휘를 구매자/판매자로 "분류"했을 뿐 반대 역할 값을 거부하라고
    명시하지는 않았다 — 이 검증은 계약 위반을 고친 것이 아니라 그 분류를 코드로도 지키는
    **방어적 강화**이며, 위반해도 "400 없음" 계약은 그대로 지킨다(어휘 밖 값과 같은 경로). 재현:
    이 검증이 없으면 구매자 요청의 `seller_orders` 가 "주문 관리" 라벨로 그대로 구매자 decompose
    프롬프트 SCREEN 블록에 실린다(반대 방향도 대칭).
    """
    buyer = BuyerChatRequest.model_validate(_buyer_payload(screen={"pageType": "seller_orders"}))
    assert buyer.screen is None

    seller = SellerChatRequest.model_validate(
        {
            "sessionId": "s1",
            "threadId": "t1",
            "message": "확인해줘",
            "screen": {"pageType": "checkout"},
        }
    )
    assert seller.screen is None


def test_screen_seller_role_also_ignores_a_wholly_unknown_page_type() -> None:
    """대조군 — 판매자 요청도 기존 '미지의 값' 무시 동작을 그대로 유지한다(회귀 금지)."""
    assert "popular_v2" not in SCREEN_PAGE_TYPES
    seller = SellerChatRequest.model_validate(
        {"sessionId": "s1", "threadId": "t1", "message": "m", "screen": {"pageType": "popular_v2"}}
    )
    assert seller.screen is None


@pytest.mark.parametrize(
    "page_type",
    [
        "home",
        "category",
        "search",
        "product_detail",
        "cart",
        "checkout",
        "order_complete",
        "my",
        "chat",
        "auth",
    ],
)
def test_screen_buyer_role_vocabulary_still_passes(page_type: str) -> None:
    """대조군 — 구매자 10종은 역할 경계 도입 후에도 정상 통과한다(회귀 금지)."""
    parsed = BuyerChatRequest.model_validate(_buyer_payload(screen={"pageType": page_type}))
    assert parsed.screen is not None
    assert parsed.screen.page_type == page_type


@pytest.mark.parametrize(
    "page_type", ["seller_dashboard", "seller_orders", "seller_products", "seller_chat"]
)
def test_screen_seller_role_vocabulary_still_passes(page_type: str) -> None:
    """대조군 — 판매자 4종은 역할 경계 도입 후에도 정상 통과한다(회귀 금지)."""
    seller = SellerChatRequest.model_validate(
        {"sessionId": "s1", "threadId": "t1", "message": "m", "screen": {"pageType": page_type}}
    )
    assert seller.screen is not None
    assert seller.screen.page_type == page_type


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


def test_a_product_id_over_the_bigint_max_is_dropped_but_siblings_survive() -> None:
    """[Claude 리뷰 15차, F-18] `productId` 가 BIGINT 상한(2**63-1)을 넘으면 그 항목만 무효
    처리된다 — 나머지 상품은 관대 유효성대로 살아남는다.

    수정 전 재현: `_coerce_positive_int` 가 `> 0` 만 확인해 `2**63 + 12345` 가 그대로 통과했다
    — recommendations.ProductId·events.user_id·profile.user_id 와 달리 이 필드만 BIGINT 상한이
    빠져 있었다. 통과한 값은 `allowed_product_ids`(app/agents/buyer/graph.py)에 합류해 담기
    가드를 통과하고, 상한 없는 `AddToCartRequest.product_id` 로 Spring I-2 호출에 실려 Spring
    Long 역직렬화에서 예측 불가한 실패가 된다.
    """
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "chat",
                "products": [
                    {"productId": 101, "name": "이어폰A"},
                    {"productId": 2**63 + 12345, "name": "오버플로"},
                    {"productId": 103, "name": "이어폰C"},
                ],
            }
        )
    )
    assert parsed.screen is not None
    assert [p.product_id for p in parsed.screen.products] == [101, 103]


def test_a_product_id_at_the_bigint_max_boundary_still_passes() -> None:
    """[Claude 리뷰 15차, F-18] 경계값(2**63-1)은 상한 검사에 걸리지 않고 정상 통과한다."""
    boundary = 2**63 - 1
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "chat",
                "products": [{"productId": boundary, "name": "경계값"}],
            }
        )
    )
    assert parsed.screen is not None
    assert [p.product_id for p in parsed.screen.products] == [boundary]


def test_non_positive_and_non_integer_product_ids_are_still_dropped() -> None:
    """[Claude 리뷰 15차, F-18] 상한 검사를 추가해도 기존 관대 유효성(음수·0·비정수 항목만
    버림)은 그대로다 — `0 < result <= _BIGINT_MAX` 로 바꿨을 뿐 하한 쪽 동작은 손대지 않았다.
    """
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "chat",
                "products": [
                    {"productId": -1, "name": "음수"},
                    {"productId": 0, "name": "영"},
                    {"productId": "abc", "name": "비정수"},
                    {"productId": 101, "name": "정상"},
                ],
            }
        )
    )
    assert parsed.screen is not None
    assert [p.product_id for p in parsed.screen.products] == [101]


def test_invalid_items_before_the_cap_do_not_starve_later_valid_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[Claude 리뷰 12차] 절단보다 필터링이 먼저다 — 앞쪽 불량 항목이 상한 슬롯을 먹지 않는다.

    `products_max=3` 인 상태에서 앞에 불량 항목 2건을 두고 뒤에 정상 5건을 두면, "먼저 자르고
    나중에 거르는"(절단 먼저) 순서에서는 `raw_products[:3]` 이 이미 불량 2건 + 정상 1건이라
    정제 후 정상 상품이 **1건**만 남는다(뒤쪽 정상 4건이 불필요하게 잘림, 실제 재현). 올바른
    순서(먼저 거르고 나중에 자름)에서는 상한만큼(3건) 정상 상품이 그대로 남아야 한다.
    """
    monkeypatch.setattr(get_settings(), "screen_products_max", 3)
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "chat",
                "products": [
                    {"foo": 1},  # 불량 1 — Mapping 이지만 productId 없음
                    "garbage",  # 불량 2 — Mapping 도 아님
                    {"productId": 1, "name": "p1"},
                    {"productId": 2, "name": "p2"},
                    {"productId": 3, "name": "p3"},
                    {"productId": 4, "name": "p4"},
                    {"productId": 5, "name": "p5"},
                ],
            }
        )
    )
    assert parsed.screen is not None
    assert [p.product_id for p in parsed.screen.products] == [1, 2, 3]


def test_dropped_invalid_screen_items_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """[Claude 리뷰 12차] 불량 항목이 하나라도 걸러지면 그 사실을 로그로 남긴다.

    불량 항목이 있었다는 것은 `resolve_screen_reference`·`grid_position` 이 신뢰하는 "남은
    배열의 인덱스 = 화면 실제 위치"라는 전제가 이번 요청에서 흔들렸을 수 있다는 뜻이다(인덱스
    시프트 자체는 FE 가 보낸 배열을 신뢰할 수밖에 없어 구조적으로 없앨 수 없다 — 관측만 남긴다).
    """
    with caplog.at_level("WARNING"):
        parsed = BuyerChatRequest.model_validate(
            _buyer_payload(
                screen={
                    "pageType": "chat",
                    "products": [{"productId": 101, "name": "A"}, {"foo": 1}],
                }
            )
        )
    assert parsed.screen is not None
    assert "screen_products_contained_invalid_items" in caplog.text


def test_no_warning_logged_when_every_screen_item_is_valid(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """대조군 — 불량 항목이 하나도 없으면 경고 로그가 없다(무해한 정상 경로 소음 방지)."""
    with caplog.at_level("WARNING"):
        parsed = BuyerChatRequest.model_validate(
            _buyer_payload(
                screen={
                    "pageType": "chat",
                    "products": [{"productId": 101, "name": "A"}, {"productId": 102, "name": "B"}],
                }
            )
        )
    assert parsed.screen is not None
    assert "screen_products_contained_invalid_items" not in caplog.text


def test_products_raw_scan_hard_cap_bounds_the_scan_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[Claude 리뷰 13차] 원본 배열 길이 자체에 하드 상한(`screen_products_raw_scan_max`)을 건다.

    12차 수정 이후 필터링 루프는 유효 항목이 `screen_products_max` 에 도달할 때까지 원본 배열을
    순회한다 — 이 하드 상한이 없으면 무효 항목을 수만~수십만 건 채운 요청이 매번 원본 전체를
    스캔한다. `screen_products_max` 를 하드 상한보다 **크게** 설정해도(=슬롯이 남아 있어도) 하드
    상한 밖의 유효 상품은 절대 스캔되지 않는다는 사실 자체가 "원본을 통째로 순회하지 않는다"는
    증거다.
    """
    monkeypatch.setattr(get_settings(), "screen_products_raw_scan_max", 5)
    monkeypatch.setattr(get_settings(), "screen_products_max", 20)  # 하드 상한보다 크게 설정
    products = [{"productId": i, "name": f"p{i}"} for i in range(1, 11)]  # 유효 10건
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(screen={"pageType": "chat", "products": products})
    )
    assert parsed.screen is not None
    # 원본 앞 5건(1~5)만 스캔 대상이라 그 안의 유효 상품 5건만 남는다 — products_max=20 이
    # 더 컸어도 6~10 은 원본 슬라이스 단계에서 이미 잘려 나가 절대 보이지 않는다.
    assert [p.product_id for p in parsed.screen.products] == [1, 2, 3, 4, 5]


def test_a_flood_of_invalid_products_does_not_error_and_stays_fast() -> None:
    """[Claude 리뷰 13차] `screen.products` 에 무효 항목을 대량으로 채워도 400 이 아니다.

    **주의 — 이 타이밍 값은 채택 근거가 아니라 헐거운 스모크 가드다.** 실측해 보니 하드 상한
    (`screen_products_raw_scan_max`) 이 없어도 200,000 건을 순회하는 비용 자체는 CPython
    에서 0.1초 대라 이 시간 상한으로는 고정 전/후를 구분하지 못한다(직접 확인) — 원본을
    실제로 상한만큼만 스캔한다는 증거는
    `test_products_raw_scan_hard_cap_bounds_the_scan_deterministically`(내용 기반, 위)가
    맡는다. 이 테스트는 "대량 무효 입력이 예외 없이 정상 처리된다"는 별개의 회귀만 지킨다.
    """
    import time

    huge_invalid_products = [{"foo": i} for i in range(200_000)]  # productId 없음 — 전부 무효
    started = time.perf_counter()
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(screen={"pageType": "chat", "products": huge_invalid_products})
    )
    elapsed = time.perf_counter() - started
    assert parsed.screen is not None  # 400 이 아니다 — screen 자체는 살아 있다(관대 유효성)
    assert parsed.screen.products == []  # 전부 무효라 유효 상품은 0건
    assert elapsed < 5.0, f"극단적으로 느려짐(캡 자체가 아니라 다른 블로우업 의심) — {elapsed:.3f}s"


def test_screen_text_raw_scan_hard_cap_bounds_the_strip_cost_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[Claude 리뷰 14차, F-17] `name`·`filters` 값의 **원문** 길이 자체에도 하드 상한을 건다.

    `_clean_screen_text` 는 `_strip_unsafe`(문자 단위 O(n) 순회)를 원문 전체에 먼저 돌린 뒤에야
    `screen_text_max_chars` 로 잘랐다 — 원문 길이 자체에는 사전 상한이 없었다. 이제
    `screen_text_raw_scan_max` 로 정제 **전에** 먼저 자른다. `screen_products_raw_scan_max` 와
    같은 증명 패턴(내용 기반) — 원문이 raw_scan_max 를 넘으면 그 뒤쪽은 정제 결과에 아예
    반영되지 않는다는 것을 직접 확인한다(`screen_text_max_chars` 를 하드 캡보다 크게 둬도
    잘린 뒤쪽은 절대 돌아오지 않는다).
    """
    monkeypatch.setattr(get_settings(), "screen_text_raw_scan_max", 5)
    monkeypatch.setattr(get_settings(), "screen_text_max_chars", 50)  # 하드 캡보다 크게 설정
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={"pageType": "chat", "products": [{"productId": 1, "name": "1234567890"}]}
        )
    )
    assert parsed.screen is not None
    # 원문 앞 5자(raw_scan_max)만 정제 대상이라 나머지는 애초에 보이지 않는다 —
    # screen_text_max_chars(50)가 더 컸어도 6번째 자리부터는 원본 슬라이스 단계에서 이미
    # 잘려 나가 절대 스캔되지 않는다.
    assert parsed.screen.products[0].name == "12345"


def test_screen_text_of_reasonable_length_is_preserved_byte_identical() -> None:
    """대조군 — 정상 길이 문자열(기본 raw_scan_max 이내)은 하드 캡 도입 전후로 동일해야 한다."""
    name = "무선 이어폰 프로 맥스 (블랙, 128GB)"
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(screen={"pageType": "chat", "products": [{"productId": 1, "name": name}]})
    )
    assert parsed.screen is not None
    assert parsed.screen.products[0].name == name


def test_a_flood_of_huge_screen_text_does_not_error_and_stays_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[Claude 리뷰 14차, F-17] name·filters 원문이 초대형이어도 400 이 아니고 유계 시간에 끝난다.

    수정 전 실측: name 200만자 × 50건이 25.02초 걸렸다 — `screen_products_raw_scan_max`(500)
    까지 채우면 더 나쁠 수 있고, 구매자 스트림 전체 상한 30s(§2.9 c)를 넘겨 사실상 서비스
    거부였다(`_strip_unsafe` 가 원문 전체를 문자 단위로 먼저 순회했기 때문). `screen_products_max`
    를 50 으로 올려 실측 재현 조건(50건)과 맞춘다 — 기본값(20)이면 앞 20건만 정제되어 원 재현
    수치와 비교할 수 없다.
    """
    import time

    monkeypatch.setattr(get_settings(), "screen_products_max", 50)
    huge_name = "a" * 2_000_000
    products = [{"productId": i, "name": huge_name} for i in range(1, 51)]
    started = time.perf_counter()
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "chat",
                "products": products,
                "filters": {"status": huge_name},
            }
        )
    )
    elapsed = time.perf_counter() - started
    assert parsed.screen is not None  # 400 이 아니다 — screen 자체는 살아 있다(관대 유효성)
    assert (
        elapsed < 5.0
    ), f"극단적으로 느려짐(하드 캡이 빠졌을 가능성 — 수정 전 실측 25.02s) — {elapsed:.3f}s"


def test_filters_lookup_never_iterates_the_raw_mapping() -> None:
    """[Claude 리뷰 13차] filters 는 허용 3키만 `.get()` 으로 직접 조회하고 원본을 순회하지 않는다.

    **타이밍으로는 이 변경을 증명할 수 없었다** — 200,000 개 무효 키를 담은 dict 를
    `.items()` 로 순회해도 0.1초 대라(직접 확인) 순회 유무를 시간으로 가를 수 없다. 그래서
    `.items()`/`__iter__` 를 부르는 순간 즉시 실패하는 가짜 `Mapping` 을 주입해, 코드가
    실제로 원본을 한 번도 순회하지 않고 `SCREEN_FILTER_KEYS` 3개만 `.get()` 하는지 **내용이
    아니라 접근 방식 자체**를 증명한다 — 이것이 이 수정을 채택하는 실제 근거다.
    """
    from collections.abc import Mapping as ABCMapping

    class _IterationForbiddenMapping(ABCMapping):
        def __init__(self, data: dict[str, str]) -> None:
            self._data = data

        def __getitem__(self, key):  # noqa: ANN001
            return self._data[key]

        def __len__(self) -> int:
            return len(self._data)

        def __iter__(self):
            raise AssertionError(
                "raw_filters 가 순회됐다 — SCREEN_FILTER_KEYS 3개만 .get() 으로 직접 "
                "조회해야 한다(13차 리뷰, .items()/for 순회 금지)"
            )

    poisoned = _IterationForbiddenMapping({"status": "판매중", "sort": "최신순", "page": "1"})
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(screen={"pageType": "chat", "filters": poisoned})
    )
    assert parsed.screen is not None
    assert parsed.screen.filters == {"status": "판매중", "sort": "최신순", "page": "1"}


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
    # pageType 은 필터 드롭 로직과 무관하지만 구매자 요청이므로 구매자 어휘를 쓴다(11차 리뷰
    # 이후 역할 밖 pageType 은 screen 전체가 무시된다).
    parsed = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "cart",
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


async def test_option_product_name_from_recommendation_reaches_reask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise CartOptionRequired(
            [CartOption(option_id=1, name="블랙"), CartOption(option_id=2, name="화이트")]
        )

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="이어폰 담아줘", threadId="t-option-product-reco")
    )
    product_name = "추" * 41
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(await _thread_key(request, _member()), [(101, product_name)])
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 101, "quantity": 1}})

    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token == (
        f"**상품:** {'추' * 40}…\n\n"
        "옵션을 선택해 주세요:\n"
        "1. **블랙**\n"
        "2. **화이트**\n"
        "어떤 걸로 담을까요?"
    )


async def test_option_product_name_from_screen_reaches_reask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise CartOptionRequired(
            [CartOption(option_id=1, name="블랙"), CartOption(option_id=2, name="화이트")]
        )

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="신상품 담아줘",
            threadId="t-option-product-screen",
            screen={
                "pageType": "chat",
                "products": [{"productId": 555, "name": "현재 화면 상품"}],
            },
        )
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 555, "quantity": 1}})

    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token.startswith("**상품:** 현재 화면 상품\n\n옵션을 선택해 주세요:")


async def test_option_product_name_from_screen_overrides_recommendation_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise CartOptionRequired(
            [CartOption(option_id=1, name="블랙"), CartOption(option_id=2, name="화이트")]
        )

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="지금 상품 담아줘",
            threadId="t-option-product-screen-priority",
            screen={
                "pageType": "chat",
                "products": [{"productId": 555, "name": "현재 화면명"}],
            },
        )
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(await _thread_key(request, _member()), [(555, "예전 추천명")])
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 555, "quantity": 1}})

    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    token = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert token.startswith("**상품:** 현재 화면명\n\n옵션을 선택해 주세요:")
    assert "예전 추천명" not in token


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
    # pageType 은 정제 로직과 무관하지만 구매자 요청이므로 구매자 어휘를 쓴다(11차 리뷰 이후
    # 역할 밖 pageType 은 screen 전체가 무시된다).
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            screen={
                "pageType": "cart",
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


async def test_wishlist_add_ordinal_reference_is_resolved_by_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """재현·수정 확인 — 화면 순번 해소(`resolve_screen_reference`)는 `cart_add` 분기에만
    연결돼 있었다. decompose 가 발화를 곧바로 `wishlist_add` 로 분류하면(2선 방어를 거치지
    않는 경로) 해소를 못 받아 `cart.productId` 가 LLM 의 원시 산출(엉뚱한 이웃 상품)로 그대로
    새는 문제가 있었다 — cart_add 분기가 쓰는 것과 같은 해소 결과를 wishlist_add 도 받아야
    한다."""
    import app.services.spring_client as sc

    added: list[int] = []

    async def fake_add_wishlist(req):  # noqa: ANN001
        added.append(req.product_id)
        return WishlistAddResult(success=True, product_id=req.product_id)

    monkeypatch.setattr(sc, "add_wishlist", fake_add_wishlist)

    products = [{"productId": 500 + i, "name": f"상품{i}"} for i in range(1, 10)]
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="3번째 거 찜해줘",
            threadId="t-screen-wishlist-ordinal",
            screen={"pageType": "chat", "columns": 3, "products": products},
        )
    )
    # LLM 은 엉뚱한 이웃(509)을 골랐다고 가정 — 코드가 화면 순번(3번째 = 503)으로 덮어써야 한다.
    llm = FakeLLM(decompose={"intent": "wishlist_add", "cart": {"productId": 509, "quantity": 1}})
    await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert added == [503]


async def test_cart_remove_corrected_to_wishlist_remove_resolves_screen_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 3 리뷰 F9] 재현·수정 확인 — decompose 가 `cart_remove` 로 오분류한 찜 해제
    발화가 `wishlist_remove` 로 정정될 때, 그 정정이 화면 순번 해소를 **거친 뒤**에 일어나야
    한다. 정정을 화면 해소보다 뒤(옛 `cart_remove` 분기 안)에서 하면, 화면 3열+찜 2건 상황에서
    `"3번째 거 찜에서 빼줘"` 가 사용자가 가리킨 항목(화면 3번째 = 503)이 아니라 decompose 가
    문맥에서 잘못 고른 원시 productId(509)를 지운다 — 이 테스트는 `test_wishlist_add_ordinal_
    reference_is_resolved_by_code` 와 같은 부류(화면 순번 해소가 모든 도착 경로에 연결돼야
    한다)를 `cart_remove`→`wishlist_remove` 정정 경로에 대해 직접 잰다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[
                WishlistItem(product_id=503, name="화면 3번째 상품", purchase_state="AVAILABLE"),
                WishlistItem(
                    product_id=509, name="decompose 오추출 상품", purchase_state="AVAILABLE"
                ),
            ]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    products = [{"productId": 500 + i, "name": f"상품{i}"} for i in range(1, 10)]
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="3번째 거 찜에서 빼줘",
            threadId="t-screen-cart-remove-corrected",
            screen={"pageType": "chat", "columns": 3, "products": products},
        )
    )
    # decompose 는 cart_remove 로 오분류하면서 문맥에서 엉뚱한 이웃(509)을 골랐다고 가정 —
    # 화면 순번(3번째 = 503)으로 코드가 덮어써야 한다.
    llm = FakeLLM(decompose={"intent": "cart_remove", "cart": {"productId": 509}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == [503]
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "WISHLIST_REMOVED"


async def test_cart_remove_corrected_to_wishlist_remove_screen_refusal_blocks_auto_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 4 리뷰 F12] 재현·수정 확인 — 화면 해소기가 순번을 **확정하지 못하고
    거부**했으면(`ordinal_out_of_range`, 화면 3건인데 `"99번째"`) 정정 경로도 그 거부를
    `_resolve_wishlist_remove_target` 규칙 2·3 까지 전달해야 한다. 안 그러면 찜이 1건일 때
    규칙 3(목록 1건 자동)이 해소기의 거부를 모른 채 그 1건을 다시 골라 삭제한다 — 사용자가
    가리키지 못한 화면 순번인데도 찜이 지워지는 파괴적 동작이다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    products = [{"productId": 500 + i, "name": f"상품{i}"} for i in range(1, 4)]
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="99번째 거 찜에서 빼줘",
            threadId="t-screen-cart-remove-refused",
            screen={"pageType": "chat", "columns": 3, "products": products},
        )
    )
    # decompose 는 cart_remove 로 오분류하면서 찜에 있는 그 항목(77)을 productId 로 골랐다고
    # 가정한다 — 화면 순번 해소는 3건뿐인 화면에서 99번째를 확정하지 못하고 거부해야 한다.
    llm = FakeLLM(decompose={"intent": "cart_remove", "cart": {"productId": 77}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == []
    assert [e["type"] for e in events][-2:] == ["token", "done"]


async def test_wishlist_remove_screen_refusal_blocks_auto_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 4 리뷰 F12] 같은 구멍이 **정정 경로가 아닌 기존 `wishlist_remove` 분기**
    에도 있었다 — decompose 가 곧장 `wishlist_remove` 를 내도 화면 해소 블록은 그 intent 를
    이미 포함하고 있어(`decision.intent in ("cart_add", "wishlist_add", "wishlist_remove")`)
    같은 거부가 난다. 두 경로 모두 고쳐야 한다는 게 이 리뷰의 핵심이라, 이 분기를 직접 잰다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        raise AssertionError("화면 해소기가 거부했는데 remove_wishlist_fn 이 호출됐다")

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    products = [{"productId": 500 + i, "name": f"상품{i}"} for i in range(1, 4)]
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="99번째 거 찜에서 빼줘",
            threadId="t-screen-wishlist-remove-refused",
            screen={"pageType": "chat", "columns": 3, "products": products},
        )
    )
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {"productId": 77}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert [e["type"] for e in events][-2:] == ["token", "done"]


async def test_wishlist_remove_screen_deictic_single_candidate_still_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 13 리뷰 F33 "추가"] `screen_deictic_markers`(`"이거"`) 경로의 종단 동작이
    F29 이후 실측으로만 확인됐고 고정하는 테스트가 없었다 — 여기서 못 박는다.

    화면에 상품 1건(501)만 있으면 `resolve_screen_reference` 의 (4) 규칙이 그 1건으로
    확정한다(`screen_resolved=True`) — 하지만 그 501 은 **찜 목록과 무관한 다른 상품**이다.
    규칙 3(목록 1건 자동)은 `screen_reference_attempted` 이면 해소 성공 여부와 무관하게
    건너뛴다(라운드 10 리뷰 F27) — 그래서 찜이 77 하나뿐이어도 그 77 을 대신 지우면 안 되고,
    사용자가 화면에서 가리킨 501 은 애초에 찜 목록에 없으니 되물어야 한다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        raise AssertionError("찜 목록에 없는 화면 후보를 대신 지우면 안 된다")

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="이거 찜에서 빼줘",
            threadId="t-screen-wishlist-deictic-single",
            screen={"pageType": "chat", "products": [{"productId": 501, "name": "상품1"}]},
        )
    )
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert [e["type"] for e in events][-2:] == ["token", "done"]
    assert not any(e["type"] == "action" for e in events)


async def test_wishlist_remove_screen_deictic_ambiguous_candidates_still_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 13 리뷰 F33 "추가"] 화면 후보가 3건(모호)이면 `resolve_screen_reference`
    의 (4) 규칙이 `ambiguous_screen_candidates` 로 거부한다(`screen_resolved=False`) — 규칙 3은
    `screen_reference_attempted` 만으로 이미 건너뛰므로 결과는 위 단일 후보 케이스와 같아야
    한다(0회 삭제·되물음), 화면 해소가 성공했든 실패했든 결과가 같다는 것 자체가 F27 이 지키는
    성질이다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        raise AssertionError("화면 후보가 모호한데 찜 항목을 대신 지우면 안 된다")

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    products = [{"productId": 500 + i, "name": f"상품{i}"} for i in range(1, 4)]
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="이거 찜에서 빼줘",
            threadId="t-screen-wishlist-deictic-ambiguous",
            screen={"pageType": "chat", "products": products},
        )
    )
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert [e["type"] for e in events][-2:] == ["token", "done"]
    assert not any(e["type"] == "action" for e in events)


async def test_wishlist_remove_screen_deictic_without_screen_still_auto_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 13 리뷰 F33 "추가"] 화면 자체가 없으면(`screen=None`) `mentions_screen_
    reference` 가 `screen is not None` 가드에 걸려 `False` 를 낸다 — `screen_reference_
    attempted=False` 라 규칙 3(목록 1건 자동)이 정상 동작해 찜 1건이 실제로 삭제돼야 한다.
    위 두 화면 케이스와 짝을 이루는 무회귀 대조군이다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=10, name="이어폰", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="이거 찜에서 빼줘", threadId="t-screen-wishlist-deictic-no-screen")
    )
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == [10]
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "WISHLIST_REMOVED"


async def test_pending_turn_screen_refused_still_blocks_wishlist_auto_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 6 리뷰 F18] 재현·수정 확인 — 옵션 되물음(pending) 중에는
    `screen_context_active=False` 라 `resolve_screen_reference` 자체가 안 돌아 `screen_reason`
    이 `None` 으로 남는다. `screen_refused` 를 `screen_reason is not None` 으로만 계산하면 그
    `None` 이 "거부 없음"으로 오독돼, 화면에 3건뿐인데 `"99번째 거 찜에서 빼줘"` 가 pending
    상태에서는 되물음 없이 찜 1건을 삭제했다(pending 이라는 무관한 상태가 안전 신호를 없앤
    재현). `screen_refused` 계산에 `not screen_context_active` 를 포함시켜 막는다 — 이 발화는
    decompose 가 `cart_add` 로 오분류해 2선 방어(F15)를 함께 태운다.

    **[라운드 8 리뷰 F21 → 라운드 9 리뷰 F25 대체]** 계산식이 이제 `mentions_screen_position
    (message) and not screen_resolved` 다(`screen_resolved` = 화면 해소 블록이 실제로 상품을
    확정했는지의 직접 결과, `cart_intent.product_id` 는 더 이상 안 본다 — F25 문단 참조).
    이 발화("99번째 거")는 위치를 실제로 언급하고, pending 이라 해소 블록 자체가 안 돌아
    `screen_resolved` 가 초기값 `False` 그대로라 여전히 막힌다 — 대신 `"찜한 거 빼줘"`(위치
    미언급) 는 pending 이어도 더 이상 막히지 않는다(F18 의 과대 차단, 아래
    `test_pending_turn_wishlist_remove_without_screen_position_is_not_blocked` 참조).
    decompose 가 이 발화에 우연히 진짜 찜 항목과 같은 `productId=77` 을 냈다는 점도 중요하다 —
    `screen_resolved` 는 그 id 를 전혀 참조하지 않으므로(해소기가 안 돌았다는 사실만 본다)
    F21 이 겪은 "미해소 id 를 확정으로 오인" 함정 자체가 이 설계에서는 성립하지 않는다."""
    import app.services.spring_client as sc
    from app.agents.buyer.cart.state import PendingAdd, get_cart_store
    from app.schemas.spring import CartOption, WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    products = [{"productId": 500 + i, "name": f"상품{i}"} for i in range(1, 4)]
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="99번째 거 찜에서 빼줘",
            threadId="t-screen-pending-refused",
            screen={"pageType": "chat", "columns": 3, "products": products},
        )
    )
    key = await _thread_key(request, _member())
    store = await get_cart_store()
    await store.set_pending(
        key,
        PendingAdd(
            product_id=9001,
            quantity=1,
            options=[
                CartOption(option_id=1001, name="일반형"),
                CartOption(option_id=1002, name="드럼형"),
            ],
        ),
    )

    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 77}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == []
    assert [e["type"] for e in events][-2:] == ["token", "done"]


async def test_pending_turn_wishlist_remove_without_screen_position_is_not_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 8 리뷰 F21] 회귀 복구 확인 — F18 의 대리값 계산(`screen_reason is not None
    or not screen_context_active`)은 pending + 화면이 있기만 하면 발화가 화면을 **전혀 가리키지
    않아도** `screen_refused=True` 였다. 그래서 옵션 되물음(pending) 중에 이 이슈의 핵심 양성
    `"찜한 거 빼줘"`(위치·순번을 전혀 언급하지 않음)까지 되물음으로 퇴화했다(실측 재현). F21 은
    `mentions_screen_position(message)` 을 먼저 보므로 위치를 안 가리킨 이 발화는 pending 이든
    아니든 `screen_refused=False` 라 규칙 3(목록 1건 자동)이 정상 동작해야 한다."""
    import app.services.spring_client as sc
    from app.agents.buyer.cart.state import PendingAdd, get_cart_store
    from app.schemas.spring import CartOption, WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    products = [{"productId": 500 + i, "name": f"상품{i}"} for i in range(1, 4)]
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="찜한 거 빼줘",
            threadId="t-screen-pending-not-refused",
            screen={"pageType": "chat", "columns": 3, "products": products},
        )
    )
    key = await _thread_key(request, _member())
    store = await get_cart_store()
    await store.set_pending(
        key,
        PendingAdd(
            product_id=9001,
            quantity=1,
            options=[
                CartOption(option_id=1001, name="일반형"),
                CartOption(option_id=1002, name="드럼형"),
            ],
        ),
    )

    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == [77]
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "WISHLIST_REMOVED"


async def test_empty_screen_products_still_blocks_an_out_of_range_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 8 리뷰 F21] 과소 차단 재현·수정 확인 — F18 의 `bool(screen and screen.
    products)` 는 `screen.products == []` 면 첫 항부터 `False` 라, `"99번째 거 찜에서 빼줘"`
    처럼 명백히 확정 불가능한 위치 지시도 거부 신호 없이 통과해 찜 1건이 삭제됐다(실측 재현).
    F21 은 화면 존재가 아니라 `mentions_screen_position(message)` 를 직접 보므로 화면이
    비어 있어도(pending 이 아니라 resolve_screen_reference 자체가 `products` 가드로 안 도는
    경우) 위치 지시를 시도했는데 확정할 productId 가 없으면 막아야 한다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="99번째 거 찜에서 빼줘",
            threadId="t-screen-empty-products",
            screen={"pageType": "chat", "columns": 3, "products": []},
        )
    )
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == []
    assert [e["type"] for e in events][-2:] == ["token", "done"]


async def test_resolved_screen_position_absent_from_wishlist_does_not_fall_back_to_the_only_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 10 리뷰 F27] 재현·수정 확인 — 화면 3번째가 503 으로 **정확히 해소**됐는데
    찜 목록엔 77(마우스) 하나뿐이면, 옛 `screen_refused`(파생값)는 "위치를 가리켰고 해소도
    성공했으니 거부 아님" 이라 `False` 가 되어 규칙 3(목록 1건 자동)이 그 무관한 77 을
    지웠다(재현, 파괴적 — `screen_resolved=True` 를 "fallback 허용"으로 오독한 것). F27 은
    규칙 3을 `screen_position_mentioned` 만으로 게이트한다 — 위치를 지목한 이상 해소 성공
    여부와 무관하게 "마침 목록에 하나 있으니 그걸로" 대체하지 않는다. 사용자가 가리킨 503 이
    찜 목록에 없으니 되물어야 한다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    products = [{"productId": 501, "name": "상품1"}, {"productId": 502, "name": "상품2"}]
    products.append({"productId": 503, "name": "상품3"})
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="3번째 거 찜에서 빼줘",
            threadId="t-screen-resolved-not-in-wishlist",
            screen={"pageType": "chat", "columns": 3, "products": products},
        )
    )
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == []
    assert [e["type"] for e in events][-2:] == ["token", "done"]


async def test_column_first_screen_reference_with_llm_id_still_blocks_auto_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 9 리뷰 F25] 재현·수정 확인 — `screen_refused` 의 둘째 항이 `cart_intent.
    product_id is None` 이면(라운드 8 리뷰 F21) 해소기가 확정한 id 와 decompose 가 낸 id 를
    구분하지 못한다. `"2번째 열 3번째 거 찜에서 빼줘"` 는 `_COLUMN_FIRST` 에 걸려 해소기가
    **양보**한다(`resolve_screen_reference` 가 `None` 을 돌려준다 — "열" 표기는 계약·프롬프트
    밖이라 아예 해소하지 않는다) — 그러면 `cart_intent.product_id` 는 decompose 가 문맥에서
    고른 원시 id(우연히 진짜 찜 항목과 같은 77) 그대로 남는다. F21 식은 이 경우 `product_id
    is None` 이 `False` 라 `screen_refused` 가 사라져 규칙 2(문맥 id)가 그 id 를 "확정된
    문맥 id"로 오인해 지웠다(재현). F25 는 `screen_resolved`(해소기가 실제로 상품을
    확정했는가 그 자체)를 쓴다 — 해소기가 양보했으면 `resolved is None` 이라 `screen_resolved`
    는 초기값 `False` 그대로 남고, `cart_intent.product_id` 는 이 판정에 아예 관여하지
    않는다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    products = [{"productId": 500 + i, "name": f"상품{i}"} for i in range(1, 10)]
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="2번째 열 3번째 거 찜에서 빼줘",
            threadId="t-screen-column-first-llm-id",
            screen={"pageType": "chat", "columns": 3, "products": products},
        )
    )
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {"productId": 77}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == []
    assert [e["type"] for e in events][-2:] == ["token", "done"]


async def test_wishlist_remove_without_screen_still_auto_resolves_single_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 4 리뷰 F12] 무회귀 — 화면이 아예 없으면 `screen_refused` 는 기본값
    `False` 라 규칙 3(목록 1건 자동)이 그대로 동작해야 한다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="찜한 거 빼줘", threadId="t-wishlist-remove-no-screen")
    )
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == [77]
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "WISHLIST_REMOVED"


async def test_cart_add_delegated_wishlist_remove_screen_refusal_blocks_auto_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 5 리뷰 F15] 재현·수정 확인 — decompose 가 `cart_add` 로 오분류한 찜 해제
    발화가 `cart/graph.py::stream_cart_add` 의 2선 방어로 `stream_wishlist_remove` 에 위임될
    때도, 화면 해소기의 확정 거부(`ordinal_out_of_range`)가 `screen_refused` 로 전달돼야 한다.
    라운드 4(F12)는 `buyer/graph.py` 의 정정 경로와 기존 `wishlist_remove` 분기만 고치고 이
    2선 방어 경로를 빠뜨렸다 — 하필 이 이슈의 원래 거짓음성 경로(decompose → cart_add → 2선
    방어)라 실제로 파괴적이었다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    products = [{"productId": 500 + i, "name": f"상품{i}"} for i in range(1, 4)]
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="99번째 거 찜에서 빼줘",
            threadId="t-screen-cart-add-delegated-refused",
            screen={"pageType": "chat", "columns": 3, "products": products},
        )
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 77}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == []
    assert [e["type"] for e in events][-2:] == ["token", "done"]


async def test_cart_add_delegated_wishlist_remove_without_screen_still_auto_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 5 리뷰 F15] 무회귀 — 화면이 없으면 2선 방어 위임도 `screen_refused=False`
    기본값이라 규칙 3(목록 1건 자동)이 그대로 동작한다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="찜한 거 빼줘", threadId="t-cart-add-delegated-no-screen")
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == [77]
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "WISHLIST_REMOVED"


async def test_ordinary_cart_remove_does_not_trigger_screen_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#440 라운드 3 리뷰 F9] 무회귀 — decompose 가 산출한 `cart_remove` 가 찜 해제로 정정되지
    않는(평범한 장바구니 삭제) 발화는 화면 순번 해소를 여전히 받지 않는다. 화면 해소 블록
    조건은 `corrected_to_wishlist_remove` 일 때만 열린다 — 이 조건이 무너지면
    `resolve_screen_reference` 가 모든 `cart_remove` 발화에 대해 도는 것으로 새기 때문에,
    호출 자체가 없었는지 직접 잰다."""
    import app.agents.buyer.graph as graph_module
    import app.services.spring_client as sc
    from app.schemas.spring import CartView, CartViewItem

    def fail_resolve_screen_reference(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("평범한 cart_remove 인데 resolve_screen_reference 가 호출됐다")

    monkeypatch.setattr(graph_module, "resolve_screen_reference", fail_resolve_screen_reference)

    async def fake_get_cart(*, user_id=None, guest_id=None):
        return CartView(
            items=[CartViewItem(cart_item_id=1, product_id=501, product_name="키보드", quantity=1)]
        )

    async def fake_delete_cart_item(cart_item_id, *, user_id=None, guest_id=None):
        assert cart_item_id == 1
        return None

    monkeypatch.setattr(sc, "get_cart", fake_get_cart)
    monkeypatch.setattr(sc, "delete_cart_item", fake_delete_cart_item)

    products = [{"productId": 500 + i, "name": f"상품{i}"} for i in range(1, 10)]
    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="키보드 빼줘",
            threadId="t-screen-cart-remove-no-correction",
            screen={"pageType": "chat", "columns": 3, "products": products},
        )
    )
    llm = FakeLLM(decompose={"intent": "cart_remove", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_REMOVED"


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

    settings = get_settings()
    for message in ("이거 담아줘", "3번째 거 담아줘", "3번째 줄 2번째 담아줘", "301 담아줘"):
        assert (
            resolve_screen_reference(
                message,
                products=[],
                columns=3,
                allowed_product_ids={101},
                deictic_markers=settings.screen_deictic_markers,
                context_reference_markers=settings.screen_context_reference_markers,
                last_recommendation_products=[],
                positional_order_verified=True,
                name_confirmation_enabled=False,
                negation_markers=settings.utterance_negation_markers,
                prefix_negation_markers=settings.utterance_prefix_negation_markers,
            )
            is None
        )


def test_screen_reference_leaves_name_and_unmatched_utterances_to_the_llm() -> None:
    """이름 매칭은 LLM 이 8/8 로 잘한다 — 코드가 가로채지 않는다."""
    from app.agents.buyer.screen_reference import resolve_screen_reference

    settings = get_settings()
    products = [(501, "코튼 러그"), (502, "라탄 바구니")]
    kwargs = {
        "products": products,
        "columns": 2,
        "allowed_product_ids": {501, 502},
        "deictic_markers": settings.screen_deictic_markers,
        "context_reference_markers": settings.screen_context_reference_markers,
        "last_recommendation_products": [],
        "positional_order_verified": True,
        "name_confirmation_enabled": False,
        "negation_markers": settings.utterance_negation_markers,
        "prefix_negation_markers": settings.utterance_prefix_negation_markers,
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
        last_recommendation_products=[],
        positional_order_verified=True,
        name_confirmation_enabled=False,
        negation_markers=settings.utterance_negation_markers,
        prefix_negation_markers=settings.utterance_prefix_negation_markers,
    )
    assert out_of_range is not None and out_of_range.product_id is None
    no_columns = resolve_screen_reference(
        "2번째 줄 1번째 담아줘",
        products=products,
        columns=None,
        allowed_product_ids=set(),
        deictic_markers=markers,
        context_reference_markers=context_markers,
        last_recommendation_products=[],
        positional_order_verified=True,
        name_confirmation_enabled=False,
        negation_markers=settings.utterance_negation_markers,
        prefix_negation_markers=settings.utterance_prefix_negation_markers,
    )
    assert no_columns is not None and no_columns.product_id is None


@pytest.mark.parametrize(
    "message",
    [
        "두 번째 옵션으로 담아줘",
        "두 번째 열 세 번째 상품 담아줘",
    ],
)
def test_structured_grid_reference_requires_explicit_row_first_evidence(message: str) -> None:
    """LLM JSON만으로 좌표를 만들지 않고 원문에 행 우선 지시가 있을 때만 소비한다."""
    from app.agents.buyer.recommendation.state import ScreenReference
    from app.agents.buyer.screen_reference import resolve_screen_reference

    settings = get_settings()
    resolution = resolve_screen_reference(
        message,
        products=[(501, "코튼 러그"), (502, "라탄 바구니"), (503, "무선 이어폰")],
        columns=2,
        allowed_product_ids={501, 502, 503},
        deictic_markers=settings.screen_deictic_markers,
        context_reference_markers=settings.screen_context_reference_markers,
        last_recommendation_products=[],
        positional_order_verified=True,
        name_confirmation_enabled=False,
        negation_markers=settings.utterance_negation_markers,
        prefix_negation_markers=settings.utterance_prefix_negation_markers,
        structured_reference=ScreenReference(kind="grid", row=1, column=2),
    )

    assert resolution is None


# ─────────── 라운드 3 — 리뷰 지적 회귀 가드 ───────────


def _resolve(
    message: str,
    products,
    columns=3,
    allowed=None,
    last_reco=(),
    *,
    positional_order_verified=True,
    name_confirmation_enabled=False,
):
    """프로덕션 해소기를 config 기본값 그대로 호출한다(기본값 자체가 이번 수정의 일부다).

    [#571] 새 키워드 인자 둘의 기본값은 **화면 표면과 같은 동작**이다(순번 항상 켜짐·이름
    확정 꺼짐) — 이 파일의 기존(화면 표면) 호출부가 전부 무회귀로 남는다.
    """
    from app.agents.buyer.screen_reference import resolve_screen_reference

    settings = get_settings()
    return resolve_screen_reference(
        message,
        products=products,
        columns=columns,
        allowed_product_ids=allowed if allowed is not None else {pid for pid, _ in products},
        deictic_markers=settings.screen_deictic_markers,
        context_reference_markers=settings.screen_context_reference_markers,
        last_recommendation_products=last_reco,
        positional_order_verified=positional_order_verified,
        name_confirmation_enabled=name_confirmation_enabled,
        negation_markers=settings.utterance_negation_markers,
        prefix_negation_markers=settings.utterance_prefix_negation_markers,
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


def test_a_name_known_only_from_last_reco_also_beats_a_positional_number() -> None:
    """[Claude 리뷰 6차, F-8] 이름이 `screen.products` 가 아니라 `last_reco` 에만 있어도 통한다.

    화면은 (501,"러그")·(502,"바구니") 뿐이고 사용자가 지목한 "무선 이어폰"(9001)은 **직전
    추천에만** 있다. 담기 허용 목록은 `last_reco ∪ screen.products` 이고 decompose 프롬프트에도
    두 블록이 다 실리는데, 이름 검사가 화면 상품만 보면 (B) 가 발동하지 않아 `"2번째"` 가 순번으로
    읽혀 화면 2번째 상품(바구니, 502)으로 override 된다 — decompose 는 9001 을 옳게 뽑았을
    것이다. F-2 와 같은 클래스의 오담기가 이름의 출처만 바뀌어 재발한 것이라 코드가 개입하지
    않아야 한다(None, LLM 산출 존중).
    """
    products = [(501, "러그"), (502, "바구니")]
    last_reco = [(9001, "무선 이어폰")]
    assert (
        _resolve("무선 이어폰 2번째 옵션으로 담아줘", products, columns=2, last_reco=last_reco)
        is None
    )
    # 대조군 — 이름 지목이 **없으면** 순번은 여전히 화면 기준으로 해소된다(회귀 금지).
    three = [(501, "러그"), (502, "바구니"), (503, "쿠션")]
    resolved = _resolve("3번째 거 담아줘", three, columns=3, last_reco=last_reco)
    assert resolved is not None and resolved.product_id == 503
    # 기존 (B) 케이스 — 이름이 `screen.products` 에 있을 때도 그대로 동작한다(회귀 금지).
    assert (
        _resolve("바구니 2번째 옵션으로 담아줘", products, columns=2, last_reco=last_reco) is None
    )


@pytest.mark.parametrize(
    "message",
    [
        "10만원대 무선 이어폰 담아줘",  # 숫자 바로 뒤가 `만` 이라 접미 목록으로는 못 막았다
        "2026년형 TV 담아줘",
        "128GB 모델 담아줘",
        "2024년형 담아줘",
        "55인치 담아줘",
        "500ml 담아줘",
        # [PR 리뷰] 단위가 **띄어쓰인** 경우 — 2판("앞뒤에 문자가 붙지 않은 토큰")이 놓쳤다.
        "3000 원짜리",
        "3000 원짜리 담아줘",
        "10 만원대",
        "128 GB 모델",
        "12 개월 할부로",
        "30 개 담아줘",
    ],
)
def test_measurements_and_years_are_not_mistaken_for_product_ids(message: str) -> None:
    """[리뷰 F-3 · PR 리뷰 후속] 가격·연도·용량의 숫자를 상품 id 로 오인해 **정상 발화가 막혔다**.

    방향은 안전(오담기가 아니라 되물음)하지만 `"10만원대 무선 이어폰 담아줘"` 는 흔한 발화다.
    붙여 쓴 경우는 "앞뒤에 문자가 붙지 않은 토큰"으로 막았는데, **띄어 쓰면 그대로 샜다** —
    `"3000 원짜리"` 가 그 예다. 이제 숫자 뒤가 **담기 동사이거나 문장 끝일 때만** id 후보로 본다.
    """
    products = [(501, "러그"), (502, "바구니")]
    assert _resolve(message, products, columns=2) is None


@pytest.mark.parametrize("message", ["301 담아줘", "301", "301담아줘", "301 넣어줘"])
def test_a_standalone_unknown_id_still_forces_a_reask(message: str) -> None:
    """F-3 을 좁히면서 원래 목적은 지켜야 한다 — 두 목록 밖 id 는 여전히 되물음이다.

    **`"301 담아줘"` 는 이 규칙이 존재하는 이유 그 자체다** — #118 의 보안 논거가 "LLM 이 발화
    속 임의 숫자를 오추출해 추천 안 된 상품을 담는 것을 차단"이고, 프로브에서 screen 주입 후
    1/8·6/8 로 깎였던 셀을 이 규칙이 8/8 로 되돌렸다. PR 리뷰가 제안한 수정
    (`(?!\\s*[0-9A-Za-z가-힣])` 로 배제를 넓히기)은 숫자 뒤가 공백+한글인 이 발화까지 함께
    배제해 **규칙을 죽인다** — 그래서 배제를 넓히는 대신 허용 목록으로 뒤집었다.
    """
    products = [(501, "러그"), (502, "바구니")]
    resolved = _resolve(message, products, columns=2)
    assert resolved is not None and resolved.product_id is None
    assert resolved.reason == "unknown_product_id_spoken"


@pytest.mark.parametrize("message", ["301번 담아줘", "302번 담아줘", "301번", "301번담아줘"])
def test_an_unknown_id_with_a_bare_beon_suffix_still_forces_a_reask(message: str) -> None:
    """[Claude 리뷰 14차, F-16] `"번"` 접미(순서수사 `"번째"`가 아니라 상품 번호 표기)가 붙어도
    같은 가드가 걸려야 한다.

    수정 전 재현: `"301 담아줘"` → `_BARE_NUMBER` `['301']` → 되물음(정상). 그런데
    `"301번 담아줘"`·`"302번 담아줘"` → `_BARE_NUMBER` `[]` → `None`(가드 미발동) — 한국어에서
    상품 번호에 `"번"` 을 붙이는 표기가 오히려 더 흔한데, 그 표기만으로 두 목록 밖 id 가드가
    통째로 빠지고 LLM 오추출이 그대로 담긴다(F-3 류 오담기 재발).
    """
    products = [(501, "러그"), (502, "바구니")]
    resolved = _resolve(message, products, columns=2)
    assert resolved is not None and resolved.product_id is None
    assert resolved.reason == "unknown_product_id_spoken"


def test_ordinal_beonjjae_is_not_swallowed_by_the_new_bare_beon_suffix() -> None:
    """[Claude 리뷰 14차, F-16] `"번째"`(순서수사)는 새로 허용한 `"번"` 접미와 겹치면 안 된다.

    `_ORDINAL` 이 `_BARE_NUMBER` 보다 먼저 검사되므로 `"3번째 거 담아줘"` 는 순번으로 풀려야
    하고, 화면 밖 id 되물음(`unknown_product_id_spoken`)으로 새면 회귀다.
    """
    products = [(501, "러그"), (502, "바구니"), (503, "쿠션")]
    resolved = _resolve("3번째 거 담아줘", products, columns=3)
    assert resolved is not None and resolved.product_id == 503
    assert resolved.reason == "ordinal"


def test_ambiguous_two_digit_beon_never_self_confirms() -> None:
    """[Claude 리뷰 14차, F-16] `"10번 담아줘"` 처럼 순번(10번째)인지 id 인지 애매한 두 자리
    입력도 스스로 확정하지 않는다 — 그래서 어느 해석으로 읽어도 오담기로 이어질 경로가 없다.

    (3) 절은 토큰이 `allowed_product_ids` **밖**일 때만 되물음을 반환하고, 안에 있으면 아무 것도
    하지 않고 다음 규칙(맨 지시대명사 등)에 넘긴다.
    """
    products = [(501, "러그"), (502, "바구니")]
    # 10 이 허용 목록 밖이면 되묻는다.
    resolved = _resolve("10번 담아줘", products, columns=2)
    assert resolved is not None and resolved.product_id is None
    assert resolved.reason == "unknown_product_id_spoken"
    # 10 이 마침 허용 목록 **안**이면 이 규칙은 침묵한다(스스로 확정하지 않음).
    assert _resolve("10번 담아줘", products, columns=2, allowed={501, 502, 10}) is None


def test_a_known_id_and_a_non_cart_context_are_left_to_the_llm() -> None:
    """목록 안 id 는 막지 않고, 담기 지목이 아닌 숫자는 애초에 규칙이 발동하지 않는다."""
    products = [(501, "러그"), (502, "바구니")]
    assert _resolve("501 담아줘", products, columns=2) is None
    # 화이트리스트는 **놓치는 쪽으로 기운다** — 못 잡으면 LLM 산출이 그대로 남을 뿐이라
    # (규칙 도입 전 동작), 정상 발화를 막는 오탐보다 안전한 방향이다.
    assert _resolve("301 상품 담아줘", products, columns=2) is None


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

    default = _unresolved_notice(None, False)
    assert default == "어떤 상품을 담을까요? 추천을 먼저 받아보시면 담아드릴게요."
    assert _unresolved_notice("some_future_reason", False) == default
    assert _unresolved_notice("ambiguous_screen_candidates", False) != default


def test_unresolved_notice_has_last_reco_changes_the_default_only() -> None:
    """[#435 W4] `has_last_reco` 는 `screen_reason` 이 없을 때만 문구를 바꾼다.

    화면 해소 사유가 있으면(순번/좌표 모호·미발견) 그 문구가 **우선**한다 — `last_reco` 유무와
    무관하게 그대로다. `screen_reason` 이 없을 때만 "이미 추천을 받았다"는 사실을 반영한다.
    """
    from app.agents.buyer.cart.graph import _unresolved_notice

    assert (
        _unresolved_notice(None, False)
        == "어떤 상품을 담을까요? 추천을 먼저 받아보시면 담아드릴게요."
    )
    assert (
        _unresolved_notice(None, True)
        == "어떤 상품을 담을까요? 추천해 드린 상품 중에서 이름을 말씀해 주시면 담아드릴게요."
    )
    for screen_reason in ("ambiguous_screen_candidates", "unknown_product_id_spoken"):
        assert _unresolved_notice(screen_reason, False) == _unresolved_notice(screen_reason, True)


def test_unresolved_notice_screen_reason_precedes_push_failed_marker() -> None:
    """[#468 I-21] #118 화면 해소 사유는 push 실패 마커가 있어도 가장 구체적인 안내로 남는다."""
    from app.agents.buyer.cart.graph import _unresolved_notice

    for screen_reason in ("ambiguous_screen_candidates", "unknown_product_id_spoken"):
        assert _unresolved_notice(screen_reason, False, has_push_failed=True) == _unresolved_notice(
            screen_reason, False
        )


# ─────────── PR 2차 리뷰 — 좌표 축 반전 (열 = column) ───────────


def test_row_coordinates_resolve_and_column_coordinates_defer_to_the_llm() -> None:
    """[PR 2차 리뷰] `열`(column)을 행 표지로 읽어 **축이 뒤집혔다**.

    `"2번째 열 3번째"` 는 col=2·row=3 이므로 정본 산술로 index 7(8번째 상품)인데, 초판 정규식이
    `줄|행|열` 을 모두 첫 숫자(행)의 표지로 삼아 row=2·col=3 → index 5 를 확정했다. 그 id 는
    화면 목록 **안**이라 담기 가드가 막지 못한다(오담기).

    정본 §3.1 과 `_SCREEN_CART_RULE` 이 가르치는 어휘는 `줄`(row)·`칸`(column) 뿐이고 `열` 은
    어디에도 없어, 지원하는 대신 **해소를 건너뛰어 LLM 산출을 세운다**. `_COORD` 에서 `열` 만
    빼는 것으로는 부족하다 — 남은 `"2번째"` 를 순번 규칙이 가로채 또 다른 상품을 확정한다.
    """
    products = [(3100 + i, f"상품{i}") for i in range(1, 10)]  # 9건 × columns=3

    # 줄(행) 표기는 그대로 해소된다 — index (3-1)*3+(2-1) = 7 → 8번째 상품.
    for message in ("3번째 줄 2번째 담아줘", "3행 2번째 담아줘", "3줄 2칸 담아줘"):
        resolved = _resolve(message, products, columns=3)
        assert resolved is not None, message
        assert resolved.product_id == 3108, message
        assert resolved.reason == "coordinate"

    # 열(칸) 기준 표기는 코드가 개입하지 않는다 — 축을 뒤집어 확정하지도, 순번으로 오독하지도 않는다.
    for message in ("2번째 열 3번째 담아줘", "2열 3번째 담아줘", "2번째 열 담아줘"):
        assert _resolve(message, products, columns=3) is None, message


def test_column_marker_yield_does_not_suppress_unrelated_utterances() -> None:
    """양보 (C)가 좌표와 무관한 발화까지 삼키지 않는지 — 삼켜도 오늘 동작과 같아야 한다."""
    products = [(3101, "코튼 러그"), (3102, "라탄 바구니")]

    # `열` 이 없는 발화는 종전대로 해소된다.
    resolved = _resolve("2번째 거 담아줘", products, columns=2)
    assert resolved is not None and resolved.product_id == 3102

    # 상품명에 섞인 `열`(예: "10 열쇠고리")은 원래도 어떤 규칙에도 안 걸렸으므로 None 그대로다.
    assert _resolve("10 열쇠고리 담아줘", products, columns=2) is None


def test_coord_regex_treats_only_row_markers_as_the_first_axis() -> None:
    """`_COORD` 자체의 의미를 고정한다 — 첫 숫자의 표지는 `줄`·`행` 뿐이다.

    위 양보 (C)가 `열` 발화를 먼저 걷어내므로 `_COORD` 의 `열` 분기는 **도달 불가**다. 그래서
    행위 테스트만으로는 이 정규식이 되돌아가도 빨간불이 안 뜬다(실제로 확인했다). 다음 사람이
    (C)를 지우는 순간 축 반전이 조용히 되살아나지 않도록 정규식 의미를 직접 못박는다.
    """
    from app.agents.buyer.screen_reference import _COORD

    assert _COORD.search("3번째 줄 2번째").groups() == ("3", "2")
    assert _COORD.search("3행 2번째").groups() == ("3", "2")
    assert _COORD.search("3줄 2칸").groups() == ("3", "2")
    # `열` 은 column 이라 첫 숫자의 표지가 될 수 없다.
    assert _COORD.search("2번째 열 3번째") is None
    assert _COORD.search("2열 3번째") is None


# ─────────── Claude 리뷰 7차 — F-9 좌표 오인 (두 번째 숫자 접미사 생략) ───────────


def test_second_number_without_a_coordinate_suffix_is_not_a_coordinate() -> None:
    """[Claude 리뷰 7차, F-9] 두 번째 숫자 뒤 접미사가 없어도 좌표로 확정되면 **오담기**다.

    `"3줄 2단 정리함 담아줘"` 는 "2단"이 상품 설명(단 수납장 몇 단)이지 좌표 지시가 아닌데,
    초판 정규식은 두 번째 숫자의 접미사(`번째|번|칸`)를 선택으로 둬 "숫자 + 줄/행 + 숫자"만
    있으면 좌표로 읽었다(`("3","2")` → 화면 8번째 상품 확정, 실제 재현). `"2줄 3인용 소파
    담아줘"` 도 "3인용"이 소파 설명이지 좌표가 아닌데 같은 방식으로 샜다(`("2","3")`). 둘 다
    사용자가 좌표를 말한 적 없는데 화면 목록 **안**의 엉뚱한 상품이 확정되는 오담기라
    F-1/F-2/F-7 과 같은 클래스다 — 이 함수는 개입하지 않아야 한다(None, LLM 산출 존중).
    """
    products = [(3100 + i, f"상품{i}") for i in range(1, 10)]  # 9건 × columns=3
    for message in ("3줄 2단 정리함 담아줘", "2줄 3인용 소파 담아줘"):
        assert _resolve(message, products, columns=3) is None, message


def test_coordinate_suffix_requirement_does_not_regress_valid_coordinate_utterances() -> None:
    """대조군 — 두 번째 숫자에 접미사가 **있는** 정상 좌표 발화는 그대로 해소된다(회귀 금지)."""
    products = [(3100 + i, f"상품{i}") for i in range(1, 10)]  # 9건 × columns=3
    for message in ("3번째 줄 2번째 담아줘", "3줄 2칸 담아줘", "3행 2번째 담아줘"):
        resolved = _resolve(message, products, columns=3)
        assert resolved is not None and resolved.product_id == 3108, message
        assert resolved.reason == "coordinate", message


def test_coord_regex_requires_a_suffix_on_the_second_number_only() -> None:
    """`_COORD` 자체의 의미를 고정한다 — 첫 숫자의 `번째` 는 선택, 두 번째 숫자의 접미사는 필수다.

    다음 사람이 이 요구사항을 다시 선택으로 되돌리는 순간 F-9 가 조용히 재발하지 않도록
    정규식 의미를 직접 못박는다(`test_coord_regex_treats_only_row_markers_as_the_first_axis`
    와 같은 이유).
    """
    from app.agents.buyer.screen_reference import _COORD

    # 첫 숫자는 접미사가 없어도 매칭된다(`"3줄 2칸"`).
    assert _COORD.search("3줄 2칸").groups() == ("3", "2")
    # 두 번째 숫자는 접미사(`번째|번|칸`)가 없으면 매칭되지 않는다.
    assert _COORD.search("3줄 2단 정리함") is None
    assert _COORD.search("2줄 3인용 소파") is None


# ─────────── Claude 리뷰 8차 — F-11 줄(row)만 말하고 칸을 안 말한 경우 ───────────


def test_row_only_utterance_is_a_reask_not_a_positional_number() -> None:
    """[Claude 리뷰 8차, F-11] 줄만 말하고 칸을 안 말하면 `_COORD` 가 실패하고, 아래 (2) 순번이
    그 숫자를 **배열 순번**으로 잡아 실제 그 줄과 무관한 상품을 확정한다.

    columns=3 인 9건 화면에서 `"3번째 줄"` 의 실제 대상은 index 6~8(7~9번째 상품)인데, 순번으로
    새면 `"3"` 이 배열 3번째(index 2)로 읽혀 **완전히 다른 상품**이 확정된다(실제 재현). 사용자가
    행을 말했는데 순번으로 해석하는 것은 F-1/F-2/F-7 이 막은 것과 같은 클래스의 오담기라, 이
    함수는 확정하지 말고 되물음으로 보내야 한다(columns 없는 좌표와 같은 사유).
    """
    products = [(3100 + i, f"상품{i}") for i in range(1, 10)]  # 9건 × columns=3
    for message in ("3번째 줄에 있는 거 담아줘", "2번째 줄 상품 담아줘"):
        resolved = _resolve(message, products, columns=3)
        assert resolved is not None, message
        assert resolved.product_id is None, message
        assert resolved.reason == "coordinate_without_columns", message


def test_row_only_reask_does_not_regress_valid_coordinates_or_ordinals() -> None:
    """대조군 — 정상 좌표·정상 지시대명사·F-9 의 비좌표 발화는 이 변경으로 영향받지 않는다."""
    products = [(3100 + i, f"상품{i}") for i in range(1, 10)]  # 9건 × columns=3

    # 정상 좌표: 칸까지 말했으면 종전대로 `_COORD` 가 먼저 해소한다.
    resolved = _resolve("3번째 줄 2번째 담아줘", products, columns=3)
    assert resolved is not None and resolved.product_id == 3108
    assert resolved.reason == "coordinate"

    # 순번만 말했으면(줄|행 언급 없음) 종전대로 배열 순번이 해소한다.
    resolved = _resolve("3번째 거 담아줘", products, columns=3)
    assert resolved is not None and resolved.product_id == 3103
    assert resolved.reason == "ordinal"

    # F-9: 두 번째 숫자가 있지만 좌표 접미사가 아닌 경우는 여전히 LLM 산출을 존중한다(None) —
    # `_ROW_ONLY` 가 이 케이스까지 삼키면 F-9 가 조용히 재발한다.
    for message in ("3줄 2단 정리함 담아줘", "2줄 3인용 소파 담아줘"):
        assert _resolve(message, products, columns=3) is None, message

    # 정상 지시대명사(화면 1건)는 종전대로 해소된다.
    single = [(9001, "무선 이어폰")]
    resolved = _resolve("이거 담아줘", single, columns=1)
    assert resolved is not None and resolved.product_id == 9001


def test_row_only_regex_requires_a_number_word_but_tolerates_unrelated_trailing_digits() -> None:
    """`_ROW_ONLY` 자체의 의미를 고정한다(F-14 로 갱신됨).

    [9차 리뷰, F-14 이전] 초판은 "줄|행 뒤에 숫자가 **전혀 없을 때만**" 매칭했다. 그런데 그
    조건은 F-9(`"3줄 2단"`)를 막는 데는 맞았지만, `"3번째 줄 5000원 넘는거 담아줘"`처럼 줄 뒤에
    **좌표가 아닌** 숫자(가격·수량)가 있는 발화까지 함께 매칭을 포기시켜 F-11 이 막으려던
    오담기가 그대로 재발했다(실제 재현) — `_COORD`도 실패(접미사 없음)·`_ROW_ONLY`도 실패(숫자
    있음) → 아래 (2) 순번이 "3"을 배열 순번으로 잡는다.

    [F-14 갱신] 그래서 두 조건으로 나눴다: ① 첫 숫자에 `번째`가 붙어 있어야 하고(F-9 의 맨
    "3줄"·"2줄"은 제외), ② 뒤에 **유효한 좌표 접미사를 동반한 숫자만** 없으면 매칭한다(좌표가
    아닌 숫자는 매칭을 막지 않는다).
    """
    from app.agents.buyer.screen_reference import _ROW_ONLY

    # 순번을 명시한(`번째`) 행 지시 + 뒤에 좌표 아닌 숫자 — F-14 가 새로 잡아야 하는 케이스.
    assert _ROW_ONLY.search("3번째 줄 5000원 넘는거 담아줘") is not None
    assert _ROW_ONLY.search("3번째 줄에 5개 담아줘") is not None
    # 뒤에 숫자가 아예 없는 F-11 원 케이스도 여전히 잡는다.
    assert _ROW_ONLY.search("3번째 줄에 있는 거 담아줘") is not None
    assert _ROW_ONLY.search("2번째 줄 상품 담아줘") is not None
    # 뒤에 **유효한 좌표**(숫자+접미사)가 있으면 `_COORD` 가 해소할 몫이라 매칭하지 않는다.
    assert _ROW_ONLY.search("3번째 줄 2번째") is None
    # F-9: `번째` 가 없는 맨 "숫자+줄|행"은 상품 설명일 수 있어 여전히 매칭하지 않는다.
    assert _ROW_ONLY.search("3줄 2단 정리함") is None
    assert _ROW_ONLY.search("2줄 3인용 소파") is None


# ─────────── Claude 리뷰 9차 — F-14 F-11 의 "뒤에 숫자 없음" 조건이 만든 구멍 ───────────


def test_row_only_reask_survives_a_trailing_non_coordinate_number() -> None:
    """[Claude 리뷰 9차, F-14] "번째 줄" 뒤에 **좌표가 아닌** 숫자(가격·수량)가 와도 되물음이다.

    F-11 초판은 "줄|행 뒤에 숫자가 전혀 없을 때만" `_ROW_ONLY` 를 매칭시켰다. `"3번째 줄 5000원
    넘는거 담아줘"`·`"3번째 줄에 5개 담아줘"` 는 줄 뒤에 숫자(5000·5)가 있어 그 조건에 걸려
    `_ROW_ONLY` 가 포기했고, `_COORD` 도 두 번째 숫자 접미사가 없어 실패해 아래 (2) 순번이 "3"을
    **배열 순번**으로 잡았다(columns=3 인 9건 화면에서 실제 3번째 줄과 무관한 상품 확정, 실제
    재현) — F-1/F-2/F-7/F-11 과 같은 클래스의 오담기다.
    """
    products = [(3100 + i, f"상품{i}") for i in range(1, 10)]  # 9건 × columns=3
    for message in ("3번째 줄 5000원 넘는거 담아줘", "3번째 줄에 5개 담아줘"):
        resolved = _resolve(message, products, columns=3)
        assert resolved is not None, message
        assert resolved.product_id is None, message
        assert resolved.reason == "coordinate_without_columns", message


def test_row_only_trailing_number_fix_does_not_regress_f9_or_valid_coordinates() -> None:
    """대조군 — F-14 수정이 F-9(비좌표 숫자 설명)·정상 좌표·정상 순번을 다시 깨지 않는다.

    9차 리뷰가 제안한 단순 교체(`(?!\\s*(?:에\\s*)?\\d+\\s*(?:번째|번|칸))` 로만 바꾸기)는 F-9 를
    되살렸다 — "2단"·"3인용"도 "숫자 뒤에 좌표 접미사가 없다"를 그대로 만족해 되물음으로 샜다
    (직접 검증해 확인, 그래서 채택하지 않았다). 이 테스트가 그 회귀를 고정한다.
    """
    products = [(3100 + i, f"상품{i}") for i in range(1, 10)]  # 9건 × columns=3

    # F-9: 두 번째 숫자가 있지만 좌표 접미사가 아니고 첫 숫자에 `번째` 도 없는 경우는
    # 여전히 LLM 산출을 존중한다(None).
    for message in ("3줄 2단 정리함 담아줘", "2줄 3인용 소파 담아줘"):
        assert _resolve(message, products, columns=3) is None, message

    # 정상 좌표: 칸까지 말했으면 종전대로 `_COORD` 가 먼저 해소한다.
    resolved = _resolve("3번째 줄 2번째 담아줘", products, columns=3)
    assert resolved is not None and resolved.product_id == 3108
    assert resolved.reason == "coordinate"
    resolved = _resolve("3줄 2칸 담아줘", products, columns=3)
    assert resolved is not None and resolved.product_id == 3108
    resolved = _resolve("3행 2번째 담아줘", products, columns=3)
    assert resolved is not None and resolved.product_id == 3108

    # 줄|행 언급이 아예 없는 순번 발화는 종전대로 배열 순번이 해소한다.
    resolved = _resolve("3번째 거 담아줘", products, columns=3)
    assert resolved is not None and resolved.product_id == 3103
    assert resolved.reason == "ordinal"


# ─────────── Claude 리뷰 8차 — F-12 1글자 지시대명사 표지의 부분일치 오탐 ───────────


def test_a_single_character_deictic_marker_would_false_positive_on_unrelated_words() -> None:
    """[Claude 리뷰 8차, F-12] `"얘"`(1글자)를 표지로 두면 `"얘기"`·`"얘들아"` 에 부분일치한다.

    이 매칭은 포함 관계(부분 문자열)라 조사·활용을 흡수하도록 설계됐다(config 주석) — 표지가
    2글자 이상이면 우연 일치가 드물지만, `"얘"` 만은 1글자라 무관한 단어에 걸린다. 실제로
    화면 후보가 1건이면 `"얘기했던 걸로 담아줘"`(대화 맥락 참조, 화면 지시가 아님)가
    **되물음 없이 확정**됐다 — `context_reference_markers`(`"아까"`·`"저번"` 등)에도 안 걸리기
    때문이다. `screen_deictic_markers` 에서 `"얘"` 를 뺀 뒤에는 이 함수가 개입하지 않는다(None).
    """
    fake_markers = ["이거", "이것", "요거", "요것", "저거", "저것", "얘"]
    products = [(555, "러그")]
    assert (
        _resolve(
            "얘기했던 걸로 담아줘",
            products,
            columns=1,
            allowed={555},
        )
        is None
    )  # 대조: 현재 config(아래) 로는 개입하지 않는다 — 아래 with_markers 로 옛 동작을 재현한다.

    def _resolve_with_markers(message: str, markers: list[str]):
        from app.agents.buyer.screen_reference import resolve_screen_reference

        settings = get_settings()
        return resolve_screen_reference(
            message,
            products=products,
            columns=1,
            allowed_product_ids={555},
            deictic_markers=markers,
            context_reference_markers=settings.screen_context_reference_markers,
            last_recommendation_products=[],
            positional_order_verified=True,
            name_confirmation_enabled=False,
            negation_markers=settings.utterance_negation_markers,
            prefix_negation_markers=settings.utterance_prefix_negation_markers,
        )

    # 옛 표지 목록(`"얘"` 포함)으로는 대화 맥락 참조 발화가 화면 상품으로 **오확정**됐다(재현).
    for message in ("얘기했던 걸로 담아줘", "얘들아 담아줘"):
        resolved = _resolve_with_markers(message, fake_markers)
        assert resolved is not None and resolved.product_id == 555, message


def test_screen_deictic_markers_no_longer_include_the_single_character_marker() -> None:
    """config `screen_deictic_markers` 에 1글자 표지가 없어야 한다 — 다른 표지는 그대로다."""
    markers = get_settings().screen_deictic_markers
    assert "얘" not in markers
    assert all(len(marker) >= 2 for marker in markers)
    # 나머지 표지의 기존 동작은 바뀌지 않는다(회귀 금지).
    for marker in ("이거", "이것", "요거", "요것", "저거", "저것"):
        assert marker in markers


# ─────────── PR 5차 리뷰 — 되물음 턴의 allowed 게이트 ───────────


async def test_pending_turn_blocks_a_screen_id_and_keeps_the_reask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[PR 5차 리뷰] 되물음 턴에는 `screen.products` 를 `allowed` 에 합류시키지 않는다.

    합류시켜 두면 LLM 이 **발화 속 숫자를 오추출**했는데 그 값이 마침 화면 상품 id 와 같을 때
    `cart.product_id in allowed` 가 참이 되어 `stream_cart_add` 의 전환 조건을 통과한다 →
    진행 중이던 옵션 되물음이 조용히 버려지고 답한 적 없는 상품이 담긴다. 실제로 재현했다
    (`"502 그램짜리로 할게"` → 502 담김·pending 소멸).

    정본 §3.1 [보안] 문단이 스스로 밝힌 목적이 "LLM 이 발화 속 임의 숫자를 오추출해 담는 것을
    막는 기존 가드는 유지된다"이므로, 이 게이트는 문면과 어긋나되 **그 목적을 지키는** 방향이다.
    """
    import app.services.spring_client as sc
    from app.agents.buyer.cart.state import PendingAdd, get_cart_store
    from app.schemas.spring import CartOption
    from app.services.spring_client import CartOptionRequired

    attempted: list[int] = []

    async def fake_add(req):  # noqa: ANN001
        attempted.append(req.product_id)
        if req.option_id is None:  # 실제 I-2 동작 — 옵션 없이 담으면 되물음
            # 후보 2개 — 1개면 #114 자동 선택이 걸려 되물음이 아니라 담기로 끝난다.
            raise CartOptionRequired(
                [
                    CartOption(option_id=1001, name="일반형"),
                    CartOption(option_id=1002, name="드럼형"),
                ]
            )
        return AddToCartResult(success=True, cart_item_id=1)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="502 그램짜리로 할게",
            threadId="t-pending-allowed",
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
    key = await _thread_key(request, _member())
    store = await get_cart_store()
    await store.set_last_reco(key, [(9001, "드럼용 세탁 세제")])
    await store.set_pending(
        key,
        PendingAdd(
            product_id=9001,
            quantity=1,
            options=[
                CartOption(option_id=1001, name="일반형"),
                CartOption(option_id=1002, name="드럼형"),
            ],
        ),
    )

    # LLM 오추출 — 발화 속 502 를 productId 로 뽑았다.
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 502, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    # 오추출한 화면 상품은 **담기 시도조차 되지 않는다**(전환 조건을 통과하지 못한다).
    assert 502 not in attempted
    assert attempted == [9001]  # 되물음 대상 상품으로만 재시도한다
    assert "action" not in [e["type"] for e in events]  # CART_ADDED 없음 — 되물음으로 흐른다
    # **되물음이 살아 있어야 한다** — 전환으로 오인돼 폐기되면 사용자는 답할 대상을 잃는다.
    assert await store.get_pending(key) is not None


async def test_non_pending_turn_still_unions_screen_products_into_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """되물음이 아닌 턴의 합집합은 그대로다 — 이번 게이트가 정본 동작을 끄지 않았다."""
    import app.services.spring_client as sc
    from app.agents.buyer.cart.state import get_cart_store

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=55)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="바구니 담아줘",
            threadId="t-nonpending-allowed",
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
    await (await get_cart_store()).set_last_reco(
        await _thread_key(request, _member()), [(9001, "드럼용 세탁 세제")]
    )

    # 502 는 직전 추천에 없고 **screen.products 에만** 있다 — 합집합이 살아 있어야 담긴다.
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 502, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 502


async def test_pending_turn_still_allows_a_previously_recommended_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """게이트가 **정상 전환 경로를 닫지 않는다** — 직전 추천 상품은 되물음 중에도 그대로 허용된다.

    화면 상품이 동시에 직전 추천이면 `last_reco` 쪽으로 allowed 에 남는다는 근거의 실측판이다.
    """
    import app.services.spring_client as sc
    from app.agents.buyer.cart.state import PendingAdd, get_cart_store
    from app.schemas.spring import CartOption

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=66)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="다른 거 담아줘",
            threadId="t-pending-lastreco",
            screen={"pageType": "chat", "products": [{"productId": 502, "name": "바구니"}]},
        )
    )
    key = await _thread_key(request, _member())
    store = await get_cart_store()
    # 202 는 직전 추천 — screen 에는 없다.
    await store.set_last_reco(key, [(9001, "드럼용 세탁 세제"), (202, "무선 이어폰")])
    await store.set_pending(
        key,
        PendingAdd(
            product_id=9001,
            quantity=1,
            options=[CartOption(option_id=1001, name="일반형")],
        ),
    )

    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 202, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 202  # 직전 추천 경유 전환은 그대로 동작한다


# ─────── #571 추천 카드 표면 ───────
#
# 추천 카드(CH-5)만 뜬 턴 — `screen` 없이 `last_reco[:turn_count]` 만 있다. 이슈 #571 전에는
# `screen_reference.resolve_screen_reference` 가 아예 호출되지 않아, 순번·"이거"·이름 지목 같은
# 결정적으로 풀리는 입력이 LLM 산출에 그대로 맡겨져 오담기가 났다(screen_reference.py 상단 F-17
# ~F-19 참조). 아래는 그 게이트 확대·`ordinal_span` 증명 게이트·이름 확정 규칙(N)을 고정한다.


async def test_reco_card_sole_candidate_is_confirmed_by_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-1] 추천 카드 1건 + "이거 담아줘" → 코드가 그 카드로 확정한다(화면 표면과 같은 규칙)."""
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=201)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="이거 담아줘", threadId="t-reco-sole")
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(await _thread_key(request, _member()), [(555, "신상품")])
    # LLM 은 추천 카드에 없는 다른 id 를 골랐다고 둔다(할루시네이션 흉내).
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 999, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 555  # LLM 산출(999) 이 아니라 추천 카드의 유일 후보


async def test_reco_cards_multiple_candidates_force_a_reask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-2] 추천 카드 3건 + "이거 담아줘" → action 없음(되물음), 문구는 "화면에 보이는 상품 중"."""
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"후보 다건에서 임의 확정이 담기까지 도달하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="이거 담아줘", threadId="t-reco-ambiguous")
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(
        await _thread_key(request, _member()),
        [(501, "러그"), (502, "바구니"), (503, "가습기")],
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 501, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert "action" not in [e["type"] for e in events]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "화면에 보이는 상품 중" in token_text


async def test_reco_card_ordinal_resolves_when_span_matches_turn_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-3] 추천 카드 5건(단일 목록, ordinal_span=5) + "3번째 거 담아줘" → 3번째 카드가 담긴다."""
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=301)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="3번째 거 담아줘", threadId="t-reco-ordinal")
    )
    cart_store = await get_cart_store()
    items = [(600 + i, f"상품{i}") for i in range(5)]
    await cart_store.set_last_reco(await _thread_key(request, _member()), items, ordinal_span=5)
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 999, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == items[2][0]


async def test_reco_card_coordinate_uses_chat_screen_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """추천 카드와 chat 화면 열 수를 결합해 2행 3열을 전체 6번째로 해소한다."""
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=302)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="2번째 줄 3번째 상품 담아줘",
            threadId="t-reco-coordinate",
            screen={"pageType": "chat", "columns": 3},
        )
    )
    cart_store = await get_cart_store()
    items = [(700 + i, f"상품{i}") for i in range(8)]
    await cart_store.set_last_reco(await _thread_key(request, _member()), items, ordinal_span=8)
    llm = FakeLLM(
        decompose={"intent": "cart_add", "cart": {"productId": items[2][0], "quantity": 1}}
    )

    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == items[5][0]


async def test_structured_grid_reference_resolves_korean_coordinate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM은 한글 수사를 행·열로만 구조화하고 서버가 전체 5번째 추천 ID를 계산한다."""
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=303)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="두번째 줄 두번째 상품 담아줘",
            threadId="t-reco-structured-coordinate",
            screen={"pageType": "chat", "columns": 3},
        )
    )
    cart_store = await get_cart_store()
    items = [(900 + i, f"상품{i}") for i in range(5)]
    await cart_store.set_last_reco(await _thread_key(request, _member()), items, ordinal_span=5)
    llm = FakeLLM(
        decompose={
            "intent": "cart_add",
            # LLM의 직접 상품 선택은 일부러 전체 2번째로 틀리게 둔다.
            "cart": {"productId": items[1][0], "quantity": 1},
            "screenReference": {"kind": "grid", "row": 2, "column": 2},
        }
    )

    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == items[4][0]


@pytest.mark.parametrize(
    ("thread_id", "screen", "ordinal_span", "reference"),
    [
        (
            "t-reco-structured-invalid",
            {"pageType": "chat", "columns": 3},
            5,
            {"kind": "grid", "row": 0, "column": 2},
        ),
        (
            "t-reco-structured-unverified",
            {"pageType": "chat", "columns": 3},
            None,
            {"kind": "grid", "row": 2, "column": 2},
        ),
        (
            "t-reco-structured-no-columns",
            {"pageType": "chat"},
            5,
            {"kind": "grid", "row": 2, "column": 2},
        ),
        (
            "t-reco-structured-out-of-range",
            {"pageType": "chat", "columns": 3},
            5,
            {"kind": "grid", "row": 3, "column": 3},
        ),
        (
            "t-reco-structured-omitted",
            {"pageType": "chat", "columns": 3},
            5,
            None,
        ),
    ],
)
async def test_structured_grid_reference_reasks_when_position_is_not_safe(
    monkeypatch: pytest.MonkeyPatch,
    thread_id: str,
    screen: dict,
    ordinal_span: int | None,
    reference: dict | None,
) -> None:
    """좌표 주장만 있고 계산 전제가 부족하면 허용 목록 안의 LLM productId도 담지 않는다."""
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"검증되지 않은 구조화 좌표가 담기까지 도달하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="두 번째 줄 두 번째 상품 담아줘",
            threadId=thread_id,
            screen=screen,
        )
    )
    cart_store = await get_cart_store()
    items = [(950 + i, f"상품{i}") for i in range(5)]
    await cart_store.set_last_reco(
        await _thread_key(request, _member()),
        items,
        **({"ordinal_span": ordinal_span} if ordinal_span is not None else {}),
    )
    llm = FakeLLM(
        decompose={
            "intent": "cart_add",
            "cart": {"productId": items[1][0], "quantity": 1},
            "screenReference": reference,
        }
    )

    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    assert "action" not in [e["type"] for e in events]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "화면에 보이는 상품 중" in token_text


async def test_reco_card_coordinate_reasks_when_card_order_is_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """추천 카드의 화면 순서를 증명할 수 없으면 columns가 있어도 좌표를 확정하지 않는다."""
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"검증되지 않은 추천 좌표가 담기까지 도달하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="2번째 줄 3번째 상품 담아줘",
            threadId="t-reco-coordinate-unverified",
            screen={"pageType": "chat", "columns": 3},
        )
    )
    cart_store = await get_cart_store()
    items = [(800 + i, f"상품{i}") for i in range(8)]
    await cart_store.set_last_reco(await _thread_key(request, _member()), items)
    llm = FakeLLM(
        decompose={"intent": "cart_add", "cart": {"productId": items[2][0], "quantity": 1}}
    )

    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    assert "action" not in [e["type"] for e in events]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "화면에 보이는 상품 중" in token_text


async def test_reco_card_ordinal_out_of_range_reasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#571-4] 같은 조건 + "9번째 거 담아줘" → 되물음(ordinal_out_of_range 문구)."""
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"범위 밖 순번이 담기까지 도달하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="9번째 거 담아줘", threadId="t-reco-ordinal-oor")
    )
    cart_store = await get_cart_store()
    items = [(600 + i, f"상품{i}") for i in range(5)]
    await cart_store.set_last_reco(await _thread_key(request, _member()), items, ordinal_span=5)
    llm = FakeLLM(
        decompose={"intent": "cart_add", "cart": {"productId": items[0][0], "quantity": 1}}
    )
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert "action" not in [e["type"] for e in events]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "화면에 보이는 상품 중" in token_text


async def test_reco_card_ordinal_unverifiable_without_matching_span_reasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-5] 다목록 턴(ordinal_span=0) + "3번째 거 담아줘" → 담기 없음, 문구는 이름 지목 유도
    (`_UNRESOLVED_WITH_RECO` 계열). LLM 이 낸 productId(허용 목록 안)도 담기지 않는다."""
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"증명되지 않은 순번이 담기까지 도달하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="3번째 거 담아줘", threadId="t-reco-ordinal-unverified")
    )
    cart_store = await get_cart_store()
    items = [(700 + i, f"상품{i}") for i in range(5)]
    # ordinal_span 을 넘기지 않으면 0(다목록·BUY_ALL 과 같은 모양)으로 저장된다.
    await cart_store.set_last_reco(await _thread_key(request, _member()), items)
    llm = FakeLLM(
        decompose={"intent": "cart_add", "cart": {"productId": items[2][0], "quantity": 1}}
    )
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert "action" not in [e["type"] for e in events]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "이름을 말씀해 주시면" in token_text


async def test_reco_card_full_name_mention_is_confirmed_by_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-6] 카드 이름 전체가 발화에 있고 1건만 매칭 → 코드가 그 카드로 확정한다."""
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=601)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="무선 블루투스 이어폰 담아줘", threadId="t-reco-name")
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(
        await _thread_key(request, _member()),
        [(801, "무선 블루투스 이어폰"), (802, "파우치")],
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 802, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 801  # LLM 산출(802) 이 아니라 이름이 지목한 카드


async def test_reco_card_unique_name_token_overrides_wrong_llm_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#639] 상품명 일부의 유일 토큰 지목은 목록 안 LLM 오답보다 우선한다.

    운영에서 ``Septwolves 지갑 담아줘``의 정답은 5644였지만, LLM이 같은 허용 목록 안의 5695를
    골라 가드가 막지 못했다. 이 테스트는 LLM이 실제 오답을 내도 Spring 요청에 5644가 실리는
    사용자 관측 결과를 고정한다.
    """
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=639)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="Septwolves 지갑 담아줘", threadId="t-reco-unique-name-token")
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(
        await _thread_key(request, _member()),
        [
            (5656, "지갑 통장지갑 양지사"),
            (4583, "양지사 통장지갑 지갑류 통장정리 통장 동지갑 정리"),
            (5643, "남성 소가죽 반지갑 남자 지갑 카드동전수납 학생지갑"),
            (5642, "남성 지퍼 반지갑 중지갑 명함 카드 동전 지갑 남자"),
            (5644, "W06 남성지갑 명품 천연가죽 남자 반지갑 카드 학생 septwolves"),
            (5654, "에스티 도장케이스 도장집 가죽 도장지갑 파우치 보관함 인감도장 고급"),
            (4570, "지갑 머니클립 카드홀더 다용도 통장지갑 보관 초경량 파우치 가방 케이스"),
            (4560, "한손 소가죽 지퍼 남성 카드지갑 동전"),
            (
                5647,
                "데일리 프리미엄 클래식 남성 가죽 지갑 모던 네추럴 고급 카드홀더 스타일 반지갑 10종",
            ),
            (5645, "남자반지갑,남성,수제,소가죽,미니,슬림,선물,두리공방"),
            (
                5650,
                "남자 반지갑 카드 지폐 동전 중지갑 슬림 고급 미니 학생 얇은 지폐 명품 수제 가죽 PU 남성용 지갑",
            ),
            (5696, "루이까또즈미니지갑 ST2SD11P"),
            (5695, "구찌 썸머블프여성 GG 마몬트 지퍼 장지갑 443123 DTD1T 1000 24FW"),
            (5698, "[명품] 인트레치아토 133945 V0016 8806 블랙 카드지갑"),
        ],
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 5695, "quantity": 1}})

    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 5644


@pytest.mark.parametrize(
    ("message", "expected_product_id"),
    [
        pytest.param("Septwolves 지갑 담아줘", 5644, id="casefolded-unique-token"),
        pytest.param("지갑 담아줘", None, id="common-token"),
        pytest.param("남성 지갑 담아줘", None, id="multiple-common-tokens"),
        pytest.param("Septwolves 구찌 지갑 담아줘", None, id="conflicting-unique-tokens"),
        pytest.param("wolf 지갑 담아줘", None, id="substring-is-not-a-token"),
        pytest.param("1000 지갑 담아줘", None, id="numeric-token"),
        pytest.param("Septwolves 말고 구찌 지갑 담아줘", None, id="negated-token"),
    ],
)
def test_reco_card_unique_name_token_safety_boundaries(
    message: str, expected_product_id: int | None
) -> None:
    """[#639] 정확 토큰·표면 유일성·단일 상품을 모두 만족할 때만 코드가 확정한다."""
    products = [
        (5656, "지갑 통장지갑 양지사"),
        (4583, "양지사 통장지갑 지갑류 통장정리 통장 동지갑 정리"),
        (5643, "남성 소가죽 반지갑 남자 지갑 카드동전수납 학생지갑"),
        (5642, "남성 지퍼 반지갑 중지갑 명함 카드 동전 지갑 남자"),
        (5644, "W06 남성지갑 명품 천연가죽 남자 반지갑 카드 학생 septwolves"),
        (5695, "구찌 썸머블프여성 GG 마몬트 지퍼 장지갑 443123 DTD1T 1000 24FW"),
    ]

    resolved = _resolve(
        message,
        products,
        columns=None,
        positional_order_verified=True,
        name_confirmation_enabled=True,
    )

    if expected_product_id is None:
        assert resolved is None
    else:
        assert resolved is not None
        assert resolved.product_id == expected_product_id
        assert resolved.reason == "screen_unique_name_token_match"


def test_reco_card_command_token_in_a_product_name_is_not_a_selection_signal() -> None:
    """상품명에 지시문 같은 토큰이 있어도 모든 담기 발화가 그 상품을 가리킨 것으로 보지 않는다."""
    products = [
        (5656, "지갑 통장지갑 양지사"),
        (4583, "양지사 지갑 정리"),
        (7777, "담아줘 특별상품"),
    ]

    resolved = _resolve(
        "지갑 담아줘",
        products,
        columns=None,
        positional_order_verified=True,
        name_confirmation_enabled=True,
    )

    assert resolved is None


async def test_reco_card_name_mention_with_negation_defers_to_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-7] 이름 + 부정("… 말고 다른 거 담아줘") → 해소기 None(LLM 산출 그대로)."""
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=701)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="무선 블루투스 이어폰 말고 다른 거 담아줘", threadId="t-reco-name-negated"
        )
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(
        await _thread_key(request, _member()),
        [(801, "무선 블루투스 이어폰"), (802, "파우치")],
    )
    # 부정 표지가 있으므로 코드는 개입하지 않는다 — LLM 이 고른 802(파우치)가 그대로 담긴다.
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 802, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 802


async def test_reco_card_ambiguous_name_match_defers_to_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-8] 이름이 2건 매칭(한 이름이 다른 이름의 부분문자열인 카드 2장) → None(LLM 산출 그대로)."""
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=801)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="이어폰케이스 담아줘", threadId="t-reco-name-ambiguous")
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(
        await _thread_key(request, _member()),
        [(901, "이어폰"), (902, "이어폰케이스")],
    )
    # 두 이름 모두 발화에 부분 문자열로 포함돼 매칭 2건 — 코드는 개입하지 않고 LLM 산출(902)을 세운다.
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 902, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 902


async def test_reco_card_blank_name_is_never_confirmed_by_name_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-9] 이름이 비어 있는 카드(이름 캐시 degrade)는 이름 매칭으로 확정되지 않는다.

    `len(name) >= 2` 가드가 없으면 빈 이름("")도 `"" in message"`가 항상 참이라 매칭에 섞여
    2건 매칭(555·556)이 되고, 그러면 (N)이 개입을 포기해 LLM 산출(555, 빈 이름 카드)이 그대로
    샌다 — 이 테스트는 그 회귀를 잡는다: 코드는 항상 이름이 실제로 있는 556 을 확정해야 한다.
    """
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=901)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="가방 담아줘", threadId="t-reco-name-blank")
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(
        await _thread_key(request, _member()),
        [(555, ""), (556, "가방")],
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 555, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 556


async def test_reco_surface_conversation_reference_is_not_forced_onto_a_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-10] (A) 회귀, 추천 표면: 카드 1건 + "아까 추천해준 이거 담아줘" → None(카드 확정 금지)."""
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=1001)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="아까 추천해준 이거 담아줘", threadId="t-reco-context-ref")
    )
    cart_store = await get_cart_store()
    key = await _thread_key(request, _member())
    # 승계분(직전 턴) — 이번 턴 카드가 아니다.
    await cart_store.set_last_reco(key, [(999, "기존상품")])
    # 이번 턴 카드 1건 — turn_count=1, 승계분(999)이 뒤에 남는다.
    await cart_store.set_last_reco(key, [(555, "러그")])
    # LLM 은 승계분(999, allowed 안이지만 이번 턴 카드는 아니다)을 골랐다고 둔다. (A) 가 화면
    # 확정을 막았다면 이 값이 그대로 담긴다 — 러그(555)가 담기면 대화 참조가 카드로 확정된 것이다.
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 999, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 999


async def test_reco_surface_name_known_only_from_carried_over_reco_beats_positional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-11] (B) 회귀, 추천 표면: 이름이 승계분(last_reco 뒤쪽)에만 있고 이번 턴 카드엔
    없음 + "<그 이름> 2번째 옵션으로 담아줘" → None(순번이 이기지 않는다)."""
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=1101)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="무선 이어폰 2번째 옵션으로 담아줘", threadId="t-reco-name-in-carry")
    )
    cart_store = await get_cart_store()
    key = await _thread_key(request, _member())
    await cart_store.set_last_reco(key, [(9001, "무선 이어폰")])
    await cart_store.set_last_reco(key, [(501, "러그"), (502, "바구니")], ordinal_span=2)
    # 순번이 이겼다면 화면 2번째(바구니, 502)가 확정된다 — (B) 가 이겨야 승계분 9001 이 그대로 산다.
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 9001, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 9001


async def test_pending_turn_reco_cards_do_not_invoke_the_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-12] 되물음 턴 예외: pending + 추천 카드 다건 + "이거 담아줘" → 해소기가 돌지 않는다
    (`screen_context_active` 가 pending 중 거짓이라 화면 표면과 동일하게 구조적으로 막힌다).
    옵션 되물음 흐름이 end-to-end 로 그대로 유지되는지까지 확인한다.
    """
    import app.agents.buyer.graph as buyer_graph
    import app.services.spring_client as sc
    from app.agents.buyer.cart.state import PendingAdd, get_cart_store
    from app.schemas.spring import CartOption
    from app.services.spring_client import CartOptionRequired

    call_count = 0
    real_resolve = buyer_graph.resolve_screen_reference

    def spy(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(buyer_graph, "resolve_screen_reference", spy)

    async def fake_add(req):  # noqa: ANN001
        if req.option_id is None:
            raise CartOptionRequired(
                [
                    CartOption(option_id=1001, name="일반형"),
                    CartOption(option_id=1002, name="드럼형"),
                ]
            )
        return AddToCartResult(success=True, cart_item_id=1201)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="이거 담아줘", threadId="t-reco-pending-noresolve")
    )
    key = await _thread_key(request, _member())
    store = await get_cart_store()
    await store.set_last_reco(key, [(501, "러그"), (502, "바구니"), (503, "가습기")])
    await store.set_pending(
        key,
        PendingAdd(
            product_id=9001,
            quantity=1,
            options=[
                CartOption(option_id=1001, name="일반형"),
                CartOption(option_id=1002, name="드럼형"),
            ],
        ),
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 9001, "optionId": 1002}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))

    assert call_count == 0
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"


async def test_reco_card_turn_boundary_excludes_carried_over_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-13] `turn_count` 경계: 이번 턴 3건 + 승계 2건일 때 "4번째 거 담아줘" → 승계분이
    아니라 범위 밖 되물음."""
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"승계분을 순번으로 확정하면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="4번째 거 담아줘", threadId="t-reco-turn-boundary")
    )
    cart_store = await get_cart_store()
    key = await _thread_key(request, _member())
    await cart_store.set_last_reco(key, [(1001, "승계1"), (1002, "승계2")])
    await cart_store.set_last_reco(
        key, [(2001, "이번1"), (2002, "이번2"), (2003, "이번3")], ordinal_span=3
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 1001, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert "action" not in [e["type"] for e in events]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    assert "화면에 보이는 상품 중" in token_text


def test_reco_resolution_is_always_within_allowed_and_products() -> None:
    """[#571-14, F-d] 오담기 최종 방벽 — 해소 결과 productId 는 항상 allowed 안이고 products
    안이다.

    [F-d] 발화별 **기대 결과를 표로 고정**한다 — 예전 버전은 단언이
    `if resolved is not None and resolved.product_id is not None:` **안**에만 있어, 해소기가
    6개 발화 전부에 `None` 을 돌려줘도(=규칙이 통째로 죽어도) 통과했다(공허한 통과,
    docs/lessons.md 2026-08-11 「판정 라벨만 비교하는 회귀 테스트는 드리프트를 통과시킨다」와
    같은 형태). 이제 각 발화를 confirm(확정, 기대 productId까지 고정)·reask(되물음,
    product_id=None)·defer(양보, resolved 자체가 None) 세 갈래로 명시하고, confirm 케이스가
    **최소 2건**임을 단언한다 — 규칙이 죽어 전부 defer 로 떨어지면 이 최소 건수 단언이 깨진다.
    """
    from app.agents.buyer.screen_reference import resolve_screen_reference

    settings = get_settings()
    products = [(2001, "이번1"), (2002, "이번2"), (2003, "이번3")]
    allowed = {pid for pid, _ in products} | {9001}
    # (발화, 기대 갈래, confirm 이면 기대 productId)
    cases = [
        ("이거 담아줘", "reask", None),  # (4) 맨 지시대명사, 후보 3건 → 되물음
        ("3번째 거 담아줘", "confirm", 2003),  # (2) 순번 — positional_order_verified=True
        ("9번째 거 담아줘", "reask", None),  # (2) 순번, 범위 밖 → 되물음
        ("이번2 담아줘", "confirm", 2002),  # (N) 이름 확정 — 정확히 1건 매칭
        ("9001 담아줘", "defer", None),  # allowed 안이지만 products 밖 id — 확정할 근거가 없어 양보
        ("아까 추천해준 그거 담아줘", "defer", None),  # (A) 대화 맥락 참조 양보
    ]
    confirmed = 0
    for message, expected, expected_pid in cases:
        resolved = resolve_screen_reference(
            message,
            products=products,
            columns=None,
            allowed_product_ids=allowed,
            deictic_markers=settings.screen_deictic_markers,
            context_reference_markers=settings.screen_context_reference_markers,
            last_recommendation_products=(),
            positional_order_verified=True,
            name_confirmation_enabled=True,
            negation_markers=settings.utterance_negation_markers,
            prefix_negation_markers=settings.utterance_prefix_negation_markers,
        )
        if expected == "defer":
            assert resolved is None, f"{message!r}: 양보(None)를 기대했는데 {resolved!r}"
            continue
        assert resolved is not None, f"{message!r}: 규칙이 발동해야 하는데 아무 것도 안 함(None)"
        if expected == "reask":
            assert (
                resolved.product_id is None
            ), f"{message!r}: 되물음을 기대했는데 확정됨: {resolved!r}"
            continue
        assert (
            resolved.product_id == expected_pid
        ), f"{message!r}: productId {expected_pid} 를 기대했는데 {resolved!r}"
        assert resolved.product_id in allowed
        assert resolved.product_id in {pid for pid, _ in products}
        confirmed += 1
    assert confirmed >= 2, "confirm 케이스가 최소 2건은 있어야 이 표가 살아있다는 증거다"


async def test_reco_cards_widen_screen_reference_attempted_blocks_wishlist_auto_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-15] 찜 해제 회귀(결정 5): 추천 카드 3건 + 찜 1건 + "이거 찜에서 빼줘" → 그 찜
    항목이 삭제되지 않는다(규칙 3 자동 삭제 차단) — #440 라운드 10·11 리뷰(F27·F29)가 화면
    표면에서 막은 것과 같은 클래스의 파괴적 동작을 추천 표면에서도 막는다."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        raise AssertionError("추천 카드 참조 시도인데 무관한 찜 항목을 지우면 안 된다")

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="이거 찜에서 빼줘", threadId="t-reco-wishlist-guard")
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(
        await _thread_key(request, _member()),
        [(501, "러그"), (502, "바구니"), (503, "가습기")],
    )
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert [e["type"] for e in events][-2:] == ["token", "done"]
    assert not any(e["type"] == "action" for e in events)


async def test_control_group_without_reco_or_screen_still_auto_deletes_the_only_wishlist_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-15 대조군] 추천 카드도 screen 도 없는 같은 발화는 오늘처럼 삭제된다(#440 의 양성
    경로를 죽이지 않았음을 증명)."""
    import app.services.spring_client as sc
    from app.schemas.spring import WishlistItem, WishlistView

    removed: list[int] = []

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        removed.append(product_id)
        return None

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="이거 찜에서 빼줘", threadId="t-no-reco-wishlist-control")
    )
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert removed == [77]
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "WISHLIST_REMOVED"


async def test_pending_turn_reco_cards_still_block_wishlist_auto_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[F-g] 되물음 턴 + 추천 카드: `surface` 는 `screen_context_active`(옵션 되물음 여부)와
    무관하게 계산되므로, 되물음 진행 중이라 해소기 자체는 돌지 않아도(`screen_context_active`
    가 거짓) `screen_reference_attempted` 는 여전히 참이 된다(추천 카드 3건 + "이거" 지시대명사).
    그래서 pending 턴 + 추천 카드 3건 + `"이거 찜에서 빼줘"` 에서도 규칙 3(목록 1건 자동 삭제)이
    차단되고, 그 찜 항목은 삭제되지 않는다 — 화면 표면(`screen is not None`)이 오늘 이미 같은
    방식으로 동작하는 것과 대칭이라 새 비대칭이 생기지 않는다(findings-draft.md F-g)."""
    import app.services.spring_client as sc
    from app.agents.buyer.cart.state import PendingAdd, get_cart_store
    from app.schemas.spring import CartOption, WishlistItem, WishlistView

    async def fake_get_wishlist(user_id):  # noqa: ANN001
        return WishlistView(
            items=[WishlistItem(product_id=77, name="마우스", purchase_state="AVAILABLE")]
        )

    async def fake_remove_wishlist(product_id, *, user_id=None):  # noqa: ANN001
        raise AssertionError(
            "되물음 턴이라도 추천 카드 참조 시도가 있으면 무관한 찜 항목을 지우면 안 된다"
        )

    monkeypatch.setattr(sc, "get_wishlist", fake_get_wishlist)
    monkeypatch.setattr(sc, "remove_wishlist", fake_remove_wishlist)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="이거 찜에서 빼줘", threadId="t-reco-pending-wishlist-guard")
    )
    key = await _thread_key(request, _member())
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(key, [(501, "러그"), (502, "바구니"), (503, "가습기")])
    # 옵션 되물음(pending) 진행 중 — 무관한 상품(9001)의 옵션 답변을 기다리는 상태다.
    await cart_store.set_pending(
        key,
        PendingAdd(
            product_id=9001,
            quantity=1,
            options=[CartOption(option_id=1001, name="일반형")],
        ),
    )
    llm = FakeLLM(decompose={"intent": "wishlist_remove", "cart": {}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert [e["type"] for e in events][-2:] == ["token", "done"]
    assert not any(e["type"] == "action" for e in events)


async def test_no_screen_and_no_reco_never_invokes_the_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-16] #240 구조적 격리: screen 없음 + last_reco 없음 → 해소기가 아예 호출되지 않는다
    (모듈 patch 로 호출 0회 확인)."""
    import app.agents.buyer.graph as buyer_graph
    import app.services.spring_client as sc

    call_count = 0

    def spy(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return None

    monkeypatch.setattr(buyer_graph, "resolve_screen_reference", spy)

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError("표면이 없는데 담기가 진행되면 안 됨")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(message="이거 담아줘", threadId="t-no-surface")
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 999, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert call_count == 0
    assert "action" not in [e["type"] for e in events]


async def test_screen_products_take_priority_over_reco_cards_for_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#571-17] 화면 표면 우선: `screen.products` 와 추천 카드가 동시에 있으면 순번은
    `screen.products` 기준으로 풀린다(추천 카드가 끼어들지 않는다)."""
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=1701)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="2번째 거 담아줘",
            threadId="t-screen-priority",
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
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(
        await _thread_key(request, _member()),
        [(601, "상품A"), (602, "상품B"), (603, "상품C")],
        ordinal_span=3,
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 999, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 502  # 화면 2번째(추천 카드 2번째 602 가 아니다)


async def test_empty_screen_products_does_not_fall_back_to_reco_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """추천 패널이 아닌 빈 screen은 이전 추천 카드로 폴백하지 않는다.

    ``pageType=chat`` + ``columns``는 추천 패널이 보인다는 양성 신호지만, 빈 검색 화면은
    사용자가 이전 추천을 보고 있다는 증거가 아니다.
    """
    import app.services.spring_client as sc

    async def fake_add(req):  # noqa: ANN001
        raise AssertionError(f"빈 screen.products 턴에 이전 턴 추천 카드가 담기면 안 됨: {req}")

    monkeypatch.setattr(sc, "add_to_cart", fake_add)

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="3번째 거 담아줘",
            threadId="t-screen-empty-products-no-reco-fallback",
            screen={"pageType": "search", "columns": 3, "products": []},
        )
    )
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(
        await _thread_key(request, _member()),
        [(601, "상품A"), (602, "상품B"), (603, "상품C")],
        ordinal_span=3,
    )
    # LLM 은 두 목록 밖 id 를 냈다고 둔다 — surface 가 비어 해소기가 안 도는데 이 값이 allowed
    # 밖이라 가드도 막아야 한다(코드가 C 로 override 하지 않는 것과 별개로 이중 확인).
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 999, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    assert "action" not in [e["type"] for e in events]
    token_text = next(e for e in events if e["type"] == "token")["data"]["text"]
    # 해소기가 아예 안 돌아 `screen_reason` 이 None 으로 남고, `has_last_reco=True` 라
    # `_unresolved_notice` 가 이름 지목 유도 문구로 떨어진다(화면 위치 재질문 문구가 아니다 —
    # 이 턴은 화면 순번을 시도할 대상 자체가 없다).
    assert "이름을 말씀해 주시면" in token_text


async def test_control_group_no_screen_field_still_resolves_reco_card_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[F-20 대조군] `screen` 필드 자체가 없는(요청에 `screen` 이 아예 안 실린) 같은 발화는
    여전히 추천 카드 3번째가 담긴다 — #571 본래 기능(추천 표면 순번 해소)이 이 수정으로
    죽지 않았음을 증명한다."""
    import app.services.spring_client as sc

    added: dict = {}

    async def fake_add(req):  # noqa: ANN001
        added["product_id"] = req.product_id
        return AddToCartResult(success=True, cart_item_id=2001)

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", _empty_cart_view())

    request = BuyerChatRequest.model_validate(
        _buyer_payload(
            message="3번째 거 담아줘",
            threadId="t-no-screen-field-reco-ordinal-control",
        )
    )
    assert request.screen is None  # screen 자체가 없음을 사전 확인 — 빈 screen 과 다른 케이스다
    cart_store = await get_cart_store()
    await cart_store.set_last_reco(
        await _thread_key(request, _member()),
        [(601, "상품A"), (602, "상품B"), (603, "상품C")],
        ordinal_span=3,
    )
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 999, "quantity": 1}})
    events = await _collect(_run_buyer_turn(request, _member(), llm=llm))
    action = next(e for e in events if e["type"] == "action")["data"]
    assert action["type"] == "CART_ADDED"
    assert added["product_id"] == 603  # 추천 카드 3번째(C) — LLM 산출(999) 아님
