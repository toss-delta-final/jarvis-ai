"""app/agents/seller/charts.py 좌표 조립 검증 (이슈 #504 — 14조합 소스 레지스트리).

실 Spring 없음 — set_spring_client 로 이중(double)을 끼운다(spring_client 규약).
pytest-asyncio 미의존 — asyncio.run 으로 실행한다(orchestrator 테스트와 동일 이식성).
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.agents.seller import charts as seller_charts
from app.agents.seller.charts import (
    CHART_SOURCES,
    CHART_TOP_PRODUCTS,
    CHART_X_LABEL_MAX,
    ChartUnavailable,
    build_charts,
)
from app.agents.seller.schemas import CHART_POINTS_MAX, ChartAxisPlan
from app.schemas.spring import (
    BehaviorEventsResult,
    BehaviorProductRow,
    SalesResult,
    SalesSeriesPoint,
    SellerProductList,
    SellerProductRow,
    SellerReviewProductStat,
    SellerReviewStats,
)
from app.services.spring_client import set_spring_client

_FROM = dt.date(2026, 7, 1)
_TO = dt.date(2026, 7, 31)


def _sales_point(day: str, sales: int = 0, orders: int = 0, qty: int | None = None):
    return SalesSeriesPoint(date=day, sales=sales, order_count=orders, sales_count=qty)


class _FakeSpring:
    """SpringClient 이중 — 차트 조립이 쓰는 조회 4종만 흉내 낸다. 호출을 기록한다."""

    def __init__(
        self,
        *,
        sales: SalesResult | Exception | None = None,
        events_product: BehaviorEventsResult | None = None,
        events_date: BehaviorEventsResult | None = None,
        events_type: BehaviorEventsResult | None = None,
        review_stats: SellerReviewStats | None = None,
        products: SellerProductList | None = None,
    ) -> None:
        self._sales = sales
        self._events = {
            "product": events_product,
            "date": events_date,
            "eventType": events_type,
        }
        self._review_stats = review_stats
        self._products = products
        self.calls: list[tuple] = []

    async def get_sales(self, brand_id, from_, to, granularity="daily"):
        self.calls.append(("get_sales", brand_id, from_, to, granularity))
        if isinstance(self._sales, Exception):
            raise self._sales
        assert self._sales is not None
        return self._sales

    async def get_events(
        self, brand_id, from_, to, event_type=None, product_id=None, group_by=None
    ):
        self.calls.append(("get_events", brand_id, from_, to, group_by))
        result = self._events.get(group_by or "product")
        assert result is not None
        return result

    async def get_review_stats(self, brand_id, *, from_=None, to=None, product_id=None):
        self.calls.append(("get_review_stats", brand_id, from_, to))
        assert self._review_stats is not None
        return self._review_stats

    async def list_products(self, brand_id, status=None, q=None, limit=None, offset=None):
        self.calls.append(("list_products", brand_id))
        assert self._products is not None
        return self._products


@pytest.fixture(autouse=True)
def _reset_spring_client():
    yield
    set_spring_client(None)


def _build(plans: list[ChartAxisPlan], client: _FakeSpring, date_from=_FROM, date_to=_TO):
    set_spring_client(client)  # type: ignore[arg-type]
    return asyncio.run(build_charts(plans, brand_id=3, date_from=date_from, date_to=date_to))


def _axis(x: str, y: str, title: str = "") -> ChartAxisPlan:
    return ChartAxisPlan(x_axis=x, y_axis=y, title=title)  # type: ignore[arg-type]


# ── 레지스트리 계약 ────────────────────────────────────────────────────────────


def test_registry_has_exactly_14_combos() -> None:
    """지원 축 조합은 정본이 확정한 14개다 — 늘리면 GRAPH_PROMPT·안내 문구도 함께 갱신."""
    assert len(CHART_SOURCES) == 14


def test_registry_attributes_match_contract() -> None:
    """정본 표의 unit·chartType·aggregate 확정값 — FE 헤더 분기의 근거."""
    assert CHART_SOURCES[("date", "sales")].unit == "KRW"
    assert CHART_SOURCES[("date", "sales")].chart_type == "line"
    assert CHART_SOURCES[("product", "avg_rating")].unit == "RATING"
    assert CHART_SOURCES[("product", "avg_rating")].aggregate == "avg"
    assert CHART_SOURCES[("product", "price")].aggregate == "none"
    assert CHART_SOURCES[("product", "price")].needs_period is False
    assert CHART_SOURCES[("product", "stock")].aggregate == "none"
    assert CHART_SOURCES[("rating", "review_count")].chart_type == "bar"
    assert CHART_SOURCES[("behavior_type", "event_count")].aggregate == "sum"


# ── 일자별 (I-6) — 제로필·버킷 ─────────────────────────────────────────────────


def test_date_sales_zero_fills_missing_days() -> None:
    """7월(31일) 요청 → 데이터가 이틀뿐이어도 정확히 31점, 빈 날은 y=0."""
    client = _FakeSpring(
        sales=SalesResult(
            series=[_sales_point("2026-07-01", sales=1000), _sales_point("2026-07-15", sales=500)]
        )
    )
    charts, unavailable = _build([_axis("date", "sales")], client)
    assert unavailable == []
    points = charts.charts[0].series[0].points
    assert len(points) == 31
    assert points[0].x == "07-01" and points[0].y == 1000
    assert points[1].y == 0  # 빈 날 제로필
    assert points[14].y == 500
    assert charts.charts[0].unit == "KRW" and charts.charts[0].aggregate == "sum"


def test_date_chart_buckets_long_periods() -> None:
    """92일 → 3일 버킷 31점(구간 시작일 라벨) / 365일 → 주 버킷 53점('MM-DD~' 라벨).

    포인트 상한 CHART_POINTS_MAX(60)를 어떤 기간에서도 넘지 않는다.
    """
    client = _FakeSpring(sales=SalesResult(series=[_sales_point("2026-05-01", sales=300)]))
    charts, _ = _build(
        [_axis("date", "sales")],
        client,
        date_from=dt.date(2026, 5, 1),
        date_to=dt.date(2026, 7, 31),
    )
    points = charts.charts[0].series[0].points
    assert len(points) == 31  # 92일 / 3일 버킷
    assert points[0].x == "05-01"
    assert "3일 단위" in charts.charts[0].summary

    client2 = _FakeSpring(sales=SalesResult(series=[_sales_point("2025-08-10", sales=300)]))
    charts2, _ = _build(
        [_axis("date", "sales")],
        client2,
        date_from=dt.date(2025, 8, 9),
        date_to=dt.date(2026, 8, 8),
    )
    points2 = charts2.charts[0].series[0].points
    assert len(points2) == 53  # 365일 / 주 버킷
    assert points2[0].x.endswith("~")
    assert len(points2) <= CHART_POINTS_MAX


def test_date_sales_quantity_null_is_no_data_not_zero() -> None:
    """[#489·#197] salesCount 가 전부 null 이면 0 으로 뭉개지 않고 no_data 다."""
    client = _FakeSpring(
        sales=SalesResult(series=[_sales_point("2026-07-01", sales=1000, qty=None)])
    )
    charts, unavailable = _build([_axis("date", "sales_quantity")], client)
    assert charts.charts == []
    assert [u.reason for u in unavailable] == ["no_data"]


def test_date_view_uses_events_date_series() -> None:
    """일자별 조회수 — I-13 groupBy=date series 의 productView 를 읽는다."""
    client = _FakeSpring(
        events_date=BehaviorEventsResult(
            group_by="date",
            series=[{"date": "2026-07-02", "productView": 42, "addToCart": 5}],
        )
    )
    charts, unavailable = _build([_axis("date", "view")], client)
    assert unavailable == []
    points = charts.charts[0].series[0].points
    assert points[1].x == "07-02" and points[1].y == 42
    assert ("get_events", 3, "2026-07-01", "2026-07-31", "date") in client.calls


# ── 상품별 — 상위 절단·라벨 절단·유일성 ────────────────────────────────────────


def _product_rows(n: int) -> BehaviorEventsResult:
    return BehaviorEventsResult(
        group_by="product",
        rows=[
            BehaviorProductRow(
                product_id=i,
                product_name=f"감귤청 프리미엄 대용량 {i}호",
                counts={"productView": 100 - i, "addToCart": 10},
                sales_quantity=100 - i,
            )
            for i in range(1, n + 1)
        ],
    )


def test_product_chart_truncates_to_top_15_and_notes_it() -> None:
    """상품 42개 → 상위 15개만, summary 에 절단 안내 — FE 추가 작업 0 규약."""
    client = _FakeSpring(events_product=_product_rows(42))
    charts, unavailable = _build([_axis("product", "sales_quantity")], client)
    assert unavailable == []
    chart = charts.charts[0]
    assert len(chart.series[0].points) == CHART_TOP_PRODUCTS
    assert chart.series[0].points[0].y == 99  # 내림차순 상위
    assert "상품 42개 중" in chart.summary and "15개만" in chart.summary
    assert chart.chart_type == "bar"


def test_product_labels_truncated_and_unique() -> None:
    """상품명 12자 절단 + 절단 충돌 시 id 접미로 x 유일성 보장(React key 계약)."""
    client = _FakeSpring(events_product=_product_rows(3))
    charts, _ = _build([_axis("product", "sales_quantity")], client)
    labels = [p.x for p in charts.charts[0].series[0].points]
    assert all(len(label) <= CHART_X_LABEL_MAX + 6 for label in labels)  # …·#id 접미 여유
    assert len(labels) == len(set(labels))  # 유일성
    assert labels[0].startswith("감귤청 프리미엄")


def test_product_rating_chart_uses_review_stats() -> None:
    """상품별 평균 평점 — I-31 stats by_product, unit=RATING·aggregate=avg."""
    client = _FakeSpring(
        review_stats=SellerReviewStats(
            total_count=10,
            average_rating=4.4,
            by_product=[
                SellerReviewProductStat(
                    product_id=1, product_name="감귤청", count=7, average_rating=4.6
                ),
                SellerReviewProductStat(
                    product_id=2, product_name="한라봉", count=3, average_rating=4.2
                ),
            ],
        )
    )
    charts, unavailable = _build([_axis("product", "avg_rating")], client)
    assert unavailable == []
    chart = charts.charts[0]
    assert chart.unit == "RATING" and chart.aggregate == "avg"
    assert [p.y for p in chart.series[0].points] == [4.6, 4.2]


def test_product_price_snapshot_notes_no_period() -> None:
    """가격 차트(I-9) — aggregate=none + 스냅샷 안내가 summary 에 들어간다."""
    client = _FakeSpring(
        products=SellerProductList(
            rows=[
                SellerProductRow(product_id=1, name="감귤청", price=12000),
                SellerProductRow(product_id=2, name="한라봉", price=15000),
            ]
        )
    )
    charts, unavailable = _build([_axis("product", "price")], client)
    assert unavailable == []
    chart = charts.charts[0]
    assert chart.aggregate == "none"
    assert "현재 시점" in chart.summary


# ── 별점별·행동 유형별 ─────────────────────────────────────────────────────────


def test_rating_distribution_fixed_five_buckets() -> None:
    """별점별 리뷰 수 — 1점~5점 고정 5칸, 빈 별점은 0."""
    client = _FakeSpring(
        review_stats=SellerReviewStats(
            total_count=10, average_rating=4.0, distribution={"5": 6, "4": 3, "1": 1}
        )
    )
    charts, unavailable = _build([_axis("rating", "review_count")], client)
    assert unavailable == []
    points = charts.charts[0].series[0].points
    assert [p.x for p in points] == ["1점", "2점", "3점", "4점", "5점"]
    assert [p.y for p in points] == [1, 0, 0, 3, 6]


def test_behavior_type_chart_four_labels() -> None:
    """행동 유형별 건수 — 정본 표기 4종(조회·장바구니·결제시작·구매) 고정 순서."""
    client = _FakeSpring(
        events_type=BehaviorEventsResult(
            group_by="eventType",
            counts={
                "productView": 100,
                "addToCart": 40,
                "checkoutStart": 20,
                "purchaseComplete": 10,
                "removeFromCart": 5,  # 차트 어휘 밖 — 싣지 않는다
            },
        )
    )
    charts, unavailable = _build([_axis("behavior_type", "event_count")], client)
    assert unavailable == []
    points = charts.charts[0].series[0].points
    assert [p.x for p in points] == ["조회", "장바구니", "결제시작", "구매"]
    assert [p.y for p in points] == [100, 40, 20, 10]


# ── 실패 사유 5종·부분 성공·중복 ───────────────────────────────────────────────


def test_unsupported_axes_lists_supported_charts() -> None:
    """other 선언(퍼널 등) → unsupported_axes, message 에 지원 목록이 들어간다."""
    client = _FakeSpring()
    charts, unavailable = _build([_axis("other", "other", title="퍼널 단계별 이탈률")], client)
    assert charts.charts == []
    assert unavailable[0].reason == "unsupported_axes"
    assert "퍼널 단계별 이탈률" in unavailable[0].message
    assert "일자별 매출" in unavailable[0].message  # 지원 목록 안내


def test_unregistered_combo_is_unsupported() -> None:
    """어휘로는 표현 가능하지만 레지스트리에 없는 조합(date×price)도 unsupported_axes."""
    client = _FakeSpring()
    charts, unavailable = _build([_axis("date", "price")], client)
    assert charts.charts == []
    assert unavailable[0].reason == "unsupported_axes"


def test_source_failure_degrades_single_chart_only() -> None:
    """Spring 실패는 그 차트만 source_failed — 다른 차트는 계속(부분 성공 공존)."""
    client = _FakeSpring(
        sales=RuntimeError("spring down"),
        events_type=BehaviorEventsResult(group_by="eventType", counts={"productView": 3}),
    )
    charts, unavailable = _build(
        [_axis("date", "sales"), _axis("behavior_type", "event_count")], client
    )
    assert [c.title for c in charts.charts] == ["행동 유형별 건수"]
    assert [u.reason for u in unavailable] == ["source_failed"]


def test_no_data_when_period_total_is_zero() -> None:
    """조회는 됐는데 기간 전체가 0건 → no_data(기간 확장 안내)."""
    client = _FakeSpring(sales=SalesResult(series=[]))
    charts, unavailable = _build([_axis("date", "sales")], client)
    assert charts.charts == []
    assert unavailable[0].reason == "no_data"
    assert "기간을 넓혀" in unavailable[0].message


def test_duplicate_axis_plans_collapse_and_share_fetch() -> None:
    """같은 조합 중복 선언은 1개로, 같은 소스(I-6)를 쓰는 두 차트는 조회 1회 공유."""
    client = _FakeSpring(
        sales=SalesResult(series=[_sales_point("2026-07-01", sales=1000, orders=2, qty=5)])
    )
    charts, unavailable = _build(
        [_axis("date", "sales"), _axis("date", "sales"), _axis("date", "order_count")], client
    )
    assert unavailable == []
    assert [c.title for c in charts.charts] == ["일별 매출 추이", "일별 주문 수 추이"]
    assert len([c for c in client.calls if c[0] == "get_sales"]) == 1  # 캐시 공유


def test_helper_messages_are_complete_sentences() -> None:
    """사유 helper — reason 어휘와 완성 문장(문구 소유권: charts.py) 계약."""
    assert seller_charts.chart_period_unclear("작년 여름").reason == "chart_period_unclear"
    assert "작년 여름" in seller_charts.chart_period_unclear("작년 여름").message
    assert seller_charts.agent_failed().reason == "agent_failed"
    assert seller_charts.source_failed().reason == "source_failed"
    assert isinstance(seller_charts.agent_failed(), ChartUnavailable)
