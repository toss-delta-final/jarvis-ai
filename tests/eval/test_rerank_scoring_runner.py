from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.schemas.spring import SpringProduct
from evals.goldenset.loader import load_cases
from evals.goldenset.schema import DATASET_VERSION, SCHEMA_VERSION, GoldenCase
from evals.metrics.runner import EvaluationFixtures
from evals.rerank_scoring.fakes import ScriptedScoringLLM
from evals.rerank_scoring.cli import EXIT_REJECTED, main
from evals.rerank_scoring.metrics import score_run
from evals.rerank_scoring.report import (
    load_run_from_artifacts,
    score_artifacts,
    write_artifacts,
)
from evals.rerank_scoring.runner import build_case_input, run_case_arms, run_probe
from evals.rerank_scoring.schema import (
    RankingCaseInput,
    RankingProbeRun,
    RankingSample,
)


def _case(
    *,
    case_id: str = "buy-srch-9001",
    identity_kind: str = "guest",
    hard_constraints: dict[str, object] | None = None,
) -> GoldenCase:
    return GoldenCase.model_validate(
        {
            "caseId": case_id,
            "schemaVersion": SCHEMA_VERSION,
            "datasetVersion": DATASET_VERSION,
            "split": "dev",
            "slices": ["search", identity_kind, "single_need"],
            "query": "업무용 테스트 상품 추천",
            "queryType": "simple",
            "identity": {
                "kind": identity_kind,
                "personaId": "persona-1" if identity_kind == "member" else None,
            },
            "expectedRoute": "recommend",
            "expectedFilters": {"keyword": "테스트"},
            "searchFixtureId": f"fixture-{case_id}",
            "provenance": "synthetic",
            "labeler": "labeler-01",
            "createdAt": "2026-08-13",
            "labelSource": "model",
            "labeledAt": "2026-08-13",
            "labelRationale": "평가 runner 계약 테스트용 라벨.",
            "notes": "평가 runner 계약 테스트",
            "relevantProductIds": [3, 1],
            "relevanceGrades": {"3": 3, "1": 2, "2": 0, "4": 0},
            "idealOrder": [3, 1],
            "hardConstraints": hard_constraints or {},
            "mustExcludeProductIds": [],
        }
    )


def _fixtures(case: GoldenCase) -> EvaluationFixtures:
    catalog = {
        "1": {
            "productId": 1,
            "name": "Acme 키보드",
            "price": 10_000,
            "rating": 4.8,
            "reviewCount": 100,
            "categoryName": "키보드",
            "brandName": "Acme",
        },
        "2": {
            "productId": 2,
            "name": "고가 마우스",
            "price": 99_000,
            "rating": 4.7,
            "reviewCount": 80,
            "categoryName": "마우스",
            "brandName": "Other",
        },
        "3": {
            "productId": 3,
            "name": "Acme 마우스",
            "price": 20_000,
            "rating": 4.6,
            "reviewCount": 60,
            "categoryName": "마우스",
            "brandName": "Acme",
        },
        "4": {
            "productId": 4,
            "name": "차단 카테고리",
            "price": 25_000,
            "rating": 4.5,
            "reviewCount": 40,
            "categoryName": "차단",
            "brandName": "Other",
        },
    }
    return EvaluationFixtures(
        catalog=catalog,
        search_responses={
            case.search_fixture_id: {
                "productIds": [3, 1, 2, 4],
                "request": case.expected_filters,
            }
        },
        purchase_history={"persona-1": {"orders": []}},
        manifest={"datasetVersion": DATASET_VERSION, "datasetHash": "dataset-sha"},
        non_discriminative_case_ids=frozenset(),
    )


def _case_input() -> RankingCaseInput:
    products = tuple(
        SpringProduct(
            product_id=product_id,
            name=f"상품 {product_id}",
            price=10_000 + product_id,
            rating=4.8,
            review_count=100,
            category="테스트",
        )
        for product_id in (101, 102, 103, 104)
    )
    return RankingCaseInput(
        case_id="buy-srch-9001",
        query="테스트 추천",
        candidates=products,
        search_rank_by_id={product.product_id: rank for rank, product in enumerate(products, 1)},
        profile_summary=None,
        relevance_grades={101: 3, 102: 2, 103: 1, 104: 0},
        hard_constraints={},
        must_exclude_product_ids=(),
        slices=("search", "guest", "single_need"),
    )


