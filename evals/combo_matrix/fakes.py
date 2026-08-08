"""러너 전용 최소 fake — 배포 경로 함수는 그대로 부르고, 판정 규칙은 재구현하지 않는다.

decompose·rerank fake 는 `tests.integration._stubs.ScriptedLLM`(정본, 규약 §5)을 그대로 쓴다.
여기 있는 건 검색·push·인기상품·주문조회처럼 **네트워크로 나가는 콜러블**의 얇은 대역뿐이다
(주입 누락 시 로컬 Spring 이 뜬 상태에 따라 결과가 뒤집힌 전례 — `docs/lessons.md` 2026-08-05).

**검색 대역이 서는 자리(이슈 #426)** — `SpringWhereCatalogBackend` 는 `search_catalog` 를
대체하지 않고 그 **아래** `SearchBackend`(실제 네트워크 경계) 자리에 들어간다. 그래서 dedup·
`rating_min`·`attr_conditions` 사후필터는 배포 코드가 그대로 돈다. 대역이 `search_catalog` 를
통째로 삼키던 때는 그 판정들이 하네스에서 한 번도 실행되지 않아, 망가져도 관측이 안 변하는
검증 사각지대였다(그리고 대역이 손으로 재구현한 `rating_min` 은 앱과 의미가 반대였다).
"""

from __future__ import annotations

from app.schemas.spring import (
    OrderStatusSummary,
    ProductSearchFilters,
    ProductSearchResult,
    SpringProduct,
)
from app.services import search_service
from app.services.spring_client import OrderStatusUnavailableError, SpringUnavailableError

# [#426] `summary`·`attributes` 는 Spring I-1 의 WHERE 대상이다(api-spec §4.6 — keyword 는
# 상품명+summary+attributes LIKE, color 는 attributes LIKE). 이 두 필드가 비어 있어서
# keyword·color 축이 검색 경계에서 재지지 않았다(#381 잔여). 값은 실제 BE 페이로드 형태를
# 따른다 — attributes 축 이름은 category.attribute_schema 의 자유 배열이고 값은 bool·숫자도
# 온다(`app/schemas/spring.py` SpringProduct.attributes, api-spec §4.6 응답 예시 `{"방수": true}`).
CATALOG_PRODUCTS = [
    SpringProduct(
        product_id=101,
        name="무선 이어폰 A",
        summary="가벼운 착용감의 무선 이어폰. 방수 기능을 지원합니다.",
        attributes={"색상": "블랙", "방수": True},
        price=39000,
        rating=4.5,
        category="무선이어폰",
        brand="나이키",
    ),
    SpringProduct(
        product_id=102,
        name="무선 이어폰 B",
        summary="화이트 컬러의 무선 이어폰, 통화 품질이 우수합니다.",
        attributes={"색상": "화이트", "방수": False},
        price=48000,
        rating=4.2,
        category="무선이어폰",
        brand="아디다스",
    ),
    SpringProduct(
        product_id=103,
        name="무선 이어폰 C",
        summary="가벼운 그립감의 프리미엄 무선 이어폰",
        attributes={"색상": "블랙", "방수": True},
        price=89000,
        rating=3.9,
        category="무선이어폰",
        brand="나이키",
    ),
]


