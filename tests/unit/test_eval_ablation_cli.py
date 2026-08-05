from __future__ import annotations

from pathlib import Path
import json
import shutil

import pytest

from evals.ablation.cli import _comparison, main
from evals.ablation.report import normalized_artifact_bytes
from evals.model_eval.pricing import PriceBook


def test_dry_run_writes_three_arms_and_is_deterministic_across_output_paths(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    args = ["--dry-run", "--case-limit", "2", "--repeats", "1"]
    assert main(["--out", str(first), *args]) == 0
    assert main(["--out", str(second), *args]) == 0

    for arm in ("pipeline", "scoring", "single_call"):
        for filename in ("results.json", "cases.csv", "calls.csv"):
            assert (first / arm / filename).is_file()
    for filename in ("comparison.json", "report.md", "run_manifest.json"):
        assert (first / filename).is_file()
    for arm in ("pipeline", "single_call"):
        results = json.loads((first / arm / "results.json").read_text(encoding="utf-8"))
        assert results["coverage"]["tokenCoverage"] == 1.0
        assert results["coverage"]["costCoverage"] == 1.0
    assert normalized_artifact_bytes(first) == normalized_artifact_bytes(second)


def test_ablation_never_overwrites_existing_directory(tmp_path: Path) -> None:
    out = tmp_path / "exists"
    out.mkdir()
    assert main(["--out", str(out), "--dry-run", "--case-limit", "1"]) == 2


def test_case_ids_selection_is_always_case_id_ascending() -> None:
    from argparse import Namespace

    from evals.ablation.cli import _select_cases
    from evals.goldenset.loader import load_cases

    cases = sorted(load_cases("dev"), key=lambda case: case.case_id)[:2]
    args = Namespace(case_ids=f"{cases[1].case_id},{cases[0].case_id}", case_limit=None)
    selected = _select_cases(cases, args, test_type_filter=None)
    assert [case.case_id for case in selected] == [case.case_id for case in cases]


def _result(
    *,
    case_primary: dict[str, list[float]],
    slices: dict[str, list[str]],
    failure_reason: str | None = None,
) -> dict[str, object]:
    rows = [
        {
            "caseId": case_id,
            "repeat": index,
            "metrics": {"slices": case_slices},
            "providerCalls": [],
            "latencyMs": 1,
            "failureReason": failure_reason if case_id == "failed" else None,
            "filterParseWarnings": (
                [{"field": "brand", "reason": "invalidTypeOrValue"}]
                if case_id == "failed"
                else []
            ),
        }
        for index, (case_id, case_slices) in enumerate(slices.items())
    ]
    return {
        "casePrimaryMetrics": case_primary,
        "caseResults": rows,
        "primarySummary": {"mean": 0.5},
        "secondaryMetricSummaries": {
            "overall.precisionAtK.10": {"overallSummary": {"mean": 0.4, "n": 2}},
            "overall.recallAtK.10": {"overallSummary": {"mean": 0.6, "n": 2}},
            "overall.mrr": {"overallSummary": {"mean": 0.7, "n": 2}},
            "overall.filterAccuracy": {"overallSummary": {"mean": 0.8, "n": 2}},
        },
        "hardFailureCount": int(failure_reason is not None),
        "hardConstraintViolationRate": 0.25,
        "rankingExcludedCases": [{"caseId": "excluded", "reason": "nonDiscriminativeRanking"}],
    }


def test_comparison_surfaces_quality_safety_slices_and_failures() -> None:
    results = {
        "pipeline": _result(
            case_primary={"a": [0.4, 0.6], "failed": [0.2]},
            slices={"a": ["simple"], "failed": ["failure"]},
            failure_reason="providerCallFailed",
        ),
        "scoring": _result(
            case_primary={"a": [0.5], "failed": [0.3]},
            slices={"a": ["simple"], "failed": ["failure"]},
        ),
        "single_call": _result(
            case_primary={"a": [0.6], "failed": [0.4]},
            slices={"a": ["simple"], "failed": ["failure"]},
        ),
    }
    comparison = _comparison(
        results,
        {
            "primaryMetric": "overall.ndcgAtK.10",
            "bootstrap": {"resamples": 20, "confidence": 0.95},
            "seed": 7,
        },
        PriceBook(version="test", entries={}),
        all_case_ids=["a", "failed", "excluded"],
    )
    pipeline = comparison["arms"]["pipeline"]
    assert pipeline["secondaryMetrics"]["overall.precisionAtK.10"] == {
        "mean": 0.4,
        "n": 2,
        "label": "exploratory",
    }
    assert pipeline["hardFailureCount"] == 1
    assert pipeline["filterParseWarningCount"] == 1
    assert pipeline["hardConstraintViolationRate"] == 0.25
    assert pipeline["rankingExcludedCaseCount"] == 1
    assert pipeline["slices"]["simple"] == {
        "primaryMean": pytest.approx(0.5),
        "n": 1,
        "label": "exploratory",
    }
    assert pipeline["failureCases"] == [
        {"caseId": "failed", "failureReason": "providerCallFailed"}
    ]


def test_normalization_scrubs_dirty_for_repo_internal_vs_tmp_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    internal = Path(".test-ablation-internal")
    external = tmp_path / "external"

    def manifest(**kwargs):
        return {
            "run": {"command": kwargs["command"]},
            "dirty": str(internal) in kwargs["command"],
        }

    monkeypatch.setattr("evals.ablation.cli.build_model_eval_manifest", manifest)
    try:
        assert main(["--out", str(internal), "--dry-run", "--case-limit", "1", "--repeats", "1"]) == 0
        assert main(["--out", str(external), "--dry-run", "--case-limit", "1", "--repeats", "1"]) == 0
        assert normalized_artifact_bytes(internal) == normalized_artifact_bytes(external)
    finally:
        if internal.exists():
            shutil.rmtree(internal)


def test_main_returns_four_when_an_llm_arm_has_no_primary_samples(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = {
        "pipeline": {"casePrimaryMetrics": {}, "budgetExceeded": False},
        "scoring": {"casePrimaryMetrics": {"a": [1.0]}, "budgetExceeded": False},
        "single_call": {"casePrimaryMetrics": {"a": [1.0]}, "budgetExceeded": False},
    }
    monkeypatch.setattr("evals.ablation.cli._run_arms", lambda **kwargs: empty)
    monkeypatch.setattr("evals.ablation.cli.write_artifacts", lambda *args, **kwargs: None)
    monkeypatch.setattr("evals.ablation.cli._comparison", lambda *args, **kwargs: {})
    monkeypatch.setattr("evals.ablation.cli.build_model_eval_manifest", lambda **kwargs: {})
    monkeypatch.setattr("evals.ablation.cli.write_top_level_artifacts", lambda *args, **kwargs: None)

    assert (
        main(
            [
                "--out",
                str(tmp_path / "empty"),
                "--dry-run",
                "--case-limit",
                "1",
                "--repeats",
                "1",
            ]
        )
        == 4
    )
