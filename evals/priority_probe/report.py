"""산출물 — results.json · report.md · samples.csv (#281 TASK 3 §5).

`evals/intent_probe/report.py` 와 같은 규율: 리포트 첫 줄에 무엇을 쟀는지(팔·프롬프트 해시 둘·
티어·픽스처·N)를 항상 싣는다.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from evals.priority_probe.metrics import MetricResult
from evals.priority_probe.runner import CellResult
from evals.priority_probe.schema import FixtureSet

TRAPS = (
    "전역 페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.",
    "실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 셀은 아래 목록에 드러난다.",
    "픽스처 문자열이 정답 신호와 겹치면 안 된다(발화에 '필수'·'선택' 같은 어휘 금지).",
    "단일 실행은 채택 판정이 아니다 — 독립 2회 이상의 분포로 판정한다.",
    "빈 맥락 프로브는 거짓 결론을 준다 — 인라인 팔은 채운 PRIOR_FILTERS/LAST_RECOMMENDATIONS 로 잰다.",
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
    prompt = results["prompt"]
    fixture = results["fixture"]
    return (
        f"arm={results['arm']} · prompt={prompt['sha12']} ({prompt['source']}) · "
        f"classifierPrompt={results['classifierPromptSha12']} · tier={results['tier']} · "
        f"model={results['modelConfig'].get('fastModel') if results['tier'] == 'fast' else results['modelConfig'].get('smartModel')} · "
        f"fixture={fixture['version']} · N={results['n']}"
    )


def build_results(
    *,
    arm: str,
    cells: list[CellResult],
    metrics: dict[str, MetricResult],
    diagnostics_payload: dict[str, Any],
    unfilled: list[dict[str, Any]],
    prompt: dict[str, Any],
    classifier_prompt_sha256: str,
    tier: str,
    model_config: dict[str, Any],
    fixture: dict[str, Any],
    n: int,
    pacer: dict[str, Any],
    budget: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "arm": arm,
        "prompt": prompt,
        "classifierPromptSha256": classifier_prompt_sha256,
        "classifierPromptSha12": classifier_prompt_sha256[:12],
        "tier": tier,
        "modelConfig": model_config,
        "fixture": fixture,
        "n": n,
        "cellCount": len(cells),
        "dryRun": dry_run,
        "metrics": {metric_id: metric.as_dict() for metric_id, metric in metrics.items()},
        "diagnostics": diagnostics_payload,
        "unfilledCells": unfilled,
        "pacer": pacer,
        "budget": budget,
        "cells": [
            {
                "cellId": cell.cell_id,
                "sampleCount": len(cell.samples),
                "attempts": cell.attempts,
                "failureCount": len(cell.failures),
                "filled": cell.filled,
            }
            for cell in cells
        ],
    }


def render_report(results: dict[str, Any]) -> str:
    metrics = results["metrics"]
    lines = [
        "# priority 신호 실측 프로브 리포트 (#281)",
        "",
        header_line(results),
        "",
        "> 인라인/전용 분류기 중 어느 쪽이 fast 티어에서 니즈 priority(1 필수/2 권장/3 선택)를 "
        "안정적으로 추출하는가. 숫자가 결정한다 — 결론을 먼저 쓰지 않는다.",
        "",
        "## 축",
        "",
        "| 축 | 점수 | 분자 정의 | 분모 정의 |",
        "|---|---|---|---|",
    ]
    # priorityOrderPairs 를 앞에 둔다 — 이 이슈의 본질 축(§4). essentialProtected 가 REQ-REC-076
    # ("1 필수는 최후")에 직접 대응하므로 그 다음이다(TASK-3-CORRECTION-2).
    order = (
        "priorityOrderPairs",
        "essentialProtected",
        "priorityOrderPairsByIndex",
        "prioritySignalPresent",
        "priorityExact",
    )
    for metric_id in order:
        metric = metrics[metric_id]
        ratio = metric["ratio"]
        score = f"{metric['numerator']}/{metric['denominator']}"
        if ratio is not None:
            score += f" ({ratio * 100:.1f}%)"
        definition = metric["definition"]
        lines.append(
            f"| `{metric_id}` {metric['title']} | {score} | {definition['numerator']} | "
            f"{definition['denominator']} |"
        )

    diag = results["diagnostics"]
    lines += [
        "",
        "## 진단 (합불 아님)",
        "",
        f"- 미파싱(unparsedCount, lengthMismatchCount 와 상호 배타적): {diag['unparsedCount']}",
        f"- 길이 불일치(lengthMismatchCount, 구조적 비용): {diag['lengthMismatchCount']}",
        f"- 이름 불일치(nameUnmatchedCount, 인라인 전용 · lengthMismatchCount 와 겹칠 수 있음): "
        f"{diag['nameUnmatchedCount']}",
        f"- 범위 밖 값(invalidValueCount, 이름은 매칭됐지만 값이 범위 밖): {diag['invalidValueCount']}",
        f"- 빈 신호 표본(emptySignalCount, 결과 지표 — 위 원인들과 겹칠 수 있음): "
        f"{diag['emptySignalCount']}",
        f"- 전송 재시도(transportRetries, TASK-3-CORRECTION — 크면 표를 신뢰하지 말 것): "
        f"{diag['transportRetries']}",
        "",
        "## 채우지 못한 셀",
        "",
    ]
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
        f"페이싱 실측: 대기 {results['pacer']['waitCount']}회 / 허용 {results['pacer']['maxRpm']} rpm.",
        "",
    ]
    return "\n".join(lines)


def write_artifacts(
    out: Path,
    *,
    results: dict[str, Any],
    manifest: dict[str, Any],
    cells: list[CellResult],
    fixture: FixtureSet,
) -> None:
    """산출물을 쓴다. `samples.csv` 는 **런을 다시 돌리지 않고 재집계**할 수 있어야 하므로
    (intent_probe 규약) needs·기대값·산출을 전부 원시 칸으로 남긴다."""
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "results.json", results)
    _write_json(out / "run_manifest.json", manifest)
    (out / "report.md").write_text(render_report(results), encoding="utf-8", newline="\n")
    cells_by_id = {cell.cell_id: cell for cell in fixture.cells}
    _write_csv(
        out / "samples.csv",
        [
            "cellId",
            "sampleIndex",
            "needs",
            "expectedPriorities",
            "priorities",
            "prioritiesByIndex",
            # [TASK-3-CORRECTION-2] 모델이 실제로 낸 leg 원문 — 런을 다시 돌리지 않고 이름 매칭
            # 규칙을 바꿔 재집계할 수 있어야 한다(#240 규약). 분류기 팔은 항상 빈 문자열이다
            # (needs 를 직접 입력받으므로 이 정합 문제 자체가 없다).
            "rawLegs",
            "lengthMismatch",
            "latencyMs",
        ],
        [
            {
                "cellId": cell.cell_id,
                "sampleIndex": sample.sample_index,
                "needs": ";".join(cells_by_id[cell.cell_id].needs),
                "expectedPriorities": ";".join(
                    str(value) for value in cells_by_id[cell.cell_id].expected_priorities
                ),
                "priorities": ";".join("" if v is None else str(v) for v in sample.priorities),
                "prioritiesByIndex": (
                    ";".join("" if v is None else str(v) for v in sample.priorities_by_index)
                    if sample.priorities_by_index is not None
                    else ""
                ),
                "rawLegs": " | ".join(
                    f"{category or ''}::{query or ''}::{priority if priority is not None else ''}"
                    for category, query, priority in sample.raw_legs
                ),
                "lengthMismatch": sample.length_mismatch,
                "latencyMs": sample.latency_ms,
            }
            for cell in cells
            for sample in cell.samples
        ],
    )
    _write_csv(
        out / "cells.csv",
        ["cellId", "sampleCount", "attempts", "failureCount", "filled"],
        [
            {
                "cellId": cell.cell_id,
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
