"""판매자 분석 워커 팩토리 테스트 (SPEC-SELLER-001 §2·§4 — 2-4b·2-5 묶음 1).

실 LLM 호출 없음 — 도구 배정·프롬프트 필수 요소·에이전트 조립 가능 여부만 검증한다.
워커가 늘 때마다 WORKERS 목록에 한 줄 추가하면 공통 검증이 전부 적용된다.
"""

from __future__ import annotations

import pytest

from app.agents.seller import models as seller_models
from app.agents.seller.prompts import (
    ABUSE_PROMPT,
    ANALYSIS_JUDGE_PROMPT,
    BEHAVIOR_PROMPT,
    CHART_INTERPRET_PROMPT,
    CHURN_PROMPT,
    CONVERSION_PROMPT,
    GENERAL_PROMPT_TEMPLATE,
    GRAPH_PROMPT,
    JUDGE_PROMPT,
    PLANNER_PROMPT,
    PRODUCT_PROMPT,
    RECOMMEND_PROMPT,
    REPORT_PROMPT,
    REVIEW_PROMPT,
    SALES_ANOMALY_PROMPT,
    WORKER_COMMON_RULES,
)
from app.agents.seller.workers import (
    ABUSE_TOOLS,
    BEHAVIOR_TOOLS,
    CHURN_TOOLS,
    CONVERSION_TOOLS,
    GENERAL_TOOLS,
    PRODUCT_DRAFT_TOOLS,
    RECOMMEND_TOOLS,
    REVIEW_TOOLS,
    SALES_ANOMALY_TOOLS,
    build_abuse_agent,
    build_analysis_judge,
    build_analysis_planner,
    build_behavior_agent,
    build_chart_interpret_agent,
    build_churn_agent,
    build_conversion_agent,
    build_general_agent,
    build_graph_agent,
    build_product_agent,
    build_recommend_agent,
    build_report_agent,
    build_report_judge,
    build_review_agent,
    build_sales_anomaly_agent,
)
from app.core.config import Settings


@pytest.fixture(autouse=True)
def _configured_seller_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """에이전트 조립 테스트에 네트워크 호출 없는 dummy OpenAI 설정을 주입한다."""
    seller_models._cached_model.cache_clear()
    settings = Settings(_env_file=None, openai_api_key="test-key")
    monkeypatch.setattr(seller_models, "get_settings", lambda: settings)


# [#620] 상품/주문 쓰기 도구(create_product/update_product/delete_product/
# update_order_status)는 실제 @tool 로 존재하지 않는다(어느 에이전트에도 바인딩된 적
# 없는 죽은 코드로 확인돼 tools.py 에서 제거됐다 — 실행은 hitl._execute_draft 가 코드로
# 담당한다). 그래도 "이 이름들은 분석·general·product 워커 어디에도 배정되지 않는다"는
# 회귀 방지 취지는 유효해 이름만 하드코딩으로 남긴다.
_KNOWN_WRITE_TOOL_NAMES = frozenset(
    {"create_product", "update_product", "delete_product", "update_order_status"}
)

# (analysis_type, 도구 목록, 프롬프트, 빌더, 배정표 기대 도구명) — 워커 추가 시 여기만 확장.
WORKERS = [
    (
        "sales_anomaly",
        SALES_ANOMALY_TOOLS,
        SALES_ANOMALY_PROMPT,
        build_sales_anomaly_agent,
        {
            "get_sales_timeseries",
            "get_order_events",
            "get_product_change_logs",
            "search_analysis_guide",
        },
    ),
    (
        "conversion",
        CONVERSION_TOOLS,
        CONVERSION_PROMPT,
        build_conversion_agent,
        {"get_funnel", "search_analysis_guide"},
    ),
    (
        "behavior",
        BEHAVIOR_TOOLS,
        BEHAVIOR_PROMPT,
        build_behavior_agent,
        {"get_behavior_events", "get_funnel", "search_analysis_guide"},
    ),
    (
        "churn",
        CHURN_TOOLS,
        CHURN_PROMPT,
        build_churn_agent,
        # [#481] I-8 자사 코호트 전환(2026-08-06)으로 churn 은 get_account_events 를
        # 더 쓰지 않는다 — WITHDRAW 는 member 에 탈퇴 필드가 없어 원래부터 상시 0건.
        {
            "get_churn_cohort",
            "get_order_events",
            "get_product_change_logs",
            "search_analysis_guide",
        },
    ),
    (
        "abuse",
        ABUSE_TOOLS,
        ABUSE_PROMPT,
        build_abuse_agent,
        {"get_behavior_events", "get_order_events", "get_account_events", "search_analysis_guide"},
    ),
    (
        "review",
        REVIEW_TOOLS,
        REVIEW_PROMPT,
        build_review_agent,
        {"get_reviews", "search_analysis_guide"},
    ),
]

