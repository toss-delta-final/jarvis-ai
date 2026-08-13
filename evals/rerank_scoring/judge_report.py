"""Artifact serialization, reproduction, and reporting for rerank blind judging."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from evals.rerank_scoring.judge import analyze_judgments
from evals.rerank_scoring.judge_schema import (
    BlindPresentation,
    CoordinatorMapping,
    JudgeFailure,
    JudgeResponse,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stable_json(value: object, *, indent: int | None = None) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=None if indent is not None else (",", ":"),
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(f"{_stable_json(value, indent=2)}\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[BaseModel], *, sort_field: str) -> None:
    payloads = [row.model_dump(by_alias=True, mode="json") for row in rows]
    payloads.sort(key=lambda row: str(row[sort_field]))
    path.write_text("".join(f"{_stable_json(row)}\n" for row in payloads), encoding="utf-8")


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


def _artifact_provenance(root: Path, manifest: Mapping[str, Any]) -> dict[str, str]:
    source = manifest["source"]
    judge = manifest["judge"]
    return {
        "sourceSamplesSha256": str(source["samplesSha256"]),
        "datasetManifestSha256": str(source["datasetManifestSha256"]),
        "judgePromptSha256": str(judge["promptSha256"]),
        "presentationsSha256": sha256_file(root / "presentations.jsonl"),
        "judgeResponsesSha256": sha256_file(root / "judge_responses.jsonl"),
        "coordinatorMappingSha256": sha256_file(root / "coordinator_mapping.jsonl"),
        "failuresSha256": sha256_file(root / "failures.jsonl"),
        "runManifestSha256": sha256_file(root / "run_manifest.json"),
    }


def render_report(results: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    coverage = results["coverage"]
    outcomes = results["pairOutcomes"]
    decisive = results["decisive"]
    clustered = results["caseClusteredPreferenceShare"]
    position = results["positionBias"]
    judge = manifest["judge"]
    source = manifest["source"]
    ci = clustered["ci95"]
    lines = [
        "# Rerank LLM Blind Judge",
        "",
        "**Status: exploratory — not confirmatory.**",
        "",
        f"- Judge: `{judge['provider']} / {judge['model']}` (`{judge['tier']}` tier)",
        f"- Source generation model: `{source.get('generationModel') or 'unknown'}`",
        f"- Same judge/source model: `{str(judge['sameAsSourceGenerationModel']).lower()}`",
        f"- Source pairs/cases: {coverage['plannedPairs']} / {source['caseCount']}",
        f"- Completed presentations: {coverage['completedPresentations']} / "
        f"{coverage['plannedPresentations']}",
        "",
        "## Swap-stable results",
        "",
        f"- Structured wins: {outcomes['structured']}",
        f"- Current wins: {outcomes['current']}",
        f"- Stable ties: {outcomes['tie']}",
        f"- Position-swap unstable: {outcomes['unstable']}",
        f"- Swap consistency: {results['swapConsistencyRate']:.4f}",
        f"- Structured decisive win rate: "
        f"{decisive['structuredWinRate']:.4f} (n={decisive['denominator']})"
        if decisive["structuredWinRate"] is not None
        else "- Structured decisive win rate: unavailable",
        f"- Case-clustered preference share: {clustered['value']:.4f}; "
        f"95% bootstrap CI [{ci[0]:.4f}, {ci[1]:.4f}]"
        if clustered["value"] is not None
        else "- Case-clustered preference share: unavailable",
        "",
        "## Position diagnostics",
        "",
        f"- Same-side A selections: {position['sameSideASelections']}",
        f"- Same-side B selections: {position['sameSideBSelections']}",
        f"- Same-display-side rate: {position['sameDisplaySideRate']:.4f}"
        if position["sameDisplaySideRate"] is not None
        else "- Same-display-side rate: unavailable",
        "",
        "## Limitations",
        "",
        "The judge saw anonymous A/B rankings, neutral product facts, query, and profile only. It did "
        "not receive arm names, component scores, search rank, candidate provenance, heuristic "
        "relevance labels, or ideal orders.",
        "",
        "This is synthetic LLM-judge evidence. It does not establish population superiority, online "
        "conversion lift, or confirmatory ranking quality. Human blind review is still required for "
        "confirmatory evidence.",
        "",
    ]
    return "\n".join(lines)


def write_artifacts(
    root: Path,
    *,
    presentations: Sequence[BlindPresentation],
    responses: Sequence[JudgeResponse],
    mappings: Sequence[CoordinatorMapping],
    failures: Sequence[JudgeFailure],
    analysis: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    if root.exists():
        raise ValueError(f"output already exists: {root}")
    root.mkdir(parents=True)
    _write_jsonl(root / "presentations.jsonl", presentations, sort_field="presentationId")
    _write_jsonl(root / "judge_responses.jsonl", responses, sort_field="presentationId")
    _write_jsonl(root / "coordinator_mapping.jsonl", mappings, sort_field="presentationId")
    _write_jsonl(root / "failures.jsonl", failures, sort_field="presentationId")
    _write_json(root / "run_manifest.json", dict(manifest))
    results = {**analysis, "artifacts": _artifact_provenance(root, manifest)}
    _write_json(root / "results.json", results)
    (root / "report.md").write_text(render_report(results, manifest), encoding="utf-8")
    return results


def reproduce_analysis(root: Path) -> dict[str, Any]:
    manifest = _load_json(root / "run_manifest.json")
    mappings = _load_jsonl(root / "coordinator_mapping.jsonl", CoordinatorMapping)
    responses = _load_jsonl(root / "judge_responses.jsonl", JudgeResponse)
    analysis_config = manifest["analysis"]
    analysis = analyze_judgments(
        responses,
        mappings,
        bootstrap_seed=int(analysis_config["bootstrapSeed"]),
        bootstrap_samples=int(analysis_config["bootstrapSamples"]),
    )
    return {**analysis, "artifacts": _artifact_provenance(root, manifest)}
