"""평가 artifact와 run manifest의 결정론 테스트."""

from __future__ import annotations

import json

from evals.metrics.report import normalize_artifacts, write_artifacts
from evals.metrics.run_manifest import build_run_manifest
from evals.metrics.cli import main


def _filter_axes_aggregate() -> dict:
    return {
        "category": {
            "counts": {
                "match": 0,
                "valueMismatch": 0,
                "spurious": 0,
                "missing": 0,
                "bothEmpty": 1,
            },
            "support": 0,
            "valueStrict": {"precision": None, "recall": None, "f1": None},
            "presence": {"precision": None, "recall": None, "f1": None},
            "definitions": {
                "support": "match + valueMismatch + missing",
                "valueStrict.precision": "match / (match + valueMismatch + spurious)",
                "valueStrict.recall": "match / (match + valueMismatch + missing)",
                "presence.precision": "(match + valueMismatch) / (match + valueMismatch + spurious)",
                "presence.recall": "(match + valueMismatch) / (match + valueMismatch + missing)",
            },
        }
    }


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
        "ndcgAtK": {"3": None, "5": None, "10": None},
        "microPrecisionAtK": {"5": 0.0},
        "microRecallAtK": {"5": 0.0},
        "filterAccuracy": 1.0,
        "filterAxes": _filter_axes_aggregate(),
        "hardConstraintViolationRate": 1.0,
        "coverage": 0.5,
        "diversity": 0.5,
        "duplicateCount": 0,
        "duplicateCaseIds": [],
        "unknownProductIds": [],
        "confirmatoryLabel": "exploratory",
        "candidateDepth": {
            "min": None,
            "median": None,
            "max": None,
            "shallowCount": 0,
            "shallowRatio": 0.0,
        },
    }
    noop_aggregate = {**aggregate, "hardConstraintViolationRate": 0.0}
    return {
        "datasetVersion": "2.0.0",
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
        "ndcgKList": [3, 5, 10],
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
                    "ndcgAtK": {"3": None, "5": None, "10": None},
                },
                "filterAccuracy": 1.0,
                "filterAxes": {"category": "bothEmpty"},
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
        "filterAxesSpec": {
            "version": "filter-axes-v1",
            "sha256": "b" * 64,
            "emptyAxisRule": "bothAbsentExcluded",
        },
        "noopBaseline": {
            "cases": [],
            "slices": {"failure": noop_aggregate},
            "overall": noop_aggregate,
        },
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
    assert (first / "filter_axes.csv").read_bytes().startswith(b"scope,caseId,axis,outcome,")
    markdown = (first / "report.md").read_text(encoding="utf-8")
    assert "buy-fail-0001" in markdown
    assert "priceMax, priceMin" in markdown
    assert "mustExclude" in markdown
    assert "Filter Accuracy 1.0" in markdown
    assert "축별 필터 지표(#334)" in markdown
    assert "filter-axes-v1" in markdown
    assert "valueStrict F1" in markdown
    assert "| axis | support | spurious | missing |" in markdown


def test_filter_axes_csv_case_rows_carry_real_precision_recall_f1_numbers(tmp_path) -> None:
    """리뷰 F2 — 케이스 행이 outcome 문자열뿐이면 CSV만으로 케이스 수준 과·소추출 집계가
    안 된다. match/missing 각각 실제 0.0/1.0/None 수치가 나오는지 손계산과 대조한다."""
    import csv

    report = _report()
    report["cases"][0]["filterAxes"] = {"category": "match", "keyword": "missing"}
    manifest = {
        "run": {"runId": "x", "timestamp": "t", "command": "c"},
        "commitSha": "a" * 40,
        "dirty": False,
    }
    output = tmp_path / "case-rows"
    write_artifacts(output, report, manifest)

    with (output / "filter_axes.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    case_rows = {row["axis"]: row for row in rows if row["scope"] == "case"}

    match_row = case_rows["category"]
    assert match_row["caseId"] == "buy-fail-0001"
    assert match_row["outcome"] == "match"
    assert match_row["matchCount"] == "1"
    assert match_row["support"] == "1"
    assert match_row["valueStrictPrecision"] == "1.0"
    assert match_row["valueStrictRecall"] == "1.0"
    assert match_row["valueStrictF1"] == "1.0"
    assert match_row["presencePrecision"] == "1.0"
    assert match_row["presenceRecall"] == "1.0"

    missing_row = case_rows["keyword"]
    assert missing_row["outcome"] == "missing"
    assert missing_row["missingCount"] == "1"
    assert missing_row["support"] == "1"
    # 분모(match+mismatch+spurious)가 0이면 precision은 0.0이 아니라 빈 칸(None)이어야 한다.
    assert missing_row["valueStrictPrecision"] == ""
    assert missing_row["valueStrictRecall"] == "0.0"
    assert missing_row["presencePrecision"] == ""
    assert missing_row["presenceRecall"] == "0.0"


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
        "filter_axes.csv",
        "filter_axes_spec.json",
        "report.md",
        "results.json",
        "run_manifest.json",
        "violations.csv",
    }


def test_filter_axes_spec_json_is_embedded_and_hash_matches_results(tmp_path) -> None:
    """리뷰 F3 — checkout 없이 hash만으로는 당시 정규화 규칙을 복원할 수 없었다. 실 CLI 경로로
    돌려 embedded `filter_axes_spec.json`의 SHA-256이 `results.json`의
    `filterAxesSpec.sha256`과 실제로 일치하는지 확인한다."""
    import hashlib
    import json as json_module

    output = tmp_path / "artifacts"
    assert main(["--out", str(output)]) == 0

    spec_bytes = (output / "filter_axes_spec.json").read_bytes()
    results = json_module.loads((output / "results.json").read_text(encoding="utf-8"))

    assert hashlib.sha256(spec_bytes).hexdigest() == results["filterAxesSpec"]["sha256"]
    spec = json_module.loads(spec_bytes)
    assert spec["version"] == results["filterAxesSpec"]["version"]
    assert "axes" in spec  # 정규화 규칙 자체(fields/normalization/reason)가 동봉된다.


def test_cli_artifacts_are_normalized_identically_across_output_paths(tmp_path) -> None:
    first = tmp_path / "eval-run1"
    second = tmp_path / "eval-run2"

    assert main(["--out", str(first)]) == 0
    assert main(["--out", str(second)]) == 0

    assert normalize_artifacts(first) == normalize_artifacts(second)
