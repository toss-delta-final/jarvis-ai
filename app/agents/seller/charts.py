"""판매자 분석 차트 조립 — 14조합 소스 레지스트리 (이슈 #504, DESIGN-SELLER-CHART-V2).

좌표 생성 주체가 LLM → 코드로 바뀐 지점이다: graph_agent 는 축 선언(ChartAxisPlan)까지만
하고, 이 모듈이 그 선언으로 Spring(I-6 매출·I-13 행동·I-9 상품·I-31 리뷰)을 직접 호출해
좌표를 조립한다. 구 구조(LLM 이 finding 요약에서 숫자를 옮겨 적고 G1 이 근거 없는 수치를
드랍)는 실데이터 좌표를 만들 경로가 없어 charts 가 상시 비었다 — 구 결정 D-4 폐기.

규칙(정본: 노션 「판매자 분석 차트 재설계 (08/08)」 · api-spec §3.2):
- 데이터 없는 날도 y=0 으로 채운다(중간이 비어 x축이 뭉개지지 않게) — 단 기간 전체가
  0 이면 no_data 로 안내한다.
- 기간이 길면 버킷으로 묶는다(일→3일→주) — 포인트 상한 CHART_POINTS_MAX(60) 준수.
- 상품축은 상위 CHART_TOP_PRODUCTS(15)개 절단, x 라벨은 CHART_X_LABEL_MAX(12)자 절단.
  절단·버킷·스냅샷 안내는 전부 summary 에 싣는다(FE 추가 작업 0).
- x 값 유일성은 서버가 보장한다(FE React key 충돌 방지).
- nullable 수치(salesCount·salesQuantity, #489)는 0 으로 뭉개지 않는다 — 값이 전혀
  없으면 no_data 다(#197 silent-mismatch 재발 방지).
- chartUnavailable.message 는 **완성된 한국어 문장**이며 문구 소유권은 이 모듈이다 —
  FE 는 reason 으로 분기하지 않고 message 를 그대로 렌더한다(지원 축이 늘 때 FE 배포
  대기 없음).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from app.agents.seller.schemas import (
    CHART_MAX,
    ChartAxisPlan,
    ChartPoint,
    ChartSeries,
    ChartSet,
    ChartSpec,
)
from app.services.spring_client import get_spring_client

logger = logging.getLogger(__name__)

# 와이어 규약 상수 — 계약값이라 Settings 가 아닌 상수다(schemas.CHART_MAX 와 동일 원칙).
CHART_TOP_PRODUCTS = 15  # 상품축 상위 절단 개수
CHART_X_LABEL_MAX = 12  # x 라벨 최대 길이(자) — FE 도 truncate 하지만 서버가 1차 절단

# 행동 유형별 차트의 x 라벨·순서(정본 표기: 조회 → 장바구니 → 결제시작 → 구매).
# I-13 counts 키는 5종(removeFromCart 포함)이지만 차트 어휘는 정본이 4종으로 확정했다.
_BEHAVIOR_TYPE_LABELS: tuple[tuple[str, str], ...] = (
    ("productView", "조회"),
    ("addToCart", "장바구니"),
    ("checkoutStart", "결제시작"),
    ("purchaseComplete", "구매"),
)

# unsupported_axes 안내에 싣는 지원 목록 — 레지스트리와 함께 갱신한다.
_SUPPORTED_SUMMARY = (
    "일자별 매출·판매 수량·주문 수·조회수·장바구니, "
    "상품별 판매 수량·조회수·장바구니·리뷰 수·평균 평점·가격·재고, "
    "별점별 리뷰 수, 행동 유형별 건수"
)


@dataclass(frozen=True)
class ChartUnavailable:
    """차트를 만들지 못한 사유 — `report.data.chartUnavailable[]` 직렬화 재료.

    reason 은 로깅·QA 용 개방형 어휘(no_data / unsupported_axes / chart_period_unclear /
    source_failed / agent_failed), message 는 FE 가 그대로 렌더하는 완성 문장이다.
    """

    reason: str
    message: str


def unsupported_axes(requested: str) -> ChartUnavailable:
    """지원 밖 축 조합 안내 — 판매자가 '그릴 수 있는 것'을 알아야 말을 바꿀 수 있다."""
    label = requested.strip() or "요청하신 그래프"
    return ChartUnavailable(
        reason="unsupported_axes",
        message=(
            f"'{label}'은(는) 그래프로 만들 수 없습니다. "
            f"현재 그릴 수 있는 것은 {_SUPPORTED_SUMMARY}입니다."
        ),
    )


def chart_period_unclear(expr: str) -> ChartUnavailable:
    """그래프 기간 해석 불가 안내 — 보고서는 살리고 차트만 생략할 때 쓴다(#504)."""
    label = expr.strip() or "요청하신 기간"
    return ChartUnavailable(
        reason="chart_period_unclear",
        message=(
            f"그래프 기간 '{label}'을(를) 해석하지 못해 차트를 만들지 못했습니다. "
            "기간 표현을 바꿔 다시 요청해 주세요."
        ),
    )


def source_failed() -> ChartUnavailable:
    """Spring 조회 실패 안내."""
    return ChartUnavailable(
        reason="source_failed",
        message="차트 데이터를 조회하지 못해 그래프를 만들지 못했습니다. 잠시 후 다시 요청해 주세요.",
    )


def agent_failed() -> ChartUnavailable:
    """차트 기획(LLM) 실패·타임아웃 안내."""
    return ChartUnavailable(
        reason="agent_failed",
        message="차트 구성을 준비하지 못했습니다. 잠시 후 다시 요청해 주세요.",
    )


def _no_data(title: str, *, snapshot: bool = False) -> ChartUnavailable:
    if snapshot:
        message = f"'{title}': 등록된 상품이 없어 그래프를 만들지 못했습니다."
    else:
        message = (
            f"'{title}': 해당 기간에 표시할 데이터가 없어 그래프를 만들지 못했습니다. "
            "기간을 넓혀 다시 요청해 주세요."
        )
    return ChartUnavailable(reason="no_data", message=message)


# ── 소스 레지스트리 — (x_axis, y_axis) → 차트 속성 (14조합의 단일 출처) ──────────


@dataclass(frozen=True)
class ChartSource:
    """축 조합 1개의 조립 속성 — unit·chart_type·aggregate 는 코드가 정한다(LLM 무관여)."""

    title: str
    unit: str  # KRW | COUNT | RATING
    chart_type: str  # line | bar
    aggregate: str  # sum | avg | none
    needs_period: bool  # False = 현재 시점 스냅샷(가격·재고) — chartPeriod 미적용
    y_label: str  # 계열 이름(범례)


CHART_SOURCES: dict[tuple[str, str], ChartSource] = {
    # 일자별 (I-6 · I-13 date 그룹) — line · sum
    ("date", "sales"): ChartSource("일별 매출 추이", "KRW", "line", "sum", True, "매출"),
    ("date", "sales_quantity"): ChartSource(
        "일별 판매 수량 추이", "COUNT", "line", "sum", True, "판매 수량"
    ),
    ("date", "order_count"): ChartSource(
        "일별 주문 수 추이", "COUNT", "line", "sum", True, "주문 수"
    ),
    ("date", "view"): ChartSource("일별 조회수 추이", "COUNT", "line", "sum", True, "조회수"),
    ("date", "cart"): ChartSource("일별 장바구니 추이", "COUNT", "line", "sum", True, "장바구니"),
    # 상품별 (I-13 product 그룹 · I-31 stats · I-9) — bar
    ("product", "sales_quantity"): ChartSource(
        "상품별 판매 수량", "COUNT", "bar", "sum", True, "판매 수량"
    ),
    ("product", "view"): ChartSource("상품별 조회수", "COUNT", "bar", "sum", True, "조회수"),
    ("product", "cart"): ChartSource("상품별 장바구니", "COUNT", "bar", "sum", True, "장바구니"),
    ("product", "review_count"): ChartSource(
        "상품별 리뷰 수", "COUNT", "bar", "sum", True, "리뷰 수"
    ),
    ("product", "avg_rating"): ChartSource(
        "상품별 평균 평점", "RATING", "bar", "avg", True, "평점"
    ),
    ("product", "price"): ChartSource("상품별 가격", "KRW", "bar", "none", False, "가격"),
    ("product", "stock"): ChartSource("상품별 재고", "COUNT", "bar", "none", False, "재고"),
    # 별점별 (I-31 stats distribution) — bar · sum
    ("rating", "review_count"): ChartSource(
        "별점별 리뷰 수", "COUNT", "bar", "sum", True, "리뷰 수"
    ),
    # 행동 유형별 (I-13 eventType 그룹) — bar · sum
    ("behavior_type", "event_count"): ChartSource(
        "행동 유형별 건수", "COUNT", "bar", "sum", True, "건수"
    ),
}


# ── 버킷·라벨 유틸 ────────────────────────────────────────────────────────────


def _bucket_days(total_days: int) -> int:
    """기간 길이 → 버킷 크기(일). 포인트 상한 60 을 넘지 않게 일→3일→주로 넓힌다.

    정본 예시와 일치: 31일→1일 31점 / 92일→3일 31점 / 365일→1주 53점.
    """
    if total_days <= 60:
        return 1
    if total_days <= 180:
        return 3
    return 7


def _bucket_label(start: date, bucket: int) -> str:
    """버킷 x 라벨 — 1·3일 버킷은 구간 시작일 'MM-DD', 주 버킷은 'MM-DD~'(정본 표기)."""
    label = f"{start.month:02d}-{start.day:02d}"
    return f"{label}~" if bucket >= 7 else label


def _bucket_starts(date_from: date, date_to: date, bucket: int) -> list[date]:
    starts: list[date] = []
    cursor = date_from
    while cursor <= date_to:
        starts.append(cursor)
        cursor += timedelta(days=bucket)
    return starts


def _parse_iso_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _bucketize(
    daily: dict[date, int | float], date_from: date, date_to: date, bucket: int
) -> list[ChartPoint]:
    """일 단위 값 → 버킷 합산 좌표. 값이 없는 구간도 y=0 으로 채운다(x축 뭉개짐 방지)."""
    points: list[ChartPoint] = []
    for start in _bucket_starts(date_from, date_to, bucket):
        end = min(start + timedelta(days=bucket - 1), date_to)
        total: float = 0
        cursor = start
        while cursor <= end:
            total += daily.get(cursor, 0)
            cursor += timedelta(days=1)
        points.append(ChartPoint(x=_bucket_label(start, bucket), y=float(total)))
    return points


def _truncate_label(text: str) -> str:
    if len(text) <= CHART_X_LABEL_MAX:
        return text
    return f"{text[:CHART_X_LABEL_MAX]}…"


def _product_label(name: str | None, product_id: int, used: set[str]) -> str:
    """상품 x 라벨 — 12자 절단 + 유일성 보장(절단 충돌 시 상품 id 접미)."""
    base = (name or "").strip() or f"상품 {product_id}"
    label = _truncate_label(base)
    if label in used:
        label = f"{label}#{product_id}"
    used.add(label)
    return label


def _period_summary(date_from: date, date_to: date, y_label: str, bucket: int) -> str:
    base = f"{date_from.isoformat()}~{date_to.isoformat()} 기간의 {y_label}입니다."
    if bucket == 3:
        return f"{base} 3일 단위로 묶어 표시했습니다."
    if bucket >= 7:
        return f"{base} 1주 단위로 묶어 표시했습니다."
    return base


def _top_summary(total: int, shown: int, y_label: str) -> str:
    if total > shown:
        return f"상품 {total}개 중 {y_label} 상위 {shown}개만 표시했습니다."
    return f"상품 {shown}개의 {y_label}입니다."


def _spec(source: ChartSource, title: str, points: list[ChartPoint], summary: str) -> ChartSpec:
    return ChartSpec(
        title=title or source.title,
        chart_type=source.chart_type,
        unit=source.unit,
        aggregate=source.aggregate,
        series=[ChartSeries(label=source.y_label, points=points)],
        summary=summary,
    )


# ── 조립기 — x축별 (조회 결과는 _FetchCache 로 차트 간 공유) ─────────────────────


class _FetchCache:
    """한 번의 build_charts 안에서 같은 Spring 조회를 두 번 하지 않기 위한 캐시.

    예: date×sales + date×sales_quantity 는 둘 다 I-6 한 번이면 된다(AI→Spring 3s
    타임아웃 예산 절약). 캐시는 요청 스코프 — 인스턴스를 넘겨 쓰고 버린다.
    """

    def __init__(self) -> None:
        self._store: dict[str, object] = {}

    async def get(self, key: str, fetch) -> object:
        if key not in self._store:
            self._store[key] = await fetch()
        return self._store[key]


async def _build_date_chart(
    plan: ChartAxisPlan,
    source: ChartSource,
    *,
    brand_id: int,
    date_from: date,
    date_to: date,
    cache: _FetchCache,
) -> ChartSpec | ChartUnavailable:
    client = get_spring_client()
    from_s, to_s = date_from.isoformat(), date_to.isoformat()
    title = plan.title or source.title
    daily: dict[date, int | float] = {}

    if plan.y_axis in ("sales", "sales_quantity", "order_count"):
        result = await cache.get(
            "sales", lambda: client.get_sales(brand_id, from_s, to_s, granularity="daily")
        )
        has_value = False
        for point in result.series:
            day = _parse_iso_date(point.date)
            if day is None:
                continue
            if plan.y_axis == "sales":
                value = point.sales
            elif plan.y_axis == "order_count":
                value = point.order_count
            else:
                # [#489] salesCount 는 nullable — None 을 0 으로 뭉개지 않는다(#197).
                value = point.sales_count
            if value is None:
                continue
            has_value = True
            daily[day] = daily.get(day, 0) + value
        if not has_value or sum(daily.values()) == 0:
            return _no_data(title)
    else:  # view | cart — I-13 date 그룹(series: date + camelCase 이벤트 카운트)
        result = await cache.get(
            "events_date",
            lambda: client.get_events(brand_id, from_s, to_s, group_by="date"),
        )
        key = "productView" if plan.y_axis == "view" else "addToCart"
        for row in result.series:
            day = _parse_iso_date(str(row.get("date", "")))
            if day is None:
                continue
            value = row.get(key)
            if value is None:
                continue
            daily[day] = daily.get(day, 0) + int(value)
        if not daily or sum(daily.values()) == 0:
            return _no_data(title)

    bucket = _bucket_days((date_to - date_from).days + 1)
    points = _bucketize(daily, date_from, date_to, bucket)
    return _spec(source, title, points, _period_summary(date_from, date_to, source.y_label, bucket))


async def _build_product_chart(
    plan: ChartAxisPlan,
    source: ChartSource,
    *,
    brand_id: int,
    date_from: date,
    date_to: date,
    cache: _FetchCache,
) -> ChartSpec | ChartUnavailable:
    client = get_spring_client()
    from_s, to_s = date_from.isoformat(), date_to.isoformat()
    title = plan.title or source.title
    rows: list[tuple[int, str | None, float]] = []  # (product_id, name, y)

    if plan.y_axis in ("sales_quantity", "view", "cart"):
        result = await cache.get(
            "events_product",
            lambda: client.get_events(brand_id, from_s, to_s, group_by="product"),
        )
        for row in result.rows:
            if plan.y_axis == "sales_quantity":
                value = row.sales_quantity  # nullable(#489) — None 은 데이터 없음
            elif plan.y_axis == "view":
                value = (row.counts or {}).get("productView")
            else:
                value = (row.counts or {}).get("addToCart")
            if value is None:
                continue
            rows.append((row.product_id, row.product_name, float(value)))
    elif plan.y_axis in ("review_count", "avg_rating"):
        stats = await cache.get(
            "review_stats",
            lambda: client.get_review_stats(brand_id, from_=from_s, to=to_s),
        )
        for item in stats.by_product:
            value = item.count if plan.y_axis == "review_count" else item.average_rating
            if value is None:
                continue
            rows.append((item.product_id, item.product_name, float(value)))
    else:  # price | stock — I-9 현재 시점 스냅샷(기간 무관, chartPeriod 미적용)
        products = await cache.get("products", lambda: client.list_products(brand_id))
        for row in products.rows:
            value = row.price if plan.y_axis == "price" else row.stock_quantity
            rows.append((row.product_id, row.name, float(value)))
        if not rows:
            return _no_data(title, snapshot=True)

    if not rows or (source.aggregate == "sum" and sum(value for _, _, value in rows) == 0):
        return _no_data(title)

    rows.sort(key=lambda item: item[2], reverse=True)
    shown = rows[:CHART_TOP_PRODUCTS]
    used: set[str] = set()
    points = [
        ChartPoint(x=_product_label(name, product_id, used), y=value)
        for product_id, name, value in shown
    ]
    summary = _top_summary(len(rows), len(shown), source.y_label)
    if not source.needs_period:
        summary = f"{summary} 가격·재고는 기간과 무관한 현재 시점 값입니다."
    return _spec(source, title, points, summary)


async def _build_rating_chart(
    plan: ChartAxisPlan,
    source: ChartSource,
    *,
    brand_id: int,
    date_from: date,
    date_to: date,
    cache: _FetchCache,
) -> ChartSpec | ChartUnavailable:
    client = get_spring_client()
    from_s, to_s = date_from.isoformat(), date_to.isoformat()
    title = plan.title or source.title
    stats = await cache.get(
        "review_stats", lambda: client.get_review_stats(brand_id, from_=from_s, to=to_s)
    )
    distribution = stats.distribution or {}
    points = [
        ChartPoint(x=f"{star}점", y=float(distribution.get(str(star), 0))) for star in range(1, 6)
    ]
    if sum(point.y for point in points) == 0:
        return _no_data(title)
    summary = f"{date_from.isoformat()}~{date_to.isoformat()} 기간의 별점별 리뷰 수입니다."
    return _spec(source, title, points, summary)


async def _build_behavior_chart(
    plan: ChartAxisPlan,
    source: ChartSource,
    *,
    brand_id: int,
    date_from: date,
    date_to: date,
    cache: _FetchCache,
) -> ChartSpec | ChartUnavailable:
    client = get_spring_client()
    from_s, to_s = date_from.isoformat(), date_to.isoformat()
    title = plan.title or source.title
    result = await cache.get(
        "events_type",
        lambda: client.get_events(brand_id, from_s, to_s, group_by="eventType"),
    )
    counts = result.counts or {}
    points = [
        ChartPoint(x=label, y=float(counts.get(key, 0))) for key, label in _BEHAVIOR_TYPE_LABELS
    ]
    if sum(point.y for point in points) == 0:
        return _no_data(title)
    summary = f"{date_from.isoformat()}~{date_to.isoformat()} 기간의 행동 유형별 건수입니다."
    return _spec(source, title, points, summary)


_BUILDERS = {
    "date": _build_date_chart,
    "product": _build_product_chart,
    "rating": _build_rating_chart,
    "behavior_type": _build_behavior_chart,
}


# ── 진입점 ────────────────────────────────────────────────────────────────────


async def build_charts(
    plans: Sequence[ChartAxisPlan],
    *,
    brand_id: int,
    date_from: date,
    date_to: date,
) -> tuple[ChartSet, list[ChartUnavailable]]:
    """축 선언 목록 → (조립된 ChartSet, 못 만든 사유 목록). 부분 성공을 허용한다.

    - 미지원 조합·"other" 선언 → unsupported_axes (지원 목록 포함 안내).
    - Spring 조회 실패 → 해당 차트만 source_failed 로 강등(다른 차트는 계속).
    - 조회 성공·데이터 0건 → no_data. 실패는 예외로 새지 않는다 — 차트는 부가
      가치라 보고서를 죽이지 않는다(recommend C2 대칭, run_graph 규약 승계).
    """
    cache = _FetchCache()
    specs: list[ChartSpec] = []
    unavailable: list[ChartUnavailable] = []
    seen: set[tuple[str, str]] = set()

    for plan in plans[:CHART_MAX]:
        key = (plan.x_axis, plan.y_axis)
        if key in seen:
            continue  # 같은 축 조합 중복 선언 — 조용히 1개만
        seen.add(key)
        source = CHART_SOURCES.get(key)
        if source is None:
            unavailable.append(unsupported_axes(plan.title))
            continue
        builder = _BUILDERS[plan.x_axis]
        try:
            built = await builder(
                plan,
                source,
                brand_id=brand_id,
                date_from=date_from,
                date_to=date_to,
                cache=cache,
            )
        except Exception:
            logger.warning("차트 조립 실패(%s×%s) — source_failed 로 강등", *key, exc_info=True)
            unavailable.append(source_failed())
            continue
        if isinstance(built, ChartUnavailable):
            unavailable.append(built)
        else:
            specs.append(built)

    return ChartSet(charts=specs), unavailable
