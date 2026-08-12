"""Adversarial buyer recommendation dataset 실행 CLI."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from evals.adversarial_recommendation.generator import load_cases
from evals.adversarial_recommendation.report import write_run_artifacts
from evals.adversarial_recommendation.runner import AdversarialBuyerRunner, build_live_runner
from evals.adversarial_recommendation.scoring import score_results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("scripted", "live"), default="scripted")
    parser.add_argument("--out", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case-ids", help="comma-separated caseId 목록")
    selection.add_argument("--case-limit", type=int)
    return parser


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
    runner = build_live_runner() if args.mode == "live" else AdversarialBuyerRunner(mode="scripted")
    executions = [await runner.run(case) for case in cases]
    results = score_results(cases, executions, mode=args.mode)
    return write_run_artifacts(
        args.out,
        cases=cases,
        results=results,
        mode=args.mode,
        model_config=runner.model_config,
        command=command,
        effective_settings=runner.settings.model_dump(by_alias=True, mode="json"),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.out.exists():
        print(f"output directory already exists: {args.out}")
        return 2
    try:
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
