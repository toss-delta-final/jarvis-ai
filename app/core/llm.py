"""2-tier LLM 클라이언트 — provider 토글(Claude/OpenAI) + tier 추상화 (이슈 #40).

노드·파이프라인은 LLMClient 프로토콜을 **주입**받고 `tier`("fast" | "smart")로 호출한다.
각 provider 가 tier → 자기 모델 id 로 매핑한다(Anthropic: fast=haiku/smart=sonnet,
OpenAI: fast=gpt-5-nano/smart=gpt-5.6-luna). get_llm 이 settings.llm_provider 로 분기하며,
해당 provider 의 API 키가 없으면 None(호출측이 LLM_UNAVAILABLE 처리).

계약(api-spec)·SSE 는 무관 — 순수 내부 구현. ChatAnthropic/ChatOpenAI 는 _chat 에서
지연 import 하여 테스트가 SDK 없이도 돈다. 타임아웃·재시도는 config(llm_timeout_s /
llm_max_retries). OpenAI 는 complete(JSON 태스크)에서만 response_format=json 을 강제하고
stream(평문 채팅)에서는 제외한다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from app.core.config import LLMProvider, Settings, get_settings

ModelTier = Literal["fast", "smart"]


@dataclass(frozen=True)
class ResolvedModel:
    """활성 provider에서 tier에 대응하는 모델 설정."""

    provider: LLMProvider
    tier: ModelTier
    model_id: str
    api_key: str = field(repr=False)
    reasoning_effort: str | None = None


class LLMError(Exception):
    """LLM 호출 실패(오류/타임아웃/미구성). 상위에서 LLM_UNAVAILABLE / LLM_TIMEOUT 로 매핑한다."""


class LLMNotConfigured(LLMError):
    """활성 provider의 API key가 없어 모델을 만들 수 없다."""


def resolve_model_id(settings: Settings, tier: ModelTier) -> str:
    """API key와 무관하게 활성 provider의 tier별 모델 ID를 해석한다."""
    if tier not in ("fast", "smart"):
        raise LLMError(f"unknown tier: {tier!r}")

    if settings.llm_provider == "openai":
        return {
            "fast": settings.openai_fast_model_id,
            "smart": settings.openai_smart_model_id,
        }[tier]
    return {"fast": settings.haiku_model_id, "smart": settings.sonnet_model_id}[tier]


def resolve_provider_model(settings: Settings, tier: ModelTier) -> ResolvedModel:
    """provider/tier를 모델 ID·API key·reasoning effort로 해석한다."""
    model_id = resolve_model_id(settings, tier)

    provider = settings.llm_provider
    if provider == "openai":
        if not settings.openai_api_key:
            raise LLMNotConfigured("openai API key is not configured")
        reasoning = {
            "fast": settings.openai_fast_reasoning_effort,
            "smart": settings.openai_smart_reasoning_effort,
        }
        return ResolvedModel(
            provider=provider,
            tier=tier,
            model_id=model_id,
            api_key=settings.openai_api_key,
            reasoning_effort=reasoning[tier],
        )

    if not settings.anthropic_api_key:
        raise LLMNotConfigured("anthropic API key is not configured")
    return ResolvedModel(
        provider=provider,
        tier=tier,
        model_id=model_id,
        api_key=settings.anthropic_api_key,
    )


@runtime_checkable
class LLMClient(Protocol):
    """LLM 호출 계약. tier("fast"|"smart")로 호출 — decompose·enrichment·delta(fast) / rerank·consolidate(smart)."""

    async def complete(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024, json_output: bool = True
    ) -> str:
        """단발 완성 텍스트를 반환한다. json_output=False 는 마크다운/평문 태스크(예: 프로필 요약)."""
        ...

    def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        """토큰 증분을 비동기로 산출한다."""
        ...


def _as_text(content: Any) -> str:
    """langchain 메시지 content(str | 블록 리스트)를 평문으로 정규화한다."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


