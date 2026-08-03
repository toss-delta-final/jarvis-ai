"""두 LLM arm을 합산하는 ablation 사전 예산 gate."""

from __future__ import annotations

from typing import Any

from evals.model_eval.budget import BudgetLimits, preflight

SINGLE_CALL_CALLS_PER_CASE = 1


def preflight_ablation(
    *, case_count: int, repeats: int, limits: BudgetLimits
) -> dict[str, Any]:
    """pipeline 보수 3회와 single-call 1회를 같은 상한에 합산한다."""
    pipeline = preflight(case_count=case_count, repeats=repeats, limits=limits)
    single_calls = case_count * repeats * SINGLE_CALL_CALLS_PER_CASE
    expected_calls = int(pipeline["expectedCalls"]) + single_calls
    return {
        "caseCount": case_count,
        "repeats": repeats,
        "pipelineCallsPerCase": pipeline["callsPerCase"],
        "singleCallCallsPerCase": SINGLE_CALL_CALLS_PER_CASE,
        "pipelineExpectedCalls": pipeline["expectedCalls"],
        "singleCallExpectedCalls": single_calls,
        "expectedCalls": expected_calls,
        "maxCalls": limits.max_calls,
        "maxTotalTokens": limits.max_total_tokens,
        "maxCostUsd": limits.max_cost_usd,
        "allowed": limits.allows_calls(expected_calls),
    }
