"""app/agents/seller/pipeline.py 파이프라인 입출력 계약 검증 (3-1)."""

from __future__ import annotations

import datetime as dt
from typing import get_args

import pytest

from app.agents.seller import pipeline
from app.agents.seller.schemas import (
    ActionRecommendation,
    AnalysisFinding,
    AnalysisPlan,
    AnalysisType,
    ChartPoint,
    ChartSeries,
    ChartSet,
    ChartSpec,
    RecommendationSet,
)


def _plan(**overrides: object) -> AnalysisPlan:
    """테스트용 기본 계획 — 필요한 필드만 덮어쓴다."""
    base: dict = {"analyses": ["sales_anomaly"], "reason": "테스트"}
    base.update(overrides)
    return AnalysisPlan(**base)


def test_resolve_plan_happy_path() -> None:
    """'지난달' 계획 → 전월 1일~말일 ResolvedPlan(코드 환산, 장치 ④)."""
    plan = _plan(analyses=["sales_anomaly", "churn"], period_expr="지난달")
    resolved = pipeline.resolve_plan(plan, today=dt.date(2026, 7, 18), recent_default_days=7)
    assert resolved.analyses == ("sales_anomaly", "churn")
    assert resolved.date_from == dt.date(2026, 6, 1)
    assert resolved.date_to == dt.date(2026, 6, 30)


def test_resolve_plan_default_period_uses_recent_default() -> None:
    """기간 미언급(period_expr 기본 '최근') → recent_default_days 일, 오늘 제외."""
    resolved = pipeline.resolve_plan(_plan(), today=dt.date(2026, 7, 18), recent_default_days=7)
    assert resolved.date_from == dt.date(2026, 7, 11)
    assert resolved.date_to == dt.date(2026, 7, 17)


def test_resolve_plan_clarification_raises_with_question() -> None:
    """clarification 이 있으면 계획 불성립 — 되물을 질문이 ValueError 메시지로 올라온다."""
    plan = _plan(analyses=[], clarification="어느 기간의 분석을 원하시나요?")
    with pytest.raises(ValueError, match="어느 기간"):
        pipeline.resolve_plan(plan, today=dt.date(2026, 7, 18), recent_default_days=7)


def test_resolve_plan_empty_analyses_raises() -> None:
    """clarification 없이 워커도 비면 planner 오류 — 되묻기 ValueError."""
    with pytest.raises(ValueError):
        pipeline.resolve_plan(_plan(analyses=[]), today=dt.date(2026, 7, 18), recent_default_days=7)


def test_resolve_plan_unsupported_period_propagates() -> None:
    """해석 불가 기간 표현("작년 여름")은 period.resolve_period 의 ValueError 가 전파된다.

    [#345] 종전에는 "이번 달" 이 이 케이스였다 — 어휘 확장으로 지금은 확인 후 통과다.
    """
    with pytest.raises(ValueError):
        pipeline.resolve_plan(
            _plan(period_expr="작년 여름"), today=dt.date(2026, 7, 18), recent_default_days=7
        )


# ── #345 P1: 확인 흐름 계약 (어휘 판정 자체는 test_seller_period.py) ──────────────


def test_resolve_plan_canonical_vocab_is_never_supplemented() -> None:
    """회귀 가드 — 기존 어휘 5종은 period_supplemented=False 로 통과한다(#345·#584).

    이 테스트가 깨지면 잘 쓰던 판매자에게 없던 기간 고지를 새로 물린 것이다.
    """
    today = dt.date(2026, 8, 6)
    for expr in ("지난달", "최근 7일", "최근", "어제", "2026-06-01~2026-06-30"):
        resolved = pipeline.resolve_plan(
            _plan(period_expr=expr), today=today, recent_default_days=7
        )
        assert resolved.period_supplemented is False, expr


def test_resolve_plan_expanded_vocab_is_supplemented() -> None:
    """신규 어휘는 값이 나오되 고지 신호를 함께 올린다(#345·#584)."""
    today = dt.date(2026, 8, 6)
    for expr in ("이번 달", "올해", "상반기", "최근 3개월"):
        resolved = pipeline.resolve_plan(
            _plan(period_expr=expr), today=today, recent_default_days=7
        )
        assert resolved.period_supplemented is True, expr
        assert resolved.period_expr == expr
        assert resolved.date_to <= dt.date(2026, 8, 5)  # R1 — 오늘 제외


