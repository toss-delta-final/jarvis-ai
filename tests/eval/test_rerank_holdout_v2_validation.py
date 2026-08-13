from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from evals.goldenset.loader import ROOT as GOLDENSET_ROOT
from evals.rerank_holdout_v2.generator import generate_bundle
from evals.rerank_holdout_v2.io import sha256_file
from evals.rerank_holdout_v2.validation import validate_bundle

CATALOG_PATH = GOLDENSET_ROOT / "fixtures/catalog_snapshot.json"


@pytest.fixture(scope="module")
def catalog() -> dict[str, dict[str, object]]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalog_sha256() -> str:
    return sha256_file(CATALOG_PATH)


@pytest.fixture(scope="module")
def bundle(catalog, catalog_sha256):
    return generate_bundle(catalog, catalog_sha256=catalog_sha256, seed=631200)


def test_validation_accepts_the_registered_bundle(bundle, catalog, catalog_sha256) -> None:
    report = validate_bundle(
        bundle,
        catalog,
        GOLDENSET_ROOT,
        catalog_sha256=catalog_sha256,
        seed=631200,
    )

    assert report.passed is True
    assert report.ranking_count == 200
    assert report.safety_count == 24
    assert report.candidate_count_min == report.candidate_count_max == 30
    assert report.unique_family_count == report.unique_query_count == 200
    assert report.max_legacy_query_similarity < 0.85
    assert "legacy_holdout_labels_unopened" in report.checks


def test_validation_never_opens_legacy_holdout_labels(
    bundle, catalog, catalog_sha256, monkeypatch
) -> None:
    real_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path.name == "buyer_holdout_labels.jsonl":
            raise AssertionError("legacy sealed labels were opened")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    report = validate_bundle(
        bundle,
        catalog,
        GOLDENSET_ROOT,
        catalog_sha256=catalog_sha256,
        seed=631200,
    )

    assert report.passed is True


def test_validation_rejects_positive_price_violator(bundle, catalog, catalog_sha256) -> None:
    labels = list(bundle.draft_labels)
    label_index = next(
        index for index, row in enumerate(labels) if row.hard_constraints.price_max is not None
    )
    row = labels[label_index]
    case = next(case for case in bundle.ranking_cases if case.case_id == row.case_id)
    violating_id = next(
        product_id
        for product_id in case.candidate_product_ids
        if catalog[str(product_id)].get("price") is None
        or int(catalog[str(product_id)]["price"]) > row.hard_constraints.price_max
    )
    labels[label_index] = row.model_copy(
        update={
            "relevant_product_ids": [violating_id],
            "relevance_grades": {violating_id: 3},
            "ideal_order": [violating_id],
        }
    )
    broken = replace(bundle, draft_labels=tuple(labels))

    with pytest.raises(ValueError, match="positive candidate violates"):
        validate_bundle(
            broken,
            catalog,
            GOLDENSET_ROOT,
            catalog_sha256=catalog_sha256,
            seed=631200,
        )


def test_validation_rejects_duplicate_positive_sets(bundle, catalog, catalog_sha256) -> None:
    labels = list(bundle.draft_labels)
    first = labels[0]
    second = labels[1]
    labels[1] = second.model_copy(
        update={
            "relevant_product_ids": list(first.relevant_product_ids),
            "relevance_grades": dict(first.relevance_grades),
            "ideal_order": list(first.ideal_order),
        }
    )
    broken = replace(bundle, draft_labels=tuple(labels))

    with pytest.raises(ValueError, match="duplicate positive label set"):
        validate_bundle(
            broken,
            catalog,
            GOLDENSET_ROOT,
            catalog_sha256=catalog_sha256,
            seed=631200,
        )


def test_validation_rejects_legacy_query_collision(bundle, catalog, catalog_sha256) -> None:
    legacy_query = json.loads(
        (GOLDENSET_ROOT / "cases/buyer_dev.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )["query"]
    cases = list(bundle.ranking_cases)
    cases[0] = cases[0].model_copy(update={"query": legacy_query})
    broken = replace(bundle, ranking_cases=tuple(cases))

    with pytest.raises(ValueError, match="legacy query collision"):
        validate_bundle(
            broken,
            catalog,
            GOLDENSET_ROOT,
            catalog_sha256=catalog_sha256,
            seed=631200,
        )


def test_validation_rejects_wrong_registered_count(bundle, catalog, catalog_sha256) -> None:
    broken = replace(
        bundle,
        ranking_cases=bundle.ranking_cases[:-1],
        draft_labels=bundle.draft_labels[:-1],
    )

    with pytest.raises(ValueError, match="exactly 200 ranking cases"):
        validate_bundle(
            broken,
            catalog,
            GOLDENSET_ROOT,
            catalog_sha256=catalog_sha256,
            seed=631200,
        )
