from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evals.model_eval.budget import BudgetExceeded, BudgetLimits, BudgetTracker, estimate_calls
from evals.model_eval.pricing import PriceBook
from evals.model_eval.recording import RecordingLLM
from evals.model_eval.stats import compare_baseline, summarize_values


def test_budget_tunables_change_preflight_and_runtime_behavior() -> None:
    assert estimate_calls(case_count=2, repeats=2) == 12
    assert BudgetLimits(max_calls=12, max_total_tokens=100, max_cost_usd=1).allows_calls(12)
    assert not BudgetLimits(max_calls=11, max_total_tokens=100, max_cost_usd=1).allows_calls(12)

    tracker = BudgetTracker(BudgetLimits(max_calls=2, max_total_tokens=100, max_cost_usd=1))
    tracker.reserve()
    tracker.record({"inputTokens": 10, "outputTokens": 5, "costUsd": 0.1})
    tracker.reserve()
    tracker.record({"inputTokens": 10, "outputTokens": 5, "costUsd": 0.1})
    with pytest.raises(BudgetExceeded):
        tracker.reserve()


def test_unknown_cost_does_not_silently_pass_budget_gate() -> None:
    tracker = BudgetTracker(BudgetLimits(max_calls=10, max_total_tokens=100, max_cost_usd=1))
    tracker.reserve()
    tracker.record({"inputTokens": 1, "outputTokens": 1, "costUsd": None})
    snapshot = tracker.snapshot()
    assert snapshot["unknownCostCallCount"] == 1
    assert snapshot["costGateStatus"] == "unknown"


def test_budget_record_preserves_provider_error_and_stops_next_call() -> None:
    tracker = BudgetTracker(BudgetLimits(max_calls=10, max_total_tokens=1, max_cost_usd=1))
    delegate = _FailingLLM()
    recorder = RecordingLLM(
        delegate,
        models={"fast": "fast-model"},
        budget=tracker,
    )
    with pytest.raises(RuntimeError, match="provider boom"):
        asyncio.run(recorder.complete(system="", user="", tier="fast"))
    assert recorder.calls[0]["error"] == "RuntimeError: provider boom"
    assert tracker.snapshot()["budgetExceededReason"] == "maxTotalTokensExceeded"
    with pytest.raises(BudgetExceeded, match="maxTotalTokensExceeded"):
        asyncio.run(recorder.complete(system="", user="", tier="fast"))
    assert delegate.calls == 1


