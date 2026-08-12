"""app/agents/seller/chart_verify.py 검증 (이슈 #600, 09-CHART.md §3).

D1~D3·C1(verifier.py)은 무접촉 재사용이라 여기서는 "합성 finding 을 통해 그대로
호출되는가"만 확인한다 — 각 체크 자체의 세부 회귀는 test_seller_verifier.py 소관이다.
이 파일이 검증하는 신규 로직은 C4(chart_claims_bounded)·인과 L0 보강·period_grounded
허용 집합 구성(_chart_citable_dates)뿐이다. 순수 함수 — LLM·IO 없음.
"""

from __future__ import annotations

import datetime as dt

from app.agents.seller import chart_verify
from app.agents.seller.charts import chart_facts
from app.agents.seller.schemas import ChartPoint, ChartSeries, ChartSet, ChartSpec

_FROM = dt.date(2026, 7, 13)
_TO = dt.date(2026, 7, 14)


def _spec(
    points: list[ChartPoint],
    *,
    title: str = "일별 매출 추이",
    chart_type: str = "line",
    aggregate: str = "sum",
    summary: str = "",
) -> ChartSpec:
    return ChartSpec(
        title=title,
        chart_type=chart_type,  # type: ignore[arg-type]
        unit="KRW",
        aggregate=aggregate,  # type: ignore[arg-type]
        series=[ChartSeries(label="매출", points=points)],
        summary=summary,
    )


def _verify(text: str, charts: ChartSet) -> list[str]:
    facts = [chart_facts(spec) for spec in charts.charts]
    return chart_verify.run_chart_verification(text, charts, facts, chart_from=_FROM, chart_to=_TO)


def test_pass_when_interpretation_only_cites_grounded_values() -> None:
    """D1~D3·C1·C4·period_grounded 전부 통과 — 좌표·chart_facts·기간만 인용."""
    charts = ChartSet(
        charts=[
            _spec(
                [ChartPoint(x="07-13", y=1240000.0), ChartPoint(x="07-14", y=980000.0)],
                summary="2026-07-13~2026-07-14 기간의 매출입니다.",
            )
        ]
    )
    text = "07월 13일 매출은 1,240,000원, 07월 14일은 980,000원으로 낮아졌습니다."
    assert _verify(text, charts) == []


def test_d2_novel_number_via_synthesized_finding_fails() -> None:
    """D2(무접촉) — 좌표·chart_facts 어디에도 없는 수치를 인용하면 실패한다."""
    charts = ChartSet(
        charts=[_spec([ChartPoint(x="07-13", y=1240000.0), ChartPoint(x="07-14", y=980000.0)])]
    )
    text = "이 기간 매출은 999,999,999원으로 집계됐습니다."
    reasons = _verify(text, charts)
    assert any("근거 없는 수치" in r and "999999999" in r for r in reasons)


def test_causal_free_blocks_causal_term_even_with_hedge_present() -> None:
    """L0 보강 — C1(check_cause_hedged)은 완화어가 있으면 근거 없이도 통과시키지만
    (verifier.py 실측, chart_verify.py 모듈 docstring 참조), 차트 레인은 완화어 유무와
    무관하게 인과 단정 어휘를 전면 차단한다(결정 86)."""
    charts = ChartSet(
        charts=[_spec([ChartPoint(x="07-13", y=1240000.0), ChartPoint(x="07-14", y=980000.0)])]
    )
    text = "가격 인상 때문에 매출이 낮아진 것으로 추정됩니다."  # 인과 어휘 + 완화어 동반
    reasons = _verify(text, charts)
    assert any("인과" in r for r in reasons)


def test_causal_free_passes_when_no_causal_language() -> None:
    """인과 어휘가 전혀 없으면(현상 서술만) 통과한다."""
    charts = ChartSet(
        charts=[_spec([ChartPoint(x="07-13", y=1240000.0), ChartPoint(x="07-14", y=980000.0)])]
    )
    text = "07월 13일 매출이 가장 높았고 07월 14일에는 낮아졌습니다."
    assert _verify(text, charts) == []


def test_c4a_snapshot_trend_blocked_when_all_charts_are_none_aggregate() -> None:
    """C4-a — 이 턴 차트가 전부 aggregate=='none'(스냅샷)일 때 추세 어휘를 막는다."""
    charts = ChartSet(
        charts=[
            _spec(
                [ChartPoint(x="감귤청", y=12000.0), ChartPoint(x="한라봉", y=15000.0)],
                title="상품별 가격",
                chart_type="bar",
                aggregate="none",
            )
        ]
    )
    text = "가격이 꾸준히 상승 추세를 보이고 있습니다."
    reasons = _verify(text, charts)
    assert any("스냅샷" in r for r in reasons)


