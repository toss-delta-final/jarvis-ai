"""러너 전용 최소 fake — 배포 경로 함수는 그대로 부르고, 판정 규칙은 재구현하지 않는다.

decompose·rerank fake 는 `tests.integration._stubs.ScriptedLLM`(정본, 규약 §5)을 그대로 쓴다.
여기 있는 건 검색·push·인기상품·주문조회처럼 **네트워크로 나가는 콜러블**의 얇은 대역뿐이다
(주입 누락 시 로컬 Spring 이 뜬 상태에 따라 결과가 뒤집힌 전례 — `docs/lessons.md` 2026-08-05).
"""

from __future__ import annotations

from app.schemas.spring import (
    OrderStatusSummary,
    ProductSearchFilters,
    ProductSearchResult,
    SpringProduct,
)
from app.services.spring_client import OrderStatusUnavailableError, SpringUnavailableError

CATALOG_PRODUCTS = [
    SpringProduct(
        product_id=101,
        name="무선 이어폰 A",
        price=39000,
        rating=4.5,
        category="무선이어폰",
        brand="나이키",
    ),
    SpringProduct(
        product_id=102,
        name="무선 이어폰 B",
        price=48000,
        rating=4.2,
        category="무선이어폰",
        brand="아디다스",
    ),
    SpringProduct(
        product_id=103,
        name="무선 이어폰 C",
        price=89000,
        rating=3.9,
        category="무선이어폰",
        brand="나이키",
    ),
]


# INV/DIR 쌍 검증(이슈 #371) 전용 카탈로그 — 기존 `CATALOG_PRODUCTS` 3건 + category 가 다른 1건.
# category 하드필터를 추가했을 때 결과 수가 **실제로** 줄어드는 것을 보여주려면(combo-0054 DIR),
# 필터를 안 태우면 아무 의미 없는 검증이 된다 — 그래서 다른 category 값이 최소 1건 필요하다.
# `CATALOG_PRODUCTS`/`make_search` 는 기존 관측(observed) 데이터·기존 테스트의 전제라 건드리지 않는다.
PAIR_CATALOG = [
    *CATALOG_PRODUCTS,
    SpringProduct(
        product_id=104,
        name="여행용 파우치",
        price=30000,
        rating=4.0,
        category="여행용품",
        brand="트래블러",
    ),
]

# `make_recording_filtering_search`(아래)가 표현 가능한 하드필터 — SpringProduct 로 값 비교가
# 되는 축만이다. keyword(상품명 LIKE)·color(attributes LIKE)·attr_conditions(속성 매칭)는
# `SpringProduct` 에 대응 필드가 없어(§ fakes.py 모듈 docstring, 판정 로직 재구현 금지) 대역이
# 흉내 낼 수 없다 — present 로 들어오면 **미적용으로 기록**하고(D1, 이슈 #381) 표현 가능한
# 축만 적용해 계속한다(값 비교는 아래 camelCase 키로 노출).
_UNREPRESENTABLE_FILTER_CAMEL = {
    "keyword": "keyword",
    "color": "color",
    "attr_conditions": "attrConditions",
}


class RecordingFilteringSearch:
    """search 콜러블이 실제로 받은 필터를 기록하고, 표현 가능한 하드필터만 적용하는 대역.

    이것은 Spring I-1 검색(외부 시스템)의 WHERE 계약 대역이지 앱 판정 로직 재구현이 아니다 —
    `SpringProduct` 로 표현 가능한 하드필터(category 정확 일치·price_min/price_max 범위·brand
    목록 포함·rating_min 이상)만 흉내 낸다. 표현 불가 필터(keyword·color·attr_conditions)가
    present 로 들어오면 **던지지 않고**(#371 §3-d 의 이전 결정 — ValueError 로 즉시 실패 — 는
    앱의 검색 실패 처리에 삼켜져 "공허 통과 방지"가 아니라 **공허 통과를 만들었다**, combo-0055
    실측·이슈 #381 D1) `unapplied_calls` 에 그 호출에서 미적용된 축(camelCase)을 기록한 뒤,
    표현 가능한 축만 적용해 정상 결과를 돌려준다 — 관측을 살려 두고 미측정 축을 데이터로
    시끄럽게 드러내는 쪽이 앱 예외 경로에 삼켜지는 것보다 강하다.
    """

    def __init__(self, products: list[SpringProduct]) -> None:
        self._products = products
        self.calls: list[ProductSearchFilters] = []
        self.unapplied_calls: list[list[str]] = []

    async def __call__(
        self, filters: ProductSearchFilters, exclude_product_ids: list[int] | None = None
    ) -> ProductSearchResult:
        self.calls.append(filters)
        unapplied = [
            camel
            for name, camel in _UNREPRESENTABLE_FILTER_CAMEL.items()
            if getattr(filters, name, None) not in (None, "", {})
        ]
        self.unapplied_calls.append(unapplied)
        items = list(self._products)
        if filters.category is not None:
            items = [p for p in items if p.category == filters.category]
        if filters.price_min is not None:
            items = [p for p in items if p.price is not None and p.price >= filters.price_min]
        if filters.price_max is not None:
            items = [p for p in items if p.price is not None and p.price <= filters.price_max]
        if filters.brand:
            items = [p for p in items if p.brand in filters.brand]
        if filters.rating_min is not None:
            items = [p for p in items if p.rating is not None and p.rating >= filters.rating_min]
        return ProductSearchResult(products=items, total_count=len(items))


