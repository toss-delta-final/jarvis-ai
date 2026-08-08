"""산출물 — results.json · report.md · samples/cells/failures CSV (intent_probe/report.py 와 같은 형식).

리포트 첫 줄에 프롬프트 해시·티어·모델·픽스처·N 을 항상 싣는다. 축 정의도 표 안에 함께
인쇄한다(#260 §4, evals/README.md 규약 8).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from evals.legs_probe.metrics import (
    SLICES,
    AxisResult,
    compute_leg_diagnostics,
)
from evals.legs_probe.runner import CellResult
from evals.legs_probe.schema import AnchorSet
from evals.metrics.run_manifest import VOLATILE_MANIFEST_KEYS

VOLATILE_JSON_KEYS = VOLATILE_MANIFEST_KEYS | frozenset(
    {"timestamp", "latencyMs", "totalWaitS", "maxWaitS"}
)
VOLATILE_CSV_COLUMNS = frozenset({"latencyMs"})

TRAPS = (
    "페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.",
    "실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 셀은 위 목록에 드러난다.",
    "단일 실행은 채택 판정이 아니다 — 독립 2~3회 분포로 판정한다.",
    "이건 골든셋이 아니다 — decompose 단계 산출 분포이며, 2단계 needs_expansion 은 부르지 않는다.",
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
    """마크다운 표 칸에 안전하게 넣는다(파이프 이스케이프, intent_probe/report.py 와 같은 이유)."""
    return value.replace("|", "\\|")


def _fmt_numerator(value: float) -> str:
    """`legCoverage` 처럼 numerator 가 [0,1] 합의 float 인 축을 사람이 읽게 자른다."""
    return f"{value:.2f}" if isinstance(value, float) and not value.is_integer() else f"{value:g}"


def _exploratory_badge(axis: dict[str, Any]) -> str:
    """[R4-3] 슬라이스 표본이 임계(`SLICE_SAMPLE_THRESHOLD`=40) 미만이면 점수 뒤에 붙인다 —
    "## 축 전체" 표는 `_axis_row` 가 성격(nature) 칸에 이미 표시하지만, 슬라이스별 쪼갠 표는
    분모가 훨씬 작아지는데도 지금까지 아무 표시가 없었다."""
    return " (exploratory: N<40)" if axis.get("belowSampleThreshold") else ""


def header_line(results: dict[str, Any]) -> str:
    prompt = results["prompt"]
    model = results["modelConfig"]
    fixture = results["fixture"]
    tier = results["tier"]
    model_id = model.get("fastModel") if tier == "fast" else model.get("smartModel")
    return (
        f"prompt={prompt['sha12']} ({prompt['source']}) · tier={tier} · model={model_id} · "
        f"fixture={fixture['version']} · N={results['n']} · "
        f"categoryFanoutMax={results['categoryFanoutMax']}"
    )


def build_results(
    *,
    cells: list[CellResult],
    scored: dict[str, Any],
    diagnostics_payload: dict[str, Any],
    baseline: dict[str, Any],
    pair_rows: list[dict[str, Any]],
    prompt_example_rows: list[dict[str, Any]],
    unfilled: list[dict[str, Any]],
    prompt: dict[str, Any],
    tier: str,
    model_config: dict[str, Any],
    fixture: dict[str, Any],
    n: int,
    category_fanout_max: int,
    repurchase_max: int,
    pacer: dict[str, Any],
    budget: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    axes: dict[str, AxisResult] = scored["axes"]
    slices: dict[str, dict[str, AxisResult]] = scored["slices"]
    return {
        "prompt": prompt,
        "tier": tier,
        "modelConfig": model_config,
        "fixture": fixture,
        "n": n,
        "categoryFanoutMax": category_fanout_max,
        "repurchaseMax": repurchase_max,
        "cellCount": len(cells),
        "dryRun": dry_run,
        "axes": {axis_id: axis.as_dict() for axis_id, axis in axes.items()},
        "slices": {
            axis_id: {slice_name: axis.as_dict() for slice_name, axis in per_slice.items()}
            for axis_id, per_slice in slices.items()
        },
        "diagnostics": diagnostics_payload,
        "baseline": baseline,
        "pairDiagnostics": pair_rows,
        "promptExampleDiagnostics": prompt_example_rows,
        "unfilledCells": unfilled,
        "pacer": pacer,
        "budget": budget,
        "cells": [
            {
                "cellId": cell.cell_id,
                "caseId": cell.case_id,
                "slice": cell.slice,
                "sampleCount": len(cell.samples),
                "attempts": cell.attempts,
                "failureCount": len(cell.failures),
                "filled": cell.filled,
                "intentCounts": cell.intent_counts(),
            }
            for cell in cells
        ],
    }


def _axis_row(axis_id: str, axis: dict[str, Any]) -> str:
    ratio = axis["ratio"]
    score = f"{_fmt_numerator(axis['numerator'])}/{axis['denominator']}"
    if ratio is not None:
        score += f" ({ratio * 100:.1f}%)"
    ci = axis["ci95"]
    ci_text = f"[{ci[0] * 100:.1f}%, {ci[1] * 100:.1f}%]" if ci else "N/A"
    nature = axis["nature"]
    if axis["belowSampleThreshold"] and nature != "exploratory":
        nature += " (N<40 → exploratory)"
    definition = axis["definition"]
    return (
        f"| `{axis_id}` {axis['title']} | {score} | CI95 {ci_text} | {nature} | "
        f"{md_cell(definition['numerator'])} | {md_cell(definition['denominator'])} |"
    )


def render_report(results: dict[str, Any]) -> str:
    axes = results["axes"]
    lines = [
        "# 니즈 전개(legs) 평가 하네스 리포트",
        "",
        header_line(results),
        "",
        "> **이건 골든셋이 아니다.** 추천 품질이 아니라 decompose 단계의 case·legs 산출 분포를 "
        "잰 표다. e2e 유효성 최종 판정은 caseId 척추(골든셋 연결)의 몫이다.",
        "",
        "## Primary confirmatory 지표",
        "",
        "> ⚠️ decompose 단계(2단계 전개 파이프라인 중 **1단계, needs_expansion #217 보정 전**) "
        "형상의 측정이다 — 사용자 체감 실패율이 아니다.",
        "",
        f"`case3UnderExpansionRate` (promptExample 제외): "
        f"{_fmt_numerator(axes['case3UnderExpansionRate']['numerator'])}/"
        f"{axes['case3UnderExpansionRate']['denominator']} "
        f"({(axes['case3UnderExpansionRate']['ratio'] or 0) * 100:.1f}%)",
    ]
    ci = axes["case3UnderExpansionRate"]["ci95"]
    if ci:
        lines.append(f"CI95 [{ci[0] * 100:.1f}%, {ci[1] * 100:.1f}%]")
    lines += [
        "",
        "슬라이스별(confirmatory 사전 등록: situational · purpose):",
        "",
        "| 슬라이스 | 점수 | CI95 |",
        "|---|---|---|",
    ]
    for slice_name, axis in results["slices"]["case3UnderExpansionRate"].items():
        ratio = axis["ratio"]
        score = f"{_fmt_numerator(axis['numerator'])}/{axis['denominator']}"
        if ratio is not None:
            score += f" ({ratio * 100:.1f}%)"
        score += _exploratory_badge(axis)
        ci = axis["ci95"]
        ci_text = f"[{ci[0] * 100:.1f}%, {ci[1] * 100:.1f}%]" if ci else "N/A"
        lines.append(f"| {slice_name} | {score} | {ci_text} |")

    lines += [
        "",
        "## 축 전체",
        "",
        "| 축 | 점수 | CI95 | 성격 | 분자 정의 | 분모 정의 |",
        "|---|---|---|---|---|---|",
    ]
    for axis_id in sorted(axes):
        lines.append(_axis_row(axis_id, axes[axis_id]))

    lines += ["", "## 슬라이스별 병기", ""]
    for axis_id, per_slice in results["slices"].items():
        lines += [f"### `{axis_id}`", "", "| 슬라이스 | 점수 | CI95 |", "|---|---|---|"]
        for slice_name in SLICES:
            axis = per_slice.get(slice_name)
            if axis is None:
                continue
            ratio = axis["ratio"]
            score = f"{_fmt_numerator(axis['numerator'])}/{axis['denominator']}"
            if ratio is not None:
                score += f" ({ratio * 100:.1f}%)"
            score += _exploratory_badge(axis)
            ci = axis["ci95"]
            ci_text = f"[{ci[0] * 100:.1f}%, {ci[1] * 100:.1f}%]" if ci else "N/A"
            lines.append(f"| {slice_name} | {score} | {ci_text} |")
        lines.append("")

    baseline = results["baseline"]
    lines += [
        "## LLM vs baseline (trivial baseline: 항상 leg 1개)",
        "",
        "baseline `legCoverage` 는 저자 라벨(`baselineGroupsHit`) 기반이다 — 발화 원문 하나를 "
        "그대로 검색어로 썼을 때 몇 개 그룹을 짚는지의 근사치이며, LLM 호출 없이 결정론으로 "
        "계산된다.",
        "",
        f"baseline `case3UnderExpansionRate` = {baseline['case3UnderExpansionRate']} (구조적 — "
        "leg 이 항상 1개이므로 정의상 1.0). baseline `overExpansionRate` = "
        f"{baseline['overExpansionRate']} (정의상 0).",
        "",
        "| 슬라이스 | LLM legCoverage | baseline legCoverage |",
        "|---|---|---|",
    ]
    # [R4-2] 슬라이스만 보면 "전체적으로 LLM 이 trivial baseline 을 넘지 못한다"는 #275 형
    # 핵심 사실이 표에서 빠진다(과소 보고) — overall 행을 맨 위에 추가한다. WithPromptExamples
    # 판(exploratory)은 쓰지 않는다 — confirmatory `legCoverage` 와 정의가 다른 분모를 대조하면
    # #234/#240 이 밟은 "다른 정의, 같은 이름" 사고가 재발한다(evals/README.md 규약 8).
    overall_llm_axis = axes["legCoverage"]
    overall_llm_ratio = overall_llm_axis["ratio"]
    overall_llm_text = f"{overall_llm_ratio * 100:.1f}%" if overall_llm_ratio is not None else "N/A"
    overall_baseline_value = baseline.get("legCoverageOverall")
    overall_baseline_text = (
        f"{overall_baseline_value * 100:.1f}%" if overall_baseline_value is not None else "N/A"
    )
    lines.append(f"| **전체** | {overall_llm_text} | {overall_baseline_text} |")
    llm_leg_coverage = results["slices"]["legCoverage"]
    for slice_name in SLICES:
        llm_axis = llm_leg_coverage.get(slice_name)
        llm_text = (
            f"{llm_axis['ratio'] * 100:.1f}%"
            if llm_axis and llm_axis["ratio"] is not None
            else "N/A"
        )
        if llm_axis is not None:
            llm_text += _exploratory_badge(llm_axis)
        baseline_value = baseline["legCoveragePerSlice"].get(slice_name)
        baseline_text = f"{baseline_value * 100:.1f}%" if baseline_value is not None else "N/A"
        lines.append(f"| {slice_name} | {llm_text} | {baseline_text} |")
    lines.append("")
    if overall_llm_ratio is not None and overall_baseline_value is not None:
        if overall_llm_ratio <= overall_baseline_value:
            lines.append(
                "**전체 기준으로 LLM 이 trivial baseline 을 넘지 못한다** — 단일 런이라 확정은 "
                "아니다(위 「단일 실행은 채택 판정이 아니다」 참조), 하지만 #275 가 랭킹에서 밟은 "
                "같은 형태의 결과다: 임의 순서 기준선 대조 없이는 몰랐을 사실이다."
            )
        else:
            lines.append(
                "전체 기준으로 LLM 이 trivial baseline 을 앞선다 — 단일 런이라 확정은 아니다."
            )

    lines += ["", "## pair 진단 (exploratory, 합불 아님)", ""]
    if results["pairDiagnostics"]:
        lines += [
            "| pairId | pairKind | caseId | 표본 | 평균 legs | 평균 legCoverage |",
            "|---|---|---|---|---|---|",
        ]
        for row in results["pairDiagnostics"]:
            avg_legs = f"{row['avgLegs']:.2f}" if row["avgLegs"] is not None else "N/A"
            avg_cov = (
                f"{row['avgLegCoverage'] * 100:.1f}%"
                if row["avgLegCoverage"] is not None
                else "N/A"
            )
            lines.append(
                f"| {row['pairId']} | {row['pairKind'] or '-'} | `{md_cell(row['caseId'])}` | "
                f"{row['sampleCount']} | {avg_legs} | {avg_cov} |"
            )
    else:
        lines.append("(없음)")

    lines += ["", "## promptExample 앵커 (confirmatory 집계에서 제외, 별도 exploratory)", ""]
    prompt_example_rows = results["promptExampleDiagnostics"]
    if prompt_example_rows:
        lines += [
            "| caseId | 슬라이스 | recommend 표본 | case==3 ∧ legs<=1 | 평균 legCoverage |",
            "|---|---|---|---|---|",
        ]
        for row in sorted(prompt_example_rows, key=lambda r: r["caseId"]):
            avg_cov = (
                f"{row['avgLegCoverage'] * 100:.1f}%"
                if row["avgLegCoverage"] is not None
                else "N/A"
            )
            lines.append(
                f"| `{md_cell(row['caseId'])}` | {row['slice']} | {row['recommendSampleCount']} | "
                f"{row['case3UnderExpansionCount']} | {avg_cov} |"
            )
    else:
        lines.append("(없음)")

    lines += [
        "",
        "## 진단 (합불 아님)",
        "",
        f"- intent!=recommend 표본(앵커별): {results['diagnostics']['nonRecommendIntentCount']}",
        f"- 발화 에코 leg(과전개 중 발화 복사): {results['diagnostics']['utteranceEchoLegCount']}",
        f"- case==3 ∧ legs==0: {results['diagnostics']['emptyLegsOnCase3Count']}",
        "",
        "## 셀별 intent 분포",
        "",
        "| 셀 | 슬라이스 | 표본 | 시도 | intent 분포 |",
        "|---|---|---|---|---|",
    ]
    for cell in results["cells"]:
        counts = ", ".join(f"{key} {value}" for key, value in cell["intentCounts"].items())
        lines.append(
            f"| `{md_cell(cell['cellId'])}` | {cell['slice']} | {cell['sampleCount']} | "
            f"{cell['attempts']} | {counts} |"
        )

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


def write_artifacts(
    out: Path,
    *,
    results: dict[str, Any],
    manifest: dict[str, Any],
    cells: list[CellResult],
    anchors: AnchorSet,
) -> None:
    """산출물 6종을 결정론적으로 쓴다."""
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "results.json", results)
    _write_json(out / "run_manifest.json", manifest)
    (out / "report.md").write_text(render_report(results), encoding="utf-8", newline="\n")
    by_case_id = {anchor.case_id: anchor for anchor in anchors.utterances}
    sample_rows: list[dict[str, Any]] = []
    for cell in cells:
        anchor = by_case_id[cell.case_id]
        for sample in cell.samples:
            diag = compute_leg_diagnostics(sample, anchor)
            sample_rows.append(
                {
                    "caseId": sample.case_id,
                    "n": sample.sample_index,
                    "slice": anchor.slice,
                    "intent": sample.intent,
                    "case": sample.case,
                    "legsCount": len(sample.category_queries),
                    "legQueries": json.dumps(
                        [q.query for q in sample.category_queries], ensure_ascii=False
                    ),
                    "legRawCategories": json.dumps(
                        [q.raw_category for q in sample.category_queries], ensure_ascii=False
                    ),
                    "matchedGroups": ";".join(diag.matched_coverage_group_ids),
                    "coverage": diag.coverage_ratio,
                    "overExpandedLegCount": diag.over_expanded_leg_count,
                    "echoLegCount": diag.echo_leg_count,
                    "buyAll": sample.buy_all,
                    "totalBudget": sample.total_budget,
                    "latencyMs": sample.latency_ms,
                }
            )
    _write_csv(
        out / "samples.csv",
        [
            "caseId",
            "n",
            "slice",
            "intent",
            "case",
            "legsCount",
            "legQueries",
            "legRawCategories",
            "matchedGroups",
            "coverage",
            "overExpandedLegCount",
            "echoLegCount",
            "buyAll",
            "totalBudget",
            "latencyMs",
        ],
        sample_rows,
    )
    _write_csv(
        out / "cells.csv",
        ["cellId", "caseId", "slice", "sampleCount", "attempts", "failureCount", "filled"],
        [
            {
                "cellId": cell.cell_id,
                "caseId": cell.case_id,
                "slice": cell.slice,
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