def test_resolve_plan_wants_chart_from_plan_field() -> None:
    """LLM 이 wants_chart=True 로 판단하면 question 키워드 없이도 전파된다."""
    plan = _plan(wants_chart=True)
    resolved = pipeline.resolve_plan(plan, today=dt.date(2026, 7, 18), recent_default_days=7)
    assert resolved.wants_chart is True


def test_resolve_plan_wants_chart_from_question_keyword() -> None:
    """plan.wants_chart=False 여도 question 에 차트 키워드가 있으면 코드가 보강한다."""
    plan = _plan(wants_chart=False)
    resolved = pipeline.resolve_plan(
        plan,
        today=dt.date(2026, 7, 18),
        recent_default_days=7,
        question="지난달 매출 그래프로 보여줘",
    )
    assert resolved.wants_chart is True


def test_resolve_plan_wants_chart_false_by_default() -> None:
    """신호가 전혀 없으면 wants_chart=False — 억지 차트 생성 방지."""
    resolved = pipeline.resolve_plan(
        _plan(),
        today=dt.date(2026, 7, 18),
        recent_default_days=7,
        question="지난달 매출이 왜 떨어졌어?",
    )
    assert resolved.wants_chart is False


def test_resolve_plan_question_default_keeps_backward_compat() -> None:
    """question 미전달(기존 호출부)은 LLM 판정만 반영 — 하위 호환 키워드 기본값."""
    resolved = pipeline.resolve_plan(
        _plan(wants_chart=False), today=dt.date(2026, 7, 18), recent_default_days=7
    )
    assert resolved.wants_chart is False


# ── [#531] wants_chart_keyword — 레인 선판정과 resolve_plan 이 공유하는 정본 ──────


@pytest.mark.parametrize(
    "message",
    [
        "매출 차트 보여줘",
        "매출 그래프 보여줘",
        "재고 시각화해줘",
        "전환율 도표로 보여줘",
        "최근 7일 매출 그려줘",
        "전환율 분석하고 그래프로 보여줘",  # 문장 중간 — 전체 매칭이 아니라 존재 검사다
    ],
)
def test_wants_chart_keyword_detects_vocabulary(message: str) -> None:
    """차트 어휘 5종은 발화 어디에 있든 True — api/seller.py ②.5 가 이 판정에 걸린다."""
    assert pipeline.wants_chart_keyword(message) is True


@pytest.mark.parametrize(
    "message",
    ["최근 7일 매출 보여줘", "지난달 매출이 왜 떨어졌어?", "신규 주문 뭐 있어?", ""],
)
def test_wants_chart_keyword_ignores_plain_lookup(message: str) -> None:
    """차트 어휘가 없으면 False — 조회 발화 전체가 analysis 로 끌려가면 #180 이 무너진다."""
    assert pipeline.wants_chart_keyword(message) is False


def test_resolve_plan_shares_keyword_check_with_lane_precheck() -> None:
    """resolve_plan 의 키워드 보강과 레인 선판정이 같은 함수를 쓴다(어휘 분기 방지).

    두 경로가 각자 정규식을 들면, 선판정이 analysis 로 보낸 발화를 resolve_plan 이
    wants_chart=False 로 판정해 차트 없는 보고서만 나오는 조용한 불일치가 생긴다.
    """
    question = "이번달 매출 그래프 보여줘"
    assert pipeline.wants_chart_keyword(question) is True
    resolved = pipeline.resolve_plan(
        _plan(wants_chart=False),
        today=dt.date(2026, 8, 9),
        recent_default_days=7,
        question=question,
    )
    assert resolved.wants_chart is True


