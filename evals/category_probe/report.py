"""산출물 — results.json · report.md · samples/cells/failures/hits/confusion CSV (#331).

리포트 첫 줄에 티어·모델·픽스처·N 을 항상 싣는다. 축 정의도 표 안에 함께 인쇄한다
(`evals/README.md` 8항).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from evals.category_probe.metrics import ADOPTED_DISTANCE_EVENTS, DROP_REASON_EVENTS, AxisResult
from evals.category_probe.runner import CellResult, Sample
from evals.metrics.run_manifest import VOLATILE_MANIFEST_KEYS

VOLATILE_JSON_KEYS = VOLATILE_MANIFEST_KEYS | frozenset({"timestamp", "latencyMs"})
VOLATILE_CSV_COLUMNS = frozenset({"latencyMs"})

BANNER = "단일 실행으로 채택 판정 금지 — 표본 분산이 커 축당 ±수 포인트가 흔들린다. 독립 2~3회 분포로 판정한다."

TRAPS = (
    "페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.",
    "실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 셀은 아래 목록에 드러난다.",
    "앵커-정답 누출 금지 — accept(canonical) 문자열이 발화 원문에 그대로 들어가면 안 된다"
    "(schema.py 가 ' > ' 포함을 거부해 강제한다).",
    BANNER,
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


def md_cell(value: str) -> str:
    return value.replace("|", "\\|")


def header_line(results: dict[str, Any]) -> str:
    model = results["modelConfig"]
    fixture = results["fixture"]
    tier = results["tier"]
    model_id = model.get("fastModel") if tier == "fast" else model.get("smartModel")
    return (
        f"tier={tier} · model={model_id} · fixture={fixture['version']} · N={results['n']} · "
        f"cells={results['cellCount']}"
    )


def build_results(
    *,
    cells: list[CellResult],
    axes: dict[str, AxisResult],
    baseline: dict[str, Any],
    diagnostics_payload: dict[str, Any],
    confusion: list[dict[str, Any]],
    distance_distribution: dict[str, Any],
    unfilled: list[dict[str, Any]],
    tier: str,
    model_config: dict[str, Any],
    fixture: dict[str, Any],
    n: int,
    pacer: dict[str, Any],
    budget: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "tier": tier,
        "modelConfig": model_config,
        "fixture": fixture,
        "n": n,
        "cellCount": len(cells),
        "dryRun": dry_run,
        "axes": {axis_id: axis.as_dict() for axis_id, axis in axes.items()},
        "baseline": baseline,
        "diagnostics": diagnostics_payload,
        "confusion": confusion,
        "distanceDistribution": distance_distribution,
        "unfilledCells": unfilled,
        "pacer": pacer,
        "budget": budget,
        "cells": [
            {
                "cellId": cell.cell_id,
                "sliceId": cell.slice_id,
                "testType": cell.test_type,
                "sampleCount": len(cell.samples),
                "attempts": cell.attempts,
                "failureCount": len(cell.failures),
                "intentSlipCount": cell.intent_slip_count,
                "filled": cell.filled,
            }
            for cell in cells
        ],
    }


def render_report(results: dict[str, Any]) -> str:
    axes = results["axes"]
    lines = [
        "# 카테고리 매핑·선택 프로브 리포트 (#331)",
        "",
        header_line(results),
        "",
        f"> ⚠ **{BANNER}**",
        "",
        "> **이건 골든셋이 아니다.** 추천 품질이 아니라 발화→카테고리 매핑·선택 정확도를 잰 표다.",
        "",
        "## 축 (파이프라인)",
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
            f"| `{axis_id}` {axis['title']} | {score} | {md_cell(definition['numerator'])} | "
            f"{md_cell(definition['denominator'])} |"
        )

    baseline = results["baseline"]
    lines += ["", "## trivial baseline 대조", "", "| 축 | 점수 | 정의 |", "|---|---|---|"]
    for axis_id in ("baselineTop1Single", "baselineTopK"):
        axis = baseline[axis_id]
        ratio = axis["ratio"]
        score = f"{axis['numerator']}/{axis['denominator']}"
        if ratio is not None:
            score += f" ({ratio * 100:.1f}%)"
        lines.append(f"| `{axis_id}` | {score} | {md_cell(axis['definition']['numerator'])} |")
    lines += ["", f"> {baseline['note']}"]

    lines += [
        "",
        "## 진단 (합불 아님)",
        "",
        f"- intent 슬립(버려짐): {results['diagnostics']['intentSlipCount']}",
        f"- leg 추출 실패(noLegCount): {results['diagnostics']['noLegCount']}",
        f"- 거리컷 드롭: {results['diagnostics']['distanceRejectedCount']}",
        f"- 택일 null: {results['diagnostics']['selectNullCount']}",
        f"- 택일 교체(changed): {results['diagnostics']['selectChangedCount']}",
        f"- exact 매치: {results['diagnostics']['exactHitCount']}",
        f"- fan-out 확장 발동: {results['diagnostics']['expansionTakenCount']}",
        "",
        "## 혼동 표 (single/multi 오답)",
        "",
        "| 기대 | 실제 | 빈도 |",
        "|---|---|---|",
    ]
    if results["confusion"]:
        lines += [
            f"| `{md_cell(row['expected'])}` | `{md_cell(row['actual'])}` | {row['count']} |"
            for row in results["confusion"]
        ]
    else:
        lines.append("| (없음) | | |")

    dist = results["distanceDistribution"]
    lines += [
        "",
        "## top-1 distance 분포 (#344 계측)",
        "",
        "| | 표본 수 | 중앙값 | Q1 | Q3 |",
        "|---|---|---|---|---|",
        f"| 정답 | {dist['correct']['count']} | {dist['correct']['median']} | "
        f"{dist['correct']['q1']} | {dist['correct']['q3']} |",
        f"| 오답 | {dist['incorrect']['count']} | {dist['incorrect']['median']} | "
        f"{dist['incorrect']['q1']} | {dist['incorrect']['q3']} |",
        "",
        f"> {dist['note']}",
    ]

    lines += ["", "## 채우지 못한 셀", ""]
    if results["unfilledCells"]:
        lines += ["| 셀 | 채움/목표 | 시도 | 오류 |", "|---|---|---|---|"]
        lines += [
            f"| `{md_cell(row['cellId'])}` | {row['got']}/{row['want']} | {row['attempts']} | "
            f"{', '.join(row['errorTypes']) or '-'} |"
            for row in results["unfilledCells"]
        ]
    else:
        lines.append("(없음)")

    lines += ["", "## 재현 함정", ""]
    lines += [f"{index}. {trap}" for index, trap in enumerate(TRAPS, 1)]
    lines += [
        "",
        f"페이싱 실측: 대기 {results['pacer']['waitCount']}회 / 허용 {results['pacer']['maxRpm']} rpm.",
        "",
    ]
    return "\n".join(lines)


def _adopted_fields(sample: Sample) -> tuple[str, str, str]:
    """[F1-8] 표본이 실제로 **채택**한 canonical 의 distance·margin·anchor_kind — 캡처된
    `category_repaired`/`category_fallback_top1`/`category_distance_override` 이벤트에서 그대로
    뽑는다(재구현 아님, `evals.category_probe.metrics.ADOPTED_DISTANCE_EVENTS` 와 같은 어휘).
    multi 셀은 leg 마다 이 이벤트가 따로 뜨므로 세미콜론으로 이어(다른 다중값 칸과 같은 규약).
    이벤트가 없는 표본(exact match)은 세 칸 모두 빈 문자열이다.
    """
    distances: list[str] = []
    margins: list[str] = []
    kinds: list[str] = []
    for event in sample.events:
        if event.get("event") not in ADOPTED_DISTANCE_EVENTS:
            continue
        distances.append(str(event.get("distance", "")))
        margins.append(str(event.get("margin", "")))
        kinds.append(str(event.get("anchor_kind", "")))
    return ";".join(distances), ";".join(margins), ";".join(kinds)


def _drop_reasons(sample: Sample) -> str:
    """[F1-8] 이 표본에서 leg 가 canonical 을 못 낸 사유 이벤트 이름 목록(발생 순서, 세미콜론)."""
    return ";".join(
        event.get("event", "")
        for event in sample.events
        if event.get("event") in DROP_REASON_EVENTS
    )


def _sample_row(cell_id: str, slice_id: str, test_type: str, sample: Sample) -> dict[str, Any]:
    adopted_distance, adopted_margin, adopted_anchor_kind = _adopted_fields(sample)
    return {
        "cellId": cell_id,
        "sliceId": slice_id,
        "testType": test_type,
        "sampleIndex": sample.sample_index,
        "legs": ";".join(f"{c}|{q or ''}" for c, q in sample.legs),
        "unresolved": ";".join(sample.unresolved),
        "expansionLeaves": ";".join(f"{c}|{q or ''}" for c, q in sample.expansion_leaves),
        "selectCalls": sample.select_calls,
        "decomposeLegs": ";".join(f"{c or ''}|{q or ''}" for c, q in sample.decompose_legs),
        "adoptedDistance": adopted_distance,
        "adoptedMargin": adopted_margin,
        "adoptedAnchorKind": adopted_anchor_kind,
        "dropReasons": _drop_reasons(sample),
        "latencyMs": sample.latency_ms,
    }


def write_artifacts(
    out: Path, *, results: dict[str, Any], manifest: dict[str, Any], cells: list[CellResult]
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "results.json", results)
    _write_json(out / "run_manifest.json", manifest)
    (out / "report.md").write_text(render_report(results), encoding="utf-8", newline="\n")

    _write_csv(
        out / "samples.csv",
        [
            "cellId",
            "sliceId",
            "testType",
            "sampleIndex",
            "legs",
            "unresolved",
            "expansionLeaves",
            "selectCalls",
            "decomposeLegs",
            # [F1-8, packet-331 §5] 원 패킷이 요구했으나 빠졌던 칸 — 캡처 events 에서 뽑는다.
            "adoptedDistance",
            "adoptedMargin",
            "adoptedAnchorKind",
            "dropReasons",
            "latencyMs",
        ],
        [
            _sample_row(cell.cell_id, cell.slice_id, cell.test_type, sample)
            for cell in cells
            for sample in cell.samples
        ],
    )
    _write_csv(
        out / "cells.csv",
        [
            "cellId",
            "sliceId",
            "testType",
            "sampleCount",
            "attempts",
            "failureCount",
            "intentSlipCount",
            "filled",
        ],
        [
            {
                "cellId": cell.cell_id,
                "sliceId": cell.slice_id,
                "testType": cell.test_type,
                "sampleCount": len(cell.samples),
                "attempts": cell.attempts,
                "failureCount": len(cell.failures),
                "intentSlipCount": cell.intent_slip_count,
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
    _write_csv(
        out / "hits.csv",
        ["cellId", "sampleIndex", "legIndex", "anchorKind", "rank", "canonical", "distance"],
        [
            {
                "cellId": cell.cell_id,
                "sampleIndex": sample.sample_index,
                "legIndex": hit.leg_index,
                "anchorKind": hit.anchor_kind,
                "rank": hit.rank,
                "canonical": hit.canonical,
                "distance": hit.distance,
            }
            for cell in cells
            for sample in cell.samples
            for hit in sample.hits
        ],
    )
    _write_csv(
        out / "confusion.csv",
        ["expected", "actual", "count"],
        results["confusion"],
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
