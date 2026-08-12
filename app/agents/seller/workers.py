"""판매자 분석 워커 팩토리 (SPEC-SELLER-001 §2 — create_agent 사용, StateGraph 수작업 금지).

워커는 create_agent 로 만든다: smart tier(init_seller_model("worker")) +
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
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from app.agents.seller import tools as seller_tools
from app.agents.seller.context import SellerContext
from app.agents.seller.middleware import (
    ModelUsageObservationMiddleware,
    ScopeGuardMiddleware,
    ToolCallObservationMiddleware,
    seller_pii_middlewares,
    tool_call_limit_middleware,
)
from app.agents.seller.models import SellerRole, init_seller_model, seller_trace_model_metadata
from app.agents.seller.prompts import (
    ABUSE_PROMPT,
    ANALYSIS_JUDGE_PROMPT,
    BEHAVIOR_INTERPRET_PROMPT,
    BEHAVIOR_PROMPT,
    CHART_INTERPRET_PROMPT,
    CHURN_INTERPRET_PROMPT,
    CHURN_PROMPT,
    CONVERSION_INTERPRET_PROMPT,
    CONVERSION_PROMPT,
    GENERAL_PROMPT_TEMPLATE,
    GRAPH_PROMPT,
    JUDGE_PROMPT,
    PLANNER_PROMPT,
    PRODUCT_PROMPT,
    RECOMMEND_PROMPT,
    REPORT_PROMPT,
    RESIDENT_RECOMMEND_PROMPT,
    RESIDENT_REPORT_PROMPT,
    REVIEW_PROMPT,
    SALES_ANOMALY_INTERPRET_PROMPT,
    SALES_ANOMALY_PROMPT,
    SUPERVISOR_PROMPT,
)
from app.agents.seller.schemas import (
    AnalysisFinding,
    AnalysisPlan,
    AnalysisScore,
    BehaviorFinding,
    ChartPlanSet,
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

# [#481] I-8 은 2026-08-06 자사 코호트 전환으로 abuse 전용 보조 소스가 됐다 —
# churn 은 더 쓰지 않는다(WITHDRAW 는 member 에 탈퇴 필드가 없어 원래부터 상시 0건).
CHURN_TOOLS = [
    seller_tools.get_churn_cohort,
    seller_tools.get_order_events,
    seller_tools.get_product_change_logs,
    seller_tools.search_analysis_guide,
]

ABUSE_TOOLS = [
    seller_tools.get_behavior_events,
    seller_tools.get_order_events,
    seller_tools.get_account_events,
    seller_tools.search_analysis_guide,
]

# [#297] 리뷰 분석 워커 — I-31(질의 시점 조회, 원문 저장 금지). 매출 급락일과 리뷰를
# 교차하는 질문은 planner 가 sales_anomaly 와 함께 선택한다(워커 간 도구 공유 없음).
REVIEW_TOOLS = [
    seller_tools.get_reviews,
    seller_tools.search_analysis_guide,
]


def _model_usage_middleware(role: SellerRole) -> ModelUsageObservationMiddleware:
    metadata = seller_trace_model_metadata(role)
    return ModelUsageObservationMiddleware(metadata["model"] if metadata is not None else None)


def _build_worker(system_prompt: str, tools: list[BaseTool]) -> CompiledStateGraph:
    """분석 워커 공통 조립 — smart tier · ToolStrategy(AnalysisFinding) · 신원 주입.

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
        middleware=[
            _model_usage_middleware("worker"),
            *seller_pii_middlewares(),
            tool_call_limit_middleware(),
            ToolCallObservationMiddleware(),
        ],
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


def build_review_agent() -> CompiledStateGraph:
    """리뷰 분석 워커 (I-31 — 집계 먼저, 저평점 원문 인용. VISIBLE 리뷰만, #297)."""
    return _build_worker(REVIEW_PROMPT, REVIEW_TOOLS)


# ── general_agent (2-6) — 분석 워커가 아닌 일반 질문 레인 ──────────────────────