class AnthropicLLM:
    """ChatAnthropic 래퍼. tier → 모델 id 매핑(fast=haiku/smart=sonnet), (model, max_tokens)별 캐시."""

    def __init__(
        self, api_key: str, *, fast_model: str, smart_model: str, timeout: float, max_retries: int
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._models = {"fast": fast_model, "smart": smart_model}
        self._cache: dict[tuple[str, int], Any] = {}

    def _resolve(self, tier: str) -> str:
        try:
            return self._models[tier]
        except KeyError:
            raise LLMError(f"unknown tier: {tier!r}") from None

    def _chat(self, model: str, max_tokens: int) -> Any:
        from langchain_anthropic import ChatAnthropic

        key = (model, max_tokens)
        if key not in self._cache:
            self._cache[key] = ChatAnthropic(
                model=model,
                api_key=self._api_key,
                timeout=self._timeout,
                max_retries=self._max_retries,
                max_tokens=max_tokens,
                stop=None,
            )
        return self._cache[key]

    async def complete(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024, json_output: bool = True
    ) -> str:
        # json_output: Anthropic 은 프롬프트 기반 JSON 이라 무시(시그니처 정합용).
        from langchain_core.messages import HumanMessage, SystemMessage

        try:
            resp = await self._chat(self._resolve(tier), max_tokens).ainvoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            )
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK 예외를 LLMError 로 통일 매핑
            raise LLMError(str(exc)) from exc
        return _as_text(resp.content)

    async def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        from langchain_core.messages import HumanMessage, SystemMessage

        try:
            async for chunk in self._chat(self._resolve(tier), max_tokens).astream(
                [SystemMessage(content=system), HumanMessage(content=user)]
            ):
                text = _as_text(chunk.content)
                if text:
                    yield text
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(str(exc)) from exc


class OpenAILLM:
    """ChatOpenAI 래퍼. tier → (모델 id, reasoning_effort) 매핑, (model, max_tokens, json)별 캐시.

    complete 는 response_format=json_object 로 구조화 출력을 강제하고(decompose·rerank·
    enrichment·profile 이 모두 JSON 소비), stream 은 평문(구매자 일반 채팅 fallback)이라 제외한다.
    fast tier 는 GPT-5 nano의 최저 지원값인 minimal로 비용·지연과 출력 예산을 안정화한다.
    """

    def __init__(
        self,
        api_key: str,
        *,
        fast_model: str,
        smart_model: str,
        timeout: float,
        max_retries: int,
        fast_reasoning_effort: str = "minimal",
        smart_reasoning_effort: str = "medium",
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._models = {"fast": fast_model, "smart": smart_model}
        self._reasoning = {"fast": fast_reasoning_effort, "smart": smart_reasoning_effort}
        self._cache: dict[tuple[str, int, bool], Any] = {}

    def _resolve(self, tier: str) -> tuple[str, str]:
        try:
            return self._models[tier], self._reasoning[tier]
        except KeyError:
            raise LLMError(f"unknown tier: {tier!r}") from None

    def _chat(self, tier: str, max_tokens: int, *, json_mode: bool) -> Any:
        from langchain_openai import ChatOpenAI

        model, effort = self._resolve(tier)
        key = (
            tier,
            max_tokens,
            json_mode,
        )  # tier→(model,effort) 결정적 — effort 구분 위해 tier 로 키
        if key not in self._cache:
            kwargs: dict[str, Any] = {
                "model": model,
                "api_key": self._api_key,
                "timeout": self._timeout,
                "max_retries": self._max_retries,
                "max_tokens": max_tokens,
            }
            if effort:
                kwargs["reasoning_effort"] = effort
            if json_mode:
                kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}
            self._cache[key] = ChatOpenAI(**kwargs)
        return self._cache[key]

    async def complete(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024, json_output: bool = True
    ) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        try:
            resp = await self._chat(tier, max_tokens, json_mode=json_output).ainvoke(
                [SystemMessage(content=system), HumanMessage(content=user)]
            )
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(str(exc)) from exc
        return _as_text(resp.content)

    async def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        from langchain_core.messages import HumanMessage, SystemMessage

        try:
            async for chunk in self._chat(tier, max_tokens, json_mode=False).astream(
                [SystemMessage(content=system), HumanMessage(content=user)]
            ):
                text = _as_text(chunk.content)
                if text:
                    yield text
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(str(exc)) from exc


def get_llm() -> LLMClient | None:
    """settings.llm_provider 로 라이브 클라이언트를 만든다. 해당 provider 키가 없으면 None.

    키가 없는 개발·CI 에서 네트워크 호출 없이 곧바로 미구성 경로(LLM_UNAVAILABLE)로 빠지게 한다.
    """
    settings = get_settings()
    try:
        fast = resolve_provider_model(settings, "fast")
        smart = resolve_provider_model(settings, "smart")
    except LLMNotConfigured:
        return None

    if fast.provider == "openai":
        return OpenAILLM(
            fast.api_key,
            fast_model=fast.model_id,
            smart_model=smart.model_id,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            fast_reasoning_effort=fast.reasoning_effort or "",
            smart_reasoning_effort=smart.reasoning_effort or "",
        )

    return AnthropicLLM(
        fast.api_key,
        fast_model=fast.model_id,
        smart_model=smart.model_id,
        timeout=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
    )
