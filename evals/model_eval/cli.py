"""실제 모델 추천 평가 CLI."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.llm import AnthropicLLM, OpenAILLM, resolve_provider_model
from evals.goldenset.loader import ROOT as GOLDENSET_ROOT
from evals.goldenset.loader import _merge_holdout, load_cases, unseal_holdout_labels
from evals.goldenset.schema import GoldenCase
from evals.metrics.runner import load_evaluation_fixtures
from evals.metrics.settings import EvaluationSettings
from evals.model_eval.adapter import LiveBuyerAdapter
from evals.model_eval.budget import BudgetLimits, BudgetTracker, preflight
from evals.model_eval.manifest import build_model_eval_manifest
from evals.model_eval.pricing import DEFAULT_PRICING_PATH, PriceBook, release_coverage_complete
from evals.model_eval.recording import RecordingLLM
from evals.model_eval.repeats import run_repeats, validate_metric_path
from evals.model_eval.report import write_artifacts
from evals.model_eval.stats import compare_baseline

EVAL_CONFIG_PATH = Path(__file__).with_name("eval_config.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="실제 모델 구매자 추천 품질 평가")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--split", choices=("dev", "holdout"), default="dev")
    parser.add_argument("--repeats", type=int)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case-ids")
    selection.add_argument("--case-limit", type=int)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--unseal-reason")
    parser.add_argument("--commit-sha")
    return parser


def _load_config() -> dict[str, Any]:
    return json.loads(EVAL_CONFIG_PATH.read_text(encoding="utf-8"))


def validate_eval_config(config: dict[str, Any]) -> None:
    """versioned 실험 선언의 지원 범위를 실행 전에 고정한다."""
    validate_metric_path(str(config.get("primaryMetric")))
    secondary = config.get("secondaryMetrics")
    if not isinstance(secondary, list):
        raise ValueError("secondaryMetrics는 목록이어야 합니다")
    for metric in secondary:
        validate_metric_path(str(metric))
    if config.get("caseOrder") != "caseId-asc":
        raise ValueError("지원하는 caseOrder는 caseId-asc뿐입니다")
    if config.get("missingRunPolicy") != "excludePairReportCount":
        raise ValueError("지원하는 missingRunPolicy는 excludePairReportCount뿐입니다")
    if config.get("multiplicity") != "primaryConfirmatoryOthersExploratory":
        raise ValueError("지원하는 multiplicity는 primaryConfirmatoryOthersExploratory뿐입니다")
    primary = str(config["primaryMetric"])
    if primary not in config.get("regressionMargin", {}):
        raise ValueError("primaryMetric의 regressionMargin이 없습니다")
    quality_bounds = config.get("releaseQualityLowerBound")
    if not isinstance(quality_bounds, dict) or primary not in quality_bounds:
        raise ValueError("primaryMetric의 releaseQualityLowerBound 선언이 없습니다")
    if not isinstance(config.get("releaseHardConstraintViolationMax"), int | float):
        raise ValueError("releaseHardConstraintViolationMax는 숫자여야 합니다")


def _validate_release_args(args: argparse.Namespace) -> str | None:
    if args.split == "holdout":
        if not args.release:
            return "--split holdout은 --release가 필요합니다"
        if not args.unseal_reason or not args.unseal_reason.strip():
            return "--split holdout은 --unseal-reason이 필요합니다"
        if not args.commit_sha:
            return "--split holdout은 --commit-sha가 필요합니다"
        from evals.goldenset.loader import COMMIT_SHA_RE

        if not COMMIT_SHA_RE.fullmatch(args.commit_sha):
            return "--commit-sha는 40자리 hex여야 합니다"
    return None


def load_release_cases(
    args: argparse.Namespace, *, root: Path = GOLDENSET_ROOT
) -> list[GoldenCase]:
    """승인 인자가 확인된 뒤에만 봉인 라벨을 읽어 결합한다."""
    error = _validate_release_args(args)
    if error:
        raise ValueError(error)
    cores = list(load_cases("holdout", root=root))
    labels = unseal_holdout_labels(
        reason=args.unseal_reason,
        commit_sha=args.commit_sha,
        root=root,
    )
    return _merge_holdout(cores, labels)


def _select_cases(cases: list[GoldenCase], args: argparse.Namespace) -> list[GoldenCase]:
    ordered = sorted(cases, key=lambda case: case.case_id)
    if args.case_ids:
        requested = [case_id.strip() for case_id in args.case_ids.split(",") if case_id.strip()]
        by_id = {case.case_id: case for case in ordered}
        missing = [case_id for case_id in requested if case_id not in by_id]
        if missing:
            raise ValueError(f"알 수 없는 caseId: {missing}")
        return [by_id[case_id] for case_id in requested]
    if args.case_limit is not None:
        if args.case_limit <= 0:
            raise ValueError("--case-limit은 0보다 커야 합니다")
        return ordered[: args.case_limit]
    return ordered


def _limits(settings: Settings) -> BudgetLimits:
    return BudgetLimits(
        max_calls=settings.model_eval_max_calls_per_run,
        max_total_tokens=settings.model_eval_max_total_tokens_per_run,
        max_cost_usd=settings.model_eval_max_cost_usd_per_run,
    )


def resolve_repeats(explicit: int | None, config: dict[str, Any]) -> int:
    """N 정본은 versioned config이며 CLI 인자만 실행값을 override한다."""
    return explicit if explicit is not None else int(config["repeats"])


def build_live_adapter(
    *,
    runtime_settings: Settings,
    budget: BudgetTracker,
    pricing: PriceBook,
) -> LiveBuyerAdapter:
    """배포 후보 모델 필드만 복사하고 나머지 평가 튜너블은 고정한다."""
    settings = EvaluationSettings(
        llm_provider=runtime_settings.llm_provider,
        anthropic_api_key=runtime_settings.anthropic_api_key,
        haiku_model_id=runtime_settings.haiku_model_id,
        sonnet_model_id=runtime_settings.sonnet_model_id,
        openai_api_key=runtime_settings.openai_api_key,
        openai_fast_model_id=runtime_settings.openai_fast_model_id,
        openai_smart_model_id=runtime_settings.openai_smart_model_id,
        openai_fast_reasoning_effort=runtime_settings.openai_fast_reasoning_effort,
        openai_smart_reasoning_effort=runtime_settings.openai_smart_reasoning_effort,
        llm_timeout_s=runtime_settings.llm_timeout_s,
        llm_max_retries=runtime_settings.llm_max_retries,
        auth_mode="dev",
        internal_api_token="model-eval-internal-token",
        search_backend="spring",
    )
    fast = resolve_provider_model(settings, "fast")
    smart = resolve_provider_model(settings, "smart")
    if fast.provider == "openai":
        delegate = OpenAILLM(
            fast.api_key,
            fast_model=fast.model_id,
            smart_model=smart.model_id,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            fast_reasoning_effort=fast.reasoning_effort or "",
            smart_reasoning_effort=smart.reasoning_effort or "",
        )
    else:
        delegate = AnthropicLLM(
            fast.api_key,
            fast_model=fast.model_id,
            smart_model=smart.model_id,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )
    recorder = RecordingLLM(
        delegate,
        models={"fast": fast.model_id, "smart": smart.model_id},
        reasoning_efforts={
            "fast": fast.reasoning_effort,
            "smart": smart.reasoning_effort,
        },
        pricing=pricing,
        budget=budget,
    )
    model_config = {
        "provider": fast.provider,
        "searchBackend": "spring",
        "tiers": {
            "fast": {
                "model": fast.model_id,
                "reasoningEffort": fast.reasoning_effort,
            },
            "smart": {
                "model": smart.model_id,
                "reasoningEffort": smart.reasoning_effort,
            },
        },
        "maxTokens": {
            "decompose": 800,
            "rerank": (
                settings.rerank_max_tokens_base
                + settings.rerank_max_tokens_per_item * settings.expose_max
            ),
        },
        "rerankMaxTokensBase": settings.rerank_max_tokens_base,
        "rerankMaxTokensPerItem": settings.rerank_max_tokens_per_item,
        "llmTimeoutS": settings.llm_timeout_s,
        "maxRetries": settings.llm_max_retries,
    }
    return LiveBuyerAdapter(recorder, settings=settings, model_config=model_config)


def result_exit_code(
    results: dict[str, Any],
    *,
    release: bool,
    hard_failure_max: int,
    hard_constraint_violation_max: float,
    quality_lower_bound: float | None,
    regression: dict[str, Any] | None,
) -> int:
    """부분 산출물 기록 뒤 적용할 종료 코드를 한 곳에서 결정한다."""
    if results["budgetExceeded"]:
        return 3
    release_incomplete = release and (
        not release_coverage_complete(results["coverage"])
        or results["hardFailureCount"] > hard_failure_max
        or results["hardConstraintViolationRate"] > hard_constraint_violation_max
        or (
            quality_lower_bound is not None
            and (
                not isinstance(results.get("primarySummary", {}).get("mean"), int | float)
                or results["primarySummary"]["mean"] < quality_lower_bound
            )
        )
        or (regression is not None and regression["verdict"] != "pass")
    )
    if release_incomplete or (regression and regression["verdict"] == "regression"):
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = _load_config()
    try:
        validate_eval_config(config)
    except ValueError as exc:
        print(str(exc))
        return 2
    error = _validate_release_args(args)
    if error:
        print(error)
        return 2
    repeats = resolve_repeats(args.repeats, config)
    if repeats < 1:
        print("--repeats는 1 이상이어야 합니다")
        return 2
    if not args.dry_run and args.out.exists():
        print(f"출력 디렉터리가 이미 존재합니다: {args.out}")
        return 2

    try:
        raw_cases = list(load_cases("dev")) if args.split == "dev" else load_release_cases(args)
        cases = _select_cases(raw_cases, args)
    except ValueError as exc:
        print(str(exc))
        return 2

    # 예산은 측정값이 아니라 운영 안전장치라 runtime env override를 허용하고 manifest에 박는다.
    runtime_settings = get_settings()
    limits = _limits(runtime_settings)
    prediction = preflight(case_count=len(cases), repeats=repeats, limits=limits)
    print(json.dumps(prediction, ensure_ascii=False, sort_keys=True))
    if not prediction["allowed"]:
        return 2
    if args.dry_run:
        return 0

    pricing = PriceBook.load()
    budget = BudgetTracker(limits)
    try:
        adapter = build_live_adapter(
            runtime_settings=runtime_settings,
            budget=budget,
            pricing=pricing,
        )
    except Exception as exc:  # 미구성 키도 사전 거부로 명확히 표면화
        print(f"LLM 설정 오류: {exc}")
        return 2

    fixtures = load_evaluation_fixtures()
    results = run_repeats(
        adapter=adapter,
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
    results["datasetVersion"] = fixtures.manifest["datasetVersion"]
    results["datasetHash"] = fixtures.manifest["datasetHash"]
    calls = [call for row in results["caseResults"] for call in row.get("providerCalls", [])]
    results["coverage"] = pricing.coverage(calls)
    results["configVersion"] = config["configVersion"]
    results["missingRunPolicy"] = config["missingRunPolicy"]
    results["multiplicity"] = config["multiplicity"]
    quality_bound = config["releaseQualityLowerBound"].get(config["primaryMetric"])
    results["releaseWarnings"] = (
        ["quality lower bound not calibrated"] if quality_bound is None else []
    )

    regression = None
    if args.baseline:
        baseline = json.loads((args.baseline / "results.json").read_text(encoding="utf-8"))
        try:
            regression = compare_baseline(
                results,
                baseline,
                margin=float(config["regressionMargin"][config["primaryMetric"]]),
                resamples=int(config["bootstrap"]["resamples"]),
                confidence=float(config["bootstrap"]["confidence"]),
                seed=int(config["seed"]),
                multiplicity=config["multiplicity"],
                missing_run_policy=config["missingRunPolicy"],
            )
        except ValueError as exc:
            print(str(exc))
            return 2
    results["regression"] = regression

    command_args = argv if argv is not None else sys.argv[1:]
    command = "uv run python -m evals.model_eval " + " ".join(
        shlex.quote(arg) for arg in command_args
    )
    manifest = build_model_eval_manifest(
        command=command,
        seed=int(config["seed"]),
        model_config=adapter.model_config,
        repeats=repeats,
        declared_repeats=int(config["repeats"]),
        split=args.split,
        case_order=config["caseOrder"],
        config_version=config["configVersion"],
        budget={
            "maxCalls": limits.max_calls,
            "maxTotalTokens": limits.max_total_tokens,
            "maxCostUsd": limits.max_cost_usd,
        },
        case_ids=[case.case_id for case in cases],
        eval_config_path=EVAL_CONFIG_PATH,
        pricing_path=DEFAULT_PRICING_PATH,
    )
    write_artifacts(args.out, results=results, manifest=manifest, regression=regression)

    verdict = regression["verdict"] if regression else "notCompared"
    print(f"verdict={verdict}")
    return result_exit_code(
        results,
        release=args.release,
        hard_failure_max=int(config["hardFailureMax"]),
        hard_constraint_violation_max=float(config["releaseHardConstraintViolationMax"]),
        quality_lower_bound=(
            float(quality_bound) if isinstance(quality_bound, int | float) else None
        ),
        regression=regression,
    )