# INV/DIR 쌍 검증(이슈 #371) 전용 카탈로그 — 기존 `CATALOG_PRODUCTS` 3건 + 하드필터가 **실제로**
# 결과를 줄인다는 것을 보이기 위한 대조군. 필터를 태워도 건수가 안 줄면 DIR 쌍이 공허하게 통과한다.
#   · 104(여행용품) — `category` 하드필터용 대조군(#371).
#   · 105(3만원 미만) — `price_min` 하드필터용 대조군(#386). 재생성으로 DIR 쌍이 흔드는 축이
#     category 에서 price_min 으로 바뀌었는데, 기존 4건의 가격이 전부 3만원 이상이라
#     `price_min=30000` 을 태워도 4/4 가 그대로 통과했다(base=3, perturbed=3 — 실측 확인).
#     104 를 더한 것과 **같은 이유·같은 해법**이다.
#     `brand`·`rating` 은 일부러 다른 값으로 둔다 — 필터 8축이 전부 present 인 케이스가
#     product 101 하나만 남긴다는 기존 전제(테스트가 고정)를 이 상품이 흔들면 안 된다.
#     **`summary`·`attributes` 도 일부러 비워 둔다(#426)** — Spring I-1 의 keyword/color 는
#     LIKE 라 그 필드가 비면 매칭에서 빠지고(데이터 부재 → 제외), 그건 실제 계약 동작이다.
#     같은 "데이터 부재"를 AI 사후필터(`attr_conditions`)는 **보존**으로 처리한다(#100 P0) —
#     두 규약이 반대라는 사실 자체를 픽스처가 표현한다.
# `CATALOG_PRODUCTS`/`make_search` 의 **필터링 가능 필드(price·rating·category·brand)와 건수**는
# 기존 관측(observed)·기존 테스트의 전제라 건드리지 않는다. `summary`·`attributes` 추가(#426)는
# 그 전제를 건드리지 않는 순수 추가이고, 오히려 `make_popular()`(인기 폴백)가 이 리스트를 쓰므로
# **여기에 attributes 가 있어야 attr_conditions 사후필터가 실제로 후보를 좁힌다** — 의도된 공유다.
PAIR_CATALOG = [
    *CATALOG_PRODUCTS,
    SpringProduct(
        product_id=104,
        name="여행용 파우치",
        summary="가벼운 소재의 여행용 파우치, 방수 원단 사용",
        attributes={"색상": "블랙", "방수": True},
        price=30000,
        rating=4.0,
        category="여행용품",
        brand="트래블러",
    ),
    SpringProduct(
        product_id=105,
        name="무선 이어폰 D",
        price=19000,
        rating=3.8,
        category="무선이어폰",
        brand="스포츠몰",
    ),
]

# 대역이 흉내 낼 수 없는 하드필터 축(camelCase 노출명) — present 로 들어오면 그 호출에서
# **미적용으로 기록**하고(D1, 이슈 #381) 나머지 축만 적용해 계속한다.
#
# [#426] **지금은 비어 있다.** 8축 전부가 실제로 재진다 — Spring 와이어 축(keyword·category·
# price·brand·color)은 `SpringWhereCatalogBackend` 가 WHERE 계약으로 흉내 내고, AI 사후필터 축
# (rating_min·attr_conditions)은 배포 코드(`search_service.apply_ai_side_filters`)가 그대로 돈다.
# 기록 메커니즘 자체는 남긴다 — 미래에 대역이 표현 못 하는 축이 새로 생기면 여기 다시 적어
# "조용히 무시"가 아니라 데이터로 드러내는 자리다(그 성질은 테스트가 잠근다).
_UNREPRESENTABLE_FILTER_CAMEL: dict[str, str] = {}


def _norm_text(value: object) -> str:
    """LIKE 근사용 정규화 — 문자열화 + 양끝 공백 제거 + casefold(대소문자 무시)."""
    return str(value).strip().casefold()


def _attribute_haystack(product: SpringProduct) -> str:
    """`attributes` 값들을 이어붙인 LIKE 대상 — 축 이름(키)은 category 별 자유 배열이라 쓰지 않는다."""
    if not product.attributes:
        return ""
    return _norm_text(" ".join(str(value) for value in product.attributes.values()))