_IDS = [w[0] for w in WORKERS]


@pytest.mark.parametrize(
    ("analysis_type", "tools", "prompt", "builder", "expected"), WORKERS, ids=_IDS
)
def test_tool_assignment_matches_table(analysis_type, tools, prompt, builder, expected) -> None:
    """배정표(HANDOFF §3)와 정확히 일치 — 초과 배정도 누락도 없다."""
    assert {t.name for t in tools} == expected


@pytest.mark.parametrize(
    ("analysis_type", "tools", "prompt", "builder", "expected"), WORKERS, ids=_IDS
)
def test_excludes_write_tools(analysis_type, tools, prompt, builder, expected) -> None:
    """쓰기 도구 3종(create/update/delete)은 분석 워커에 절대 배정되지 않는다(§4)."""
    write_names = _KNOWN_WRITE_TOOL_NAMES
    assert {t.name for t in tools}.isdisjoint(write_names)


@pytest.mark.parametrize(
    ("analysis_type", "tools", "prompt", "builder", "expected"), WORKERS, ids=_IDS
)
def test_tools_hide_identity(analysis_type, tools, prompt, builder, expected) -> None:
    """신원 인자(runtime·brand_id·seller_id)는 LLM 노출 스키마에 없다(IDOR)."""
    for t in tools:
        for hidden in ("runtime", "brand_id", "seller_id"):
            assert hidden not in t.args


@pytest.mark.parametrize(
    ("analysis_type", "tools", "prompt", "builder", "expected"), WORKERS, ids=_IDS
)
def test_prompt_required_elements(analysis_type, tools, prompt, builder, expected) -> None:
    """확정 프롬프트 필수 요소 — analysis_type 고정·기준서 먼저·공통 규칙 결합."""
    assert analysis_type in prompt  # 워커별 analysis_type 고정 지시
    assert "search_analysis_guide" in prompt  # 기준서 검색 먼저(장치 ③)
    assert prompt.endswith(WORKER_COMMON_RULES)  # 공통 규칙이 말미에 결합됨


def test_secondary_source_rule_in_abuse() -> None:
    """I-8 보조 소스 규약 — 보조 실패는 degrade 사유가 아님을 명시한다.

    [#481] I-8 자사 코호트 전환으로 소비 워커는 abuse 하나다 — churn 프롬프트에는
    get_account_events 절차가 없어야 한다(배정표와 프롬프트의 정합).
    """
    assert "get_account_events 는 보조 소스" in ABUSE_PROMPT
    assert "주 소스 결과만으로 계속" in ABUSE_PROMPT
    assert "get_account_events" not in CHURN_PROMPT


def test_common_rules_content() -> None:
    """공통 규칙 3요소 — 코드 판정 번복 금지(§5)·degrade(§4)·기간은 planner(장치 ④)."""
    assert "번복하지 않는다" in WORKER_COMMON_RULES
    assert "데이터 확보 실패" in WORKER_COMMON_RULES
    assert "날짜를 직접 계산하지 않는다" in WORKER_COMMON_RULES


