"""Spring 역방향 호출 스키마 (api-spec v0.15.0 §4).

AI → Spring 역방향 계약 (모두 camelCase on the wire, Pydantic alias):
  1. search_products      — I-1, GET /internal/products/search (§4.6, 최우선 C-15; GET/POST 역제안 🔴)
  2. get_recent_purchases — I-19, GET /internal/members/{id}/orders (§4.7, C-6, dedup·프로필 소스)
  3. add_to_cart          — I-2, POST /internal/cart/items 단건 (§4.1, C-3)
  4. get_cart             — I-18, GET /internal/cart (§4.9, C-16)
  5. push_recommendations — I-21, POST /internal/recommendations, lists[] (경로 B, §4.2, C-9)
  6. fetch_product_changes — I-17, GET /internal/products/changes (§4.8, C-4, AI 생성물 배치)
  (I-6/I-9 판매자 콜백 응답 스키마는 C-13/C-14 협의 후 확정 — dict 유지)

Python 속성은 snake_case, 직렬화는 by_alias=True 로 camelCase, 입력은 populate_by_name 으로 양쪽 허용.
[HARD] push 페이로드에는 표시 필드(price/image/reviewCount)를 넣지 않는다 — 추천 산출물만 (§4.2).

[변경 v0.6.0] 장바구니 구계약(CartItem items[] 다건 + reason 4종/GUEST_NOT_ALLOWED) 폐기
→ BE I-2 문서 채택: AddToCartRequest 단건(userId|guestId 본문 신원, optionId), 게스트 허용.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Annotated, Literal, Mapping, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

_log = logging.getLogger(__name__)


class CamelModel(BaseModel):
    """camelCase 직렬화 공통 베이스 (schemas.chat.CamelModel 와 동일 규약)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# ── 1. 상품 검색 (§4.6, C-15) ──


class ProductSearchFilters(CamelModel):
    """AI 검색 명세 (decompose 산출, api-spec §4.6) — 와이어로 나가는 건 subset 뿐이다.

    Spring I-1 요청으로 실제 전송되는 건 `_search_query_params`(spring_client)가 추출하는 와이어
    필드(keyword·category·price·brand)뿐이다. exclude_product_ids·rating_min·limit 은 Spring 에
    보내지 않고 AI 가 사후처리에 쓰는 필드다 — dedup(결정 14-F)·평점 하한은 사후필터,
    limit 은 **AI 후보 상한(rerank 입력 top-K)**. 정렬은 rerank(LLM) 소관이라 별도 필드가 없다(#100 P2).
    excludeProductIds 원천 = GET /orders/recent(§4.7), 게스트는 빈 배열.
    [2026-07-23, BE 합의] size 제거로 limit 은 더 이상 Spring 요청 size 가 아니다(§4.6) — Spring 은
    전량 반환하고, limit 은 search_catalog 가 top-K 절단에 쓴다. [#395, 2026-08-07] 재도입 폐지 확정.
    """

    category: str | None = None
    # 수치 하한/상한은 **음수가 될 수 없다**(PR #248 3차 리뷰). 제약이 없으면 `priceMax=-50000`
    # 같은 값이 검증을 통과해 그대로 Spring 쿼리 파라미터로 나가고, 오류 없이 "조건에 맞는 상품이
    # 없다"는 **조용한 0건**으로만 드러나 원인 추적이 안 된다. 출처는 셋 다 신뢰 경계 밖이다 —
    # decompose(LLM 산출)·저장된 완화 오퍼(pg-profile 왕복)·멀티턴 병합. 값 검증은 여기 한 곳에
    # 모으고 호출부는 스키마에 맡긴다(사전 목록을 각자 두면 스키마보다 좁아져 조용히 어긋난다).
    price_min: int | None = Field(default=None, ge=0)
    price_max: int | None = Field(default=None, ge=0)
    brand: list[str] | None = None
    rating_min: float | None = Field(default=None, ge=0)
    keyword: str | None = None  # 상품명 LIKE — Spring I-1 와이어 파라미터
    # 의미검색용 자연어(#101) — AI 내부 필드. keyword(상품명 LIKE)와 분리되며 Spring 에 안 나가고
    # EmbeddingRerankBackend(방식2)가 pgvector 재정렬의 query 임베딩 입력으로 쓴다(§4.8 방식2).
    semantic_query: str | None = None
    # 색상 조건(#100 P1) — 내부 모델: decompose 가 사용자 발화에서 표기 하나만 뽑아 담는다.
    # 와이어로 나갈 때 app.pipelines.color_synonyms.expand_color 가 동의어 묶음으로 펼쳐
    # 반복 파라미터 리스트가 된다(api-spec §4.6, #258) — 여기 타입이 str 인 건 드리프트가
    # 아니라 의도다. BE 매칭은 attributes 전문 LIKE 가 아니라 json_extract 로 색상 키만
    # 좁힌 뒤 regexp_instr 부분 일치다(#258, BE 2026-08-03 개정).
    color: str | None = None
    # 명시 속성조건(PR②, api-spec §4.6 "2차 압축 속성 매칭 대상") — 축→희망값(예 {소재:린넨, 핏:오버핏}).
    # AI 내부 필드(Spring 에 안 나감) — search_catalog 가 SpringProduct.attributes 와 관대 하드 매칭한다.
    # 사용자 명시 조건만 담는다(하드). 추측 선호(소프트)는 rerank(원문+attributes)가 판단(별도 필드 없음).
    attr_conditions: dict[str, str] | None = None
    exclude_product_ids: list[int] = Field(default_factory=list)
    # AI 후보 상한(rerank 입력 top-K) — Spring size 아님(§4.6, 2026-07-23). ge=0: products[:limit]
    # slice 절단이라 음수면 '뒤에서 제외'로 뒤집혀 불변식 붕괴(형제 category_fanout_* 와 정합, PR#73).
    limit: int = Field(default=30, ge=0)


class SpringProduct(CamelModel):
    """Spring 검색 응답(BE I-1)의 상품 1건 (api-spec §4.6 응답표).

    [#100 정합, #171, #278] I-1 이 실제 반환하는 필드 = productId·name·summary·attributes·
    categoryName·brandName·price·rating·reviewCount·options·optionCount. 별칭은
    categoryName·brandName(to_camel 기본과 달라 명시 별칭으로 덮는다 — 안 그러면 rerank 가
    category/brand 를 None 으로 받는다).
    [#100 P0/P1, #171] price·rating·reviewCount 는 display 가 아니라 **AI 계산용**(예산검증
    verifiedSum·평점 사후필터·rerank 신호·리뷰부재 판별) — 질의 시점(rerank 이전)에 필요해 후보와
    함께 받는다(§4.6). 표시는 CH-5.
    [#100 P0] summary·attributes 는 필드가 없으면 파싱에서 유실되므로 명시(소비는 #101).
    list_price(originalPrice)·main_image(imageUrl)·stock 은 **I-1 미반환**(표시 전용 → CH-5 §4.3,
    재고는 담기/주문 시점 §4.1) — 방어적 optional None 으로 남겨두되 추천 경로는 쓰지 않는다.
    """

    product_id: int  # 숫자(BIGINT, product.id §2.6) — 별칭 productId
    name: str
    summary: str | None = None  # BE I-1 요약(#100 P0) — 소비는 #101
    # Layer2 속성(소재·핏 등, #100 P0) — 유연매칭(#101). 값 타입은 dict[str, object](비-str 관대):
    # BE 가 {"방수": true} 등 bool·숫자를 주면 dict[str, str] 은 후보 1건이 ValidationError 로
    # 검색 전체를 무너뜨린다(PR#127 리뷰). 소비는 #101 이라 지금 값 타입을 강제하지 않는다.
    # [#395, api-spec §4.6] BE 가 2026-08-08 배포로 `_extra`·`_source_pid`·`_domain`·`_category`
    # 를 더 이상 싣지 않는다 — 값 타입 관대 수신 규약은 그대로 유지된다.
    attributes: dict[str, object] | None = None
    price: int | None = None  # 판매가 — AI 계산용(예산·maxPrice·rerank, #100 P1), 표시 아님
    rating: float | None = None  # 조회 시 집계(DDL D9) — AI 계산용(평점필터·rerank, #100 P0)
    # 조회 시 집계 리뷰수 — AI 계산용(#171). rating 과 짝지어 '리뷰 없어 0'(review_count==0,
    # 데이터 부재)과 '리뷰 있고 저평점'을 가른다. None/미전송이면 rating 이 지배(구 동작 폴백).
    review_count: int | None = None
    # [#278] Spring 송신 계약은 옵션 이름 최대 20개다. AI는 초과분도 원본 배열로 관대 수신한다.
    # [#508, 2026-08-09 BE 배포] option_count 는 "절단 전 전체 개수"가 아니라 **품절 제외 후
    # 구매 가능한 옵션 개수**다(구 기준 폐기, api-spec §4.6) — BE 가 이미 필터한 목록의 크기를
    # 그대로 쓰므로 options·option_count 는 항상 같은 기준이다. rerank·문구 소비는 여전히
    # 하지 않으며, 옵션 되물음(#455/#454)에서만 소비한다.
    options: list[str] | None = None
    option_count: int | None = Field(default=None, alias="optionCount", ge=0)
    category: str | None = Field(default=None, alias="categoryName")
    brand: str | None = Field(default=None, alias="brandName")
    # ↓ I-1 미반환(표시 전용 → CH-5 §4.3 / 재고는 §4.1) — 방어적 optional, 추천 경로 미사용
    list_price: int | None = Field(default=None, alias="originalPrice")
    stock: int | None = None
    main_image: str | None = Field(default=None, alias="imageUrl")


