from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from app.core.config import Settings
from evals.goldenset.loader import load_cases
from evals.metrics.runner import load_evaluation_fixtures
from evals.model_eval.adapter import LiveBuyerAdapter, _evaluation_filters
from evals.model_eval.budget import BudgetLimits, BudgetTracker
from evals.metrics.settings import EvaluationSettings
from evals.model_eval.cli import (
    build_live_adapter,
    load_release_cases,
    main,
    resolve_repeats,
    result_exit_code,
    validate_eval_config,
)
from evals.model_eval.manifest import build_model_eval_manifest
from evals.model_eval.pricing import PriceBook, release_coverage_complete
from evals.model_eval.repeats import metric_value, run_repeats
from evals.model_eval.report import write_artifacts


def test_model_eval_budget_defaults_are_validated() -> None:
    settings = Settings(_env_file=None)
    assert settings.model_eval_max_calls_per_run == 800
    assert settings.model_eval_max_total_tokens_per_run == 30_000_000
    assert settings.model_eval_max_cost_usd_per_run == 20.0
    assert not hasattr(settings, "model_eval_repeats_default")
    with pytest.raises(ValueError):
        Settings(_env_file=None, model_eval_max_calls_per_run=0)
    assert resolve_repeats(None, {"repeats": 5}) == 5
    assert resolve_repeats(2, {"repeats": 5}) == 2


def test_eval_config_metric_and_policy_wiring() -> None:
    row = {
        "metrics": {
            "ndcgAtK": {"10": 0.2},
            "recallAtK": {"5": 0.75},
            "mrr": 0.4,
        },
        "filterAccuracy": 0.6,
        "hardConstraintViolated": True,
    }
    assert metric_value(row, "overall.ndcgAtK.10") == 0.2
    assert metric_value(row, "overall.recallAtK.5") == 0.75
    assert metric_value(row, "overall.hardConstraintViolationRate") == 1.0
    config = {
        "primaryMetric": "overall.ndcgAtK.10",
        "secondaryMetrics": ["overall.recallAtK.5"],
        "caseOrder": "caseId-asc",
        "missingRunPolicy": "excludePairReportCount",
        "multiplicity": "primaryConfirmatoryOthersExploratory",
        "regressionMargin": {"overall.ndcgAtK.10": 0.03},
        "releaseQualityLowerBound": {"overall.ndcgAtK.10": None},
        "releaseHardConstraintViolationMax": 0,
    }
    validate_eval_config(config)
    for key, value in (
        ("caseOrder", "random"),
        ("missingRunPolicy", "zeroFill"),
        ("multiplicity", "unknown"),
        ("primaryMetric", "overall.unknown"),
    ):
        with pytest.raises(ValueError):
            validate_eval_config({**config, key: value})


def test_repository_eval_config_has_calibrated_release_quality_bound() -> None:
    config = json.loads(Path("evals/model_eval/eval_config.json").read_text(encoding="utf-8"))
    assert config["releaseQualityLowerBound"] == {"overall.ndcgAtK.10": 0.60}
    assert (
        result_exit_code(
            {
                "budgetExceeded": False,
                "coverage": {"costCoverage": 1.0, "tokenCoverage": 1.0},
                "hardFailureCount": 0,
                "hardConstraintViolationRate": 0.0,
                "primarySummary": {"mean": 0.59},
            },
            release=True,
            hard_failure_max=0,
            hard_constraint_violation_max=0,
            quality_lower_bound=config["releaseQualityLowerBound"]["overall.ndcgAtK.10"],
            regression=None,
        )
        == 4
    )


def test_dry_run_does_not_create_output_or_live_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "dry"

    def forbidden(**kwargs):
        del kwargs
        raise AssertionError("live factory must not be created")

    monkeypatch.setattr("evals.model_eval.cli.build_live_adapter", forbidden)
    monkeypatch.setattr(
        "evals.model_eval.cli.get_settings",
        lambda: Settings(
            _env_file=None,
            model_eval_max_calls_per_run=20,
            model_eval_max_total_tokens_per_run=100,
            model_eval_max_cost_usd_per_run=1,
        ),
    )
    assert main(["--out", str(out), "--dry-run", "--case-limit", "2", "--repeats", "2"]) == 0
    assert not out.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["expectedCalls"] == 12
    assert payload["maxCalls"] == 20
    assert payload["maxTotalTokens"] == 100
    assert payload["maxCostUsd"] == 1


