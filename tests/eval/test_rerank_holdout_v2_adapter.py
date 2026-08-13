from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evals.goldenset.loader import ROOT as GOLDENSET_ROOT
from evals.rerank_holdout_v2.adapter import build_case_input
from evals.rerank_holdout_v2.generator import generate_bundle
from evals.rerank_holdout_v2.io import sha256_file
from evals.rerank_scoring import cli as rerank_cli
from evals.rerank_scoring.fakes import ScriptedScoringLLM
from evals.rerank_scoring.runner import run_input_probe

CATALOG_PATH = GOLDENSET_ROOT / "fixtures/catalog_snapshot.json"


@pytest.fixture(scope="module")
def generated():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_sha256 = sha256_file(CATALOG_PATH)
    bundle = generate_bundle(catalog, catalog_sha256=catalog_sha256, seed=631200)
    return catalog, catalog_sha256, bundle


def _general_case(generated):
    catalog, _catalog_sha256, bundle = generated
    case = next(case for case in bundle.ranking_cases if case.stratum == "general")
    labels = next(labels for labels in bundle.draft_labels if labels.case_id == case.case_id)
    return catalog, case, labels


def test_adapter_preserves_candidate_order_profile_and_labels(generated) -> None:
    catalog, case, labels = _general_case(generated)

    result = build_case_input(case, labels, catalog)

    assert tuple(result.search_rank_by_id) == tuple(case.candidate_product_ids)
    assert result.search_rank_by_id == {
        product_id: rank for rank, product_id in enumerate(case.candidate_product_ids, 1)
    }
    assert result.profile_summary == case.profile_summary
    assert result.relevance_grades == labels.relevance_grades
    assert len(result.candidates) == 30
    assert result.slices[:3] == ("ranking", "general", case.identity.kind)


def test_adapter_applies_hard_filters_before_reranking(generated) -> None:
    catalog, _catalog_sha256, bundle = generated
    labels_by_case = {labels.case_id: labels for labels in bundle.draft_labels}
    case = next(
        case
        for case in bundle.ranking_cases
        if labels_by_case[case.case_id].hard_constraints.price_max is not None
    )
    labels = labels_by_case[case.case_id]

    result = build_case_input(case, labels, catalog)

    assert len(result.candidates) < 30
    assert all(
        product.price is not None and product.price <= labels.hard_constraints.price_max
        for product in result.candidates
    )
    assert not set(result.search_rank_by_id) & set(labels.must_exclude_product_ids)


async def test_input_probe_preserves_explicit_dataset_provenance(generated) -> None:
    catalog, catalog_sha256, bundle = generated
    case = next(case for case in bundle.ranking_cases if case.stratum == "general")
    labels = next(labels for labels in bundle.draft_labels if labels.case_id == case.case_id)
    case_input = build_case_input(case, labels, catalog)

    run = await run_input_probe(
        ScriptedScoringLLM(),
        case_inputs=(case_input,),
        dataset_version="1.0.0",
        dataset_hash=catalog_sha256,
        arms=("current", "structured"),
        repeats=1,
        attempt_multiplier=1,
        order_seeds=(11,),
    )

    assert len(run.samples) == 2
    assert run.dataset_version == "1.0.0"
    assert run.dataset_hash == catalog_sha256
    assert {sample.dataset_hash for sample in run.samples} == {catalog_sha256}


