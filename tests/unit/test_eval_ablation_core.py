from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.ablation.budget import preflight_ablation
from evals.ablation.config import validate_ablation_config
from evals.ablation.stats import paired_comparisons
from evals.model_eval.budget import BudgetLimits


def _config() -> dict[str, object]:
    return json.loads(Path("evals/ablation/ablation_config.json").read_text(encoding="utf-8"))


def test_ablation_config_rejects_unsupported_preregistration_values() -> None:
    config = _config()
    validate_ablation_config(config)
    invalid = [
        {**config, "arms": ["pipeline", "scoring"]},
        {**config, "caseOrder": "random"},
        {**config, "configVersion": "ablation-config-v1"},
        {**config, "missingRunPolicy": "zeroFill"},
        {**config, "primaryMetric": "overall.mrr"},
        {**config, "rankingExclusionPolicy": "includeAll"},
        {**config, "repeats": 4},
        {**config, "secondaryMetrics": list(reversed(config["secondaryMetrics"]))},
        {
            **config,
            "secondaryMetrics": [
                metric
                for metric in config["secondaryMetrics"]
                if metric != "overall.precisionAtK.10"
            ],
        },
        {**config, "inconclusiveRule": "pickMeanWinner"},
        {**config, "split": "holdout"},
    ]
    for candidate in invalid:
        with pytest.raises(ValueError):
            validate_ablation_config(candidate)


def test_ablation_preflight_sums_pipeline_and_single_call_arms() -> None:
    allowed = preflight_ablation(
        case_count=2,
        repeats=3,
        limits=BudgetLimits(max_calls=24, max_total_tokens=1000, max_cost_usd=1),
    )
    assert allowed["pipelineExpectedCalls"] == 18
    assert allowed["singleCallExpectedCalls"] == 6
    assert allowed["expectedCalls"] == 24
    assert allowed["allowed"] is True

    rejected = preflight_ablation(
        case_count=2,
        repeats=3,
        limits=BudgetLimits(max_calls=23, max_total_tokens=1000, max_cost_usd=1),
    )
    assert rejected["allowed"] is False


def test_paired_primary_deltas_and_zero_inclusive_ci_are_inconclusive() -> None:
    arms = {
        "pipeline": {"casePrimaryMetrics": {"a": [0.8], "b": [0.4]}},
        "scoring": {"casePrimaryMetrics": {"a": [0.5], "b": [0.5]}},
        "single_call": {"casePrimaryMetrics": {"a": [0.6], "b": [0.6]}},
    }
    compared = paired_comparisons(
        arms,
        all_case_ids=("a", "b"),
        pairs=(("pipeline", "scoring"), ("pipeline", "single_call"), ("single_call", "scoring")),
        resamples=2000,
        confidence=0.95,
        seed=20260803,
    )
    first = compared["pipeline-scoring"]
    assert first["caseDeltas"] == [
        {"caseId": "a", "delta": pytest.approx(0.3), "nLeft": 1, "nRight": 1},
        {"caseId": "b", "delta": pytest.approx(-0.1), "nLeft": 1, "nRight": 1},
    ]
    assert first["meanDelta"] == pytest.approx(0.1)
    assert first["verdict"] == "inconclusive"
    assert first["label"] == "confirmatory"


def test_paired_comparison_reports_one_sided_both_missing_and_partial_samples() -> None:
    arms = {
        "left": {"casePrimaryMetrics": {"a": [0.8, 1.0], "b": [0.4]}},
        "right": {"casePrimaryMetrics": {"a": [0.5], "c": [0.3]}},
    }
    result = paired_comparisons(
        arms,
        all_case_ids=("a", "b", "c", "d"),
        pairs=(("left", "right"),),
        resamples=20,
        confidence=0.95,
        seed=7,
    )["left-right"]

    assert result["caseDeltas"] == [
        {"caseId": "a", "delta": pytest.approx(0.4), "nLeft": 2, "nRight": 1}
    ]
    assert result["excludedCaseIds"] == [
        {"caseId": "b", "reason": "missingRight", "nLeft": 1, "nRight": 0},
        {"caseId": "c", "reason": "missingLeft", "nLeft": 0, "nRight": 1},
        {"caseId": "d", "reason": "missingBoth", "nLeft": 0, "nRight": 0},
    ]
