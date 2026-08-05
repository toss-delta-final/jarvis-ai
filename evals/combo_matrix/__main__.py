"""재생성 CLI — `python -m evals.combo_matrix regenerate` (§8 검증 명령).

**케이스를 다시 만들 뿐 기대동작(`expected_behavior.jsonl`)은 건드리지 않는다** — 그건 사람이
코드 근거를 확인해 쓴 것이다(§4). axes.json 이 바뀌어 케이스 구성이 달라지면 expected_behavior 도
같이 갱신해야 하고, 그건 이 커맨드의 책임 밖이다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

from evals.combo_matrix.generator import (
    add_directed_cases,
    add_perturbations,
    assemble_cases,
    generate,
)
from evals.combo_matrix.loader import (
    AXES_PATH,
    CASES_PATH,
    MANIFEST_PATH,
    dump_cases_jsonl,
    load_axes,
)
from evals.combo_matrix.schema import Manifest


def regenerate(*, write: bool) -> tuple[str, dict]:
    doc = load_axes()
    result = generate(doc)
    cases = assemble_cases(result)
    cases = add_perturbations(doc, cases)
    cases = add_directed_cases(doc, cases)
    text = dump_cases_jsonl(cases)

    manifest = Manifest.model_validate(
        {
            "datasetVersion": doc.dataset_version,
            "seed": doc.seed,
            "axesSha256": hashlib.sha256(AXES_PATH.read_bytes()).hexdigest(),
            "casesSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "caseCount": len(cases),
            "generatorParams": {
                "leavesTotal": result.leaves_total,
                "validPairs": len(result.valid_pairs),
                "uncoveredPairs": sorted(str(p) for p in result.uncovered_pairs),
                "riskTriples": {
                    rt_id: {"targets": len(t), "covered": len(result.risk_triple_covered[rt_id])}
                    for rt_id, t in result.risk_triple_targets.items()
                },
            },
        }
    )
    if write:
        CASES_PATH.write_text(text, encoding="utf-8")
        MANIFEST_PATH.write_text(
            json.dumps(
                manifest.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
    return text, manifest.model_dump(mode="json", by_alias=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.combo_matrix")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "regenerate", help="axes.json 에서 cases/combo_cases.jsonl + manifest.json 재생성"
    )
    args = parser.parse_args(argv)
    if args.command == "regenerate":
        _, manifest = regenerate(write=True)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