def test_dry_run_reads_budget_limit_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MODEL_EVAL_MAX_CALLS_PER_RUN", "10")
    monkeypatch.setattr(
        "evals.model_eval.cli.get_settings",
        lambda: Settings(_env_file=None),
    )
    assert (
        main(
            [
                "--out",
                str(tmp_path / "dry"),
                "--dry-run",
                "--case-limit",
                "2",
                "--repeats",
                "2",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["maxCalls"] == 10
    assert payload["allowed"] is False


def test_holdout_gate_rejects_before_unsealing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "evals.model_eval.cli.unseal_holdout_labels",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError(kwargs)),
    )
    assert main(["--out", str(tmp_path / "out"), "--split", "holdout", "--dry-run"]) == 2


def test_output_directory_is_never_overwritten(tmp_path: Path) -> None:
    out = tmp_path / "existing"
    out.mkdir()
    assert main(["--out", str(out), "--case-limit", "1"]) == 2


def test_release_coverage_requires_both_dimensions() -> None:
    assert release_coverage_complete({"costCoverage": 1.0, "tokenCoverage": 1.0})
    assert not release_coverage_complete({"costCoverage": 0.5, "tokenCoverage": 1.0})
    assert (
        result_exit_code(
            {
                "budgetExceeded": True,
                "coverage": {"costCoverage": 1.0, "tokenCoverage": 1.0},
                "hardFailureCount": 0,
                "hardConstraintViolationRate": 0.0,
                "primarySummary": {"mean": 0.9},
            },
            release=False,
            hard_failure_max=0,
            hard_constraint_violation_max=0,
            quality_lower_bound=None,
            regression=None,
        )
        == 3
    )
    release_results = {
        "budgetExceeded": False,
        "coverage": {"costCoverage": 1.0, "tokenCoverage": 1.0},
        "hardFailureCount": 0,
        "hardConstraintViolationRate": 0.1,
        "primarySummary": {"mean": 0.9},
    }
    assert (
        result_exit_code(
            release_results,
            release=True,
            hard_failure_max=0,
            hard_constraint_violation_max=0,
            quality_lower_bound=None,
            regression=None,
        )
        == 4
    )
    assert (
        result_exit_code(
            release_results,
            release=False,
            hard_failure_max=0,
            hard_constraint_violation_max=0,
            quality_lower_bound=None,
            regression={"verdict": "inconclusive"},
        )
        == 0
    )
    release_results["hardConstraintViolationRate"] = 0.0
    assert (
        result_exit_code(
            release_results,
            release=True,
            hard_failure_max=0,
            hard_constraint_violation_max=0,
            quality_lower_bound=0.95,
            regression=None,
        )
        == 4
    )
    assert (
        result_exit_code(
            release_results,
            release=True,
            hard_failure_max=0,
            hard_constraint_violation_max=0,
            quality_lower_bound=None,
            regression={"verdict": "inconclusive"},
        )
        == 4
    )


class _FakeLLM:
    def __init__(self, *, intent: str, product_ids: list[int], price_max: int = 12345) -> None:
        self.intent = intent
        self.product_ids = product_ids
        self.price_max = price_max

    async def complete(self, **kwargs) -> str:
        if kwargs["tier"] == "fast":
            return json.dumps(
                {
                    "intent": self.intent,
                    "reply": "",
                    "case": 2,
                    "semanticQuery": "고정 질의",
                    "categoryQueries": [],
                    "filters": {"priceMax": self.price_max},
                    "cart": {"productId": None, "optionId": None, "quantity": 1},
                    "revertCategories": [],
                    "repurchaseProducts": [],
                }
            )
        return json.dumps(
            {
                "ranked": [
                    {"productId": product_id, "rationale": "고정"}
                    for product_id in self.product_ids
                ],
                "overallComment": "고정",
            }
        )

    async def stream(self, **kwargs):
        del kwargs
        yield "고정"


def test_evaluation_filters_omit_mechanical_and_empty_values() -> None:
    class FakeFilters:
        def model_dump(self, **kwargs):
            assert kwargs == {"by_alias": True, "exclude_none": True}
            return {
                "keyword": "",
                "categoryIds": [],
                "semanticQuery": "모델 산출",
                "limit": 30,
                "excludeProductIds": [42],
            }

    assert _evaluation_filters(FakeFilters()) == {"semanticQuery": "모델 산출"}


def test_live_adapter_uses_real_decompose_filters_and_records_nonrecommend_failure() -> None:
    case = list(load_cases("dev"))[0]
    fixtures = load_evaluation_fixtures()
    product_ids = fixtures.search_responses[case.search_fixture_id]["productIds"]

    failed = LiveBuyerAdapter(_FakeLLM(intent="general", product_ids=product_ids))
    failed_result = failed(case, fixtures)
    assert failed_result["hardFailure"] is True
    assert failed_result["rankedProductIds"] == []
    assert failed_result["extractedFilters"]["priceMax"] == 12345

    succeeded = LiveBuyerAdapter(
        _FakeLLM(intent="recommend", product_ids=product_ids, price_max=999_999_999)
    )
    succeeded_result = succeeded(case, fixtures)
    assert succeeded_result["hardFailure"] is False
    assert succeeded_result["rankedProductIds"]
    assert succeeded_result["extractedFilters"]["priceMax"] == 999_999_999
    assert succeeded_result["extractedFilters"]["semanticQuery"] == "고정 질의"
    assert "limit" not in succeeded_result["extractedFilters"]
    assert "excludeProductIds" not in succeeded_result["extractedFilters"]


def test_empty_fixture_is_expected_zero_not_hard_failure() -> None:
    cases = {case.case_id: case for case in load_cases("dev")}
    case = cases["buy-cmap-0005"]
    fixtures = load_evaluation_fixtures()
    assert fixtures.search_responses[case.search_fixture_id]["productIds"] == []
    result = LiveBuyerAdapter(_FakeLLM(intent="recommend", product_ids=[]))(case, fixtures)
    assert result["hardFailure"] is False
    assert result["failureReason"] is None
    assert result["expectedZeroCandidates"] is True


def test_nonempty_fixture_without_push_remains_hard_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = list(load_cases("dev"))[0]
    fixtures = load_evaluation_fixtures()
    product_ids = fixtures.search_responses[case.search_fixture_id]["productIds"]
    assert product_ids

    async def no_push(**kwargs):
        del kwargs
        if False:
            yield ""

    monkeypatch.setattr("evals.model_eval.adapter.stream_recommendation", no_push)
    result = LiveBuyerAdapter(
        _FakeLLM(intent="recommend", product_ids=product_ids, price_max=999_999_999)
    )(case, fixtures)
    assert result["hardFailure"] is True
    assert result["failureReason"] == "emptyPush"
    assert result["expectedZeroCandidates"] is False


def test_model_eval_manifest_adds_versioned_config_hashes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "evals.model_eval.manifest.build_run_manifest",
        lambda **kwargs: {"hashes": {}, "seed": kwargs["seed"]},
    )
    eval_config = tmp_path / "eval.json"
    pricing = tmp_path / "pricing.json"
    eval_config.write_text("{}", encoding="utf-8")
    pricing.write_text("{}", encoding="utf-8")
    manifest = build_model_eval_manifest(
        command="cmd",
        seed=1,
        model_config={"provider": "fake"},
        repeats=2,
        declared_repeats=5,
        split="dev",
        case_order="caseId-asc",
        config_version="config-v1",
        budget={"maxCalls": 10},
        case_ids=["a"],
        eval_config_path=eval_config,
        pricing_path=pricing,
    )
    assert manifest["hashes"]["evalConfig"]
    assert manifest["hashes"]["pricingManifest"]
    assert manifest["modelEval"]["caseIds"] == ["a"]
    assert manifest["modelEval"]["declaredRepeats"] == 5
    assert manifest["modelEval"]["executedRepeats"] == 2
    assert manifest["hashes"]["graph"]
    assert manifest["hashes"]["modelEvalModules"]


