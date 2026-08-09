"""app/agents/seller/schemas.py 구조화 출력 스키마 검증 (일관성 장치 ⑤ — Literal·ge/le)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.seller.schemas import (
    ActionRecommendation,
    ANALYSIS_SCORE_AXES,
    AnalysisFinding,
    AnalysisPlan,
    AnalysisScore,
    CHART_MAX,
    CHART_POINTS_MAX,
    ChartAxisPlan,
    ChartPlanSet,
    ChartPoint,
    ChartSeries,
    ChartSet,
    ChartSpec,
    DraftChange,
    DraftProposal,
    ProposedChange,
    RecommendationSet,
    ReportScore,
    RouteDecision,
    SCORE_AXES,
)


# ── 3-1: AnalysisPlan ────────────────────────────────────────────────────────


def test_analysis_plan_defaults_and_construction() -> None:
    """정상 계획 — period_expr 기본값은 '최근', clarification 기본값은 빈 문자열."""
    plan = AnalysisPlan(analyses=["sales_anomaly", "conversion"], reason="매출 급락 질문")
    assert plan.analyses == ["sales_anomaly", "conversion"]
    assert plan.period_expr == "최근"
    assert plan.clarification == ""
    assert plan.wants_chart is False


def test_analysis_plan_wants_chart_explicit_true() -> None:
    """wants_chart=True 를 명시하면 그대로 보존된다(이슈 #242 — resolve_plan OR 판정 재료)."""
    plan = AnalysisPlan(analyses=["sales_anomaly"], reason="추이", wants_chart=True)
    assert plan.wants_chart is True


def test_analysis_plan_dedupes_preserving_order() -> None:
    """중복 워커는 거부하지 않고 첫 등장만 남긴다(ToolStrategy 재시도 루프 방지)."""
    plan = AnalysisPlan(analyses=["churn", "sales_anomaly", "churn"], reason="이탈+매출")
    assert plan.analyses == ["churn", "sales_anomaly"]


def test_analysis_plan_rejects_unknown_type_and_overflow() -> None:
    """AnalysisType 밖 값·6종 초과([#297] review 추가로 5→6)는 거부."""
    with pytest.raises(ValidationError):
        AnalysisPlan(analyses=["revenue"], reason="r")
    with pytest.raises(ValidationError):
        AnalysisPlan(
            analyses=[
                "sales_anomaly",
                "conversion",
                "behavior",
                "churn",
                "abuse",
                "review",
                "sales_anomaly",
            ],
            reason="r",
        )  # max_length=6 은 validator(dedupe) 이전에 걸린다


def test_route_decision_accepts_valid_categories() -> None:
    """analysis/product/general 세 값 모두 정상 생성된다."""
    for category in ("analysis", "product", "general"):
        decision = RouteDecision(category=category, reason="근거", confidence=0.9)
        assert decision.category == category


def test_route_decision_rejects_unknown_category() -> None:
    """Literal 밖 카테고리는 ValidationError — LLM 이 신규값을 지어낼 수 없다."""
    with pytest.raises(ValidationError):
        RouteDecision(category="chitchat", reason="근거", confidence=0.9)


def test_route_decision_confidence_bounds() -> None:
    """confidence 는 0~1 범위를 벗어나면 거부된다(ge/le)."""
    with pytest.raises(ValidationError):
        RouteDecision(category="general", reason="근거", confidence=1.5)
    with pytest.raises(ValidationError):
        RouteDecision(category="general", reason="근거", confidence=-0.1)


def test_analysis_finding_full_construction() -> None:
    """정상 finding — 5종 Literal 유형과 심각도, 근거 목록이 그대로 보존된다."""
    finding = AnalysisFinding(
        analysis_type="sales_anomaly",
        summary="6월 12일 매출이 직전 7일 평균 대비 42% 급락했다.",
        evidence=["06-12 매출 180,000원 (직전 7일 평균 310,000원)", "동일 06-11 가격 인상 이력"],
        severity="warning",
        recommendation="가격 인상 폭 재검토",
    )
    assert finding.analysis_type == "sales_anomaly"
    assert len(finding.evidence) == 2
    assert finding.chart_data_hint == ""  # 차트 보류(§12) — 기본값 빈 문자열


def test_analysis_finding_rejects_unknown_type_and_severity() -> None:
    """analysis_type(5종)·severity(3종) Literal 위반은 거부된다."""
    with pytest.raises(ValidationError):
        AnalysisFinding(analysis_type="revenue", summary="s", severity="warning")
    with pytest.raises(ValidationError):
        AnalysisFinding(analysis_type="churn", summary="s", severity="fatal")


def test_analysis_finding_degrade_shape() -> None:
    """조회 실패 degrade finding(SPEC §4) — evidence 빈 목록·recommendation 생략 가능."""
    finding = AnalysisFinding(
        analysis_type="abuse",
        summary="데이터 확보 실패 — I-13/I-14 조회가 타임아웃되어 분석을 생략했다.",
        severity="info",
    )
    assert finding.evidence == []
    assert finding.recommendation == ""


# ── 2-2b: ReportScore · RecommendationSet ─────────────────────────────────────


def test_report_score_total_is_code_sum() -> None:
    """총점은 LLM 필드가 아니라 코드 property — 3축 합산이 그대로 나온다."""
    score = ReportScore(accuracy=8, completeness=7, clarity=6, feedback="근거 수치 보강 필요")
    assert score.total == 21  # 통과 임계(21/30)와 같은 값 — 판정 자체는 verifier 코드 소관


def test_report_score_axis_bounds() -> None:
    """축 점수는 0~10 을 벗어나면 거부된다(ge/le) — judge 가 배점을 지어낼 수 없다."""
    with pytest.raises(ValidationError):
        ReportScore(accuracy=11, completeness=5, clarity=5, feedback="f")
    with pytest.raises(ValidationError):
        ReportScore(accuracy=5, completeness=-1, clarity=5, feedback="f")


def test_score_axes_constant_matches_model_fields() -> None:
    """SCORE_AXES(확장 지점)는 실제 모델 필드와 어긋나면 안 된다 — total 합산의 안전망."""
    for axis in SCORE_AXES:
        assert axis in ReportScore.model_fields


# ── 이슈 #242: AnalysisScore (분석 검증 층, ReportScore 와 축 이름 분리) ────────


def test_analysis_score_total_is_code_sum() -> None:
    """총점은 LLM 필드가 아니라 코드 property — 3축 합산이 그대로 나온다."""
    score = AnalysisScore(grounding=8, sufficiency=7, relevance=6, feedback="근거 보강 필요")
    assert score.total == 21  # ReportScore 와 같은 임계 눈금(21/30)


def test_analysis_score_axis_bounds() -> None:
    """축 점수는 0~10 을 벗어나면 거부된다(ge/le) — judge 가 배점을 지어낼 수 없다."""
    with pytest.raises(ValidationError):
        AnalysisScore(grounding=11, sufficiency=5, relevance=5, feedback="f")
    with pytest.raises(ValidationError):
        AnalysisScore(grounding=5, sufficiency=-1, relevance=5, feedback="f")


def test_analysis_score_axes_constant_matches_model_fields() -> None:
    """ANALYSIS_SCORE_AXES(확장 지점)는 실제 모델 필드와 어긋나면 안 된다."""
    for axis in ANALYSIS_SCORE_AXES:
        assert axis in AnalysisScore.model_fields


def test_analysis_score_axes_distinct_from_report_score_axes() -> None:
    """두 judge 가 보는 대상이 다르므로 축 이름을 공유하지 않는다(설계 §4.1)."""
    assert set(ANALYSIS_SCORE_AXES).isdisjoint(SCORE_AXES)


# ── 이슈 #242: ChartSet (graph_agent 구조화 출력, FE SellerAnalysis 정렬 — 결정 D-2) ──


def _chart(**overrides: object) -> ChartSpec:
    base: dict = {
        "title": "일별 매출",
        "chart_type": "line",
        "unit": "KRW",
        "series": [ChartSeries(label="매출", points=[ChartPoint(x="07-01", y=1240000)])],
    }
    base.update(overrides)
    return ChartSpec(**base)


def test_chart_spec_defaults_and_construction() -> None:
    """정상 차트 — summary 기본값은 빈 문자열."""
    chart = _chart()
    assert chart.chart_type == "line"
    assert chart.unit == "KRW"
    assert chart.summary == ""
    assert chart.series[0].points[0].x == "07-01"
    assert chart.series[0].points[0].y == 1240000


def test_chart_spec_rejects_unknown_chart_type_and_unit() -> None:
    """chart_type·unit 은 Literal 밖 값을 거부한다(LLM 이 신규값을 지어낼 수 없다)."""
    with pytest.raises(ValidationError):
        _chart(chart_type="pie")
    with pytest.raises(ValidationError):
        _chart(unit="USD")


def test_chart_spec_series_capped_at_one() -> None:
    """series 상한은 1(결정 D-3) — FE AnalysisChart.tsx 가 series[0] 만 그린다."""
    with pytest.raises(ValidationError):
        _chart(
            series=[
                ChartSeries(label="A", points=[ChartPoint(x="1", y=1)]),
                ChartSeries(label="B", points=[ChartPoint(x="1", y=2)]),
            ]
        )


def test_chart_series_points_capped() -> None:
    """points 상한은 CHART_POINTS_MAX — 시계열 과다 방지."""
    with pytest.raises(ValidationError):
        ChartSeries(
            label="매출",
            points=[ChartPoint(x=str(i), y=i) for i in range(CHART_POINTS_MAX + 1)],
        )


def test_chart_set_charts_capped_at_max() -> None:
    """charts 상한은 CHART_MAX(3) — 스키마 계약(와이어 아님)이라 상수."""
    with pytest.raises(ValidationError):
        ChartSet(charts=[_chart() for _ in range(CHART_MAX + 1)])


def test_chart_set_allows_empty() -> None:
    """그릴 게 없으면 빈 목록을 허용한다(억지 차트 금지)."""
    assert ChartSet().charts == []


def test_chart_spec_aggregate_default_and_rating_unit() -> None:
    """[#504] aggregate 기본 sum(하위 호환) + RATING 단위·avg/none 집계 허용."""
    assert _chart().aggregate == "sum"
    rating = _chart(unit="RATING", aggregate="avg")
    assert rating.unit == "RATING" and rating.aggregate == "avg"
    snapshot = _chart(aggregate="none")
    assert snapshot.aggregate == "none"
    with pytest.raises(ValidationError):
        _chart(aggregate="median")


def test_chart_axis_plan_vocab() -> None:
    """[#504] 축 선언 어휘 — 지원 어휘는 통과, Literal 밖 값은 거부(LLM 신조어 차단)."""
    plan = ChartAxisPlan(x_axis="date", y_axis="sales")
    assert plan.title == ""
    other = ChartAxisPlan(x_axis="other", y_axis="other", title="퍼널 단계별 이탈률")
    assert other.title == "퍼널 단계별 이탈률"
    with pytest.raises(ValidationError):
        ChartAxisPlan(x_axis="funnel", y_axis="sales")
    with pytest.raises(ValidationError):
        ChartAxisPlan(x_axis="date", y_axis="revenue")


def test_chart_plan_set_capped_and_allows_empty() -> None:
    """[#504] 축 선언도 CHART_MAX(3) 상한 + 빈 목록 허용(억지 차트 금지 승계)."""
    assert ChartPlanSet().charts == []
    with pytest.raises(ValidationError):
        ChartPlanSet(
            charts=[ChartAxisPlan(x_axis="date", y_axis="sales") for _ in range(CHART_MAX + 1)]
        )


def test_analysis_plan_chart_fields_504() -> None:
    """[#504] chart_period_expr·chart_only 기본값과 명시 설정."""
    plan = AnalysisPlan(analyses=["sales_anomaly"], reason="r")
    assert plan.chart_period_expr == "" and plan.chart_only is False
    chart_only = AnalysisPlan(
        analyses=[], reason="r", chart_only=True, chart_period_expr="최근 7일"
    )
    assert chart_only.chart_only is True and chart_only.chart_period_expr == "최근 7일"


def test_recommendation_set_preserves_order() -> None:
    """목록 순서가 곧 'N번'(§6.3) — recommendations[N-1] 조회 계약을 보존한다."""
    first = ActionRecommendation(
        action_type="price_adjust",
        product_id=101,
        title="1번: 감귤청 가격 10% 인하",
        rationale="매출 급락 3일이 가격 인상 직후와 겹침",
        changes=[ProposedChange(field="price", after="12900")],
        expected_effect="전환율 회복",
    )
    second = ActionRecommendation(
        action_type="description_update",
        product_id=102,
        title="2번: 상세 설명에 용량 표기 추가",
        rationale="상세 이탈률이 유사 상품 대비 높음",
        changes=[ProposedChange(field="description", after="500ml 대용량...")],
    )
    rec_set = RecommendationSet(recommendations=[first, second], summary="가격·설명 2건")
    assert rec_set.recommendations[0].title.startswith("1번")
    assert rec_set.recommendations[1].action_type == "description_update"
    assert rec_set.recommendations[0].changes[0].after == "12900"  # 수치도 str 통일


def test_action_recommendation_rejects_unknown_action_type_and_field() -> None:
    """action_type(5종)·ProposedChange.field(8종) Literal 위반은 거부된다."""
    with pytest.raises(ValidationError):
        ActionRecommendation(action_type="discount_event", product_id=101, title="t", rationale="r")
    with pytest.raises(ValidationError):
        ProposedChange(field="seller_id", after="x")  # 신원 필드는 애초에 8종에 없다


def test_action_recommendation_requires_product_id() -> None:
    """product_id 는 필수(2026-07-18 확정) — 없으면 draft 변환이 불가능하다."""
    with pytest.raises(ValidationError):
        ActionRecommendation(action_type="promotion", title="t", rationale="r")


def test_recommendation_set_degrade_and_max_length() -> None:
    """빈 목록 degrade 는 허용, 5건 초과는 거부된다(max_length)."""
    assert RecommendationSet().recommendations == []
    one = ActionRecommendation(action_type="promotion", product_id=101, title="t", rationale="r")
    assert one.changes == []  # promotion — 필드 변경 없는 유형은 changes 빈 목록
    with pytest.raises(ValidationError):
        RecommendationSet(recommendations=[one] * 6)


# ── 2-7: DraftChange · DraftProposal ─────────────────────────────────────────


def test_draft_proposal_update_shape() -> None:
    """update draft — before/after 쌍이 보존되고 draftId 필드는 존재하지 않는다."""
    draft = DraftProposal(
        op="update",
        product_id=101,
        changes=[DraftChange(field="price", before="15000", after="12900")],
        summary="가격 12,900원으로 인하",
    )
    assert draft.changes[0].before == "15000"
    assert draft.clarification == ""  # 기본값 — draft 성립 상태
    assert "draft_id" not in DraftProposal.model_fields  # draftId 는 코드 발급(4단계)


def test_draft_proposal_delete_as_status_change() -> None:
    """delete draft — soft delete 를 status <조회값>→DELETED 1건으로 가시화한다.

    `HIDDEN`(숨김·판매정지)이 아니다 — 숨김은 판매자 목록에 남아 되돌릴 수 있고 삭제는
    목록에서도 빠지며 되돌릴 수 없다(api-spec §4.5).
    """
    draft = DraftProposal(
        op="delete",
        product_id=102,
        changes=[DraftChange(field="status", before="ON_SALE", after="DELETED")],
        summary="상품 삭제(물리 삭제는 아니나 복구 불가)",
    )
    assert draft.changes[0].after == "DELETED"


def test_draft_proposal_clarification_pattern() -> None:
    """대상 모호 시 — clarification 채움 + changes 비움 + product_id 기본 null(F2 숫자 전환)."""
    draft = DraftProposal(
        op="update",
        summary="",
        clarification="'감귤' 상품이 3건입니다. 어느 상품인가요? (p-1/p-2/p-3)",
    )
    assert draft.clarification != ""  # 호출부가 되묻기 token 으로 전환하는 판정 재료
    assert draft.changes == []
    assert draft.product_id is None  # [변경 2026-07-19] 숫자 전환 — create/미정은 null


def test_draft_rejects_unknown_op_and_field() -> None:
    """op(3종)·DraftChange.field(8종) Literal 위반은 거부된다."""
    with pytest.raises(ValidationError):
        DraftProposal(op="archive", summary="s")
    with pytest.raises(ValidationError):
        DraftChange(field="brand_id", before="", after="x")  # 신원 필드는 8종에 없다