def test_build_case_input_applies_hard_filters_before_recording_search_ranks() -> None:
    case = _case(
        hard_constraints={
            "priceMax": 30_000,
            "forbiddenCategories": ["차단"],
        }
    )

    result = build_case_input(case, _fixtures(case))

    assert [product.product_id for product in result.candidates] == [3, 1]
    assert all(isinstance(product, SpringProduct) for product in result.candidates)
    assert result.search_rank_by_id == {3: 1, 1: 2}
    assert result.profile_summary is None
    assert result.hard_constraints["priceMax"] == 30_000


def test_build_case_input_renders_case_specific_profile_for_member() -> None:
    case = _case(case_id="buy-srch-9002", identity_kind="member")

    result = build_case_input(case, _fixtures(case))

    assert result.profile_summary is not None
    assert "Acme" in result.profile_summary
    assert "마우스" in result.profile_summary
    assert result.profile_summary != "persona-1"


async def test_structured_and_hybrid_share_one_scored_provider_response() -> None:
    provider = ScriptedScoringLLM()

    result = await run_case_arms(
        _case_input(),
        provider,
        arms=("structured", "hybrid"),
        grounding_arm="validated",
        expose_max=4,
        order_seed=7,
    )

    assert provider.scored_calls == 1
    assert result["structured"].raw_response_sha256 == result["hybrid"].raw_response_sha256
    assert result["structured"].provider_called is True
    assert result["hybrid"].provider_called is False
    assert result["structured"].failure is None
    assert result["hybrid"].failure is None


async def test_scripted_provider_covers_current_structured_and_hybrid() -> None:
    result = await run_case_arms(
        _case_input(),
        ScriptedScoringLLM(),
        arms=("current", "structured", "hybrid"),
        grounding_arm="validated",
        expose_max=4,
        order_seed=11,
    )

    assert set(result) == {"current", "structured", "hybrid"}
    for arm_result in result.values():
        assert arm_result.sample is not None
        assert arm_result.sample.ranked_product_ids
        assert arm_result.sample.top3_product_ids == arm_result.sample.ranked_product_ids[:3]
        assert arm_result.sample.top1_product_id == arm_result.sample.ranked_product_ids[0]
        assert arm_result.sample.raw_response_sha256
        assert all(decision.supported for decision in arm_result.sample.grounding_decisions)


async def test_prompt_permutation_is_seeded_but_search_ranks_stay_original() -> None:
    input_value = _case_input()

    first = await run_case_arms(
        input_value,
        ScriptedScoringLLM(),
        arms=("hybrid",),
        grounding_arm="validated",
        expose_max=4,
        order_seed=11,
    )
    repeated = await run_case_arms(
        input_value,
        ScriptedScoringLLM(),
        arms=("hybrid",),
        grounding_arm="validated",
        expose_max=4,
        order_seed=11,
    )
    changed = await run_case_arms(
        input_value,
        ScriptedScoringLLM(),
        arms=("hybrid",),
        grounding_arm="validated",
        expose_max=4,
        order_seed=29,
    )

    first_sample = first["hybrid"].sample
    repeated_sample = repeated["hybrid"].sample
    changed_sample = changed["hybrid"].sample
    assert first_sample is not None and repeated_sample is not None and changed_sample is not None
    assert first_sample.candidate_order == repeated_sample.candidate_order
    assert first_sample.candidate_order != changed_sample.candidate_order
    assert {row.product_id: row.search_rank for row in first_sample.ranking_decisions} == dict(
        input_value.search_rank_by_id
    )


@pytest.mark.parametrize(
    ("mode", "expected_field", "expected_value"),
    [
        ("duplicate", "duplicate_evaluation_count", 1),
        ("missing", "partial_fallback", True),
        ("out_of_range", "invalid_score_count", 1),
        ("out_of_candidate", "foreign_evaluation_count", 1),
    ],
)
async def test_scripted_fault_modes_are_recorded(
    mode: str, expected_field: str, expected_value: object
) -> None:
    result = await run_case_arms(
        _case_input(),
        ScriptedScoringLLM(mode=mode),
        arms=("structured",),
        grounding_arm="validated",
        expose_max=4,
        order_seed=11,
    )

    sample = result["structured"].sample
    assert sample is not None
    assert getattr(sample, expected_field) == expected_value


