"""산출물 — results.json · report.md · samples/cells/failures CSV (legs_probe/report.py 승계).

리포트 첫 줄에 프롬프트 해시·티어·모델·픽스처·N 을 항상 싣는다. 축 정의도 표 안에 함께
인쇄한다(#260 §4, evals/README.md 규약 8).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from evals.metrics.run_manifest import VOLATILE_MANIFEST_KEYS
from evals.underspecified_probe.metrics import (
    CONFIRMATORY_FALSE_ALARM_SLICES,
    CONFIRMATORY_MISS_SLICES,
)
from evals.underspecified_probe.runner import CellResult
from evals.underspecified_probe.schema import SLICES

VOLATILE_JSON_KEYS = VOLATILE_MANIFEST_KEYS | frozenset(
    {"timestamp", "latencyMs", "totalWaitS", "maxWaitS"}
)
VOLATILE_CSV_COLUMNS = frozenset({"latencyMs"})

TRAPS = (
    "페이서 없이 돌리면 429 로 표본이 비고, 빈 칸을 오답으로 세면 분포가 거짓이 된다.",
    "실패는 표본이 아니다 — 성공 N개를 채우고, 못 채운 셀은 위 목록에 드러난다.",
    "단일 실행은 채택 판정이 아니다 — 독립 2~3회 분포로 판정한다.",
    "이건 골든셋이 아니다 — decompose 직후·판정 직전의 형상만 잰다. #372 의 되물음 답변 턴 "
    "결정론 fixture 실측과 숫자를 섞지 말 것.",
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
    """마크다운 표 칸에 안전하게 넣는다(파이프 이스케이프, legs_probe/report.py 와 같은 이유)."""
    return value.replace("|", "\\|")


def _fmt_score(axis: dict[str, Any]) -> str:
    numerator = axis["numerator"]
    text = (
        f"{numerator:.2f}"
        if isinstance(numerator, float) and not numerator.is_integer()
        else f"{numerator:g}"
    )
    score = f"{text}/{axis['denominator']}"
    ratio = axis["ratio"]
    if ratio is not None:
        score += f" ({ratio * 100:.1f}%)"
    return score


def _sample_size_badge(axis: dict[str, Any]) -> str:
    """[F-5] 분모 0 은 "표본이 적다"가 아니라 "이 슬라이스에서는 애초에 잴 수 없다" — 다른
    표시를 쓴다(예: `missRate` 의 `what_axis` 행처럼 구조적으로 표본이 있을 수 없는 칸)."""
    if axis["denominator"] == 0:
        return " (해당 없음)"
    return " (exploratory: N<40)" if axis.get("belowSampleThreshold") else ""


def _ci_text(axis: dict[str, Any]) -> str:
    ci = axis["ci95"]
    return f"[{ci[0] * 100:.1f}%, {ci[1] * 100:.1f}%]" if ci else "N/A"


def header_line(results: dict[str, Any]) -> str:
    prompt = results["prompt"]
    model = results["modelConfig"]
    fixture = results["fixture"]
    tier = results["tier"]
    model_id = model.get("fastModel") if tier == "fast" else model.get("smartModel")
    return (
        f"prompt={prompt['sha12']} ({prompt['source']}) · tier={tier} · model={model_id} · "
        f"fixture={fixture['version']} · N={results['n']} · "
        f"underspecifiedReaskEnabled={results['judgment']['underspecifiedReaskEnabled']}"
    )


def build_results(
    *,
    cells: list[CellResult],
    scored: dict[str, Any],
    diagnostics_payload: dict[str, Any],
    baseline: dict[str, Any],
    cause_summary: dict[str, Any],
    sample_rows_payload: list[dict[str, Any]],
    unfilled: list[dict[str, Any]],
    prompt: dict[str, Any],
    tier: str,
    model_config: dict[str, Any],
    fixture: dict[str, Any],
    n: int,
    judgment: dict[str, Any],
    pacer: dict[str, Any],
    budget: dict[str, Any],
    dry_run: bool,
    union_enabled: bool = False,
) -> dict[str, Any]:
    axes = scored["axes"]
    slices = scored["slices"]
    payload = {
        "prompt": prompt,
        "tier": tier,
        "modelConfig": model_config,
        "fixture": fixture,
        "n": n,
        "judgment": judgment,
        "cellCount": len(cells),
        "dryRun": dry_run,
        "axes": {axis_id: axis.as_dict() for axis_id, axis in axes.items()},
        "slices": {
            axis_id: {slice_name: axis.as_dict() for slice_name, axis in per_slice.items()}
            for axis_id, per_slice in slices.items()
        },
        "diagnostics": diagnostics_payload,
        "baseline": baseline,
        "causeAxisSummary": cause_summary,
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
            }
            for cell in cells
        ],
        "sampleRows": sample_rows_payload,
    }
    if union_enabled:
        # [F-4, 리뷰 findings-432-r1] 기본(off) 산출물 형상을 그대로 얼린다(§2-1) — union 관련
        # 키는 union_enabled 일 때만 넣는다. off 에서 이 키가 섞이면 #433 이 굳힌 6판과 구조
        # 비교를 할 때 union 을 안 켰는데도 키가 달라진다.
        payload["unionEnabled"] = True
    return payload


def _axis_row(axis_id: str, axis: dict[str, Any]) -> str:
    score = _fmt_score(axis)
    nature = axis["nature"]
    if axis["denominator"] == 0:
        nature += " (해당 없음)"
    elif axis["belowSampleThreshold"] and nature not in ("exploratory", "invariant"):
        nature += " (N<40 → exploratory)"
    definition = axis["definition"]
    return (
        f"| `{axis_id}` {axis['title']} | {score} | CI95 {_ci_text(axis)} | {nature} | "
        f"{md_cell(definition['numerator'])} | {md_cell(definition['denominator'])} |"
    )


def _slice_table(per_slice: dict[str, Any]) -> list[str]:
    lines = ["| 슬라이스 | 점수 | CI95 |", "|---|---|---|"]
    for slice_name in SLICES:
        axis = per_slice.get(slice_name)
        if axis is None:
            continue
        score = _fmt_score(axis) + _sample_size_badge(axis)
        lines.append(f"| {slice_name} | {score} | {_ci_text(axis)} |")
    return lines


def render_report(results: dict[str, Any]) -> str:
    axes = results["axes"]
    lines = [
        "# 과소지정 판정 축 실 LLM 프로브 리포트",
        "",
        header_line(results),
        "",
        "> **이건 골든셋이 아니다.** 추천 품질이 아니라 `is_underspecified_turn` 판정의 실 LLM "
        "반복 분포를 잰 표다. 프로덕션은 decompose 뒤에 카테고리 매핑·`needs_expansion` 보정을 "
        "거치므로(§측정 범위와 한계, README 참조) 이 표의 수치를 사용자 체감 되물음율로 곧바로 "
        "읽으면 오독이다.",
        "",
        "## Primary confirmatory 지표 — missRate",
        "",
        f"`missRate`: {_fmt_score(axes['missRate'])}",
    ]
    if axes["missRate"]["ci95"]:
        lines.append(f"CI95 {_ci_text(axes['missRate'])}")
    lines += [
        "",
        f"슬라이스별(사전 등록: {' · '.join(CONFIRMATORY_MISS_SLICES)}):",
        "",
        *_slice_table(results["slices"]["missRate"]),
        "",
        "## Confirmatory-secondary 지표 — falseAlarmRate",
        "",
        f"`falseAlarmRate`(multiturn_gate 제외): {_fmt_score(axes['falseAlarmRate'])}",
    ]
    if axes["falseAlarmRate"]["ci95"]:
        lines.append(f"CI95 {_ci_text(axes['falseAlarmRate'])}")
    lines += [
        "",
        f"슬라이스별(사전 등록: {' · '.join(CONFIRMATORY_FALSE_ALARM_SLICES)}):",
        "",
        *_slice_table(results["slices"]["falseAlarmRate"]),
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
        lines += [f"### `{axis_id}`", "", *_slice_table(per_slice), ""]

    baseline = results["baseline"]
    lines += [
        "## LLM vs baseline (trivial baseline: 항상 reask=false)",
        "",
        f"baseline `missRate` = {baseline['missRate']} (구조적 — 항상 False 를 내므로 "
        "expectedReask=true 앵커를 전부 놓친다). baseline `falseAlarmRate` = "
        f"{baseline['falseAlarmRate']} (정의상 — 항상 False 이므로 오탐이 성립하지 않는다).",
        "",
        "| | LLM judgmentAccuracy | baseline judgmentAccuracy |",
        "|---|---|---|",
    ]
    llm_acc = axes["judgmentAccuracy"]
    llm_acc_text = f"{llm_acc['ratio'] * 100:.1f}%" if llm_acc["ratio"] is not None else "N/A"
    base_acc = baseline["judgmentAccuracy"]
    base_acc_text = f"{base_acc['ratio'] * 100:.1f}%" if base_acc["ratio"] is not None else "N/A"
    lines.append(f"| **전체(게이트 제외)** | {llm_acc_text} | {base_acc_text} |")
    llm_acc_slices = results["slices"]["judgmentAccuracy"]
    for slice_name in SLICES:
        llm_axis = llm_acc_slices.get(slice_name)
        llm_text = (
            f"{llm_axis['ratio'] * 100:.1f}%"
            if llm_axis and llm_axis["ratio"] is not None
            else "N/A"
        )
        if llm_axis is not None:
            llm_text += _sample_size_badge(llm_axis)
        base_slice = baseline["judgmentAccuracyPerSlice"].get(slice_name)
        base_text = (
            f"{base_slice['ratio'] * 100:.1f}%"
            if base_slice and base_slice["ratio"] is not None
            else "N/A"
        )
        lines.append(f"| {slice_name} | {llm_text} | {base_text} |")
    lines.append("")
    if llm_acc["ratio"] is not None and base_acc["ratio"] is not None:
        if llm_acc["ratio"] <= base_acc["ratio"]:
            lines.append(
                "**전체 기준으로 LLM 판정이 trivial baseline(항상 reask=false) 을 넘지 못한다** — "
                "단일 런이라 확정은 아니다(아래 「단일 실행은 채택 판정이 아니다」 참조)."
            )
        else:
            lines.append(
                "전체 기준으로 LLM 판정이 trivial baseline 을 앞선다 — 단일 런이라 확정은 아니다."
            )

    cause = results["causeAxisSummary"]
    lines += [
        "",
        "## 원인 축 분해 (이슈 완료 조건 3)",
        "",
        f"outcome 분포: {cause['outcomeCounts']}",
        "",
        "### 미탐 원인 축(ablation 이 뒤집은 축 — 복수 가능, `multiple` = 단일 축으로 안 뒤집힘)",
        "",
    ]
    if cause["missCauseAxisCounts"]:
        lines += ["| 축 | 미탐 표본 수 |", "|---|---|"]
        lines += [f"| `{axis}` | {count} |" for axis, count in cause["missCauseAxisCounts"].items()]
    else:
        lines.append("(미탐 없음)")
    lines += [
        "",
        "### 미탐 `blockingAxes` 조합별 분포 (F-2 — 실측 조합, 서술 아님)",
        "",
        "`multiple` 로 뭉뚱그리면 실제로 어떤 축들이 함께 막았는지가 감춰진다 — 이 표는 미탐",
        "표본의 `blockingAxes`(비어 있지 않은 차단 축 전부) 조합을 그대로 집계한다.",
        "",
    ]
    if cause["missBlockingAxisComboCounts"]:
        lines += ["| blockingAxes 조합 | 미탐 표본 수 |", "|---|---|"]
        lines += [
            f"| `{combo}` | {count} |"
            for combo, count in cause["missBlockingAxisComboCounts"].items()
        ]
    else:
        lines.append("(미탐 없음)")
    lines += ["", "### 오탐 원인 축(앵커 referenceAxes — 채워졌어야 할 축)", ""]
    if cause["falseAlarmCauseAxisCounts"]:
        lines += ["| 축 | 오탐 표본 수 |", "|---|---|"]
        lines += [
            f"| `{axis}` | {count} |" for axis, count in cause["falseAlarmCauseAxisCounts"].items()
        ]
    else:
        lines.append("(오탐 없음)")
    lines += [
        "",
        "표본별 상세(`verdict`·`expectedReask`·`outcome`·`causeAxes`·`blockingAxes`)는 "
        "`samples.csv` 에 실려 있다 — 런 재실행 없이 재집계할 수 있다.",
    ]

    lines += ["", "## 불변 재판정 (§D9, LLM 콜 0)", ""]
    flag_off = axes["flagOffInvariant"]
    prior_gate = axes["priorGateInvariant"]
    lines.append(
        f"- `flagOffInvariant`: {_fmt_score(flag_off)} (0/{flag_off['denominator']} 이어야 한다)"
    )
    lines.append(
        f"- `priorGateInvariant`: {_fmt_score(prior_gate)} (0/{prior_gate['denominator']} 이어야 한다)"
    )
    if flag_off["numerator"] or prior_gate["numerator"]:
        lines.append(
            "- ⚠️ **불변식 위반** — 위 두 값 중 하나 이상이 0 이 아니다. 판정 함수 배선이나 "
            "이 하네스의 재판정 호출부를 재검토하라."
        )

    lines += [
        "",
        "## 진단 (합불 아님)",
        "",
        f"- `categoryEchoWithoutQueriesCount`: {results['diagnostics']['categoryEchoWithoutQueriesCount']}"
        f" — {results['diagnostics']['definition']['categoryEchoWithoutQueriesCount']}",
        f"- `nonRecommendIntentCount`(앵커별·intent별, F-1): "
        f"{results['diagnostics']['nonRecommendIntentCount']}"
        f" — {results['diagnostics']['definition']['nonRecommendIntentCount']}",
        f"- `expansionGateWouldFireRate`: {_fmt_score(axes['expansionGateWouldFireRate'])}",
        f"- `missRateUnderExpansionAssumption`: {_fmt_score(axes['missRateUnderExpansionAssumption'])}",
    ]
    if results.get("unionEnabled"):
        lines.append(
            f"- `unionStageErrorCount`: {results['diagnostics']['unionStageErrorCount']}"
            f" — {results['diagnostics']['definition']['unionStageErrorCount']}"
        )

    if results.get("unionEnabled"):
        lines += [
            "",
            "## union(전개 후 판정) — #432",
            "",
            "> **오염 통제(§2-6)**: 이 절의 축은 전부 exploratory 다 — confirmatory 로 승격하지 "
            "않는다. `#331`(카테고리 매핑 품질)·`#332`(니즈 전개 품질)의 실패가 이 표에 섞인다 — "
            "분해는 `samples.csv` 의 `unionMappedLegCount`·`unionExpansionReason`·"
            "`unionCategoryExpanded` 컬럼으로 읽는다. union 단계에서 예외가 난 표본은 버리지 "
            "않고 union 축 **분모에서만** 제외한다(`unionStageErrorCount` 위 참조).",
            "",
            "| 축 | decompose 직후 | union(전개 후) |",
            "|---|---|---|",
            f"| 미탐율 | `missRate` {_fmt_score(axes['missRate'])} | "
            f"`missRateAfterExpansion` {_fmt_score(axes['missRateAfterExpansion'])} |",
            f"| 오탐율 | `falseAlarmRate` {_fmt_score(axes['falseAlarmRate'])} | "
            f"`falseAlarmRateAfterExpansion` {_fmt_score(axes['falseAlarmRateAfterExpansion'])} |",
            f"| 전개 게이트 발동률(분모가 서로 다르다 — 아래 참조) | `expansionGateWouldFireRate` "
            f"{_fmt_score(axes['expansionGateWouldFireRate'])} | `expansionGateFiredRate` "
            f"{_fmt_score(axes['expansionGateFiredRate'])} |",
            "",
            f"`expansionSuppressionRate`(전개가 되물음을 꺼뜨린 비율, decompose True → union "
            f"False): {_fmt_score(axes['expansionSuppressionRate'])}"
            + (
                f" CI95 {_ci_text(axes['expansionSuppressionRate'])}"
                if axes["expansionSuppressionRate"]["ci95"]
                else ""
            ),
            "",
            "위 두 miss 축의 분모는 `expectedReask=true` 앵커의 recommend 표본(라벨 기준)이고, "
            "`expansionSuppressionRate` 의 분모는 라벨과 무관한 decompose 판정 True 표본이다 — "
            "**분모가 다르므로 두 miss 축의 차이가 `expansionSuppressionRate` 와 같지 않다**"
            "(F-1, 2차 리뷰어 발견). `expansionSuppressionRate` 는 독립 축으로 읽어라 — 두 miss "
            '축은 "전개 후 미탐이 어떻게 되는가"를, 억제율은 "판정 True 였던 것 중 몇 %가 '
            '꺼졌는가"를 각각 답한다.',
            "",
            "`expansionGateWouldFireRate` 와 `expansionGateFiredRate` 도 **분모가 다른 별개 "
            '질문**이다(F-5) — 전자는 "판정 True 표본 중 몇 %가 게이트에 걸리나", 후자는 '
            '"전체 recommend 표본 중 몇 %에서 게이트가 실제로 발동하나"를 답한다. 분모를 '
            "일부러 좁히지 않았다 — 판정 True 표본은 정의상 `category_queries` 가 비어 있고, "
            "`detect_expansion_need([], case=…, unresolved=…)` 는 `unresolved` 값과 무관하게 "
            "`no_legs` 를 돌려주므로 그 좁은 분모 안에서는 두 값이 **구조적으로 항상 같다**(분모를 "
            "좁히면 이 축이 기존 축의 복제가 되어 새로 재는 정보가 0 이 된다). 넓은 분모라서 "
            "비로소 D2(`mapping_failed`)가 처음 관측된다 — 두 수치를 직접 비율로 대조하지 마라.",
        ]

    lines += ["", "## 셀별 표본 수", "", "| 셀 | 슬라이스 | 표본 | 시도 |", "|---|---|---|---|"]
    for cell in results["cells"]:
        lines.append(
            f"| `{md_cell(cell['cellId'])}` | {cell['slice']} | {cell['sampleCount']} | {cell['attempts']} |"
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

    lines += ["", "## 측정 범위와 한계 (요약 — 전문은 README §측정 범위와 한계)", ""]
    lines += [
        "1. `category_legs` 는 이 하네스에서 항상 빈다(카테고리 매핑을 부르지 않는다).",
        "2. `filters.category` 는 decompose 의 `filters` JSON 스키마에 키가 없어 구조적으로 항상 "
        "빈다 — `categoryEchoWithoutQueriesCount` 가 그 사실을 실측한다.",
        "3. `_prepare_recommendation` 의 카테고리 매핑과 전개 LLM 생성(`needs_expansion` #217 의 "
        "전개 호출)은 부르지 않는다 — **다만 게이트 판정 함수 `detect_expansion_need` 자체는 "
        "진단 목적으로 매 표본 부른다**(F-3, `expansionGateWouldFireRate`·"
        "`missRateUnderExpansionAssumption` 의 근거).",
        "4. 프로덕션은 `intent==recommend` 인 턴에서만 판정을 호출한다 — confirmatory 축은 "
        "recommend 표본으로 좁힌다(F-1). 비-recommend 표본은 `nonRecommendIntentCount` 로만 "
        "드러나며, 그 실패는 intent 라우팅 축(`evals/intent_probe`)의 소관이다.",
        "5. 단일 턴만 잰다(컨텍스트 행렬 없음).",
    ]

    lines += ["", "## 재현 함정", ""]
    lines += [f"{index}. {trap}" for index, trap in enumerate(TRAPS, 1)]
    lines += [
        "",
        f"페이싱 실측: 대기 {results['pacer']['waitCount']}회 / 허용 {results['pacer']['maxRpm']} rpm.",
        "",
    ]
    return "\n".join(lines)


UNION_SAMPLE_COLUMNS = (
    "unionVerdict",
    "unionOutcome",
    "unionMappedLegCount",
    "unionCategoryExpanded",
    "unionFiltersCategory",
    "unionExpansionReason",
    "unionBlockingAxes",
    "unionStageLatencyMs",
    "unionStageError",
)


def write_artifacts(
    out: Path,
    *,
    results: dict[str, Any],
    manifest: dict[str, Any],
    cells: list[CellResult],
    sample_rows_payload: list[dict[str, Any]],
    union_enabled: bool = False,
) -> None:
    """산출물 6종을 결정론적으로 쓴다.

    [#432] `union_enabled=False`(기본)면 `samples.csv` 에 union 컬럼이 **아예 없다** — #464
    진단 컬럼을 제외한 기존 union 산출물 형상은 그대로라 #433 이 굳힌 6판과 계속 비교 가능하다."""
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "results.json", results)
    _write_json(out / "run_manifest.json", manifest)
    (out / "report.md").write_text(render_report(results), encoding="utf-8", newline="\n")
    sample_columns = [
        "caseId",
        "n",
        "slice",
        "intent",
        "case",
        "semanticQueryIsFallback",
        "semanticQuery",
        "verdict",
        "expectedReask",
        "outcome",
        "causeAxes",
        "blockingAxes",
        "attrConditionAxes",
        "attrConditionsSuppressedAxes",
        "expansionReason",
        "latencyMs",
    ]
    if union_enabled:
        sample_columns.extend(UNION_SAMPLE_COLUMNS)
    sample_rows_csv = []
    for row in sample_rows_payload:
        csv_row = {
            "caseId": row["caseId"],
            "n": row["n"],
            "slice": row["slice"],
            "intent": row["intent"],
            "case": row["case"],
            "semanticQueryIsFallback": row["semanticQueryIsFallback"],
            "semanticQuery": row["semanticQuery"] or "",
            "verdict": row["verdict"],
            "expectedReask": row["expectedReask"],
            "outcome": row["outcome"],
            "causeAxes": ";".join(row["causeAxes"]),
            "blockingAxes": ";".join(row["blockingAxes"]),
            "attrConditionAxes": "|".join(row["attrConditionAxes"]),
            "attrConditionsSuppressedAxes": "|".join(row["attrConditionsSuppressedAxes"]),
            "expansionReason": row["expansionReason"] or "",
            "latencyMs": row["latencyMs"],
        }
        if union_enabled:
            csv_row["unionVerdict"] = row.get("unionVerdict")
            csv_row["unionOutcome"] = row.get("unionOutcome") or ""
            csv_row["unionMappedLegCount"] = row.get("unionMappedLegCount")
            csv_row["unionCategoryExpanded"] = row.get("unionCategoryExpanded")
            csv_row["unionFiltersCategory"] = row.get("unionFiltersCategory") or ""
            csv_row["unionExpansionReason"] = row.get("unionExpansionReason") or ""
            csv_row["unionBlockingAxes"] = ";".join(row.get("unionBlockingAxes") or [])
            csv_row["unionStageLatencyMs"] = row.get("unionStageLatencyMs")
            csv_row["unionStageError"] = row.get("unionStageError") or ""
        sample_rows_csv.append(csv_row)
    _write_csv(out / "samples.csv", sample_columns, sample_rows_csv)
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
