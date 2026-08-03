"""actual-model 평가 JSON·Markdown·CSV 산출물."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _number(value: object) -> str:
    return f"{float(value):.6f}" if isinstance(value, int | float) else "unknown"


def render_report(results: dict[str, Any], regression: dict[str, Any] | None) -> str:
    """사람이 primary 분포·실패·paired Δ를 바로 판독할 Markdown을 만든다."""
    verdict = regression.get("verdict") if regression else "notCompared"
    coverage = results.get("coverage", {})
    primary = results.get("primarySummary", {})
    ci = primary.get("bootstrapCi95", {})
    lines = [
        "# Actual-model evaluation report",
        "",
        f"- Dataset hash: `{results.get('datasetHash')}`",
        (
            f"- Cases × repeats: {results.get('uniqueCaseCount', 0)} × "
            f"{results.get('executedRepeats', 0)}"
        ),
        f"- Primary metric: `{results.get('primaryMetric')}`",
        (
            "- Primary summary: "
            f"N={primary.get('n', 0)}, mean={_number(primary.get('mean'))}, "
            f"median={_number(primary.get('median'))}, SD={_number(primary.get('sd'))}, "
            f"IQR={_number(primary.get('iqr'))}, "
            f"95% CI=[{_number(ci.get('low'))}, {_number(ci.get('high'))}]"
        ),
        f"- Hard failures: {results.get('hardFailureCount', 0)}",
        (
            "- Hard-constraint violations: "
            f"{results.get('hardConstraintViolationCount', 0)} "
            f"(rate={_number(results.get('hardConstraintViolationRate'))})"
        ),
        f"- Budget exceeded: {str(results.get('budgetExceeded', False)).lower()}",
        f"- Token coverage: {coverage.get('tokenCoverage')}",
        f"- Cost coverage: {coverage.get('costCoverage')}",
        f"- Baseline verdict: **{verdict}**",
        "",
    ]
    warnings = results.get("releaseWarnings", [])
    if warnings:
        lines.extend(["## Warnings", "", *(f"- {warning}" for warning in warnings), ""])

    lines.extend(
        [
            "## Per-case primary metric",
            "",
            "| caseId | N | mean | median | SD | IQR |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for case_id, summary in sorted(results.get("caseRepeatSummaries", {}).items()):
        lines.append(
            f"| {case_id} | {summary.get('n', 0)} | {_number(summary.get('mean'))} | "
            f"{_number(summary.get('median'))} | {_number(summary.get('sd'))} | "
            f"{_number(summary.get('iqr'))} |"
        )

    lines.extend(["", "## Hard failures", ""])
    failures = [row for row in results.get("caseResults", []) if row.get("hardFailure") is True]
    lines.extend(
        [
            f"- {row.get('caseId')}: {row.get('failureReason')} (repeat={row.get('repeat')})"
            for row in failures
        ]
        or ["- None"]
    )

    excluded = results.get("rankingExcludedCases", [])
    lines.extend(["", "## Ranking denominator exclusions", ""])
    lines.extend([f"- {row['caseId']}: {row['reason']}" for row in excluded] or ["- None"])

    secondary = results.get("secondaryMetricSummaries", {})
    lines.extend(["", "## Secondary metrics (exploratory)", ""])
    lines.extend(
        [
            f"- `{metric}`: mean={_number(payload.get('overallSummary', {}).get('mean'))}"
            for metric, payload in sorted(secondary.items())
        ]
        or ["- None"]
    )

    if regression:
        deltas = sorted(regression.get("caseDeltas", []), key=lambda row: row["delta"])
        paired = regression.get("pairedSummary", {})
        paired_ci = regression.get("bootstrapCi95", {})
        lines.extend(
            [
                "",
                "## Paired baseline deltas",
                "",
                (
                    f"- N={paired.get('n', 0)}, mean={_number(paired.get('mean'))}, "
                    f"median={_number(paired.get('median'))}, "
                    f"SD={_number(paired.get('sd'))}, IQR={_number(paired.get('iqr'))}, "
                    f"95% CI=[{_number(paired_ci.get('low'))}, "
                    f"{_number(paired_ci.get('high'))}]"
                ),
                "",
            ]
        )
        for label, rows in (("Lowest", deltas[:5]), ("Highest", list(reversed(deltas[-5:])))):
            lines.append(f"### {label}")
            lines.extend(
                [f"- {row['caseId']}: {_number(row['delta'])}" for row in rows] or ["- None"]
            )
    lines.extend(
        [
            "",
            "Secondary metrics and slices are exploratory; only the configured primary metric is confirmatory.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_artifacts(
    out: Path,
    *,
    results: dict[str, Any],
    manifest: dict[str, Any],
    regression: dict[str, Any] | None,
) -> None:
    """새 출력 디렉터리에 부분 결과까지 원자적이지 않게 즉시 보존한다."""
    if out.exists():
        raise FileExistsError(f"출력 디렉터리가 이미 존재합니다: {out}")
    out.mkdir(parents=True)
    _write_json(out / "results.json", results)
    _write_json(out / "run_manifest.json", manifest)

    case_rows = results.get("caseResults", [])
    _csv(
        out / "cases.csv",
        ["repeat", "caseId", "hardFailure", "failureReason", "latencyMs"],
        [
            {
                "repeat": row.get("repeat"),
                "caseId": row.get("caseId"),
                "hardFailure": row.get("hardFailure"),
                "failureReason": row.get("failureReason"),
                "latencyMs": row.get("latencyMs"),
            }
            for row in case_rows
        ],
    )
    calls = [call for row in case_rows for call in row.get("providerCalls", [])]
    call_fields = [
        "correlationId",
        "callSite",
        "tier",
        "model",
        "inputTokens",
        "outputTokens",
        "reasoningTokens",
        "cacheTokens",
        "usageSource",
        "costUsd",
        "latencyMs",
        "error",
    ]
    _csv(
        out / "calls.csv",
        call_fields,
        [{key: call.get(key) for key in call_fields} for call in calls],
    )
    regression_rows = [regression] if regression else []
    regression_fields = [
        "verdict",
        "pairedCount",
        "missingCurrentCount",
        "missingBaselineCount",
        "meanDelta",
        "margin",
    ]
    _csv(
        out / "regression.csv",
        regression_fields,
        [{key: row.get(key) for key in regression_fields} for row in regression_rows],
    )
    (out / "report.md").write_text(
        render_report(results, regression),
        encoding="utf-8",
        newline="\n",
    )