def test_model_eval_module_hash_changes_with_file_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "evals.model_eval.manifest.build_run_manifest",
        lambda **kwargs: {"hashes": {}},
    )
    module_root = tmp_path / "modules"
    module_root.mkdir()
    module = module_root / "sample.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    graph = tmp_path / "graph.py"
    graph.write_text("GRAPH = 1\n", encoding="utf-8")
    eval_config = tmp_path / "eval.json"
    pricing = tmp_path / "pricing.json"
    eval_config.write_text("{}", encoding="utf-8")
    pricing.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("evals.model_eval.manifest.MODEL_EVAL_ROOT", module_root)
    monkeypatch.setattr("evals.model_eval.manifest.GRAPH_PATH", graph)

    def build():
        return build_model_eval_manifest(
            command="cmd",
            seed=1,
            model_config={},
            repeats=1,
            declared_repeats=1,
            split="dev",
            case_order="caseId-asc",
            config_version="v1",
            budget={},
            case_ids=[],
            eval_config_path=eval_config,
            pricing_path=pricing,
        )

    before = build()["hashes"]["modelEvalModules"]["sample.py"]
    module.write_text("VALUE = 2\n", encoding="utf-8")
    after = build()["hashes"]["modelEvalModules"]["sample.py"]
    assert before != after


