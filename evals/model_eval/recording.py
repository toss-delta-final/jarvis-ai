"""프로덕션 LLMClient 호출 경로를 보존하며 provider usage를 채록한다."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from app.core.llm import LLMClient
from evals.model_eval.budget import BudgetTracker
from evals.model_eval.pricing import PriceBook

_call_site: ContextVar[str | None] = ContextVar("model_eval_call_site", default=None)


def _usage(message: Any) -> tuple[dict[str, Any] | None, str]:
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        return usage, "usage_metadata"
    metadata = getattr(message, "response_metadata", None)
    fallback = metadata.get("token_usage") if isinstance(metadata, dict) else None
    if isinstance(fallback, dict):
        return fallback, "response_metadata.token_usage"
    return None, "missing"


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _detail(usage: dict[str, Any], group: str, *names: str) -> int | None:
    details = usage.get(group)
    if not isinstance(details, dict):
        return None
    for name in names:
        value = _integer(details.get(name))
        if value is not None:
            return value
    return None


class RecordingLLM:
    """LLMClient 위임 호출마다 model·usage·latency·correlation ID를 남긴다."""

    def __init__(
        self,
        delegate: LLMClient,
        *,
        models: dict[str, str],
        reasoning_efforts: dict[str, str | None] | None = None,
        pricing: PriceBook | None = None,
        budget: BudgetTracker | None = None,
    ) -> None:
        self.delegate = delegate
        self.models = dict(models)
        self.reasoning_efforts = dict(reasoning_efforts or {})
        self.pricing = pricing
        self.budget = budget
        self.calls: list[dict[str, Any]] = []

    @contextmanager
    def scope(self, label: str) -> Iterator[None]:
        token = _call_site.set(label)
        try:
            yield
        finally:
            _call_site.reset(token)

    def _new_call(self, *, tier: str, json_output: bool, stream: bool) -> dict[str, Any]:
        if label := _call_site.get():
            call_site = label
        elif stream:
            call_site = "stream"
        elif tier == "fast" and json_output:
            call_site = "decompose"
        elif tier == "smart" and json_output:
            call_site = "rerank"
        else:
            call_site = "complete"
        return {
            "correlationId": str(uuid4()),
            "callSite": call_site,
            "tier": tier,
            "model": self.models.get(tier),
            "reasoningEffort": self.reasoning_efforts.get(tier),
            "inputTokens": None,
            "outputTokens": None,
            "reasoningTokens": None,
            "cacheTokens": None,
            "usageSource": "missing",
            "usageMetadata": None,
            "observedChunkCount": 0,
            "latencyMs": None,
            "costUsd": None,
            "error": None,
        }

    @staticmethod
    def _capture(call: dict[str, Any], message: Any, model: str) -> None:
        usage, source = _usage(message)
        call["observedChunkCount"] += 1
        if usage is None:
            return
        # 스트림 usage는 provider마다 최종 chunk 전용 또는 누적값이다. 합산하면 누적형을
        # 이중 계산하므로 호출에서 관측한 마지막 non-null snapshot만 보존한다.
        call["model"] = model or call["model"]
        call["usageSource"] = source
        call["usageMetadata"] = usage
        call["inputTokens"] = _integer(usage.get("input_tokens", usage.get("prompt_tokens")))
        call["outputTokens"] = _integer(usage.get("output_tokens", usage.get("completion_tokens")))
        call["cacheTokens"] = _detail(
            usage,
            "input_token_details",
            "cache_read",
            "cache_read_input_tokens",
            "cached_tokens",
        )
        call["reasoningTokens"] = _detail(
            usage,
            "output_token_details",
            "reasoning",
            "reasoning_tokens",
        )

    def _finish(self, call: dict[str, Any], started: float, error: BaseException | None) -> None:
        call["latencyMs"] = int(round((perf_counter() - started) * 1000))
        if error is not None:
            call["error"] = f"{type(error).__name__}: {error}"
        model = call.get("model")
        if self.pricing is not None and isinstance(model, str):
            call["costUsd"] = self.pricing.cost(
                model=model,
                input_tokens=call["inputTokens"],
                output_tokens=call["outputTokens"],
            )
        self.calls.append(call)
        if self.budget is not None:
            self.budget.record(call)

    async def complete(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
        json_output: bool = True,
    ) -> str:
        if self.budget is not None:
            self.budget.reserve()
        call = self._new_call(tier=tier, json_output=json_output, stream=False)
        started = perf_counter()
        error: BaseException | None = None
        try:
            with patch(
                "app.core.llm._record_usage",
                side_effect=lambda message, model: self._capture(call, message, model),
            ):
                return await self.delegate.complete(
                    system=system,
                    user=user,
                    tier=tier,
                    max_tokens=max_tokens,
                    json_output=json_output,
                )
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._finish(call, started, error)

    async def stream(
        self,
        *,
        system: str,
        user: str,
        tier: str,
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        if self.budget is not None:
            self.budget.reserve()
        call = self._new_call(tier=tier, json_output=False, stream=True)
        started = perf_counter()
        error: BaseException | None = None
        try:
            with patch(
                "app.core.llm._record_usage",
                side_effect=lambda message, model: self._capture(call, message, model),
            ):
                async for chunk in self.delegate.stream(
                    system=system,
                    user=user,
                    tier=tier,
                    max_tokens=max_tokens,
                ):
                    yield chunk
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._finish(call, started, error)
