"""평가 artifact와 run manifest의 결정론 테스트."""

from __future__ import annotations

import json

from evals.metrics.report import normalize_artifacts, write_artifacts
from evals.metrics.run_manifest import build_run_manifest
from evals.metrics.cli import main


def _report() -> dict:
    aggregate = {
        "caseCount": 1,
        "rankingCaseCount": 0,
        "rankingExcludedCount": 1,
        "rankingExcludedCaseIds": ["buy-fail-0001"],
        "ndcgCaseCount": 0,
        "precisionAtK": {"5": 0.0},
        "recallAtK": {"5": 0.0},
        "mrr": 0.0,
        "ndcgAtK": {"5": None},
        "microPrecisionAtK": {"5": 0.0},
        "microRecallAtK": {"5": 0.0},
        "filterAccuracy": 1.0,
        "hardConstraintViolationRate": 1.0,
        "coverage": 0.5,
        "diversity": 0.5,
        "duplicateCount": 0,
        "duplicateCaseIds": [],
        "unknownProductIds": [],
    }
    return {
        "datasetVersion": "1.0.0",
        "datasetHash": "abc",
        "algorithmVersion": "buyer-metrics-v1",
        "configVersion": "buyer-eval-config-v1",
        "modelConfig": {
            "provider": "scripted",
            "decompose": "expectedFilters",
            "rerank": "searchOrderPassthrough",
        },
        "prGateConstraints": ["priceMax", "priceMin"],
        "kList": [5],
        "cases": [
            {
                "caseId": "buy-fail-0001",
                "slices": ["failure"],
                "rankedProductIds": [2],
                "extractedFilters": {},
                "rankingExcluded": True,
                "rankingExclusionReason": "emptyRelevance",
                "metrics": {
                    "precisionAtK": {"5": 0.0},
                    "recallAtK": {"5": 0.0},
                    "mrr": 0.0,
                    "ndcgAtK": {"5": None},
                },
                "filterAccuracy": 1.0,
                "hardConstraintViolated": True,
                "violations": [{"productId": 2, "constraint": "mustExclude"}],
                "diversity": 0.5,
                "duplicateCount": 0,
                "unknownProductIds": [],
                "relevantProductIds": [],
                "eligibleProductIds": [1, 2],
            }
        ],
        "slices": {"failure": aggregate},
        "overall": aggregate,
        "violations": [{"caseId": "buy-fail-0001", "productId": 2, "constraint": "mustExclude"}],
    }


def test_artifacts_are_byte_identical_after_runtime_normalization(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    report = _report()
    manifest = {
        "run": {
            "runId": "one",
            "timestamp": "2026-08-03T00:00:00Z",
            "command": "uv run python -m evals.metrics --out /tmp/first",
        },
        "commitSha": "a" * 40,
        "dirty": True,
    }
    write_artifacts(first, report, manifest)
    manifest["run"] = {
        "runId": "two",
        "timestamp": "2026-08-03T01:00:00Z",
        "command": "uv run python -m evals.metrics --out /tmp/second",
    }
    write_artifacts(second, report, manifest)

    assert normalize_artifacts(first) == normalize_artifacts(second)
    assert (first / "cases.csv").read_bytes().startswith(b"caseId,")
    assert (first / "violations.csv").read_bytes().startswith(b"caseId,productId,constraint")
    markdown = (first / "report.md").read_text(encoding="utf-8")
    assert "buy-fail-0001" in markdown
    assert "priceMax, priceMin" in markdown
    assert "mustExclude" in markdown
    assert "Filter Accuracy 1.0" in markdown


def test_run_manifest_contains_required_environment_and_hashes() -> None:
    manifest = build_run_manifest(
        command="uv run python -m evals.metrics --out /tmp/out",
        seed=20260803,
    )

    assert len(manifest["commitSha"]) == 40
    assert isinstance(manifest["dirty"], bool)
    assert manifest["seed"] == 20260803
    assert manifest["run"]["command"].startswith("uv run python")
    assert "command" not in manifest
    assert manifest["pythonVersion"]
    assert manifest["platform"]
    assert manifest["image"] is None
    assert set(manifest["hashes"]) == {
        "uvLock",
        "datasetManifest",
        "fixtures",
        "prompts",
        "config",
    }
    assert all(
        len(value) == 64
        for key, value in manifest["hashes"].items()
        if key not in {"fixtures", "prompts"}
    )
    json.dumps(manifest, ensure_ascii=False, sort_keys=True)


def test_cli_writes_full_dev_artifacts_to_explicit_directory(tmp_path) -> None:
    output = tmp_path / "artifacts"

    assert main(["--out", str(output)]) == 0

    assert {path.name for path in output.iterdir()} == {
        "aggregates.csv",
        "cases.csv",
        "failures.csv",
        "report.md",
        "results.json",
        "run_manifest.json",
        "violations.csv",
    }


def test_cli_artifacts_are_normalized_identically_across_output_paths(tmp_path) -> None:
    first = tmp_path / "eval-run1"
    second = tmp_path / "eval-run2"

    assert main(["--out", str(first)]) == 0
    assert main(["--out", str(second)]) == 0

    assert normalize_artifacts(first) == normalize_artifacts(second)
