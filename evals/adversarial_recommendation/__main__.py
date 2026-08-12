"""Adversarial buyer recommendation dataset 실행 CLI."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from app.agents.buyer.recommendation.rerank_grounding import GroundingArm
from evals.adversarial_recommendation.generator import load_cases
from evals.adversarial_recommendation.report import write_run_artifacts
from evals.adversarial_recommendation.runner import (
    AdversarialBuyerRunner,
    build_live_runner,
    derive_validated_execution,
)
from evals.adversarial_recommendation.scoring import score_results

_ARMS: tuple[GroundingArm, ...] = ("current", "prompt_only", "validated")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("scripted", "live"), default="scripted")
    parser.add_argument(
        "--arms",
        default="current",
        help="current,prompt_only,validated 또는 all (기본: current)",
    )
    parser.add_argument("--out", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case-ids", help="comma-separated caseId 목록")
    selection.add_argument("--case-limit", type=int)
    return parser


def _parse_arms(raw: str) -> tuple[GroundingArm, ...]:
    if raw == "all":
        return _ARMS
    values = [value.strip() for value in raw.split(",") if value.strip()]
    if not values:
        raise ValueError("--arms에 하나 이상의 arm이 필요합니다")
    if len(values) != len(set(values)):
        raise ValueError("--arms에 중복 arm을 지정할 수 없습니다")
    unknown = [value for value in values if value not in _ARMS]
    if unknown:
        raise ValueError(f"알 수 없는 grounding arm: {unknown}")
    selected = set(values)
    return tuple(arm for arm in _ARMS if arm in selected)


def _select_cases(cases, *, case_ids: str | None, case_limit: int | None):  # noqa: ANN001
    ordered = sorted(cases, key=lambda case: case.case_id)
    if case_ids:
        requested = [case_id.strip() for case_id in case_ids.split(",") if case_id.strip()]
        by_id = {case.case_id: case for case in ordered}
        missing = [case_id for case_id in requested if case_id not in by_id]
        if missing:
            raise ValueError(f"알 수 없는 caseId: {missing}")
        return [by_id[case_id] for case_id in requested]
    if case_limit is not None:
        if case_limit <= 0:
            raise ValueError("--case-limit은 0보다 커야 합니다")
        return ordered[:case_limit]
    return ordered


async def _run(args: argparse.Namespace, *, command: list[str]) -> dict:
    cases = _select_cases(load_cases(), case_ids=args.case_ids, case_limit=args.case_limit)
    arms = _parse_arms(args.arms)
    executions_by_arm: dict[GroundingArm, list[dict]] = {}
    model_configs: dict[str, dict] = {}
    shared_decisions = {}
    decision_sources: dict[str, GroundingArm] = {}
    for arm in arms:
        if arm == "validated" and "prompt_only" in executions_by_arm:
            executions_by_arm[arm] = [
                derive_validated_execution(execution)
                for execution in executions_by_arm["prompt_only"]
            ]
            model_configs[arm] = {
                **model_configs["prompt_only"],
                "derivedFromArm": "prompt_only",
            }
            continue
        runner = (
            build_live_runner(grounding_arm=arm)
            if args.mode == "live"
            else AdversarialBuyerRunner(mode="scripted", grounding_arm=arm)
        )
        executions_by_arm[arm] = [
            await runner.run(
                case,
                decision_override=shared_decisions.get(case.case_id),
                decompose_source_arm=decision_sources.get(case.case_id),
            )
            for case in cases
        ]
        for case_id, decision in runner.decompose_decisions.items():
            if case_id not in shared_decisions:
                shared_decisions[case_id] = decision
                decision_sources[case_id] = arm
        model_configs[arm] = runner.model_config
    results = [
        result
        for arm in arms
        for result in score_results(cases, executions_by_arm[arm], mode=args.mode)
    ]
    return write_run_artifacts(
        args.out,
        cases=cases,
        results=results,
        mode=args.mode,
        model_config={"byArm": model_configs},
        command=command,
        effective_settings=runner.settings.model_dump(by_alias=True, mode="json"),
        arms=arms,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.out.exists():
        print(f"output directory already exists: {args.out}")
        return 2
    try:
        args.arms = ",".join(_parse_arms(args.arms))
        command = [
            sys.executable,
            "-m",
            "evals.adversarial_recommendation",
            *(argv if argv is not None else sys.argv[1:]),
        ]
        summary = asyncio.run(_run(args, command=command))
    except (OSError, ValueError) as exc:
        print(str(exc))
        return 2
    print(
        f"wrote {summary['caseCount']} results to {args.out} (verdicts={summary['verdictCounts']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