def test_resolve_plan_promotes_chart_only_when_planner_leaves_analyses_empty() -> None:
    """[#531] 차트 어휘 + 빈 analyses 는 되묻기가 아니라 chart_only 승격이다.

    선판정이 analysis 레인으로 보낸 차트 발화를 planner 가 chart_only 로 못 잡고
    analyses 까지 비우면 예전에는 "어떤 분석을 원하시는지..." 되묻기로 끝났다 —
    ASCII 아트가 되묻기로 바뀔 뿐 좌표(report.charts[])는 여전히 나가지 못한다.
    """
    resolved = pipeline.resolve_plan(
        _plan(analyses=[], chart_only=False),
        today=dt.date(2026, 8, 9),
        recent_default_days=7,
        question="이번달 매출 그래프 보여줘",
    )

    assert resolved.chart_only is True
    assert resolved.wants_chart is True
    assert resolved.analyses == ()


def test_resolve_plan_clarification_wins_over_chart_promotion() -> None:
    """planner 가 되물을 이유를 댔으면 차트 승격이 그것을 덮지 않는다.

    clarification 은 "계획이 성립하지 않는다"는 구체적 신호라 어휘 검사보다 강하다.
    """
    plan = _plan(analyses=[], clarification="어떤 상품의 그래프를 원하시나요?")

    with pytest.raises(ValueError, match="어떤 상품"):
        pipeline.resolve_plan(
            plan,
            today=dt.date(2026, 8, 9),
            recent_default_days=7,
            question="그래프 보여줘",
        )


def test_resolve_plan_empty_analyses_without_chart_word_still_raises() -> None:
    """[회귀] 차트 어휘가 없으면 빈 analyses 는 그대로 되묻기다 — 승격이 번지지 않는다."""
    with pytest.raises(ValueError, match="어떤 분석"):
        pipeline.resolve_plan(
            _plan(analyses=[]),
            today=dt.date(2026, 8, 9),
            recent_default_days=7,
            question="지난달 매출이 왜 떨어졌어?",
        )


def test_resolve_plan_chart_period_expr_resolved_separately() -> None:
    """[#504] 차트 전용 기간 표현은 본 기간과 별도로 환산돼 chart_from/to 에 담긴다."""
    plan = _plan(period_expr="지난달", chart_period_expr="최근 7일")
    resolved = pipeline.resolve_plan(plan, today=dt.date(2026, 7, 18), recent_default_days=7)
    assert (resolved.date_from, resolved.date_to) == (dt.date(2026, 6, 1), dt.date(2026, 6, 30))
    assert (resolved.chart_from, resolved.chart_to) == (dt.date(2026, 7, 11), dt.date(2026, 7, 17))
    assert resolved.chart_period_error == ""
    assert resolved.wants_chart is True  # 차트 기간을 말했다 = 차트를 원한다


def test_resolve_plan_chart_period_error_does_not_kill_pipeline() -> None:
    """[#504] 차트 기간만 해석 불가("작년 여름")면 ValueError 로 죽이지 않고
    chart_period_error 에 담는다 — 보고서는 살리고 차트만 chartUnavailable 로 강등."""
    plan = _plan(period_expr="지난달", chart_period_expr="작년 여름")
    resolved = pipeline.resolve_plan(plan, today=dt.date(2026, 7, 18), recent_default_days=7)
    assert resolved.date_from == dt.date(2026, 6, 1)  # 본 기간은 정상 환산
    assert resolved.chart_from is None and resolved.chart_to is None
    assert resolved.chart_period_error != ""


def test_resolve_plan_chart_period_absent_leaves_fields_empty() -> None:
    """[#504] 차트 기간 별도 언급이 없으면 chart_* 는 비어 있다 — 차트는 본 기간을 따른다."""
    resolved = pipeline.resolve_plan(_plan(), today=dt.date(2026, 7, 18), recent_default_days=7)
    assert resolved.chart_period_expr == ""
    assert resolved.chart_from is None and resolved.chart_to is None
    assert resolved.chart_period_error == ""


def test_resolve_plan_chart_only_allows_empty_analyses() -> None:
    """[#504] chart_only 턴은 워커를 쓰지 않으므로 analyses 가 비어도 계획이 성립하고,
    wants_chart 가 강제로 True 다."""
    plan = _plan(analyses=[], chart_only=True, period_expr="최근 7일")
    resolved = pipeline.resolve_plan(plan, today=dt.date(2026, 7, 18), recent_default_days=7)
    assert resolved.analyses == ()
    assert resolved.chart_only is True
    assert resolved.wants_chart is True


