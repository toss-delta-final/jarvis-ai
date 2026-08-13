"""Paired quality, stability, integrity, and efficiency metrics."""

from __future__ import annotations

import itertools
import statistics
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from scipy.stats import spearmanr

from evals.model_eval.stats import bootstrap_mean_ci
from evals.rerank_scoring.schema import RankingProbeRun, RankingSample
from scripts.aggregate_observability import percentile

_COMPARISONS = (
    ("currentToStructured", "current", "structured"),
    ("currentToHybrid", "current", "hybrid"),
    ("structuredToHybrid", "structured", "hybrid"),
)
_CODE_ASSISTED_COMPARISONS = (
    ("currentToCodeAssisted", "current", "code_assisted"),
    ("structuredToCodeAssisted", "structured", "code_assisted"),
)


def _primary_comparison(arms: Sequence[str]) -> str | None:
    available = set(arms)
    if {"current", "code_assisted"} <= available:
        return "currentToCodeAssisted"
    if {"current", "hybrid"} <= available:
        return "currentToHybrid"
    if {"current", "structured"} <= available:
        return "currentToStructured"
    if {"structured", "hybrid"} <= available:
        return "structuredToHybrid"
    return None


def _rate(numerator: int, denominator: int) -> dict[str, float | int | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def _validate_provenance(run: RankingProbeRun) -> None:
    dataset_hashes = {sample.dataset_hash for sample in run.samples if sample.dataset_hash}
    if len(dataset_hashes) > 1 or (dataset_hashes and run.dataset_hash not in dataset_hashes):
        raise ValueError("mixed dataset hashes cannot be compared")
    model_configs = {sample.model_config_json for sample in run.samples if sample.model_config_json}
    if len(model_configs) > 1:
        raise ValueError("mixed model configs cannot be compared")
    prompts_by_arm: dict[str, set[str]] = defaultdict(set)
    for sample in run.samples:
        if sample.prompt_hash:
            prompts_by_arm[sample.arm].add(sample.prompt_hash)
    if any(len(values) > 1 for values in prompts_by_arm.values()):
        raise ValueError("mixed prompt hashes within an arm cannot be compared")


def _sample_ndcg(sample: RankingSample) -> float | None:
    return sample.ndcg_at_10


def _case_means(run: RankingProbeRun, arm: str) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for sample in run.samples:
        value = _sample_ndcg(sample)
        if sample.arm == arm and value is not None:
            grouped[sample.case_id].append(float(value))
    return {case_id: statistics.fmean(values) for case_id, values in grouped.items() if values}


def _comparison(run: RankingProbeRun, baseline: str, current: str) -> dict[str, Any]:
    baseline_means = _case_means(run, baseline)
    current_means = _case_means(run, current)
    paired_ids = sorted(baseline_means.keys() & current_means.keys())
    deltas = [current_means[case_id] - baseline_means[case_id] for case_id in paired_ids]
    ci = bootstrap_mean_ci(deltas, resamples=2000, confidence=0.95, seed=20260813)
    low, high = ci["low"], ci["high"]
    if not deltas:
        verdict = "not-tested"
    elif any(
        sample.arm == current and sample.hard_constraint_violation_count for sample in run.samples
    ):
        verdict = "regressed"
    elif low is not None and low > 0:
        verdict = "supported"
    elif high is not None and high < 0:
        verdict = "regressed"
    else:
        verdict = "inconclusive"
    return {
        "baselineArm": baseline,
        "currentArm": current,
        "pairedCaseIds": paired_ids,
        "pairedCount": len(paired_ids),
        "missingCurrentCount": len(baseline_means.keys() - current_means.keys()),
        "missingBaselineCount": len(current_means.keys() - baseline_means.keys()),
        "caseDeltas": [
            {
                "caseId": case_id,
                "baseline": baseline_means[case_id],
                "current": current_means[case_id],
                "delta": current_means[case_id] - baseline_means[case_id],
            }
            for case_id in paired_ids
        ],
        "meanDelta": statistics.fmean(deltas) if deltas else None,
        "bootstrapCi95": ci,
        "verdict": verdict,
    }


def _spearman(left: Sequence[int], right: Sequence[int]) -> float | None:
    common = set(left) & set(right)
    if len(common) < 2:
        return None
    left_rank = {product_id: rank for rank, product_id in enumerate(left, 1)}
    right_rank = {product_id: rank for rank, product_id in enumerate(right, 1)}
    ordered = sorted(common)
    value = spearmanr(
        [left_rank[product_id] for product_id in ordered],
        [right_rank[product_id] for product_id in ordered],
    ).statistic
    return float(value)


def _stability(run: RankingProbeRun, arm: str) -> dict[str, Any]:
    by_case: dict[str, list[RankingSample]] = defaultdict(list)
    for sample in run.samples:
        if sample.arm == arm:
            by_case[sample.case_id].append(sample)
    jaccards: list[float] = []
    top1: list[float] = []
    correlations: list[float] = []
    pair_count = 0
    for samples in by_case.values():
        for left, right in itertools.combinations(samples, 2):
            pair_count += 1
            left_top = set(left.top3_product_ids)
            right_top = set(right.top3_product_ids)
            union = left_top | right_top
            jaccards.append(len(left_top & right_top) / len(union) if union else 1.0)
            top1.append(float(left.top1_product_id == right.top1_product_id))
            if (
                correlation := _spearman(left.ranked_product_ids, right.ranked_product_ids)
            ) is not None:
                correlations.append(correlation)
    return {
        "pairCount": pair_count,
        "top3Jaccard": statistics.fmean(jaccards) if jaccards else None,
        "top1Agreement": statistics.fmean(top1) if top1 else None,
        "spearman": statistics.fmean(correlations) if correlations else None,
        "spearmanPairCount": len(correlations),
        "spearmanUnavailableReason": (
            None if correlations else "fewer_than_two_common_ranked_ids_or_no_pairs"
        ),
    }


def _integrity(run: RankingProbeRun, arm: str) -> dict[str, Any]:
    samples = [sample for sample in run.samples if sample.arm == arm]
    full_fallback_failures = [
        failure for failure in run.failures if failure.arm == arm and failure.full_fallback
    ]
    denominator = len(samples)
    return {
        "sampleCount": denominator,
        "hardConstraintViolation": _rate(
            sum(sample.hard_constraint_violation_count > 0 for sample in samples), denominator
        ),
        "foreignEvaluation": _rate(
            sum(sample.foreign_evaluation_count > 0 for sample in samples), denominator
        ),
        "duplicateEvaluation": _rate(
            sum(sample.duplicate_evaluation_count > 0 for sample in samples), denominator
        ),
        "invalidScoreRows": {
            "count": sum(sample.invalid_score_count for sample in samples),
            "sampleDenominator": denominator,
        },
        "evaluatedCoverage": (
            statistics.fmean(sample.evaluated_coverage for sample in samples) if samples else None
        ),
        "partialFallback": _rate(sum(sample.partial_fallback for sample in samples), denominator),
        "fullFallback": _rate(
            sum(sample.full_fallback for sample in samples) + len(full_fallback_failures),
            denominator + len(full_fallback_failures),
        ),
    }


def _efficiency(run: RankingProbeRun, arm: str) -> dict[str, Any]:
    samples = [sample for sample in run.samples if sample.arm == arm]
    latency = [sample.latency_ms for sample in samples]
    known_usage = [
        sample
        for sample in samples
        if sample.input_tokens is not None
        and sample.output_tokens is not None
        and sample.cost_usd is not None
    ]
    unknown_reasons = sorted(
        {
            sample.usage_unknown_reason or "provider usage unavailable"
            for sample in samples
            if sample not in known_usage
        }
    )
    return {
        "sampleCount": len(samples),
        "latencyMs": {
            "p50": percentile(latency, 50) if latency else None,
            "p95": percentile(latency, 95) if latency else None,
        },
        "inputTokens": sum(sample.input_tokens or 0 for sample in known_usage),
        "outputTokens": sum(sample.output_tokens or 0 for sample in known_usage),
        "costUsd": sum(sample.cost_usd or 0.0 for sample in known_usage),
        "knownUsageCount": len(known_usage),
        "unknownUsageCount": len(samples) - len(known_usage),
        "unknownReason": "; ".join(unknown_reasons) if unknown_reasons else None,
    }


def score_run(run: RankingProbeRun) -> dict[str, Any]:
    """Score only successful samples; failures remain separate attempt evidence."""

    _validate_provenance(run)
    comparison_specs = _COMPARISONS + (
        _CODE_ASSISTED_COMPARISONS if "code_assisted" in run.arms else ()
    )
    comparisons = {
        name: _comparison(run, baseline, current) for name, baseline, current in comparison_specs
    }
    primary = _primary_comparison(run.arms)
    return {
        "status": comparisons[primary]["verdict"] if primary is not None else "not-tested",
        "primaryComparison": primary,
        "datasetVersion": run.dataset_version,
        "datasetHash": run.dataset_hash,
        "arms": list(run.arms),
        "repeats": run.repeats,
        "orderSeeds": list(run.order_seeds),
        "comparisons": comparisons,
        "stability": {arm: _stability(run, arm) for arm in run.arms},
        "integrity": {arm: _integrity(run, arm) for arm in run.arms},
        "efficiency": {arm: _efficiency(run, arm) for arm in run.arms},
        "failureCount": len(run.failures),
        "sampleCount": len(run.samples),
    }
