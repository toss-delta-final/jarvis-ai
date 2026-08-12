"""supervisor 라우팅(orchestrator.route_question) 검증 (4-1a).

실 LLM 없음 — build_supervisor 를 스텁으로 교체한다. 검증 항목(REALIGN §4 → #180 개정):
  - 정상 분류는 그대로 통과
  - confidence 미달 → general 재지정 (#180 저신뢰 폴백 역전 — 원분류 general 은 그대로)
  - supervisor 장애(예외·타임아웃·비정형 출력) → general 폴백

confirm 선판정은 2026-07-22(FE 계약 A-2)에 message 파싱에서 요청 스키마 필드로 이관됐다 —
검증은 tests/unit/test_seller_chat_request.py(SellerChatRequest) 로 이동했다.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.agents.seller import orchestrator
from app.agents.seller.context import SellerContext
from app.agents.seller.schemas import RouteDecision
from app.core.llm import LLMNotConfigured

_CTX = SellerContext(seller_id="7", brand_id="3")


def _settings(confidence_min: float = 0.6, timeout_s: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(
        seller_route_confidence_min=confidence_min,
        seller_route_timeout_s=timeout_s,
    )


class _StubSupervisor:
    """create_agent 대역 — ainvoke 만 흉내(정상/예외/지연/비정형)."""

    def __init__(
        self,
        decision: RouteDecision | object | None = None,
        exc: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._decision = decision
        self._exc = exc
        self._delay_s = delay_s

    async def ainvoke(self, _input: dict, context: object = None) -> dict:
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._exc is not None:
            raise self._exc
        return {"structured_response": self._decision}


def _patch(
    monkeypatch: pytest.MonkeyPatch, stub: _StubSupervisor, **settings_kwargs: float
) -> None:
    monkeypatch.setattr(orchestrator, "build_supervisor", lambda: stub)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: _settings(**settings_kwargs))


def _route(question: str = "지난달 매출 어때?") -> RouteDecision:
    return asyncio.run(orchestrator.route_question(question, _CTX))


def test_confident_decision_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """confidence 충분한 정상 분류는 그대로 반환된다."""
    decision = RouteDecision(category="product", reason="가격 수정 요청", confidence=0.95)
    _patch(monkeypatch, _StubSupervisor(decision=decision))

    result = _route("가격 12,900원으로 바꿔줘")

    assert result is decision  # 재작성 없이 원본 통과


def test_low_confidence_analysis_reroutes_to_general(monkeypatch: pytest.MonkeyPatch) -> None:
    """confidence 미달(원분류 analysis) → general 재지정(#180 저신뢰 폴백 역전).

    단순 조회가 analysis 로 가면 5단 파이프라인이라 회복 불가·최고 비용 —
    불확실하면 가벼운 레인에서 답하고 general 의 분석 안내로 회복한다.
    """
    decision = RouteDecision(category="analysis", reason="애매", confidence=0.4)
    _patch(monkeypatch, _StubSupervisor(decision=decision), confidence_min=0.6)

    result = _route("최근 7일 매출")

    assert result.category == "general"
    assert orchestrator.ROUTE_LOW_CONFIDENCE_REASON in result.reason
    assert "analysis" in result.reason  # 원분류 보존(디버깅 재료)
    assert result.confidence == 0.4  # 원 confidence 보존(디버깅 재료)


def test_low_confidence_product_reroutes_to_general(monkeypatch: pytest.MonkeyPatch) -> None:
    """confidence 미달(원분류 product) → general 재지정 — 변경 레인도 예외가 아니다.

    불확실한 변경 발화가 product 로 가면 엉뚱한 draft 가 생성될 수 있다 —
    general 이 조회로 답하거나 상품관리 기능을 안내해 회복한다.
    """
    decision = RouteDecision(category="product", reason="변경 추정", confidence=0.3)
    _patch(monkeypatch, _StubSupervisor(decision=decision), confidence_min=0.6)

    result = _route("그거 바꿔줘")

    assert result.category == "general"
    assert orchestrator.ROUTE_LOW_CONFIDENCE_REASON in result.reason


def test_low_confidence_general_stays_general(monkeypatch: pytest.MonkeyPatch) -> None:
    """원분류가 general 이면 confidence 미달이어도 재지정 없이 그대로 간다."""
    decision = RouteDecision(category="general", reason="단편 발화", confidence=0.3)
    _patch(monkeypatch, _StubSupervisor(decision=decision))

    result = _route("최근 7일")

    assert result is decision


def test_supervisor_exception_falls_back_to_general(monkeypatch: pytest.MonkeyPatch) -> None:
    """supervisor 예외 → general 폴백(2026-07-19 사용자 결정 — 작동 우선)."""
    _patch(monkeypatch, _StubSupervisor(exc=RuntimeError("api down")))

    result = _route()

    assert result.category == "general"
    assert result.reason == orchestrator.ROUTE_FALLBACK_REASON


def test_model_configuration_error_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """provider 키 누락은 general도 실행 불가하므로 라우팅 폴백으로 삼키지 않는다."""
    _patch(monkeypatch, _StubSupervisor(exc=LLMNotConfigured("openai key missing")))

    with pytest.raises(LLMNotConfigured):
        _route()


def test_supervisor_timeout_falls_back_to_general(monkeypatch: pytest.MonkeyPatch) -> None:
    """seller_route_timeout_s 초과 → general 폴백."""
    _patch(monkeypatch, _StubSupervisor(delay_s=0.2), timeout_s=0.05)

    result = _route()

    assert result.category == "general"


def test_malformed_structured_response_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """비정형 출력(RouteDecision 아님) → general 폴백(TypeError 경로)."""
    _patch(monkeypatch, _StubSupervisor(decision={"category": "analysis"}))

    result = _route()

    assert result.category == "general"


# confirm 선판정 테스트는 test_seller_chat_request.py(SellerChatRequest 스키마)로 이관됐다.


# ── 대화 스레드 맥락 주입 — 프롬프트 불변, 입력 메시지 조립만 ─────────────────────


class _CapturingSupervisor(_StubSupervisor):
    """입력 메시지를 기록하는 스텁 — 맥락 주입 형식 검증용."""

    def __init__(self, decision: RouteDecision) -> None:
        super().__init__(decision=decision)
        self.received: list[str] = []

    async def ainvoke(self, agent_input: dict, context: object = None) -> dict:
        self.received.append(agent_input["messages"][0].content)
        return await super().ainvoke(agent_input, context=context)


def test_route_without_turns_sends_raw_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """맥락이 없으면 질문 원문 그대로 — 기존 supervisor 입력 계약 불변."""
    decision = RouteDecision(category="general", reason="r", confidence=0.9)
    stub = _CapturingSupervisor(decision)
    _patch(monkeypatch, stub)

    asyncio.run(orchestrator.route_question("어제 매출 알려줘", _CTX))

    assert stub.received == ["어제 매출 알려줘"]


def test_route_injects_recent_turns_into_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """최근 대화가 있으면 [최근 대화] 블록 + [이번 질문] 라벨로 입력을 조립한다."""
    decision = RouteDecision(category="analysis", reason="r", confidence=0.9)
    stub = _CapturingSupervisor(decision)
    _patch(monkeypatch, stub)
    turns = [("user", "어제 매출 알려줘"), ("assistant", "어제 매출은 120만원입니다.")]

    asyncio.run(orchestrator.route_question("그럼 지난주는?", _CTX, recent_turns=turns))

    sent = stub.received[0]
    assert sent.startswith("[최근 대화]")
    assert "사용자: 어제 매출 알려줘" in sent
    assert sent.endswith("[이번 질문] 그럼 지난주는?")


# ─────────── S-4 화면 맥락 주입 (이슈 #118) ───────────


def _screen_ctx(**payload):
    """정본 관대 정규화를 실제로 태운 ScreenContext."""
    from app.schemas.seller import SellerChatRequest

    return SellerChatRequest.model_validate(
        {"sessionId": "s", "threadId": "t", "message": "m", "screen": payload}
    ).screen


def test_route_without_screen_sends_the_same_input_as_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[회귀 0] `screen=None` 이면 supervisor 입력이 오늘과 **바이트 동일**하다.

    판매자 FE 도 아직 `screen` 을 보내지 않으므로 이게 절대다수 경로다.
    """
    decision = RouteDecision(category="general", reason="r", confidence=0.9)
    turns = [("user", "어제 매출 알려줘"), ("assistant", "120만원입니다.")]

    stub_without = _CapturingSupervisor(decision)
    _patch(monkeypatch, stub_without)
    asyncio.run(orchestrator.route_question("그럼 지난주는?", _CTX, recent_turns=turns))

    stub_explicit_none = _CapturingSupervisor(decision)
    _patch(monkeypatch, stub_explicit_none)
    asyncio.run(
        orchestrator.route_question("그럼 지난주는?", _CTX, recent_turns=turns, screen=None)
    )

    assert stub_without.received == stub_explicit_none.received
    assert "[현재 화면]" not in stub_without.received[0]


def test_route_injects_screen_context_into_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """정본 §3.2 — `pageType`·`filters` 가 실려 "이 목록 왜 비어?" 류에 답할 수 있다."""
    decision = RouteDecision(category="general", reason="r", confidence=0.9)
    stub = _CapturingSupervisor(decision)
    _patch(monkeypatch, stub)

    asyncio.run(
        orchestrator.route_question(
            "이 목록 왜 비어?",
            _CTX,
            screen=_screen_ctx(pageType="seller_orders", filters={"status": "신규주문"}),
        )
    )

    sent = stub.received[0]
    assert sent.startswith("[현재 화면]")
    assert "주문 관리" in sent and "status=신규주문" in sent
    assert sent.endswith("[이번 질문] 이 목록 왜 비어?")


def test_route_screen_injection_does_not_touch_the_supervisor_prompt() -> None:
    """프롬프트 파일은 한 글자도 바꾸지 않는다 — 주입은 **입력 메시지에만**(§9.1 이력 선례)."""
    from app.agents.seller import prompts

    assert "현재 화면" not in prompts.SUPERVISOR_PROMPT
    assert "screen" not in prompts.SUPERVISOR_PROMPT.lower()


# ── [#591] analysis 재정의 — 경계 예시 판정 계약 ───────────────────────────────
#
# 실 LLM 없이 검증할 수 있는 것은 "프롬프트가 무엇을 지시하는가"다. 판정이 뒤집힌 8개는
# 프롬프트의 [경계 예시] 줄이 유일한 근거이므로, 그 줄이 general 을 가리키는지를 고정한다.
# 여기가 초록인데 실제 라우팅이 틀리면 프롬프트가 아니라 모델 문제로 좁혀진다.

_FLIPPED_TO_GENERAL = [
    "최근 7일 매출 왜 떨어졌어?",
    "주문 패턴이 이상해",
    "전환율이 낮은 것 같은데 문제야?",
    "지난달 매출 어때?",
    "요즘 장사 잘 되고 있어?",
    "이번 주 리뷰 요약해줘",
    "평점 낮은 리뷰 뭐가 문제야?",
    "매출 올리려면?",
]

_STILL_ANALYSIS = [
    "분석 보고서 보여줘",
    "이번 주 리포트 있어?",
    "어제 분석 결과 뭐였어?",
]


def _boundary_examples() -> list[str]:
    """[경계 예시] 절만 잘라낸다 — 같은 발화가 위 카테고리 설명에도 나오므로 구간을 좁힌다."""
    from app.agents.seller import prompts

    block = prompts.SUPERVISOR_PROMPT.split("[경계 예시", 1)
    assert len(block) == 2, "[경계 예시] 절이 사라졌다"
    return block[1].split("\n[", 1)[0].splitlines()


def _example_line(utterance: str) -> str:
    lines = [ln for ln in _boundary_examples() if f'"{utterance}"' in ln]
    assert lines, f"경계 예시에서 사라졌다: {utterance}"
    assert len(lines) == 1, f"경계 예시에 중복 판정이 있다: {utterance}"
    return lines[0]


@pytest.mark.parametrize("utterance", _FLIPPED_TO_GENERAL)
def test_interpretation_questions_are_routed_to_general(utterance: str) -> None:
    """원인·평가·요약 질문은 general 이다 — 그 자리에서 해석해 줄 주체가 없어졌다."""
    assert "→ general" in _example_line(utterance)


@pytest.mark.parametrize("utterance", _STILL_ANALYSIS)
def test_saved_report_requests_are_routed_to_analysis(utterance: str) -> None:
    """analysis 는 저장된 산출물(보고서·리포트·지난 분석 결과)을 찾는 발화만 남는다."""
    assert "→ analysis" in _example_line(utterance)


def test_supervisor_prompt_drops_the_interpretation_catch_all() -> None:
    """혼합 규칙의 "해석 의도가 하나라도 있으면 analysis" 는 삭제됐다.

    이 문장이 남아 있으면 위 8개를 general 로 적어둬도 혼합 발화가 전부 analysis 로
    빨려 들어가 경계 예시가 무력화된다.
    """
    from app.agents.seller import prompts

    assert "해석 의도가 하나라도 있으면" not in prompts.SUPERVISOR_PROMPT
    assert "저장된 보고서를 찾는 요청이 섞여 있으면" in prompts.SUPERVISOR_PROMPT


def test_supervisor_prompt_keeps_the_route_decision_vocabulary() -> None:
    """[S-4 무개정] 카테고리 값 3종은 그대로다 — 바뀐 것은 analysis 의 의미뿐이다."""
    from app.agents.seller import prompts

    for category in ("analysis", "product", "general"):
        assert category in prompts.SUPERVISOR_PROMPT


def test_general_prompt_points_to_the_report_page_not_an_on_demand_analysis() -> None:
    """[#591] 판매자가 분석을 요청하는 경로가 사라졌다 — 구 안내는 거짓말이 된다."""
    from app.agents.seller import prompts

    prompt = prompts.GENERAL_PROMPT_TEMPLATE.format(today="2026-08-11")
    assert "분석을 요청해 주세요" not in prompt
    assert "보고서 페이지" in prompt


def test_general_prompt_covers_the_widened_tool_scope() -> None:
    """3번 "지원 범위" 문구가 12종 바인딩과 같은 것을 약속한다(한쪽만 늘면 거짓말이 된다)."""
    from app.agents.seller import prompts

    prompt = prompts.GENERAL_PROMPT_TEMPLATE.format(today="2026-08-11")
    for scope in ("퍼널", "행동 이벤트", "변경 이력", "이탈 코호트", "계정 이벤트"):
        assert scope in prompt
    assert "get_latest_report" in prompt


def test_general_prompt_chart_guidance_is_untouched() -> None:
    """[#531] 차트 안내는 무수정 — 게이트 ②.5 가 실제로 그 발화를 잡으므로 참인 안내다."""
    from app.agents.seller import prompts

    prompt = prompts.GENERAL_PROMPT_TEMPLATE.format(today="2026-08-11")
    assert "차트나 그래프를 그리지 않는다" in prompt
    assert "매출 그래프 보여줘" in prompt
