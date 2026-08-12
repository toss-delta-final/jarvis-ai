"""2-tier LLM 클라이언트 — provider 토글(Claude/OpenAI) + tier 추상화 (이슈 #40).

노드·파이프라인은 LLMClient 프로토콜을 **주입**받고 `tier`("fast" | "smart")로 호출한다.
각 provider 가 tier → 자기 모델 id 로 매핑한다(Anthropic: fast=haiku/smart=sonnet,
OpenAI: fast=gpt-5-nano/smart=gpt-5.6-luna). get_llm 이 settings.llm_provider 로 분기하며,
해당 provider 의 API 키가 없으면 None(호출측이 LLM_UNAVAILABLE 처리).

계약(api-spec)·SSE 는 무관 — 순수 내부 구현. ChatAnthropic/ChatOpenAI 는 _chat 에서
지연 import 하여 테스트가 SDK 없이도 돈다. 타임아웃·재시도는 config(llm_timeout_s /
llm_max_retries). OpenAI 는 complete(JSON 태스크)에서만 response_format=json 을 강제하고
stream(평문 채팅)에서는 제외한다.

OpenAI 는 모델별로 function tools + reasoning_effort 조합 지원이 갈린다 — 조합 미지원
모델(config 목록)에서는 tool 을 싣는 호출(resolve_provider_model(with_tools=True))의
effort 를 override 값으로 강등한다(이슈 #178).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import import_module
from time import perf_counter
from typing import Any, Literal, Protocol, runtime_checkable

from langsmith.run_helpers import tracing_context

from app.core.config import LLMProvider, Settings, get_settings
from app.core.tracing import current_model_call_usage, current_request_trace

ModelTier = Literal["fast", "smart"]

# #438 D6/R3 G1 — scripted(부하 테스트 스텁) 모델 id 정본. 서버 로그 chat_request.model_ids 에
# 이 id 가 실려 manifest 가 그대로 실으므로, 운영자가 provider 라벨을 잘못 붙여도 산출물
# 자체가 스텁임을 증언한다(D6). 관측 경로(record_model_call → resolve_model_id, 이 값을 쓴다)와
# usage 경로(app/core/llm_scripted.py::_record_loadtest_usage)가 여기 값을 함께 가져다 쓴다 —
# 두 곳에 리터럴을 따로 적으면 드리프트 시 tier 당 서로 다른 id 가 새어 manifest 의
# model_ids 가 스텁 id 4개로 불어난다(R3 G1 실측). llm_scripted 는 이미 이 모듈의 LLMError 를
# import 하므로(순환 없음) 정본을 여기 두고 llm_scripted 가 가져다 쓰는 방향이 자연스럽다.
LOADTEST_MODEL_IDS: dict[str, str] = {
    "fast": "scripted-stub-fast",
    "smart": "scripted-stub-smart",
}


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


@lru_cache(maxsize=1)
def _timeout_exception_types() -> tuple[type[BaseException], ...]:
    """타임아웃으로 볼 예외 타입 집합 — 설치된 SDK 만 지연 수집한다.

    내장 ``TimeoutError`` 는 3.11+ 에서 ``asyncio.TimeoutError`` 와 **같은 객체**라
    한 항목으로 둘 다 덮인다. httpx·provider SDK 는 import 실패를 무시한다 —
    이 모듈의 기존 규약대로 SDK 없이도 테스트가 돌아야 하기 때문이다.
    """
    types_: list[type[BaseException]] = [TimeoutError]
    for module_name, attr in (
        ("httpx", "TimeoutException"),
        ("anthropic", "APITimeoutError"),
        ("openai", "APITimeoutError"),
    ):
        try:
            module = import_module(module_name)
        except ImportError:  # pragma: no cover - SDK 미설치 환경
            continue
        candidate = getattr(module, attr, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            types_.append(candidate)
    return tuple(types_)


def is_timeout_error(exc: BaseException | None) -> bool:
    """예외(와 그 원인 체인)가 상류 타임아웃인지 **타입으로** 판정한다.

    문자열 매칭을 쓰지 않는 이유: provider SDK 의 메시지는 ``"Request timed out."``
    (timed **out**, 공백 포함)이라 ``"timeout" in str(exc)`` 로는 걸리지 않고,
    ``httpx.ReadTimeout`` 은 ``str(exc)`` 가 비는 경우가 있다. 가짜 예외로 쓴 테스트만
    통과하고 실제 SDK 예외는 한 번도 통과시켜 본 적이 없는 판정이 된다.

    원인 체인을 따라가는 이유: ``AnthropicLLM.complete`` 등이 SDK 예외를
    ``raise LLMError(str(exc)) from exc`` 로 감싸므로 원본 타입이 ``__cause__`` 에만 남는다.
    """
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _timeout_exception_types()):
            return True
        current = current.__cause__ or current.__context__
    return False


@lru_cache(maxsize=1)
def _output_length_exception_types() -> tuple[type[BaseException], ...]:
    """출력 토큰 예산 소진으로 볼 예외 타입 집합 — 설치된 SDK 만 지연 수집한다.

    ``_timeout_exception_types`` 와 같은 규약: import 실패를 무시해 SDK 미설치
    환경(테스트 등)에서도 이 모듈이 죽지 않는다.
    """
    types_: list[type[BaseException]] = []
    for module_name, attr in (("openai", "LengthFinishReasonError"),):
        try:
            module = import_module(module_name)
        except ImportError:  # pragma: no cover - SDK 미설치 환경
            continue
        candidate = getattr(module, attr, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            types_.append(candidate)
    return tuple(types_)


def is_output_length_error(exc: BaseException | None) -> bool:
    """예외(와 그 원인 체인)가 출력 토큰 예산 소진인지 **타입으로** 판정한다(#325 R6).

    ``openai.LengthFinishReasonError`` 는 응답이 ``finish_reason="length"`` 로 끊겨
    구조화 출력 파싱 자체가 불가능할 때 SDK 가 던진다 — #325 의 원 사례이며, 같은
    입력을 다시 보내도 같은 자리에서 끊기는 그 항목 고유의 결정적 실패다.
    ``is_timeout_error`` 와는 판정 대상이 겹치지 않는 별도 헬퍼로 둔다 — 그 함수는
    사용자 대면 ``LLM_TIMEOUT`` 매핑에 쓰이므로 판정 범위를 넓히면 무관한 계약 표면이
    틀어진다.
    """
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _output_length_exception_types()):
            return True
        current = current.__cause__ or current.__context__
    return False


def resolve_model_id(settings: Settings, tier: ModelTier) -> str:
    """API key와 무관하게 활성 provider의 tier별 모델 ID를 해석한다."""
    if tier not in ("fast", "smart"):
        raise LLMError(f"unknown tier: {tier!r}")

    if settings.llm_provider == "scripted":
        # #438 D6 — 산출물 자기표기. LOADTEST_MODEL_IDS(이 모듈, 위)가 정본이다.
        return LOADTEST_MODEL_IDS[tier]
    if settings.llm_provider == "openai":
        return {
            "fast": settings.openai_fast_model_id,
            "smart": settings.openai_smart_model_id,
        }[tier]
    return {"fast": settings.haiku_model_id, "smart": settings.sonnet_model_id}[tier]


def supports_tool_reasoning(settings: Settings, model_id: str) -> bool:
    """model_id 가 function tools + reasoning_effort 동시 사용을 지원하는지 (이슈 #178).

    config 의 미지원 목록과 **접두사** 매칭한다 — 날짜 스냅샷 ID(예:
    gpt-5.6-luna-2026-07-01)도 같은 제약을 받는다고 본다. 빈 항목은 전체 매칭을
    유발하므로 무시한다.
    """
    return not any(
        entry and model_id.startswith(entry)
        for entry in settings.openai_tool_reasoning_incompatible_models
    )


def resolve_provider_model(
    settings: Settings, tier: ModelTier, *, with_tools: bool = False
) -> ResolvedModel:
    """provider/tier를 모델 ID·API key·reasoning effort로 해석한다.

    with_tools 는 호출부가 **function tools 를 싣는다**는 선언이다 — 판매자 그래프의
    create_agent 는 tools 가 비어도 ToolStrategy 구조화 출력이 function tool 로 나가므로
    전부 해당한다. 조합 미지원 모델에서는 effort 를 config override 값으로 강등해
    400(invalid_request_error)을 막는다(이슈 #178). 구매자 레인(OpenAILLM.complete/
    stream)은 tool 을 싣지 않아 기본값 False 로 영향이 없다.
    """
    model_id = resolve_model_id(settings, tier)

    provider = settings.llm_provider
    if provider == "scripted":
        # #438 D5 — 부하 테스트 스텁은 API 키가 전혀 필요 없다. config.py 의 G1 validator 가
        # local/test 밖 기동을 이미 막으므로 여기서는 무조건 통과시킨다(오탐일 수 없다 — 설정값이
        # 곧 사실이다).
        return ResolvedModel(provider=provider, tier=tier, model_id=model_id, api_key="")
    if provider == "openai":
        if not settings.openai_api_key:
            raise LLMNotConfigured("openai API key is not configured")
        reasoning = {
            "fast": settings.openai_fast_reasoning_effort,
            "smart": settings.openai_smart_reasoning_effort,
        }
        effort = reasoning[tier]
        if with_tools and not supports_tool_reasoning(settings, model_id):
            effort = settings.openai_tool_reasoning_effort_override
        return ResolvedModel(
            provider=provider,
            tier=tier,
            model_id=model_id,
            api_key=settings.openai_api_key,
            reasoning_effort=effort,
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
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
        reasoning_effort: str | None = None,
    ) -> str:
        """단발 완성 텍스트를 반환한다. json_output=False 는 마크다운/평문 태스크(예: 프로필 요약).

        reasoning_effort: None 이면 tier 기본 effort(현행 동작 불변). 값이 주어지면 그 호출만
        tier 기본 대신 그 effort 로 강제한다(#325 enrichment 등 구조화 추출 전용 안정화).
        """
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


def _record_content(system: str, user: str, output: str | None) -> None:
    """[#326] 콘텐츠 추적 모드에서 활성 LLM span 에 prompt·응답 원문을 싣는다(모드 off 면 no-op)."""
    if (trace := current_request_trace()) and trace.captures_content:
        trace.record_llm_content(system=system, user=user, output=output)


def _record_usage(message: Any, model: str) -> None:
    """Record only normalized model/token facts on the active explicit LLM span."""
    usage = getattr(message, "usage_metadata", None)
    response_metadata = getattr(message, "response_metadata", None)
    if not isinstance(usage, dict):
        usage = (
            response_metadata.get("token_usage") if isinstance(response_metadata, dict) else None
        )
    usage = usage if isinstance(usage, dict) else {}
    prompt_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    completion_tokens = usage.get("output_tokens", usage.get("completion_tokens"))

    details: list[dict[str, Any]] = []
    for value in (
        usage.get("input_token_details"),
        usage.get("prompt_tokens_details"),
        response_metadata.get("input_tokens_details")
        if isinstance(response_metadata, dict)
        else None,
        response_metadata.get("prompt_tokens_details")
        if isinstance(response_metadata, dict)
        else None,
    ):
        if isinstance(value, dict):
            details.append(value)

    def detail_tokens(*keys: str, suffixes: tuple[str, ...] = ()) -> int | None:
        for source in (*details, usage):
            for key, value in source.items():
                if (key in keys or any(key.endswith(suffix) for suffix in suffixes)) and isinstance(
                    value, int
                ):
                    return max(value, 0)
        return None

    cached_input_tokens = detail_tokens(
        "cache_read",
        "cached_tokens",
        "cache_read_input_tokens",
        suffixes=("_cache_read",),
    )
    cache_write_tokens = detail_tokens(
        "cache_creation",
        "cache_write",
        "cache_write_tokens",
        "cache_creation_input_tokens",
        suffixes=("_cache_creation", "_cache_write"),
    )
    if trace := current_request_trace():
        trace.record_llm_usage(
            model=model,
            prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
            completion_tokens=completion_tokens if isinstance(completion_tokens, int) else None,
            call_id=current_model_call_usage(),
            cached_input_tokens=cached_input_tokens,
            cache_write_tokens=cache_write_tokens,
        )


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
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
        reasoning_effort: str | None = None,
    ) -> str:
        # json_output: Anthropic 은 프롬프트 기반 JSON 이라 무시(시그니처 정합용).
        # reasoning_effort: Anthropic 은 effort 개념이 없어 무시한다(#325).
        del reasoning_effort
        from langchain_core.messages import HumanMessage, SystemMessage

        model = self._resolve(tier)
        try:
            with tracing_context(enabled=False):
                resp = await self._chat(model, max_tokens).ainvoke(
                    [SystemMessage(content=system), HumanMessage(content=user)]
                )
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK 예외를 LLMError 로 통일 매핑
            raise LLMError(str(exc)) from exc
        _record_usage(resp, model)
        text = _as_text(resp.content)
        _record_content(system, user, text)
        return text

    async def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        from langchain_core.messages import HumanMessage, SystemMessage

        model = self._resolve(tier)
        started = perf_counter()
        first_text = True
        # [#326] 콘텐츠 추적 모드에서만 응답 원문을 누적한다(off 면 메모리 비용 0).
        collected: list[str] | None = None
        if (t := current_request_trace()) and t.captures_content:
            collected = []
        try:
            with tracing_context(enabled=False):
                async for chunk in self._chat(model, max_tokens).astream(
                    [SystemMessage(content=system), HumanMessage(content=user)]
                ):
                    _record_usage(chunk, model)
                    text = _as_text(chunk.content)
                    if text:
                        if first_text:
                            first_text = False
                            if trace := current_request_trace():
                                trace.record_provider_ttft(
                                    int(round((perf_counter() - started) * 1000))
                                )
                        if collected is not None:
                            collected.append(text)
                        yield text
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(str(exc)) from exc
        finally:
            # [#326] 부분 소비·중단 시에도 그때까지 모인 응답을 콘텐츠로 남긴다(모드 off 면 None).
            if collected is not None:
                _record_content(system, user, "".join(collected))


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
        self._cache: dict[tuple[str, int, bool, str | None], Any] = {}

    def _resolve(self, tier: str) -> tuple[str, str]:
        try:
            return self._models[tier], self._reasoning[tier]
        except KeyError:
            raise LLMError(f"unknown tier: {tier!r}") from None

    def _chat(
        self, tier: str, max_tokens: int, *, json_mode: bool, effort_override: str | None = None
    ) -> Any:
        from langchain_openai import ChatOpenAI

        model, tier_effort = self._resolve(tier)
        effort = effort_override if effort_override is not None else tier_effort
        # effort_override 를 키에 포함 — 같은 (tier, max_tokens, json) 에서 override 유/무가
        # 섞이면 먼저 만든 클라이언트가 재사용돼 effort 가 조용히 무시된다(#325 캐시 오염).
        key = (tier, max_tokens, json_mode, effort_override)
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
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
        reasoning_effort: str | None = None,
    ) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        model, _ = self._resolve(tier)
        try:
            with tracing_context(enabled=False):
                resp = await self._chat(
                    tier, max_tokens, json_mode=json_output, effort_override=reasoning_effort
                ).ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(str(exc)) from exc
        _record_usage(resp, model)
        text = _as_text(resp.content)
        _record_content(system, user, text)
        return text

    async def stream(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024
    ) -> AsyncIterator[str]:
        from langchain_core.messages import HumanMessage, SystemMessage

        model, _ = self._resolve(tier)
        started = perf_counter()
        first_text = True
        # [#326] 콘텐츠 추적 모드에서만 응답 원문을 누적한다(off 면 메모리 비용 0).
        collected: list[str] | None = None
        if (t := current_request_trace()) and t.captures_content:
            collected = []
        try:
            with tracing_context(enabled=False):
                async for chunk in self._chat(tier, max_tokens, json_mode=False).astream(
                    [SystemMessage(content=system), HumanMessage(content=user)]
                ):
                    _record_usage(chunk, model)
                    text = _as_text(chunk.content)
                    if text:
                        if first_text:
                            first_text = False
                            if trace := current_request_trace():
                                trace.record_provider_ttft(
                                    int(round((perf_counter() - started) * 1000))
                                )
                        if collected is not None:
                            collected.append(text)
                        yield text
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(str(exc)) from exc
        finally:
            # [#326] 부분 소비·중단 시에도 그때까지 모인 응답을 콘텐츠로 남긴다(모드 off 면 None).
            if collected is not None:
                _record_content(system, user, "".join(collected))


def get_llm() -> LLMClient | None:
    """settings.llm_provider 로 라이브 클라이언트를 만든다. 해당 provider 키가 없으면 None.

    키가 없는 개발·CI 에서 네트워크 호출 없이 곧바로 미구성 경로(LLM_UNAVAILABLE)로 빠지게 한다.
    """
    settings = get_settings()
    if settings.llm_provider == "scripted":
        # 지연 import — llm_scripted 는 이 모듈의 LLMError 를 참조하므로 모듈 top-level에서
        # 서로를 import 하면 순환이 생긴다(#438 D5). 키를 전혀 요구하지 않고 항상 돌려준다.
        from app.core.llm_scripted import LoadTestLLM

        return LoadTestLLM(
            mode=settings.scripted_llm_mode,
            delay_s=settings.scripted_llm_delay_s,
        )
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