def make_recording_filtering_search(
    products: list[SpringProduct] = PAIR_CATALOG,
) -> RecordingFilteringSearch:
    return RecordingFilteringSearch(products)


def make_search(products: list[SpringProduct] = CATALOG_PRODUCTS):
    async def _search(filters, exclude_product_ids=None):
        return ProductSearchResult(products=list(products), total_count=len(products))

    return _search


async def failing_search(filters, exclude_product_ids=None):
    raise SpringUnavailableError("spring search 시간 초과(주입)")


def make_popular(products: list[SpringProduct] = CATALOG_PRODUCTS):
    async def _popular(size: int):
        return ProductSearchResult(products=list(products), total_count=len(products))

    return _popular


async def make_order_status_ok(user_id: int):
    """`fetch_order_status(user_id)` 계약(`app/agents/buyer/order_status.py:130`)을 그대로 따른다 —
    반환값은 `.orders` 속성이 있어야 하므로(`format_order_status`) 실제 스키마
    `OrderStatusSummary` 를 쓴다. 예전엔 무관한 dict({"orderId":...})를 돌려줬는데, 그 경로는
    `identity=guest` 케이스만 exercised 돼(로그인 게이트가 먼저 걸림) `summary.orders`
    AttributeError 가 한 번도 안 드러났다(리뷰 R8 대응 중 발견) — 빈 주문 목록으로 항상 유효하게.
    """
    return OrderStatusSummary(orders=[])


async def failing_order_status(user_id: int):
    raise OrderStatusUnavailableError("upstream_unavailable")


class RecordingPush:
    def __init__(self) -> None:
        self.pushes: list = []

    async def __call__(self, push) -> bool:
        self.pushes.append(push)
        return True


class RecordingExactMatchCategoryMapping:
    """`map_categories`(app/agents/buyer/graph.py `_map_categories`)의 규칙 ① raw exact match
    → raw 만 대역한다 — 임베딩 최근접·거리컷·택일·확장(expansion)은 재구현하지 않는다(#331 소관).

    이것은 pg taxonomy 조회(exact_lookup)의 경계 대역이지 매핑 판정 로직의 재구현이 아니다.
    이슈 #371 R1 결정 — combo-0054 DIR 검증이 `category` 하드필터를 실제로 관측하려면
    `decision.category_legs` 가 최소 1개 채워져야 한다(canonical-or-null degrade,
    `app/agents/buyer/graph.py:520-537` — legs 가 비면 `filters.category` 는 무조건 None 이 된다).
    **#381 D5 로 `runner.py::_observe_chat` 도 이 대역을 쓴다** — `build_decompose_json` 이
    `category=="present"` 면 `categoryQueries` 를 채우므로(§ 아래 `map_categories_noop`) 일반
    관측 러너도 이제 이 exact-match 매핑이 실제로 legs 를 채워야 검색 경계에 도달한다. 두 러너
    (`runner.py`·`pair_runner.py`)가 이 대역 하나를 공유한다 — 갈라 두면 매핑 규칙이 드리프트한다.
    """

    def __init__(self, taxonomy: set[str]) -> None:
        self._taxonomy = taxonomy
        self.calls: list = []

    async def __call__(self, *, category_queries=(), **_kwargs):
        from app.agents.buyer.recommendation.category_mapping import CategoryMapping

        legs: list[tuple[str, str | None]] = []
        unresolved: list[str] = []
        for cq in category_queries:
            raw = cq.raw_category
            if raw and raw in self._taxonomy:
                legs.append((raw, cq.query))
            elif raw or cq.query:
                unresolved.append(raw or cq.query)
        mapping = CategoryMapping(legs=legs, unresolved=unresolved)
        self.calls.append(mapping)
        return mapping


def make_exact_match_category_mapping(
    taxonomy: set[str] | None = None,
) -> RecordingExactMatchCategoryMapping:
    resolved_taxonomy = (
        {p.category for p in PAIR_CATALOG if p.category} if taxonomy is None else taxonomy
    )
    return RecordingExactMatchCategoryMapping(resolved_taxonomy)


async def map_categories_noop(*args, **kwargs):
    """항상 빈 매핑(legs·unresolved 둘 다 없음)을 돌려주는 대역.

    **#381 D5 이후 유일한 용도는 `runner.py::_warm_up_last_reco` 의 웜업 턴이다** — 그 턴의
    decompose 는 `{"intent": "recommend", ..., "filters": {"keyword": "무선 이어폰"}}` 하드코딩
    dict 라 `categoryQueries` 가 애초에 없다(category 축과 무관한 워밍업 목적, cart_add/
    wishlist_add 관측 전에 last_reco 만 채우면 된다). 그래서 어떤 category 매핑 대역을 써도
    legs·unresolved 는 항상 빈 채로 나온다 — `RecordingExactMatchCategoryMapping` 을 대신 써도
    결과가 같지만, "이 턴은 category 축을 관측하지 않는다"는 의도를 이름으로 드러내려고 이
    전용 noop 을 그대로 둔다. 관측 대상 턴(`_observe_chat` 본체)은 `categoryQueries` 를 채울 수
    있어(`build_decompose_json`) `RecordingExactMatchCategoryMapping` 을 쓴다(§ 위 클래스 docstring).
    """
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    return CategoryMapping()