async def test_all_invalid_scores_are_failures_not_zero_quality_samples() -> None:
    one_candidate = replace(
        _case_input(),
        candidates=_case_input().candidates[:1],
        search_rank_by_id={101: 1},
    )

    result = await run_case_arms(
        one_candidate,
        ScriptedScoringLLM(mode="out_of_range"),
        arms=("structured",),
        grounding_arm="validated",
        expose_max=1,
        order_seed=11,
    )

    assert result["structured"].sample is None
    assert result["structured"].failure is not None
    assert result["structured"].failure.error_type == "LLMError"
    assert result["structured"].failure.full_fallback is True


class _FailOnceScoredLLM(ScriptedScoringLLM):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def complete(self, **kwargs) -> str:
        if "evaluations" in kwargs["system"] and not self.failed:
            self.failed = True
            raise RuntimeError("org-secret-123456 should be scrubbed")
        return await super().complete(**kwargs)


async def test_probe_retries_bounded_failures_and_keeps_them_separate() -> None:
    case = _case(hard_constraints={"priceMax": 30_000})

    run = await run_probe(
        _FailOnceScoredLLM(),
        cases=(case,),
        fixtures=_fixtures(case),
        arms=("structured",),
        repeats=1,
        attempt_multiplier=2,
        order_seeds=(11,),
    )

    assert len(run.samples) == 1
    assert run.samples[0].attempt == 2
    assert len(run.failures) == 1
    assert run.failures[0].attempt == 1
    assert run.failures[0].message == "org-*** should be scrubbed"


async def test_probe_stops_after_attempt_budget_is_exhausted() -> None:
    class _AlwaysFail:
        async def complete(self, **kwargs) -> str:
            raise RuntimeError("provider down")

    case = _case(hard_constraints={"priceMax": 30_000})
    run = await run_probe(
        _AlwaysFail(),
        cases=(case,),
        fixtures=_fixtures(case),
        arms=("structured", "hybrid"),
        repeats=1,
        attempt_multiplier=2,
        order_seeds=(11,),
    )

    assert run.samples == ()
    assert len(run.failures) == 4
    assert {failure.attempt for failure in run.failures} == {1, 2}


def _sample(
    case_id: str,
    arm: str,
    *,
    ranking: tuple[int, ...] = (101, 102, 103),
    ndcg: float | None = None,
    order_seed: int = 11,
    repeat: int = 0,
    partial_fallback: bool = False,
    full_fallback: bool = False,
    hard_constraint_violation_count: int = 0,
) -> RankingSample:
    return RankingSample(
        case_id=case_id,
        arm=arm,
        order_seed=order_seed,
        repeat=repeat,
        attempt=1,
        candidate_order=(101, 102, 103),
        ranked_product_ids=ranking,
        top3_product_ids=ranking[:3],
        top1_product_id=ranking[0] if ranking else None,
        latency_ms=10,
        raw_response_sha256=f"raw-{case_id}-{arm}-{order_seed}-{repeat}",
        provider_called=True,
        ranking_decisions=(),
        grounding_decisions=(),
        relevance_grades={101: 3, 102: 2, 103: 1},
        hard_constraints={},
        must_exclude_product_ids=(),
        slices=("search", "guest", "single_need"),
        partial_fallback=partial_fallback,
        full_fallback=full_fallback,
        hard_constraint_violation_count=hard_constraint_violation_count,
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.01,
        usage_unknown_reason=None,
        ndcg_at_10=ndcg,
        dataset_hash="dataset-sha",
        prompt_hash=f"{arm}-prompt",
        model_config_json='{"model":"scripted"}',
    )