def test_c4a_allows_trend_language_when_not_all_snapshot() -> None:
    """C4-a — 스냅샷이 아닌 차트(sum)에는 추세 어휘를 막지 않는다."""
    charts = ChartSet(
        charts=[_spec([ChartPoint(x="07-13", y=1240000.0), ChartPoint(x="07-14", y=980000.0)])]
    )
    text = "매출이 하락 추세를 보였습니다."
    reasons = _verify(text, charts)
    assert not any("스냅샷" in r for r in reasons)


def test_c4b_daily_language_blocked_for_bucketed_chart() -> None:
    """C4-b — summary 에 '3일 단위'가 있으면 하루 단위 서술을 막는다."""
    charts = ChartSet(
        charts=[
            _spec(
                [ChartPoint(x="07-13", y=1240000.0)],
                summary="2026-07-13~2026-08-11 기간의 매출입니다. 3일 단위로 묶어 표시했습니다.",
            )
        ]
    )
    text = "07월 13일 당일 매출이 급증했습니다."
    reasons = _verify(text, charts)
    assert any("하루 단위" in r for r in reasons)


def test_c4c_bottom_rank_blocked_when_summary_notes_truncation() -> None:
    """C4-c — summary 에 절단 안내('개만 표시')가 있으면 하위 단정을 막는다."""
    charts = ChartSet(
        charts=[
            _spec(
                [ChartPoint(x="감귤청", y=99.0)],
                title="상품별 판매 수량",
                chart_type="bar",
                summary="상품 42개 중 판매 수량 상위 15개만 표시했습니다.",
            )
        ]
    )
    text = "가장 안 팔리는 상품은 이 목록의 마지막 항목입니다."
    reasons = _verify(text, charts)
    assert any("하위를 단정" in r for r in reasons)


def test_c4d_behavior_all_blocked_for_behavior_type_chart() -> None:
    """C4-d — 행동 유형별(4라벨 고정) 차트를 '전체 행동'으로 서술하면 막는다."""
    charts = ChartSet(
        charts=[
            _spec(
                [
                    ChartPoint(x="조회", y=100.0),
                    ChartPoint(x="장바구니", y=40.0),
                    ChartPoint(x="결제시작", y=20.0),
                    ChartPoint(x="구매", y=10.0),
                ],
                title="행동 유형별 건수",
                chart_type="bar",
            )
        ]
    )
    text = "전체 행동 건수는 조회가 가장 많았습니다."
    reasons = _verify(text, charts)
    assert any("전체 행동" in r for r in reasons)


def test_c4d_not_triggered_for_non_behavior_bar_chart() -> None:
    """C4-d — 라벨 집합이 4종과 다르면(다른 bar 차트) 오탐하지 않는다."""
    charts = ChartSet(
        charts=[
            _spec(
                [ChartPoint(x="감귤청", y=99.0), ChartPoint(x="한라봉", y=50.0)],
                title="상품별 판매 수량",
                chart_type="bar",
            )
        ]
    )
    text = "전체 행동 패턴과는 별개로 감귤청이 가장 많이 팔렸습니다."
    reasons = _verify(text, charts)
    assert not any("전체 행동" in r for r in reasons)


def test_period_grounded_blocks_date_outside_allowed_set() -> None:
    """V2-d 재사용 — chart_from/to·summary ISO 날짜 밖의 날짜를 인용하면 실패."""
    charts = ChartSet(
        charts=[_spec([ChartPoint(x="07-13", y=1240000.0)], summary="2026-07-13 기간입니다.")]
    )
    text = "2099-01-01 에 매출이 급증했습니다."
    reasons = _verify(text, charts)
    assert any("인용 불가 날짜" in r for r in reasons)


def test_period_grounded_allows_chart_from_to_and_summary_dates() -> None:
    """허용 집합 — chart_from/to 자체와 summary 에 실린 ISO 날짜는 인용 가능하다."""
    charts = ChartSet(
        charts=[
            _spec(
                [ChartPoint(x="07-13", y=1240000.0)],
                summary="2026-07-13~2026-07-14 기간의 매출입니다.",
            )
        ]
    )
    text = "2026-07-13부터 2026-07-14까지의 매출을 보여드립니다."
    reasons = _verify(text, charts)
    assert not any("인용 불가 날짜" in r for r in reasons)