def test_pricing_cost_and_coverage_propagate_unknown(tmp_path: Path) -> None:
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "version": "1",
                "entries": [
                    {
                        "model": "known",
                        "inPer1k": 0.1,
                        "outPer1k": 0.2,
                        "effectiveDate": "2026-01-01",
                        "source": "https://example.test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    book = PriceBook.load(path)
    assert book.cost(model="known", input_tokens=1000, output_tokens=500) == pytest.approx(0.2)
    assert book.cost(model="missing", input_tokens=1, output_tokens=1) is None
    assert book.cost(model="known", input_tokens=None, output_tokens=1) is None
    coverage = book.coverage(
        [
            {"inputTokens": 1, "outputTokens": 2, "costUsd": 0.1},
            {"inputTokens": None, "outputTokens": 2, "costUsd": None},
        ]
    )
    assert coverage == {"costCoverage": 0.5, "tokenCoverage": 0.5}


def test_repository_pricing_manifest_includes_luna() -> None:
    book = PriceBook.load()
    assert book.cost(model="gpt-5.6-luna", input_tokens=1000, output_tokens=1000) == pytest.approx(
        0.007
    )
    assert book.entries["gpt-5.6-luna"]["effectiveDate"] == "2026-08-13"
    assert (
        book.entries["gpt-5.6-luna"]["source"]
        == "https://developers.openai.com/api/docs/models/gpt-5.6-luna"
    )


class _Message:
    def __init__(
        self,
        content: str,
        *,
        usage_metadata: dict | None = None,
        response_metadata: dict | None = None,
    ) -> None:
        self.content = content
        self.usage_metadata = usage_metadata
        self.response_metadata = response_metadata or {}


class _FakeLLM:
    def __init__(self, messages: list[_Message]) -> None:
        self.messages = messages

    async def complete(self, **kwargs) -> str:
        del kwargs
        from app.core import llm

        message = self.messages[0]
        llm._record_usage(message, "delegate-model")
        return message.content

    async def stream(self, **kwargs):
        del kwargs
        from app.core import llm

        for message in self.messages:
            llm._record_usage(message, "delegate-model")
            yield message.content


class _FailingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **kwargs) -> str:
        del kwargs
        self.calls += 1
        from app.core import llm

        llm._record_usage(
            _Message("error", usage_metadata={"input_tokens": 2, "output_tokens": 1}),
            "fast-model",
        )
        raise RuntimeError("provider boom")

    async def stream(self, **kwargs):
        del kwargs
        if False:
            yield ""


def test_recording_captures_usage_details_fallback_missing_and_correlation_ids() -> None:
    book = PriceBook(
        version="test",
        entries={"fast-model": {"inPer1k": 0.1, "outPer1k": 0.2}},
    )
    usage = {
        "input_tokens": 10,
        "output_tokens": 4,
        "input_token_details": {"cache_read": 3},
        "output_token_details": {"reasoning": 2},
    }
    recorder = RecordingLLM(
        _FakeLLM([_Message("ok", usage_metadata=usage)]),
        models={"fast": "fast-model", "smart": "smart-model"},
        pricing=book,
    )
    with recorder.scope("decompose"):
        assert (
            asyncio.run(recorder.complete(system="", user="", tier="fast", json_output=True))
            == "ok"
        )
    call = recorder.calls[0]
    assert call["usageSource"] == "usage_metadata"
    assert call["inputTokens"] == 10
    assert call["outputTokens"] == 4
    assert call["cacheTokens"] == 3
    assert call["reasoningTokens"] == 2
    assert call["correlationId"]

    fallback = RecordingLLM(
        _FakeLLM(
            [
                _Message(
                    "ok",
                    response_metadata={"token_usage": {"prompt_tokens": 7, "completion_tokens": 2}},
                )
            ]
        ),
        models={"fast": "fast-model"},
        pricing=book,
    )
    asyncio.run(fallback.complete(system="", user="", tier="fast"))
    assert fallback.calls[0]["usageSource"] == "response_metadata.token_usage"

    missing = RecordingLLM(
        _FakeLLM([_Message("ok")]),
        models={"fast": "fast-model"},
        pricing=book,
    )
    asyncio.run(missing.complete(system="", user="", tier="fast"))
    assert missing.calls[0]["usageSource"] == "missing"
    assert missing.calls[0]["inputTokens"] is None


def test_recording_stream_keeps_last_non_null_usage_without_summing() -> None:
    messages = [
        _Message("a", usage_metadata={"input_tokens": 3, "output_tokens": 1}),
        _Message("b", usage_metadata={"input_tokens": 3, "output_tokens": 2}),
    ]
    recorder = RecordingLLM(_FakeLLM(messages), models={"smart": "smart-model"})

    async def consume() -> list[str]:
        return [part async for part in recorder.stream(system="", user="", tier="smart")]

    assert asyncio.run(consume()) == ["a", "b"]
    assert recorder.calls[0]["observedChunkCount"] == 2
    assert recorder.calls[0]["outputTokens"] == 2


def test_stats_and_regression_boundaries() -> None:
    summary = summarize_values([1.0, 2.0, 3.0, 4.0])
    assert summary["n"] == 4
    assert summary["mean"] == pytest.approx(2.5)
    assert summary["median"] == pytest.approx(2.5)
    assert summary["sd"] == pytest.approx(1.2909944487)
    assert summary["iqr"] == pytest.approx(2.0)

    current = {
        "datasetHash": "same",
        "casePrimaryMetrics": {"a": [0.9], "b": [0.9]},
    }
    baseline = {
        "datasetHash": "same",
        "casePrimaryMetrics": {"a": [1.0], "b": [1.0]},
    }
    result = compare_baseline(
        current,
        baseline,
        margin=0.03,
        resamples=200,
        confidence=0.95,
        seed=7,
        multiplicity="customMultiplicity",
    )
    assert result["verdict"] == "regression"
    assert result["pairedCount"] == 2
    assert result["pairedSummary"]["n"] == 2
    assert result["pairedSummary"]["mean"] == pytest.approx(-0.1)
    assert result["multiplicity"] == "customMultiplicity"
    assert result["currentHardFailureCount"] == 0
    hard_failure_result = compare_baseline(
        {**current, "hardFailureCount": 1},
        baseline,
        margin=0.2,
        resamples=20,
        confidence=0.95,
        seed=7,
        multiplicity="customMultiplicity",
    )
    assert hard_failure_result["verdict"] == "inconclusive"
    with pytest.raises(ValueError, match="datasetHash"):
        compare_baseline(
            current,
            {**baseline, "datasetHash": "different"},
            margin=0.03,
            resamples=20,
            confidence=0.95,
            seed=7,
            multiplicity="customMultiplicity",
        )