class ProductSearchResult(CamelModel):
    """POST /products/search 응답. total_count 는 사후필터 통과 후보 수(top-K 절단 전).

    [#100 P2 결정] 별도 totalCount 필드는 두지 않는다. size 제거(전량 반환)로 search_catalog 가
    **top-K 절단 전에** total_count 를 확정하므로(PR#127 리뷰 반영) 이는 현재 필터를 통과한 매칭
    수이지 절단값(min(매칭, limit))이 아니다. 완화 칩 estCount 는 '완화된 다른 필터'의 count 라 이
    값으로 못 구하고(재쿼리/BE count 필요 — 완화 칩 자체가 미구현, 별도 이슈), 되돌리기 칩은
    top-K 절단된 응답 후보 내 억제 수라 page-local 근사다(전량 기준 진짜 억제 수보다 작을 수 있음).
    """

    products: list[SpringProduct] = Field(default_factory=list)
    total_count: int = 0


# ── 2. 구매 이력 조회 (§4.7, C-6) — 구 order_seed 시드 대체 ──


class OrderHistoryItem(CamelModel):
    """I-19 주문 아이템 (api-spec §4.7). [v0.15.10] categoryName 포함(BE 확정) — exact productId
    제외 + 소모품 카테고리 억제(결정 14-F) 모두의 소스."""

    order_item_id: int
    product_id: int  # 숫자(BIGINT, §2.6 internal)
    product_name: str | None = None
    option_name: str | None = None
    quantity: int = 1
    price: int | None = None
    status: str | None = None
    category: str | None = Field(
        default=None, alias="categoryName"
    )  # I-19 v0.15.9 (소모품 억제 소스)


class OrderHistory(CamelModel):
    """I-19 주문 1건. shippingFee 는 MVP 항상 0, totalAmount = 아이템 스냅샷 합."""

    order_id: int
    ordered_at: str  # ISO-8601
    status: str | None = (
        None  # [v0.15.5 정정] 주문 상태 6종(PAID/PREPARING/SHIPPING/DELIVERED/CANCELED/RETURNED)
    )
    items: list[OrderHistoryItem] = Field(default_factory=list)
    items_total: int | None = None
    shipping_fee: int = 0
    total_amount: int | None = None