@pytest.mark.parametrize(
    ("analysis_type", "tools", "prompt", "builder", "expected"), WORKERS, ids=_IDS
)
def test_builder_compiles(analysis_type, tools, prompt, builder, expected) -> None:
    """create_agent 조립이 성공하고 실행 인터페이스(ainvoke)를 갖는다 — LLM 호출 없음."""
    agent = builder()
    assert hasattr(agent, "ainvoke")


# ── general_agent (2-6) — 분석 워커와 별개 검증 ────────────────────────────────


def test_general_tool_assignment() -> None:
    """[#591] search 레인 배정표 — 조회 11종 + get_latest_report, 쓰기 0.

    스냅샷으로 고정하는 이유: 이 목록이 곧 GENERAL_PROMPT 3번 "지원 범위" 문구의 근거다.
    한쪽만 늘면 프롬프트가 없는 도구를 약속하거나 있는 도구를 감춘다.
    """
    assert {t.name for t in GENERAL_TOOLS} == {
        "get_sales_timeseries",
        "get_funnel",  # [#591] I-7
        "get_behavior_events",  # [#591] I-13
        "get_order_events",
        "get_orders",  # [#297] I-29 현재 상태 스냅샷
        "get_product_change_logs",  # [#591] I-15
        "get_churn_cohort",  # [#591] I-16
        "get_account_events",  # [#591] I-8
        "get_reviews",  # [#297] I-31 리뷰 단순 조회
        "list_my_products",
        "calculate",
        "get_latest_report",  # [#591] 보고서 조회는 이 하나뿐(결정 10)
    }
    write_names = _KNOWN_WRITE_TOOL_NAMES
    assert {t.name for t in GENERAL_TOOLS}.isdisjoint(write_names)
    # [#297] 주문 쓰기(발송)도 general 에 절대 없다.
    assert "update_order_status" not in {t.name for t in GENERAL_TOOLS}
    for t in GENERAL_TOOLS:
        for hidden in ("runtime", "brand_id", "seller_id"):
            assert hidden not in t.args


def test_general_lane_does_not_bind_permanent_stub() -> None:
    """[#591] 영구 스텁(search_analysis_guide)은 search 레인에 없다.

    항상 "Error:" 를 돌려주는 도구가 바인딩돼 있으면 LLM 이 용어 질문에 그걸 호출하고
    실패를 판매자에게 그대로 안내한다 — 도구가 없느니만 못한 상태였다. 분석 워커 6종의
    바인딩은 상주 파이프라인 소관이라 건드리지 않았으므로, 여기서 **부재만** 고정한다.
    """
    assert "search_analysis_guide" not in {t.name for t in GENERAL_TOOLS}


def test_general_prompt_principles() -> None:
    """확정 원칙 — 경량 해석만 허용(#650)·calculate 강제·미지원 안내 + today 주입 슬롯."""
    prompt = GENERAL_PROMPT_TEMPLATE.format(today="2026-07-18")
    assert "2026-07-18" in prompt  # today 주입(대화 맥락 — 기간 환산용이 아니다)
    assert "경량 해석만 허용" in prompt
    assert "원인 가설" in prompt  # 원인 규명·복수 지표 교차는 여전히 금지
    assert "calculate" in prompt
    assert "암산·추정 금지" in prompt
    assert "미지원 안내" in prompt


def test_general_prompt_delegates_period_to_code() -> None:
    """[#346] 기간 문구 소유권 — general 프롬프트는 어휘도 환산 규칙도 알지 않는다.

    이 레인의 기간 정의가 프롬프트에 적혀 있었기 때문에 period.py 와 갈라졌고
    (`이번 달` = 당월 1일~오늘 vs ~어제), 상한·0/음수 가드도 통째로 비켜갔다.
    어휘를 하나라도 다시 프롬프트에 적으면 그 분기가 되살아나므로 **부재**를 고정한다.
    """
    prompt = GENERAL_PROMPT_TEMPLATE.format(today="2026-07-18")
    assert "입력에 주어진 from/to 를 그대로" in prompt
    assert "날짜를 직접 계산·추론하지" in prompt
    # 어휘·환산 정의가 프롬프트로 돌아오지 않았는가 — 정의의 유일한 소유자는 period.py 다.
    for vocab in ("지난달", "이번 달", "최근 N일", "전월 1일", "당월 1일"):
        assert vocab not in prompt, f"기간 어휘 '{vocab}' 가 프롬프트로 돌아왔다(#346)"


