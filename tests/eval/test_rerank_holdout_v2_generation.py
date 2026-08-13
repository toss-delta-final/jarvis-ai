from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from evals.goldenset.loader import ROOT as GOLDENSET_ROOT
from evals.rerank_holdout_v2.cli import GENERATED_FILES, main
from evals.rerank_holdout_v2.generator import generate_bundle, serialize_bundle
from evals.rerank_holdout_v2.io import ROOT, load_dataset, sha256_file

CATALOG_PATH = GOLDENSET_ROOT / "fixtures/catalog_snapshot.json"


@pytest.fixture(scope="module")
def catalog() -> dict[str, dict[str, object]]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bundle(catalog):
    return generate_bundle(
        catalog,
        catalog_sha256=sha256_file(CATALOG_PATH),
        seed=631200,
    )


def test_generation_hits_the_registered_200_case_design(bundle) -> None:
    assert len(bundle.ranking_cases) == 200
    assert Counter(case.stratum for case in bundle.ranking_cases) == {
        "general": 48,
        "budget_multi": 40,
        "personalization": 48,
        "repurchase": 24,
        "long_tail": 24,
        "adversarial": 16,
    }
    assert Counter(case.identity.kind for case in bundle.ranking_cases) == {
        "guest": 100,
        "member": 100,
    }


def test_generation_uses_thirty_unique_catalog_candidates(bundle, catalog) -> None:
    known = {int(product_id) for product_id in catalog}
    for case in bundle.ranking_cases:
        assert len(case.candidate_product_ids) == 30
        assert len(set(case.candidate_product_ids)) == 30
        assert set(case.candidate_product_ids) <= known
        assert set(case.candidate_provenance) == set(case.candidate_product_ids)


def test_generation_creates_one_valid_draft_label_per_case(bundle) -> None:
    labels_by_case = {labels.case_id: labels for labels in bundle.draft_labels}
    assert len(labels_by_case) == 200
    for case in bundle.ranking_cases:
        labels = labels_by_case[case.case_id]
        assert set(labels.relevant_product_ids) <= set(case.candidate_product_ids)
        assert 1 <= len(labels.relevant_product_ids) <= 6
        assert 3 in labels.relevance_grades.values()
        assert labels.label_status == "draft"
        assert labels.label_source == "heuristic"


def test_generation_balances_personalization_variants(bundle) -> None:
    cases = [case for case in bundle.ranking_cases if case.stratum == "personalization"]
    assert Counter(case.variant for case in cases) == {
        "preference_helpful": 24,
        "profile_overreach": 24,
    }
    assert all(case.identity.kind == "member" for case in cases)
    assert all(case.profile_summary for case in cases)


def test_generation_keeps_safety_cases_outside_ranking_count(bundle) -> None:
    assert len(bundle.safety_cases) == 24
    assert Counter(case.scenario for case in bundle.safety_cases) == {
        "catalog_prompt_injection": 8,
        "hard_constraint_integrity": 8,
        "candidate_set_integrity": 8,
    }
    assert not (
        {case.case_id for case in bundle.ranking_cases}
        & {case.case_id for case in bundle.safety_cases}
    )


def test_generation_is_byte_stable(catalog) -> None:
    catalog_sha256 = sha256_file(CATALOG_PATH)
    first = generate_bundle(catalog, catalog_sha256=catalog_sha256, seed=631200)
    second = generate_bundle(catalog, catalog_sha256=catalog_sha256, seed=631200)

    assert serialize_bundle(first) == serialize_bundle(second)


def test_generation_changes_when_seed_changes(catalog) -> None:
    catalog_sha256 = sha256_file(CATALOG_PATH)

    first = generate_bundle(catalog, catalog_sha256=catalog_sha256, seed=631200)
    second = generate_bundle(catalog, catalog_sha256=catalog_sha256, seed=631201)

    assert serialize_bundle(first) != serialize_bundle(second)


def test_generation_queries_and_families_are_unique(bundle) -> None:
    queries = [case.query.casefold().strip() for case in bundle.ranking_cases]
    families = [case.family_id for case in bundle.ranking_cases]
    assert len(queries) == len(set(queries)) == 200
    assert len(families) == len(set(families)) == 200


def test_committed_dataset_is_complete_and_audited() -> None:
    dataset = load_dataset(ROOT, label_policy="draft")

    assert len(dataset.ranking_cases) == 200
    assert len(dataset.labels_by_case) == 200
    assert len(dataset.safety_cases) == 24
    assert dataset.manifest.confirmatory_eligible is False
    assert dataset.manifest.label_status == "draft"
    assert dataset.manifest.ranking_count == 200
    assert dataset.manifest.safety_count == 24


def test_committed_artifacts_match_fresh_generation(tmp_path: Path) -> None:
    destination = tmp_path / "dataset"

    assert main(["generate", "--out", str(destination), "--seed", "631200"]) == 0

    for relative in GENERATED_FILES:
        assert (destination / relative).read_bytes() == (ROOT / relative).read_bytes()


def test_generate_refuses_an_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "dataset"
    destination.mkdir()

    assert main(["generate", "--out", str(destination)]) == 2


def test_audit_command_accepts_committed_dataset() -> None:
    assert main(["audit", "--root", str(ROOT)]) == 0
