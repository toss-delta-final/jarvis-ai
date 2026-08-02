"""구매자 골든셋 manifest·누출 감사·커버리지 회귀 테스트."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import evals.goldenset.audit as goldenset_audit
from app.core.config import get_settings
from evals.goldenset.audit import dataset_hash, run_audit
from evals.goldenset.loader import _load_labeled_holdout_for_audit
from evals.goldenset.schema import GoldenCase

ROOT = Path("evals/goldenset")


def test_manifest_file_hashes_match_committed_files() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    for entry in manifest["files"]:
        payload = (ROOT / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["bytes"] == len(payload)
    assert manifest["datasetHash"] == dataset_hash(manifest["files"])


def test_committed_dataset_has_required_counts_and_slice_coverage() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["counts"]["total"] == 43
    assert manifest["counts"]["dev"] == 31
    assert manifest["counts"]["holdout"] == 12
    assert 30 <= manifest["counts"]["total"] <= 50
    assert all(count > 0 for count in manifest["counts"]["bySlice"]["dev"].values())
    required = {"search", "personalization", "repurchase", "category_mapping_failure", "failure"}
    assert required <= {
        name for name, count in manifest["counts"]["bySlice"]["holdout"].items() if count
    }
    assert manifest["counts"]["bySlice"]["dev"]["personalization_overreach"] >= 3


def test_committed_dataset_audit_has_zero_violations() -> None:
    report = run_audit(write=False)
    assert report["summary"]["violationCount"] == 0
    assert report["coverageLimitations"] == [
        "holdout cold_start slice 표본이 0건입니다",
        "holdout multi_constraint slice 표본이 0건입니다",
        "holdout personalization_overreach slice 표본이 0건입니다",
    ]


def _cases():
    return (
        _load_labeled_holdout_for_audit("dev"),
        _load_labeled_holdout_for_audit("holdout"),
    )


def _violation_kinds(dev, holdout) -> set[str]:
    return {
        item["kind"]
        for item in run_audit(dev_cases=dev, holdout_cases=holdout, write=False)["violations"]
    }


def test_audit_detects_query_near_duplicate_only_mutation() -> None:
    dev, holdout = _cases()
    holdout[0] = holdout[0].model_copy(update={"query": dev[0].query})
    assert _violation_kinds(dev, holdout) == {"queryNearDuplicate"}


def test_audit_detects_relevant_set_overlap_only_mutation() -> None:
    dev, holdout = _cases()
    holdout[0] = holdout[0].model_copy(
        update={"relevant_product_ids": list(dev[0].relevant_product_ids)}
    )
    assert _violation_kinds(dev, holdout) == {"relevantSetOverlap"}


def test_audit_detects_persona_overlap_only_mutation() -> None:
    dev, holdout = _cases()
    dev_member = next(case for case in dev if case.identity.persona_id)
    index = next(index for index, case in enumerate(holdout) if case.identity.persona_id)
    holdout[index] = holdout[index].model_copy(
        update={
            "identity": holdout[index].identity.model_copy(
                update={"persona_id": dev_member.identity.persona_id}
            )
        }
    )
    assert _violation_kinds(dev, holdout) == {"personaOverlap"}


def test_audit_detects_catalog_scenario_overlap_only_mutation() -> None:
    dev, holdout = _cases()
    holdout[0] = holdout[0].model_copy(update={"search_fixture_id": dev[0].search_fixture_id})
    assert _violation_kinds(dev, holdout) == {"catalogScenarioOverlap"}


def test_audit_detects_duplicate_case_id_only_mutation() -> None:
    dev, holdout = _cases()
    holdout[0] = holdout[0].model_copy(update={"case_id": dev[0].case_id})
    assert _violation_kinds(dev, holdout) == {"duplicateCaseId"}


def test_audit_detects_missing_dev_slice_only_mutation() -> None:
    dev, holdout = _cases()
    dev = [
        case.model_copy(update={"slices": [name for name in case.slices if name != "cold_start"]})
        for case in dev
    ]
    assert _violation_kinds(dev, holdout) == {"missingDevSlice"}


def test_holdout_ratio_config_changes_audit_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = get_settings().model_copy(update={"goldenset_holdout_ratio": 0.8})
    monkeypatch.setattr(goldenset_audit, "get_settings", lambda: config)
    report = run_audit(write=False)
    assert any(item["kind"] == "holdoutRatioDrift" for item in report["warnings"])


def test_audit_reports_exact_non_discriminative_ranking_cases() -> None:
    report = run_audit(write=False)
    warning = next(
        item for item in report["warnings"] if item["kind"] == "nonDiscriminativeRanking"
    )
    assert warning["cases"] == [
        {"caseId": "buy-srch-0001", "candidateCount": 1, "relevantCount": 1},
        {"caseId": "buy-srch-0003", "candidateCount": 2, "relevantCount": 2},
        {"caseId": "buy-gust-0001", "candidateCount": 1, "relevantCount": 1},
        {"caseId": "buy-cmap-0002", "candidateCount": 5, "relevantCount": 5},
        {"caseId": "buy-mult-0002", "candidateCount": 5, "relevantCount": 5},
        {"caseId": "buy-pers-0003", "candidateCount": 1, "relevantCount": 1},
        {"caseId": "buy-srch-0004", "candidateCount": 1, "relevantCount": 1},
        {"caseId": "buy-srch-0006", "candidateCount": 1, "relevantCount": 1},
    ]


def test_audit_removes_case_after_distractor_is_injected() -> None:
    responses = json.loads((ROOT / "fixtures" / "search_responses.json").read_text())
    responses["dev-001"]["productIds"].append(999_999_999)
    report = run_audit(search_responses=responses, write=False)
    warning = next(
        item for item in report["warnings"] if item["kind"] == "nonDiscriminativeRanking"
    )
    assert "buy-srch-0001" not in {item["caseId"] for item in warning["cases"]}


def test_audit_detects_combined_regression_for_backward_compatibility() -> None:
    dev = _load_labeled_holdout_for_audit("dev")
    holdout = _load_labeled_holdout_for_audit("holdout")
    duplicate = GoldenCase.model_validate(
        {
            **dev[0].model_dump(by_alias=True),
            "caseId": "buy-fail-9999",
            "split": "holdout",
        }
    )
    report = run_audit(dev_cases=dev, holdout_cases=[duplicate, *holdout], write=False)
    kinds = {item["kind"] for item in report["violations"]}
    assert {"queryNearDuplicate", "catalogScenarioOverlap"} <= kinds