# [#591] search 레인 도구 12종 — supervisor 의 `general`(조회)과 `analysis`(저장된 보고서를
# 찾는 의도)가 **같은 이 레인**을 쓴다. 조회 11종 + 보고서 조회 1종이고 쓰기는 0개다.
#
# search_analysis_guide 를 뺀 이유: 영구 스텁이라 항상 "Error:" 를 돌려주는데, LLM 은 쓸 수
# 있는 도구로 보고 용어 질문("장바구니 전환율이 뭐야?")에 호출했다가 그 실패를 판매자에게
# 그대로 안내한다 — 도구가 없느니만 못하다. 용어 설명은 GENERAL_PROMPT 의 "용어·서비스
# 설명" 절이 이미 담당한다. 함수와 분석 워커 6종의 바인딩은 그대로 둔다(상주 파이프라인 소관).
GENERAL_TOOLS = [
    seller_tools.get_sales_timeseries,
    seller_tools.get_funnel,  # [#591] I-7 퍼널
    seller_tools.get_behavior_events,  # [#591] I-13 행동 이벤트
    seller_tools.get_order_events,
    # [#297] I-29 현재 상태 스냅샷("신규 주문 뭐 있어?") — 전이 이력(I-14)과 역할 분리.
    seller_tools.get_orders,
    seller_tools.get_product_change_logs,  # [#591] I-15 변경 이력
    seller_tools.get_churn_cohort,  # [#591] I-16 이탈 코호트
    seller_tools.get_account_events,  # [#591] I-8 계정 이벤트
    # [#297] I-31 리뷰 단순 조회("최근 리뷰 보여줘") — 요약·해석은 하지 않는다(프롬프트 1번).
    seller_tools.get_reviews,
    seller_tools.list_my_products,
    seller_tools.calculate,
    # [#591] 보고서 조회 도구는 이것 하나뿐 — 목록 브라우징은 보고서 페이지의 일이다.
    seller_tools.get_latest_report,
]


def build_general_agent(
    today: str, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    """일반 질문 에이전트 (경량 해석 허용·calculate 강제·미지원 안내 — 자유 텍스트 응답, #650).

    [#650] 단일 지표 증감·순위·임계값 비교 같은 경량 해석까지는 직접 답한다 — 원인
    가설·복수 지표 교차·행동 추천은 여전히 금지(GENERAL_PROMPT_TEMPLATE 응답 원칙 1).

    분석 워커와 달리 response_format 을 강제하지 않는다 — 3단계에서 astream→token
    SSE 1차 배선 대상이다. planner 를 거치지 않는 레인이라 기간 환산을 프롬프트가
    담당한다(2026-07-18 확정): today("YYYY-MM-DD")를 빌드 시점에 주입한다.

    checkpointer 가 주어지면 대화 스레드 누적이 활성화된다 — 호출부(app/api/seller.py)
    가 thread.chat_config 의 thread_id 를 함께 넘겨 멀티턴 대화를 잇는다. 유일한
    대화형 레인이라 general 만 붙인다(2026-07-29 확정) — one-shot 구조화 출력
    에이전트(supervisor/planner 등)는 checkpointer 없이 입력 메시지 주입으로 맥락을
    받는다. 요청마다 재빌드(C1)해도 상태는 checkpointer 에 있어 스레드는 이어진다.

    Args:
        today: 오늘 날짜(YYYY-MM-DD) — 호출부(요청 시점)가 결정해 넘긴다.
        checkpointer: 공용 checkpointer(checkpoint.get_checkpointer) — 미지정 시 무상태.
    """
    return create_agent(
        model=init_seller_model("worker"),
        tools=GENERAL_TOOLS,
        system_prompt=GENERAL_PROMPT_TEMPLATE.format(today=today),
        context_schema=SellerContext,
        checkpointer=checkpointer,
        # 유일한 자유 텍스트 대면 에이전트 — scope 가드(end 점프)를 직접 붙인다(3-6).
        middleware=[
            _model_usage_middleware("worker"),
            ScopeGuardMiddleware(),
            *seller_pii_middlewares(),
            tool_call_limit_middleware(),
            ToolCallObservationMiddleware(),
        ],
    )


# ── product_agent (2-7) — draft 생성까지, 쓰기는 4단계 confirm-resume 코드 경로 ──

# A안(2026-07-18 확정): 조회만 바인딩 — LLM 이 쓰기 도구를 볼 수 없어 HITL
# (발화 ≠ 동의 [HARD])이 프롬프트가 아니라 구조로 보장된다. 실행(4단계)은 LLM
# 도구 호출이 아니라 코드가 담당한다 — hitl._execute_draft 가 승인된 draft 를
# SpringClient 로 직접 매핑한다(#620, 배정표 §3 개정 — 쓰기 도구는 존재하지 않는다).
# calculate 는 2-9 리뷰 반영(2026-07-18 사용자 확정) — 재고 증감 환산 암산 방지.
PRODUCT_DRAFT_TOOLS = [
    seller_tools.list_my_products,
    seller_tools.calculate,
    # [#297] 발송 draft(op=ship)의 대상 orderItemId·현재 상태 확인용(I-29, 조회 전용).
    # 발송 실행도 hitl._execute_draft 가 코드로 담당한다 — 쓰기 도구는 여기 바인딩하지
    # 않는다(HITL 구조 보장 유지).
    seller_tools.get_orders,
]


def build_product_agent() -> CompiledStateGraph:
    """상품관리 draft 생성 에이전트 (smart tier · ToolStrategy(DraftProposal)).

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
        middleware=[
            _model_usage_middleware("product"),
            *seller_pii_middlewares(),
            tool_call_limit_middleware(),
            ToolCallObservationMiddleware(),
        ],
    )


# ── supervisor (4-1a) — 3분기 라우터: analysis / product / general ────────────


def build_supervisor() -> CompiledStateGraph:
    """3분기 라우터 (smart tier · 도구 없음 · ToolStrategy(RouteDecision)).

    출력 계약: RouteDecision(category/reason/confidence). 후처리는 전부 코드
    (orchestrator.route_question) 소관 — confidence 미달 = general 재지정(#180
    저신뢰 폴백 역전), 장애 = general 폴백(REALIGN §4). scope 선차단·confirm
    코드 선판정은 SSE 배선(4-1b) 입구에서 이 라우터보다 먼저 실행된다.
    """
    return create_agent(
        model=init_seller_model("supervisor"),
        tools=[],
        system_prompt=SUPERVISOR_PROMPT,
        response_format=ToolStrategy(RouteDecision),
        context_schema=SellerContext,
        # 구조화 출력 레인 — end 점프 금지, PII 정제만 (3-6 배정표).
        middleware=[_model_usage_middleware("supervisor"), *seller_pii_middlewares()],
    )


# ── analysis_planner (3-2) — 파이프라인 앞단: 워커 선택 + 기간 표현 분류 ────────


def build_analysis_planner() -> CompiledStateGraph:
    """분석 계획 수립자 (smart tier · 도구 없음 · ToolStrategy(AnalysisPlan)).

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
        middleware=[_model_usage_middleware("planner"), *seller_pii_middlewares()],
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
        middleware=[_model_usage_middleware("report")],
    )


