"""sop/compute/{sales_anomaly,conversion}.py — 브랜드 축 판정 (이슈 #594, `01` §4.4).

기존 통계 모듈은 이미 자체 테스트가 있다(`test_seller_analysis_timeseries` 등). 여기서
보는 것은 **번역 규약**이다 — `decided=False` → `undecided`, 정의 불가·무한대 값 제외,
카운트 정합 이상 시 clamp 금지.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from app.agents.seller.sop.compute.conversion import compute_conversion
from app.agents.seller.sop.compute.sales_anomaly import compute_sales_anomaly
from app.agents.seller.sop.context import AnalysisContext
from app.core.config import Settings
from app.schemas.spring import FunnelResult, SalesResult

_FROM = date(2026, 8, 3)
_TO = date(2026, 9, 1)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _ctx(worker: str) -> AnalysisContext:
    return AnalysisContext(worker=worker, brand_id=7, period_from=_FROM, period_to=_TO)


def _verdict(ctx: AnalysisContext, key: str):
    return next((verdict for verdict in ctx.verdicts if verdict.key == key), None)


def _sales(values: list[float], start: date = date(2026, 8, 5)) -> SalesResult:
    series = [
        {"date": (start + timedelta(days=index)).isoformat(), "sales": int(value), "orderCount": 1}
        for index, value in enumerate(values)
    ]
    return SalesResult.model_validate({"series": series})


def _weekly(weeks: int = 5) -> list[float]:
    """요일 효과만 있는 규칙 시계열 — 주말이 낮다."""
    pattern = [100.0, 100.0, 100.0, 100.0, 100.0, 60.0, 60.0]
    return [pattern[index % 7] for index in range(weeks * 7)]


def _funnel(view: int, cart: int, checkout: int, purchase: int, **extra) -> FunnelResult:
    payload = {"view": view, "cart": cart, "checkout": checkout, "purchase": purchase}
    payload.update(extra)
    return FunnelResult.model_validate(payload)


# ── sales_anomaly ────────────────────────────────────────────────────────────────


def test_매출_급락은_significant_drop으로_나온다(settings: Settings) -> None:
    values = _weekly()
    values[-3] = 5.0
    ctx = _ctx("sales_anomaly")
    compute_sales_anomaly(ctx, _sales(values), settings=settings)

    drops = [verdict for verdict in ctx.verdicts if verdict.verdict == "significant_drop"]
    assert drops, "급락 1건이 검출돼야 한다"
    assert drops[0].key.startswith("sales_anomaly:")
    assert drops[0].method == "stl_gesd"
    # 이상점 날짜의 실측치가 인용 가능 수치로 남는다(검증층 F2 재료).
    assert any(metric.key.startswith("sales:") for metric in ctx.metrics)


def test_요일_효과만_있으면_이상으로_보지_않는다(settings: Settings) -> None:
    ctx = _ctx("sales_anomaly")
    compute_sales_anomaly(ctx, _sales(_weekly()), settings=settings)

    assert [verdict.verdict for verdict in ctx.verdicts] == ["no_significant_change"]


def test_표본_부족은_이상_없음이_아니라_판정_보류다(settings: Settings) -> None:
    ctx = _ctx("sales_anomaly")
    compute_sales_anomaly(ctx, _sales([100.0, 120.0]), settings=settings)

    verdict = _verdict(ctx, "sales_anomaly")
    assert verdict.verdict == "undecided"
    assert any(hold.reason.startswith("sales_undecided") for hold in ctx.holds)


def test_빈_시계열은_보류로_남는다(settings: Settings) -> None:
    ctx = _ctx("sales_anomaly")
    compute_sales_anomaly(ctx, SalesResult(), settings=settings)

    assert _verdict(ctx, "sales_anomaly").verdict == "undecided"
    assert any(hold.reason.startswith("no_series") for hold in ctx.holds)


def test_짧은_lookback은_detail에_각인된다(settings: Settings) -> None:
    ctx = _ctx("sales_anomaly")
    compute_sales_anomaly(ctx, _sales(_weekly(weeks=2)), settings=settings)

    assert all(verdict.detail.get("sample_short") == 1.0 for verdict in ctx.verdicts)


def test_detail에는_유한한_수치만_실린다(settings: Settings) -> None:
    """무매출 이력 직후의 매출은 `sigma=inf` 가 될 수 있다 — 그 값을 숫자로 위장하지 않는다."""
    ctx = _ctx("sales_anomaly")
    compute_sales_anomaly(ctx, _sales([0.0] * 20 + [900.0]), settings=settings)

    assert ctx.verdicts
    for verdict in ctx.verdicts:
        assert all(math.isfinite(value) for value in verdict.detail.values())


# ── conversion ───────────────────────────────────────────────────────────────────


def test_전환율_유의_변화가_verdict로_나온다(settings: Settings) -> None:
    ctx = _ctx("conversion")
    compute_conversion(
        ctx,
        _funnel(10_000, 3_000, 1_500, 750),
        _funnel(10_000, 2_000, 1_000, 500),
        settings=settings,
    )
    verdict = _verdict(ctx, "conversion:view_to_cart")
    assert verdict.verdict == "significant_rise"
    assert verdict.p_value is not None
    assert _verdict(ctx, "conversion:cart_to_checkout").verdict == "no_significant_change"


def test_미집계_단계는_0건이_아니라_판정_보류다(settings: Settings) -> None:
    ctx = _ctx("conversion")
    compute_conversion(
        ctx,
        _funnel(1_000, 200, 0, 50, uncomputableStages=["checkout"]),
        _funnel(1_000, 180, 0, 40, uncomputableStages=["checkout"]),
        settings=settings,
    )
    assert _verdict(ctx, "conversion:cart_to_checkout").verdict == "undecided"
    assert _verdict(ctx, "conversion:checkout_to_purchase").verdict == "undecided"
    assert sum(1 for hold in ctx.holds if hold.reason.startswith("uncomputable_stage")) == 2
    # 미집계 카운트는 0 이 아니라 결측이다.
    checkout = next(metric for metric in ctx.metrics if metric.key == "funnel_checkout")
    assert checkout.value is None


def test_단계_역전은_clamp하지_않고_보류한다(settings: Settings) -> None:
    """I-7 은 이벤트 카운트라 `cart > view` 가 실데이터에서 나온다(`tools.py` 선례)."""
    ctx = _ctx("conversion")
    compute_conversion(
        ctx,
        _funnel(100, 130, 40, 20),
        _funnel(100, 90, 40, 20),
        settings=settings,
    )
    verdict = _verdict(ctx, "conversion:view_to_cart")
    assert verdict.verdict == "undecided"
    assert verdict.detail == {}  # clamp 한 비율을 근거처럼 남기지 않는다
    assert any(hold.reason.startswith("funnel_inconsistent") for hold in ctx.holds)


def test_표본이_없으면_보류하되_hold를_달지_않는다(settings: Settings) -> None:
    ctx = _ctx("conversion")
    compute_conversion(ctx, _funnel(0, 0, 0, 0), _funnel(0, 0, 0, 0), settings=settings)

    assert all(verdict.verdict == "undecided" for verdict in ctx.verdicts)
    assert ctx.holds == []  # 정상 저볼륨을 사고처럼 보고하지 않는다


def test_비교_기간을_주지_않으면_인접_직전_구간이_된다(settings: Settings) -> None:
    ctx = _ctx("conversion")
    compute_conversion(
        ctx,
        _funnel(1_000, 300, 150, 75),
        _funnel(1_000, 200, 100, 50),
        settings=settings,
    )
    comparison = ctx.comparisons[0]
    assert comparison.baseline_to == _FROM - timedelta(days=1)
    assert comparison.baseline_from == comparison.baseline_to - (_TO - _FROM)