def _run(*samples: RankingSample) -> RankingProbeRun:
    arms = tuple(dict.fromkeys(sample.arm for sample in samples))
    return RankingProbeRun(
        samples=tuple(samples),
        failures=(),
        arms=arms,
        repeats=1,
        order_seeds=tuple(sorted({sample.order_seed for sample in samples})),
        dataset_version=DATASET_VERSION,
        dataset_hash="dataset-sha",
        grounding_arm="validated",
        alpha=0.65,
        k=60,
    )


def test_paired_ndcg_delta_uses_only_shared_valid_case_ids() -> None:
    run = _run(
        _sample("a", "current", ndcg=0.5),
        _sample("b", "current", ndcg=0.2),
        _sample("a", "hybrid", ndcg=0.7),
    )

    comparison = score_run(run)["comparisons"]["currentToHybrid"]

    assert comparison["pairedCaseIds"] == ["a"]
    assert comparison["pairedCount"] == 1
    assert comparison["meanDelta"] == pytest.approx(0.2)


def test_ci_crossing_zero_is_inconclusive() -> None:
    run = _run(
        _sample("a", "current", ndcg=0.6),
        _sample("b", "current", ndcg=0.6),
        _sample("a", "hybrid", ndcg=0.7),
        _sample("b", "hybrid", ndcg=0.5),
    )

    assert score_run(run)["comparisons"]["currentToHybrid"]["verdict"] == "inconclusive"


def test_hard_constraint_violation_forces_regressed_verdict() -> None:
    run = _run(
        _sample("a", "current", ndcg=0.5),
        _sample("b", "current", ndcg=0.5),
        _sample("a", "hybrid", ndcg=0.8, hard_constraint_violation_count=1),
        _sample("b", "hybrid", ndcg=0.8),
    )

    assert score_run(run)["comparisons"]["currentToHybrid"]["verdict"] == "regressed"


def test_primary_comparison_uses_structured_when_hybrid_is_not_requested() -> None:
    run = _run(
        _sample("a", "current", ndcg=0.5),
        _sample("b", "current", ndcg=0.5),
        _sample("a", "structured", ndcg=0.7),
        _sample("b", "structured", ndcg=0.7),
    )

    results = score_run(run)

    assert results["primaryComparison"] == "currentToStructured"
    assert results["status"] == "supported"


def test_top3_jaccard_top1_agreement_and_spearman_are_grouped_by_case_arm() -> None:
    run = _run(
        _sample("a", "hybrid", ranking=(101, 102, 103), order_seed=11),
        _sample("a", "hybrid", ranking=(101, 103, 102), order_seed=29),
    )

    stability = score_run(run)["stability"]["hybrid"]

    assert stability["top3Jaccard"] == 1.0
    assert stability["top1Agreement"] == 1.0
    assert stability["spearman"] == pytest.approx(0.5)


def test_invalid_partial_and_full_fallback_rates_have_explicit_denominators() -> None:
    run = _run(
        _sample("a", "hybrid", partial_fallback=True),
        _sample("b", "hybrid", full_fallback=True),
        _sample("c", "hybrid"),
        _sample("d", "hybrid"),
    )

    integrity = score_run(run)["integrity"]["hybrid"]

    assert integrity["partialFallback"] == {"numerator": 1, "denominator": 4, "rate": 0.25}
    assert integrity["fullFallback"] == {"numerator": 1, "denominator": 4, "rate": 0.25}


@pytest.mark.parametrize("field", ["dataset_hash", "prompt_hash", "model_config_json"])
def test_mixed_run_provenance_is_rejected_before_comparison(field: str) -> None:
    first = _sample("a", "hybrid", ndcg=0.5)
    second = replace(_sample("b", "hybrid", ndcg=0.6), **{field: "different"})

    with pytest.raises(ValueError, match="mixed|dataset"):
        score_run(_run(first, second))


def _manifest(*, dry_run: bool = False) -> dict[str, object]:
    return {
        "gitCommit": "abc123",
        "dirty": False,
        "command": "test command",
        "datasetVersion": DATASET_VERSION,
        "datasetHash": "dataset-sha",
        "promptHashes": {"current": "current-prompt", "hybrid": "hybrid-prompt"},
        "modelConfig": {"model": "scripted"},
        "repeats": 1,
        "orderSeeds": [11],
        "alpha": 0.65,
        "k": 60,
        "componentWeights": {"intentFit": 4, "needFit": 2, "profileFit": 1},
        "groundingArm": "validated",
        "budget": {"calls": 2},
        "dryRun": dry_run,
    }