def test_build_general_agent_compiles() -> None:
    """today 를 주입한 조립이 성공하고 실행 인터페이스(ainvoke)를 갖는다."""
    agent = build_general_agent(today="2026-07-18")
    assert hasattr(agent, "ainvoke")


# ── product_agent (2-7) — A안: 조회만 바인딩, 쓰기는 구조적으로 차단 ───────────


def test_product_agent_binds_read_only() -> None:
    """A안 + calculate(2-9) + get_orders(#297 ship 대상 해소) — 조회·계산만, 쓰기는 볼 수 없다."""
    assert {t.name for t in PRODUCT_DRAFT_TOOLS} == {
        "list_my_products",
        "calculate",
        "get_orders",  # [#297] ship draft 의 orderItemId·현재 상태 확인(조회 전용)
    }
    write_names = _KNOWN_WRITE_TOOL_NAMES
    assert {t.name for t in PRODUCT_DRAFT_TOOLS}.isdisjoint(write_names)
    # [#297] 주문 쓰기(발송, update_order_status)도 draft 에이전트가 볼 수 없다 —
    # HITL(발화 ≠ 동의)이 프롬프트가 아니라 구조로 보장된다.
    assert "update_order_status" not in {t.name for t in PRODUCT_DRAFT_TOOLS}
    for t in PRODUCT_DRAFT_TOOLS:
        for hidden in ("runtime", "brand_id", "seller_id"):
            assert hidden not in t.args


def test_product_prompt_principles() -> None:
    """확정 원칙 — before 조회 강제·모호 시 되묻기·추천 적용 발화 격리·삭제/숨김 구분."""
    assert "list_my_products" in PRODUCT_PROMPT  # before 는 조회값에서만
    assert "추측·기억으로 채우지 않는다" in PRODUCT_PROMPT
    assert "clarification" in PRODUCT_PROMPT  # 모호 시 되묻기(임의 선택 금지)
    assert "N번 적용해줘" in PRODUCT_PROMPT  # §6.3 — 이력 조회 경로로 격리
    assert "after 는 DELETED" in PRODUCT_PROMPT  # delete = DELETED 전이(api-spec §4.5)
    # 삭제를 숨김이라 부르면 판매자가 되돌릴 수 있는 조작으로 오인한 채 승인한다.
    assert '삭제를 "숨김"이라고 표현하지 않는다' in PRODUCT_PROMPT
    assert "쓰기 도구는 없다" in PRODUCT_PROMPT


def test_build_product_agent_compiles() -> None:
    """create_agent 조립이 성공하고 실행 인터페이스(ainvoke)를 갖는다."""
    agent = build_product_agent()
    assert hasattr(agent, "ainvoke")


# ── analysis_planner (3-2) — 워커 선택 + 기간 분류 ─────────────────────────────


def test_planner_prompt_covers_all_workers() -> None:
    """워커 6종 전부가 선택 기준으로 설명된다 — 누락 시 해당 분석이 계획에서 실종."""
    for analysis_type in ("sales_anomaly", "conversion", "behavior", "churn", "abuse", "review"):
        assert analysis_type in PLANNER_PROMPT