def build_report_judge() -> CompiledStateGraph:
    """보고서 채점 judge (smart tier · ToolStrategy(ReportScore)).

    결정론 검사(verifier.run_deterministic_checks) 이후에 호출된다 — 21/30 판정과
    재작성 루프는 3단계 코드 소관이고, judge 는 축별 점수·feedback 만 낸다.
    """
    return create_agent(
        model=init_seller_model("judge"),
        tools=[],
        system_prompt=JUDGE_PROMPT,
        response_format=ToolStrategy(ReportScore),
        context_schema=SellerContext,
        middleware=[_model_usage_middleware("judge")],
    )


def build_analysis_judge() -> CompiledStateGraph:
    """브랜치 분석 검증 judge (smart tier · 도구 없음 · ToolStrategy(AnalysisScore)).

    이슈 #242 분석 검증 층 — report_judge(build_report_judge)와 대칭이지만 채점
    대상이 다르다: 이쪽은 (도구 원출력, finding) 1건을 보고 "적절한 분석인가"를
    채점한다. F1~F3(verifier.run_finding_checks) 결정론 검사와 병행되며, 판정·
    재실행 상한은 orchestrator(4단계)가 Settings 에서 읽어 수행한다.
    """
    return create_agent(
        model=init_seller_model("analysis_judge"),
        tools=[],
        system_prompt=ANALYSIS_JUDGE_PROMPT,
        response_format=ToolStrategy(AnalysisScore),
        context_schema=SellerContext,
        middleware=[_model_usage_middleware("analysis_judge")],
    )


def build_graph_agent() -> CompiledStateGraph:
    """차트 기획 에이전트 (smart tier · 도구 없음 · ToolStrategy(ChartPlanSet)).

    [#504] 출력 계약이 ChartSet(좌표) → ChartPlanSet(축 선언)으로 바뀌었다 — LLM 은
    "어떤 축을 그릴 것인가"만 정하고, 좌표는 charts.build_charts 가 Spring 을 직접
    호출해 조립한다(구 결정 D-4 폐기 — 근거 대조 G1 도 검사 대상이 사라져 함께 삭제).
    도구는 여전히 없다 — 축 판단에는 findings·보고서·질문이면 충분하고, 조회는
    코드 소관이다.
    """
    return create_agent(
        model=init_seller_model("graph"),
        tools=[],
        system_prompt=GRAPH_PROMPT,
        response_format=ToolStrategy(ChartPlanSet),
        context_schema=SellerContext,
        middleware=[_model_usage_middleware("graph")],
    )


