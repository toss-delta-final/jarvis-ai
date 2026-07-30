"""Spring 역방향 호출 스키마 (api-spec v0.15.0 §4).

AI → Spring 역방향 계약 (모두 camelCase on the wire, Pydantic alias):
  1. search_products      — I-1, GET /internal/products/search (§4.6, 최우선 C-15; GET/POST 역제안 🔴)
  2. get_recent_purchases — I-19, GET /internal/members/{id}/orders (§4.7, C-6, dedup·프로필 소스)
  3. add_to_cart          — I-2, POST /internal/cart/items 단건 (§4.1, C-3)
  4. get_cart             — I-18, GET /internal/cart (§4.9, C-16)
  5. push_recommendations — I-21, POST /internal/recommendations, productIds (경로 B, §4.2, C-9)
  6. fetch_product_changes — I-17, GET /internal/products/changes (§4.8, C-4, AI 생성물 배치)
  (I-6/I-9 판매자 콜백 응답 스키마는 C-13/C-14 협의 후 확정 — dict 유지)

Python 속성은 snake_case, 직렬화는 by_alias=True 로 camelCase, 입력은 populate_by_name 으로 양쪽 허용.
[HARD] push 페이로드에는 표시 필드(price/image/reviewCount)를 넣지 않는다 — 추천 산출물만 (§4.2).

[변경 v0.6.0] 장바구니 구계약(CartItem items[] 다건 + reason 4종/GUEST_NOT_ALLOWED) 폐기
→ BE I-2 문서 채택: AddToCartRequest 단건(userId|guestId 본문 신원, optionId), 게스트 허용.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


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
    전량 반환하고, limit 은 search_catalog 가 top-K 절단에 쓴다.
    """

    category: str | None = None
    price_min: int | None = None
    price_max: int | None = None
    brand: list[str] | None = None
    rating_min: float | None = None
    keyword: str | None = None  # 상품명 LIKE — Spring I-1 와이어 파라미터
    # 의미검색용 자연어(#101) — AI 내부 필드. keyword(상품명 LIKE)와 분리되며 Spring 에 안 나가고
    # EmbeddingRerankBackend(방식2)가 pgvector 재정렬의 query 임베딩 입력으로 쓴다(§4.8 방식2).
    semantic_query: str | None = None
    color: str | None = None  # 색상 조건(#100 P1) — BE I-1 attributes LIKE 로 필터
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

    [#100 정합, #171] I-1 이 실제 반환하는 필드 = productId·name·summary·attributes·categoryName·
    brandName·price·rating·reviewCount. 별칭은 categoryName·brandName(to_camel 기본과 달라 명시
    별칭으로 덮는다 — 안 그러면 rerank 가 category/brand 를 None 으로 받는다).
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
    attributes: dict[str, object] | None = None
    price: int | None = None  # 판매가 — AI 계산용(예산·maxPrice·rerank, #100 P1), 표시 아님
    rating: float | None = None  # 조회 시 집계(DDL D9) — AI 계산용(평점필터·rerank, #100 P0)
    # 조회 시 집계 리뷰수 — AI 계산용(#171). rating 과 짝지어 '리뷰 없어 0'(review_count==0,
    # 데이터 부재)과 '리뷰 있고 저평점'을 가른다. None/미전송이면 rating 이 지배(구 동작 폴백).
    review_count: int | None = None
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


# ── 3. 장바구니 담기 (I-2, §4.1) — BE 문서 채택, 단건 ──


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


class AddToCartResult(CamelModel):
    """I-2 성공 응답 — {success, data:{cartItemId}} (api-spec §4.1).

    실패는 HTTP 오류로 옴: 400 CART_OPTION_REQUIRED(options 목록 → 되물음 멀티턴) /
    400 CART_OPTION_INVALID / 404 PRODUCT_NOT_FOUND / 401 INTERNAL_TOKEN_INVALID.
    SSE action.reason 매핑: PRODUCT_NOT_FOUND | STOCK_INSUFFICIENT | CART_ERROR (STOCK_INSUFFICIENT는
    BE CART_STOCK_INSUFFICIENT+availableStock 유래, 2026-07-22).
    """

    success: bool
    cart_item_id: int | None = None  # 숫자(BIGINT, cart_item.id)


# ── 4. 장바구니 조회 (I-9, §4.9, C-16) ──


class CartViewItem(CamelModel):
    """I-9 GET /internal/cart 응답 항목 — productName/optionName 은 챗 답변 생성 필수(🔴)."""

    cart_item_id: int  # 숫자(BIGINT, cart_item.id)
    product_id: int  # 숫자(BIGINT, product.id)
    product_name: str | None = None
    option_id: int | None = None  # 숫자(BIGINT, product_option.id)
    option_name: str | None = None
    quantity: int = 1
    price: int | None = None  # 표시가(선택, 총액 안내용 — 표시 권위는 Spring)


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
    """I-9 응답. 빈 장바구니는 items=[] 정상 200 (오류 아님, §4.9)."""

    items: list[CartViewItem] = Field(default_factory=list)


# ── 5. 추천 목록 push (I-21, §4.2, 경로 B) ──


class RecoReason(CamelModel):
    """I-21 추천 근거 항목 — Spring 이 Redis 저장 → CH-5 카드에 echo (§4.2 v0.15.15 확정, C-9).

    productId 로 키잉한다(순서 권위는 RecommendationPush.product_ids). rerank 가 산출한
    상품별 1문장 근거를 담으며, 근거를 생성하지 못한 상품은 생략(부분집합/순서무관 허용).
    생성 목표는 한글 40자 이내 1문장(rerank 프롬프트) — reason 은 판매자 입력 영향 자유 텍스트라
    push 전 그래프에서 개행 제거·안전 상한(config reason_max_len)으로 방어 정제한다. 표시 오버플로는 FE.
    """

    product_id: int  # 숫자(BIGINT, product.id) — internal 계약(§2.6)
    reason: str


class RecommendationPush(CamelModel):
    """I-21 POST /internal/recommendations 요청 (경로 B, api-spec §4.2, v0.15.0).

    최종 랭크 상품 id(Top-N)만 전달한다 — listId 는 FastAPI 가 생성해 넘기고(Spring 이 Redis 에
    이 키로 TTL 저장), FE 는 CH-5 GET /api/chat/lists/{listId} 로 카드를 조회한다.
    [변경 v0.15.0] 구 groups/items 구조 폐기. [확정 v0.15.15, BE 구현 07-18] reason 은 이 콜백에
    포함(reasons[{productId, reason}]) → Spring 이 CH-5 카드에 echo(§4.2). 선택 필드 — 근거 없는
    상품은 생략, 비면 [] 로 직렬화.
    productId 는 internal 계약이라 숫자(BIGINT, §2.6). 순서 유지 = 렌더 순서.
    노출 개수(Top-N)는 config(expose_min~expose_max)로 recommendation 그래프가 결정 —
    이 스키마는 전송 컨테이너라 하드 개수 대신 방어적 상한(max_length)만 둔다.
    상위는 이 콜백이 성공한 뒤에만 SSE products.ready 를 emit 한다(§3.3).
    """

    session_id: str
    list_id: str
    product_ids: list[int] = Field(
        default_factory=list, max_length=50
    )  # 방어적 상한(실제 개수는 config)
    reasons: list[RecoReason] = Field(default_factory=list, max_length=50)  # productId로 키잉(§4.2)


# ── 6. 상품 변경분 pull (I-17, §4.8, C-4) — AI 생성물 갱신 배치 ──


class ProductChange(CamelModel):
    """I-17 변경분 항목. status=HIDDEN 은 AI 생성물 삭제/비활성 트리거 (§4.8).

    콘텐츠 필드는 enrichment·search_doc 조립 입력 — AI 는 저장하지 않고 산출물 생성에만 사용.
    미정의 status 는 페이지 전체 계약 위반으로 거부해 해당 페이지 artifact·커서를 보존한다.
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
    is_anomaly: bool = False
    deviation_pct: float | None = None


class SalesResult(SellerAggregateModel):
    """I-6 GET /internal/seller/{brandId}/sales 응답 (매출 시계열)."""

    series: list[SalesSeriesPoint] = Field(default_factory=list)


# ── I-7 구매전환 퍼널 (§4.4) ──


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
    변환한다(count null = 0, checkout v1 미계산 구간). 평면 입력(테스트 스텁·구계약)도
    그대로 허용한다."""

    view: int = 0
    cart: int = 0
    checkout: int = 0
    purchase: int = 0

    @model_validator(mode="before")
    @classmethod
    def _flatten_stages(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("stages"), list):
            data = dict(data)
            for entry in data["stages"]:
                if not isinstance(entry, dict):
                    continue
                field = _FUNNEL_STAGE_FIELD.get(entry.get("stage"))
                if field is not None:
                    data[field] = entry.get("count") or 0
        return data


# ── I-13 행동 이벤트 집계 (§4.4 — 07/17 BE 확정 명세 반영, REALIGN F4/②-3) ──


class BehaviorProductRow(CamelModel):
    """I-13 groupBy=product rows[] 항목 — 상품별 행동 카운트·전환 보조 지표.

    counts 키는 event_type 의 camelCase(productView/addToCart/checkoutStart/
    purchaseComplete). viewToCartRate 는 addToCart/productView(분모 0 = null).
    uniqueVisitors = distinct(memberId, guestId) — 비로그인 게스트 포함.
    """

    product_id: int
    product_name: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    view_to_cart_rate: float | None = None
    unique_visitors: int | None = None


class BehaviorEventsResult(SellerAggregateModel):
    """I-13 GET /internal/seller/{brandId}/events 응답 (07/17 확정 — 구 events[] 폐기).

    원천 = behavior_events(상품 연계 4종만 — session_start/login/search/page_view 는
    브랜드 귀속 불가로 제외). groupBy 3형이 한 모델에 겹친다 — 채워지는 필드:
      - product(기본) : rows (+ total)
      - eventType     : counts
      - date          : series (date + camelCase 이벤트 카운트, 키 동적 → dict 유지)
    ⚠️ purchaseComplete 는 이벤트 기준(주문 완료 페이지 발사) — 매출·주문수의
    권위는 I-6/I-14(order 기준)다(명세 집계 규칙 — 워커 해석 주의).
    """

    group_by: str = "product"
    rows: list[BehaviorProductRow] = Field(default_factory=list)
    counts: dict[str, int] | None = None
    series: list[dict] = Field(default_factory=list)
    total: int | None = None


# ── I-14 주문 상태 전이/조회 (§4.4) ──


class OrderEventsResult(SellerAggregateModel):
    """I-14 GET /internal/seller/{brandId}/order-events 응답 — 필드 최소집합(🔴 확정 대기).

    구매자 fetch_product_changes(I-8·§4.8)와 무관한 별개 계약(혼동 금지, §4.4 주)."""

    events: list[dict] = Field(default_factory=list)
    stats: dict | None = None


# ── I-15 상품 변경 이력(판매자 감사 로그) (§4.4) ──


class ProductChangeLogResult(SellerAggregateModel):
    """I-15 GET /internal/seller/{brandId}/product-changes 응답 — 판매자 감사 로그.

    [혼동 금지] 구매자 ProductChangesPage(I-8 AI 생성물 배치, §4.8)와 다른 계약이다."""

    logs: list[dict] = Field(default_factory=list)


# ── I-16 이탈 코호트 (§4.4) ──


class ChurnResult(SellerAggregateModel):
    """I-16 GET /internal/seller/{brandId}/churn 응답 — churnRate/preChurnSignals."""

    churn_rate: float = 0.0
    pre_churn_signals: list[dict] = Field(default_factory=list)


# ── I-8 계정/보안 이벤트 집계 (전역, brandId 없음) (§4.4) ──


class AccountEventsResult(SellerAggregateModel):
    """I-8 GET /internal/account-events 응답 — 전역(브랜드 스코프 아님)·admin 소유 🔴."""

    events: list[dict] = Field(default_factory=list)


# ── I-9 자사 상품 목록 (§4.5) ──


class SellerProductRow(CamelModel):
    """I-9 rows[] 항목. originalPrice 는 구매자 SpringProduct.listPrice 와 필드명이 달라
    별도 모델로 유지한다(§2.4)."""

    product_id: int
    name: str
    price: int
    original_price: int | None = None
    stock_quantity: int = 0
    status: str = "ON_SALE"  # ON_SALE | HIDDEN
    displayed_sales_count: int | None = None
    category: str | None = None
    description: str | None = None
    image_url: str | None = None


class SellerProductList(CamelModel):
    """I-9 GET /internal/seller/{brandId}/products 응답."""

    rows: list[SellerProductRow] = Field(default_factory=list)


# ── I-10/I-11/I-12 상품 쓰기 (§4.5, product_agent 전용, HITL 승인 후 호출) ──


class ProductCreate(CamelModel):
    """I-10 POST 요청 본문 — name/price/stockQuantity 필수(price ≤ originalPrice)."""

    name: str
    price: int
    original_price: int | None = None
    stock_quantity: int = Field(0, ge=0)
    category: str | None = None
    description: str | None = None
    image_url: str | None = None


class ProductUpdate(CamelModel):
    """I-11 PATCH 요청 본문 — 바꿀 필드만(전 필드 Optional). 재고도 이 API로 통합."""

    name: str | None = None
    price: int | None = None
    original_price: int | None = None
    stock_quantity: int | None = Field(default=None, ge=0)
    category: str | None = None
    description: str | None = None
    image_url: str | None = None
    status: str | None = None  # ON_SALE | HIDDEN


class ProductCreateResult(SellerAggregateModel):
    """I-10 201 응답 — {productId, status:"ON_SALE"}."""

    product_id: int
    status: str = "ON_SALE"


class ProductUpdateResult(SellerAggregateModel):
    """I-11 200 응답 — 갱신분(🔴 스키마 미확정, extra="allow"로 여분 필드 보존)."""

    product_id: int


class ProductDeleteResult(SellerAggregateModel):
    """I-12 200 응답 — soft delete, {productId, status:"HIDDEN"}."""

    product_id: int
    status: str = "HIDDEN"