def test_format_worker_input_contains_period_and_question() -> None:
    """워커 입력 포맷 — from/to(ISO)와 질문이 규약 형태로 들어간다(기간 주입 규약)."""
    resolved = pipeline.ResolvedPlan(
        analyses=("sales_anomaly",),
        date_from=dt.date(2026, 6, 1),
        date_to=dt.date(2026, 6, 30),
    )
    text = pipeline.format_worker_input("지난달 매출이 왜 떨어졌어?", resolved)
    assert "[분석 기간] from=2026-06-01 to=2026-06-30" in text
    assert "[판매자 질문] 지난달 매출이 왜 떨어졌어?" in text


def test_format_findings_block_numbers_and_details() -> None:
    """번호·유형·심각도·요약·근거·조치 힌트가 규약 형태로 직렬화된다(report/judge 공용)."""
    findings = [
        AnalysisFinding(
            analysis_type="sales_anomaly",
            summary="급락 발견",
            evidence=["06-12 매출 180,000원"],
            severity="warning",
            recommendation="가격 재검토",
        ),
        AnalysisFinding(
            analysis_type="abuse", summary="데이터 확보 실패", evidence=[], severity="info"
        ),
    ]
    block = pipeline.format_findings_block(findings)
    assert "1. [sales_anomaly] (severity=warning) 급락 발견" in block
    assert "   - 근거: 06-12 매출 180,000원" in block
    assert "   - 조치 힌트: 가격 재검토" in block
    assert "2. [abuse] (severity=info) 데이터 확보 실패" in block


def test_format_rewrite_and_judge_inputs() -> None:
    """재작성 입력은 이전 보고서+개선 지시를, judge 입력은 보고서를 포함한다(3-4 계약)."""
    findings = [
        AnalysisFinding(
            analysis_type="churn",
            summary="이탈 증가",
            evidence=["이탈률 12.5%"],
            severity="warning",
        )
    ]
    rewrite = pipeline.format_rewrite_input(findings, "이전 본문", "수치 근거를 인용할 것")
    assert "[이전 보고서]\n이전 본문" in rewrite
    assert "[개선 지시]\n수치 근거를 인용할 것" in rewrite
    assert "[분석 결과]" in rewrite

    judge = pipeline.format_judge_input(findings, "보고서 본문")
    assert "[보고서]\n보고서 본문" in judge
    assert "[분석 결과]" in judge


def _recommendation(title: str, effect: str = "") -> ActionRecommendation:
    return ActionRecommendation(
        action_type="price_adjust",
        product_id=101,
        title=title,
        rationale="근거",
        expected_effect=effect,
    )


def test_compose_response_numbers_follow_list_order() -> None:
    """번호("N번.")는 목록 순서 그대로 — §6.3 recommendations[N-1] 조회 계약의 표면."""
    recs = RecommendationSet(
        recommendations=[
            _recommendation("감귤청 가격 10% 인하", "전환율 회복"),
            _recommendation("품절 상품 재입고"),
        ],
        summary="가격·재고 중심 2건",
    )
    text = pipeline.compose_response("보고서 본문", recs)
    assert text.startswith("보고서 본문")
    assert "[추천 행동]" in text
    assert "1번. 감귤청 가격 10% 인하" in text
    assert "   기대 효과: 전환율 회복" in text
    assert "2번. 품절 상품 재입고" in text
    assert "가격·재고 중심 2건" in text  # summary 는 목록 앞에 포함(마감 리뷰 테스트 공백)
    assert "N번 적용해줘" in text


def test_compose_response_empty_recommendations() -> None:
    """빈 추천 — 보고서만(사유 summary 가 있으면 한 줄 덧붙임), 안내 문구 없음."""
    assert pipeline.compose_response("본문", RecommendationSet()) == "본문"

    with_reason = pipeline.compose_response(
        "본문", RecommendationSet(recommendations=[], summary="추천할 근거가 없습니다")
    )
    assert with_reason == "본문\n\n[추천 행동]\n추천할 근거가 없습니다"
    assert "N번" not in with_reason


def _chart_set() -> ChartSet:
    return ChartSet(
        charts=[
            ChartSpec(
                title="일별 매출",
                chart_type="line",
                unit="KRW",
                series=[ChartSeries(label="매출", points=[ChartPoint(x="07-01", y=1240000)])],
            )
        ]
    )