def test_planner_prompt_period_vocabulary() -> None:
    """[#345] 기간 규약 — 원문 옮겨적기 + 날짜 산수 금지(장치 ④) + 표기 정규화 2종.

    3-1 확정 당시에는 "정규 어휘 4종 외에는 clarification" 이었으나, P1 어휘 확장으로
    **판정 자체가 코드로 넘어갔다**. planner 는 이제 어휘 목록을 알지 않는다.
    """
    assert "그대로 옮겨적는다" in PLANNER_PROMPT
    assert "날짜를 계산·추론·환산하지 않는다" in PLANNER_PROMPT
    assert "YYYY-MM-DD~YYYY-MM-DD" in PLANNER_PROMPT  # 표기 정규화 ①
    assert "M월 D일~M월 D일" in PLANNER_PROMPT  # 표기 정규화 ② — 연도 없는 날짜
    assert "최근" in PLANNER_PROMPT  # 기간 미언급 기본값
    assert "clarification" in PLANNER_PROMPT


def test_planner_prompt_forbids_period_clarification() -> None:
    """[#345] 기간 문구 소유권 — planner 는 기간을 이유로 되묻지 않는다(DESIGN §4.2).

    이 문장이 사라지면 planner 가 다시 자기 clarification 을 써서, "예외 메시지가 곧
    사용자 문구"라는 P0 보장이 조건부로 되돌아간다 — 되묻기 문구가 코드와 LLM 두 곳에서
    나오던 #345 §3 의 상태다. 스키마 description(AnalysisPlan)과 한 쌍으로 지켜야 한다.
    """
    assert "기간을 이유로 clarification 을 쓰지 않는다" in PLANNER_PROMPT
    assert "되묻기가 필요한지는 코드가 판단하고" in PLANNER_PROMPT

    from app.agents.seller.schemas import AnalysisPlan

    fields = AnalysisPlan.model_fields
    assert "기간을 이유로 clarification 금지" in (fields["period_expr"].description or "")
    assert "기간을 이유로는 쓰지 않는다" in (fields["clarification"].description or "")


def test_planner_prompt_clarification_contract() -> None:
    """clarification 시 analyses 를 비운다 — resolve_plan 불성립 신호와 접속."""
    assert "analyses 를 반드시 비운다" in PLANNER_PROMPT


def test_build_analysis_planner_compiles() -> None:
    """도구 없는 create_agent 조립이 성공하고 실행 인터페이스를 갖는다 — LLM 호출 없음."""
    agent = build_analysis_planner()
    assert hasattr(agent, "ainvoke")


# ── 분석 파이프라인 후단 (2-8) — report · judge · recommend ────────────────────


def test_recommend_tool_assignment() -> None:
    """배정표(§3) — 읽기 2종만, 쓰기 0, 신원 은닉."""
    assert {t.name for t in RECOMMEND_TOOLS} == {
        "list_my_products",
        "get_product_change_logs",
    }
    write_names = _KNOWN_WRITE_TOOL_NAMES
    assert {t.name for t in RECOMMEND_TOOLS}.isdisjoint(write_names)
    for t in RECOMMEND_TOOLS:
        for hidden in ("runtime", "brand_id", "seller_id"):
            assert hidden not in t.args


def test_report_prompt_principles() -> None:
    """report — 수치는 finding 만·번복 금지·전 finding 반영·한계 정직(D2·D3 와 짝)."""
    assert "새 수치를 만들거나" in REPORT_PROMPT
    assert "번복하지 않는다" in REPORT_PROMPT
    assert "빠짐없이 반영" in REPORT_PROMPT
    assert "데이터 한계" in REPORT_PROMPT


def test_judge_prompt_principles() -> None:
    """judge — 3축 채점·관대한 채점 금지·미달 축 중심 feedback."""
    for axis in ("accuracy", "completeness", "clarity"):
        assert axis in JUDGE_PROMPT
    assert "관대한 채점 금지" in JUDGE_PROMPT
    assert "feedback" in JUDGE_PROMPT


