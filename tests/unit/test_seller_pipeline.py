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
    """미지원 기간 표현("이번 달")은 normalize_period 의 ValueError 가 전파된다."""
    with pytest.raises(ValueError):
        pipeline.resolve_plan(
            _plan(period_expr="이번 달"), today=dt.date(2026, 7, 18), recent_default_days=7
        )


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
    assert "요청하신 차트를 만들지 못했습니다" in text

    empty_charts_text = pipeline.compose_response(
        "본문", RecommendationSet(), ChartSet(charts=[]), chart_requested=True
    )
    assert "[차트 안내]" in empty_charts_text


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
    assert pipeline.ALL_WORKERS_FAILED_TOKEN.startswith("죄송합니다")


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
