"""3-arm case-level primary metric paired 비교."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from typing import Any

from evals.model_eval.stats import bootstrap_mean_ci, summarize_values


def _case_samples(result: dict[str, Any]) -> dict[str, list[float]]:
    return {
        case_id: [float(value) for value in values]
        for case_id, values in result.get("casePrimaryMetrics", {}).items()
        if values
    }


def paired_comparisons(
    arms: dict[str, dict[str, Any]],
    *,
    all_case_ids: Sequence[str],
    pairs: Sequence[tuple[str, str]],
    resamples: int,
    confidence: float,
    seed: int,
) -> dict[str, dict[str, Any]]:
    """arm 쌍의 앞 arm−뒤 arm case 평균 Δ와 bootstrap CI를 계산한다."""
    comparisons: dict[str, dict[str, Any]] = {}
    for offset, (left, right) in enumerate(pairs):
        left_samples = _case_samples(arms[left])
        right_samples = _case_samples(arms[right])
        left_means = {
            case_id: statistics.fmean(values) for case_id, values in left_samples.items()
        }
        right_means = {
            case_id: statistics.fmean(values) for case_id, values in right_samples.items()
        }
        paired_ids = sorted(left_means.keys() & right_means.keys())
        rows = [
            {
                "caseId": case_id,
                "delta": left_means[case_id] - right_means[case_id],
                "nLeft": len(left_samples[case_id]),
                "nRight": len(right_samples[case_id]),
            }
            for case_id in paired_ids
        ]
        excluded = []
        for case_id in sorted(set(all_case_ids) - set(paired_ids)):
            n_left = len(left_samples.get(case_id, []))
            n_right = len(right_samples.get(case_id, []))
            if n_left and not n_right:
                reason = "missingRight"
            elif n_right and not n_left:
                reason = "missingLeft"
            else:
                reason = "missingBoth"
            excluded.append(
                {
                    "caseId": case_id,
                    "reason": reason,
                    "nLeft": n_left,
                    "nRight": n_right,
                }
            )
        deltas = [float(row["delta"]) for row in rows]
        ci = bootstrap_mean_ci(
            deltas,
            resamples=resamples,
            confidence=confidence,
            seed=seed + offset,
        )
        low, high = ci["low"], ci["high"]
        if low is not None and low > 0:
            verdict = f"{left}Wins"
        elif high is not None and high < 0:
            verdict = f"{right}Wins"
        else:
            verdict = "inconclusive"
        comparisons[f"{left}-{right}"] = {
            "leftArm": left,
            "rightArm": right,
            "pairedCaseIds": paired_ids,
            "pairedCount": len(paired_ids),
            "missingLeftCount": len(right_means.keys() - left_means.keys()),
            "missingRightCount": len(left_means.keys() - right_means.keys()),
            "excludedCaseIds": excluded,
            "caseDeltas": rows,
            "meanDelta": statistics.fmean(deltas) if deltas else None,
            "summary": summarize_values(deltas),
            "bootstrapCi95": ci,
            "verdict": verdict,
            "label": "confirmatory",
        }
    return comparisons