def build_chart_interpret_agent() -> CompiledStateGraph:
    """차트 해석 에이전트 (smart tier · 도구 없음 · 자유 텍스트, 이슈 #600).

    `build_report_agent`/`build_resident_report_agent`와 같은 모양(zero-tool·자유
    텍스트)이지만 chart_only 턴 전용이라 완전히 분리한 상수·역할을 쓴다 — 입력은
    좌표 전량 + `charts.chart_facts` 산출(`pipeline.format_chart_input`)뿐이고,
    findings·보고서는 chart_only 턴에는 애초에 없다(run_graph 호출부 참조).
    """
    return create_agent(
        model=init_seller_model("chart_interpret"),
        tools=[],
        system_prompt=CHART_INTERPRET_PROMPT,
        context_schema=SellerContext,
        middleware=[_model_usage_middleware("chart_interpret")],
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
        middleware=[
            _model_usage_middleware("recommend"),
            tool_call_limit_middleware(),
            ToolCallObservationMiddleware(),
        ],  # 읽기 2종 호출 상한 + 실제 호출 관측
    )


# ── 상주(무인) 분석 파이프라인 (이슈 #598) ──────────────────────────────────────
# 채팅 레인(위 report/recommend/워커 5종)과 완전히 분리한다 — 무접촉 보장이 설계
# 결정이다(design-598 §3-5 안 B). 전부 zero-tool: 입력은 ctx 표/finding/보고서
# 문자열뿐이고, 조회는 SOP `load`/`compare` 스텝(코드)이 이미 끝냈다.


def _build_interpret_worker(
    system_prompt: str, response_schema: type[AnalysisFinding]
) -> CompiledStateGraph:
    """워커 4종 상주 interpret 공통 조립 — smart tier · 도구 없음."""
    return create_agent(
        model=init_seller_model("interpret"),
        tools=[],
        system_prompt=system_prompt,
        response_format=ToolStrategy(response_schema),
        context_schema=SellerContext,
        middleware=[_model_usage_middleware("interpret")],
    )


def build_behavior_interpret_agent() -> CompiledStateGraph:
    """고객 행동 상주 interpret (세그먼트별 별칭·설명 — `BehaviorFinding` 출력)."""
    return _build_interpret_worker(BEHAVIOR_INTERPRET_PROMPT, BehaviorFinding)


def build_churn_interpret_agent() -> CompiledStateGraph:
    """고객 이탈 상주 interpret."""
    return _build_interpret_worker(CHURN_INTERPRET_PROMPT, AnalysisFinding)


def build_conversion_interpret_agent() -> CompiledStateGraph:
    """구매전환 상주 interpret."""
    return _build_interpret_worker(CONVERSION_INTERPRET_PROMPT, AnalysisFinding)


def build_sales_anomaly_interpret_agent() -> CompiledStateGraph:
    """매출 이상 상주 interpret."""
    return _build_interpret_worker(SALES_ANOMALY_INTERPRET_PROMPT, AnalysisFinding)


def build_resident_report_agent() -> CompiledStateGraph:
    """상주 보고서 작성 에이전트 (smart tier · 도구 없음 · 자유 텍스트).

    채팅 레인 `build_report_agent`/`REPORT_PROMPT` 와 완전히 분리된 별도 상수를 쓴다
    (설계 결정 3 — 채팅 레인 무접촉 보장). 검증(V1 D1~D3 + V2 C1~C3/V2-d + judge)·
    재작성 루프 배선은 `resident.py` 소관 — 여기는 빌더만.
    """
    return create_agent(
        model=init_seller_model("resident_report"),
        tools=[],
        system_prompt=RESIDENT_REPORT_PROMPT,
        context_schema=SellerContext,
        middleware=[_model_usage_middleware("resident_report")],
    )


def build_resident_recommend_agent() -> CompiledStateGraph:
    """상주 행동 추천 에이전트 (smart tier · 도구 없음 · `ctx.candidate_actions` 입력).

    채팅 레인 `build_recommend_agent`(도구 2개)와 달리 도구가 없다 — 실존·중복 확인은
    후보 생성기(후속 이슈)가 후보를 만드는 시점에 이미 보장한다는 전제다(design-598
    §3-4). 출력 스키마는 채팅 레인과 동일한 `RecommendationSet`을 공유한다.
    """
    return create_agent(
        model=init_seller_model("resident_recommend"),
        tools=[],
        system_prompt=RESIDENT_RECOMMEND_PROMPT,
        response_format=ToolStrategy(RecommendationSet),
        context_schema=SellerContext,
        middleware=[_model_usage_middleware("resident_recommend")],
    )
