"""판매자 분석 파이프라인 오케스트레이션 (SPEC-SELLER-001 §2·§4·§7 — 3-3 팬아웃).

pipeline.py(순수 계약, LLM·IO 없음)와 달리 이 모듈은 **LLM 실행·비동기 IO**를 가진다.
설계서의 Send 팬아웃은 create_agent 확정(HANDOFF §1 — StateGraph 수작업 조립 금지)에
따라 순수 파이썬 asyncio.gather 로 구현한다. 검증 루프(3-4)·compose(3-5)도 여기 쌓인다.

진행 token 은 Emit 콜백으로 방출한다 — SSE 계층(3-7~)이 큐 넣기 함수를 꽂고,
테스트는 리스트 수집 함수를 꽂는다(2026-07-18 확정).

degrade 수렴 3층(§4·§7):
- 도구 실패("Error:" 문자열) → 워커 자신이 degrade finding 반환(프롬프트 규약, 코드 무개입).
- 워커 예외·타임아웃·구조화 출력 누락 → 코드가 degrade finding 생성(본 모듈).
- 선택 워커 **전부 예외** → AllWorkersFailedError → 호출부가 사과 token 후 done.
  워커가 스스로 반환한 degrade finding 은 실패로 세지 않는다(문자열 판정 의존 회피).

브랜치 분석 검증(이슈 #242, DESIGN-ANALYSIS-V31-242): run_branches/_run_one_branch 가
팬아웃 단위에 F1~F3(verifier.run_finding_checks) + analysis_judge 검증을 추가한다.
이 검증의 미달(F 잔존 강등·judge 미달)은 위 3층 "워커 예외"와 **다른 신호**다 —
3층 집계에 섞이면 F 미달이 흔한 상황에서 AllWorkersFailedError 가 오발동한다(R3).
후단(write_verified_report 이하)은 이 변경으로 무영향이다 — [vf.finding for vf in ...]
로 평범한 AnalysisFinding 목록을 그대로 받는다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph

from app.agents.seller import history
from app.agents.seller import thread as seller_thread
from app.agents.seller.context import SellerContext
from app.agents.seller.middleware import check_scope
from app.agents.seller.models import SellerRole, seller_trace_model_metadata
from app.agents.seller.pipeline import (
    ALL_WORKERS_FAILED_TOKEN,
    PROGRESS_TOKENS,
    WORKER_PROGRESS_TOKENS,
    ResolvedPlan,
    compose_response,
    format_analysis_judge_input,
    format_graph_input,
    format_judge_input,
    format_recommend_input,
    format_report_input,
    format_rewrite_input,
    format_worker_input,
    format_worker_retry_input,
    resolve_plan,
)
from app.agents.seller.schemas import (
    AnalysisFinding,
    AnalysisPlan,
    AnalysisScore,
    AnalysisType,
    ChartSet,
    RecommendationSet,
    ReportScore,
    RouteDecision,
)
from app.agents.seller.verifier import (
    run_chart_checks,
    run_deterministic_checks,
    run_finding_checks,
)
from app.agents.seller.workers import (
    build_abuse_agent,
    build_analysis_judge,
    build_analysis_planner,
    build_behavior_agent,
    build_churn_agent,
    build_conversion_agent,
    build_graph_agent,
    build_recommend_agent,
    build_report_agent,
    build_report_judge,
    build_sales_anomaly_agent,
    build_supervisor,
)
from app.core.config import Settings, get_settings
from app.core.llm import LLMNotConfigured
from app.core.tracing import current_request_trace, trace_span

logger = logging.getLogger(__name__)

# 진행 token 방출 콜백 — SSE 계층이 주입한다(예: 큐 put). 테스트는 리스트 수집.
Emit = Callable[[str], Awaitable[None]]

# 배정표(HANDOFF §3)의 실행판 — AnalysisType 전 값 커버를 테스트가 강제한다.
WORKER_BUILDERS: dict[AnalysisType, Callable[[], CompiledStateGraph]] = {
    "sales_anomaly": build_sales_anomaly_agent,
    "conversion": build_conversion_agent,
    "behavior": build_behavior_agent,
    "churn": build_churn_agent,
    "abuse": build_abuse_agent,
}


def _llm_metadata(role: SellerRole) -> dict[str, str] | None:
    return seller_trace_model_metadata(role)


def _mark_degraded(reason: str) -> None:
    if trace := current_request_trace():
        trace.mark_degraded(reason)


# ── supervisor 라우팅 (4-1a) ──────────────────────────────────────────────────

# 폴백 사유 문구 — 회귀 테스트·로그가 참조하는 계약값(코드 단일 출처).
ROUTE_FALLBACK_REASON = "라우팅 장애 — general 폴백(코드 지정)"
# [#180, 2026-07-29] 저신뢰 폴백 역전 — 구 ROUTE_CONSERVATIVE_REASON(analysis 보수
# 재지정, 2026-07-19 결정) 폐기. 오분류 비용 비대칭이 전제와 반대였다: 단순 조회가
# analysis 로 가면 5단 파이프라인(회복 불가·최고 비용), 분석 질문이 general 로 가면
# general 프롬프트의 "분석을 요청해 주세요" 안내로 한 턴에 회복된다.
ROUTE_LOW_CONFIDENCE_REASON = "confidence 미달 — general 재지정(코드 지정)"


async def route_question(
    question: str,
    context: SellerContext,
    recent_turns: Sequence[seller_thread.Turn] = (),
) -> RouteDecision:
    """supervisor 3분기 라우팅 + 코드 후처리 (4-1a, REALIGN §4 → #180 개정).

    코드가 최종 판정한다(LLM 은 제안만):
      - supervisor 장애(타임아웃·예외·비정형 출력) → **general 폴백** + warning
        로그(2026-07-19 사용자 결정 — MVP '작동 우선', 최소한의 답변 보장).
      - confidence < settings.seller_route_confidence_min → **general 재지정**
        (#180, 2026-07-29 — 구 'analysis 보수 재지정'(SPEC 장치 ⑤)을 역전.
        불확실하면 가장 가벼운 레인에서 답하고, 분석이 필요하면 general 의
        분석 안내로 회복한다). 원분류가 general 이면 재지정 불필요.
        장애 폴백과 방향이 일치한다 — "불확실하면 general" 단일 원칙.
    scope 선차단·confirm 코드 선판정은 호출부(SSE 배선) 소관 — 이 함수는
    라우팅만 담당한다(관심사 분리).

    recent_turns(대화 스레드 최근 턴)는 **입력 메시지에만** 주입한다 — 프롬프트
    불변(§9.1 이력 주입 선례). "그럼 지난주는?" 류 후속 발화가 직전 대화 맥락으로
    분류된다. 맥락이 없으면 질문 원문 그대로다(기존 계약 불변).
    """
    settings = get_settings()
    supervisor_input = seller_thread.build_contextual_input(question, recent_turns)
    try:
        supervisor = build_supervisor()
        with trace_span("llm.seller.supervisor", "llm", _llm_metadata("supervisor")):
            result = await asyncio.wait_for(
                supervisor.ainvoke(
                    {"messages": [HumanMessage(content=supervisor_input)]},
                    context=context,
                ),
                timeout=settings.seller_route_timeout_s,
            )
        decision = result.get("structured_response")
        if not isinstance(decision, RouteDecision):
            raise TypeError("supervisor 가 RouteDecision 을 반환하지 않았다")
    except LLMNotConfigured:
        raise
    except Exception:
        logger.warning("supervisor 라우팅 장애 — general 폴백", exc_info=True)
        return RouteDecision(category="general", reason=ROUTE_FALLBACK_REASON, confidence=0.0)
    if (
        decision.category != "general"
        and decision.confidence < settings.seller_route_confidence_min
    ):
        logger.info(
            "라우팅 저신뢰 재지정: %s(%.2f) → general (%s)",
            decision.category,
            decision.confidence,
            decision.reason,
        )
        return RouteDecision(
            category="general",
            reason=f"{ROUTE_LOW_CONFIDENCE_REASON} — 원분류 {decision.category}: {decision.reason}",
            confidence=decision.confidence,
        )
    return decision


class AllWorkersFailedError(RuntimeError):
    """선택된 워커 전부가 예외로 실패 — 호출부는 ALL_WORKERS_FAILED_TOKEN 후 done(§7)."""


def _degrade_finding(analysis_type: AnalysisType, cause: str) -> AnalysisFinding:
    """워커 예외를 degrade finding 으로 변환 — D3 탐지 문자열("확보 실패") 유지."""
    return AnalysisFinding(
        analysis_type=analysis_type,
        summary=f"데이터 확보 실패 — 분석 실행 오류({cause})",
        evidence=[],
        severity="info",
    )


def _harvest_tool_outputs(messages: Sequence[object]) -> list[str]:
    """ToolMessage 원출력을 F2 근거 대조 재료로 수확한다(이슈 #242 — 도구출력⊇finding).

    이슈 원안은 "팬인 시점에 ToolMessage 가 소실되어 검증이 구조적으로 불가능"
    이라 적었으나 실측 결과는 다르다 — ainvoke 결과의 messages 는 이 함수 호출
    시점(브랜치 내부)까지는 살아 있고, 그동안 `_run_one_worker` 가 structured_response
    만 읽고 버렸을 뿐이다. 그래서 LangGraph 구조 변경 없이 수확 지점만 추가한다.
    """
    return [_content_to_text(msg.content) for msg in messages if isinstance(msg, ToolMessage)]


async def _run_one_worker(
    analysis_type: AnalysisType,
    message: str,
    context: SellerContext,
    timeout_s: float,
) -> tuple[AnalysisFinding, list[str]]:
    """워커 1종 실행 — 요청마다 빌드(C1 철학·상태 공유 방지), (finding, 도구 원출력) 반환.

    타임아웃·예외는 여기서 처리하지 않고 올린다 — 수렴은 run_branches 소관.
    """
    with trace_span(f"seller.worker.{analysis_type}", "chain"):
        agent = WORKER_BUILDERS[analysis_type]()
        with trace_span("llm.seller.worker", "llm", _llm_metadata("worker")):
            result = await asyncio.wait_for(
                agent.ainvoke({"messages": [HumanMessage(content=message)]}, context=context),
                timeout=timeout_s,
            )
    finding = result.get("structured_response")
    if not isinstance(finding, AnalysisFinding):
        raise TypeError(f"워커 {analysis_type} 가 AnalysisFinding 을 반환하지 않았다")
    tool_outputs = _harvest_tool_outputs(result.get("messages", []))
    return finding, tool_outputs


# ── 브랜치 분석 검증 (3-3 확장, 이슈 #242) — F1~F3 결정론 + analysis_judge ──────


@dataclass(frozen=True)
class VerifiedFinding:
    """브랜치 1개의 분석 검증 결과 — run_branches 팬인·후단(write_verified_report)
    입력 재료. degrade 3층(§4·§7)에는 **워커 단계 예외만** 산입되고, 여기 담긴
    F/judge 미달(passed=False)은 별도 관측 대상이다(R3 — 오발동 방지).
    """

    finding: AnalysisFinding
    passed: bool
    attempts: int
    failed_checks: tuple[str, ...]
    last_score: AnalysisScore | None
    degraded: bool  # F 잔존으로 강등됐는가(True 면 finding 은 이미 degrade 형)


async def _score_finding(
    finding: AnalysisFinding,
    tool_outputs: list[str],
    context: SellerContext,
    settings: Settings,
) -> AnalysisScore:
    """analysis_judge 1회 채점 — analysis_judge_timeout_s(워커 타임아웃과 분리, §9-R1)."""
    judge_agent = build_analysis_judge()
    judge_input = format_analysis_judge_input(finding, tool_outputs)
    with trace_span("llm.seller.analysis_judge", "llm", _llm_metadata("analysis_judge")):
        judge_result = await asyncio.wait_for(
            judge_agent.ainvoke(
                {"messages": [HumanMessage(content=judge_input)]},
                context=context,
            ),
            timeout=settings.seller_analysis_judge_timeout_s,
        )
    score = judge_result.get("structured_response")
    if not isinstance(score, AnalysisScore):
        raise TypeError("analysis_judge 가 AnalysisScore 를 반환하지 않았다")
    return score


async def _run_one_branch(
    analysis_type: AnalysisType,
    question: str,
    plan: ResolvedPlan,
    context: SellerContext,
    settings: Settings,
) -> VerifiedFinding:
    """브랜치 1개: 워커 실행 → F1~F3 + analysis_judge → 미달 시 ≤N회 재실행(핵심 로직).

    - 워커 예외(초기 호출)는 여기서 처리하지 않고 올린다 — run_branches 가 기존
      run_workers 와 동일하게 degrade+3층 판정으로 수렴한다(worker 단계 예외만 산입).
    - F 미달·judge 미달·judge 장애는 예외로 올리지 않는다 — 이 함수 안에서
      소진되며 VerifiedFinding.passed=False 로 반환된다(3층 미산입, R3).
    - judge 장애(타임아웃 등, seller_analysis_judge_timeout_s 초과 시 실제 발생 가능)
      시에도 F 미달이 남아있으면 재실행 없이 곧장 강등한다 — F 미달 finding 을
      "미검증 채택"으로 흘리면 이 PR의 핵심(도구 출력 ⊇ finding 근거 사슬 검증)이
      judge 장애 한 번에 무력화된다. F 가 전부 통과한 상태의 judge 장애만
      미검증 채택(Q2 와 동일 철학 — 재실행 없이 현재 finding 인정)한다.
    - seller_branch_deadline_s 예산을 넘기면 재실행을 포기하고 직전 결과를 채택한다
      (강등이 아니라 "시간 내 못 끝냄" — §9-R1). [PR 리뷰 반영] 데드라인 통과 여부만
      보면 "새 재실행을 시작은 하되 완료는 못 해 오히려 더 초과"하는 경우를 못
      막는다 — 재실행 1회(worker+judge)를 끝까지 완주할 잔여 예산이 있을 때만
      재실행하도록 판단한다(아래 can_retry).
    """
    deadline = time.monotonic() + settings.seller_branch_deadline_s
    max_attempts = settings.seller_worker_max_retries + 1
    threshold = settings.seller_analysis_score_threshold
    # 재실행 1회의 최악 소요(worker 풀타임아웃 + judge 풀타임아웃) — 잔여 예산이 이보다
    # 작으면 재실행을 시작해도 도중에 예산을 넘길 뿐이라 애초에 시작하지 않는다.
    retry_cycle_cost_s = (
        settings.seller_worker_timeout_s + settings.seller_analysis_judge_timeout_s
    )
    message = format_worker_input(question, plan)

    finding, tool_outputs = await _run_one_worker(
        analysis_type, message, context, settings.seller_worker_timeout_s
    )
    last_score: AnalysisScore | None = None
    failed_checks: list[str] = []
    attempt = 1

    while True:
        failed_checks = run_finding_checks(finding, tool_outputs, expected_type=analysis_type)
        try:
            score = await _score_finding(finding, tool_outputs, context, settings)
        except LLMNotConfigured:
            raise
        except Exception as exc:
            if failed_checks:
                logger.warning(
                    "analysis_judge %s %d회차 실패(%r) — F 미달 잔존으로 강등",
                    analysis_type,
                    attempt,
                    exc,
                )
                degraded = _degrade_finding(analysis_type, "분석 검증 미달")
                return VerifiedFinding(degraded, False, attempt, tuple(failed_checks), None, True)
            logger.warning(
                "analysis_judge %s %d회차 실패(%r) — 미검증 채택", analysis_type, attempt, exc
            )
            return VerifiedFinding(finding, False, attempt, (), None, False)

        last_score = score
        if not failed_checks and score.total >= threshold:
            return VerifiedFinding(finding, True, attempt, (), score, False)

        # [PR 리뷰 반영] "데드라인 통과 전"이 아니라 "재실행 1회를 완주할 잔여 예산이
        # 있는가"로 판단한다 — 잔여가 retry_cycle_cost_s 보다 작으면 재실행을 시작해도
        # 도중에 예산을 넘길 뿐이라 애초에 포기한다(§9-R1 의도한 상한 실효화 방지).
        remaining_s = deadline - time.monotonic()
        can_retry = attempt < max_attempts and remaining_s >= retry_cycle_cost_s
        if not can_retry:
            break

        feedback = "\n".join([*failed_checks, score.feedback])
        retry_message = format_worker_retry_input(question, plan, finding, feedback)
        try:
            finding, tool_outputs = await _run_one_worker(
                analysis_type, retry_message, context, settings.seller_worker_timeout_s
            )
        except LLMNotConfigured:
            raise
        except Exception as exc:
            logger.warning("브랜치 %s 재실행 실패(%r) — 이전 finding 채택", analysis_type, exc)
            break
        attempt += 1

    if failed_checks:
        degraded = _degrade_finding(analysis_type, "분석 검증 미달")
        return VerifiedFinding(degraded, False, attempt, tuple(failed_checks), last_score, True)
    return VerifiedFinding(finding, False, attempt, (), last_score, False)


async def run_branches(
    question: str,
    plan: ResolvedPlan,
    context: SellerContext,
    *,
    emit: Emit,
) -> list[VerifiedFinding]:
    """선택된 브랜치를 병렬 실행하고 VerifiedFinding 목록으로 수렴한다 (팬아웃 → 팬인, §2).

    구 run_workers(3-3)의 팬아웃·degrade 3층 판정을 그대로 유지하되, 브랜치마다
    F1~F3 + analysis_judge 검증을 추가한다(이슈 #242). **3층 판정은 워커 단계
    예외만 산입** — F/judge 미달은 _run_one_branch 안에서 소진되고 예외로 올라오지
    않으므로 여기 집계에 섞이지 않는다(R3, 전 워커 F미달이 사과 응답으로 오발동하는
    문제 방지).

    - 시작 시 워커별 진행 token 을 계획 순서대로 emit(first-token·체감 대기, §7).
    - 실행은 asyncio.gather 병렬 — 반환 순서는 plan.analyses 순서를 유지한다.
    - 일부 실패는 degrade finding 으로 수렴해 부분 보고서로 계속(§4).
    - provider 미구성은 전역 설정 오류라 degrade하지 않고 API 경계까지 전파한다.
    - 전부 예외면 AllWorkersFailedError — 부분 보고서조차 불가능한 경우만이다.
    """
    settings = get_settings()

    for analysis_type in plan.analyses:
        await emit(WORKER_PROGRESS_TOKENS[analysis_type])

    results = await asyncio.gather(
        *(_run_one_branch(t, question, plan, context, settings) for t in plan.analyses),
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, LLMNotConfigured):
            raise result

    verified: list[VerifiedFinding] = []
    failures = 0
    for analysis_type, result in zip(plan.analyses, results, strict=True):
        if isinstance(result, BaseException):
            failures += 1
            logger.warning("분석 브랜치 %s 실패: %r", analysis_type, result)
            cause = "응답 시간 초과" if isinstance(result, asyncio.TimeoutError) else "내부 오류"
            degraded = _degrade_finding(analysis_type, cause)
            verified.append(VerifiedFinding(degraded, False, 0, (), None, True))
        else:
            verified.append(result)

    if failures and failures == len(plan.analyses):
        _mark_degraded("all_workers_failed")
        raise AllWorkersFailedError("선택된 분석 워커가 전부 실패했다")
    if failures:
        _mark_degraded("worker_degrade")
    # F/judge 미달(VerifiedFinding.passed=False)은 SELLER_DEGRADE_REASON_PRECEDENCE
    # (app/core/tracing.py — 닫힌 4종 계약)에 새 사유를 추가하지 않는 한 여기서
    # _mark_degraded 하지 않는다. 관측은 7단계(구조화 로그: F2 발화율·judge 미달률
    # 등, DESIGN-ANALYSIS-V31-242 §6)에서 별도 로그로 다룬다 — degradeReason 계약
    # 확장은 그 자체로 owner 승인이 필요한 변경이라 1단계 범위 밖이다.
    if any(not vf.passed for vf in verified if vf.attempts > 0):
        logger.info(
            "브랜치 검증 미달 %d/%d건",
            sum(1 for vf in verified if not vf.passed and vf.attempts > 0),
            len(verified),
        )
    return verified


# ── 검증 루프 (3-4) — 결정론 검사 + judge 채점 → ≤N회 재작성 (SPEC §10-⑦) ──────


@dataclass(frozen=True)
class VerifiedReport:
    """검증 루프 결과 — save_history(4단계)·로그·테스트 재료.

    passed=False 는 두 경우다: 루프 소진(미달 채택, §7) 또는 루프 중 LLM 장애로
    기존 보고서를 채택(Q2 결정). attempts 는 완료된 작성 시도 수.
    """

    report: str
    passed: bool
    attempts: int
    last_score: ReportScore | None


def _content_to_text(content: object) -> str:
    """provider별 문자열·블록 메시지 content를 텍스트로 정규화한다."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return str(content)


async def write_verified_report(
    findings: list[AnalysisFinding],
    context: SellerContext,
    *,
    emit: Emit,
) -> VerifiedReport:
    """보고서 작성 → 검증 → 재작성 루프 (판정은 전부 코드 소관, LLM 필드 없음).

    한 시도 = report(smart) 작성 → 결정론 검사(D1~D3) → judge(fast) 채점.
    통과 = 결정론 실패 0건 AND score.total >= Settings 임계(21/30).
    미달 시 결정론 사유 + judge feedback 을 **합산**해 재작성에 주입한다
    (2026-07-18 확정 — 결정론 실패여도 judge 는 항상 실행).

    degrade(Q2, 2026-07-18 위임 결정 — 추후 변경 가능):
    - 루프 소진 → 마지막 보고서 채택 + warning 로그 (§7).
    - 재작성/judge 중 LLM 장애 → 이미 가진 보고서를 미달 채택(passed=False).
      1차 작성부터 실패하면 보고서가 없으므로 예외 전파(호출부 사과 경로).
    """
    settings = get_settings()
    timeout_s = settings.seller_worker_timeout_s
    threshold = settings.seller_report_score_threshold
    max_attempts = settings.seller_report_max_retries

    report_agent = build_report_agent()
    judge_agent = build_report_judge()

    report: str | None = None
    last_score: ReportScore | None = None
    feedback = ""

    for attempt in range(1, max_attempts + 1):
        await emit(PROGRESS_TOKENS["report"])
        message = (
            format_report_input(findings)
            if attempt == 1
            else format_rewrite_input(findings, report or "", feedback)
        )
        try:
            llm_role = "report" if attempt == 1 else "rewrite"
            with trace_span(
                f"llm.seller.{llm_role}",
                "llm",
                _llm_metadata("report"),
            ):
                result = await asyncio.wait_for(
                    report_agent.ainvoke(
                        {"messages": [HumanMessage(content=message)]},
                        context=context,
                    ),
                    timeout=timeout_s,
                )
            report = _content_to_text(result["messages"][-1].content)
        except Exception as exc:
            if report is None:
                raise  # 1차 작성 실패 — 내보낼 보고서가 없다(호출부 사과 경로)
            logger.warning("보고서 재작성 %d회차 실패(%r) — 기존 보고서 미달 채택", attempt, exc)
            _mark_degraded("partial_report")
            return VerifiedReport(report, passed=False, attempts=attempt - 1, last_score=last_score)

        await emit(PROGRESS_TOKENS["verify"])
        det_reasons = run_deterministic_checks(report, findings)
        try:
            with trace_span("llm.seller.judge", "llm", _llm_metadata("judge")):
                judge_result = await asyncio.wait_for(
                    judge_agent.ainvoke(
                        {"messages": [HumanMessage(content=format_judge_input(findings, report))]},
                        context=context,
                    ),
                    timeout=timeout_s,
                )
            score = judge_result.get("structured_response")
            if not isinstance(score, ReportScore):
                raise TypeError("judge 가 ReportScore 를 반환하지 않았다")
        except Exception as exc:
            logger.warning("judge %d회차 실패(%r) — 현재 보고서 미검증 채택", attempt, exc)
            _mark_degraded("partial_report")
            return VerifiedReport(report, passed=False, attempts=attempt, last_score=last_score)

        last_score = score
        if not det_reasons and score.total >= threshold:
            return VerifiedReport(report, passed=True, attempts=attempt, last_score=score)

        feedback = "\n".join([*det_reasons, score.feedback])
        logger.info(
            "보고서 검증 미달 %d회차 — 결정론 %d건, 점수 %d/%d",
            attempt,
            len(det_reasons),
            score.total,
            threshold,
        )

    logger.warning("보고서 검증 %d회 미달 — 마지막 보고서 채택(§7 degrade)", max_attempts)
    _mark_degraded("partial_report")
    return VerifiedReport(report or "", passed=False, attempts=max_attempts, last_score=last_score)


# ── recommend + 파이프라인 통합 (3-5) — SPEC §2 REC·COMP ───────────────────────


async def run_recommend(
    findings: list[AnalysisFinding],
    report: str,
    context: SellerContext,
    *,
    emit: Emit,
) -> RecommendationSet:
    """행동 추천 실행 — 실패는 빈 추천으로 degrade(보고서를 죽이지 않는다).

    추천은 부가 가치다: LLM 장애·타임아웃·구조화 출력 실패(6건 초과
    ValidationError — 이월 C2 포함)가 나도 검증된 보고서는 그대로 나간다.
    빈 RecommendationSet 은 §6.3 조회 시 "해당 추천 없음" 경로로 자연 합류한다.
    """
    await emit(PROGRESS_TOKENS["recommend"])
    agent = build_recommend_agent()
    try:
        with trace_span("llm.seller.recommend", "llm", _llm_metadata("recommend")):
            result = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": [HumanMessage(content=format_recommend_input(findings, report))]},
                    context=context,
                ),
                timeout=get_settings().seller_worker_timeout_s,
            )
        recommendations = result.get("structured_response")
        if not isinstance(recommendations, RecommendationSet):
            raise TypeError("recommend 가 RecommendationSet 을 반환하지 않았다")
        return recommendations
    except Exception as exc:
        logger.warning("recommend 실패(%r) — 추천 없이 계속(C2 degrade)", exc)
        return RecommendationSet(recommendations=[], summary="")


# ── graph (이슈 #242) — 차트 생성 + G1 검증. wants_chart 일 때만 run_analysis_pipeline
# 이 asyncio.gather(run_recommend, run_graph) 로 호출한다(3단계 배선 — 아래 참조) ──


async def run_graph(
    findings: list[AnalysisFinding],
    report: str,
    question: str,
    context: SellerContext,
    *,
    emit: Emit,
) -> ChartSet:
    """차트 생성 실행 — 실패는 빈 ChartSet 으로 degrade(보고서를 죽이지 않는다, C2 대칭).

    차트도 recommend 와 같은 부가 가치다: LLM 장애·타임아웃·구조화 출력 실패가
    나도 검증된 보고서는 그대로 나간다. G1(verifier.run_chart_checks)이 미달
    ChartSpec 을 드랍한다 — 보고서 검증(D)과 달리 재작성 루프는 없다.

    호출부(run_analysis_pipeline)는 resolved.wants_chart 일 때만 이 함수를
    run_recommend 와 asyncio.gather 로 병렬 호출한다 — 원치 않는 요청까지
    graph LLM 콜을 태우지 않는다(비용·wall-clock 절약, SPEC-SELLER-001 §1-12
    조정표의 "chart 는 전달 경로 없어 보류" 원칙이 3단계로 해소된다).
    """
    await emit(PROGRESS_TOKENS["graph"])
    agent = build_graph_agent()
    try:
        with trace_span("llm.seller.graph", "llm", _llm_metadata("graph")):
            result = await asyncio.wait_for(
                agent.ainvoke(
                    {
                        "messages": [
                            HumanMessage(content=format_graph_input(findings, report, question))
                        ]
                    },
                    context=context,
                ),
                timeout=get_settings().seller_worker_timeout_s,
            )
        charts = result.get("structured_response")
        if not isinstance(charts, ChartSet):
            raise TypeError("graph 가 ChartSet 을 반환하지 않았다")
    except Exception as exc:
        logger.warning("graph 실패(%r) — 차트 없이 계속(C2 대칭)", exc)
        return ChartSet(charts=[])

    passed, dropped = run_chart_checks(charts, findings)
    if dropped:
        logger.info("차트 드랍 %d건(G1): %s", len(dropped), "; ".join(dropped))
    return passed


@dataclass(frozen=True)
class PipelineResult:
    """분석 파이프라인 최종 산출 — SSE 계층·save_history(4단계)가 소비한다.

    kind: report(정상 보고서) / clarification(되묻기 — 파이프라인 미실행) /
    apology(전 워커 실패 사과). text 는 세 경우 모두 사용자에게 보낼 최종 문안.
    """

    kind: Literal["report", "clarification", "apology", "refused"]
    text: str
    verified: VerifiedReport | None = None
    recommendations: RecommendationSet | None = None
    charts: ChartSet | None = None


async def run_analysis_pipeline(
    question: str,
    context: SellerContext,
    *,
    today: date,
    emit: Emit,
    recent_turns: Sequence[seller_thread.Turn] = (),
) -> PipelineResult:
    """분석 레인 전체: planner → resolve → 팬아웃 → 검증 루프 → recommend → compose.

    되묻기(계획 불성립·미지원 기간)와 전 워커 실패 사과는 예외가 아니라
    PipelineResult 로 반환한다 — 호출부(SSE)는 kind 와 무관하게 text 를 token 으로
    흘리고 done 하면 된다. **예외 전파는 두 경우다**: planner 자체 장애, 그리고
    1차 보고서 작성 실패(Q2 — 내보낼 보고서가 없음). 호출부는 둘 다 사과/error
    경로로 처리해야 한다.

    scope 가드(3-6): 구조화 출력 레인은 end 점프 미들웨어를 쓸 수 없어(계약 파손)
    **파이프라인 입구에서 check_scope 코드 검사**로 차단한다 — LLM 호출 0회 거절.
    """
    settings = get_settings()

    refusal = check_scope(question)
    if refusal:
        return PipelineResult(kind="refused", text=refusal)

    # 4-3 §9.1: 최근 이력을 planner **입력 메시지**에 주입(프롬프트 불변).
    # 이력은 부가 맥락 — 조회 실패는 주입 없이 계속(분석을 죽이지 않는다).
    planner_input = question
    try:
        entries = await history.load_recent(context.seller_id)
        planner_input = history.build_planner_input(question, entries)
    except Exception:
        logger.warning("분석 이력 조회 실패 — 이력 주입 없이 진행", exc_info=True)

    # 대화 스레드 최근 턴 주입 — 순서: [최근 대화] → [최근 분석 이력] → [이번 질문].
    # 이력 블록이 없으면(원문 그대로면) [이번 질문] 라벨은 대화 블록 쪽이 단다.
    conversation_block = seller_thread.render_recent_turns(recent_turns)
    if conversation_block:
        if planner_input == question:
            planner_input = seller_thread.build_contextual_input(question, recent_turns)
        else:
            planner_input = f"{conversation_block}\n\n{planner_input}"

    await emit(PROGRESS_TOKENS["planner"])
    planner = build_analysis_planner()
    with trace_span("llm.seller.planner", "llm", _llm_metadata("planner")):
        result = await asyncio.wait_for(
            planner.ainvoke({"messages": [HumanMessage(content=planner_input)]}, context=context),
            timeout=settings.seller_worker_timeout_s,
        )
    plan = result.get("structured_response")
    if not isinstance(plan, AnalysisPlan):
        raise TypeError("planner 가 AnalysisPlan 을 반환하지 않았다")

    try:
        resolved = resolve_plan(
            plan,
            today=today,
            recent_default_days=settings.seller_recent_days_default,
            question=question,
        )
    except ValueError as exc:
        return PipelineResult(kind="clarification", text=str(exc))

    try:
        verified_branches = await run_branches(question, resolved, context, emit=emit)
    except AllWorkersFailedError:
        return PipelineResult(kind="apology", text=ALL_WORKERS_FAILED_TOKEN)
    findings = [vf.finding for vf in verified_branches]

    verified = await write_verified_report(findings, context, emit=emit)

    # wants_chart 일 때만 graph 를 recommend 와 병렬 실행한다(이슈 #242, 3단계 배선) —
    # 요청 없는 대다수 질문에서 불필요한 LLM 콜·wall-clock 을 추가하지 않는다.
    if resolved.wants_chart:
        recommendations, charts = await asyncio.gather(
            run_recommend(findings, verified.report, context, emit=emit),
            run_graph(findings, verified.report, question, context, emit=emit),
        )
    else:
        recommendations = await run_recommend(findings, verified.report, context, emit=emit)
        charts = None

    # 4-3 §9.1: compose 후 save_history — §6.3 "N번 적용해줘"·planner 주입의 원천.
    # 저장 실패는 응답을 죽이지 않는다(이력은 부가 데이터 — degrade + warning).
    try:
        await history.save_history(
            context.seller_id,
            question=question,
            analyses=list(resolved.analyses),
            date_from=resolved.date_from.isoformat(),
            date_to=resolved.date_to.isoformat(),
            report=verified.report,
            recommendations=recommendations,
        )
    except Exception:
        logger.warning("분석 이력 저장 실패 — 응답은 계속", exc_info=True)

    return PipelineResult(
        kind="report",
        text=compose_response(
            verified.report,
            recommendations,
            charts,
            chart_requested=resolved.wants_chart,
        ),
        verified=verified,
        recommendations=recommendations,
        charts=charts,
    )
