"""판매자 분석 파이프라인 입출력 계약 (SPEC-SELLER-001 §2, 3-1 — 2026-07-18 사용자 확정).

이 모듈은 파이프라인의 **계약 상수·순수 함수만** 둔다(LLM·IO 없음, verifier 와 동일 원칙):
- ResolvedPlan: AnalysisPlan(LLM 출력)의 기간을 코드가 환산한 내부 실행 계획.
- resolve_plan: AnalysisPlan → ResolvedPlan. 모든 불성립(clarification·빈 워커·환산
  실패)은 ValueError 로 통일 — 호출부(3-3)는 이를 받아 되묻기 token 으로 전환한다.
- format_worker_input / format_general_input: 입력 메시지 포맷(기간 주입 규약,
  장치 ④ 접속점). 두 레인이 같은 규약으로 기간을 받는다(#346).
- PROGRESS_TOKENS·WORKER_PROGRESS_TOKENS·ALL_WORKERS_FAILED_TOKEN: 진행 token 문구.

오케스트레이션(asyncio.gather 팬아웃·검증 루프·SSE 배선)은 3-3 이후 소관.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import get_args

from app.agents.seller import period as seller_period
from app.agents.seller.schemas import (
    AnalysisFinding,
    AnalysisPlan,
    AnalysisType,
    ChartSet,
    RecommendationSet,
)


# 차트 요청 키워드 검사 (이슈 #242 — LLM wants_chart 판정을 코드 키워드 검사로 보강).
# 문장 전체 매칭이 아니라 **존재 검사**다("N번 적용해줘"류 _APPLY_RE 와 다른 철학) —
# 차트 요청은 다른 발화와 섞여 와도("전환율 분석하고 그래프로 보여줘") 감지해야 한다.
_CHART_RE = re.compile(r"차트|그래프|시각화|도표|그려")


def wants_chart_keyword(message: str) -> bool:
    """차트 요청 키워드 존재 검사 — 레인 선판정(②.5)과 resolve_plan 이 공유하는 정본.

    [#531] 이 판정이 resolve_plan 안에만 있던 탓에 **analysis 레인 진입 이후**에야
    실행됐다. 그런데 차트 좌표(report 이벤트 charts[])는 그 레인에만 있고, supervisor 는
    "매출 그래프 보여줘"를 조회로 보아 general 로 보낸다 — 판정이 영영 도달하지 못하는
    순환이었다. 함수로 승격해 호출점을 레인 결정 **앞**(api/seller.py ②.5)으로 끌어올리되,
    정본은 여기 하나로 둔다(두 경로가 서로 다른 어휘를 갖지 않게).
    """
    return bool(_CHART_RE.search(message))


@dataclass(frozen=True)
class ResolvedPlan:
    """기간이 코드로 환산된 실행 계획 — LLM 비노출 내부 계약.

    analyses 는 tuple(불변) — AnalysisPlan validator 가 중복을 제거했고
    순서는 팬아웃에서 무의미하지만 진행 token 방출 순서로는 쓰인다.

    [#345] period 3필드는 확인 흐름(DESIGN-SELLER-PERIOD §5)용 확장이다.
    needs_confirmation 이 True 면 코드가 값을 보충한 해석이라 실행 전에 판매자
    확인을 받는다 — 확인 대기에 이 ResolvedPlan 을 통째로 저장했다가 승인 시
    그대로 재개하므로 planner 재호출이 0회가 된다(#269 완료 조건).
    신규 필드는 전부 default 있는 keyword 로만 추가한다(frozen dataclass —
    기존 생성부·픽스처의 positional 호환 유지, PipelineResult 와 같은 규약).
    """

    analyses: tuple[AnalysisType, ...]
    date_from: date
    date_to: date
    wants_chart: bool = False
    needs_confirmation: bool = False
    period_expr: str = ""
    period_clipped: bool = False
    # [#346] 비교(기준) 기간 — 판매자가 대조군을 함께 말했을 때만 채워진다.
    # needs_confirmation 은 **본 기간과 비교 기간의 합집합**이다(어느 쪽이든 코드가 값을
    # 보충했으면 확인을 받는다) — period.PeriodResolution.any_confirmation_needed 참조.
    comparison_expr: str = ""
    compare_from: date | None = None
    compare_to: date | None = None
    # [#504] 차트 전용 기간 — 판매자가 그래프 기간을 분석 기간과 별도로 말했을 때만
    # 채워진다("지난달 보고서 + 최근 7일 매출 그래프"). 비워지면 차트는 본 기간을 따른다.
    # 차트 기간은 확인(needs_confirmation) 대상이 **아니다** — 차트는 부가 가치이고,
    # 해석 결과가 report.chartPeriod 뱃지로 그대로 노출되므로 고지로 갈음한다
    # (DESIGN-SELLER-PERIOD §7.2 확인/고지 비대칭의 고지 측). 해석 실패는 파이프라인을
    # 죽이지 않고 chart_period_error 에 담아 chartUnavailable(chart_period_unclear)로
    # 강등한다 — 보고서는 살린다.
    chart_period_expr: str = ""
    chart_from: date | None = None
    chart_to: date | None = None
    chart_period_error: str = ""
    # [#504] 보고서 없이 차트만 원하는 턴 — 워커 팬아웃·보고서·추천을 생략한다.
    chart_only: bool = False


def resolve_plan(
    plan: AnalysisPlan,
    *,
    today: date,
    recent_default_days: int,
    max_days: int | None = None,
    question: str = "",
) -> ResolvedPlan:
    """AnalysisPlan(LLM) → ResolvedPlan(코드) — 불성립은 전부 ValueError.

    - plan.clarification 이 있으면 계획 불성립: 되묻기 질문을 그대로 ValueError
      메시지로 올린다(호출부가 token 으로 전달). [#345] planner 는 **기간을 이유로**
      clarification 을 쓰지 않는다 — 기간 문구의 소유자는 period.py 하나다
      (PLANNER_PROMPT `[기간]` 절 + AnalysisPlan.period_expr description).
      여기 남는 clarification 은 "어떤 분석을 원하는지 모를 때" 뿐이다.
    - analyses 가 비면 planner 오류로 간주하고 되묻기 처리. [#531] 단, 질문에 차트
      어휘가 있으면 되묻지 않고 chart_only 턴으로 승격한다 — 레인 선판정이 이미
      차트 요청으로 판정해 보낸 발화다(본문 주석 참조).
    - 기간 환산은 period.resolve_period 소관 — 해석 불가 표현("작년 여름" 등)의
      ValueError 도 그대로 전파된다. 그 메시지는 판매자에게 그대로 노출되므로
      period 쪽에서 안내문 형태로 쓴다(#269 P0 계약 승계).
    - max_days 미지정이면 period 가 Settings 에서 읽는다 — 상한 초과는 되묻기다.
    - wants_chart = plan.wants_chart OR wants_chart_keyword(question) — LLM 판정과
      코드 키워드 검사의 OR(이슈 #242). question 은 키워드 기본값(""라 매칭 안 됨)
      이라 기존 호출부(question 미전달)는 LLM 판정만 반영해 하위 호환된다.
      [#531] 같은 검사가 레인 선판정에서도 먼저 돌지만 여기 OR 를 걷어내지 않는다 —
      승인 재개(pending)·직접 호출처럼 선판정을 거치지 않는 진입로가 남아 있다.
    """
    if plan.clarification:
        raise ValueError(plan.clarification)
    # [#531] 차트 어휘로 레인 선판정(api/seller.py ②.5)에 걸려 여기 온 발화인데 planner 가
    # analyses 를 비우고 chart_only 도 안 걸면 되묻기로 끝난다 — ASCII 아트가 되묻기로
    # 바뀔 뿐 좌표(report.charts[])는 여전히 나가지 못하고, 이슈의 완료 조건이 planner
    # LLM 판정 하나에 매달린다. 선판정이 이미 "차트 요청"이라 판정한 근거가 있으므로
    # 코드가 chart_only 로 승격한다(§5 3층 분담 — 코드가 LLM 누락을 받아내는 층).
    # clarification 은 위에서 이미 갈렸다 — planner 가 되물을 이유를 댔으면 그쪽이 이긴다.
    chart_only = plan.chart_only or (not plan.analyses and wants_chart_keyword(question))
    if not plan.analyses and not chart_only:
        # [#504] chart_only 턴은 워커를 쓰지 않으므로 analyses 가 비어도 계획이 성립한다.
        raise ValueError(
            "어떤 분석을 원하시는지 파악하지 못했습니다. 조금 더 구체적으로 알려주세요."
        )
    resolution = seller_period.resolve_period(
        plan.period_expr,
        today=today,
        recent_default_days=recent_default_days,
        max_days=max_days,
    )
    if plan.comparison_expr:
        # [#346] 비교 기간도 같은 모듈이 환산한다 — general 레인과 어휘·경계 규칙이 하나다.
        resolution = replace(
            resolution,
            comparison=seller_period.resolve_comparison(
                plan.comparison_expr, resolution, max_days=max_days
            ),
        )
    # [#504] 차트 전용 기간 — 해석 실패는 ValueError 로 전파하지 않는다(보고서를 죽이지
    # 않고 chartUnavailable 로 강등). 확인 대상도 아니다 — ResolvedPlan 필드 주석 참조.
    chart_expr = plan.chart_period_expr.strip()
    chart_from: date | None = None
    chart_to: date | None = None
    chart_period_error = ""
    if chart_expr:
        try:
            chart_resolution = seller_period.resolve_period(
                chart_expr,
                today=today,
                recent_default_days=recent_default_days,
                max_days=max_days,
            )
        except ValueError as exc:
            chart_period_error = str(exc)
        else:
            chart_from = chart_resolution.date_from
            chart_to = chart_resolution.date_to
            chart_expr = chart_resolution.expr
    wants_chart = (
        plan.wants_chart or chart_only or bool(chart_expr) or wants_chart_keyword(question)
    )
    comparison = resolution.comparison
    return ResolvedPlan(
        analyses=tuple(plan.analyses),
        date_from=resolution.date_from,
        date_to=resolution.date_to,
        wants_chart=wants_chart,
        needs_confirmation=resolution.any_confirmation_needed,
        period_expr=resolution.expr,
        period_clipped=resolution.clipped,
        comparison_expr=comparison.expr if comparison else "",
        compare_from=comparison.date_from if comparison else None,
        compare_to=comparison.date_to if comparison else None,
        chart_period_expr=chart_expr,
        chart_from=chart_from,
        chart_to=chart_to,
        chart_period_error=chart_period_error,
        chart_only=chart_only,
    )


def period_confirmation_text(plan: ResolvedPlan) -> str:
    """확인 대기 문구 — 문구 생성은 period.py 소관이고 여기는 어댑터다(§4.2).

    ResolvedPlan 은 확인 대기 저장·재개의 단위(DESIGN §6)라 PeriodResolution 을
    따로 들고 다니지 않는다. 대신 필요한 3필드를 되돌려 문구를 만든다.
    """
    return seller_period.confirmation_text(
        seller_period.PeriodResolution(
            date_from=plan.date_from,
            date_to=plan.date_to,
            needs_confirmation=True,
            expr=plan.period_expr,
            clipped=plan.period_clipped,
            comparison=_plan_comparison(plan),
        )
    )


def _plan_comparison(plan: ResolvedPlan) -> seller_period.PeriodResolution | None:
    """ResolvedPlan 의 비교 3필드 → PeriodResolution 되돌리기 (#346).

    확인 문구·워커 입력이 둘 다 필요로 해 한 곳에 둔다. needs_confirmation 을 True 로
    두는 것은 문구 생성용이다 — 저장·재개 단위인 ResolvedPlan 은 합집합 판정만 들고 있고
    어느 쪽이 보충됐는지는 구분하지 않는다(문구가 양쪽 날짜를 다 밝히므로 필요 없다).
    """
    if plan.compare_from is None or plan.compare_to is None:
        return None
    return seller_period.PeriodResolution(
        date_from=plan.compare_from,
        date_to=plan.compare_to,
        needs_confirmation=True,
        expr=plan.comparison_expr,
    )


# ── 워커 입력 메시지 포맷 (기간 주입 규약 — prompts.WORKER_COMMON_RULES 접속점) ──

# 워커 프롬프트의 "기간은 입력에 주어진 from/to 를 그대로" 계약이 참조하는 포맷.
# 단일 기간만 주입한다(R3 두 기간 비교는 보류 — REVIEW-SELLER-STAGE2 §2).
WORKER_INPUT_TEMPLATE = """\
[분석 기간] from={date_from} to={date_to}
[판매자 질문] {question}"""

# [#346] 비교 기간 줄 — 있을 때만 [분석 기간] 뒤에 끼운다. 도구 시그니처는 바꾸지 않는다:
# 워커는 두 기간으로 같은 도구를 **각각** 호출한다(CONVERSION_PROMPT 가 이미 그렇게 지시).
COMPARISON_INPUT_LINE = "[비교 기간] from={date_from} to={date_to}"


def _comparison_line(plan: ResolvedPlan) -> str:
    if plan.compare_from is None or plan.compare_to is None:
        return ""
    return "\n" + COMPARISON_INPUT_LINE.format(
        date_from=plan.compare_from.isoformat(), date_to=plan.compare_to.isoformat()
    )


def format_worker_input(question: str, plan: ResolvedPlan) -> str:
    """워커에 넣을 HumanMessage 본문을 만든다 — 전 워커 공통 1건."""
    body = WORKER_INPUT_TEMPLATE.format(
        date_from=plan.date_from.isoformat(),
        date_to=plan.date_to.isoformat(),
        question=question,
    )
    head, _, tail = body.partition("\n")
    return f"{head}{_comparison_line(plan)}\n{tail}"


# general 레인 입력 포맷 (#346) — 워커 포맷과 **나란히** 둔다.
# 두 레인이 같은 모양으로 기간을 받는 것이 "같은 기간 표현 → 같은 (from, to)" 를
# 눈으로 확인할 수 있게 하는 지점이다. 라벨만 다르다(분석 기간 / 조회 기간) —
# general 은 분석이 아니라 조회 레인이라 판매자 대면 어휘를 맞춘다.
GENERAL_INPUT_TEMPLATE = """\
[조회 기간] from={date_from} to={date_to}
[판매자 질문] {question}"""


def format_general_input(question: str, resolution: seller_period.PeriodResolution) -> str:
    """general_agent 에 넣을 HumanMessage 본문 — 코드가 환산한 기간을 주입한다.

    ResolvedPlan 이 아니라 PeriodResolution 을 받는 이유: general 레인에는 planner 도
    분석 계획도 없고 필요한 것은 기간뿐이다. 굳이 ResolvedPlan 을 만들면 analyses 가
    빈 가짜 계획이 생겨 "계획이 있는데 워커가 없다"는 없는 상태가 하나 늘어난다.
    """
    body = GENERAL_INPUT_TEMPLATE.format(
        date_from=resolution.date_from.isoformat(),
        date_to=resolution.date_to.isoformat(),
        question=question,
    )
    if resolution.comparison is None:
        return body
    head, _, tail = body.partition("\n")
    comparison = COMPARISON_INPUT_LINE.format(
        date_from=resolution.comparison.date_from.isoformat(),
        date_to=resolution.comparison.date_to.isoformat(),
    )
    return f"{head}\n{comparison}\n{tail}"


# ── report·judge 입력 포맷 (3-4 검증 루프 — REPORT/JUDGE_PROMPT 의 "입력" 계약) ──


def format_findings_block(findings: list[AnalysisFinding]) -> str:
    """AnalysisFinding 목록을 report/judge 공용 번호 블록으로 직렬화한다.

    번호·유형·심각도·요약·근거를 사람이 읽는 형태로 — JSON 덤프보다 smart tier 서술
    품질이 안정적이고, judge 의 축별 대조(수치·누락)도 같은 표현을 본다.
    """
    lines: list[str] = []
    for i, finding in enumerate(findings, start=1):
        lines.append(
            f"{i}. [{finding.analysis_type}] (severity={finding.severity}) {finding.summary}"
        )
        for item in finding.evidence:
            lines.append(f"   - 근거: {item}")
        if finding.recommendation:
            lines.append(f"   - 조치 힌트: {finding.recommendation}")
    return "\n".join(lines)


def format_report_input(findings: list[AnalysisFinding]) -> str:
    """report_agent 1차 작성 입력."""
    return f"[분석 결과]\n{format_findings_block(findings)}"


def format_rewrite_input(findings: list[AnalysisFinding], prev_report: str, feedback: str) -> str:
    """report_agent 재작성 입력 — 결정론 실패 사유 + judge feedback 합산본을 주입한다."""
    return (
        f"[분석 결과]\n{format_findings_block(findings)}\n\n"
        f"[이전 보고서]\n{prev_report}\n\n"
        f"[개선 지시]\n{feedback}\n\n"
        "위 개선 지시를 반영해 보고서를 처음부터 다시 작성하라."
    )


def format_judge_input(findings: list[AnalysisFinding], report: str) -> str:
    """judge 채점 입력 — (1) 분석 결과 (2) 보고서 (JUDGE_PROMPT 입력 계약)."""
    return f"[분석 결과]\n{format_findings_block(findings)}\n\n[보고서]\n{report}"


# ── 브랜치 분석 검증 입력 포맷 (이슈 #242 — analysis_judge·워커 재실행 계약) ────


def format_analysis_judge_input(finding: AnalysisFinding, tool_outputs: list[str]) -> str:
    """analysis_judge 채점 입력 — (1) 도구 원출력 (2) finding (ANALYSIS_JUDGE_PROMPT 계약).

    tool_outputs 가 비면(도구 미호출·수확 실패) "(도구 출력 없음)"으로 명시한다 —
    빈 문자열을 그냥 붙이면 judge 가 입력 누락을 알아채지 못한다.
    """
    outputs_block = "\n".join(f"- {o}" for o in tool_outputs) or "(도구 출력 없음)"
    return f"[도구 원출력]\n{outputs_block}\n\n[분석 결과]\n{format_findings_block([finding])}"


def format_worker_retry_input(
    question: str, plan: ResolvedPlan, prev_finding: AnalysisFinding, feedback: str
) -> str:
    """브랜치 재실행 입력 — 원 지시(기간·질문) + 이전 finding + F/judge 합산 개선 지시.

    write_verified_report.format_rewrite_input 과 같은 철학(결정론 사유 + judge
    feedback 합산 주입)을 브랜치 단위로 적용한다.
    """
    return (
        f"{format_worker_input(question, plan)}\n\n"
        f"[이전 분석 결과]\n{format_findings_block([prev_finding])}\n\n"
        f"[개선 지시]\n{feedback}\n\n"
        "위 개선 지시를 반영해 도구를 다시 호출하고 분석을 재작성하라."
    )


def format_recommend_input(findings: list[AnalysisFinding], report: str) -> str:
    """recommend 입력 — (1) 분석 결과 (2) 검증된 보고서 (RECOMMEND_PROMPT 입력 계약)."""
    return f"[분석 결과]\n{format_findings_block(findings)}\n\n[검증된 보고서]\n{report}"


def format_graph_input(findings: list[AnalysisFinding], report: str, question: str) -> str:
    """graph 입력 — (1) 분석 결과 (2) 검증된 보고서 (3) 판매자 질문 (GRAPH_PROMPT 계약).

    recommend 입력과 같은 재료(findings+report)에 질문을 한 줄 더 얹는다 — 어떤
    축을 그릴지 판단하는 근거다(이슈 #242 5단계, D-4 — 도구 없이 기존 재료만 사용).
    """
    return (
        f"[분석 결과]\n{format_findings_block(findings)}\n\n"
        f"[검증된 보고서]\n{report}\n\n"
        f"[판매자 질문]\n{question}"
    )


# ── compose_response (3-5) — 최종 응답 조립 (순수 함수, SPEC §2 COMP) ──────────

# "N번 적용해줘" 안내 — §6.3 조회 계약(목록 순서=N번)의 사용자측 표면.
# [이슈 #296] SSE 계층(_report_event)도 report.data.applyGuide 로 내보내므로 공개 상수다.
APPLY_GUIDE = '적용을 원하시면 "N번 적용해줘"라고 말씀해 주세요.'


def compose_response(
    report: str,
    recommendations: RecommendationSet,
    charts: ChartSet | None = None,
    *,
    chart_requested: bool = False,
    chart_unavailable: Sequence[object] = (),
) -> str:
    """검증된 보고서 + 추천(+ 차트 안내)을 최종 응답 텍스트로 조립한다.

    번호("1번.")는 RecommendationSet 목록 순서 그대로 — §6.3 이 recommendations[N-1]
    로 조회하는 그 순서다(순서가 계약). 추천이 비면 보고서만(사유 summary 가 있으면
    한 줄 덧붙임) — 억지 추천 금지(RECOMMEND_PROMPT)와 짝.

    charts/chart_requested 는 이슈 #242 D-5 4자 확장 — 차트 자체는 텍스트에
    담기지 않는다(`report` 이벤트 charts[] 로 별도 직렬화, api/seller.py). 여기서는
    "차트를 요청했는데 만들지 못한 경우"만 안내를 덧붙인다.

    [#504] chart_unavailable 은 charts.ChartUnavailable 목록(구조적 타이핑 — 이 모듈은
    순수 계약이라 charts.py 를 import 하지 않는다). 사유가 있으면 그 완성 문장을 그대로
    싣고(원인을 아는데 뭉개면 오보), 없으면 종전처럼 일반 안내 한 줄이다 — 단 구
    "데이터 부족으로 생략합니다" 단정은 뺐다(원인을 모르는 상태에서 특정 원인을
    말하던 문장). 부분 성공(charts 도 있고 사유도 있음)에도 사유는 싣는다.
    """
    items = recommendations.recommendations
    if not items:
        text = (
            f"{report}\n\n[추천 행동]\n{recommendations.summary}"
            if recommendations.summary
            else report
        )
    else:
        lines = [report, "", "[추천 행동]"]
        if recommendations.summary:
            lines.append(recommendations.summary)
        for i, rec in enumerate(items, start=1):
            lines.append(f"{i}번. {rec.title}")
            if rec.expected_effect:
                lines.append(f"   기대 효과: {rec.expected_effect}")
        lines.append("")
        lines.append(APPLY_GUIDE)
        text = "\n".join(lines)

    notes = [str(getattr(item, "message", "")) for item in chart_unavailable]
    notes = [note for note in notes if note]
    if notes:
        text = f"{text}\n\n[차트 안내]\n" + "\n".join(notes)
    elif chart_requested and (charts is None or not charts.charts):
        text = f"{text}\n\n[차트 안내]\n요청하신 차트를 만들지 못했습니다."
    return text


# ── report SSE summary 분리 (이슈 #296 — api-spec §3.2 v0.24.0 `report.data.summary`) ──

# 첫 문단이 이 길이를 넘으면 "첫 문단 = 핵심 요약"(REPORT_PROMPT 구성 1) 가정이 깨진
# 것으로 보고 절단 fallback 으로 전환한다. 와이어 직렬화 규칙(계약 상수)이라 Settings
# 가 아닌 상수다 — schemas.MAX_RECOMMENDATIONS·CHART_MAX 와 동일 원칙.
SUMMARY_FIRST_PARAGRAPH_MAX = 300
SUMMARY_FALLBACK_CHARS = 200


def _strip_heading_lines(text: str) -> str:
    """마크다운 헤딩 줄("## 핵심 요약" 등)을 제거한다 — 요약 후보에서 제외.

    REPORT_PROMPT 은 산문을 강제하지만 LLM 이 마크다운 구성으로 쓰는 사례가
    실측됐다(2026-08-05 화면 캡처 — "## 핵심 요약" 단독 블록). 헤딩 줄을 요약으로
    잡으면 summary 가 제목 한 줄("## 핵심 요약")이 되므로 걸러낸다.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ).strip()


def split_report_summary(report: str) -> str:
    """보고서 산문에서 핵심 요약(첫 실제 문단)을 분리한다 — `report` 이벤트 summary 원천.

    REPORT_PROMPT 이 "1. 핵심 요약(2~3문장) 먼저"를 강제하지만 LLM 산출이라 어길 수
    있다 — 마크다운 헤딩만 있는 블록은 건너뛰고 **첫 실제 내용 문단**을 요약으로
    잡는다. 그 문단이 없거나 과장(> SUMMARY_FIRST_PARAGRAPH_MAX)이면 헤딩 제거본 앞
    SUMMARY_FALLBACK_CHARS 자 절단 + 말줄임으로 degrade 한다(보고서를 죽이지 않는다).
    report 가 빈/공백 문자열이면 ""(검증 루프 소진 degrade 케이스 — FE 는 body 로
    fallback). 순수 함수 — LLM·IO 없음(이 모듈 원칙).
    """
    stripped = report.strip()
    first = ""
    for block in stripped.split("\n\n"):
        candidate = _strip_heading_lines(block)
        if candidate:
            first = candidate
            break
    if first and len(first) <= SUMMARY_FIRST_PARAGRAPH_MAX:
        return first
    plain = _strip_heading_lines(stripped)
    if len(plain) > SUMMARY_FALLBACK_CHARS:
        return f"{plain[:SUMMARY_FALLBACK_CHARS]}…"
    return plain


# ── 진행 token 문구 (SPEC §2 — first-token 10s·체감 대기 완화, 2026-07-18 확정) ──

# 파이프라인 단계 진입 시 방출 — 키는 단계 식별자(오케스트레이션 3-3~3-5 가 소비).
# "graph"(이슈 #242 5단계)는 wants_chart 일 때만 emit 된다 — 미배선 상태에서는
# 이 키를 아무도 조회하지 않는다(6단계 배선 전까지 사전 등록만).
PROGRESS_TOKENS: dict[str, str] = {
    "planner": "질문을 분석하고 있습니다…",
    "report": "보고서를 작성하고 있습니다…",
    "verify": "보고서를 검증하고 있습니다…",
    "recommend": "개선 방안을 정리하고 있습니다…",
    "graph": "차트를 만들고 있습니다…",
}

# 워커 시작 시 유형별 방출 — AnalysisType 전 값을 커버해야 한다(테스트 강제).
WORKER_PROGRESS_TOKENS: dict[AnalysisType, str] = {
    "sales_anomaly": "매출 이상 분석 중…",
    "conversion": "구매전환 분석 중…",
    "behavior": "고객 행동 분석 중…",
    "churn": "고객 이탈 분석 중…",
    "abuse": "어뷰징 점검 중…",
    "review": "리뷰 분석 중…",  # [#297] I-31
}

# 전 워커 실패(집계 전부 실패) 시 사과 후 done 종료(SPEC §4·§7 degrade).
ALL_WORKERS_FAILED_TOKEN = (
    "죄송합니다. 지금 데이터 조회가 원활하지 않아 분석을 완료하지 못했습니다. "
    "잠시 후 다시 시도해 주세요."
)


def _assert_worker_tokens_cover_all_types() -> None:
    """모듈 로드 시 자기검증 — AnalysisType 추가 시 token 누락을 즉시 드러낸다."""
    missing = set(get_args(AnalysisType)) - set(WORKER_PROGRESS_TOKENS)
    if missing:
        raise RuntimeError(f"WORKER_PROGRESS_TOKENS 누락: {sorted(missing)}")


_assert_worker_tokens_cover_all_types()


# confirm 판정은 요청 스키마로 이관됐다 (2026-07-22, FE 계약 A-2): 승인은 message 가
# 아니라 최상위 `action`/`draftId` 구조화 필드로 받는다(app/schemas/seller.py
# SellerChatRequest). 입구 판정은 app/api/seller.py `_seller_stream` 이 request.action
# 으로 직접 수행한다 — 구 parse_confirm_message(message JSON 파싱)는 제거됐다.


# ── "N번 적용해줘" 코드 선판정 (4-3 §6.3 — 입구 ①.5, 2026-07-20 사용자 확정) ─────

# 문장 **전체**가 적용 발화일 때만 매칭 — "2번 상품에 할인 적용해줘" 같은 일반 수정
# 요청(여분 토큰 존재)은 통과시켜 supervisor 라우팅으로 흘린다(오매칭 방지).
# APPLY_GUIDE("N번 적용해줘")가 안내하는 정형 발화 + 가벼운 변형(추천/조사/존대)만.
_APPLY_RE = re.compile(
    r"^\s*(\d{1,3})\s*번(?:\s*추천)?(?:\s*[을를])?\s*적용"
    r"\s*(?:해\s*(?:줘|주세요|줘요)?|부탁해요?|하기)?\s*[.!?~]*\s*$"
)


def parse_apply_message(message: str) -> int | None:
    """메시지가 정형 추천 적용 발화("N번 적용해줘")면 N(1 이상)을, 아니면 None.

    코드 선판정 이유(confirm 과 동일 철학): N 추출은 어차피 코드 몫이고(§6.3 —
    대화 재해석 금지), LLM 라우팅은 정확도를 더해주지 않는다. 판정 후 실제 조회·
    변환은 history.apply_recommendation 이 수행한다.
    """
    match = _APPLY_RE.match(message)
    if not match:
        return None
    n = int(match.group(1))
    return n if n >= 1 else None
