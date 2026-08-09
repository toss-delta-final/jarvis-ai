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
from evals.goldenset.refresh_manifest import HASH_EXCLUDED_PATHS
from evals.goldenset.schema import GoldenCase

ROOT = Path("evals/goldenset")
EVALS_ROOT = Path("evals")


def test_manifest_file_hashes_match_committed_files() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    for entry in manifest["files"]:
        payload = (ROOT / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
        assert entry["bytes"] == len(payload)
    assert manifest["datasetHash"] == dataset_hash(manifest["files"])


def test_manifest_covers_every_non_runtime_goldenset_file() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    actual = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.is_file() and path.name != "manifest.json" and "__pycache__" not in path.parts
    }
    assert actual - HASH_EXCLUDED_PATHS == {entry["path"] for entry in manifest["files"]}


def _nested_dataset_references(value: object):
    if isinstance(value, dict):
        if {"datasetVersion", "datasetHash"} <= value.keys():
            yield value["datasetVersion"], value["datasetHash"]
        for nested in value.values():
            yield from _nested_dataset_references(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _nested_dataset_references(nested)


def test_current_goldenset_baselines_reference_current_manifest_hash() -> None:
    """현행 datasetVersion baseline은 manifest와 같은 datasetHash를 가리켜야 한다."""
    manifest = json.loads((ROOT / "manifest.json").read_text())
    current_version = manifest["datasetVersion"]
    current_hash = manifest["datasetHash"]
    checked: list[tuple[Path, str]] = []

    for path in EVALS_ROOT.glob("**/baselines/**/*.json"):
        for version, recorded_hash in _nested_dataset_references(json.loads(path.read_text())):
            if version == current_version:
                checked.append((path, recorded_hash))

    assert checked, f"no baseline claims current datasetVersion {current_version}"
    assert not [
        f"{path}: datasetVersion={current_version} datasetHash={recorded_hash} "
        f"(manifest={current_hash})"
        for path, recorded_hash in checked
        if recorded_hash != current_hash
    ]


def test_committed_dataset_has_required_counts_and_slice_coverage() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["counts"]["total"] == 133
    assert manifest["counts"]["dev"] == 109
    assert manifest["counts"]["holdout"] == 24
    assert 30 <= manifest["counts"]["total"] <= 160
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


def _non_discriminative_case(template: GoldenCase, case_id: str, fixture_id: str) -> GoldenCase:
    # v2 데이터는 실제 하드 네거티브가 섞여 있어 후보=정답인 케이스가 더 이상 존재하지
    # 않는다(#333 Part 2 라이브 캠페인의 의도된 결과) — 그래서 이 경고 자체는 합성
    # 케이스로 독립 검증한다(백워드 호환 테스트와 동일 패턴).
    return GoldenCase.model_validate(
        {
            **template.model_dump(by_alias=True),
            "caseId": case_id,
            "searchFixtureId": fixture_id,
            "relevantProductIds": [111, 222],
            "relevanceGrades": {"111": 2, "222": 2},
            "idealOrder": [111, 222],
        }
    )


def test_audit_reports_exact_non_discriminative_ranking_cases() -> None:
    dev = _load_labeled_holdout_for_audit("dev")
    non_discriminative = _non_discriminative_case(dev[0], "buy-test-9001", "dev-audit-test-9001")
    responses = json.loads((ROOT / "fixtures" / "search_responses.json").read_text())
    responses["dev-audit-test-9001"] = {
        "request": {},
        "productIds": [111, 222],
        "totalCount": 2,
        "recordedAt": "2026-08-05T00:00:00+09:00",
        "source": "live-spring-i1",
    }
    report = run_audit(
        dev_cases=[*dev, non_discriminative], search_responses=responses, write=False
    )
    warning = next(
        item for item in report["warnings"] if item["kind"] == "nonDiscriminativeRanking"
    )
    assert {"caseId": "buy-test-9001", "candidateCount": 2, "relevantCount": 2} in warning["cases"]


def test_audit_removes_case_after_distractor_is_injected() -> None:
    dev = _load_labeled_holdout_for_audit("dev")
    non_discriminative = _non_discriminative_case(dev[0], "buy-test-9002", "dev-audit-test-9002")
    responses = json.loads((ROOT / "fixtures" / "search_responses.json").read_text())
    responses["dev-audit-test-9002"] = {
        "request": {},
        "productIds": [111, 222],
        "totalCount": 2,
        "recordedAt": "2026-08-05T00:00:00+09:00",
        "source": "live-spring-i1",
    }
    before = run_audit(
        dev_cases=[*dev, non_discriminative], search_responses=responses, write=False
    )
    before_warning = next(
        item for item in before["warnings"] if item["kind"] == "nonDiscriminativeRanking"
    )
    assert "buy-test-9002" in {item["caseId"] for item in before_warning["cases"]}

    responses["dev-audit-test-9002"]["productIds"].append(999_999_999)
    after = run_audit(dev_cases=[*dev, non_discriminative], search_responses=responses, write=False)
    after_cases = {
        item["caseId"]
        for warning in after["warnings"]
        if warning["kind"] == "nonDiscriminativeRanking"
        for item in warning["cases"]
    }
    assert "buy-test-9002" not in after_cases


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
