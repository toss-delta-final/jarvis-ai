"""기존 metric runner를 반복 호출해 case×repeat 결과를 수집한다."""

from __future__ import annotations

from typing import Any

from evals.goldenset.schema import GoldenCase
from evals.metrics.runner import EvaluationFixtures, evaluate
from evals.metrics.settings import EvaluationSettings
from evals.model_eval.adapter import LiveBuyerAdapter
from evals.model_eval.budget import BudgetExceeded, BudgetTracker
from evals.model_eval.stats import bootstrap_mean_ci, summarize_values

_NESTED_METRICS = frozenset({"ndcgAtK", "recallAtK", "precisionAtK"})
_SCALAR_METRICS = frozenset({"mrr", "filterAccuracy", "hardConstraintViolationRate"})
_SUPPORTED_K = frozenset(EvaluationSettings().eval_buyer_k_list)


def validate_metric_path(path: str) -> None:
    """지원하는 overall metric 경로만 실행 전에 허용한다."""
    parts = path.split(".")
    if len(parts) == 3 and parts[0] == "overall" and parts[1] in _NESTED_METRICS:
        if parts[2].isdigit() and int(parts[2]) in _SUPPORTED_K:
            return
    if len(parts) == 2 and parts[0] == "overall" and parts[1] in _SCALAR_METRICS:
        return
    raise ValueError(f"지원하지 않는 metric 경로입니다: {path}")


def metric_value(row: dict[str, Any], path: str) -> float | None:
    """overall 선언 경로를 #143 per-case row 값으로 해석한다."""
    validate_metric_path(path)
    parts = path.split(".")
    name = parts[1]
    if name in _NESTED_METRICS:
        value = row["metrics"][name].get(parts[2])
    elif name == "mrr":
        value = row["metrics"]["mrr"]
    elif name == "filterAccuracy":
        value = row["filterAccuracy"]
    else:
        value = float(bool(row["hardConstraintViolated"]))
    return float(value) if isinstance(value, int | float) else None


def _metric_summaries(
    values: dict[str, list[float]],
    *,
    resamples: int,
    confidence: float,
    seed: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    case_summaries = {
        case_id: summarize_values(samples) for case_id, samples in sorted(values.items())
    }
    case_means = [
        float(summary["mean"])
        for summary in case_summaries.values()
        if isinstance(summary.get("mean"), int | float)
    ]
    overall = summarize_values(case_means)
    overall["bootstrapCi95"] = bootstrap_mean_ci(
        case_means,
        resamples=resamples,
        confidence=confidence,
        seed=seed,
    )
    return case_summaries, overall


def run_repeats(
    *,
    adapter: LiveBuyerAdapter,
    cases: list[GoldenCase],
    fixtures: EvaluationFixtures,
    repeats: int,
    budget: BudgetTracker,
    primary_metric: str = "overall.ndcgAtK.10",
    secondary_metrics: list[str] | None = None,
    resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 20260803,
) -> dict[str, Any]:
    validate_metric_path(primary_metric)
    resolved_secondary = list(secondary_metrics or [])
    for metric in resolved_secondary:
        validate_metric_path(metric)

    rows: list[dict[str, Any]] = []
    case_primary: dict[str, list[float]] = {}
    secondary_values: dict[str, dict[str, list[float]]] = {
        metric: {} for metric in resolved_secondary
    }
    excluded: dict[str, str] = {}
    budget_exceeded = False
    hard_constraint_count = 0
    evaluated_metric_rows = 0
    for repeat_index in range(repeats):
        for case in cases:
            if budget.exceeded_reason is not None:
                budget_exceeded = True
                break
            call_start = len(getattr(adapter.llm, "calls", []))
            try:
                report = evaluate(adapter=adapter, cases=[case], fixtures=fixtures)
            except BudgetExceeded:
                budget_exceeded = True
                rows.append(
                    {
                        "repeat": repeat_index,
                        "caseId": case.case_id,
                        "metrics": None,
                        "rankedProductIds": [],
                        "extractedFilters": {},
                        "modelConfig": dict(adapter.model_config),
                        "providerCalls": list(getattr(adapter.llm, "calls", [])[call_start:]),
                        "latencyMs": None,
                        "hardFailure": True,
                        "failureReason": "budgetExceeded",
                    }
                )
                break
            metric_row = report["cases"][0]
            output = dict(adapter.last_output)
            hard_constraint_count += int(bool(metric_row["hardConstraintViolated"]))
            evaluated_metric_rows += 1
            if metric_row["rankingExcluded"]:
                excluded[case.case_id] = str(metric_row["rankingExclusionReason"])
            elif not output.get("hardFailure"):
                primary = metric_value(metric_row, primary_metric)
                if primary is not None:
                    case_primary.setdefault(case.case_id, []).append(primary)
                for metric in resolved_secondary:
                    value = metric_value(metric_row, metric)
                    if value is not None:
                        secondary_values[metric].setdefault(case.case_id, []).append(value)
            rows.append(
                {
                    "repeat": repeat_index,
                    "caseId": case.case_id,
                    "metrics": metric_row,
                    **output,
                }
            )
        if budget_exceeded:
            break

    case_summaries, primary_summary = _metric_summaries(
        case_primary,
        resamples=resamples,
        confidence=confidence,
        seed=seed,
    )
    secondary_summaries: dict[str, Any] = {}
    for offset, metric in enumerate(resolved_secondary, 1):
        metric_cases, overall = _metric_summaries(
            secondary_values[metric],
            resamples=resamples,
            confidence=confidence,
            seed=seed + offset,
        )
        secondary_summaries[metric] = {
            "label": "exploratory",
            "caseMetrics": secondary_values[metric],
            "caseSummaries": metric_cases,
            "overallSummary": overall,
        }
    return {
        "repeatsRequested": repeats,
        "executedRepeats": len({row["repeat"] for row in rows}),
        "uniqueCaseCount": len(cases),
        "primaryMetric": primary_metric,
        "caseResults": rows,
        "casePrimaryMetrics": case_primary,
        "caseRepeatSummaries": case_summaries,
        "primarySummary": primary_summary,
        "secondaryMetricSummaries": secondary_summaries,
        "rankingExcludedCases": [
            {"caseId": case_id, "reason": reason} for case_id, reason in sorted(excluded.items())
        ],
        "hardFailureCount": sum(row.get("hardFailure") is True for row in rows),
        "hardConstraintViolationCount": hard_constraint_count,
        "hardConstraintViolationRate": (
            hard_constraint_count / evaluated_metric_rows if evaluated_metric_rows else 0.0
        ),
        "budgetExceeded": budget_exceeded or budget.snapshot()["budgetExceeded"],
        "budget": budget.snapshot(),
    }