def test_analysis_judge_prompt_principles() -> None:
    """analysis_judge(이슈 #242) — 3축(grounding/sufficiency/relevance)·관대한 채점
    금지·degrade finding 비감점 규칙·report_judge 와 다른 층임을 명시."""
    for axis in ("grounding", "sufficiency", "relevance"):
        assert axis in ANALYSIS_JUDGE_PROMPT
    assert "관대한 채점 금지" in ANALYSIS_JUDGE_PROMPT
    assert "feedback" in ANALYSIS_JUDGE_PROMPT
    assert "degrade finding" in ANALYSIS_JUDGE_PROMPT
    assert "report_judge" in ANALYSIS_JUDGE_PROMPT  # 글쓰기 검증과의 층 구분 명시


def test_recommend_prompt_principles() -> None:
    """recommend — product_id 실존 확인·중복 추천 회피·실행 금지·순서=우선순위."""
    assert "list_my_products" in RECOMMEND_PROMPT  # 실존 확인 강제
    assert "존재하지 않는 상품 금지" in RECOMMEND_PROMPT
    assert "중복 추천" in RECOMMEND_PROMPT
    assert "실행하지 않는다" in RECOMMEND_PROMPT
    assert "N번 적용해줘" in RECOMMEND_PROMPT  # §6.3 순서 계약


def test_pipeline_builders_compile() -> None:
    """report·judge·recommend·analysis_judge·graph 조립이 성공하고 실행 인터페이스를 갖는다.

    LLM 호출 없음 — analysis_judge·graph(이슈 #242) 모두 도구 없는 구조화 출력
    에이전트라 report_judge 와 동일한 조립 계약을 공유한다.
    """
    for builder in (
        build_report_agent,
        build_report_judge,
        build_recommend_agent,
        build_analysis_judge,
        build_graph_agent,
        build_chart_interpret_agent,
    ):
        assert hasattr(builder(), "ainvoke")


def test_graph_prompt_principles() -> None:
    """graph([#504] 축 선언 계약) — 좌표 생성 금지·14조합 명시·other 강등·빈 목록 허용."""
    assert "좌표·수치는 만들지 않는다" in GRAPH_PROMPT  # 좌표는 코드 소관
    assert "14개" in GRAPH_PROMPT  # 지원 축 조합 명시(레지스트리와 짝)
    assert "date" in GRAPH_PROMPT and "product" in GRAPH_PROMPT
    assert "rating" in GRAPH_PROMPT and "behavior_type" in GRAPH_PROMPT
    assert '"other"' in GRAPH_PROMPT  # 지원 밖 요청은 임의 대체 없이 other 선언
    assert "charts=[]" in GRAPH_PROMPT  # 억지 차트 금지


def test_chart_interpret_prompt_principles() -> None:
    """chart_interpret(이슈 #600) — L0 인과 금지·발화 금지 6종·[코드 계산] 인용·6문장 이내."""
    assert "[코드 계산]" in CHART_INTERPRET_PROMPT
    assert "인과 어휘 금지" in CHART_INTERPRET_PROMPT
    assert "완곡하게 낮춘 인과 표현" in CHART_INTERPRET_PROMPT  # L2 도 금지(L0 — 06 서술과 구분)
    assert "전체 행동" in CHART_INTERPRET_PROMPT  # C4-d 형제 규칙(행동 4종)
    assert "6문장 이내" in CHART_INTERPRET_PROMPT
    assert "제목" not in CHART_INTERPRET_PROMPT or "제목은 쓰지 않는다" in CHART_INTERPRET_PROMPT


def test_planner_prompt_comparison_vocabulary() -> None:
    """[#346] 비교 기간도 planner 는 표현만 옮겨적는다 — 환산·되묻기는 코드 소관."""
    assert "comparison_expr" in PLANNER_PROMPT
    assert "직전 동일 기간" in PLANNER_PROMPT
    assert "period_expr 에 섞어 적지 않는다" in PLANNER_PROMPT


def test_worker_common_rules_cover_comparison_period() -> None:
    """워커는 [비교 기간] 을 받으면 두 기간을 각각 조회한다 — 차이를 직접 산술하지 않는다."""
    assert "[비교 기간]" in WORKER_COMMON_RULES
    assert "각각 호출" in WORKER_COMMON_RULES
