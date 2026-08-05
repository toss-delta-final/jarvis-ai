"""평가 결과를 결정론적 JSON·Markdown·CSV artifact로 기록한다."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


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


def _aggregate_csv(
    name: str, aggregate: dict[str, Any], *, arm: str = "system"
) -> dict[str, object]:
    return {
        "arm": arm,
        "scope": name,
        "n": aggregate["rankingCaseCount"],
        "confirmatoryLabel": aggregate.get("confirmatoryLabel", ""),
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


def _ndcg10(aggregate: dict[str, Any]) -> float | None:
    return aggregate.get("ndcgAtK", {}).get("10")


def _fmt(value: float | None) -> str:
    return f"{value:.6f}" if value is not None else "N/A"


def _delta(system: float | None, noop: float | None) -> float | None:
    return None if system is None or noop is None else system - noop


def _depth_triplet(candidate_depth: dict[str, Any]) -> str:
    values = (candidate_depth.get("min"), candidate_depth.get("median"), candidate_depth.get("max"))
    if any(value is None for value in values):
        return "N/A"
    return "/".join(str(value) for value in values)


def _markdown(report: dict[str, Any]) -> str:
    overall = report["overall"]
    excluded = ", ".join(overall["rankingExcludedCaseIds"]) or "(없음)"
    gate = ", ".join(report["prGateConstraints"])
    violations = report["violations"]
    noop = report.get("noopBaseline")
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
        "- primary confirmatory metric은 `overall.ndcgAtK.10` 1개뿐이다 — 나머지 cutoff(3·5)와 "
        "슬라이스별 수치는 exploratory다(#328 다중비교 통제 규약).",
        "",
        "## 전체",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| cases | {overall['caseCount']} |",
        f"| ranking cases | {overall['rankingCaseCount']} |",
        f"| nDCG@10 (primary) | {_fmt(_ndcg10(overall))} |",
        f"| filter accuracy | {overall['filterAccuracy']:.6f} |",
        f"| hard constraint violation rate | {overall['hardConstraintViolationRate']:.6f} |",
        f"| coverage | {overall['coverage']:.6f} |",
        f"| diversity | {overall['diversity']:.6f} |",
        f"| MRR | {overall['mrr']:.6f} |",
        f"| candidate depth (min/median/max) | {_depth_triplet(overall['candidateDepth'])} |",
        (
            f"| candidates ≤10 (count/ratio) | {overall['candidateDepth']['shallowCount']}/"
            f"{overall['candidateDepth']['shallowRatio']:.4f} |"
        ),
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
    if noop is not None:
        noop_overall = noop["overall"]
        lines.extend(
            [
                "",
                "## no-op 기준선 비교",
                "",
                f"- {noop.get('definition', 'README `no-op 기준선` 절 참조.')}",
                "- 서로 다른 `datasetHash` 점수와는 비교하지 않는다.",
                "",
                "| metric | system | no-op | delta(system-noop) |",
                "|---|---:|---:|---:|",
                (
                    f"| nDCG@10 | {_fmt(_ndcg10(overall))} | {_fmt(_ndcg10(noop_overall))} | "
                    f"{_fmt(_delta(_ndcg10(overall), _ndcg10(noop_overall)))} |"
                ),
                (
                    f"| MRR | {overall['mrr']:.6f} | {noop_overall['mrr']:.6f} | "
                    f"{_fmt(overall['mrr'] - noop_overall['mrr'])} |"
                ),
                (
                    f"| hard constraint violation rate | {overall['hardConstraintViolationRate']:.6f} | "
                    f"{noop_overall['hardConstraintViolationRate']:.6f} | "
                    f"{_fmt(overall['hardConstraintViolationRate'] - noop_overall['hardConstraintViolationRate'])} |"
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## Slice",
            "",
            "| slice | N(ranking) | cases | HCV | label |",
            "|---|---:|---:|---:|---|",
        ]
    )
    lines.extend(
        f"| {name} | {aggregate['rankingCaseCount']} | {aggregate['caseCount']} | "
        f"{aggregate['hardConstraintViolationRate']:.6f} | "
        f"{aggregate.get('confirmatoryLabel', '')} |"
        for name, aggregate in report["slices"].items()
    )
    return "\n".join(lines) + "\n"


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
    aggregate_rows = [_aggregate_csv("overall", report["overall"], arm="system")]
    aggregate_rows.extend(
        _aggregate_csv(name, aggregate, arm="system")
        for name, aggregate in report["slices"].items()
    )
    noop = report.get("noopBaseline")
    if noop is not None:
        aggregate_rows.append(_aggregate_csv("overall", noop["overall"], arm="noop"))
        aggregate_rows.extend(
            _aggregate_csv(name, aggregate, arm="noop")
            for name, aggregate in noop["slices"].items()
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
