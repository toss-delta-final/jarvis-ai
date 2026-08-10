"""ablation 비교 JSON·Markdown와 결정론 normalize 도구."""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

from evals.metrics.run_manifest import VOLATILE_MANIFEST_KEYS
from evals.model_eval.pricing import PriceBook

# 산출물 비교(결정론 테스트)에서 빼는 실행별 변동값.
VOLATILE_JSON_KEYS = VOLATILE_MANIFEST_KEYS | frozenset(
    {"latencyMs", "correlationId", "perCaseTotalLatencyMs"}
)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize_resource_axes(result: dict[str, Any], pricing: PriceBook) -> dict[str, Any]:
    """token·cost·전체 벽시계 latency를 case 단위와 coverage로 요약한다."""
    rows = result.get("caseResults", [])
    calls = [call for row in rows for call in row.get("providerCalls", [])]
    call_count = len(calls)
    latencies = [float(row["latencyMs"]) for row in rows if isinstance(row.get("latencyMs"), int | float)]
    latency_coverage = len(latencies) / len(rows) if rows else 0.0
    if call_count == 0:
        return {
            "callCount": 0,
            "applicability": "notApplicableNoLlmCalls",
            "perCaseInputTokens": None,
            "perCaseOutputTokens": None,
            "perCaseCostUsd": None,
            "tokenCoverage": None,
            "costCoverage": None,
            "perCaseTotalLatencyMs": _mean(latencies),
            "latencyCoverage": latency_coverage,
            "ttftMs": None,
            "ttftStatus": "unknownOfflineHarnessCannotMeasureServerFirstTextToken",
        }
    coverage = pricing.coverage(calls)
    case_count = len(rows)
    input_total = sum(int(call["inputTokens"]) for call in calls if isinstance(call.get("inputTokens"), int))
    output_total = sum(int(call["outputTokens"]) for call in calls if isinstance(call.get("outputTokens"), int))
    costs = [float(call["costUsd"]) for call in calls if isinstance(call.get("costUsd"), int | float)]
    return {
        "callCount": call_count,
        "applicability": "measured",
        "perCaseInputTokens": input_total / case_count if coverage["tokenCoverage"] == 1.0 and case_count else None,
        "perCaseOutputTokens": output_total / case_count if coverage["tokenCoverage"] == 1.0 and case_count else None,
        "perCaseCostUsd": sum(costs) / case_count if coverage["costCoverage"] == 1.0 and case_count else None,
        "tokenCoverage": coverage["tokenCoverage"],
        "costCoverage": coverage["costCoverage"],
        "perCaseTotalLatencyMs": _mean(latencies),
        "latencyCoverage": latency_coverage,
        "ttftMs": None,
        "ttftStatus": "unknownOfflineHarnessCannotMeasureServerFirstTextToken",
    }


def _number(value: object, digits: int = 6) -> str:
    return f"{float(value):.{digits}f}" if isinstance(value, int | float) else "N/A"


