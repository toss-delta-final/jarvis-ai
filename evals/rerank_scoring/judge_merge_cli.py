"""Merge disjoint rerank blind-judge shards with provenance validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from evals.metrics.run_manifest import build_run_manifest
from evals.rerank_scoring.judge import analyze_judgments
from evals.rerank_scoring.judge_report import sha256_file, write_artifacts
from evals.rerank_scoring.judge_schema import (
    BlindPresentation,
    CoordinatorMapping,
    JudgeFailure,
    JudgeResponse,
)

EXIT_OK = 0
EXIT_REJECTED = 2
ModelT = TypeVar("ModelT", bound=BaseModel)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="merge disjoint rerank blind-judge shards")
    parser.add_argument("--shards", required=True, help="comma-separated shard directories")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def _load_jsonl(path: Path, model: type[ModelT]) -> tuple[ModelT, ...]:
    rows: list[ModelT] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                rows.append(model.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f"{path}:{line_number}: invalid row: {exc}") from exc
    return tuple(rows)


def _load_presentations(
    path: Path, mappings: Sequence[CoordinatorMapping]
) -> tuple[BlindPresentation, ...]:
    orientation_by_id = {row.presentation_id: row.orientation for row in mappings}
    rows: list[BlindPresentation] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                presentation_id = str(payload["presentationId"])
                payload["orientation"] = orientation_by_id[presentation_id]
                rows.append(BlindPresentation.model_validate(payload))
            except Exception as exc:
                raise ValueError(f"{path}:{line_number}: invalid row: {exc}") from exc
    return tuple(rows)


def _unique_by_presentation(rows: Sequence[ModelT], *, label: str) -> tuple[ModelT, ...]:
    seen: set[str] = set()
    for row in rows:
        presentation_id = str(getattr(row, "presentation_id"))
        if presentation_id in seen:
            raise ValueError(f"overlapping {label}: {presentation_id}")
        seen.add(presentation_id)
    return tuple(rows)


def _same_value(manifests: Sequence[dict[str, Any]], path: tuple[str, ...]) -> object:
    values: list[object] = []
    for manifest in manifests:
        value: object = manifest
        for key in path:
            if not isinstance(value, dict) or key not in value:
                raise ValueError(f"manifest missing {'.'.join(path)}")
            value = value[key]
        values.append(value)
    first = values[0]
    if any(value != first for value in values[1:]):
        raise ValueError(f"shard mismatch: {'.'.join(path)}")
    return first


def _command(argv: Sequence[str]) -> str:
    return "uv run python -m evals.rerank_scoring.judge_merge_cli " + " ".join(
        shlex.quote(value) for value in argv
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    if args.out.exists():
        print(f"--out already exists: {args.out}")
        return EXIT_REJECTED
    shards = tuple(Path(value.strip()) for value in args.shards.split(",") if value.strip())
    if not shards:
        print("input rejected: no shards")
        return EXIT_REJECTED
    try:
        manifests = tuple(_load_json(shard / "run_manifest.json") for shard in shards)
        for path in (
            ("source", "samplesSha256"),
            ("source", "datasetManifestSha256"),
            ("source", "datasetHash"),
            ("judge", "provider"),
            ("judge", "model"),
            ("judge", "tier"),
            ("judge", "promptSha256"),
            ("judge", "mappingSeed"),
            ("analysis", "bootstrapSeed"),
            ("analysis", "bootstrapSamples"),
        ):
            _same_value(manifests, path)
        mappings = _unique_by_presentation(
            tuple(
                row
                for shard in shards
                for row in _load_jsonl(shard / "coordinator_mapping.jsonl", CoordinatorMapping)
            ),
            label="mapping",
        )
        mappings_by_shard = {
            shard: _load_jsonl(shard / "coordinator_mapping.jsonl", CoordinatorMapping)
            for shard in shards
        }
        presentations = _unique_by_presentation(
            tuple(
                row
                for shard in shards
                for row in _load_presentations(
                    shard / "presentations.jsonl", mappings_by_shard[shard]
                )
            ),
            label="presentation",
        )
        responses = _unique_by_presentation(
            tuple(
                row
                for shard in shards
                for row in _load_jsonl(shard / "judge_responses.jsonl", JudgeResponse)
            ),
            label="response",
        )
        failures = tuple(
            row for shard in shards for row in _load_jsonl(shard / "failures.jsonl", JudgeFailure)
        )
        presentation_ids = {row.presentation_id for row in presentations}
        mapping_ids = {row.presentation_id for row in mappings}
        response_ids = {row.presentation_id for row in responses}
        if presentation_ids != mapping_ids or not response_ids <= presentation_ids:
            raise ValueError("shards have inconsistent presentation/mapping/response coverage")
    except (OSError, ValueError) as exc:
        print(f"input rejected: {exc}")
        return EXIT_REJECTED

    bootstrap_seed = int(manifests[0]["analysis"]["bootstrapSeed"])
    bootstrap_samples = int(manifests[0]["analysis"]["bootstrapSamples"])
    analysis = analyze_judgments(
        responses,
        mappings,
        bootstrap_seed=bootstrap_seed,
        bootstrap_samples=bootstrap_samples,
    )
    base = build_run_manifest(
        command=_command(argv),
        seed=int(manifests[0]["judge"]["mappingSeed"]),
    )
    source = dict(manifests[0]["source"])
    source["pairCount"] = len({row.pair_id for row in mappings})
    source["caseCount"] = len({row.case_id for row in mappings})
    total_budget = {
        "callCount": sum(int(manifest["budget"]["callCount"]) for manifest in manifests),
        "totalTokens": sum(int(manifest["budget"]["totalTokens"]) for manifest in manifests),
        "totalCostUsd": sum(float(manifest["budget"]["totalCostUsd"]) for manifest in manifests),
        "unknownTokenCallCount": sum(
            int(manifest["budget"]["unknownTokenCallCount"]) for manifest in manifests
        ),
        "unknownCostCallCount": sum(
            int(manifest["budget"]["unknownCostCallCount"]) for manifest in manifests
        ),
    }
    manifest = {
        **base,
        "schemaVersion": "rerank-blind-merged-run-v1",
        "evidenceStatus": "exploratory",
        "source": source,
        "judge": dict(manifests[0]["judge"]),
        "analysis": dict(manifests[0]["analysis"]),
        "budget": total_budget,
        "pacer": {"scope": "per-shard", "shardCount": len(shards)},
        "failureAttemptCount": len(failures),
        "merge": {
            "shardCount": len(shards),
            "shardManifestSha256": {
                str(shard): sha256_file(shard / "run_manifest.json") for shard in shards
            },
            "combinedInputSha256": hashlib.sha256(
                "\n".join(sorted(str(shard.resolve()) for shard in shards)).encode()
            ).hexdigest(),
        },
    }
    try:
        write_artifacts(
            args.out,
            presentations=presentations,
            responses=responses,
            mappings=mappings,
            failures=failures,
            analysis=analysis,
            manifest=manifest,
        )
    except (OSError, ValueError) as exc:
        print(f"artifact write failed: {exc}")
        return EXIT_REJECTED
    print(
        f"shards={len(shards)} pairs={len({row.pair_id for row in mappings})} "
        f"presentations={len(presentations)} responses={len(responses)} out={args.out}"
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
