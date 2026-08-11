"""#153 평가 자산 준비·분석 CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.blind_pairwise.analysis import reproduce_analysis
from evals.blind_pairwise.design import (
    PairInput,
    generate_assignments,
    write_assignments,
    write_public_assignments_by_evaluator,
)
from evals.blind_pairwise.preregistration import (
    CONFIG_PATH,
    load_preregistration,
    sha256_file,
    validate_preregistration,
)
from evals.blind_pairwise.report import render_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare or analyze blind buyer pairwise evals")
    subparsers = parser.add_subparsers(dest="command", required=True)

    assign = subparsers.add_parser("assign", help="generate coordinator and public assignments")
    assign.add_argument("--pairs", type=Path, required=True, help="JSONL pair inputs")
    assign.add_argument("--evaluators", required=True, help="comma-separated eval-* aliases")
    assign.add_argument("--seed", type=int, required=True)
    assign.add_argument("--coordinator-out", type=Path, required=True)
    assign.add_argument("--public-dir", type=Path, default=None)
    assign.add_argument("--public-out", type=Path, default=None, help=argparse.SUPPRESS)
    assign.add_argument("--ratings-per-pair", type=int, default=3)
    assign.add_argument("--preregistration", type=Path, default=CONFIG_PATH)

    analyze = subparsers.add_parser("analyze", help="analyze actual human JSONL responses")
    analyze.add_argument("--raw", type=Path, required=True)
    analyze.add_argument("--assignments", type=Path, required=True)
    analyze.add_argument("--out", type=Path, required=True)
    analyze.add_argument("--preregistration", type=Path, default=CONFIG_PATH)
    analyze.add_argument("--judge", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "assign":
        pairs = _load_pairs(args.pairs)
        preregistration = load_preregistration(args.preregistration)
        validate_preregistration(preregistration)
        if args.seed != preregistration["seed"]:
            raise ValueError("seed differs from frozen preregistration")
        if len(pairs) != preregistration["pairCount"]:
            raise ValueError("pair input count differs from frozen preregistration")
        if args.ratings_per_pair != preregistration["ratingsPerPair"]:
            raise ValueError("ratings_per_pair differs from frozen preregistration")
        evaluator_ids = tuple(value.strip() for value in args.evaluators.split(",") if value.strip())
        if len(set(evaluator_ids)) < preregistration["minimumEligibleEvaluators"]:
            raise ValueError("eligible evaluator count is below frozen preregistration")
        assignments = generate_assignments(
            pairs,
            evaluator_ids=evaluator_ids,
            ratings_per_pair=args.ratings_per_pair,
            seed=args.seed,
        )
        public_dir = args.public_dir
        if public_dir is None:
            if args.public_out is not None:
                raise ValueError("use --public-dir; one public artifact for all evaluators is forbidden")
            raise ValueError("--public-dir is required")
        routes = write_public_assignments_by_evaluator(public_dir, assignments)
        args.coordinator_out.parent.mkdir(parents=True, exist_ok=True)
        write_assignments(
            args.coordinator_out,
            assignments,
            seed=args.seed,
            pair_input_sha256=sha256_file(args.pairs),
            preregistration_sha256=sha256_file(args.preregistration),
            pair_count=preregistration["pairCount"],
            ratings_per_pair=preregistration["ratingsPerPair"],
            minimum_eligible_evaluators=preregistration["minimumEligibleEvaluators"],
            confidence=preregistration["confidence"],
            routing={evaluator_id: path.name for evaluator_id, path in routes.items()},
        )
        return 0
    if args.command == "analyze":
        result = reproduce_analysis(
            args.raw,
            args.assignments,
            preregistration_path=args.preregistration,
            llm_judge_path=args.judge,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        report_path = args.out.with_suffix(".md")
        report_path.write_text(render_report(result), encoding="utf-8", newline="\n")
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


def _load_pairs(path: Path) -> list[PairInput]:
    pairs: list[PairInput] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        expected = {"pairId", "prompt", "baselineText", "recommendationV2Text"}
        if not isinstance(row, dict) or set(row) != expected:
            raise ValueError(f"pair input line {line_number} has invalid fields")
        pairs.append(
            PairInput(
                pair_id=row["pairId"],
                prompt=row["prompt"],
                baseline_text=row["baselineText"],
                recommendation_v2_text=row["recommendationV2Text"],
            )
        )
    return pairs


__all__ = ["build_parser", "main"]
