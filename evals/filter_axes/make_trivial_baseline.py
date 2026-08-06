"""trivial baseline(빈 필터) 산출물 생성 — 규약1(`evals/README.md`), 재실행 가능해야 한다(#334).

uv run python -m evals.filter_axes.make_trivial_baseline --out evals/filter_axes/baselines/trivial_empty

goldenset dev의 `expectedFilters`에 대해 예측=빈 필터로 `aggregate_axis_metrics`를 돌린다.
datasetVersion·datasetHash는 goldenset manifest에서 읽는다(하드코딩 금지 — v2 머지 후
재실행만으로 갱신되게 하기 위함).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from evals.filter_axes.metrics import aggregate_axis_metrics, case_axis_outcomes
from evals.filter_axes.spec import AXES_SPEC_PATH, axes_spec_sha256, load_axes_spec
from evals.goldenset.loader import ROOT as GOLDENSET_ROOT
from evals.goldenset.loader import load_cases

DEFAULT_OUT = Path(__file__).parent / "baselines" / "trivial_empty"


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_trivial_baseline_results() -> dict[str, Any]:
    """예측=빈 필터로 dev 케이스 전부를 채점한다 — trivial baseline(규약1)."""
    spec = load_axes_spec()
    manifest = json.loads((GOLDENSET_ROOT / "manifest.json").read_text(encoding="utf-8"))
    cases = load_cases("dev")
    case_outcomes = [case_axis_outcomes(case.expected_filters, {}, spec) for case in cases]
    aggregated = aggregate_axis_metrics(case_outcomes)
    return {
        "datasetVersion": manifest["datasetVersion"],
        "datasetHash": manifest["datasetHash"],
        "caseCount": len(cases),
        "prediction": "emptyFilters",
        "filterAxesSpec": {
            "version": spec["version"],
            "sha256": axes_spec_sha256(AXES_SPEC_PATH),
            "emptyAxisRule": spec["emptyAxisRule"],
        },
        "filterAxes": aggregated,
    }


def write_baseline(output_dir: Path, results: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(
        _json_text(results) + "\n", encoding="utf-8", newline="\n"
    )
    # 리뷰 F3 — hash만으로는 당시 정규화 규칙을 복원할 수 없다. axes.json 바이트를 동봉한다.
    (output_dir / "filter_axes_spec.json").write_bytes(AXES_SPEC_PATH.read_bytes())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="필터 축 trivial(빈 필터) baseline 생성(#334)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    results = build_trivial_baseline_results()
    write_baseline(args.out, results)
    print(f"cases={results['caseCount']} → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
