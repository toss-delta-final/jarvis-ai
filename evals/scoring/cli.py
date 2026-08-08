"""동일 dev 케이스를 passthrough/scoring 두 arm으로 실행하는 paired CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from evals.goldenset.loader import load_cases
from evals.metrics.report import normalize_artifacts, write_artifacts
from evals.metrics.run_manifest import build_run_manifest, strip_volatile_manifest_keys
from evals.metrics.runner import evaluate, load_evaluation_fixtures
from evals.scoring.adapter import ReferenceClockBuyerAdapter, ScoringBuyerAdapter
from evals.scoring.embeddings import FIXTURE_PATH

DEFAULT_SEED = 20260803


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(_json_text(value) + "\n", encoding="utf-8", newline="\n")


class _TimedAdapter:
    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self.model_config = adapter.model_config
        self.latency_ms: dict[str, float] = {}

    def __call__(self, case, fixtures):
        started = time.perf_counter()
        try:
            return self.adapter(case, fixtures)
        finally:
            self.latency_ms[case.case_id] = (time.perf_counter() - started) * 1000


def _delta(scoring: object, passthrough: object) -> object:
    if isinstance(scoring, dict) and isinstance(passthrough, dict):
        return {
            key: _delta(scoring[key], passthrough[key])
            for key in sorted(scoring.keys() & passthrough.keys())
            if isinstance(scoring[key], (int, float, dict))
            and not isinstance(scoring[key], bool)
        }
    if isinstance(scoring, (int, float)) and isinstance(passthrough, (int, float)):
        return scoring - passthrough
    return {}


def _comparison(passthrough: dict[str, Any], scoring: dict[str, Any]) -> dict[str, object]:
    return {
        "passthrough": passthrough["overall"],
        "scoring": scoring["overall"],
        "delta": _delta(scoring["overall"], passthrough["overall"]),
    }


def _comparison_markdown(comparison: dict[str, object]) -> str:
    passthrough = comparison["passthrough"]
    scoring = comparison["scoring"]
    delta = comparison["delta"]
    rows = [
        ("nDCG@10", passthrough["ndcgAtK"]["10"], scoring["ndcgAtK"]["10"], delta["ndcgAtK"]["10"]),
        ("MRR", passthrough["mrr"], scoring["mrr"], delta["mrr"]),
        (
            "Precision@10",
            passthrough["precisionAtK"]["10"],
            scoring["precisionAtK"]["10"],
            delta["precisionAtK"]["10"],
        ),
        (
            "Recall@10",
            passthrough["recallAtK"]["10"],
            scoring["recallAtK"]["10"],
            delta["recallAtK"]["10"],
        ),
        ("Diversity", passthrough["diversity"], scoring["diversity"], delta["diversity"]),
    ]
    lines = [
        "# Scoring baseline paired 비교",
        "",
        "| metric | passthrough | scoring | delta |",
        "|---|---:|---:|---:|",
    ]
    lines.extend(
        f"| {name} | {left:.6f} | {right:.6f} | {change:+.6f} |"
        for name, left, right, change in rows
    )
    return "\n".join(lines) + "\n"


def normalize_paired_artifacts(output_dir: Path) -> dict[str, bytes]:
    """arm별 #143 정규화에 더해 paired runtime latency/run·commitSha·dirty 메타를 제외한다."""
    normalized: dict[str, bytes] = {}
    for arm in ("passthrough", "scoring"):
        for name, content in normalize_artifacts(output_dir / arm).items():
            normalized[f"{arm}/{name}"] = content
    for name in ("comparison.json", "comparison.md", "scores_scoring.json"):
        normalized[name] = (output_dir / name).read_bytes()
    manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
    normalized["run_manifest.json"] = (
        _json_text(strip_volatile_manifest_keys(manifest)) + "\n"
    ).encode()
    return normalized


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="추천 scoring baseline paired dev 평가")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--split", default="dev", choices=("dev", "holdout"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    if args.split != "dev":
        raise NotImplementedError("sealed holdout 평가는 #144에서 구현합니다")

    args.out.mkdir(parents=True, exist_ok=True)
    cases = list(load_cases("dev"))
    fixtures = load_evaluation_fixtures()
    passthrough_adapter = _TimedAdapter(ReferenceClockBuyerAdapter())
    scoring = ScoringBuyerAdapter()
    scoring_adapter = _TimedAdapter(scoring)
    passthrough_report = evaluate(
        adapter=passthrough_adapter, cases=cases, fixtures=fixtures
    )
    scoring_report = evaluate(adapter=scoring_adapter, cases=cases, fixtures=fixtures)
    for arm, report in (
        ("passthrough", passthrough_report),
        ("scoring", scoring_report),
    ):
        command = (
            f"uv run python -m evals.scoring --out {args.out} "
            f"--split {args.split} --seed {args.seed}"
        )
        write_artifacts(
            args.out / arm,
            report,
            build_run_manifest(command=command, seed=args.seed),
        )

    comparison = _comparison(passthrough_report, scoring_report)
    _write_json(args.out / "comparison.json", comparison)
    (args.out / "comparison.md").write_text(
        _comparison_markdown(comparison), encoding="utf-8", newline="\n"
    )
    _write_json(
        args.out / "scores_scoring.json",
        [scoring.score_records[case_id] for case_id in sorted(scoring.score_records)],
    )
    _write_json(
        args.out / "latency.json",
        {
            "passthroughMs": passthrough_adapter.latency_ms,
            "scoringMs": scoring_adapter.latency_ms,
        },
    )
    manifest = {
        "run": {
            "command": f"uv run python -m evals.scoring --out {args.out}",
        },
        "datasetHash": fixtures.manifest["datasetHash"],
        "datasetVersion": fixtures.manifest["datasetVersion"],
        "seed": args.seed,
        "embeddingsSha256": hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(),
        "arms": {
            "passthrough": passthrough_report["modelConfig"],
            "scoring": scoring_report["modelConfig"],
        },
        "rankingExcludedCaseIds": scoring_report["overall"]["rankingExcludedCaseIds"],
    }
    _write_json(args.out / "run_manifest.json", manifest)
    overall = scoring_report["overall"]
    print(
        f"cases={overall['caseCount']} ranking={overall['rankingCaseCount']} "
        f"ndcg10={overall['ndcgAtK']['10']:.6f} mrr={overall['mrr']:.6f} out={args.out}"
    )
    return 0