def test_live_cli_rejects_draft_dataset_before_provider_build(
    generated, monkeypatch, tmp_path
) -> None:
    catalog, catalog_sha256, bundle = generated
    dataset = SimpleNamespace(
        manifest=SimpleNamespace(
            label_status="draft",
            confirmatory_eligible=False,
            dataset_version="1.0.0",
            catalog_sha256=catalog_sha256,
        ),
        ranking_cases=bundle.ranking_cases,
        labels_by_case={labels.case_id: labels for labels in bundle.draft_labels},
        catalog=catalog,
    )
    provider_called = False

    def fake_load(*args, **kwargs):
        return dataset

    def forbidden_provider(**kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("provider should not be built")

    monkeypatch.setattr(rerank_cli, "load_holdout_dataset", fake_load)
    monkeypatch.setattr(rerank_cli, "build_live_delegate", forbidden_provider)

    code = rerank_cli.main(
        [
            "--dataset",
            "rerank-holdout-v2",
            "--arms",
            "current,structured",
            "--out",
            str(tmp_path / "run"),
        ]
    )

    assert code == rerank_cli.EXIT_REJECTED
    assert provider_called is False


def test_live_cli_allows_explicit_exploratory_draft_run(generated, monkeypatch, tmp_path) -> None:
    catalog, catalog_sha256, bundle = generated
    selected = next(case for case in bundle.ranking_cases if case.stratum == "general")
    dataset = SimpleNamespace(
        manifest=SimpleNamespace(
            label_status="draft",
            confirmatory_eligible=False,
            dataset_version="1.0.0",
            dataset_hash=catalog_sha256,
            catalog_sha256=catalog_sha256,
        ),
        ranking_cases=bundle.ranking_cases,
        labels_by_case={labels.case_id: labels for labels in bundle.draft_labels},
        catalog=catalog,
    )
    monkeypatch.setattr(
        rerank_cli,
        "load_holdout_dataset",
        lambda *args, **kwargs: dataset,
    )
    monkeypatch.setattr(
        rerank_cli,
        "build_live_delegate",
        lambda **kwargs: (
            ScriptedScoringLLM(),
            {"provider": "scripted", "model": "scripted-scoring"},
        ),
    )
    out = tmp_path / "draft-live"

    code = rerank_cli.main(
        [
            "--dataset",
            "rerank-holdout-v2",
            "--allow-draft-live",
            "--arms",
            "current,structured",
            "--case-ids",
            selected.case_id,
            "--order-seeds",
            "11",
            "--attempt-multiplier",
            "1",
            "--out",
            str(out),
        ]
    )

    assert code == rerank_cli.EXIT_OK
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert manifest["labelStatus"] == "draft"
    assert manifest["confirmatory"] is False
    assert results["status"] == "exploratory"


def test_dry_run_cli_accepts_one_draft_case_and_marks_non_confirmatory(
    generated, monkeypatch, tmp_path
) -> None:
    catalog, catalog_sha256, bundle = generated
    selected = next(case for case in bundle.ranking_cases if case.stratum == "general")
    labels_by_case = {labels.case_id: labels for labels in bundle.draft_labels}
    dataset = SimpleNamespace(
        manifest=SimpleNamespace(
            label_status="draft",
            confirmatory_eligible=False,
            dataset_version="1.0.0",
            catalog_sha256=catalog_sha256,
        ),
        ranking_cases=bundle.ranking_cases,
        labels_by_case=labels_by_case,
        catalog=catalog,
    )
    selected_root = tmp_path / "selected-dataset"
    loaded_roots = []

    def fake_load(root, *args, **kwargs):
        loaded_roots.append(root)
        return dataset

    monkeypatch.setattr(rerank_cli, "load_holdout_dataset", fake_load)
    out = tmp_path / "dry-run"

    code = rerank_cli.main(
        [
            "--dataset",
            "rerank-holdout-v2",
            "--dataset-root",
            str(selected_root),
            "--arms",
            "current,structured",
            "--case-ids",
            selected.case_id,
            "--order-seeds",
            "11",
            "--attempt-multiplier",
            "1",
            "--dry-run",
            "--out",
            str(out),
        ]
    )

    assert code == rerank_cli.EXIT_OK
    assert loaded_roots == [selected_root]
    manifest = json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset"] == "rerank-holdout-v2"
    assert manifest["labelStatus"] == "draft"
    assert manifest["confirmatory"] is False