def test_compose_response_charts_present_no_extra_notice() -> None:
    """차트가 실제로 있으면(요청함) 안내 문구를 덧붙이지 않는다 — SSE chart 이벤트가 별도로 전달."""
    text = pipeline.compose_response(
        "본문", RecommendationSet(), _chart_set(), chart_requested=True
    )
    assert text == "본문"
    assert "[차트 안내]" not in text


def test_compose_response_chart_requested_but_missing_appends_notice() -> None:
    """요청했는데 차트가 없으면(graph 실패·G1 전건 드랍) 그 경우만 안내한다(D-5)."""
    text = pipeline.compose_response("본문", RecommendationSet(), None, chart_requested=True)
    assert "[차트 안내]" in text
    assert "요청하신 차트는 이번엔 만들어 드리지 못했어요" in text

    empty_charts_text = pipeline.compose_response(
        "본문", RecommendationSet(), ChartSet(charts=[]), chart_requested=True
    )
    assert "[차트 안내]" in empty_charts_text


def test_compose_response_chart_unavailable_messages_verbatim() -> None:
    """[#504] 사유(ChartUnavailable)가 있으면 그 완성 문장을 그대로 싣는다 — 원인을
    아는데 일반 문구로 뭉개면 오보다. 부분 성공(charts 있음)에도 사유는 붙는다."""
    from app.agents.seller.charts import ChartUnavailable

    reason = ChartUnavailable(reason="no_data", message="해당 기간에 표시할 데이터가 없습니다.")
    text = pipeline.compose_response(
        "본문", RecommendationSet(), _chart_set(), chart_requested=True, chart_unavailable=[reason]
    )
    assert "[차트 안내]" in text
    assert "해당 기간에 표시할 데이터가 없습니다." in text


def test_compose_response_chart_not_requested_no_notice() -> None:
    """차트를 요청하지 않았으면(chart_requested=False 기본값) 차트 언급 자체가 없다."""
    text = pipeline.compose_response("본문", RecommendationSet())
    assert "[차트 안내]" not in text
    assert text == "본문"


def test_format_recommend_input_contract() -> None:
    """recommend 입력 — 분석 결과 + 검증된 보고서 (RECOMMEND_PROMPT 계약)."""
    findings = [
        AnalysisFinding(
            analysis_type="conversion", summary="병목", evidence=["전환율 2.1%"], severity="warning"
        )
    ]
    text = pipeline.format_recommend_input(findings, "검증 본문")
    assert "[분석 결과]" in text
    assert "[검증된 보고서]\n검증 본문" in text


def test_format_analysis_judge_input_contract() -> None:
    """analysis_judge 입력 — (1) 도구 원출력 (2) finding (ANALYSIS_JUDGE_PROMPT 계약)."""
    finding = AnalysisFinding(
        analysis_type="conversion", summary="병목", evidence=["전환율 2.1%"], severity="warning"
    )
    text = pipeline.format_analysis_judge_input(finding, ["전환율 조회 결과: 2.1%"])
    assert "[도구 원출력]\n- 전환율 조회 결과: 2.1%" in text
    assert "[분석 결과]" in text
    assert "conversion" in text


def test_format_analysis_judge_input_empty_tool_outputs_is_explicit() -> None:
    """도구 출력이 비면 '(도구 출력 없음)'을 명시한다 — 빈 문자열 은폐 방지."""
    finding = AnalysisFinding(
        analysis_type="abuse", summary="데이터 확보 실패", evidence=[], severity="info"
    )
    text = pipeline.format_analysis_judge_input(finding, [])
    assert "[도구 원출력]\n(도구 출력 없음)" in text


