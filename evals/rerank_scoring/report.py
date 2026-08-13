"""Immutable, reconstructable artifacts for rerank-scoring evaluation."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.agents.buyer.recommendation.rerank_code_assisted import CodeAssistedDecision
from app.agents.buyer.recommendation.rerank_scoring import RankingDecision
from evals.rerank_scoring.metrics import score_run
from evals.rerank_scoring.schema import RankingFailure, RankingProbeRun, RankingSample

_SAMPLE_FIELDS = [
    "caseId",
    "arm",
    "orderSeed",
    "repeat",
    "attempt",
    "candidateOrder",
    "rankedProductIds",
    "top3ProductIds",
    "top1ProductId",
    "latencyMs",
    "rawResponseSha256",
    "providerCalled",
    "rankingDecisions",
    "codeAssistedDecisions",
    "relevanceGrades",
    "hardConstraints",
    "mustExcludeProductIds",
    "slices",
    "foreignEvaluationCount",
    "duplicateEvaluationCount",
    "invalidScoreCount",
    "evaluatedCoverage",
    "partialFallback",
    "fullFallback",
    "hardConstraintViolationCount",
    "inputTokens",
    "outputTokens",
    "costUsd",
    "usageUnknownReason",
    "ndcgAt10",
    "datasetHash",
    "promptHash",
    "modelConfigJson",
]
_FAILURE_FIELDS = [
    "caseId",
    "arm",
    "orderSeed",
    "repeat",
    "attempt",
    "errorType",
    "message",
    "fullFallback",
]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _sample_row(sample: RankingSample) -> dict[str, object]:
    return {
        "caseId": sample.case_id,
        "arm": sample.arm,
        "orderSeed": sample.order_seed,
        "repeat": sample.repeat,
        "attempt": sample.attempt,
        "candidateOrder": _json(sample.candidate_order),
        "rankedProductIds": _json(sample.ranked_product_ids),
        "top3ProductIds": _json(sample.top3_product_ids),
        "top1ProductId": "" if sample.top1_product_id is None else sample.top1_product_id,
        "latencyMs": sample.latency_ms,
        "rawResponseSha256": sample.raw_response_sha256,
        "providerCalled": _bool(sample.provider_called),
        "rankingDecisions": _json([asdict(value) for value in sample.ranking_decisions]),
        "codeAssistedDecisions": _json([asdict(value) for value in sample.code_assisted_decisions]),
        "relevanceGrades": _json(sample.relevance_grades),
        "hardConstraints": _json(sample.hard_constraints),
        "mustExcludeProductIds": _json(sample.must_exclude_product_ids),
        "slices": _json(sample.slices),
        "foreignEvaluationCount": sample.foreign_evaluation_count,
        "duplicateEvaluationCount": sample.duplicate_evaluation_count,
        "invalidScoreCount": sample.invalid_score_count,
        "evaluatedCoverage": sample.evaluated_coverage,
        "partialFallback": _bool(sample.partial_fallback),
        "fullFallback": _bool(sample.full_fallback),
        "hardConstraintViolationCount": sample.hard_constraint_violation_count,
        "inputTokens": "" if sample.input_tokens is None else sample.input_tokens,
        "outputTokens": "" if sample.output_tokens is None else sample.output_tokens,
        "costUsd": "" if sample.cost_usd is None else sample.cost_usd,
        "usageUnknownReason": sample.usage_unknown_reason or "",
        "ndcgAt10": "" if sample.ndcg_at_10 is None else sample.ndcg_at_10,
        "datasetHash": sample.dataset_hash,
        "promptHash": sample.prompt_hash,
        "modelConfigJson": sample.model_config_json,
    }


def _write_samples(path: Path, run: RankingProbeRun) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_SAMPLE_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(_sample_row(sample) for sample in run.samples)


def _write_failures(path: Path, run: RankingProbeRun) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FAILURE_FIELDS, lineterminator="\n")
        writer.writeheader()
        for failure in run.failures:
            writer.writerow(
                {
                    "caseId": failure.case_id,
                    "arm": failure.arm,
                    "orderSeed": failure.order_seed,
                    "repeat": failure.repeat,
                    "attempt": failure.attempt,
                    "errorType": failure.error_type,
                    "message": failure.message,
                    "fullFallback": _bool(failure.full_fallback),
                }
            )


def _validate_manifest(run: RankingProbeRun, manifest: dict[str, Any]) -> None:
    if manifest.get("datasetHash") != run.dataset_hash:
        raise ValueError("manifest and run dataset hashes differ")
    expected_model = _json(manifest.get("modelConfig") or {})
    sample_models = {sample.model_config_json for sample in run.samples if sample.model_config_json}
    if sample_models and sample_models != {expected_model}:
        raise ValueError("mixed model config or manifest mismatch")
    prompt_hashes = manifest.get("promptHashes")
    if not isinstance(prompt_hashes, dict) or not prompt_hashes:
        raise ValueError("promptHashes must be recorded")
    for sample in run.samples:
        expected_prompt = prompt_hashes.get(sample.arm)
        if sample.prompt_hash and expected_prompt != sample.prompt_hash:
            raise ValueError("mixed prompt hash or manifest mismatch")


def render_report(results: dict[str, Any], manifest: dict[str, Any]) -> str:
    primary = results.get("primaryComparison")
    comparison = results["comparisons"].get(primary) if primary is not None else None
    lines = [
        "# Rerank scoring paired evaluation",
        "",
        f"- status: `{results['status']}`",
        f"- dataset: `{manifest['datasetVersion']}` / `{manifest['datasetHash']}`",
        f"- arms: `{','.join(results['arms'])}`",
        f"- repeats/seeds: `{results['repeats']}` / `{results['orderSeeds']}`",
        f"- dry-run: `{manifest.get('dryRun', False)}`",
        f"- label status: `{manifest.get('labelStatus', 'unspecified')}`",
        f"- confirmatory: `{manifest.get('confirmatory', False)}`",
        "",
        f"## Primary comparison: {primary or 'not-tested'}",
        "",
        "| paired N | mean ΔnDCG@10 | CI low | CI high | verdict | statistical verdict |",
        "|---:|---:|---:|---:|---|---|",
        (
            f"| {comparison['pairedCount']} | {comparison['meanDelta']} | "
            f"{comparison['bootstrapCi95']['low']} | {comparison['bootstrapCi95']['high']} | "
            f"{comparison['verdict']} | "
            f"{comparison.get('statisticalVerdict', comparison['verdict'])} |"
            if comparison is not None
            else "| 0 | None | None | None | not-tested | not-tested |"
        ),
        "",
        "## Integrity",
        "",
        "| arm | samples | hard violations | foreign rows | duplicates | partial/full fallback |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for arm in results["arms"]:
        value = results["integrity"][arm]
        lines.append(
            f"| {arm} | {value['sampleCount']} | "
            f"{value['hardConstraintViolation']['numerator']} | "
            f"{value['foreignEvaluation']['numerator']} | "
            f"{value['duplicateEvaluation']['numerator']} | "
            f"{value['partialFallback']['numerator']}/{value['fullFallback']['numerator']} |"
        )
    if manifest.get("dryRun") is True:
        claim_note = (
            "Dry-run verifies the harness only; it is never evidence of production quality."
        )
    elif manifest.get("labelStatus") == "draft":
        claim_note = (
            "Heuristic draft labels make this exploratory only; it is not confirmatory evidence."
        )
    else:
        claim_note = "Interpret this run under the label and release status recorded above."
    lines += [
        "",
        claim_note,
        "Initial 4:2:1 weights and RRF alpha/k remain experimental until a live paired run supports them.",
        "",
    ]
    return "\n".join(lines)


def score_artifacts(run: RankingProbeRun, manifest: dict[str, Any]) -> dict[str, Any]:
    """Recompute the exact persisted result, including dry-run claim suppression."""

    _validate_manifest(run, manifest)
    results = score_run(run)
    if manifest.get("dryRun") is True:
        return {
            **results,
            "status": "not-tested",
            "comparisons": {
                name: {**comparison, "verdict": "not-tested"}
                for name, comparison in results["comparisons"].items()
            },
        }
    if manifest.get("labelStatus") == "draft":
        return {
            **results,
            "status": "exploratory",
            "comparisons": {
                name: {
                    **comparison,
                    "statisticalVerdict": comparison["verdict"],
                    "verdict": ("exploratory" if comparison["pairedCount"] else "not-tested"),
                }
                for name, comparison in results["comparisons"].items()
            },
        }
    return results


def write_artifacts(out: Path, *, run: RankingProbeRun, manifest: dict[str, Any]) -> None:
    """Write exactly five immutable files after provenance consistency checks."""

    if out.exists():
        raise FileExistsError(f"output directory already exists: {out}")
    results = score_artifacts(run, manifest)
    out.mkdir(parents=True)
    _write_samples(out / "samples.csv", run)
    _write_failures(out / "failures.csv", run)
    _write_json(out / "results.json", results)
    _write_json(out / "run_manifest.json", manifest)
    (out / "report.md").write_text(render_report(results, manifest), encoding="utf-8", newline="\n")


def _optional_int(value: str) -> int | None:
    return None if value == "" else int(value)


def _optional_float(value: str) -> float | None:
    return None if value == "" else float(value)


def _load_sample(row: dict[str, str]) -> RankingSample:
    decisions = tuple(RankingDecision(**value) for value in json.loads(row["rankingDecisions"]))
    code_assisted_decisions = tuple(
        CodeAssistedDecision(**value)
        for value in json.loads(row.get("codeAssistedDecisions") or "[]")
    )
    return RankingSample(
        case_id=row["caseId"],
        arm=row["arm"],  # type: ignore[arg-type]
        order_seed=int(row["orderSeed"]),
        repeat=int(row["repeat"]),
        attempt=int(row["attempt"]),
        candidate_order=tuple(json.loads(row["candidateOrder"])),
        ranked_product_ids=tuple(json.loads(row["rankedProductIds"])),
        top3_product_ids=tuple(json.loads(row["top3ProductIds"])),
        top1_product_id=_optional_int(row["top1ProductId"]),
        latency_ms=int(row["latencyMs"]),
        raw_response_sha256=row["rawResponseSha256"],
        provider_called=row["providerCalled"] == "true",
        ranking_decisions=decisions,
        grounding_decisions=(),
        relevance_grades={
            int(key): int(value) for key, value in json.loads(row["relevanceGrades"]).items()
        },
        hard_constraints=json.loads(row["hardConstraints"]),
        must_exclude_product_ids=tuple(json.loads(row["mustExcludeProductIds"])),
        slices=tuple(json.loads(row["slices"])),
        code_assisted_decisions=code_assisted_decisions,
        foreign_evaluation_count=int(row["foreignEvaluationCount"]),
        duplicate_evaluation_count=int(row["duplicateEvaluationCount"]),
        invalid_score_count=int(row["invalidScoreCount"]),
        evaluated_coverage=float(row["evaluatedCoverage"]),
        partial_fallback=row["partialFallback"] == "true",
        full_fallback=row["fullFallback"] == "true",
        hard_constraint_violation_count=int(row["hardConstraintViolationCount"]),
        input_tokens=_optional_int(row["inputTokens"]),
        output_tokens=_optional_int(row["outputTokens"]),
        cost_usd=_optional_float(row["costUsd"]),
        usage_unknown_reason=row["usageUnknownReason"] or None,
        ndcg_at_10=_optional_float(row["ndcgAt10"]),
        dataset_hash=row["datasetHash"],
        prompt_hash=row["promptHash"],
        model_config_json=row["modelConfigJson"],
    )


def load_run_from_artifacts(
    samples_path: Path,
    failures_path: Path,
    manifest: dict[str, Any],
) -> RankingProbeRun:
    """Reconstruct every metric input from the two raw CSV files."""

    with samples_path.open(encoding="utf-8", newline="") as handle:
        samples = tuple(_load_sample(row) for row in csv.DictReader(handle))
    with failures_path.open(encoding="utf-8", newline="") as handle:
        failures = tuple(
            RankingFailure(
                case_id=row["caseId"],
                arm=row["arm"],  # type: ignore[arg-type]
                order_seed=int(row["orderSeed"]),
                repeat=int(row["repeat"]),
                attempt=int(row["attempt"]),
                error_type=row["errorType"],
                message=row["message"],
                full_fallback=row["fullFallback"] == "true",
            )
            for row in csv.DictReader(handle)
        )
    arms = tuple(dict.fromkeys(sample.arm for sample in samples)) or tuple(
        manifest.get("arms") or ()
    )
    return RankingProbeRun(
        samples=samples,
        failures=failures,
        arms=arms,  # type: ignore[arg-type]
        repeats=int(manifest["repeats"]),
        order_seeds=tuple(int(value) for value in manifest["orderSeeds"]),
        dataset_version=str(manifest["datasetVersion"]),
        dataset_hash=str(manifest["datasetHash"]),
        grounding_arm=str(manifest["groundingArm"]),  # type: ignore[arg-type]
        alpha=float(manifest["alpha"]),
        k=int(manifest["k"]),
    )
