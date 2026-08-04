"""산출물 — results.json · report.md · samples/cells/failures CSV.

리포트 첫 줄에 프롬프트 해시·티어·모델·픽스처·N 을 항상 싣는다. 표만 복사돼 돌아다니는 것을
막는 최소 장치이며, 축 정의도 표 안에 함께 인쇄한다(#260 §4).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from evals.intent_probe.metrics import AxisResult, ISSUE_240_AXIS_ORDER, issue240_line
from evals.intent_probe.runner import CellResult

# 산출물 비교(결정론 테스트)에서 빼는 실행별 변동값.
VOLATILE_JSON_KEYS = frozenset(
    {"run", "dirty", "timestamp", "latencyMs", "totalWaitS", "maxWaitS", "correlationId"}
)
VOLATILE_CSV_COLUMNS = frozenset({"latencyMs"})

TRAPS = (
    "페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.",
    "실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 셀은 위 목록에 드러난다.",
    "픽스처 문자열이 정답 신호와 겹치면 안 된다(옵션 이름이 상품명에 섞이면 옵션 축이 무너진다).",
    "단일 실행은 채택 판정이 아니다 — 축당 ±2, 특정 셀은 2/8~6/8 까지 흔들린다. 독립 2~3회로 판정.",
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def header_line(results: dict[str, Any]) -> str:
    """이 표가 무엇을 잰 것인지 한 줄로."""
    prompt = results["prompt"]
    model = results["modelConfig"]
    fixture = results["fixture"]
    tier = results["tier"]
    model_id = model.get("fastModel") if tier == "fast" else model.get("smartModel")
    return (
        f"prompt={prompt['sha12']} ({prompt['source']}) · tier={tier} · model={model_id} · "
        f"fixture={fixture['version']} · N={results['n']}"
    )


def build_results(
    *,
    cells: list[CellResult],
    axes: dict[str, AxisResult],
    diagnostics_payload: dict[str, Any],
    unfilled: list[dict[str, Any]],
    prompt: dict[str, Any],
    tier: str,
    model_config: dict[str, Any],
    fixture: dict[str, Any],
    n: int,
    pacer: dict[str, Any],
    budget: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "prompt": prompt,
        "tier": tier,
        "modelConfig": model_config,
        "fixture": fixture,
        "n": n,
        "cellCount": len(cells),
        "dryRun": dry_run,
        "issue240Line": issue240_line(axes),
        "issue240AxisOrder": list(ISSUE_240_AXIS_ORDER),
        "axes": {axis_id: axis.as_dict() for axis_id, axis in axes.items()},
        "diagnostics": diagnostics_payload,
        "unfilledCells": unfilled,
        "pacer": pacer,
        "budget": budget,
        "cells": [
            {
                "cellId": cell.cell_id,
                "utteranceId": cell.utterance_id,
                "contextId": cell.context_id,
                "group": cell.group,
                "sampleCount": len(cell.samples),
                "attempts": cell.attempts,
                "failureCount": len(cell.failures),
                "filled": cell.filled,
                "intentCounts": cell.intent_counts(),
            }
            for cell in cells
        ],
    }


def render_report(results: dict[str, Any]) -> str:
    """사람이 축·셀 분포·못 채운 칸을 바로 읽는 Markdown."""
    axes = results["axes"]
    lines = [
        "# intent 라우팅 프로브 리포트",
        "",
        header_line(results),
        "",
        f"#240 축 순서 요약: `{results['issue240Line']}`",
        f"(순서: {' / '.join(results['issue240AxisOrder'])})",
        "",
        "> **이건 골든셋이 아니다.** 추천 품질이 아니라 intent 라우팅 분포를 잰 표다.",
        "",
        "## 축",
        "",
        "| 축 | 점수 | 분자 정의 | 분모 정의 |",
        "|---|---|---|---|",
    ]
    for axis_id in sorted(axes):
        axis = axes[axis_id]
        ratio = axis["ratio"]
        score = f"{axis['numerator']}/{axis['denominator']}"
        if ratio is not None:
            score += f" ({ratio * 100:.1f}%)"
        if axis["unfilledSampleCount"]:
            score += f" ⚠ 미충족 {axis['unfilledSampleCount']}"
        definition = axis["definition"]
        lines.append(
            f"| `{axis_id}` {axis['title']} | {score} | {definition['numerator']} | "
            f"{definition['denominator']} |"
        )
        if definition["notComparableWith"]:
            lines.append(
                f"| | | ⚠ 직접 비교 금지: {', '.join(definition['notComparableWith'])} | |"
            )

    lines += [
        "",
        "## 진단 (합불 아님)",
        "",
        f"- 되물음 상품 에코(위험한 실패): {results['diagnostics']['reaskProductEchoCount']}",
        f"- productId null(안전한 퇴화): {results['diagnostics']['productIdNullCount']}",
        "",
        "## 셀별 intent 분포",
        "",
        "| 셀 | 표본 | 시도 | intent 분포 |",
        "|---|---|---|---|",
    ]
    for cell in results["cells"]:
        counts = ", ".join(f"{key} {value}" for key, value in cell["intentCounts"].items())
        lines.append(
            f"| `{cell['cellId']}` | {cell['sampleCount']} | {cell['attempts']} | {counts} |"
        )

    lines += ["", "## 채우지 못한 셀", ""]
    if results["unfilledCells"]:
        lines += ["| 셀 | 채움/목표 | 시도 | 오류 |", "|---|---|---|---|"]
        lines += [
            f"| `{row['cellId']}` | {row['got']}/{row['want']} | {row['attempts']} | "
            f"{', '.join(row['errorTypes']) or '-'} |"
            for row in results["unfilledCells"]
        ]
    else:
        lines.append("(없음)")

    lines += ["", "## 재현 함정", ""]
    lines += [f"{index}. {trap}" for index, trap in enumerate(TRAPS, 1)]
    lines += [
        "",
        f"페이싱 실측: 대기 {results['pacer']['waitCount']}회 / "
        f"허용 {results['pacer']['maxRpm']} rpm.",
        "",
    ]
    return "\n".join(lines)


def write_artifacts(
    out: Path, *, results: dict[str, Any], manifest: dict[str, Any], cells: list[CellResult]
) -> None:
    """산출물 6종을 결정론적으로 쓴다."""
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "results.json", results)
    _write_json(out / "run_manifest.json", manifest)
    (out / "report.md").write_text(render_report(results), encoding="utf-8", newline="\n")
    _write_csv(
        out / "samples.csv",
        [
            "cellId",
            "utteranceId",
            "contextId",
            "group",
            "sampleIndex",
            "intent",
            "productId",
            "optionId",
            "quantity",
            "case",
            "scopedToPrevious",
            "latencyMs",
        ],
        [
            {
                "cellId": cell.cell_id,
                "utteranceId": cell.utterance_id,
                "contextId": cell.context_id,
                "group": cell.group,
                "sampleIndex": sample.sample_index,
                "intent": sample.intent,
                "productId": sample.product_id,
                "optionId": sample.option_id,
                "quantity": sample.quantity,
                "case": sample.case,
                "scopedToPrevious": sample.scoped_to_previous,
                "latencyMs": sample.latency_ms,
            }
            for cell in cells
            for sample in cell.samples
        ],
    )
    _write_csv(
        out / "cells.csv",
        [
            "cellId",
            "utteranceId",
            "contextId",
            "group",
            "sampleCount",
            "attempts",
            "failureCount",
            "filled",
        ],
        [
            {
                "cellId": cell.cell_id,
                "utteranceId": cell.utterance_id,
                "contextId": cell.context_id,
                "group": cell.group,
                "sampleCount": len(cell.samples),
                "attempts": cell.attempts,
                "failureCount": len(cell.failures),
                "filled": cell.filled,
            }
            for cell in cells
        ],
    )
    _write_csv(
        out / "failures.csv",
        ["cellId", "attempt", "errorType", "message"],
        [
            {
                "cellId": failure.cell_id,
                "attempt": failure.attempt,
                "errorType": failure.error_type,
                "message": failure.message,
            }
            for cell in cells
            for failure in cell.failures
        ],
    )


def _strip_volatile(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: _strip_volatile(value)
            for key, value in payload.items()
            if key not in VOLATILE_JSON_KEYS
        }
    if isinstance(payload, list):
        return [_strip_volatile(item) for item in payload]
    return payload


def normalized_artifact_bytes(out: Path) -> dict[str, bytes]:
    """실행별 변동값(시각·지연·runId)을 걷어낸 산출물 — 결정론 테스트용."""
    normalized: dict[str, bytes] = {}
    for path in sorted(out.iterdir()):
        if path.suffix == ".json":
            payload = _strip_volatile(json.loads(path.read_text(encoding="utf-8")))
            normalized[path.name] = json.dumps(
                payload, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8")
        elif path.suffix == ".csv":
            rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
            for row in rows:
                for column in VOLATILE_CSV_COLUMNS & set(row):
                    row[column] = ""
            normalized[path.name] = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
        else:
            normalized[path.name] = path.read_bytes()
    return normalized
