"""ablation baseline `20260803-dev-full-n5` 축별 재채점 — 존재 증명 산출물(#334 R3-2).

축별 지표가 실제 실측 왜곡(pipeline filterAccuracy 0.067 vs scoring 1.0)을 **원인 축으로
분해함을 커밋된 산출물로 증명**하기 위한 오프라인·무비용 스크립트다. 원본
`evals/ablation/baselines/20260803-dev-full-n5/`는 읽기 전용이며 개변하지 않는다 — 이
스크립트는 그 `caseResults[]`(155행 = 31케이스×5반복)의 `extractedFilters`를 골든셋 dev
`expectedFilters`와 caseId로 조인해 `evals/filter_axes` 축별 지표를 계산할 뿐이다.

uv run python -m evals.filter_axes.rescore_ablation --out evals/filter_axes/baselines/20260803-dev-full-n5-rescored
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from evals.filter_axes.metrics import aggregate_axis_metrics, case_axis_outcomes
from evals.filter_axes.spec import AXES_SPEC_PATH, axes_spec_sha256, load_axes_spec
from evals.goldenset.loader import load_cases

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = REPO_ROOT / "evals" / "ablation" / "baselines" / "20260803-dev-full-n5"
ARMS = ("pipeline", "single_call", "scoring")


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def rescore_arm(
    arm_results: dict[str, Any],
    *,
    expected_by_case: dict[str, dict[str, object]],
    spec: dict[str, Any],
) -> dict[str, Any]:
    """arm 1개의 caseResults[] 전체(반복 분리 없이 155행)를 축별로 재채점한다."""
    case_results = arm_results["caseResults"]
    outcomes = []
    accuracies: list[float] = []
    for row in case_results:
        expected = expected_by_case[row["caseId"]]
        actual = row.get("extractedFilters") or {}
        outcomes.append(case_axis_outcomes(expected, actual, spec))
        accuracy = (row.get("metrics") or {}).get("filterAccuracy")
        if accuracy is not None:
            accuracies.append(accuracy)
    return {
        "arm": arm_results["arm"],
        "rowCount": len(case_results),
        "uniqueCaseCount": arm_results["uniqueCaseCount"],
        "repeats": arm_results["repeatsRequested"],
        "meanFilterAccuracy": _mean(accuracies),
        "filterAxes": aggregate_axis_metrics(outcomes),
    }


def build_rescore_results(baseline_dir: Path = DEFAULT_BASELINE) -> dict[str, Any]:
    """arm별 축별 재채점 결과 + 입력 식별(규약7·8 — 무엇을, 무엇으로 쟀는지 동봉)."""
    spec = load_axes_spec()
    cases = load_cases("dev")
    expected_by_case = {case.case_id: case.expected_filters for case in cases}

    arms: dict[str, Any] = {}
    source_arms: dict[str, Any] = {}
    dataset_version: str | None = None
    dataset_hash: str | None = None
    for arm in ARMS:
        arm_path = baseline_dir / arm / "results.json"
        arm_results = json.loads(arm_path.read_text(encoding="utf-8"))
        if dataset_version is None:
            dataset_version = arm_results["datasetVersion"]
            dataset_hash = arm_results["datasetHash"]
        arms[arm] = rescore_arm(arm_results, expected_by_case=expected_by_case, spec=spec)
        source_arms[arm] = {
            "resultsPath": str(arm_path.relative_to(REPO_ROOT)),
            "sha256": _sha256(arm_path),
        }

    return {
        "source": {
            "baselineDir": str(baseline_dir.relative_to(REPO_ROOT)),
            "arms": source_arms,
            "datasetVersion": dataset_version,
            "datasetHash": dataset_hash,
        },
        "filterAxesSpec": {
            "version": spec["version"],
            "sha256": axes_spec_sha256(AXES_SPEC_PATH),
            "emptyAxisRule": spec["emptyAxisRule"],
        },
        "arms": arms,
    }


def write_rescore_artifacts(output_dir: Path, results: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(
        _json_text(results) + "\n", encoding="utf-8", newline="\n"
    )
    (output_dir / "filter_axes_spec.json").write_bytes(AXES_SPEC_PATH.read_bytes())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ablation baseline 20260803-dev-full-n5 축별 재채점(#334 R3-2, 존재 증명)"
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "evals" / "filter_axes" / "baselines" / "20260803-dev-full-n5-rescored",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    results = build_rescore_results(args.baseline)
    write_rescore_artifacts(args.out, results)
    print(f"arms={list(results['arms'])} → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
