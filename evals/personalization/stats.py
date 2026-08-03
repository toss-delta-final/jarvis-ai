"""개인화 arm 간 paired delta 통계."""

from __future__ import annotations

import statistics
from typing import Any

from evals.model_eval.stats import bootstrap_mean_ci


def _rows(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = {row["caseId"]: row for row in report["cases"]}
    if len(rows) != len(report["cases"]):
        raise ValueError("paired report에 중복 caseId가 있습니다")
    return rows


def _validate_pair(
    current: dict[str, Any], baseline: dict[str, Any], k_list: tuple[int, ...]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if current.get("datasetHash") != baseline.get("datasetHash"):
        raise ValueError("paired report datasetHash가 다릅니다")
    if current.get("kList") != baseline.get("kList") or not set(k_list) <= set(
        current.get("kList", [])
    ):
        raise ValueError("paired report kList가 다릅니다")
    current_rows, baseline_rows = _rows(current), _rows(baseline)
    if set(current_rows) != set(baseline_rows):
        missing_current = sorted(set(baseline_rows) - set(current_rows))
        missing_baseline = sorted(set(current_rows) - set(baseline_rows))
        raise ValueError(
            f"paired report caseId가 다릅니다: missingCurrent={missing_current}, "
            f"missingBaseline={missing_baseline}"
        )
    for case_id in current_rows:
        if set(current_rows[case_id]["slices"]) != set(baseline_rows[case_id]["slices"]):
            raise ValueError(f"paired report slice가 다릅니다: {case_id}")
    return current_rows, baseline_rows


def paired_metric_deltas(
    current: dict[str, Any],
    baseline: dict[str, Any],
    *,
    k_list: tuple[int, ...],
    resamples: int,
    confidence: float,
    seed: int,
) -> dict[str, Any]:
    """동일 case의 ΔNDCG@K·Δdiversity를 지표별 분모로 전체/slice 집계한다."""
    current_rows, baseline_rows = _validate_pair(current, baseline, k_list)
    paired_ids = sorted(current_rows)
    slice_names = sorted(
        {slice_name for case_id in paired_ids for slice_name in current_rows[case_id]["slices"]}
    )

    def denominator(included: list[str], excluded: list[dict[str, str]], total: int) -> dict:
        return {
            "pairedCount": total,
            "includedCount": len(included),
            "includedCaseIds": included,
            "excluded": excluded,
        }

    def metric(
        values: list[float], offset: int, metric_denominator: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "deltas": values,
            "meanDelta": statistics.fmean(values) if values else None,
            "bootstrapCi95": bootstrap_mean_ci(
                values, resamples=resamples, confidence=confidence, seed=seed + offset
            ),
            "denominator": metric_denominator,
        }

    def summarize(case_ids: list[str]) -> dict[str, Any]:
        ranking_ids: list[str] = []
        ranking_excluded: list[dict[str, str]] = []
        for case_id in case_ids:
            left, right = current_rows[case_id], baseline_rows[case_id]
            if left["rankingExcluded"] or right["rankingExcluded"]:
                ranking_excluded.append(
                    {
                        "caseId": case_id,
                        "reason": str(
                            left["rankingExclusionReason"]
                            or right["rankingExclusionReason"]
                            or "rankingExcluded"
                        ),
                    }
                )
            else:
                ranking_ids.append(case_id)
        ranking_denominator = denominator(ranking_ids, ranking_excluded, len(case_ids))
        diversity_denominator = denominator(list(case_ids), [], len(case_ids))
        ndcg: dict[str, Any] = {}
        for offset, k in enumerate(k_list):
            key = str(k)
            eligible = [
                case_id
                for case_id in ranking_ids
                if current_rows[case_id]["metrics"]["ndcgAtK"][key] is not None
                and baseline_rows[case_id]["metrics"]["ndcgAtK"][key] is not None
            ]
            values = [
                float(current_rows[case_id]["metrics"]["ndcgAtK"][key])
                - float(baseline_rows[case_id]["metrics"]["ndcgAtK"][key])
                for case_id in eligible
            ]
            missing_metric = sorted(set(ranking_ids) - set(eligible))
            ndcg_denominator = (
                ranking_denominator
                if not missing_metric
                else denominator(
                    eligible,
                    [{"caseId": case_id, "reason": "metricMissing"} for case_id in missing_metric]
                    + ranking_excluded,
                    len(case_ids),
                )
            )
            ndcg[key] = metric(values, offset, ndcg_denominator)
        diversity_values = [
            float(current_rows[case_id]["diversity"]) - float(baseline_rows[case_id]["diversity"])
            for case_id in case_ids
        ]
        return {
            "ndcgAtK": ndcg,
            "diversity": metric(diversity_values, len(k_list), diversity_denominator),
        }

    return {
        "overall": summarize(paired_ids),
        "slices": {
            name: summarize(
                [case_id for case_id in paired_ids if name in current_rows[case_id]["slices"]]
            )
            for name in slice_names
        },
    }