def test_format_worker_retry_input_combines_prior_finding_and_feedback() -> None:
    """브랜치 재실행 입력 — 원 지시(기간·질문) + 이전 finding + 개선 지시(F+judge 합산)."""
    resolved = pipeline.ResolvedPlan(
        analyses=("sales_anomaly",),
        date_from=dt.date(2026, 6, 1),
        date_to=dt.date(2026, 6, 30),
    )
    prev = AnalysisFinding(
        analysis_type="sales_anomaly",
        summary="급락 발견",
        evidence=["999,999원"],
        severity="warning",
    )
    text = pipeline.format_worker_retry_input(
        "지난달 매출이 왜 떨어졌어?",
        resolved,
        prev,
        "근거 없는 수치 999999 — 도구 출력을 그대로 인용할 것",
    )
    assert "[분석 기간] from=2026-06-01 to=2026-06-30" in text
    assert "[판매자 질문] 지난달 매출이 왜 떨어졌어?" in text
    assert "[이전 분석 결과]" in text
    assert "급락 발견" in text
    assert "[개선 지시]\n근거 없는 수치 999999 — 도구 출력을 그대로 인용할 것" in text


def test_format_graph_input_contract() -> None:
    """graph 입력 — 분석 결과 + 검증된 보고서 + 판매자 질문(GRAPH_PROMPT 계약)."""
    findings = [
        AnalysisFinding(
            analysis_type="sales_anomaly",
            summary="급락 발견",
            evidence=["06-12 매출 180,000원"],
            severity="warning",
        )
    ]
    text = pipeline.format_graph_input(findings, "검증 본문", "지난달 매출 추이 보여줘")
    assert "[분석 결과]" in text
    assert "[검증된 보고서]\n검증 본문" in text
    assert "[판매자 질문]\n지난달 매출 추이 보여줘" in text


# ── chart 해석 입력 포맷 (이슈 #600, 09-CHART.md §2.2·§2.3·§3.1·§3.2) ─────────────


def _line_chart(*, title: str = "일별 매출 추이", summary: str = "") -> ChartSpec:
    return ChartSpec(
        title=title,
        chart_type="line",
        unit="KRW",
        aggregate="sum",
        series=[
            ChartSeries(
                label="매출",
                points=[
                    ChartPoint(x="07-13", y=1240000.0),
                    ChartPoint(x="07-14", y=980000.0),
                ],
            )
        ],
        summary=summary,
    )


def test_display_number_uses_display_form_not_raw_float() -> None:
    """결정 88 — 정수값이면 정수, 아니면 소수 1자리. float 원본을 그대로 쓰지 않는다."""
    assert pipeline._display_number(1240000.0) == "1,240,000"
    assert pipeline._display_number(4.55) == "4.5"  # round() 은행원 규칙(0.5 짝수 반올림)
    assert pipeline._display_number(-270000.0) == "-270,000"


def test_format_chart_points_uses_display_form() -> None:
    """format_chart_input(LLM 입력)과 같은 표기를 D2 허용 집합에도 써야 한다(결정 88)."""
    charts = ChartSet(charts=[_line_chart()])
    lines = pipeline.format_chart_points(charts)
    assert len(lines) == 1
    assert "07-13=1,240,000" in lines[0]
    assert "1240000.0" not in lines[0]  # 정규화 안 된 float 표기가 새면 D2 가 전건 실패한다


def test_format_chart_facts_and_points_share_display_form() -> None:
    """format_chart_input 과 format_chart_evidence 가 같은 함수(_display_number)로
    숫자를 만든다 — 두 출력의 좌표 표기가 바이트 단위로 같아야 한다."""
    from app.agents.seller.charts import chart_facts as _chart_facts

    spec = _line_chart()
    charts = ChartSet(charts=[spec])
    facts = [_chart_facts(spec)]

    chart_from, chart_to = dt.date(2026, 7, 13), dt.date(2026, 7, 14)
    llm_input = pipeline.format_chart_input(charts, facts, chart_from, chart_to, "매출 그래프")
    evidence = pipeline.format_chart_evidence(charts, facts)

    assert "07-13=1,240,000" in llm_input
    assert any("07-13=1,240,000" in line for line in evidence)
    assert "[판매자 질문]\n매출 그래프" in llm_input