def _parse_ordered_at(value: str | None) -> datetime | None:
    """ISO-8601 ordered_at 파싱(실패 시 None). tz-aware 는 naive 로 정규화(naive 비교)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # aware 는 UTC 로 변환 후 naive 화(offset 만 버리면 wall-clock 이 최대 수시간 어긋남).
    # naive(offset 없음)는 그대로 — 90일 윈도우 granularity 에선 UTC 가정 오차 무의미.
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class RecentPurchases(CamelModel):
    """I-19 GET /internal/members/{id}/orders 응답 data (api-spec §4.7, BE 본문 재작성 v0.15.0).

    [변경 v0.15.0] 구 3필드(productId/category/purchasedAt) 폐기 — BE 본문 재작성 반영.
    [v0.15.10] items 에 categoryName 포함(BE 확정) → exact productId 제외 + 소모품 카테고리
    억제(결정 14-F) 모두 가능. 실패/타임아웃 시 dedup 없이 추천 진행(degrade, §4.7).
    """

    orders: list[OrderHistory] = Field(default_factory=list)

    def recent_items(
        self, *, since: datetime | None = None, exclude_statuses=frozenset()
    ) -> list["OrderHistoryItem"]:
        """윈도우·상태 필터를 통과한 구매 아이템 목록 — exact 제외·카테고리 억제(결정 14-F) 공용 소스.

        since 보다 오래된 주문(또는 ordered_at 불명)은 제외(영구 제외 방지). exclude_statuses
        (예: CANCELED/RETURNED)의 아이템은 보유분이 아니라 제외한다.
        """
        blocked = {s.upper() for s in exclude_statuses}
        out: list["OrderHistoryItem"] = []
        for order in self.orders:
            if since is not None:
                dt = _parse_ordered_at(order.ordered_at)
                if dt is None or dt < since:
                    continue
            order_status = (order.status or "").upper()
            for item in order.items:
                if (item.status or order_status).upper() in blocked:
                    continue
                out.append(item)
        return out

    def purchased_product_ids(
        self, *, since: datetime | None = None, exclude_statuses=frozenset()
    ) -> set[int]:
        """exact 제외 dedup(결정 14-F) 대상 productId 집합 — recent_items 위임."""
        return {
            item.product_id
            for item in self.recent_items(since=since, exclude_statuses=exclude_statuses)
        }


# ── 3. 주문 상태 요약 (I-4, §4.10) ──


OrderItemStatus = Literal[
    "PENDING",
    "ORDERED",
    "SHIPPING",
    "DELIVERED",
    "CONFIRMED",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "RETURN_REQUESTED",
    "RETURNED",
]
OrderItemStatusText = Literal[
    "결제 대기",
    "주문 완료",
    "배송중",
    "배송 완료",
    "구매 확정",
    "취소 접수",
    "취소 완료",
    "반품 접수",
    "반품 완료",
]
OrderRepresentativeStatus = Literal[
    "결제 대기",
    "결제 실패",
    "주문 완료",
    "배송중",
    "배송 완료",
    "구매 확정",
    "취소/반품 진행중",
    "처리 완료",
]
ORDER_STATUS_RECENT = 3

ORDER_ITEM_STATUS_TEXT: Mapping[OrderItemStatus, OrderItemStatusText] = MappingProxyType(
    {
        "PENDING": "결제 대기",
        "ORDERED": "주문 완료",
        "SHIPPING": "배송중",
        "DELIVERED": "배송 완료",
        "CONFIRMED": "구매 확정",
        "CANCEL_REQUESTED": "취소 접수",
        "CANCELLED": "취소 완료",
        "RETURN_REQUESTED": "반품 접수",
        "RETURNED": "반품 완료",
    }
)


class OrderStatusItem(CamelModel):
    """I-4 주문 상품. status/statusText는 Spring의 canonical pair만 허용한다."""

    product_name: Annotated[StrictStr, Field(max_length=200)]
    status: OrderItemStatus
    status_text: OrderItemStatusText

    @model_validator(mode="after")
    def validate_status_text_pair(self) -> "OrderStatusItem":
        if self.status_text != ORDER_ITEM_STATUS_TEXT[self.status]:
            raise ValueError("statusText does not match status")
        return self


class OrderStatusOrder(CamelModel):
    """I-4 주문 1건. 주문 번호와 시각은 coercion 없이 계약 범위를 지킨다."""

    order_id: Annotated[StrictInt, Field(ge=1, le=9_223_372_036_854_775_807)]
    ordered_at: AwareDatetime
    representative_status: OrderRepresentativeStatus
    items: list[OrderStatusItem]

    @field_validator("ordered_at", mode="before")
    @classmethod
    def ordered_at_must_be_datetime_or_iso_string(cls, value: object) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("orderedAt must be an ISO-8601 datetime")
        return value

    @field_validator("items", mode="before")
    @classmethod
    def items_must_be_list(cls, value: object) -> object:
        if not isinstance(value, list):
            raise ValueError("items must be a list")
        return value


class OrderStatusSummary(CamelModel):
    """I-4 data envelope. orders is required and bounded by the fixed recent=3 request."""

    orders: list[OrderStatusOrder] = Field(max_length=ORDER_STATUS_RECENT)

    @field_validator("orders", mode="before")
    @classmethod
    def orders_must_be_list(cls, value: object) -> object:
        if not isinstance(value, list):
            raise ValueError("orders must be a list")
        return value


# ── 4. 장바구니 담기 (I-2, §4.1) — BE 문서 채택, 단건 ──


class RecommendationContext(CamelModel):
    """추천 카드에서 전환된 담기의 출처(I-2, 선택).

    Spring은 ``list_id``를 권위로 다시 검증하므로, 이 값이 없거나 낡았어도 담기 자체는
    실패하지 않는다. AI는 직전 추천을 실제로 노출한 경우에만 이 문맥을 보낸다.
    """

    recommendation_request_id: str = Field(max_length=36)
    list_id: str = Field(max_length=64)


class AddToCartRequest(CamelModel):
    """I-2 POST /internal/cart/items 요청 본문 (api-spec §4.1, BE 문서 채택).

    userId/guestId 둘 중 하나 — AI-검증 JWT sub 유래(챗 요청의 메아리, FE 본문 값 불신).
    게스트 담기 허용(결정 8 개정). id 타입 = DB 스키마 기준: productId·optionId = 숫자(BIGINT), guestId = UUID 문자열(§2.6).
    quantity 1~99, 동일 상품·옵션 기존 존재 시 Spring 이 합산.
    """

    user_id: int | None = None
    guest_id: str | None = None  # 게스트 UUID(guest.id CHAR(36))
    product_id: int  # 숫자(BIGINT, product.id)
    option_id: int | None = None  # 숫자(BIGINT, product_option.id)
    quantity: int = Field(1, ge=1, le=99)
    # Spring은 "chat:{chatSessionId}" sentinel로 챗봇 전환 이벤트를 적재한다. 누락 시
    # 담기는 성공하지만 분석 이벤트만 빠지므로, 챗봇 경로는 항상 sessionId를 배선한다.
    chat_session_id: str | None = None
    recommendation_context: RecommendationContext | None = None


class AddToCartResult(CamelModel):
    """I-2 성공 응답 — {success, data:{cartItemId}} (api-spec §4.1).

    실패는 HTTP 오류로 옴: 400 CART_OPTION_REQUIRED(options 목록 → 되물음 멀티턴) /
    400 CART_OPTION_INVALID / 404 PRODUCT_NOT_FOUND / 401 INTERNAL_TOKEN_INVALID.
    SSE action.reason 매핑: PRODUCT_NOT_FOUND | STOCK_INSUFFICIENT | CART_ERROR (STOCK_INSUFFICIENT는
    BE CART_STOCK_INSUFFICIENT+availableStock 유래, 2026-07-22).
    """

    success: bool
    cart_item_id: int | None = None  # 숫자(BIGINT, cart_item.id)


# ── 4. 장바구니 조회 (I-18, §4.9, C-16) ──

# 구매 가능 상태 — I-18(§4.9) · I-28(§4.16) 공용. 겹치면 HIDDEN 우선(서버가 정해서 내린다).
# HIDDEN 은 status != ON_SALE 이라 옵션과 무관하게 상품 전체가 판매 종료다. SOLD_OUT 판정
# 규칙("옵션 중 하나라도 살 수 있으면 AVAILABLE")은 그대로지만, [#508, 2026-08-09 BE 배포]
# 근거는 "재고가 product.stock_quantity 하나로 옵션 전체에 공유(product_option 에 재고 컬럼
# 없음)"에서 "옵션별 재고(product_stock(product_id, option_id, quantity), 02 D33)의 OR"로
# 바뀌었다 — 구 근거는 폐기.
PurchaseState = Literal["AVAILABLE", "SOLD_OUT", "HIDDEN"]

# 상태 → 안내용 한국어 라벨. **전사(全射) 매핑을 의도한다**(ORDER_ITEM_STATUS_TEXT 와 같은 형태).
# 다만 그 전사성을 강제하는 건 아래 타입 어노테이션이 **아니라**
# `tests/unit/test_cart.py::test_purchase_state_label_covers_every_literal_value` 다 —
# `Mapping[PurchaseState, str]` 은 부분 매핑을 막지 못하고(TypedDict 가 아니다) 이 리포 CI 엔
# 타입체커도 없다. 소비 측이 `.get(state, "")` 로 조용히 넘어가므로, 그 테스트가 없으면 새 상태를
# 추가하고 라벨을 빠뜨렸을 때 예외 없이 라벨만 사라진다(PR #400 리뷰).
# AVAILABLE 이 빈 문자열인 것은 "살 수 있다"를 굳이 말하지 않는다는 표현 정책이며, 매핑의
# 완전성과는 별개다 — 라벨을 붙일지 말지는 소비 측(app/agents/buyer/cart/purchase_state.py)이 정한다.
PURCHASE_STATE_LABEL: Mapping[PurchaseState, str] = MappingProxyType(
    {
        "AVAILABLE": "",
        "SOLD_OUT": "품절",
        "HIDDEN": "판매 종료",
    }
)

# 유효값 판정은 라벨 매핑이 아니라 **Literal 에서 직접 뽑는다** — 라벨은 표현용이라 누군가
# 항목을 빼도 이상하지 않지만, 그때 그 상태가 조용히 "모름"으로 강등되면 안 된다. 계약의
# 단일 진실 원천은 `PurchaseState` 하나다.
_KNOWN_PURCHASE_STATES: frozenset[str] = frozenset(get_args(PurchaseState))


def _degrade_unknown_purchase_state(value: object) -> object:
    """계약 밖 상태값을 `None`(모름)으로 강등한다 — 항목 자체는 살린다(#310).

    찜(`spring_client::_parse_wishlist_items`)은 미지 값이 오면 그 **항목을 skip** 하지만
    장바구니에 같은 처방을 쓰면 안 된다: 스킵된 항목이 목록에서 조용히 사라지면
    "전부 빼줘"(`cart_remove_all_markers` → 전 항목 삭제)가 일부만 지우고 CART_REMOVED 로
    성공을 보고하고, `_existing_quantity` 합산 안내도 어긋난다. 장바구니 항목은 사용자
    소유물이자 파괴적 후속 동작의 입력이라 **사라지는 것이 조용히 틀리는 것보다 나쁘다**.
    미지 값은 "모름"과 사실상 같은 처지이므로 `None` 으로 떨어뜨리고 관측용 warning 만 남긴다.

    **`isinstance(value, str)` 가드가 멤버십 검사보다 먼저다**(PR #400 리뷰). `{}`·`[]` 같은
    unhashable 값이 오면 `value in frozenset` 이 `hash(value)` 에서 `TypeError` 를 내는데,
    pydantic v2 는 `BeforeValidator` 가 던진 `TypeError` 를 `ValidationError` 로 감싸지 않고
    그대로 올린다. 그러면 `spring_client::get_cart` 의
    `except (httpx.HTTPError, ValueError, ValidationError)` 를 빠져나가 **degrade 조차 못 하고**,
    이 함수가 막으려던 "드리프트 하나가 장바구니 전체를 죽인다"가 더 나쁜 형태로 재현된다.
    숫자·bool 은 hashable 이라 가드 없이도 강등됐지만, 타입을 좁히는 편이 의도에도 맞다 —
    `purchaseState` 는 애초에 문자열 enum 이다.
    """
    if value is None or (isinstance(value, str) and value in _KNOWN_PURCHASE_STATES):
        return value
    _log.warning("계약 밖 purchaseState 값 — None(모름)으로 강등한다: %r", value)
    return None


class CartViewItem(CamelModel):
    """I-18 GET /internal/cart 응답 항목 — productName/optionName 은 챗 답변 생성 필수(🔴)."""

    cart_item_id: int  # 숫자(BIGINT, cart_item.id)
    product_id: int  # 숫자(BIGINT, product.id)
    product_name: str | None = None
    option_id: int | None = None  # 숫자(BIGINT, product_option.id)
    option_name: str | None = None
    quantity: int = 1
    price: int | None = None  # 표시가(선택, 총액 안내용 — 표시 권위는 Spring)
    # 미수신은 None(모름) — "AVAILABLE" 로 단정하지 않는다. 소비가 붙은 이상 기본값은 주장이
    # 되고, 키가 없다는 사실을 "구매 가능이 확인됨"으로 바꿔 읽으면 못 사는 상품을 살 수
    # 있다고 안내하게 된다(#310, REQ-CART-037).
    purchase_state: Annotated[
        PurchaseState | None, BeforeValidator(_degrade_unknown_purchase_state)
    ] = None


class CartOption(CamelModel):
    """CART_OPTION_REQUIRED/INVALID 응답의 옵션 항목 — LLM 되물음 문구 생성용(§4.1).

    [BE 확정 2026-07-18] I-2 CART_OPTION_REQUIRED 는 error.detail.options 에
    [{optionId, name, extraPrice}] 를 싣는다(C-3 OPEN-CART-2 해소). extraPrice 는
    옵션 추가금(표시·되물음 문구용, 없으면 None).
    """

    option_id: int  # 숫자(BIGINT, product_option.id)
    name: str = ""  # 표시명(BE: name)
    extra_price: int | None = None  # 옵션 추가금(extraPrice)


class CartView(CamelModel):
    """I-18 응답. 빈 장바구니는 items=[] 정상 200 (오류 아님, §4.9)."""

    items: list[CartViewItem] = Field(default_factory=list)


# ── 5. 추천 목록 push (I-21, §4.2, 경로 B) ──

# I-21 계약 하드 상한(api-spec §4.2) — **튜너블이 아니다.** Spring 이 400 으로 거절하는 경계라
# 설정으로 넘길 수 있으면 안 된다. config 의 노출 개수(expose_max)가 이 값을 참조해 상한을
# 넘지 못하도록 기동 시점에 검증한다(core/config.py) — 넘으면 push 페이로드 생성에서
# ValidationError 가 나는데, 그 지점은 degrade 블록 밖이라 SSE 스트림이 통째로 끊긴다(PR #212 리뷰).
LIST_MAX_PRODUCTS = 9  # 목록당 상품(2026-07-30 확정, 구 Top5 에서 상향)
MAX_LISTS = 10  # 한 콜백의 목록 수 — 목록 10 × 상품 9 = 90, FE 는 CH-5 를 10번 호출한다
LIST_ID_MAX_LEN = 64  # Redis 키 오염 방지
LIST_LABEL_MAX_LEN = 50  # BE VARCHAR(50)
REQUEST_ID_MAX_LEN = 36  # BE CHAR(36)


class RecoReason(CamelModel):
    """I-21 추천 근거 항목 — Spring 이 Redis 저장 → CH-5 카드에 echo (§4.2 v0.15.15 확정, C-9).

    productId 로 키잉한다 — 순서 권위는 **자기가 속한 목록의** RecommendationListEntry.product_ids
    다(v0.17.1 lists[] 전환 전에는 평평한 RecommendationPush.product_ids 였다). rerank 가 산출한
    상품별 1문장 근거를 담으며, 근거를 생성하지 못한 상품은 생략(부분집합/순서무관 허용).
    생성 목표는 한글 40자 이내 1문장(rerank 프롬프트) — reason 은 판매자 입력 영향 자유 텍스트라
    push 전 그래프에서 개행 제거·안전 상한(config reason_max_len)으로 방어 정제한다. 표시 오버플로는 FE.
    """

    product_id: int  # 숫자(BIGINT, product.id) — internal 계약(§2.6)
    reason: str


class RecommendationListEntry(CamelModel):
    """I-21 목록 1건 (api-spec §4.2 v0.17.1 다중 목록).

    한 번의 추천이 목록을 여러 개 낼 수 있다 — 니즈별 추천(파우치·어댑터 각각의 후보)과
    세트 여러 안(조합 A·B·C)은 목록 하나로 표현되지 않는다. 목록이 1개여도 lists 는 길이 1 배열.
    listId 는 FastAPI 가 생성하는 UUID 급 무작위(≥128bit) — CH-5 가 인증 불필요 공개 조회라
    사실상 bearer 키이므로 순번·타임스탬프 등 추측 가능한 형식 금지(v0.16.1).
    상한은 계약 하드 값이지 튜너블이 아니다 — 실제 노출 개수는 config(expose_min~expose_max).
    """

    # 허용 문자 영숫자·`-`·`_`, ≤64자 — Redis 키 오염 방지(§4.2 400 조건).
    list_id: str = Field(pattern=rf"^[A-Za-z0-9_-]{{1,{LIST_ID_MAX_LEN}}}$")
    # 세트 성격("알뜰")·니즈 이름("파우치"). BE VARCHAR(50) — 초과 시 400.
    label: str | None = Field(default=None, max_length=LIST_LABEL_MAX_LEN)
    # 순서 유지 = 렌더 순서. 목록당 1~9개(2026-07-30 확정, 구 Top5 에서 상향).
    product_ids: list[int] = Field(min_length=1, max_length=LIST_MAX_PRODUCTS)
    # productId로 키잉(§4.2) — 상품마다 최대 1건이라 상품 상한과 같은 값.
    reasons: list[RecoReason] = Field(default_factory=list, max_length=LIST_MAX_PRODUCTS)

    @field_validator("product_ids")
    @classmethod
    def _no_duplicate_product(cls, value: list[int]) -> list[int]:
        """한 목록 안 productId 중복은 400 — 같은 카드가 두 번 렌더된다(§4.2)."""
        if len(set(value)) != len(value):
            raise ValueError("productIds 에 중복 id 가 있다(§4.2)")
        return value


class RecommendationPush(CamelModel):
    """I-21 POST /internal/recommendations 요청 (경로 B, api-spec §4.2, v0.17.1).

    최종 랭크 상품 id 목록만 전달한다 — listId 는 FastAPI 가 생성해 넘기고(Spring 이 Redis 에
    이 키로 TTL 10분 저장), FE 는 CH-5 GET /api/chat/lists/{listId} 로 카드를 조회한다.
    [변경 v0.15.0] 구 groups/items 구조 폐기. [확정 v0.15.15, BE 구현 07-18] reason 은 이 콜백에
    포함(reasons[{productId, reason}]) → Spring 이 CH-5 카드에 echo(§4.2). 선택 필드 — 근거 없는
    상품은 생략, 비면 [] 로 직렬화.
    [변경 v0.17.1, 이슈 #209] 최상위가 lists[] 배열 — 구 평평 3필드(listId·productIds·reasons)는
    폐기다(BE 과도기 수용 코드 제거 조건). listType·totalBudget·label 은 표시 필드가 아니라
    목록의 성격 메타이며 표시 권위는 그대로 Spring 에 있다(경로 B 유지).
    productId 는 internal 계약이라 숫자(BIGINT, §2.6).
    빈 목록은 400 이 아니라 "보내지 않는 것"이 맞다 — 후보가 없으면 콜백 자체를 생략한다.
    상위는 이 콜백이 성공한 뒤에만 SSE products.ready 를 emit 한다(§3.3).
    """

    session_id: str
    # 추천 실행 1회를 가리키는 opaque id(FastAPI 생성). 노출·클릭·담기·주문을 그 추천에
    # 귀속시키는 조인 키로 listId 와 역할이 달라 서로 대체하지 않는다(이슈 #140). BE CHAR(36).
    recommendation_request_id: str = Field(max_length=REQUEST_ID_MAX_LEN)
    # 목록 안 상품들이 대체재(PICK_ONE — 하나만 산다)인지 보완재(BUY_ALL — 전부 산다)인지.
    # 목록 개수는 lists 길이로 알 수 있지만 이 값은 개수로 복원할 수 없어 항상 싣는다(§4.2).
    list_type: Literal["PICK_ONE", "BUY_ALL"]
    # BUY_ALL + 예산 발화 시에만("5만원 내로" → 50000). 채우는 쪽은 LLM 이 발화에서 뽑아낸
    # 값이라 신뢰경계에서 하한을 막는다 — 예산 상한이 음수일 수는 없다(#60·#163 착수 전 예방).
    # 상한은 두지 않는다: 과도하게 큰 값은 "제한 없음"과 같아 무해하고 가격은 BIGINT 다.
    total_budget: int | None = Field(default=None, ge=0)
    lists: list[RecommendationListEntry] = Field(min_length=1, max_length=MAX_LISTS)

    @field_validator("lists")
    @classmethod
    def _no_duplicate_list_id(
        cls, value: list[RecommendationListEntry]
    ) -> list[RecommendationListEntry]:
        """한 콜백 안 listId 중복은 400 (§4.2).

        멱등 키가 (recommendationRequestId, listId) 쌍이라 같은 listId 가 두 번 오면
        뒤 목록이 재전송으로 오해돼 조용히 버려진다 — 저장 전에 거절한다.
        """
        ids = [entry.list_id for entry in value]
        if len(set(ids)) != len(ids):
            raise ValueError("한 콜백 안에 같은 listId 가 두 번 왔다(§4.2)")
        return value


# ── 6. 상품 변경분 pull (I-17, §4.8, C-4) — AI 생성물 갱신 배치 ──


class ProductChange(CamelModel):
    """I-17 변경분 항목. status=HIDDEN 은 AI 생성물 삭제/비활성 트리거 (§4.8).

    콘텐츠 필드는 enrichment·search_doc 조립 입력 — AI 는 저장하지 않고 산출물 생성에만 사용.
    미정의 status 는 페이지 전체 계약 위반으로 거부해 해당 페이지 artifact·커서를 보존한다.

    **`DELETED`(BE 02 D41)를 여기 추가하지 않는다** — Spring 이 `!= ON_SALE` 을 전부
    `"HIDDEN"` 으로 실어 보내므로(`ProductChangesResponse.Item.hidden()`) 삭제·숨김이
    AI 에겐 같은 신호(생성물 제거)이고 동작이 동일하다. 3값으로 넓히면 위 fail-closed
    규약과 충돌해 정상 페이지가 전량 실패한다.
    """

    product_id: (
        int  # 숫자(BIGINT, product.id) — BE I-17 예시 문자열은 DDL과 불일치, 스키마 기준 int
    )
    status: Literal["ON_SALE", "HIDDEN"]
    updated_at: str  # ISO-8601
    name: str | None = None
    description: str | None = None
    category: str | None = None
    brand: str | None = None  # BE 07-18 확정 — enrichment·search_doc 입력(저장 안 함)
    attributes: dict | None = None


class ProductChangesPage(CamelModel):
    """I-17 응답 페이지. hasMore=True 면 nextCursor 로 같은 주기 안에서 즉시 재요청."""

    items: list[ProductChange] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


# ══════════════════════════════════════════════════════════════════════════
# 판매자 조회/집계·상품 CRUD 스키마 (DESIGN-SELLER-TOOLS-STAGE1 §2.4, api-spec §4.4/§4.5).
#
# 전부 초안(🔴 C-13/C-14) — Spring 실측 필드 유동을 감안해 응답 모델 다수는
# extra="allow" 로 여분 필드를 허용한다(파싱 실패로 도구가 죽지 않도록).
# 확정 시 이 섹션의 어댑터만 수정한다(도구·calc 계층 불변).
# ══════════════════════════════════════════════════════════════════════════


class SellerAggregateModel(CamelModel):
    """판매자 집계/이력 응답 공통 베이스 — 필드 유동 대비 extra="allow"."""

    model_config = ConfigDict(extra="allow")


# ── I-6 매출 시계열 (§4.4) ──


class SalesSeriesPoint(SellerAggregateModel):
    """매출 시계열 1건. isAnomaly/deviationPct 는 Spring 참고치 — calc.py 는 무시하고 원시
    sales 로 재판정한다(§0.1 D, C-13).

    [수정 2026-07-30] deviationPct 는 nullable — Spring(SellerSalesService)이 이동평균
    구간 미달(MIN_WINDOW 미만)·기준선 0 인 포인트에 null 을 내려보낸다. float 고정이면
    기간 첫 포인트들에서 ValidationError → SpringUnavailableError → 도구 degrade 로
    매출 조회 전체가 "응답 형식 오류"로 실패했다."""

    date: str
    sales: int
    order_count: int
    # [#489] 판매 **수량** — PAID 주문 order_item 의 SUM(oi.quantity), CANCELLED/
    # RETURNED 제외(S-1 과 동일 규칙). orderCount(주문 **건수**)와 단위가 다르다.
    # nullable 인 이유: granularity=summary 응답에는 수량 필드가 없다고 명세가
    # 명시했고, BE 미배포 구간 대비도 겸한다. (2026-08-06 드리프트 정정 — 구현
    # sumSellerSalesByPeriod 에는 처음부터 있었으나 스키마에 필드가 없어 AI 가
    # 파싱하지 않고 버리고 있었다. 신규 계약이 아니라 문서 정정이다.)
    sales_count: int | None = None
    is_anomaly: bool = False
    deviation_pct: float | None = None


class SalesResult(SellerAggregateModel):
    """I-6 GET /internal/seller/{brandId}/sales 응답 (매출 시계열)."""

    series: list[SalesSeriesPoint] = Field(default_factory=list)


# ── I-7 구매전환 퍼널 (§4.4) ──


# I-7 stages[].stage → 평면 필드 매핑. 키는 snake_case — BE 실측 확정(#194 리뷰 검증):
# SellerAnalyticsService.funnel 이 "product_view"/"add_to_cart"/"checkout_start"/
# "purchase_complete" 리터럴로 Stage 를 만든다(I-13 counts 의 camelCase 와 다른 어휘 —
# 혼동 주의). 어휘가 어긋나면 아래 validator 가 해당 단계를 미집계로 편입해 표면화한다.
_FUNNEL_STAGE_FIELD = {
    "product_view": "view",
    "add_to_cart": "cart",
    "checkout_start": "checkout",
    "purchase_complete": "purchase",
}


class FunnelResult(SellerAggregateModel):
    """I-7 GET /internal/seller/{brandId}/funnel 응답 — view→cart→checkout→purchase 4단.

    [수정 2026-07-30] 실제 Spring 응답(SellerFunnelResponse)은 평면 4필드가 아니라
    {stages:[{stage,count,source,computable}], conversionRates:{...}} 형태다. 종전
    스키마는 기본값 0 + extra="allow" 탓에 검증이 조용히 통과해 전 단계 0 인
    퍼널(무데이터)로 오분석될 수 있었다 — before validator 로 stages 를 평면 4필드로
    변환한다. 평면 입력(테스트 스텁·구계약)도 그대로 허용한다.

    [PR#184 리뷰 반영] count=null·computable=false 단계(checkout v1 미계산 구간)는
    "집계 안 됨"이지 "실제 0건"이 아니다 — 0 으로만 수렴시키면 calc.conversion_rates 가
    미계산 구간을 진짜 0% 전환으로 보고할 위험이 있다. 해당 단계명을
    uncomputable_stages 에 기록해 하류(calc·tools)가 전환율 계산에서 제외하고
    "미집계"로 표시하게 한다."""

    view: int = 0
    cart: int = 0
    checkout: int = 0
    purchase: int = 0
    # stages[] 변환 시 집계 불가로 판정된 평면 필드명("checkout" 등) — 평면 입력은 빈 목록.
    uncomputable_stages: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _flatten_stages(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("stages"), list):
            data = dict(data)
            # [#194 리뷰 반영] 매핑으로 채워지지 않은 평면 필드를 추적한다 — stage 어휘가
            # 코드 가정과 어긋나면(오탈자·casing 변경·단계 누락) 종전에는 조용히 건너뛰어
            # 기본값 0(= "실제 0건")으로 새던 것을, "미집계"로 편입해 표면화한다
            # (I-14/I-15 에서 고친 것과 동일한 은폐 패턴 차단).
            # [#194 리뷰 2] 미집계 판정은 entry 별 즉시 append 가 아니라 필드별 최종
            # 항목 기준으로 내린다 — 동일 stage 가 중복으로 오면(BE 이상 응답) 값은
            # 마지막 항목으로 덮이는데 판정만 앞 항목 것이 남아, 값은 정상 수치인데
            # "미집계"로 표시되는 불일치가 생기던 문제.
            uncomputable_by_field: dict[str, bool] = {}
            unknown_stages: list[object] = []
            for entry in data["stages"]:
                if not isinstance(entry, dict):
                    continue
                field = _FUNNEL_STAGE_FIELD.get(entry.get("stage"))
                if field is None:
                    unknown_stages.append(entry.get("stage"))
                    continue
                count = entry.get("count")
                # count null(집계값 없음) 또는 computable=false(v1 미계산 구간) = 미집계.
                uncomputable_by_field[field] = count is None or entry.get("computable") is False
                data[field] = count if count is not None else 0
            unfilled = set(_FUNNEL_STAGE_FIELD.values()) - uncomputable_by_field.keys()
            if unknown_stages or unfilled:
                _log.warning(
                    "I-7 funnel stages 계약 어긋남 — 미인식 stage=%s, 미수신 단계=%s "
                    "(해당 단계는 미집계 처리)",
                    unknown_stages,
                    sorted(unfilled),
                )
            uncomputable = [f for f, unc in uncomputable_by_field.items() if unc]
            uncomputable.extend(sorted(unfilled))
            data.setdefault("uncomputableStages", uncomputable)
        return data


# ── I-13 행동 이벤트 집계 (§4.4 — 07/17 BE 확정 명세 반영, REALIGN F4/②-3) ──


class BehaviorProductRow(SellerAggregateModel):
    """I-13 groupBy=product rows[] 항목 — 상품별 행동 카운트·전환 보조 지표.

    counts 키는 event_type 의 camelCase(productView/addToCart/removeFromCart/
    checkoutStart/purchaseComplete — 2026-08-06 개정으로 4종 → 5종, 장바구니
    이벤트 BE 이관과 함께 remove_from_cart 편입). viewToCartRate 는
    addToCart/productView(분모 0 = null).
    uniqueVisitors = distinct(memberId, guestId) — 비로그인 게스트 포함.

    [#489 근본 수정] 베이스가 CamelModel(extra 무설정 = pydantic 기본 "ignore")
    이라 형제 판매자 모델(SellerAggregateModel, extra="allow")과 달리 **BE 가
    추가한 신필드가 예외도 안 나고 model_extra 에도 안 남고 통째로 소실**됐다.
    필드 5종을 더하는 것보다 이 베이스 교체가 근본 수정이다 — 앞으로 BE 가 뭘
    추가하든 같은 일이 반복된다. SellerAggregateModel 은 CamelModel 하위라
    alias_generator(to_camel)·populate_by_name 규약은 그대로다.

    [#489] 신필드 5종은 전부 nullable 이며 **기본값 0 을 두지 않는다** — 명세가
    null 을 계약값으로 규정한다(0="안 팔림"/"체류 0초", null="미조회"/"표본 없음").
    0 기본값은 churn_rate 에서 잡았던 silent-mismatch(#197)를 그대로 재도입한다.
    """

    product_id: int
    product_name: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    # 판매 **수량**(2026-08-06 신설) — 기간 내 PAID 주문(orders.paid_at)의
    # SUM(oi.quantity). 아이템 상태 PENDING/CANCELLED/RETURNED 를 **제외**한다
    # (I-6 salesCount 와 동일 산식). 같은 row 의 counts.purchaseComplete 는 주문
    # **건수**이고 아이템 상태를 보지 않아 부분 취소·반품을 포함한다 — 단위도
    # 규칙도 달라 두 값을 직접 비교하면 안 된다(명세상 의도된 비대칭).
    # eventType 필터에 purchase_complete 가 없으면 null(= 미조회).
    sales_quantity: int | None = None
    # 체류시간 4종(2026-08-06 신설 — 이벤트 차분 산출, FE 무변경·과거 로그 소급).
    # 같은 session_key 안에서 occurred_at 오름차순 정렬 후 product_view 부터 다음
    # 이벤트까지의 초 단위 시간차(끝 이벤트 종류 무관 = 이탈 시각).
    # ⚠️ 세션의 **마지막 조회는 표본에서 빠진다** — page_leave 를 수집하지 않아
    # 끝 시각이 없다(구조적 한계, 버그 아님). dwellSource 가 이 사실을 표기한다.
    # 이상치 제외 3종은 BE 설정 고정(AI 가 바꿀 수 있는 쿼리 파라미터가 아니다):
    # properties._timeShifted 행 · 음수(시계 역전) · 1800초 초과(세션 30분 정합).
    # eventType 필터에 product_view 가 없거나 표본 0건이면 4필드 모두 null.
    median_dwell_seconds: float | None = None  # 주 지표(롱테일 분포라 평균은 참고값)
    avg_dwell_seconds: float | None = None  # 분포 왜곡 확인용 참고값
    dwell_sample_count: int | None = None  # 이것 없이 평균·중앙값만 해석 금지
    dwell_source: str | None = None  # "next_event" | (향후) "page_leave"
    view_to_cart_rate: float | None = None
    unique_visitors: int | None = None


class BehaviorEventsResult(SellerAggregateModel):
    """I-13 GET /internal/seller/{brandId}/events 응답 (07/17 확정 — 구 events[] 폐기).

    원천 = behavior_events(상품 연계 **5종**만 — session_start/login/search/page_view
    는 브랜드 귀속 경로가 없어 제외). [2026-08-06 개정, #489] remove_from_cart 편입
    으로 4종 → 5종 — 장바구니 이벤트 BE 이관과 동시 신설된 이벤트로, 열지 않으면
    이벤트가 쌓여도 AI 가 읽을 경로가 없다. counts 키가 5종이 되고 **rows 정렬
    기준(활동량 합)에도 removeFromCart 가 포함**된다(삭제가 잦은 상품의 순위가
    올라갈 수 있다 — "담김도 삭제도 활동"이라는 의도된 변경).
    groupBy 3형이 한 모델에 겹친다 — 채워지는 필드:
      - product(기본) : rows (+ total)
      - eventType     : counts
      - date          : series (date + camelCase 이벤트 카운트, 키 동적 → dict 유지)
    ⚠️ purchaseComplete 는 **주문 기준** 집계다 — order_item × product × brand 의
    PAID(paid_at) 건을 COUNT(DISTINCT order_id) 한 값으로 I-7 퍼널 4단과 같은
    정본이며, 이벤트 유실과 무관하고 과거 구간도 소급 복구된다. 건수이지 수량이
    아니고(한 주문에 같은 상품 여러 개여도 1), 상품별 합이 eventType 합계보다
    클 수 있으며(한 주문에 자사 상품 여러 종), 조회·담기 없이 구매만 있는 상품도
    rows 에 등장한다. 구 "이벤트 기준·권위는 I-6/I-14" 규정은 2026-07-31 개정
    (jarvis-backend#62)으로 폐기(#488).
    ⚠️ 판매 **수량**은 purchaseComplete 가 아니라 rows[].salesQuantity 다(#489,
    2026-08-06 신설 — PAID SUM(oi.quantity), 아이템 취소·반품 제외). 위 "건수이지
    수량이 아니다"의 답이 이 필드이며, 취소·반품 처리 규칙까지 서로 다르다.
    """

    group_by: str = "product"
    rows: list[BehaviorProductRow] = Field(default_factory=list)
    counts: dict[str, int] | None = None
    series: list[dict] = Field(default_factory=list)
    total: int | None = None


# ── I-14 주문 상태 전이/조회 (§4.4) ──


class OrderEventsResult(SellerAggregateModel):
    """I-14 GET /internal/seller/{brandId}/order-events 응답 (#194 — BE 실측 정렬).

    [수정 2026-07-30] 구 events/stats 필드 폐기 — Spring(SellerOrderEventsResponse)은
    rows/total/byStatus/cancelReasonsTop 을 내려보낸다(NON_NULL, shape 상호 배제).
    종전 스키마는 extra="allow" 탓에 검증이 조용히 통과해 events 가 항상 빈 목록
    (= "주문 상태 전이 0건")으로 새던 버그의 원인이었다.

    shape 3형이 한 모델에 겹친다 — 채워지는 필드:
      - 목록(기본)        : rows(Row: orderId/orderItemId/fromStatus/toStatus/
                            actorType/reason/customerLabel/createdAt) + total
      - stats=true        : byStatus + cancelReasonsTop (rows 없음)
      - groupBy=memberId  : rows(MemberRow: customerLabel/orderCount/cancelCount/
                            cancelRatio/maxOrdersPerHour/isSuspicious) + total
    rows 는 groupBy 에 따라 이형(異形)이라 dict 유지 — kv 요약(_summarize_events)이
    양쪽 모두 소화한다. rows 는 limit(기본 100) 절단본, 전수는 total.

    [개정 2026-08-06, #481] ① buyerMemberId(Long) → customerLabel(String, HMAC 6자
    사례번호) 교체 — orderId+memberId 를 S-2 화면과 대조하면 회원이 재식별되는
    프라이버시 문제. 같은 회원=같은 라벨(반복 패턴 표현), 브랜드별 상이, 역산 불가.
    ② rows[].orderItemId 신설 — 아이템 전이(SHIPPING/DELIVERED/CANCELLED/RETURNED)만
    값이 채워지고 주문 전이(PENDING/PAID/PAYMENT_FAILED)는 null. 같은 orderId 행
    복수 = 아이템별 전이(중복 아님). ③ 브랜드 스코프 축소 — orderItemId 가 있는
    행은 자사 아이템만 포함(행 수·byStatus 감소는 회귀가 아니라 타사 노출 제거).
    ④ 단위 — byStatus 는 상태별 층위 상이(SHIPPING·DELIVERED=아이템 수,
    PAID·PAYMENT_FAILED=주문 수), cancelReasonsTop.count 는 아이템 수. MemberRow 의
    orderCount·cancelRatio·isSuspicious·maxOrdersPerHour 는 DISTINCT 기반 불변
    (I-14 의 isSuspicious 는 브랜드 스코프가 원래라 I-8 과 달리 유지).

    구매자 fetch_product_changes(I-8·§4.8)와 무관한 별개 계약(혼동 금지, §4.4 주)."""

    rows: list[dict] = Field(default_factory=list)
    total: int | None = None
    by_status: dict[str, int] | None = None
    cancel_reasons_top: list[dict] = Field(default_factory=list)


# ── I-15 상품 변경 이력(판매자 감사 로그) (§4.4) ──


class ProductChangeLogRow(CamelModel):
    """I-15 rows[] 항목 (#194 — Spring SellerProductChangesResponse.Row 실측 정렬).

    oldValue/newValue 는 BE 가 Java String 으로 내려보낸다(PRICE/STOCK 숫자도 문자열) —
    품절 신호 = STOCK 변경의 newValue "0" (SOLD_OUT 상태 미도입, 07/17 D32)."""

    product_id: int
    product_name: str | None = None
    change_type: str  # PRICE | STOCK | STATUS
    old_value: str | None = None
    new_value: str | None = None
    created_at: str | None = None


class ProductChangeLogResult(SellerAggregateModel):
    """I-15 GET /internal/seller/{brandId}/product-changes 응답 — 판매자 감사 로그.

    [수정 2026-07-30, #194] 구 logs 필드 폐기 — Spring 은 rows/total 을 내려보낸다.
    종전 스키마는 extra="allow" 탓에 logs 가 항상 빈 목록으로 새던 버그의 원인이었다.
    rows 는 limit(기본 100) 절단본, 전수는 total.

    [혼동 금지] 구매자 ProductChangesPage(I-8 AI 생성물 배치, §4.8)와 다른 계약이다."""

    rows: list[ProductChangeLogRow] = Field(default_factory=list)
    total: int | None = None


# ── I-16 이탈 코호트 (§4.4) ──


class PreChurnSignals(SellerAggregateModel):
    """I-16 preChurnSignals — Spring SellerChurnResponse.PreChurnSignals 실측(#197).

    이탈 회원 전체 기준 집계다. zeroResultSearchSessions 는 현 수집 스키마상 상시
    0(E-1 FE 에 검색 결과 수 미적재, 노션 I-16 합의) — '검색 불만 없음'의 근거로
    쓰면 안 된다. priceIncreaseExposed = PRICE 인상 이후 해당 상품을 조회한 이탈
    회원 수(근사, 명수)."""

    cancel_count: int = 0
    return_reasons_top: list[dict] = Field(default_factory=list)  # ReasonCount {reason, count}
    zero_result_search_sessions: int = 0
    price_increase_exposed: int = 0


class ChurnMember(SellerAggregateModel):
    """I-16 members[] 항목(SellerChurnResponse.Member 실측, #197) — 이탈 회원 상세.

    preChurnEvent: 클레임 있으면 "RETURNED(상품불량)" 형식(최신 1건), 없으면 마지막
    행동 이벤트 타입. 서버가 CHURN_LIST_CAP=50 으로 절단해 내려보낸다(별도 total
    없음 — 이탈 전수는 cohortSize×churnRate 로 유추).

    [개정 2026-08-06, #487 — #481 잔여분] memberId(Long) → customerLabel(HMAC-SHA256
    앞 6자 Base32 사례번호)로 교체하고 lastLoginAt 을 제거한다. 원시 memberId 는
    가명이 아니라 재식별 키라 S-2(orderId+수령인 실명) 대조로 회원이 특정된다 —
    I-14 개정(#481)과 같은 근거가 I-16 에만 적용되지 않고 있었다. lastLoginAt 은
    계정 보안 정보라 판매자에게 회원 단위로 줄 이유가 없다(요약에서 읽히지도 않던 사문).

    ※ 필드 제거는 배포 순서와 무관하게 안전하다 — SellerAggregateModel 이
    extra="allow" 라 BE 미배포 구간의 구응답 memberId/lastLoginAt 은 예외 없이
    model_extra 로 흡수되고, 표시 계층이 읽지 않으므로 노출만 끊긴다."""

    customer_label: str | None = None  # HMAC 익명 라벨(사례번호) — 구 memberId 대체
    last_activity_at: str | None = None
    # to_camel("sessions_30d") 은 "sessions30D"(숫자 뒤 접미 대문자화)라 와이어 키
    # "sessions30d" 와 어긋난다 — 명시 alias 로 고정한다(#197, 테스트로 회귀 방지).
    sessions_30d: int | None = Field(default=None, alias="sessions30d")
    pre_churn_event: str | None = None


class ChurnResult(SellerAggregateModel):
    """I-16 GET /internal/seller/{brandId}/churn 응답(SellerChurnResponse 실측 정렬).

    [수정 #197] 구 pre_churn_signals: list[dict] 폐기 — Spring 은 객체를 내려보내
    ValidationError → degrade 로 새던 버그의 원인(I-14/I-15 #194 와 동일 패턴).
    churnRate 는 소수(fraction, 0.6=60%, round3 — DTO 주석 명시) — % 변환은 표시
    계층(tools)만 한다(스키마는 와이어 값 보존). 응답의 brandId/from/to/inactiveDays
    에코는 extra="allow" 로 흡수한다. 코호트 0명이면 cohortSize=0·churnRate=0.0·
    빈 signals·빈 members 로 온다(BE short-circuit).

    [#495 주의] 코호트 0명의 churnRate 는 정본(노션 I-16)이 null 인데 본 사본
    api-spec §4.4(v0.19.1)는 0.0 으로 적어 어긋나 있다 — 런타임은 tools 의
    cohort_size == 0 조기 반환이 먼저라 어느 값이 와도 무해하다. 사본 정정 자체는
    전수 대조(#472) 소관이라 여기서는 표시만 남긴다."""

    # [#197 PR 리뷰] 기본값 0.0 금지 — churnRate 키 결측이 조용히 "이탈률 0.0%"로
    # 렌더링되는 silent-mismatch(이 PR 이 제거한 #194 패턴)를 이 필드만 재도입하지
    # 않는다. 결측은 None 으로 남겨 표시 계층(tools)이 "미수신"으로 명시 처리한다.
    churn_rate: float | None = None  # fraction (0.0~1.0), None = 응답 결측
    cohort_size: int | None = None
    pre_churn_signals: PreChurnSignals | None = None  # 실측상 항상 옴 — None 은 방어 기본값
    members: list[ChurnMember] = Field(default_factory=list)


# ── I-8 계정 이벤트 집계 (자사 코호트, §4.4 — 2026-08-06 브랜드 스코프 전환) ──


class AccountEventsResult(SellerAggregateModel):
    """I-8 GET /internal/seller/{brandId}/account-events 응답 — 자사 코호트 스코프
    (#481, 노션 2026-08-06 개정)·IP 마스킹·집계 전용.

    [개정 2026-08-06] 전역 → 자사 코호트 전환에 따른 ip rows 개정 —
    isSuspicious(브랜드 스코프에선 임계 도달 불가 → 상시 false 오보 위험이라
    재정의가 아니라 제거)·failCount(동일 사유, 총량은 eventCount 대체)·
    nullMemberRatio(member 조인 코호트에선 상시 0) 제거.
    suspiciousMemberCount(해당 IP 코호트 회원 중 I-14 어뷰징 기준 해당 **회원
    수** — 서버가 SQL 교차, 회원 키는 주지 않는다)·scope("brand" 에코) 신설.

    [수정 #197] 구 events 필드 폐기 — Spring 은 rows 를 내려보낸다. extra="allow"
    탓에 검증이 조용히 통과해 항상 "0건 집계됨"으로 새던 I-14/I-15(#194)와 동일
    패턴. rows 는 groupBy 별 이형(異形) — eventType|hour: Bucket{key, count} /
    ip: IpRow{ipMasked, distinctMembers, suspiciousMemberCount, eventCount,
    firstSeen, lastSeen} — 이라 dict 유지, kv 요약이 소화한다.
    응답의 groupBy 에코는 서버가 실제 적용한 집계 기준 고지용."""

    group_by: str | None = None
    scope: str | None = None  # "brand" 에코(2026-08-06) — 구버전(전역) 응답 오독 차단
    rows: list[dict] = Field(default_factory=list)


# ── I-38 고객 행동 피처 집계 (노션 2026-08-10 확정 / BE #582 실장, 이슈 #592) ──


class SellerCustomerFeatureRow(SellerAggregateModel):
    """I-38 rows[] 1건 — `SellerCustomerFeaturesResponse.Row`(jarvis-back) 1:1 대응.

    실측(`SellerAnalyticsService.toCustomerRow`): 전 필드가 `long`/`String` primitive라
    BE가 null을 보내지 않는다 — `str=""`/`int=0` 기본값 위장 문제(02-DATA-SOURCES §0.1 ③)의
    대상이 아니다. 그래도 파싱 실패 시 도구 전체가 degrade하는 사고를 피하려면 필드는
    required로 선언하고(결측이면 즉시 ValidationError로 드러나야 한다), 기본값을 주지 않는다.

    `customerLabel`은 I-14·I-16과 동일한 브랜드 스코프 HMAC — memberId·IP는 어떤 필드로도
    내려가지 않는다(CustomerLabeler.label, jarvis-back 실측).
    """

    customer_label: str
    sessions: int
    product_views: int
    cart_adds: int
    checkout_starts: int
    order_count: int
    cancel_count: int
    amount_bucket: str
    last_activity_days_ago: int
    first_seen_days_ago: int


class SellerCustomerFeaturesResult(SellerAggregateModel):
    """I-38 GET /internal/seller/{brandId}/customer-features 응답
    (`SellerAnalyticsService.customerFeatures` 실측 — 이슈 #592).

    `from`/`to` 에코는 다른 I-* 응답과 동일하게 `extra="allow"`로 흡수하고 별도 필드를
    선언하지 않는다(`from`이 파이썬 예약어라 별도 alias 처리가 필요해지는데, 다른 조회
    모델들도 echo 필드를 흡수만 하지 소비하지 않는 관행 — ChurnResult 등과 동일).

    `insufficientCohort=true`일 때 BE는 `rows=[]`를 내려보낸다(코호트 30명 미만 —
    `MIN_COHORT_SIZE`, jarvis-back 실측). "고객 없음"으로 오독 금지(노션 규약,
    02-DATA-SOURCES §2 최소 모집단 가드).
    `truncated=true`는 `totalCustomers > rowLimit`(1000, `CUSTOMER_ROW_LIMIT`)일 때 —
    활동량(이벤트 3종 합) 내림차순으로 절단(02 §2 "정렬" 절 실측).
    """

    total_customers: int
    row_limit: int
    truncated: bool
    insufficient_cohort: bool
    amount_buckets: list[str] = Field(default_factory=list)
    rows: list[SellerCustomerFeatureRow] = Field(default_factory=list)


# ── I-9 자사 상품 목록 (§4.5) ──


class SellerStockRow(CamelModel):
    """I-9 rows[].stocks[] 항목 (#524) — 에이전트가 I-11 에 넣을 optionId 의 원천.

    판매자용이라 품절(quantity 0) 옵션도 그대로 내려온다. option_id null 은 옵션 없는
    상품의 유일한 재고 행이다. 구(stockQuantity 단일) BE 는 이 배열을 보내지 않으므로
    수신은 관대하게 — 빈 목록이 기본값이다.
    """

    option_id: int | None = None
    option_name: str | None = None
    quantity: int = 0


class SellerProductRow(CamelModel):
    """I-9 rows[] 항목. originalPrice 는 구매자 SpringProduct.listPrice 와 필드명이 달라
    별도 모델로 유지한다(§2.4).

    `status` 는 `ON_SALE`|`HIDDEN` 만 온다 — `DELETED` 는 BE 가 목록에서 제외한다(§4.5).

    [#524] stocks 는 옵션별 재고 — BE PR B 배포 후에만 채워진다(그 전엔 빈 목록).
    stockQuantity 는 그 시점부터 "옵션 재고 합계(파생)" 로 의미가 바뀌지만 필드는 유지된다.
    빈 목록은 "옵션이 없다"가 아니라 **"BE 가 아직 구버전"** 이다(hitl._stocks_mode_ready).
    """

    product_id: int
    name: str
    price: int
    original_price: int | None = None
    stock_quantity: int = 0
    stocks: list[SellerStockRow] = Field(default_factory=list)
    status: str = "ON_SALE"  # ON_SALE | HIDDEN
    displayed_sales_count: int | None = None
    category: str | None = None
    description: str | None = None
    image_url: str | None = None


class SellerProductList(CamelModel):
    """I-9 GET /internal/seller/{brandId}/products 응답.

    [이슈 #622] `total`(필터 적용 전체 건수)은 BE 가 이미 내려주고 있었다
    (`SellerProductInternalListResponse{rows, total}`) — 이 모델에 필드가 없어
    pydantic 이 조용히 버렸을 뿐이다. `hitl._find_product` 가 페이지 순회 종료를
    정확히 판단하는 데 쓴다(상한 도달 vs 진짜 끝).
    """

    rows: list[SellerProductRow] = Field(default_factory=list)
    total: int = 0


# ── I-10/I-11/I-12 상품 쓰기 (§4.5, product_agent 전용, HITL 승인 후 호출) ──


class StockEntry(CamelModel):
    """I-10/I-11 요청 `stocks[]` 한 줄 (#524) — 옵션 없는 상품은 optionId null 한 줄.

    quantity 음수는 BE 422 INVALID_STOCK — 여기서 ge=0 으로 선차단한다. optionId 가
    그 상품의 옵션이 아니어도 INVALID_STOCK 인데, 그 검증은 hitl 이 I-9 stocks 로
    선해소하므로(resolve_stock_option) 정상 경로에서는 발생하지 않는다.

    직렬화 주의: 클라이언트가 exclude_none 으로 본문을 만들므로 optionId null 은
    **키 자체가 빠져** 나간다 — Jackson 은 키 누락을 null 로 바인딩하니(record) 계약상
    "optionId: null 한 줄"과 동등하다. BE 가 키 존재를 강제하게 되면 여기를 고친다.
    """

    option_id: int | None = None
    quantity: int = Field(ge=0)


class ProductCreate(CamelModel):
    """I-10 POST 요청 본문 — name/price/stockQuantity/categoryId 필수(price ≤ originalPrice).

    [2026-08-09 정정] `category`(자유 문자열) → `categoryId`(Long). BE
    `SellerProductCreateRequest` 는 `categoryId: Long` 만 받고 소분류(leaf)인지까지
    검증한다(`Category.isRoot()` → PRODUCT_CATEGORY_INVALID). 구 구현이 보내던
    `category` 키는 BE 가 조용히 버렸고, 남은 categoryId 누락으로 등록이 항상
    실패했다 — 판매자에게는 "등록 중 오류"로만 보였다.

    필수지만 타입은 `int | None` 이다: 누락은 여기서 500 을 만들지 말고 상위
    (hitl.validate_draft)가 되묻기로 전환해야 한다. 전송은 exclude_none 이므로
    None 이면 키 자체가 빠지고 BE 가 422 MISSING_FIELD 로 응답한다.
    """

    name: str
    price: int
    original_price: int | None = None
    # [#524 듀얼모드] 재고는 wire_mode 에 따라 정확히 한 필드만 채운다(다른 쪽 None →
    # exclude_none 으로 본문에서 빠진다). quantity 모드=stock_quantity(현행 BE),
    # stocks 모드=stocks(PR B 이후). 등록 시점엔 옵션이 없어 stocks 는 optionId null 한 줄.
    stock_quantity: int | None = Field(default=None, ge=0)
    stocks: list[StockEntry] | None = None
    category_id: int | None = None
    description: str | None = None
    image_url: str | None = None


class ProductUpdate(CamelModel):
    """I-11 PATCH 요청 본문 — 바꿀 필드만(전 필드 Optional). 재고도 이 API로 통합.

    ⚠️ `category` 는 BE `SellerProductUpdateRequest` 에 **없는 필드**다(2026-08-09 실측,
    #620 재확인) — DTO 자체에 필드가 없어 Jackson 이 모르는 키로 버리므로 카테고리 수정
    요청은 조용히 무시된다. I-10 등록 시에만 정할 수 있는 값이라는 뜻이고, preview 의
    "카테고리는 등록 후 변경할 수 없습니다" 경고와도 일치한다. 그래서 이 스키마엔 애초에
    `category` 필드를 두지 않는다 — draft 단계(hitl.validate_draft)에서 update 초안에
    category 변경이 섞여 있으면 선차단한다. BE 가 필드를 열기 전까지는 수정 흐름에서
    카테고리를 다루려면 별도 이슈로 BE 를 먼저 연다."""

    name: str | None = None
    price: int | None = None
    original_price: int | None = None
    # [#524 듀얼모드] ProductCreate 와 동일 — 정확히 한 재고 필드만 채운다.
    # stocks 는 부분 수정이다: 배열에 실린 옵션만 갱신되고 나머지는 그대로다(05 §I-11).
    stock_quantity: int | None = Field(default=None, ge=0)
    stocks: list[StockEntry] | None = None
    description: str | None = None
    image_url: str | None = None
    status: str | None = None  # ON_SALE | HIDDEN — DELETED 는 BE 가 거부(삭제는 I-12 전용)


class ProductCreateResult(SellerAggregateModel):
    """I-10 201 응답 — {productId, status:"ON_SALE"}."""

    product_id: int
    status: str = "ON_SALE"


class ProductUpdateResult(SellerAggregateModel):
    """I-11 200 응답 — 갱신분(🔴 스키마 미확정, extra="allow"로 여분 필드 보존).

    `changes` 는 BE change-log 어휘 대문자 배열(PRICE/STOCK/STATUS만 — 로그 없는 필드
    변경은 미포함, SellerProductUpdateResponse 참조). 빈 배열은 "요청은 성공했지만
    실제로 바뀐 값이 없다"는 뜻(#620) — `_execute_draft` 가 이 경우 "이미 그 값이었어요"
    로 갈음한다."""

    product_id: int
    changes: list[str] = Field(default_factory=list)


class ProductDeleteResult(SellerAggregateModel):
    """I-12 200 응답 — soft delete, {productId, status:"DELETED"} (§4.5, 정본 Notion I-12).

    `HIDDEN`(숨김·판매정지)이 아니다 — 숨김은 판매자 목록에 남고 삭제는 목록에서도 빠진다.
    """

    product_id: int
    status: str = "DELETED"


# ── I-29/I-30/I-31 판매자 주문·리뷰 (§4.18~§4.20, 이슈 #297 — 🔶 초안, BE 협의 전) ──
#
# 전부 초안 — Spring 실측 전이라 응답 모델은 SellerAggregateModel(extra="allow")로
# 필드 유동을 흡수한다(파싱 실패로 도구가 죽지 않도록). 확정 시 이 섹션만 조인다.


class SellerOrderItemRow(SellerAggregateModel):
    """I-29 rows[].items[] 항목 — 자사 아이템만(타사 이름·금액 미노출, S-2 규칙 상속).

    orderItemId·현재 status 가 I-30 발송 대상 해소의 키다(§4.18)."""

    order_item_id: int
    product_id: int | None = None
    name: str = ""
    option_name: str | None = None
    quantity: int = 0
    price: int = 0
    status: str = ""  # 아이템 상태기계 어휘(ORDERED/SHIPPING/…)
    active_claim_status: str | None = None


class SellerOrderRow(SellerAggregateModel):
    """I-29 rows[] 항목 — S-2 주문 단위 파생 규칙 상속(대표 상태·자사 금액·orderNo 파생)."""

    order_id: int
    order_no: str = ""
    ordered_at: str = ""
    recipient_name: str = ""
    payment_method: str | None = None
    my_items_amount: int = 0
    status: str = ""  # 대표 상태(파생) — S-2 탭 어휘
    claim_status: str | None = None
    items: list[SellerOrderItemRow] = Field(default_factory=list)


class SellerOrderList(SellerAggregateModel):
    """I-29 GET /internal/seller/{brandId}/orders 응답 (§4.18).

    0건이어도 200 + 빈 rows·tabCounts 전부 0 — "주문 없음"은 정상 결과(I-14 규칙).
    orderId 직조회의 타사/미존재도 200 + 빈 rows(존재 은닉, 확정 2026-08-04)."""

    tab_counts: dict[str, int] = Field(default_factory=dict)
    rows: list[SellerOrderRow] = Field(default_factory=list)
    total: int = 0


class OrderItemStatusUpdate(CamelModel):
    """I-30 PATCH 요청 본문 — MVP 유효 toStatus 는 SHIPPING 뿐(§4.19)."""

    to_status: str
    reason: str | None = None


class OrderItemStatusResult(SellerAggregateModel):
    """I-30 200 응답 — {orderItemId, fromStatus, toStatus, changedAt}."""

    order_item_id: int
    from_status: str = ""
    to_status: str = ""
    changed_at: str = ""


class SellerReviewRow(SellerAggregateModel):
    """I-31 rows[] 항목 — VISIBLE 리뷰만(P-3 와 동일한 진실). authorNickname 은 공개 정보.

    [#518] content·authorNickname 은 **nullable**이다 — DDL 이 `content TEXT NULL` 이고
    별점만 남기는 리뷰가 실제로 존재한다. 구 `str = ""` 기본값은 키 **결측**만 흡수하고
    명시적 `null` 은 못 먹어서(pydantic 은 기본값과 무관하게 `str` 에 `None` 을 거부),
    rows 한 행만 content 가 null 이어도 `_validate` → ValidationError →
    SpringUnavailableError 로 **리뷰 조회 전체가** degrade 됐다. 부분 결측이 전량 실패로
    번지는 것을 막는 게 요지이며, `SellerAggregateModel` 의 `extra="allow"` 는 여분 필드만
    다루지 이 경로를 구제하지 못한다(#489 의 `extra="ignore"` 건과 층위가 다르다).

    표시 폴백은 여기서 하지 않는다 — 스키마는 와이어 값을 보존하고, 결측 문구는 tools 가
    정한다(#495 에서 churnRate 를 두고 세운 것과 같은 규약).
    """

    review_id: int
    product_id: int | None = None
    product_name: str = ""
    rating: int = 0
    content: str | None = None
    author_nickname: str | None = None
    created_at: str = ""


class SellerReviewList(SellerAggregateModel):
    """I-31 GET /internal/seller/{brandId}/reviews 응답 (§4.20, stats 미지정)."""

    rows: list[SellerReviewRow] = Field(default_factory=list)
    total: int = 0


class SellerReviewProductStat(SellerAggregateModel):
    """I-31 stats=true 의 byProduct[] 항목 — count 내림차순, 평균 소수 1자리."""

    product_id: int | None = None
    product_name: str = ""
    count: int = 0
    average_rating: float | None = None


class SellerReviewStats(SellerAggregateModel):
    """I-31 stats=true 응답 — 리뷰 0건이면 averageRating 은 0 이 아니라 null(I-16 규칙)."""

    total_count: int = 0
    average_rating: float | None = None
    distribution: dict[str, int] = Field(default_factory=dict)
    by_product: list[SellerReviewProductStat] = Field(default_factory=list)


# ── 7. 장바구니 삭제 · 찜 (이슈 #116·#117, I-24~I-28 — 확정 2026-08-05, Spring 구현됨) ──
# [#285] BE `jarvis-backend` main 실측(2026-08-08, BE PR #92·#93) — api-spec §4.12~4.16 v0.31.3.


class AddWishlistRequest(CamelModel):
    """I-26 POST /internal/wishlist 요청 본문(확정 2026-08-05).

    회원 전용(USER) — guestId 없음(게스트 찜은 없다). userId 는 AI-검증 JWT sub 유래
    (요청 본문 불신, §2.3과 동일 규약).
    """

    user_id: int
    product_id: int


class WishlistAddResult(CamelModel):
    """I-26 200 성공 응답(확정 2026-08-05) — {success, data:{productId}}, wishlistId 없음."""

    success: bool
    product_id: int | None = None


class WishlistItem(CamelModel):
    """I-28 GET /internal/wishlist 응답 항목(확정 2026-08-05).

    AI 가 실제로 쓰는 필드는 productId·name·purchaseState 세 개뿐이다(경로 B — SSE 에는 상품 카드를
    싣지 않는다). brandName·price·originalPrice·imageUrl·rating·reviewCount 같은 표시 필드는
    BE 응답에는 있어도 이 스키마에는 두지 않는다 — AI 가 쓰지 않는 필드까지 파싱·보존하면
    사용처 없는 결합만 늘어난다.

    `PurchaseState` 정의는 장바구니 조회(I-18) 절에 있다 — 두 계약이 같은 enum·같은 규칙을 쓴다.

    기본값 재검토(#310): PR #305 는 구 boolean 필드(기본값 참)와 의미를 맞추려 `"AVAILABLE"` 을
    뒀지만, 이제 이 값을 읽어 안내 문구를 가르므로 **기본값이 주장으로 승격**된다 — 키가
    없다는 사실을 "구매 가능이 확인됨"으로 바꿔 읽으면 품절 상품을 살 수 있다고 안내하게 된다.
    모름은 주장이 아니므로 `None` 으로 두고 소비 측이 아무 라벨도 붙이지 않는다.
    BE 가 이 세 값 밖의 상태를 추가했을 때의 파싱 견고성은 이 클래스가 아니라
    `app/services/spring_client.py::_parse_wishlist_items`(항목 단위 skip)가 책임진다 —
    상세 근거는 그쪽 docstring 하나에만 둔다(장바구니는 항목이 사라지면 안 돼 처방이 다르다).
    """

    product_id: int
    name: str | None = None  # BE 필드명은 name(productName 아님)
    purchase_state: PurchaseState | None = None


class WishlistView(CamelModel):
    """I-28 응답(확정 2026-08-05). 찜 0건도 200 + items:[](404 아님, get_cart 와 같은 규약)."""

    items: list[WishlistItem] = Field(default_factory=list)


# ── 8. 장바구니 수량 변경 (I-25, §4.13 — 확정 2026-08-05, Spring 구현됨 · AI 구현 착수 #285) ──


class ChangeCartQuantityRequest(CamelModel):
    """I-25 PATCH /internal/cart/items/{cartItemId} 요청 본문(확정 2026-08-05).

    quantity 하나뿐 — **1~99 치환값**(합산 아님)이다. "3개로 바꿔줘"류 발화가 이 계약을 쓰고,
    "하나 더 담아줘"류(합산)는 이 계약이 아니라 I-2(§4.1) 재호출이다 — 두 발화를 섞지 않는다.
    범위는 `decompose`(`CartIntent.quantity`)가 이미 1~99 로 클램프하지만, 어댑터에 들어오는
    값이 그 경로만은 아니므로 `add_to_cart`(`AddToCartRequest.quantity`)와 같은 관례로
    스키마에서도 강제한다.
    """

    quantity: int = Field(..., ge=1, le=99)


class ChangeCartQuantityResult(CamelModel):
    """I-25 200 성공 응답(확정 2026-08-05) — {success, data:{cartItemId, quantity}}.

    삭제(I-24)와 달리 **응답에 값이 있다** — `quantity` 가 BE 가 반영한 최종 수량이다(치환
    요청값과 같아야 정상이지만, AI 는 요청값이 아니라 이 응답값을 신뢰해 emit 한다).
    """

    success: bool
    cart_item_id: int | None = None  # 숫자(BIGINT, cart_item.id)
    quantity: int | None = None