def test_holdout_unseal_reads_only_fake_root(tmp_path: Path) -> None:
    dev_case = list(load_cases("dev"))[0]
    payload = dev_case.model_dump(by_alias=True)
    labels = {
        key: payload.pop(key)
        for key in (
            "relevantProductIds",
            "relevanceGrades",
            "idealOrder",
            "hardConstraints",
            "mustExcludeProductIds",
        )
    }
    labels["notes"] = payload["notes"]
    payload["split"] = "holdout"
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "buyer_holdout.jsonl").write_text(
        json.dumps(payload, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (cases / "buyer_holdout_labels.jsonl").write_text(
        json.dumps({"caseId": payload["caseId"], **labels}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"datasetHash": "fake"}),
        encoding="utf-8",
    )
    args = Namespace(
        split="holdout",
        release=True,
        unseal_reason="unit test fake root",
        commit_sha="a" * 40,
    )
    merged = load_release_cases(args, root=tmp_path)
    assert [case.case_id for case in merged] == [dev_case.case_id]
    assert (tmp_path / "audit" / "holdout_runs.jsonl").exists()


def test_runtime_budget_stop_prevents_over_limit_delegate_call() -> None:
    case = list(load_cases("dev"))[0]
    fixtures = load_evaluation_fixtures()
    budget = BudgetTracker(BudgetLimits(max_calls=2, max_total_tokens=100, max_cost_usd=1))

    class Adapter:
        model_config = {"provider": "fake"}

        def __init__(self) -> None:
            self.llm = type("Calls", (), {"calls": []})()

        def __call__(self, case, fixtures):
            del case, fixtures
            for _ in range(3):
                budget.reserve()
                call = {"inputTokens": 1, "outputTokens": 1, "costUsd": 0.01}
                self.llm.calls.append(call)
                budget.record(call)
            raise AssertionError("unreachable")

    results = run_repeats(
        adapter=Adapter(),
        cases=[case],
        fixtures=fixtures,
        repeats=1,
        budget=budget,
    )
    assert results["budgetExceeded"] is True
    assert results["caseResults"][0]["failureReason"] == "budgetExceeded"
    assert len(results["caseResults"][0]["providerCalls"]) == 2


def test_run_repeats_uses_configured_metrics_and_excludes_ranking_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = list(load_cases("dev"))[:2]
    fixtures = load_evaluation_fixtures()

    class Adapter:
        model_config = {"provider": "fake"}

        def __init__(self) -> None:
            self.llm = type("Calls", (), {"calls": []})()
            self.last_output = {
                "hardFailure": False,
                "failureReason": None,
                "providerCalls": [],
            }

    def fake_evaluate(*, cases, **kwargs):
        del kwargs
        excluded = cases[0].case_id == cases_ids[1]
        return {
            "cases": [
                {
                    "caseId": cases[0].case_id,
                    "metrics": {
                        "ndcgAtK": {"10": 0.2},
                        "recallAtK": {"5": 0.8},
                        "precisionAtK": {"5": 0.4},
                        "mrr": 0.5,
                    },
                    "filterAccuracy": 0.7,
                    "hardConstraintViolated": False,
                    "rankingExcluded": excluded,
                    "rankingExclusionReason": ("nonDiscriminativeRanking" if excluded else None),
                }
            ]
        }

    cases_ids = [case.case_id for case in cases]
    monkeypatch.setattr("evals.model_eval.repeats.evaluate", fake_evaluate)
    results = run_repeats(
        adapter=Adapter(),
        cases=cases,
        fixtures=fixtures,
        repeats=1,
        budget=BudgetTracker(BudgetLimits(10, 100, 1)),
        primary_metric="overall.recallAtK.5",
        secondary_metrics=["overall.mrr"],
        resamples=20,
        confidence=0.95,
        seed=1,
    )
    assert results["casePrimaryMetrics"] == {cases_ids[0]: [0.8]}
    assert results["rankingExcludedCases"] == [
        {"caseId": cases_ids[1], "reason": "nonDiscriminativeRanking"}
    ]
    secondary = results["secondaryMetricSummaries"]["overall.mrr"]
    assert secondary["label"] == "exploratory"
    assert secondary["caseMetrics"] == {cases_ids[0]: [0.5]}


def test_build_live_adapter_uses_explicit_runtime_model_settings() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        openai_api_key="fake",
        openai_fast_model_id="fast-candidate",
        openai_smart_model_id="smart-candidate",
        openai_fast_reasoning_effort="low",
        openai_smart_reasoning_effort="high",
        llm_timeout_s=12,
        llm_max_retries=3,
    )
    adapter = build_live_adapter(
        runtime_settings=settings,
        budget=BudgetTracker(BudgetLimits(10, 100, 1)),
        pricing=PriceBook(version="test", entries={}),
    )
    assert adapter.model_config["tiers"]["fast"]["model"] == "fast-candidate"
    assert adapter.model_config["tiers"]["smart"]["model"] == "smart-candidate"
    assert adapter.model_config["llmTimeoutS"] == 12
    assert adapter.model_config["maxRetries"] == 3
    assert adapter.model_config["searchBackend"] == "spring"
    eval_settings = adapter.settings
    assert isinstance(eval_settings, EvaluationSettings)
    assert eval_settings.search_backend == "spring"
    assert adapter.model_config["maxTokens"]["rerank"] == (
        eval_settings.rerank_max_tokens_base
        + eval_settings.rerank_max_tokens_per_item * eval_settings.expose_max
    )


