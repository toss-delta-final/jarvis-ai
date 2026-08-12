"""판매자 그래프 모델 팩토리 (SPEC-SELLER-001 §8 — provider-neutral 2-tier).

역할→티어 매핑을 코드로 고정한다: 판매자 역할 7종을 **전부 smart** 로 올린다
(2026-07-29) — 라우팅·분류·정형 분석까지 품질을 우선한다. provider·모델 ID·API key는
공용 resolver에서 해석하고, OpenAI는 reasoning effort를, Anthropic은 기존
temperature를 적용한다.

워커 5종은 전부 같은 티어라 역할 "worker" 하나로 묶는다(2026-07-18 확정) —
워커별 모델 차등이 필요해지면 SellerRole 에 세분 역할을 추가하고 ROLE_TIER 에
등록하면 된다. 모델 버전 변경은 일관성 리셋 이벤트로 CHANGELOG 에 기록(§10-①).

⚠️ 전 역할 smart 는 §7 의 90s 목표·seller_route_timeout_s(10s) 예산을 압박한다 —
지연이 문제가 되면 supervisor·judge 부터 fast 로 되돌린다.

판매자 레인은 **전 역할이 function tools 를 싣는다**고 보고 resolver 를
with_tools=True 로 부른다 — create_agent 는 tools 가 비어도 ToolStrategy 구조화
출력이 function tool 로 나가고, 지금 tool 이 없는 report 도 나중에 도구가 붙으면
조용히 깨지기 때문이다(이슈 #178). 조합 미지원 모델에서는 resolver 가
reasoning_effort 를 강등한다.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Literal

from langchain.chat_models import init_chat_model
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel

from app.core.config import LLMProvider, get_settings
from app.core.llm import LLMNotConfigured, ModelTier, resolve_provider_model
from app.core.tracing import current_request_trace

logger = logging.getLogger(__name__)


class _ContentTraceCallback(AsyncCallbackHandler):
    """[#326] 콘텐츠 추적 모드에서 seller 레인 모델 호출 원문을 활성 llm span 에 싣는다.

    seller 는 `app/core/llm.py` 초크포인트를 거치지 않는다 — `init_chat_model` 로 만든
    모델을 LangGraph agent 가 직접 `ainvoke` 하므로(orchestrator 9개 호출부), 모델
    인스턴스에 붙는 이 콜백이 유일한 공통 관문이다. async 핸들러라 호출 태스크의
    contextvar(활성 span 스택)를 그대로 본다.

    같은 span 안에서 agent 가 모델을 여러 번 부르면 마지막 호출이 이전 기록을 덮는다 —
    react agent 의 마지막 모델 호출 입력에는 이전 턴·툴 결과가 모두 포함되므로 마지막
    호출이 곧 가장 완전한 기록이다. 모드 off 면 전 훅이 no-op 이다.

    입력은 `user` 가 아니라 **`transcript`(strict 키)** 로 싣는다 — 이 히스토리에는 tool
    결과(Spring 이 반환한 주문·회원 데이터)가 섞여 있어 "사용자가 직접 타이핑한 텍스트"라는
    lenient 면제 근거가 성립하지 않고, outputs 를 strict 로 둔 것과 같은 위협 모델이라
    숫자열 카나리아(휴대폰·주민번호)를 유지해야 한다(PR #327 리뷰).
    """

    async def on_chat_model_start(  # type: ignore[override]
        self, serialized: dict[str, Any], messages: list[list[Any]], **kwargs: Any
    ) -> None:
        del serialized, kwargs
        if (trace := current_request_trace()) and trace.captures_content:
            rendered = "\n".join(
                f"{message.type}: {message.content}" for batch in messages for message in batch
            )
            trace.record_llm_content(transcript=rendered)

    async def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # type: ignore[override]
        del kwargs
        if (trace := current_request_trace()) and trace.captures_content:
            try:
                text = response.generations[0][0].text
            except (AttributeError, IndexError):
                text = ""
            trace.record_llm_content(output=text)


_CONTENT_TRACE_CALLBACK = _ContentTraceCallback()

SellerRole = Literal[
    "supervisor",
    "planner",
    "worker",
    "judge",
    "product",
    "report",
    "recommend",
    "analysis_judge",
    "graph",
    # [#506] 이미지 기반 등록 초안 — vision(사진 1회 분석)·draft_gate(초안 대기 발화 분류).
    "vision",
    "draft_gate",
    # [#506 후속] category — 에이전트가 카테고리를 못 고른 턴의 폴백 택1(단발 호출).
    "category",
    # [#598] 상주 분석 파이프라인 전용 — 채팅 레인 "report"/"recommend" 와 완전히
    # 분리한다(설계 결정 — 채팅 레인 REPORT_PROMPT/build_report_agent 무접촉 보장).
    "resident_report",
    "resident_recommend",
    # [#598] 워커 4종의 ctx-표 기반 interpret 스텝 — 채팅 레인 워커 역할("worker")과
    # 분리한다. 도구 호출 없는 zero-tool 호출이라 관측·로그를 워커 레인과 구분해 둔다.
    "interpret",
    # [#600] chart_only 턴의 차트 해석 에이전트 — graph(축 선언)와도, interpret(#598
    # 상주 워커)와도 분리한다. zero-tool·자유 텍스트라 report 와 형태는 같지만, 관측·
    # 타임아웃(seller_chart_interpret_timeout_s)을 report 레인과 독립적으로 조정하기
    # 위해 별도 역할로 둔다.
    "chart_interpret",
]

# SPEC §8 표의 코드화 — 판매자 전 역할 smart(2026-07-29, 품질 우선 전환).
# analysis_judge(이슈 #242 분석 검증 층)도 이 정책을 따른다 — DESIGN-ANALYSIS-V31-242
# 결정 D-1: 이슈 원안(fast)을 채택하지 않고 판정 품질을 우선한다. 비용·wall-clock 은
# seller_analysis_judge_timeout_s 분리로 흡수한다(같은 설계서 §9-R1).
# graph(차트 생성, 이슈 #242 5단계)도 smart — 이슈 원안 그대로이며 전 역할 정책과도 일치한다.
ROLE_TIER: dict[SellerRole, ModelTier] = {
    "supervisor": "smart",
    "planner": "smart",
    "worker": "smart",
    "judge": "smart",
    "product": "smart",
    "report": "smart",
    "recommend": "smart",
    "analysis_judge": "smart",
    "graph": "smart",
    # [#506] 전 역할 smart 정책 유지 — vision 은 이미지 이해 품질이 초안 전체의 원천이고,
    # draft_gate 는 오분류가 곧 UX 사고(초안 방치·오폐기)다. 지연이 문제가 되면
    # draft_gate 부터 fast 강등을 검토한다(모듈 docstring 정책과 동일한 완화 순서).
    "vision": "smart",
    "draft_gate": "smart",
    # [#506 후속] 오배정이 등록 후 되돌릴 수 없는 필드(카테고리)를 결정하므로 smart 유지.
    "category": "smart",
    # [#598] 상주 파이프라인도 전 역할 smart 정책을 그대로 따른다 — 판매자 미통지·
    # 무인 실행이라 품질 저하를 사람이 즉시 교정할 기회가 없다.
    "resident_report": "smart",
    "resident_recommend": "smart",
    "interpret": "smart",
    # [#600] 해석도 전 역할 smart 정책을 따른다 — 좌표 근거를 벗어나면 D2/C4/period_grounded
    # 가 잡지만, 애초에 형식·발화 금지를 지키는 산출이 재작성 루프(1회뿐, judge 없음)의
    # 성공률을 좌우한다(09-CHART §3.6).
    "chart_interpret": "smart",
}


def seller_trace_model_metadata(role: SellerRole) -> dict[str, str] | None:
    """Resolve the configured bounded model ID without affecting the seller request."""
    try:
        resolved = resolve_provider_model(get_settings(), ROLE_TIER[role], with_tools=True)
    except Exception:
        logger.warning(
            "seller telemetry model resolution failed code=SELLER_TELEMETRY_MODEL_RESOLUTION_FAILED"
        )
        return None
    return {"model": resolved.model_id}


@lru_cache(maxsize=None)
def _cached_model(
    provider: LLMProvider,
    model_id: str,
    api_key: str,
    temperature: float | None,
    reasoning_effort: str | None,
    timeout: float,
    max_retries: int,
) -> BaseChatModel:
    """실효 provider 모델 설정당 1회만 생성한다.

    모델 인스턴스는 무상태(신원·대화는 호출 인자로만 전달)라 공유해도 안전하다.
    """
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout,
        "max_retries": max_retries,
        # [#326] 콘텐츠 추적 콜백 — 무상태이고 모드 off 면 no-op 이라 캐시 공유에 안전하다.
        "callbacks": [_CONTENT_TRACE_CALLBACK],
    }
    if provider == "openai":
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
    elif temperature is not None:
        kwargs["temperature"] = temperature
    return init_chat_model(model=model_id, model_provider=provider, **kwargs)


def init_seller_model(role: SellerRole) -> BaseChatModel:
    """역할에 배정된 채팅 모델을 반환한다 (SPEC §8).

    Literal 밖 역할은 KeyError 로 즉시 실패 — 신규 역할(예: chart 복원 §12)은
    SellerRole·ROLE_TIER 에 먼저 등록한다. 같은 실효 provider 설정은 같은 인스턴스를
    공유한다(lru_cache).

    **판매자 레인은 `LLM_PROVIDER=scripted`(#438 부하 테스트 스텁)를 지원하지 않는다** —
    명시적으로 먼저 검사해 뜻이 분명한 예외를 던진다. 이 함수는 `LLMClient`(app/core/llm.py)를
    거치지 않고 `init_chat_model(model_provider=...)` 로 langchain 모델을 직접 만들기 때문에,
    검사 없이 흘려보내면 `model_provider="scripted"` 가 langchain 안 어딘가에서 알아보기
    힘든 예외로 죽는다(#438 D5 — 판매자 스텁 구현은 이번 범위 밖, 무료 모드는 구매자 레인
    전용, evals/benchmark/README.md 참조).
    """
    settings = get_settings()
    if settings.llm_provider == "scripted":
        raise LLMNotConfigured(
            "LLM_PROVIDER=scripted has no seller-lane implementation (#438) — the free "
            "load-test mode only covers the buyer recommendation lane. Set LLM_PROVIDER to "
            "openai/anthropic to exercise seller endpoints, or skip seller scenarios in the "
            "load-test manifest."
        )
    tier = ROLE_TIER[role]
    resolved = resolve_provider_model(settings, tier, with_tools=True)
    temperature = None
    if resolved.provider == "anthropic":
        temperature = (
            settings.seller_haiku_temperature
            if tier == "fast"
            else settings.seller_sonnet_temperature
        )
    return _cached_model(
        resolved.provider,
        resolved.model_id,
        resolved.api_key,
        temperature,
        resolved.reasoning_effort,
        settings.llm_timeout_s,
        settings.llm_max_retries,
    )
