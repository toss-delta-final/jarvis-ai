"""판매자 분석 워커 팩토리 (SPEC-SELLER-001 §2 — create_agent 사용, StateGraph 수작업 금지).

워커는 create_agent 로 만든다: fast tier(init_seller_model("worker")) +
배정표 도구(HANDOFF §3·SPEC §4) + response_format=ToolStrategy(AnalysisFinding).
신원은 context_schema=SellerContext 로 요청마다 주입된다(ToolRuntime, IDOR 방지) —
어떤 도구 시그니처에도 신원 인자가 없다.

입력 계약(전 워커 공통): 호출 메시지에 planner 가 정규화한 기간(from/to, 장치 ④)이
포함되어야 한다 — 워커는 날짜를 직접 계산하지 않는다(prompts.WORKER_COMMON_RULES).

2-4b~2-5 로 분석 워커 5종(sales_anomaly·conversion·behavior·churn·abuse) 완성.
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph

from app.agents.seller import tools as seller_tools
from app.agents.seller.context import SellerContext
from app.agents.seller.middleware import (
    ScopeGuardMiddleware,
    seller_pii_middlewares,
    tool_call_limit_middleware,
)
from app.agents.seller.models import init_seller_model
from app.agents.seller.prompts import (
    ABUSE_PROMPT,
    BEHAVIOR_PROMPT,
    CHURN_PROMPT,
    CONVERSION_PROMPT,
    GENERAL_PROMPT_TEMPLATE,
    JUDGE_PROMPT,
    PLANNER_PROMPT,
    PRODUCT_PROMPT,
    RECOMMEND_PROMPT,
    REPORT_PROMPT,
    SALES_ANOMALY_PROMPT,
    SUPERVISOR_PROMPT,
)
from app.agents.seller.schemas import (
    AnalysisFinding,
    AnalysisPlan,
    DraftProposal,
    RecommendationSet,
    ReportScore,
    RouteDecision,
)

# ── 배정표의 코드화 (HANDOFF §3) — 워커별 도구 목록의 단일 출처(쓰기 도구 포함 금지) ──

SALES_ANOMALY_TOOLS = [
    seller_tools.get_sales_timeseries,
    seller_tools.get_order_events,
    seller_tools.get_product_change_logs,
    seller_tools.search_analysis_guide,
]

CONVERSION_TOOLS = [
    seller_tools.get_funnel,
    seller_tools.search_analysis_guide,
]

BEHAVIOR_TOOLS = [
    seller_tools.get_behavior_events,
    seller_tools.get_funnel,
    seller_tools.search_analysis_guide,
]

# get_account_events 는 보조 소스(I-8 admin 소유 🔴) — 실패해도 주 소스로 계속(프롬프트).
CHURN_TOOLS = [
    seller_tools.get_churn_cohort,
    seller_tools.get_order_events,
    seller_tools.get_product_change_logs,
    seller_tools.get_account_events,
    seller_tools.search_analysis_guide,
]

ABUSE_TOOLS = [
    seller_tools.get_behavior_events,
    seller_tools.get_order_events,
    seller_tools.get_account_events,
    seller_tools.search_analysis_guide,
]


def _build_worker(system_prompt: str, tools: list[BaseTool]) -> CompiledStateGraph:
    """분석 워커 공통 조립 — fast tier · ToolStrategy(AnalysisFinding) · 신원 주입.

    미들웨어(3-6, 마감 리뷰 M1 반영): PII 정제 + ToolCallLimit — planner 의 PII
    미들웨어는 planner 모델 호출에만 적용될 뿐 원문 question 은 그대로 워커에
    전달되므로, 워커 각자가 입력을 정제해야 한다. scope 는 파이프라인 입구
    (orchestrator 코드 경로) 소관.
    """
    return create_agent(
        model=init_seller_model("worker"),
        tools=tools,
        system_prompt=system_prompt,
        response_format=ToolStrategy(AnalysisFinding),
        context_schema=SellerContext,
        middleware=[*seller_pii_middlewares(), tool_call_limit_middleware()],
    )


def build_sales_anomaly_agent() -> CompiledStateGraph:
    """매출 이상 분석 워커 (get_sales_timeseries 가 이상 판정을 내장 — LLM 은 해석만)."""
    return _build_worker(SALES_ANOMALY_PROMPT, SALES_ANOMALY_TOOLS)


def build_conversion_agent() -> CompiledStateGraph:
    """구매전환 분석 워커 (전환율은 get_funnel 이 계산 — 병목 식별·해석만)."""
    return _build_worker(CONVERSION_PROMPT, CONVERSION_TOOLS)


def build_behavior_agent() -> CompiledStateGraph:
    """고객 행동 분석 워커 (I-13 집계 주 소스 + 퍼널 보조 — 특이 패턴 해석)."""
    return _build_worker(BEHAVIOR_PROMPT, BEHAVIOR_TOOLS)


def build_churn_agent() -> CompiledStateGraph:
    """고객 이탈 분석 워커 (I-16 코호트 주 소스 + 주문/변경 이력 단서, I-8 보조)."""
    return _build_worker(CHURN_PROMPT, CHURN_TOOLS)


def build_abuse_agent() -> CompiledStateGraph:
    """어뷰징 탐지 워커 (I-13+I-14 조합이 주 소스 — I-8 확정 전, HANDOFF §3)."""
    return _build_worker(ABUSE_PROMPT, ABUSE_TOOLS)


# ── general_agent (2-6) — 분석 워커가 아닌 일반 질문 레인 ──────────────────────

GENERAL_TOOLS = [
    seller_tools.get_sales_timeseries,
    seller_tools.get_order_events,
    seller_tools.list_my_products,
    seller_tools.calculate,
    seller_tools.search_analysis_guide,
]


def build_general_agent(today: str) -> CompiledStateGraph:
    """일반 질문 에이전트 (해석 금지·calculate 강제·미지원 안내 — 자유 텍스트 응답).

    분석 워커와 달리 response_format 을 강제하지 않는다 — 3단계에서 astream→token
    SSE 1차 배선 대상이다. planner 를 거치지 않는 레인이라 기간 환산을 프롬프트가
    담당한다(2026-07-18 확정): today("YYYY-MM-DD")를 빌드 시점에 주입한다.

    Args:
        today: 오늘 날짜(YYYY-MM-DD) — 호출부(요청 시점)가 결정해 넘긴다.
    """
    return create_agent(
        model=init_seller_model("worker"),
        tools=GENERAL_TOOLS,
        system_prompt=GENERAL_PROMPT_TEMPLATE.format(today=today),
        context_schema=SellerContext,
        # 유일한 자유 텍스트 대면 에이전트 — scope 가드(end 점프)를 직접 붙인다(3-6).
        middleware=[
            ScopeGuardMiddleware(),
            *seller_pii_middlewares(),
            tool_call_limit_middleware(),
        ],
    )


# ── product_agent (2-7) — draft 생성까지, 쓰기는 4단계 confirm-resume 코드 경로 ──

# A안(2026-07-18 확정): 조회만 바인딩 — LLM 이 쓰기 도구를 볼 수 없어 HITL
# (발화 ≠ 동의 [HARD])이 프롬프트가 아니라 구조로 보장된다. 배정표(§3)의
# PRODUCT_TOOLS(쓰기 3종 포함)는 4단계 실행 레인용으로 유지된다.
# calculate 는 2-9 리뷰 반영(2026-07-18 사용자 확정) — 재고 증감 환산 암산 방지.
# 배정표 §3 개정 필요(REVIEW-SELLER-STAGE2 기록).
PRODUCT_DRAFT_TOOLS = [
    seller_tools.list_my_products,
    seller_tools.calculate,
]


def build_product_agent() -> CompiledStateGraph:
    """상품관리 draft 생성 에이전트 (fast tier · ToolStrategy(DraftProposal)).

    출력 계약: DraftProposal — clarification 이 비어있지 않으면 draft 불성립이며
    호출부가 되묻기 token 으로 전환한다. draftId 발급·interrupt·confirm-resume 은
    4단계 소관(SPEC §6.1) — 이 에이전트는 초안 변환까지만 담당한다.
    """
    return create_agent(
        model=init_seller_model("product"),
        tools=PRODUCT_DRAFT_TOOLS,
        system_prompt=PRODUCT_PROMPT,
        response_format=ToolStrategy(DraftProposal),
        context_schema=SellerContext,
        # 구조화 출력 레인 — scope end 점프 금지(계약 파손), PII·한도만(3-6).
        # scope 는 4단계 product 배선 시 check_scope 코드 경로로 처리한다.
        middleware=[*seller_pii_middlewares(), tool_call_limit_middleware()],
    )


# ── supervisor (4-1a) — 3분기 라우터: analysis / product / general ────────────


def build_supervisor() -> CompiledStateGraph:
    """3분기 라우터 (fast tier · 도구 없음 · ToolStrategy(RouteDecision)).

    출력 계약: RouteDecision(category/reason/confidence). 후처리는 전부 코드
    (orchestrator.route_question) 소관 — confidence 미달 = analysis 보수 재지정,
    장애 = general 폴백(REALIGN §4, 2026-07-19 사용자 결정). scope 선차단·confirm
    코드 선판정은 SSE 배선(4-1b) 입구에서 이 라우터보다 먼저 실행된다.
    """
    return create_agent(
        model=init_seller_model("supervisor"),
        tools=[],
        system_prompt=SUPERVISOR_PROMPT,
        response_format=ToolStrategy(RouteDecision),
        context_schema=SellerContext,
        # 구조화 출력 레인 — end 점프 금지, PII 정제만 (3-6 배정표).
        middleware=[*seller_pii_middlewares()],
    )


# ── analysis_planner (3-2) — 파이프라인 앞단: 워커 선택 + 기간 표현 분류 ────────


def build_analysis_planner() -> CompiledStateGraph:
    """분석 계획 수립자 (fast tier · 도구 없음 · ToolStrategy(AnalysisPlan)).

    출력 계약: AnalysisPlan — 기간 환산은 pipeline.resolve_plan(코드) 소관이며,
    불성립(clarification·빈 워커·미지원 기간)은 전부 ValueError → 되묻기 token
    경로다(3-1 확정). 시맨틱 캐시(§10-⑧)와 최근 5건 이력 주입(§9.1)은 4단계
    소관 — 이력은 프롬프트 변경 없이 입력 메시지로 주입될 예정이다.
    """
    return create_agent(
        model=init_seller_model("planner"),
        tools=[],
        system_prompt=PLANNER_PROMPT,
        response_format=ToolStrategy(AnalysisPlan),
        context_schema=SellerContext,
        # 구조화 출력 레인 — scope 는 orchestrator 코드 경로, 여기는 PII 정제만(3-6).
        middleware=[*seller_pii_middlewares()],
    )


# ── 분석 파이프라인 후단 (2-8) — report · judge · recommend ────────────────────
# 루프 배선(결정론 검사 → judge 21/30 → ≤3회 재작성)은 3단계 소관 — 여기는 빌더만.

RECOMMEND_TOOLS = [
    seller_tools.list_my_products,
    seller_tools.get_product_change_logs,
]


def build_report_agent() -> CompiledStateGraph:
    """보고서 작성 에이전트 (smart tier · 도구 없음 · 자유 텍스트).

    findings 는 3단계 파이프라인이 입력 메시지로 주입한다(배정표 §3 — 도구 없음).
    출력 보고서는 verifier(결정론 검사 + judge)를 통과해야 SSE 본문이 된다.
    """
    return create_agent(
        model=init_seller_model("report"),
        tools=[],
        system_prompt=REPORT_PROMPT,
        context_schema=SellerContext,
    )


def build_report_judge() -> CompiledStateGraph:
    """보고서 채점 judge (fast tier · ToolStrategy(ReportScore)).

    결정론 검사(verifier.run_deterministic_checks) 이후에 호출된다 — 21/30 판정과
    재작성 루프는 3단계 코드 소관이고, judge 는 축별 점수·feedback 만 낸다.
    """
    return create_agent(
        model=init_seller_model("judge"),
        tools=[],
        system_prompt=JUDGE_PROMPT,
        response_format=ToolStrategy(ReportScore),
        context_schema=SellerContext,
    )


def build_recommend_agent() -> CompiledStateGraph:
    """행동 추천 에이전트 (smart tier · 읽기 2종 · ToolStrategy(RecommendationSet)).

    출력은 save_history 가 순서 그대로 저장하는 §6.3 의 원천이다 — product_id 실존
    확인(list_my_products)과 중복 추천 회피(get_product_change_logs)를 프롬프트로
    강제한다. 쓰기 도구는 없다(추천은 제안일 뿐, 실행은 HITL 경로).
    """
    return create_agent(
        model=init_seller_model("recommend"),
        tools=RECOMMEND_TOOLS,
        system_prompt=RECOMMEND_PROMPT,
        response_format=ToolStrategy(RecommendationSet),
        context_schema=SellerContext,
        middleware=[tool_call_limit_middleware()],  # 읽기 2종 호출 상한(3-6)
    )
