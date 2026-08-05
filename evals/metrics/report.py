"""평가 결과를 결정론적 JSON·Markdown·CSV artifact로 기록한다."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from evals.filter_axes.metrics import aggregate_axis_metrics
from evals.filter_axes.spec import AXES_SPEC_PATH


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: object) -> None:
    path.write_text(_json_text(value) + "\n", encoding="utf-8", newline="\n")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _cases_csv(report: dict[str, Any]) -> list[dict[str, object]]:
    rows = []
    for case in report["cases"]:
        rows.append(
            {
                "caseId": case["caseId"],
                "slices": "|".join(case["slices"]),
                "rankedProductIds": _json_text(case["rankedProductIds"]),
                "rankingExcluded": str(case["rankingExcluded"]).lower(),
                "rankingExclusionReason": case["rankingExclusionReason"] or "",
                "filterAccuracy": case["filterAccuracy"],
                "hardConstraintViolated": str(case["hardConstraintViolated"]).lower(),
                "diversity": case["diversity"],
                "duplicateCount": case["duplicateCount"],
                "unknownProductIds": _json_text(case["unknownProductIds"]),
                "metrics": _json_text(case["metrics"]),
            }
        )
    return rows


_FILTER_AXES_CSV_FIELDS = [
    "scope",
    "caseId",
    "axis",
    "outcome",
    "matchCount",
    "valueMismatchCount",
    "spuriousCount",
    "missingCount",
    "bothEmptyCount",
    "support",
    "valueStrictPrecision",
    "valueStrictRecall",
    "valueStrictF1",
    "presencePrecision",
    "presenceRecall",
    "presenceF1",
]


def _filter_axes_metric_row(
    *, scope: str, case_id: str, axis: str, outcome: str, aggregate: dict[str, Any]
) -> dict[str, object]:
    counts = aggregate["counts"]
    value_strict = aggregate["valueStrict"]
    presence = aggregate["presence"]
    return {
        "scope": scope,
        "caseId": case_id,
        "axis": axis,
        "outcome": outcome,
        "matchCount": counts["match"],
        "valueMismatchCount": counts["valueMismatch"],
        "spuriousCount": counts["spurious"],
        "missingCount": counts["missing"],
        "bothEmptyCount": counts["bothEmpty"],
        "support": aggregate["support"],
        "valueStrictPrecision": value_strict["precision"],
        "valueStrictRecall": value_strict["recall"],
        "valueStrictF1": value_strict["f1"],
        "presencePrecision": presence["precision"],
        "presenceRecall": presence["recall"],
        "presenceF1": presence["f1"],
    }


def _filter_axes_case_rows(report: dict[str, Any]) -> list[dict[str, object]]:
    """케이스 행도 aggregate와 같은 P/R/F1 칸을 채운다 — `aggregate_axis_metrics`를 케이스 1건짜리
    outcome map에 돌려 그 케이스만의 counts(0/1)·support·valueStrict/presence를 얻는다(값은
    0.0/1.0/None 중 하나). `outcome` 컬럼은 그대로 유지해 사람이 읽을 판정 문자열도 남긴다.
    """
    rows: list[dict[str, object]] = []
    for case in report["cases"]:
        per_axis = aggregate_axis_metrics([case["filterAxes"]])
        for axis, outcome in sorted(case["filterAxes"].items()):
            rows.append(
                _filter_axes_metric_row(
                    scope="case",
                    case_id=case["caseId"],
                    axis=axis,
                    outcome=outcome,
                    aggregate=per_axis[axis],
                )
            )
    return rows


def _filter_axes_aggregate_rows(scope: str, filter_axes: dict[str, Any]) -> list[dict[str, object]]:
    return [
        _filter_axes_metric_row(scope=scope, case_id="", axis=axis, outcome="", aggregate=aggregate)
        for axis, aggregate in sorted(filter_axes.items())
    ]


def _filter_axes_csv(report: dict[str, Any]) -> list[dict[str, object]]:
    """케이스×축 outcome(scope=case) + slice/overall 축별 집계(scope=overall|slice:<name>)."""
    rows = _filter_axes_case_rows(report)
    rows.extend(_filter_axes_aggregate_rows("overall", report["overall"]["filterAxes"]))
    for slice_name, aggregate in sorted(report["slices"].items()):
        rows.extend(_filter_axes_aggregate_rows(f"slice:{slice_name}", aggregate["filterAxes"]))
    return rows


def _aggregate_csv(name: str, aggregate: dict[str, Any]) -> dict[str, object]:
    return {
        "scope": name,
        "caseCount": aggregate["caseCount"],
        "rankingCaseCount": aggregate["rankingCaseCount"],
        "rankingExcludedCount": aggregate["rankingExcludedCount"],
        "rankingExcludedCaseIds": _json_text(aggregate["rankingExcludedCaseIds"]),
        "precisionAtK": _json_text(aggregate["precisionAtK"]),
        "recallAtK": _json_text(aggregate["recallAtK"]),
        "mrr": aggregate["mrr"],
        "ndcgAtK": _json_text(aggregate["ndcgAtK"]),
        "microPrecisionAtK": _json_text(aggregate["microPrecisionAtK"]),
        "microRecallAtK": _json_text(aggregate["microRecallAtK"]),
        "filterAccuracy": aggregate["filterAccuracy"],
        "hardConstraintViolationRate": aggregate["hardConstraintViolationRate"],
        "coverage": aggregate["coverage"],
        "diversity": aggregate["diversity"],
        "duplicateCount": aggregate["duplicateCount"],
        "unknownProductIds": _json_text(aggregate["unknownProductIds"]),
    }


def _markdown(report: dict[str, Any]) -> str:
    overall = report["overall"]
    excluded = ", ".join(overall["rankingExcludedCaseIds"]) or "(없음)"
    gate = ", ".join(report["prGateConstraints"])
    violations = report["violations"]
    lines = [
        "# 구매자 추천 품질 평가",
        "",
        f"- dataset: `{report['datasetVersion']}` / `{report['datasetHash']}`",
        f"- algorithm/config: `{report['algorithmVersion']}` / `{report['configVersion']}`",
        f"- model config: `{_json_text(report['modelConfig'])}`",
        "- 기본 scripted adapter는 expectedFilters를 decompose 출력으로 사용하므로 "
        "Filter Accuracy 1.0이 구조적으로 기대됩니다(#144에서 실모델로 교체).",
        f"- PR gate constraints: {gate}; 판단 의존 mustExclude·forbiddenCategory·"
        "forbiddenProductId는 전부 보고하되 #144 전까지 gate하지 않습니다.",
        "",
        "## 전체",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| cases | {overall['caseCount']} |",
        f"| ranking cases | {overall['rankingCaseCount']} |",
        f"| filter accuracy | {overall['filterAccuracy']:.6f} |",
        f"| hard constraint violation rate | {overall['hardConstraintViolationRate']:.6f} |",
        f"| coverage | {overall['coverage']:.6f} |",
        f"| diversity | {overall['diversity']:.6f} |",
        f"| MRR | {overall['mrr']:.6f} |",
        "",
        "## 순위 지표 분모 제외",
        "",
        f"- count: {overall['rankingExcludedCount']}",
        f"- caseId: {excluded}",
        "",
        "## 위반 제약",
        "",
    ]
    if violations:
        lines.extend(
            [
                "| caseId | productId | constraint |",
                "|---|---:|---|",
                *[
                    f"| {row['caseId']} | {row['productId']} | {row['constraint']} |"
                    for row in violations
                ],
            ]
        )
    else:
        lines.append("- 없음")
    lines.extend(["", "## Slice", "", "| slice | cases | HCV |", "|---|---:|---:|"])
    lines.extend(
        f"| {name} | {aggregate['caseCount']} | {aggregate['hardConstraintViolationRate']:.6f} |"
        for name, aggregate in report["slices"].items()
    )
    lines.extend(
        [
            "",
            "## 축별 필터 지표(#334)",
            "",
            f"- axes spec: `{report['filterAxesSpec']['version']}` / "
            f"`{report['filterAxesSpec']['sha256']}` "
            f"(emptyAxisRule={report['filterAxesSpec']['emptyAxisRule']})",
            "- 기존 filter accuracy(합집합 분모 단일값)와 병행 — 정의·관계는 "
            "`evals/filter_axes/README.md` 참조. 기본 scripted adapter는 expectedFilters를 "
            "그대로 내므로 아래 값이 구조상 전부 1.0/None입니다(model_eval/ablation에서 실측).",
            "",
            "| axis | support | spurious | missing | valueStrict P | valueStrict R | "
            "valueStrict F1 | presence P | presence R |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for axis, aggregate in sorted(overall["filterAxes"].items()):
        counts = aggregate["counts"]
        value_strict = aggregate["valueStrict"]
        presence = aggregate["presence"]
        lines.append(
            f"| {axis} | {aggregate['support']} | {counts['spurious']} | {counts['missing']} | "
            f"{_fmt(value_strict['precision'])} | {_fmt(value_strict['recall'])} | "
            f"{_fmt(value_strict['f1'])} | {_fmt(presence['precision'])} | "
            f"{_fmt(presence['recall'])} |"
        )
    return "\n".join(lines) + "\n"


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6f}"


def write_artifacts(
    output_dir: Path, report: dict[str, Any], run_manifest: dict[str, object]
) -> list[Path]:
    """명시 디렉터리에 case·slice·전체·실패·위반 artifact를 쓴다."""
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "results.json", report)
    _write_json(output_dir / "run_manifest.json", run_manifest)
    (output_dir / "report.md").write_text(_markdown(report), encoding="utf-8", newline="\n")
    _write_csv(
        output_dir / "cases.csv",
        [
            "caseId",
            "slices",
            "rankedProductIds",
            "rankingExcluded",
            "rankingExclusionReason",
            "filterAccuracy",
            "hardConstraintViolated",
            "diversity",
            "duplicateCount",
            "unknownProductIds",
            "metrics",
        ],
        _cases_csv(report),
    )
    _write_csv(
        output_dir / "filter_axes.csv",
        _FILTER_AXES_CSV_FIELDS,
        _filter_axes_csv(report),
    )
    # 리뷰 F3 — hash만으로는 당시 정규화 규칙을 복원할 수 없다. axes.json 바이트 그대로를
    # 동봉해 `filterAxesSpec.sha256`이 이 파일의 SHA-256과 항상 일치하게 한다(같은
    # AXES_SPEC_PATH를 evaluate()와 이 writer가 같은 프로세스에서 읽으므로 보장된다).
    (output_dir / "filter_axes_spec.json").write_bytes(AXES_SPEC_PATH.read_bytes())
    aggregate_rows = [_aggregate_csv("overall", report["overall"])]
    aggregate_rows.extend(
        _aggregate_csv(name, aggregate) for name, aggregate in report["slices"].items()
    )
    _write_csv(
        output_dir / "aggregates.csv",
        list(aggregate_rows[0]),
        aggregate_rows,
    )
    _write_csv(
        output_dir / "violations.csv",
        ["caseId", "productId", "constraint"],
        list(report["violations"]),
    )
    failures = [
        {
            "caseId": case["caseId"],
            "reason": case["rankingExclusionReason"] or "listQualityViolation",
            "duplicateCount": case["duplicateCount"],
            "unknownProductIds": _json_text(case["unknownProductIds"]),
            "violations": _json_text(case["violations"]),
        }
        for case in report["cases"]
        if case["rankingExclusionReason"] == "emptyRelevance"
        or case["duplicateCount"]
        or case["unknownProductIds"]
        or case["violations"]
    ]
    _write_csv(
        output_dir / "failures.csv",
        ["caseId", "reason", "duplicateCount", "unknownProductIds", "violations"],
        failures,
    )
    return sorted(output_dir.iterdir())


def normalize_artifacts(output_dir: Path) -> dict[str, bytes]:
    """run 섹션의 실행 인스턴스 메타데이터를 제거해 artifact 결정론을 비교한다."""
    normalized: dict[str, bytes] = {}
    for path in sorted(output_dir.iterdir()):
        if path.name == "run_manifest.json":
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest.pop("run", None)
            normalized[path.name] = (_json_text(manifest) + "\n").encode()
        else:
            normalized[path.name] = path.read_bytes()
    return normalized
