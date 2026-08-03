"""반복 분포와 caseId paired baseline 비교."""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from typing import Any

from scripts.aggregate_observability import percentile


def summarize_values(values: Sequence[float]) -> dict[str, float | int | None]:
    """N·mean·median·표본 SD·IQR을 계산한다."""
    numeric = [float(value) for value in values]
    if not numeric:
        return {"n": 0, "mean": None, "median": None, "sd": None, "iqr": None}
    q1 = percentile(numeric, 25)
    q3 = percentile(numeric, 75)
    return {
        "n": len(numeric),
        "mean": statistics.fmean(numeric),
        "median": statistics.median(numeric),
        "sd": statistics.stdev(numeric) if len(numeric) >= 2 else None,
        "iqr": q3 - q1 if q1 is not None and q3 is not None else None,
    }


def bootstrap_mean_ci(
    values: Sequence[float], *, resamples: int, confidence: float, seed: int
) -> dict[str, float | None]:
    """#151과 동일한 지역 고정 seed percentile bootstrap."""
    if len(values) < 2:
        return {"low": None, "high": None}
    rng = random.Random(seed)
    size = len(values)
    estimates = [
        statistics.fmean(values[rng.randrange(size)] for _ in range(size)) for _ in range(resamples)
    ]
    alpha = (1 - confidence) / 2
    return {
        "low": percentile(estimates, alpha * 100),
        "high": percentile(estimates, (1 - alpha) * 100),
    }


def _case_means(payload: dict[str, Any]) -> dict[str, float]:
    values = payload.get("casePrimaryMetrics", {})
    return {
        case_id: statistics.fmean(float(value) for value in samples)
        for case_id, samples in values.items()
        if samples
    }


def compare_baseline(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    margin: float,
    resamples: int,
    confidence: float,
    seed: int,
    multiplicity: str,
    missing_run_policy: str = "excludePairReportCount",
) -> dict[str, Any]:
    """공통 caseId의 primary metric 차이를 paired 비교한다."""
    if current.get("datasetHash") != baseline.get("datasetHash"):
        raise ValueError("baseline과 current의 datasetHash가 다릅니다")
    current_means = _case_means(current)
    baseline_means = _case_means(baseline)
    paired_ids = sorted(current_means.keys() & baseline_means.keys())
    deltas = [current_means[case_id] - baseline_means[case_id] for case_id in paired_ids]
    case_deltas = [
        {
            "caseId": case_id,
            "current": current_means[case_id],
            "baseline": baseline_means[case_id],
            "delta": current_means[case_id] - baseline_means[case_id],
        }
        for case_id in paired_ids
    ]
    ci = bootstrap_mean_ci(
        deltas,
        resamples=resamples,
        confidence=confidence,
        seed=seed,
    )
    low, high = ci["low"], ci["high"]
    if low is not None and low > -margin:
        verdict = "pass"
    elif high is not None and high < -margin:
        verdict = "regression"
    else:
        verdict = "inconclusive"
    current_hard_failures = int(current.get("hardFailureCount", 0))
    baseline_hard_failures = int(baseline.get("hardFailureCount", 0))
    if verdict == "pass" and current_hard_failures:
        verdict = "inconclusive"
    return {
        "pairedCaseIds": paired_ids,
        "pairedCount": len(paired_ids),
        "missingCurrentCount": len(baseline_means.keys() - current_means.keys()),
        "missingBaselineCount": len(current_means.keys() - baseline_means.keys()),
        "meanDelta": statistics.fmean(deltas) if deltas else None,
        "pairedSummary": summarize_values(deltas),
        "caseDeltas": case_deltas,
        "bootstrapCi95": ci,
        "margin": margin,
        "verdict": verdict,
        "currentHardFailureCount": current_hard_failures,
        "baselineHardFailureCount": baseline_hard_failures,
        "missingRunPolicy": missing_run_policy,
        "multiplicity": multiplicity,
    }