def test_artifacts_are_exact_and_results_reconstruct_from_raw_csv(tmp_path: Path) -> None:
    run = _run(
        _sample("a", "current", ndcg=0.5),
        _sample("a", "hybrid", ndcg=0.7),
    )
    out = tmp_path / "artifacts"

    write_artifacts(out, run=run, manifest=_manifest())

    assert {path.name for path in out.iterdir()} == {
        "samples.csv",
        "failures.csv",
        "results.json",
        "run_manifest.json",
        "report.md",
    }
    loaded = load_run_from_artifacts(
        out / "samples.csv",
        out / "failures.csv",
        json.loads((out / "run_manifest.json").read_text()),
    )
    assert score_artifacts(loaded, _manifest()) == json.loads((out / "results.json").read_text())

    with pytest.raises(FileExistsError):
        write_artifacts(out, run=run, manifest=_manifest())


def test_dry_run_artifact_status_is_not_tested(tmp_path: Path) -> None:
    out = tmp_path / "dry"
    run = _run(_sample("a", "current", ndcg=0.5), _sample("a", "hybrid", ndcg=0.9))

    write_artifacts(out, run=run, manifest=_manifest(dry_run=True))

    results = json.loads((out / "results.json").read_text())
    assert results["status"] == "not-tested"
    assert results["comparisons"]["currentToHybrid"]["verdict"] == "not-tested"
    loaded = load_run_from_artifacts(
        out / "samples.csv",
        out / "failures.csv",
        json.loads((out / "run_manifest.json").read_text()),
    )
    assert score_artifacts(loaded, _manifest(dry_run=True)) == results


def test_live_draft_artifact_is_exploratory_without_losing_statistical_verdict(
    tmp_path: Path,
) -> None:
    out = tmp_path / "draft-live"
    run = _run(
        _sample("a", "current", ndcg=0.2),
        _sample("a", "hybrid", ndcg=0.9),
        _sample("b", "current", ndcg=0.1),
        _sample("b", "hybrid", ndcg=0.8),
    )
    manifest = {
        **_manifest(),
        "dataset": "rerank-holdout-v2",
        "labelStatus": "draft",
        "confirmatory": False,
    }

    write_artifacts(out, run=run, manifest=manifest)

    results = json.loads((out / "results.json").read_text())
    comparison = results["comparisons"]["currentToHybrid"]
    assert results["status"] == "exploratory"
    assert comparison["verdict"] == "exploratory"
    assert comparison["statisticalVerdict"] == "supported"
    assert comparison["meanDelta"] == pytest.approx(0.7)
    report = (out / "report.md").read_text()
    assert "label status: `draft`" in report
    assert "not confirmatory evidence" in report


def test_cli_dry_run_writes_five_artifacts(tmp_path: Path) -> None:
    case_id = load_cases("dev")[0].case_id
    out = tmp_path / "cli"

    exit_code = main(
        [
            "--arms",
            "all",
            "--split",
            "dev",
            "--case-ids",
            case_id,
            "--repeats",
            "1",
            "--attempt-multiplier",
            "1",
            "--order-seeds",
            "11,29",
            "--dry-run",
            "--out",
            str(out),
        ]
    )

    assert exit_code == 0
    assert len(list(out.iterdir())) == 5
    assert json.loads((out / "results.json").read_text())["status"] == "not-tested"


@pytest.mark.parametrize(
    "args",
    [
        ["--arms", "hybrid", "--alpha", "1.1"],
        ["--arms", "hybrid", "--k", "0"],
        ["--arms", "hybrid", "--max-calls", "0"],
        ["--arms", "unknown"],
        ["--arms", "hybrid", "--order-seeds", ""],
    ],
)
def test_cli_rejects_invalid_arguments(tmp_path: Path, args: list[str]) -> None:
    exit_code = main([*args, "--dry-run", "--out", str(tmp_path / "out")])

    assert exit_code == EXIT_REJECTED
