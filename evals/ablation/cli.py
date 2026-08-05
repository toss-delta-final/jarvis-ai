"""3-arm 추천 pipeline ablation CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import statistics
import sys
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from evals.ablation.adapters import ScoringAblationAdapter
from evals.ablation.budget import preflight_ablation
from evals.ablation.config import CONFIG_PATH, load_ablation_config, validate_ablation_config
from evals.ablation.report import summarize_resource_axes, write_top_level_artifacts
from evals.ablation.single_call import SINGLE_CALL_SYSTEM_PROMPT, SingleCallBuyerAdapter
from evals.ablation.stats import paired_comparisons
from evals.goldenset.loader import load_cases
from evals.goldenset.schema import GoldenCase
from evals.metrics.runner import EvaluationFixtures, load_evaluation_fixtures
from evals.metrics.settings import EvaluationSettings
from evals.model_eval.budget import BudgetLimits, BudgetTracker
from evals.model_eval.cli import build_live_adapter
from evals.model_eval.manifest import build_model_eval_manifest
from evals.model_eval.pricing import DEFAULT_PRICING_PATH, PriceBook
from evals.model_eval.recording import RecordingLLM
from evals.model_eval.repeats import run_repeats
from evals.model_eval.report import write_artifacts

PAIRS = (("pipeline", "scoring"), ("pipeline", "single_call"), ("single_call", "scoring"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="추천 pipeline 3-arm paired ablation")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repeats", type=int)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case-limit", type=int)
    selection.add_argument("--case-ids")
    return parser


def _select_cases(
    cases: list[GoldenCase], args: argparse.Namespace, *, test_type_filter: str | None
) -> list[GoldenCase]:
    if test_type_filter is not None:
        cases = [case for case in cases if case.test_type == test_type_filter]
    ordered = sorted(cases, key=lambda case: case.case_id)
    if args.case_ids:
        requested = [value.strip() for value in args.case_ids.split(",") if value.strip()]
        by_id = {case.case_id: case for case in ordered}
        missing = [case_id for case_id in requested if case_id not in by_id]
        if missing:
            raise ValueError(f"알 수 없는 caseId: {missing}")
        return sorted((by_id[case_id] for case_id in requested), key=lambda case: case.case_id)
    if args.case_limit is not None:
        if args.case_limit < 1:
            raise ValueError("--case-limit은 1 이상이어야 합니다")
        return ordered[: args.case_limit]
    return ordered


def _limits(settings: Settings) -> BudgetLimits:
    return BudgetLimits(
        max_calls=settings.model_eval_max_calls_per_run,
        max_total_tokens=settings.model_eval_max_total_tokens_per_run,
        max_cost_usd=settings.model_eval_max_cost_usd_per_run,
    )


class _UsageMessage:
    def __init__(self) -> None:
        self.usage_metadata = {"input_tokens": 100, "output_tokens": 20}
        self.response_metadata: dict[str, object] = {}


class _DryRunLLM:
    """골든 케이스와 고정 후보만 소비하는 네트워크 없는 deterministic LLM."""

    def __init__(self, cases: list[GoldenCase]) -> None:
        self.by_query = {case.query: case for case in cases}

    def _case(self, user: str) -> GoldenCase:
        for query, case in self.by_query.items():
            if query in user:
                return case
        raise ValueError("dry-run prompt에서 case query를 찾지 못했습니다")

    @staticmethod
    def _candidate_ids(user: str) -> list[int]:
        raw = user.rsplit("CANDIDATES: ", 1)[1]
        candidates = json.loads(raw)
        return [int(item["productId"]) for item in candidates]

    async def complete(self, **kwargs) -> str:
        from app.core import llm

        llm._record_usage(_UsageMessage(), "gpt-5.6-luna")
        user = str(kwargs["user"])
        case = self._case(user)
        if kwargs["system"] == SINGLE_CALL_SYSTEM_PROMPT:
            return json.dumps(
                {
                    "extractedFilters": dict(case.expected_filters),
                    "ranked": [
                        {"productId": product_id, "reason": "고정 후보 기반 추천"}
                        for product_id in self._candidate_ids(user)
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        if kwargs["tier"] == "fast":
            filters = dict(case.expected_filters)
            semantic_query = filters.pop("semanticQuery", case.query)
            return json.dumps(
                {
                    "intent": case.expected_route,
                    "reply": "",
                    "case": 2,
                    "semanticQuery": semantic_query,
                    "categoryQueries": [],
                    "filters": filters,
                    "cart": {"productId": None, "optionId": None, "quantity": 1},
                    "revertCategories": [],
                    "repurchaseProducts": [],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        return json.dumps(
            {
                "ranked": [
                    {"productId": product_id, "rationale": "고정 후보 기반 추천"}
                    for product_id in self._candidate_ids(user)
                ],
                "overallComment": "dry-run 고정 응답",
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    async def stream(self, **kwargs):
        del kwargs
        yield "dry-run"


def _dry_adapters(
    *,
    cases: list[GoldenCase],
    budget: BudgetTracker,
    pricing: PriceBook,
) -> dict[str, Any]:
    settings = EvaluationSettings(
        auth_mode="dev",
        internal_api_token="ablation-dry-internal-token",
        search_backend="spring",
    )
    recorder = RecordingLLM(
        _DryRunLLM(cases),
        models={"fast": "gpt-5.6-luna", "smart": "gpt-5.6-luna"},
        reasoning_efforts={"fast": "dry-run", "smart": "dry-run"},
        pricing=pricing,
        budget=budget,
    )
    pipeline_config = {
        "provider": "fake",
        "searchBackend": "spring",
        "tiers": {
            "fast": {"model": "gpt-5.6-luna", "reasoningEffort": "dry-run"},
            "smart": {"model": "gpt-5.6-luna", "reasoningEffort": "dry-run"},
        },
    }
    from evals.model_eval.adapter import LiveBuyerAdapter

    return {
        "pipeline": LiveBuyerAdapter(
            recorder, settings=settings, model_config=pipeline_config
        ),
        "scoring": ScoringAblationAdapter(),
        "single_call": SingleCallBuyerAdapter(
            recorder,
            settings=settings,
            model_config={
                "provider": "fake",
                "tier": "smart",
                "model": "gpt-5.6-luna",
                "reasoningEffort": "dry-run",
                "purchaseHistoryIncluded": False,
            },
        ),
    }


def _live_adapters(
    *, runtime_settings: Settings, budget: BudgetTracker, pricing: PriceBook
) -> dict[str, Any]:
    pipeline = build_live_adapter(
        runtime_settings=runtime_settings, budget=budget, pricing=pricing
    )
    smart = pipeline.model_config["tiers"]["smart"]
    return {
        "pipeline": pipeline,
        "scoring": ScoringAblationAdapter(),
        "single_call": SingleCallBuyerAdapter(
            pipeline.llm,
            settings=pipeline.settings,
            model_config={
                "provider": pipeline.model_config["provider"],
                "tier": "smart",
                "model": smart["model"],
                "reasoningEffort": smart["reasoningEffort"],
                "maxTokens": (
                    pipeline.settings.rerank_max_tokens_base
                    + pipeline.settings.rerank_max_tokens_per_item
                    * pipeline.settings.expose_max
                ),
                "purchaseHistoryIncluded": False,
            },
        ),
    }


def _run_arms(
    *,
    adapters: dict[str, Any],
    cases: list[GoldenCase],
    fixtures: EvaluationFixtures,
    repeats: int,
    config: dict[str, Any],
    budget: BudgetTracker,
    pricing: PriceBook,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for arm in config["arms"]:
        result = run_repeats(
            adapter=adapters[arm],
            cases=cases,
            fixtures=fixtures,
            repeats=repeats,
            budget=budget,
            primary_metric=config["primaryMetric"],
            secondary_metrics=config["secondaryMetrics"],
            resamples=int(config["bootstrap"]["resamples"]),
            confidence=float(config["bootstrap"]["confidence"]),
            seed=int(config["seed"]),
        )
        result.update(
            {
                "arm": arm,
                "datasetVersion": fixtures.manifest["datasetVersion"],
                "datasetHash": fixtures.manifest["datasetHash"],
                "configVersion": config["configVersion"],
                "missingRunPolicy": config["missingRunPolicy"],
                "multiplicity": config["multiplicity"],
                "deterministicRepeatedForPairing": arm == "scoring",
            }
        )
        calls = [
            call
            for row in result["caseResults"]
            for call in row.get("providerCalls", [])
        ]
        result["coverage"] = (
            {**pricing.coverage(calls), "applicability": "measured"}
            if calls
            else {
                "tokenCoverage": None,
                "costCoverage": None,
                "applicability": "notApplicableNoLlmCalls",
            }
        )
        results[arm] = result
    return results


def _comparison(
    results: dict[str, dict[str, Any]],
    config: dict[str, Any],
    pricing: PriceBook,
    *,
    all_case_ids: list[str],
) -> dict[str, Any]:
    arm_summaries: dict[str, dict[str, Any]] = {}
    for arm, result in results.items():
        case_samples = {
            case_id: [float(value) for value in values]
            for case_id, values in result.get("casePrimaryMetrics", {}).items()
            if values
        }
        case_means = {
            case_id: statistics.fmean(values) for case_id, values in case_samples.items()
        }
        case_slices: dict[str, set[str]] = {}
        failure_rows: set[tuple[str, str]] = set()
        filter_parse_warning_count = 0
        for row in result.get("caseResults", []):
            case_id = str(row.get("caseId"))
            metrics = row.get("metrics")
            if isinstance(metrics, dict):
                case_slices.setdefault(case_id, set()).update(
                    str(value) for value in metrics.get("slices", [])
                )
            if row.get("failureReason"):
                failure_rows.add((case_id, str(row["failureReason"])))
            warnings = row.get("filterParseWarnings")
            if isinstance(warnings, list):
                filter_parse_warning_count += len(warnings)
        slices = sorted({value for values in case_slices.values() for value in values})
        slice_summaries = {}
        for slice_name in slices:
            values = [
                case_means[case_id]
                for case_id, tags in case_slices.items()
                if slice_name in tags and case_id in case_means
            ]
            slice_summaries[slice_name] = {
                "primaryMean": statistics.fmean(values) if values else None,
                "n": len(values),
                "label": "exploratory",
            }
        secondary = {
            metric: {
                "mean": payload.get("overallSummary", {}).get("mean"),
                "n": payload.get("overallSummary", {}).get("n", 0),
                "label": "exploratory",
            }
            for metric, payload in result.get("secondaryMetricSummaries", {}).items()
        }
        arm_summaries[arm] = {
            "primaryMean": result.get("primarySummary", {}).get("mean"),
            "secondaryMetrics": secondary,
            "hardFailureCount": result.get("hardFailureCount", 0),
            "filterParseWarningCount": filter_parse_warning_count,
            "hardConstraintViolationRate": result.get(
                "hardConstraintViolationRate", 0.0
            ),
            "rankingExcludedCaseCount": len(result.get("rankingExcludedCases", [])),
            "slices": slice_summaries,
            "failureCases": [
                {"caseId": case_id, "failureReason": reason}
                for case_id, reason in sorted(failure_rows)
            ],
            "resources": summarize_resource_axes(result, pricing),
        }
    return {
        "primaryMetric": config["primaryMetric"],
        "primaryLabel": "confirmatory",
        "secondaryLabel": "exploratory",
        "arms": arm_summaries,
        "hardConstraintViolationRateDenominator": (
            "hardFailure 행도 evaluated metric row 분모에 포함되어 위반율이 희석될 수 있음; "
            "hardFailureCount를 함께 해석"
        ),
        "pairedComparisons": paired_comparisons(
            results,
            all_case_ids=all_case_ids,
            pairs=PAIRS,
            resamples=int(config["bootstrap"]["resamples"]),
            confidence=float(config["bootstrap"]["confidence"]),
            seed=int(config["seed"]),
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = load_ablation_config()
    try:
        validate_ablation_config(config)
        repeats = args.repeats if args.repeats is not None else int(config["repeats"])
        if repeats < 1:
            raise ValueError("--repeats는 1 이상이어야 합니다")
        if args.out.exists():
            raise ValueError(f"출력 디렉터리가 이미 존재합니다: {args.out}")
        cases = _select_cases(
            list(load_cases("dev")), args, test_type_filter=config.get("caseTestTypeFilter")
        )
    except ValueError as exc:
        print(str(exc))
        return 2

    runtime_settings = EvaluationSettings() if args.dry_run else get_settings()
    limits = _limits(runtime_settings)
    prediction = preflight_ablation(
        case_count=len(cases), repeats=repeats, limits=limits
    )
    print(json.dumps(prediction, ensure_ascii=False, sort_keys=True))
    if not prediction["allowed"]:
        return 2

    fixtures = load_evaluation_fixtures()
    pricing = PriceBook.load()
    budget = BudgetTracker(limits)
    try:
        adapters = (
            _dry_adapters(cases=cases, budget=budget, pricing=pricing)
            if args.dry_run
            else _live_adapters(
                runtime_settings=runtime_settings, budget=budget, pricing=pricing
            )
        )
    except Exception as exc:  # 미구성 provider는 네트워크 전 명확히 거부
        print(f"LLM 설정 오류: {exc}")
        return 2

    args.out.mkdir(parents=True)
    results = _run_arms(
        adapters=adapters,
        cases=cases,
        fixtures=fixtures,
        repeats=repeats,
        config=config,
        budget=budget,
        pricing=pricing,
    )
    for arm, result in results.items():
        write_artifacts(args.out / arm, results=result, manifest={}, regression=None)

    comparison = _comparison(
        results,
        config,
        pricing,
        all_case_ids=[case.case_id for case in cases],
    )
    command_args = argv if argv is not None else sys.argv[1:]
    command = "uv run python -m evals.ablation " + " ".join(
        shlex.quote(value) for value in command_args
    )
    manifest = build_model_eval_manifest(
        command=command,
        seed=int(config["seed"]),
        model_config={arm: adapter.model_config for arm, adapter in adapters.items()},
        repeats=repeats,
        declared_repeats=int(config["repeats"]),
        split="dev",
        case_order=config["caseOrder"],
        config_version=config["configVersion"],
        budget={
            "limits": {
                "maxCalls": limits.max_calls,
                "maxTotalTokens": limits.max_total_tokens,
                "maxCostUsd": limits.max_cost_usd,
            },
            "preflight": prediction,
            "actual": budget.snapshot(),
        },
        case_ids=[case.case_id for case in cases],
        eval_config_path=CONFIG_PATH,
        pricing_path=DEFAULT_PRICING_PATH,
    )
    manifest["ablation"] = {
        "arms": list(config["arms"]),
        "caseTestTypeFilter": config.get("caseTestTypeFilter"),
        "primaryMetric": config["primaryMetric"],
        "primaryLabel": "confirmatory",
        "secondaryLabel": "exploratory",
        "rankingExclusionPolicy": config["rankingExclusionPolicy"],
        "missingRunPolicy": config["missingRunPolicy"],
        "inconclusiveRule": config["inconclusiveRule"],
        "datasetVersion": fixtures.manifest["datasetVersion"],
        "datasetHash": fixtures.manifest["datasetHash"],
        "scoringConfound": "scripted expectedFilters decompose; pipeline과 rerank 축만 다른 비교가 아님",
        "scoringRepeatedForPairedCondition": True,
        "singleCallPurchaseHistoryIncluded": False,
        "totalLatencyDefinition": "adapter 전체 벽시계 latencyMs",
        "ttft": {
            "value": None,
            "status": "unknown",
            "reason": "오프라인 하네스는 server_first_text_token_ms를 직접 관측하지 않음",
        },
    }
    hashes = manifest.setdefault("hashes", {})
    hashes["singleCallPrompt"] = hashlib.sha256(
        SINGLE_CALL_SYSTEM_PROMPT.encode()
    ).hexdigest()
    ablation_root = Path(__file__).resolve().parent
    hashes["ablationModules"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(ablation_root.glob("*.py"))
    }
    write_top_level_artifacts(args.out, comparison=comparison, manifest=manifest)
    print(f"artifacts={args.out}")
    if any(result["budgetExceeded"] for result in results.values()):
        return 3
    if any(not results[arm].get("casePrimaryMetrics") for arm in ("pipeline", "single_call")):
        return 4
    return 0
