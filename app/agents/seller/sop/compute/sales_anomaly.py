"""`sales_anomaly` compute — I-6 매출 시계열의 계절성 인지 이상 판정 (이슈 #594).

`analysis.timeseries.detect_seasonal_anomalies`(STL + robust GESD) 재사용뿐이다. 이 스텝이
더하는 것은 셋이다 — ① `decided=False`(표본 부족)를 `undecided` 로 옮기는 번역 ② 이상점
1건당 Verdict/Metric 발행 ③ **정의 불가·무한대 값을 `detail` 에서 빼는 것**.

`detail` 이 `dict[str, float]` 이라 날짜 문자열을 담을 수 없어 이상점 날짜는 `key` 에
싣는다(`sales_anomaly:2026-08-09`). `sigma=inf`(무매출 이력 직후 매출 발생)와
`deviation_pct=None`(기대값 0 이하)은 **숫자로 위장하지 않고 뺀다** — inf 는 JSON 직렬화도
안 되고, 검증층이 인용 가능 수치로 오인하면 보고서에 "무한대"가 실린다.
"""

from __future__ import annotations

import math
from datetime import date

from app.agents.seller.analysis import timeseries
from app.agents.seller.sop.context import AnalysisContext, Hold, Metric, Verdict
from app.core.config import Settings
from app.schemas.spring import SalesResult

_METHOD = "stl_gesd"
_KEY = "sales_anomaly"


def _as_date(value: str, fallback: date) -> date:
    """I-6 `date`("YYYY-MM-DD") → date. 형식이 어긋나면 ctx 기간으로 물러난다."""
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


def compute_sales_anomaly(ctx: AnalysisContext, sales: SalesResult, *, settings: Settings) -> None:
    """매출 이상 판정 — LLM 0회 (`01` §4.4 표 sales_anomaly 행).

    조회 구간(28일 lookback = `seller_analysis_lookback_days`)을 정하는 것은 호출부의
    일이라 여기서는 받은 series 를 그대로 쓴다. 다만 구간이 기대보다 짧으면 판정의
    신뢰도가 달라지므로 `detail["sample_short"]` 로 각인한다 — 조용히 넘어가면 14일치로
    낸 판정과 28일치로 낸 판정이 보고서에서 구분되지 않는다.
    """
    points = list(sales.series)
    sample_short = len(points) < settings.seller_analysis_lookback_days

    if not points:
        ctx.verdicts.append(
            Verdict(
                key=_KEY,
                verdict="undecided",
                method=_METHOD,
                detail={"sample_size": 0.0},
            )
        )
        ctx.holds.append(
            Hold(step="compute", reason="no_series: I-6 매출 시계열이 비어 판정할 수 없다")
        )
        return

    dates = [point.date for point in points]
    values = [float(point.sales) for point in points]
    detection = timeseries.detect_seasonal_anomalies(
        dates,
        values,
        period=settings.seller_stl_period,
        alpha=settings.seller_gesd_alpha,
        max_anomalies_ratio=settings.seller_gesd_max_anomalies_ratio,
        min_history_for_stl=settings.seller_min_history_for_stl,
    )

    base_detail: dict[str, float] = {
        "sample_size": float(detection.sample_size),
        "seasonal_adjusted": 1.0 if detection.seasonal_adjusted else 0.0,
    }
    if sample_short:
        base_detail["sample_short"] = 1.0

    ctx.metrics.append(
        Metric(
            key="sales_series_days",
            value=float(len(points)),
            unit="일",
            source="I-6",
            period_from=ctx.period_from,
            period_to=ctx.period_to,
        )
    )

    if not detection.decided:
        # 빈 목록 하나가 "이상 없음"과 "판정 보류"를 동시에 뜻하던 모호성을 #512 가
        # 타입으로 갈랐다 — 그 구분을 여기서 지운다면 그 작업이 무의미해진다.
        ctx.verdicts.append(
            Verdict(
                key=_KEY,
                verdict="undecided",
                method=_METHOD,
                detail={**base_detail, "min_samples": float(detection.min_samples)},
            )
        )
        ctx.holds.append(
            Hold(
                step="compute",
                reason=(
                    f"sales_undecided: 표본 {detection.sample_size}개는 검정 최소"
                    f" {detection.min_samples}개 미만이라 이상 판정을 보류한다"
                ),
            )
        )
        return

    if not detection.anomalies:
        ctx.verdicts.append(
            Verdict(key=_KEY, verdict="no_significant_change", method=_METHOD, detail=base_detail)
        )
        return

    for anomaly in detection.anomalies:
        detail = dict(base_detail)
        detail["actual"] = anomaly.actual
        detail["expected"] = anomaly.expected
        if anomaly.deviation_pct is not None:
            detail["deviation_pct"] = anomaly.deviation_pct
        if math.isfinite(anomaly.sigma):
            detail["sigma"] = anomaly.sigma
        ctx.verdicts.append(
            Verdict(
                key=f"{_KEY}:{anomaly.date}",
                verdict="significant_drop" if anomaly.direction == "drop" else "significant_rise",
                method=_METHOD,
                detail=detail,
            )
        )
        point_date = _as_date(anomaly.date, ctx.period_to)
        ctx.metrics.append(
            Metric(
                key=f"sales:{anomaly.date}",
                value=anomaly.actual,
                unit="원",
                source="I-6",
                period_from=point_date,
                period_to=point_date,
            )
        )
