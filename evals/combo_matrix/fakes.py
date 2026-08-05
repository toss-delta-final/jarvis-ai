"""러너 전용 최소 fake — 배포 경로 함수는 그대로 부르고, 판정 규칙은 재구현하지 않는다.

decompose·rerank fake 는 `tests.integration._stubs.ScriptedLLM`(정본, 규약 §5)을 그대로 쓴다.
여기 있는 건 검색·push·인기상품·주문조회처럼 **네트워크로 나가는 콜러블**의 얇은 대역뿐이다
(주입 누락 시 로컬 Spring 이 뜬 상태에 따라 결과가 뒤집힌 전례 — `docs/lessons.md` 2026-08-05).
"""

from __future__ import annotations

from app.schemas.spring import OrderStatusSummary, ProductSearchResult, SpringProduct
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


async def map_categories_noop(*args, **kwargs):
    """카테고리 leg fan-out 은 이 매트릭스의 범위 밖(§ README 리스크) — 항상 매핑 없음.

    이 하네스는 `categoryQueries` 를 채우지 않으므로(§ runner.py `build_decompose_json`) 실제로는
    호출돼도 legs·unresolved 둘 다 빈 채로 반환하는 것이 맞다 — `CategoryMapping` 실계약을 그대로 쓴다.
    """
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    return CategoryMapping()