def test_format_chart_input_includes_code_computed_block() -> None:
    """[코드 계산] 블록 — 합계·평균·최고·최저·처음→끝 이 LLM 입력에 실린다(§2.2)."""
    from app.agents.seller.charts import chart_facts as _chart_facts

    spec = _line_chart(summary="2026-07-13~2026-07-14 기간의 매출입니다.")
    charts = ChartSet(charts=[spec])
    facts = [_chart_facts(spec)]
    text = pipeline.format_chart_input(
        charts, facts, dt.date(2026, 7, 13), dt.date(2026, 7, 14), "질문"
    )
    assert "[코드 계산 — 이 값만 인용한다]" in text
    assert "합계 2,220,000" in text
    assert "처음→끝 1,240,000 → 980,000" in text
    assert "안내: 2026-07-13~2026-07-14 기간의 매출입니다." in text


def test_format_chart_rewrite_input_injects_feedback() -> None:
    """재작성 입력 — 이전 해석문 + 개선 지시(chart_verify 실패 사유)를 합산 주입."""
    from app.agents.seller.charts import chart_facts as _chart_facts

    spec = _line_chart()
    charts = ChartSet(charts=[spec])
    facts = [_chart_facts(spec)]
    text = pipeline.format_chart_rewrite_input(
        charts,
        facts,
        dt.date(2026, 7, 13),
        dt.date(2026, 7, 14),
        "매출 그래프",
        "이전 해석문 본문",
        "근거 없는 수치 999999 — [코드 계산] 값만 인용할 것",
    )
    assert "[이전 해석문]\n이전 해석문 본문" in text
    assert "[개선 지시]\n근거 없는 수치 999999" in text
    assert "처음부터 다시 작성하라" in text


def test_worker_progress_tokens_cover_all_analysis_types() -> None:
    """진행 token 은 AnalysisType 5종 전부를 커버한다(누락 시 모듈 로드도 실패)."""
    assert set(pipeline.WORKER_PROGRESS_TOKENS) == set(get_args(AnalysisType))


def test_progress_token_stages() -> None:
    """단계 token 키 계약 — 오케스트레이션(3-3~3-5)+graph(이슈 #242 5단계)가 소비."""
    assert set(pipeline.PROGRESS_TOKENS) == {
        "planner",
        "report",
        "verify",
        "recommend",
        "graph",
    }
    assert pipeline.ALL_WORKERS_FAILED_TOKEN.startswith("지금 데이터를 불러오는 중에")


# ── split_report_summary (이슈 #296 — report SSE summary 분리, §5.1 규칙) ────────


def test_split_report_summary_takes_first_paragraph() -> None:
    """정상 케이스 — 첫 빈 줄 전까지(핵심 요약 문단)를 그대로 반환한다."""
    report = (
        "지난달 매출이 12% 감소했습니다. 주말 급락이 원인입니다.\n\n상세 내용은 다음과 같습니다."
    )
    assert (
        pipeline.split_report_summary(report)
        == "지난달 매출이 12% 감소했습니다. 주말 급락이 원인입니다."
    )


def test_split_report_summary_no_blank_line_falls_back_to_truncation() -> None:
    """빈 줄 없는 통짜 장문 — 첫 문단 분리 실패로 보고 200자 절단 + 말줄임."""
    report = "가" * 500
    summary = pipeline.split_report_summary(report)
    assert summary == "가" * pipeline.SUMMARY_FALLBACK_CHARS + "…"


def test_split_report_summary_oversized_first_paragraph_falls_back() -> None:
    """첫 문단이 상한(300자) 초과 — 핵심 요약 가정이 깨진 것으로 보고 절단 fallback."""
    report = "나" * (pipeline.SUMMARY_FIRST_PARAGRAPH_MAX + 1) + "\n\n둘째 문단."
    summary = pipeline.split_report_summary(report)
    assert summary.startswith("나" * pipeline.SUMMARY_FALLBACK_CHARS)
    assert summary.endswith("…")


def test_split_report_summary_short_report_returns_whole() -> None:
    """빈 줄 없지만 200자 이하 — 전문 그대로(말줄임 없음)."""
    assert pipeline.split_report_summary("짧은 보고서.") == "짧은 보고서."


def test_split_report_summary_empty_report_returns_empty() -> None:
    """빈/공백 문자열 — ""(검증 루프 소진 degrade, FE 는 body fallback)."""
    assert pipeline.split_report_summary("") == ""
    assert pipeline.split_report_summary("   \n\n  ") == ""