def test_report_contains_primary_stats_failures_and_not_calibrated_warning(
    tmp_path: Path,
) -> None:
    results = {
        "datasetHash": "hash",
        "primaryMetric": "overall.ndcgAtK.10",
        "executedRepeats": 2,
        "uniqueCaseCount": 2,
        "caseResults": [
            {
                "repeat": 0,
                "caseId": "a",
                "hardFailure": True,
                "failureReason": "boom",
                "latencyMs": 1,
                "providerCalls": [],
            }
        ],
        "caseRepeatSummaries": {"a": {"n": 2, "mean": 0.4, "median": 0.4, "sd": 0.1, "iqr": 0.2}},
        "primarySummary": {
            "n": 1,
            "mean": 0.4,
            "median": 0.4,
            "sd": None,
            "iqr": 0.0,
            "bootstrapCi95": {"low": None, "high": None},
        },
        "secondaryMetricSummaries": {},
        "hardFailureCount": 1,
        "budgetExceeded": False,
        "coverage": {"tokenCoverage": 1.0, "costCoverage": 1.0},
        "releaseWarnings": ["quality lower bound not calibrated"],
    }
    write_artifacts(tmp_path / "out", results=results, manifest={}, regression=None)
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "Cases × repeats: 2 × 2" in report
    assert "Primary metric: `overall.ndcgAtK.10`" in report
    assert "| a | 2 | 0.400000" in report
    assert "a: boom" in report
    assert "quality lower bound not calibrated" in report


def test_main_records_sys_argv_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(sys, "argv", ["prog", "--out", str(tmp_path / "out"), "--case-limit", "1"])
    monkeypatch.setattr(
        "evals.model_eval.cli.get_settings",
        lambda: Settings(_env_file=None, openai_api_key="fake"),
    )
    monkeypatch.setattr(
        "evals.model_eval.cli.build_live_adapter",
        lambda **kwargs: type(
            "Adapter",
            (),
            {"model_config": {"provider": "fake"}, "llm": type("Calls", (), {"calls": []})()},
        )(),
    )
    monkeypatch.setattr(
        "evals.model_eval.cli.run_repeats",
        lambda **kwargs: {
            "caseResults": [],
            "casePrimaryMetrics": {},
            "caseRepeatSummaries": {},
            "primarySummary": {"mean": None},
            "secondaryMetricSummaries": {},
            "hardFailureCount": 0,
            "hardConstraintViolationRate": 0.0,
            "budgetExceeded": False,
            "budget": {},
        },
    )
    monkeypatch.setattr(
        "evals.model_eval.cli.build_model_eval_manifest",
        lambda **kwargs: captured.update(command=kwargs["command"]) or {},
    )
    monkeypatch.setattr("evals.model_eval.cli.write_artifacts", lambda *args, **kwargs: None)
    assert main(None) == 0
    assert "--case-limit 1" in captured["command"]