class SpringWhereCatalogBackend:
    """Spring I-1 검색의 **WHERE 계약**만 흉내 내는 `SearchBackend` 대역(고정 카탈로그).

    흉내 내는 범위는 `spring_client._search_query_params` 가 실제로 와이어에 싣는 축뿐이다 —
    `keyword`·`categoryName`·`minPrice`·`maxPrice`·`brandName`·`color`. LIKE 대상은 api-spec
    §4.6 규범 그대로다: keyword 는 상품명+summary+attributes, color 는 attributes.

    `rating_min`·`attr_conditions`·`exclude_product_ids` 는 **여기 없다** — Spring payload 축이
    아니라 AI 사후필터라(`spring_client.search_filter_axes` docstring) 배포 코드
    (`search_service.search_catalog` → `apply_ai_side_filters`)가 소유한다. 대역이 이 축을
    흉내 내면 앱과 같은 판정을 두 벌 갖게 되어 한쪽만 고쳐지는 드리프트가 난다(모듈 docstring).

    `evals/filter_axes/probe.py::LocalCatalogSearchBackend` 와 같은 계열이다 — app 코드 무수정으로
    `search_catalog` 가 받는 프로토콜 자리에 들어간다.
    """

    def __init__(self, products: list[SpringProduct]) -> None:
        self._products = products

    async def search(self, filters: ProductSearchFilters) -> ProductSearchResult:
        items = list(self._products)
        if filters.category is not None:
            items = [p for p in items if p.category == filters.category]
        if filters.price_min is not None:
            items = [p for p in items if p.price is not None and p.price >= filters.price_min]
        if filters.price_max is not None:
            items = [p for p in items if p.price is not None and p.price <= filters.price_max]
        if filters.brand:
            items = [p for p in items if p.brand in filters.brand]
        if filters.keyword:
            # BE I-1: 상품명+summary+attributes LIKE(api-spec §4.6, DDL D7 의 LIKE 2단은 DB 구현
            # 상세라 재현하지 않는다 — 관측 대상은 "AND 조건으로 후보가 줄어드는가"다).
            want = _norm_text(filters.keyword)
            items = [
                p
                for p in items
                if want in _norm_text(f"{p.name} {p.summary or ''}") + " " + _attribute_haystack(p)
            ]
        if filters.color:
            # BE I-1: attributes LIKE(#100 P1) — 상품명·summary 는 대상이 아니다.
            want = _norm_text(filters.color)
            items = [p for p in items if want in _attribute_haystack(p)]
        return ProductSearchResult(products=items, total_count=len(items))


class RecordingFilteringSearch:
    """search 콜러블이 실제로 받은 필터를 기록하고, 배포 `search_catalog` 를 그대로 태우는 대역.

    대역이 서는 자리는 `search_catalog` 가 아니라 그 아래 `SearchBackend`(= 실제 네트워크 경계)
    다 — 모듈 docstring 의 "배포 경로 함수는 그대로 부르고, 판정 규칙은 재구현하지 않는다"를
    지키는 유일한 위치다. 이 자리로 내리면(이슈 #426):

    - `rating_min`·`attr_conditions` 사후필터와 dedup 이 **배포 코드로 실제 실행**된다. 예전엔
      `search_catalog` 를 통째로 대체해 그 단계가 하네스에서 한 번도 돌지 않았고(= 그 판정이
      망가져도 관측이 안 변함), 대역이 손으로 재구현한 `rating_min` 은 앱과 의미가 달랐다
      (대역 `rating is not None and ...` vs 앱 "반증된 것만 제거", `search_service.py`).
    - 표현 불가 축이 사라져 `unapplied_calls` 는 빈 리스트가 된다. 기록 메커니즘은 남긴다
      (§ `_UNREPRESENTABLE_FILTER_CAMEL`).

    표현 불가 축이 들어와도 **던지지 않는다**는 #381 D1 결정은 그대로다 — ValueError 로 즉시
    실패시키던 구 동작은 앱의 검색 실패 처리에 삼켜져 "공허 통과 방지"가 아니라 **공허 통과를
    만들었다**(combo-0055 실측).
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
        return await search_service.search_catalog(
            filters,
            exclude_product_ids,
            backend=SpringWhereCatalogBackend(self._products),
        )


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
