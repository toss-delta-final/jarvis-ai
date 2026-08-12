"""Regenerable JSON/CSV/Markdown artifacts for rerank grounding runs."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from evals.rerank_grounding.metrics import ArmMetrics, score_samples
from evals.rerank_grounding.runner import ProbeRun, Sample


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _latency(run: ProbeRun, arm: str) -> dict[str, int | None]:
    values = [
        sample.latency_ms for cell in run.cells for sample in cell.samples if sample.arm == arm
    ]
    return {"p50Ms": _percentile(values, 0.5), "p95Ms": _percentile(values, 0.95)}


def _inv_mismatch_count(run: ProbeRun, arm: str) -> int:
    by_pair_and_index: dict[tuple[str, int], list[Sample]] = {}
    for cell in run.cells:
        for sample in cell.samples:
            if sample.arm == arm and sample.pair_id is not None:
                by_pair_and_index.setdefault((sample.pair_id, sample.sample_index), []).append(
                    sample
                )
    mismatches = 0
    for samples in by_pair_and_index.values():
        if len(samples) != 2:
            continue
        left, right = samples
        if (left.ranked_product_ids, left.displayed_rationales) != (
            right.ranked_product_ids,
            right.displayed_rationales,
        ):
            mismatches += 1
    return mismatches


def _status(*, run: ProbeRun, manifest: dict[str, Any], metrics: dict[str, ArmMetrics]) -> str:
    if manifest.get("dryRun") is True:
        return "not tested"
    if run.unfilled_cells():
        return "inconclusive"
    validated = metrics.get("validated")
    if validated is None:
        return "not tested"
    current = metrics.get("current")
    coverage_floor = (
        current.valid_rank_coverage - 0.05
        if current is not None and current.valid_rank_coverage is not None
        else None
    )
    failed = (
        validated.unsupported_evidence_rate != 0.0
        or validated.out_of_candidate_id_count != 0
        or validated.duplicate_id_count != 0
        or validated.invalid_structured_evidence_count != 0
        or validated.detected_overall_claim_violation_rate != 0.0
        or validated.overall_invalid_structured_claim_count != 0
        or validated.supported_overall_claim_coverage is None
        or (
            coverage_floor is not None
            and (
                validated.valid_rank_coverage is None
                or validated.valid_rank_coverage < coverage_floor
            )
        )
    )
    return "rejected" if failed else "supported"


def build_results(run: ProbeRun, manifest: dict[str, Any]) -> dict[str, Any]:
    metrics = score_samples(run.metric_samples())
    return {
        "status": _status(run=run, manifest=manifest, metrics=metrics),
        "fixtureVersion": run.fixture_version,
        "arms": list(run.arms),
        "repeats": run.repeats,
        "metrics": {arm: value.as_dict() for arm, value in metrics.items()},
        "latency": {arm: _latency(run, arm) for arm in run.arms},
        "invMismatchCount": {arm: _inv_mismatch_count(run, arm) for arm in run.arms},
        "unfilledCells": run.unfilled_cells(),
        "failureCount": len(run.failures()),
        "limits": [
            "rating/review/relative-price와 정확한 숫자 주장만 자동 판정한다.",
            "overall comment는 등록된 표현군과 구조화 claim code만 자동 판정한다.",
            "검색 관련성이나 전체 자연어 진실성을 증명하지 않는다.",
            "dry-run은 실행기 검증이며 live 품질 근거가 아니다.",
        ],
    }


def render_report(results: dict[str, Any], manifest: dict[str, Any]) -> str:
    lines = [
        "# Buyer rerank grounding experiment",
        "",
        (
            f"status={results['status']} · commit={manifest.get('gitCommit')} · "
            f"dataset={manifest.get('datasetVersion')}:{str(manifest.get('datasetHash'))[:12]} · "
            f"N={results['repeats']} · dryRun={manifest.get('dryRun')}"
        ),
        "",
        "## Primary and guardrails",
        "",
        "| arm | unsupported 분자/분모 | rate | out-of-candidate | duplicate | invalid after validation | coverage |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in results["arms"]:
        metric = results["metrics"].get(arm)
        if metric is None:
            continue
        unsupported = metric["unsupportedEvidence"]
        rate = unsupported["rate"]
        lines.append(
            f"| `{arm}` | {unsupported['numerator']}/{unsupported['denominator']} | "
            f"{rate if rate is not None else '-'} | {metric['outOfCandidateIdCount']} | "
            f"{metric['duplicateIdCount']} | {metric['invalidStructuredEvidenceCount']} | "
            f"{metric['validRankCoverage'] if metric['validRankCoverage'] is not None else '-'} |"
        )
    lines += [
        "",
        "## Overall comment grounding",
        "",
        "| arm | detected violation 분자/분모 | rate | supported coverage | downgrade | invalid after validation |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for arm in results["arms"]:
        metric = results["metrics"].get(arm)
        if metric is None:
            continue
        violation = metric["detectedOverallClaimViolation"]
        coverage = metric["supportedOverallClaimCoverage"]
        lines.append(
            f"| `{arm}` | {violation['numerator']}/{violation['denominator']} | "
            f"{violation['rate'] if violation['rate'] is not None else '-'} | "
            f"{coverage['rate'] if coverage['rate'] is not None else '-'} | "
            f"{metric['overallValidatorDowngradeCount']} | "
            f"{metric['overallInvalidStructuredClaimCount']} |"
        )
    lines += [
        "",
        "## Operational",
        "",
        f"- failures: {results['failureCount']}",
        f"- unfilled cells: {len(results['unfilledCells'])}",
        f"- model: {manifest.get('modelConfig')}",
        f"- budget: {manifest.get('budget')}",
        "",
        "## Limits",
        "",
        *[f"- {limit}" for limit in results["limits"]],
        "",
        "Frozen C1~C4 release claims are unchanged; this report is exploratory appendix evidence.",
        "",
    ]
    return "\n".join(lines)


def _write_samples(path: Path, run: ProbeRun) -> None:
    fieldnames = [
        "caseId",
        "testType",
        "pairId",
        "arm",
        "sampleIndex",
        "latencyMs",
        "rankedProductIds",
        "displayedRationales",
        "rawOverallComment",
        "rawOverallClaims",
        "finalView",
        "detectedOverallClaimCodes",
        "overallGroundingDecision",
        "renderedOverallComment",
        "groundingDecisions",
        "validatorDowngradeCount",
        "outOfCandidateIdCount",
        "duplicateIdCount",
        "validRankCoverage",
        "candidateCount",
        "rawResponse",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for cell in run.cells:
            for sample in cell.samples:
                writer.writerow(
                    {
                        "caseId": sample.case_id,
                        "testType": sample.test_type,
                        "pairId": sample.pair_id or "",
                        "arm": sample.arm,
                        "sampleIndex": sample.sample_index,
                        "latencyMs": sample.latency_ms,
                        "rankedProductIds": json.dumps(sample.ranked_product_ids),
                        "displayedRationales": json.dumps(
                            sample.displayed_rationales, ensure_ascii=False
                        ),
                        "rawOverallComment": sample.raw_overall_comment,
                        "rawOverallClaims": json.dumps(
                            sample.raw_overall_claims, ensure_ascii=False
                        ),
                        "finalView": json.dumps(asdict(sample.final_view), ensure_ascii=False),
                        "detectedOverallClaimCodes": json.dumps(
                            sample.detected_overall_claim_codes, ensure_ascii=False
                        ),
                        "overallGroundingDecision": json.dumps(
                            (
                                asdict(sample.overall_grounding_decision)
                                if sample.overall_grounding_decision is not None
                                else None
                            ),
                            ensure_ascii=False,
                        ),
                        "renderedOverallComment": sample.displayed_overall_comment,
                        "groundingDecisions": json.dumps(
                            [asdict(decision) for decision in sample.grounding_decisions],
                            ensure_ascii=False,
                        ),
                        "validatorDowngradeCount": sample.validator_downgrade_count,
                        "outOfCandidateIdCount": sample.out_of_candidate_id_count,
                        "duplicateIdCount": sample.duplicate_id_count,
                        "validRankCoverage": sample.valid_rank_coverage,
                        "candidateCount": sample.candidate_count,
                        "rawResponse": sample.raw_response,
                    }
                )


def _write_failures(path: Path, run: ProbeRun) -> None:
    fieldnames = ["caseId", "arm", "attempt", "errorType", "message"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for failure in run.failures():
            writer.writerow(
                {
                    "caseId": failure.case_id,
                    "arm": failure.arm,
                    "attempt": failure.attempt,
                    "errorType": failure.error_type,
                    "message": failure.message,
                }
            )


def write_artifacts(out: Path, *, run: ProbeRun, manifest: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    results = build_results(run, manifest)
    _write_json(out / "results.json", results)
    _write_json(out / "run_manifest.json", manifest)
    _write_samples(out / "samples.csv", run)
    _write_failures(out / "failures.csv", run)
    (out / "report.md").write_text(render_report(results, manifest), encoding="utf-8", newline="\n")