def test_split_report_summary_skips_markdown_heading_blocks() -> None:
    """실측 사례(2026-08-05) — LLM 이 마크다운 구성으로 쓰면 "## 핵심 요약" 헤딩
    블록이 첫 문단이 된다. 헤딩은 요약이 아니므로 건너뛰고 첫 실제 문단을 잡는다."""
    report = (
        "## 핵심 요약\n\n"
        "매출 데이터 응답 형식 오류로 최근 7일 매출을 분석하지 못했습니다.\n\n"
        "## 발견 상세\n\n상세 내용입니다."
    )
    assert (
        pipeline.split_report_summary(report)
        == "매출 데이터 응답 형식 오류로 최근 7일 매출을 분석하지 못했습니다."
    )


def test_split_report_summary_heading_and_text_in_same_block() -> None:
    """헤딩과 본문이 빈 줄 없이 한 블록이어도 헤딩 줄만 제거하고 본문을 잡는다."""
    report = "## 핵심 요약\n지난달 매출이 12% 감소했습니다.\n\n다음 문단."
    assert pipeline.split_report_summary(report) == "지난달 매출이 12% 감소했습니다."


def test_split_report_summary_headings_only_returns_empty() -> None:
    """전부 헤딩뿐인 비정상 산출 — ""(FE 는 body fallback)."""
    assert pipeline.split_report_summary("## 제목\n\n### 소제목") == ""


# ── 비교(기준) 기간 (#346) ─────────────────────────────────────────────────────


def test_resolve_plan_resolves_comparison_expression() -> None:
    """[#346] comparison_expr 도 코드가 환산한다 — planner 는 표현만 옮겨적는다."""
    plan = _plan(analyses=["conversion"], period_expr="이번 달", comparison_expr="지난달 대비")

    resolved = pipeline.resolve_plan(
        plan, today=dt.date(2026, 8, 6), recent_default_days=7, max_days=731
    )

    assert (resolved.date_from, resolved.date_to) == (dt.date(2026, 8, 1), dt.date(2026, 8, 5))
    assert (resolved.compare_from, resolved.compare_to) == (
        dt.date(2026, 7, 1),
        dt.date(2026, 7, 5),
    )
    assert resolved.comparison_expr == "지난달 대비"


def test_resolve_plan_supplemented_is_the_union_of_both_periods() -> None:
    """본 기간이 명시적이어도 비교 기간이 보충됐으면 고지 대상이다(합집합)."""
    plan = _plan(
        analyses=["conversion"],
        period_expr="2026-06-01~2026-06-30",
        comparison_expr="작년 대비",
    )

    resolved = pipeline.resolve_plan(
        plan, today=dt.date(2026, 8, 6), recent_default_days=7, max_days=731
    )

    assert resolved.period_supplemented is True


def test_format_worker_input_injects_comparison_period() -> None:
    """비교 기간이 있으면 워커 입력에 한 줄 더 실린다 — 도구 시그니처는 그대로다.

    워커는 두 기간으로 같은 도구를 각각 호출한다(CONVERSION_PROMPT 절차 4) — 그래서
    Spring 계약도, 도구 인자도 건드리지 않고 비교가 성립한다.
    """
    plan = pipeline.ResolvedPlan(
        analyses=("conversion",),
        date_from=dt.date(2026, 8, 1),
        date_to=dt.date(2026, 8, 5),
        comparison_expr="지난달 대비",
        compare_from=dt.date(2026, 7, 1),
        compare_to=dt.date(2026, 7, 5),
    )

    body = pipeline.format_worker_input("지난달 대비 이번 달 전환율", plan)

    assert "[분석 기간] from=2026-08-01 to=2026-08-05" in body
    assert "[비교 기간] from=2026-07-01 to=2026-07-05" in body
    assert body.index("[분석 기간]") < body.index("[비교 기간]") < body.index("[판매자 질문]")


def test_format_worker_input_omits_comparison_line_when_absent() -> None:
    """비교가 없으면 줄 자체가 없다 — 빈 값을 흘려 워커가 오해하게 두지 않는다."""
    plan = pipeline.ResolvedPlan(
        analyses=("conversion",), date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 5)
    )

    assert "[비교 기간]" not in pipeline.format_worker_input("전환율", plan)
