"""CLI for paired current/structured/hybrid/code-assisted rerank evaluation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from app.agents.buyer.recommendation.rerank import (
    _SYSTEM,
    _SYSTEM_CODE_ASSISTED,
    _SYSTEM_STRUCTURED_GROUNDING,
    _SYSTEM_STRUCTURED_SCORING,
)
from app.agents.buyer.recommendation.rerank_grounding import GroundingArm
from app.agents.buyer.recommendation.rerank_scoring import RankingArm
from app.core.config import get_settings
from evals.goldenset.loader import load_cases
from evals.intent_probe.client import PacedLLM, build_live_delegate
from evals.intent_probe.pacer import GlobalPacer, PacerLimits
from evals.metrics.run_manifest import build_run_manifest
from evals.metrics.runner import load_evaluation_fixtures
from evals.model_eval.budget import BudgetExceeded, BudgetLimits, BudgetTracker
from evals.model_eval.pricing import DEFAULT_PRICING_PATH, PriceBook
from evals.rerank_holdout_v2.adapter import build_case_input as build_holdout_case_input
from evals.rerank_holdout_v2.io import ROOT as HOLDOUT_ROOT
from evals.rerank_holdout_v2.io import load_dataset as load_holdout_dataset
from evals.rerank_scoring.fakes import ScriptedScoringLLM
from evals.rerank_scoring.report import write_artifacts
from evals.rerank_scoring.runner import run_input_probe, run_probe

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_BUDGET = 3
ALL_ARMS: tuple[RankingArm, ...] = (
    "current",
    "structured",
    "hybrid",
    "code_assisted",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="buyer rerank scoring paired 평가")
    parser.add_argument(
        "--arms",
        required=True,
        help="all 또는 current,structured,hybrid,code_assisted",
    )
    parser.add_argument(
        "--dataset",
        default="goldenset-dev",
        choices=("goldenset-dev", "rerank-holdout-v2"),
    )
    parser.add_argument(
        "--dataset-root",
        type=Path,
        help="rerank-holdout-v2 draft 또는 sealed release root",
    )
    parser.add_argument("--split", default="dev", choices=("dev",))
    parser.add_argument("--case-ids", help="caseId 쉼표 목록")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--attempt-multiplier", type=int, default=3)
    parser.add_argument("--order-seeds", default="11,29,47")
    parser.add_argument("--alpha", type=float, default=0.65)
    parser.add_argument("--k", type=int, default=60)
    parser.add_argument(
        "--grounding-arm",
        choices=("current", "prompt_only", "validated"),
        default="validated",
    )
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--rpm", type=int, default=PacerLimits.max_rpm)
    parser.add_argument("--tpm", type=int, default=PacerLimits.max_tpm)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-draft-live",
        action="store_true",
        help="heuristic draft를 명시적으로 exploratory live 평가한다",
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser


def _parse_arms(raw: str) -> tuple[RankingArm, ...]:
    if raw == "all":
        return ALL_ARMS
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if not values or len(values) != len(set(values)) or not set(values) <= set(ALL_ARMS):
        raise ValueError(
            "--arms must be all or a unique current,structured,hybrid,code_assisted list"
        )
    return cast(tuple[RankingArm, ...], values)


def _parse_order_seeds(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(int(value.strip()) for value in raw.split(",") if value.strip())
    except ValueError as exc:
        raise ValueError("--order-seeds must contain integers") from exc
    if not values or len(values) != len(set(values)):
        raise ValueError("--order-seeds must be a non-empty unique integer list")
    return values


def _filter_cases(cases: Sequence[Any], raw: str | None):
    if raw is None:
        return tuple(cases)
    requested = {value.strip() for value in raw.split(",") if value.strip()}
    known = {case.case_id for case in cases}
    missing = sorted(requested - known)
    if not requested or missing:
        raise ValueError(f"unknown case IDs: {missing or sorted(requested)}")
    return tuple(case for case in cases if case.case_id in requested)


def _select_cases(raw: str | None):
    return _filter_cases(tuple(load_cases("dev")), raw)


def _prompt_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _command(argv: list[str]) -> str:
    return "uv run python -m evals.rerank_scoring " + " ".join(shlex.quote(value) for value in argv)


def _manifest(
    *,
    command: str,
    dry_run: bool,
    arms: tuple[RankingArm, ...],
    case_ids: Sequence[str],
    dataset_name: str,
    dataset_version: str,
    dataset_hash: str,
    label_status: str,
    confirmatory: bool,
    repeats: int,
    attempt_multiplier: int,
    order_seeds: tuple[int, ...],
    alpha: float,
    k: int,
    grounding_arm: GroundingArm,
    model_config: dict[str, object],
    budget: dict[str, object],
    pacer: dict[str, object],
) -> dict[str, object]:
    base = build_run_manifest(command=command, seed=order_seeds[0])
    current_prompt = _SYSTEM if grounding_arm == "current" else _SYSTEM_STRUCTURED_GROUNDING
    scored_hash = _prompt_hash(_SYSTEM_STRUCTURED_SCORING)
    prompt_hashes = {
        "current": _prompt_hash(current_prompt),
        "structured": scored_hash,
        "hybrid": scored_hash,
    }
    if "code_assisted" in arms:
        prompt_hashes["code_assisted"] = _prompt_hash(_SYSTEM_CODE_ASSISTED)
    manifest: dict[str, object] = {
        "gitCommit": base["commitSha"],
        "dirty": base["dirty"],
        "command": command,
        "dryRun": dry_run,
        "arms": list(arms),
        "caseIds": list(case_ids),
        "dataset": dataset_name,
        "datasetVersion": dataset_version,
        "datasetHash": dataset_hash,
        "labelStatus": label_status,
        "confirmatory": confirmatory,
        "promptHashes": prompt_hashes,
        "modelConfig": model_config,
        "repeats": repeats,
        "attemptMultiplier": attempt_multiplier,
        "orderSeeds": list(order_seeds),
        "alpha": alpha,
        "k": k,
        "componentWeights": {"intentFit": 4, "needFit": 2, "profileFit": 1},
        "groundingArm": grounding_arm,
        "budget": budget,
        "pacer": pacer,
    }
    if "code_assisted" in arms:
        manifest["codeAssistedComponentOwners"] = {
            "code": [
                "ratingQuality",
                "reviewConfidence",
                "explicitConditionCoverage",
                "evidence",
                "searchRank",
            ],
            "llm": ["semanticIntentFit", "useCaseFit", "profileFit", "finalSelection"],
        }
    return manifest


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    if args.out.exists():
        print(f"--out directory already exists: {args.out}")
        return EXIT_REJECTED
    try:
        arms = _parse_arms(args.arms)
        order_seeds = _parse_order_seeds(args.order_seeds)
        if (
            args.repeats <= 0
            or args.attempt_multiplier <= 0
            or not 0 <= args.alpha <= 1
            or args.k <= 0
            or args.rpm <= 0
            or args.tpm <= 0
        ):
            raise ValueError("numeric limits are outside their valid ranges")
        if args.allow_draft_live and (args.dataset != "rerank-holdout-v2" or args.dry_run):
            raise ValueError("--allow-draft-live requires a non-dry rerank-holdout-v2 run")
        if args.dataset == "goldenset-dev":
            if args.dataset_root is not None:
                raise ValueError("--dataset-root requires --dataset rerank-holdout-v2")
            cases = _select_cases(args.case_ids)
            fixtures = load_evaluation_fixtures()
            case_inputs = None
            dataset_version = str(fixtures.manifest.get("datasetVersion") or "unknown")
            dataset_hash = str(fixtures.manifest.get("datasetHash") or "unknown")
            label_status = "model"
            confirmatory = False
        else:
            label_policy = "draft" if args.dry_run or args.allow_draft_live else "sealed"
            holdout = load_holdout_dataset(
                args.dataset_root or HOLDOUT_ROOT,
                label_policy=label_policy,
            )
            if (
                not args.dry_run
                and not args.allow_draft_live
                and (
                    holdout.manifest.label_status != "sealed"
                    or not holdout.manifest.confirmatory_eligible
                )
            ):
                raise ValueError("sealed labels required for confirmatory evaluation")
            cases = _filter_cases(holdout.ranking_cases, args.case_ids)
            case_inputs = tuple(
                build_holdout_case_input(
                    case,
                    holdout.labels_by_case[case.case_id],
                    holdout.catalog,
                )
                for case in cases
            )
            fixtures = None
            dataset_version = str(holdout.manifest.dataset_version)
            dataset_hash = str(
                getattr(
                    holdout.manifest,
                    "dataset_hash",
                    holdout.manifest.catalog_sha256,
                )
            )
            label_status = str(holdout.manifest.label_status)
            confirmatory = bool(holdout.manifest.confirmatory_eligible)
    except ValueError as exc:
        print(f"input rejected: {exc}")
        return EXIT_REJECTED

    provider_calls_per_cell = (
        int("current" in arms)
        + int(bool({"structured", "hybrid"} & set(arms)))
        + int("code_assisted" in arms)
    )
    expected_calls = len(cases) * len(order_seeds) * args.repeats * provider_calls_per_cell
    max_calls = (
        args.max_calls if args.max_calls is not None else expected_calls * args.attempt_multiplier
    )
    if max_calls <= 0 or expected_calls > max_calls:
        print(f"expected calls {expected_calls} exceed max calls {max_calls}")
        return EXIT_REJECTED
    settings = get_settings()
    max_cost_usd = (
        args.max_cost_usd
        if args.max_cost_usd is not None
        else settings.model_eval_max_cost_usd_per_run
    )
    if max_cost_usd <= 0:
        print("--max-cost-usd must be positive")
        return EXIT_REJECTED
    budget = BudgetTracker(
        BudgetLimits(
            max_calls=max_calls,
            max_total_tokens=settings.model_eval_max_total_tokens_per_run,
            max_cost_usd=max_cost_usd,
        )
    )
    pacer = GlobalPacer(PacerLimits(max_rpm=args.rpm, max_tpm=args.tpm))
    if args.dry_run:
        llm = ScriptedScoringLLM()
        model_config = dict(llm.model_config)
    else:
        try:
            delegate, live_config = build_live_delegate(
                runtime_settings=settings,
                budget=budget,
                pricing=PriceBook.load(DEFAULT_PRICING_PATH),
            )
        except Exception as exc:  # noqa: BLE001 - live preflight failure is reported
            print(f"LLM configuration error: {exc}")
            return EXIT_REJECTED
        llm = PacedLLM(delegate, pacer=pacer)
        model_config = dict(live_config)
        llm.model_config = model_config

    try:
        if case_inputs is None:
            assert fixtures is not None
            run = asyncio.run(
                run_probe(
                    llm,
                    cases=cases,
                    fixtures=fixtures,
                    arms=arms,
                    repeats=args.repeats,
                    attempt_multiplier=args.attempt_multiplier,
                    order_seeds=order_seeds,
                    grounding_arm=args.grounding_arm,
                    expose_max=9,
                    alpha=args.alpha,
                    k=args.k,
                )
            )
        else:
            run = asyncio.run(
                run_input_probe(
                    llm,
                    case_inputs=case_inputs,
                    dataset_version=dataset_version,
                    dataset_hash=dataset_hash,
                    arms=arms,
                    repeats=args.repeats,
                    attempt_multiplier=args.attempt_multiplier,
                    order_seeds=order_seeds,
                    grounding_arm=args.grounding_arm,
                    expose_max=9,
                    alpha=args.alpha,
                    k=args.k,
                )
            )
    except BudgetExceeded as exc:
        print(f"budget exceeded: {exc}")
        return EXIT_BUDGET

    manifest = _manifest(
        command=_command(argv),
        dry_run=args.dry_run,
        arms=arms,
        case_ids=[case.case_id for case in cases],
        dataset_name=args.dataset,
        dataset_version=dataset_version,
        dataset_hash=dataset_hash,
        label_status=label_status,
        confirmatory=confirmatory,
        repeats=args.repeats,
        attempt_multiplier=args.attempt_multiplier,
        order_seeds=order_seeds,
        alpha=args.alpha,
        k=args.k,
        grounding_arm=args.grounding_arm,
        model_config=model_config,
        budget=budget.snapshot(),
        pacer=pacer.snapshot(),
    )
    write_artifacts(args.out, run=run, manifest=manifest)
    print(
        f"arms={','.join(arms)} cases={len(cases)} samples={len(run.samples)} "
        f"failures={len(run.failures)} out={args.out}"
    )
    return EXIT_OK