def render_comparison_report(comparison: dict[str, Any]) -> str:
    """#154 결과 문서에 그대로 붙일 수 있는 비교표를 만든다."""
    lines = [
        "# Recommendation pipeline ablation",
        "",
        "## Arm summary",
        "",
        "| arm | primary mean | calls | input tokens/case | output tokens/case | cost USD/case | total latency ms/case | token coverage | cost coverage | TTFT |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for arm in ("pipeline", "scoring", "single_call"):
        payload = comparison["arms"][arm]
        resource = payload["resources"]
        lines.append(
            f"| {arm} | {_number(payload.get('primaryMean'))} | {resource['callCount']} | "
            f"{_number(resource.get('perCaseInputTokens'), 2)} | "
            f"{_number(resource.get('perCaseOutputTokens'), 2)} | "
            f"{_number(resource.get('perCaseCostUsd'), 8)} | "
            f"{_number(resource.get('perCaseTotalLatencyMs'), 2)} | "
            f"{_number(resource.get('tokenCoverage'), 3)} | "
            f"{_number(resource.get('costCoverage'), 3)} | unknown |"
        )
    lines.extend(
        [
            "",
            "Arm B의 token/cost는 0이 아니라 LLM 호출 없음으로 해당 없음이다.",
            "total latency는 adapter 전체 벽시계이며 TTFT는 server_first_text_token_ms를 오프라인에서 관측할 수 없어 unknown이다.",
            "",
            "## Quality and safety",
            "",
            "| arm | nDCG@10 | Precision@10 | Recall@10 | MRR | FilterAcc | hard failures | hard constraint violation rate | ranking excluded |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for arm in ("pipeline", "scoring", "single_call"):
        payload = comparison["arms"][arm]
        secondary = payload["secondaryMetrics"]
        lines.append(
            f"| {arm} | {_number(payload.get('primaryMean'))} | "
            f"{_number(secondary.get('overall.precisionAtK.10', {}).get('mean'))} | "
            f"{_number(secondary.get('overall.recallAtK.10', {}).get('mean'))} | "
            f"{_number(secondary.get('overall.mrr', {}).get('mean'))} | "
            f"{_number(secondary.get('overall.filterAccuracy', {}).get('mean'))} | "
            f"{payload.get('hardFailureCount', 0)} | "
            f"{_number(payload.get('hardConstraintViolationRate'))} | "
            f"{payload.get('rankingExcludedCaseCount', 0)} |"
        )
    lines.extend(
        [
            "",
            (
                "hard constraint violation rate는 hardFailure 행도 evaluated metric row 분모에 "
                "포함하므로 희석될 수 있으며 hard failure count와 함께 해석한다."
            ),
            "",
            "## Exploratory slice primary metrics",
            "",
        ]
    )
    for arm in ("pipeline", "scoring", "single_call"):
        lines.append(f"### {arm}")
        slices = comparison["arms"][arm].get("slices", {})
        lines.extend(
            [
                f"- `{name}`: n={payload.get('n', 0)}, mean={_number(payload.get('primaryMean'))}"
                for name, payload in sorted(slices.items())
            ]
            or ["- 없음"]
        )
    lines.extend(["", "## Failure cases", ""])
    for arm in ("pipeline", "scoring", "single_call"):
        lines.append(f"### {arm}")
        failures = comparison["arms"][arm].get("failureCases", [])
        lines.extend(
            [
                f"- `{row['caseId']}`: {row['failureReason']}"
                for row in failures
            ]
            or ["- 없음"]
        )
    lines.extend(
        [
            "",
            "## Confirmatory paired primary deltas",
            "",
            "| pair (left-right) | paired N | mean delta | bootstrap 95% CI | verdict |",
            "|---|---:|---:|---|---|",
        ]
    )
    for name, payload in comparison["pairedComparisons"].items():
        ci = payload["bootstrapCi95"]
        lines.append(
            f"| {name} | {payload['pairedCount']} | {_number(payload.get('meanDelta'))} | "
            f"[{_number(ci.get('low'))}, {_number(ci.get('high'))}] | {payload['verdict']} |"
        )
    lines.extend(
        [
            "",
            "Primary metric만 confirmatory이며 secondary metric·latency·token·cost는 exploratory다. CI가 0을 포함하면 승자를 정하지 않고 inconclusive로 표기한다.",
            "",
        ]
    )
    return "\n".join(lines)


def write_top_level_artifacts(
    out: Path, *, comparison: dict[str, Any], manifest: dict[str, Any]
) -> None:
    """최상위 비교 산출물을 기록한다."""
    (out / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (out / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (out / "report.md").write_text(
        render_comparison_report(comparison), encoding="utf-8", newline="\n"
    )


def _scrub_json(value: object) -> object:
    if isinstance(value, list):
        return [_scrub_json(item) for item in value]
    if not isinstance(value, dict):
        return value
    scrubbed: dict[str, object] = {}
    for key, item in value.items():
        if key in VOLATILE_JSON_KEYS:
            continue
        scrubbed[key] = _scrub_json(item)
    return scrubbed


def _normalize_csv(path: Path) -> str:
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))
    fields = list(rows[0]) if rows else path.read_text(encoding="utf-8").splitlines()[0].split(",")
    drop = {"latencyMs", "correlationId"}
    fields = [field for field in fields if field not in drop]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows([{field: row.get(field) for field in fields} for row in rows])
    return output.getvalue()


def normalized_artifact_bytes(out: Path) -> bytes:
    """실행 경로·ID·시각·벽시계를 제거한 전체 산출물의 canonical bytes를 만든다."""
    normalized: dict[str, object] = {}
    for path in sorted(item for item in out.rglob("*") if item.is_file()):
        relative = path.relative_to(out).as_posix()
        if path.suffix == ".json":
            normalized[relative] = _scrub_json(json.loads(path.read_text(encoding="utf-8")))
        elif path.suffix == ".csv":
            normalized[relative] = _normalize_csv(path)
        else:
            text = path.read_text(encoding="utf-8")
            normalized[relative] = re.sub(
                r"\| ([0-9]+(?:\.[0-9]+)?) \| ([^|]+) \| ([^|]+) \| unknown \|$",
                r"| <latency-normalized> | \2 | \3 | unknown |",
                text,
                flags=re.MULTILINE,
            )
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
