"""구매자 추천 품질 평가 CLI."""

from __future__ import annotations

import argparse
from pathlib import Path

from evals.metrics.report import write_artifacts
from evals.metrics.run_manifest import build_run_manifest
from evals.metrics.runner import evaluate

DEFAULT_SEED = 20260803


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="구매자 추천 dev 골든셋 품질 평가")
    parser.add_argument("--out", type=Path, required=True, help="artifact 출력 디렉터리")
    parser.add_argument("--split", default="dev", choices=("dev", "holdout"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    if args.split != "dev":
        raise NotImplementedError("sealed holdout 평가는 #144에서 구현합니다")

    command = (
        f"uv run python -m evals.metrics --out {args.out} --split {args.split} --seed {args.seed}"
    )
    report = evaluate(split=args.split)
    manifest = build_run_manifest(command=command, seed=args.seed)
    write_artifacts(args.out, report, manifest)
    overall = report["overall"]
    print(
        f"cases={overall['caseCount']} ranking={overall['rankingCaseCount']} "
        f"hcv={overall['hardConstraintViolationRate']:.6f} out={args.out}"
    )
    return 0
