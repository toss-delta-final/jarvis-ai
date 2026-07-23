"""판매자 그래프 모델 팩토리 (SPEC-SELLER-001 §8 — provider-neutral 2-tier).

역할→티어 매핑을 코드로 고정한다: supervisor·planner·워커 5종·judge 는
fast, report·recommend 는 smart. provider·모델 ID·API key는 공용 resolver에서
해석하고, OpenAI는 reasoning effort를, Anthropic은 기존 temperature를 적용한다.

워커 5종은 전부 같은 티어라 역할 "worker" 하나로 묶는다(2026-07-18 확정) —
워커별 모델 차등이 필요해지면 SellerRole 에 세분 역할을 추가하고 ROLE_TIER 에
등록하면 된다. 모델 버전 변경은 일관성 리셋 이벤트로 CHANGELOG 에 기록(§10-①).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from app.core.config import LLMProvider, get_settings
from app.core.llm import ModelTier, resolve_provider_model

SellerRole = Literal["supervisor", "planner", "worker", "judge", "product", "report", "recommend"]

# SPEC §8 표의 코드화 — 라우팅·분류·정형 분석은 fast, 서술·추천은 smart.
ROLE_TIER: dict[SellerRole, ModelTier] = {
    "supervisor": "fast",
    "planner": "fast",
    "worker": "fast",
    "judge": "fast",
    "product": "fast",
    "report": "smart",
    "recommend": "smart",
}


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
    """
    settings = get_settings()
    tier = ROLE_TIER[role]
    resolved = resolve_provider_model(settings, tier)
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
