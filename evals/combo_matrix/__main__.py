"""재생성 CLI — `python -m evals.combo_matrix regenerate` (§8 검증 명령).

**케이스를 다시 만들 뿐 기대동작(`expected_behavior.jsonl`)은 건드리지 않는다** — 그건 사람이
코드 근거를 확인해 쓴 것이다(§4). axes.json 이 바뀌어 케이스 구성이 달라지면 expected_behavior 도
같이 갱신해야 하고, 그건 이 커맨드의 책임 밖이다.

`refresh-observed` 서브커맨드(이슈 #381 D6)는 별개다 — `expected_behavior.jsonl` 의 `observed`
필드**만** 러너(`runner.py::observe`) 재실행으로 덮어쓴다. **이것도 덮어쓰기 도구이지 판정
도구가 아니다** — `observed` 값이 바뀌었다고 그게 "고쳐졌다"는 뜻은 아니다. 바뀐 값이 실측
개선인지, 회귀인지, 단순 필드 추가인지는 사람이 코드 근거로 판단해 README("관측 재생성 이력")에
남겨야 한다.
"""

from __future__ import annotations

import argparse
import asyncio
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
    EXPECTED_PATH,
    MANIFEST_PATH,
    dump_cases_jsonl,
    dump_expected_jsonl,
    load_axes,
    load_cases,
    load_expected,
)
from evals.combo_matrix.runner import observe
from evals.combo_matrix.schema import ExpectedBehaviorRow, Manifest


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
        # `newline="\n"` 필수 — 저장소 표준이 LF 다(`.gitattributes`: `* text=auto eol=lf`,
        # "CRLF 오염 재발 방지"). Windows 기본 변환으로 CRLF 를 쓰면 `axesSha256` 이
        # `read_bytes()` 기반이라 로컬 해시와 CI(LF 체크아웃) 해시가 갈려, 재생성한 사람만
        # 통과하고 CI 는 깨진다(#386 에서 실제로 밟았다).
        CASES_PATH.write_text(text, encoding="utf-8", newline="\n")
        MANIFEST_PATH.write_text(
            json.dumps(
                manifest.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return text, manifest.model_dump(mode="json", by_alias=True)


async def refresh_observed(*, write: bool) -> tuple[str, list[ExpectedBehaviorRow]]:
    """`expected_behavior.jsonl` 의 `observed` 필드만 러너 재실행으로 덮어쓴다(이슈 #381 D6).

    **덮어쓰기 도구이지 판정 도구가 아니다** — `observation_mode=ci` 인 행만 갱신하고, 그 외
    필드(`expected`·`evidence`·`status`·`undefined_tuple`·`aspect`·`linked`·`tracking`)·행 순서·
    `manual` 행의 `observed`(항상 `null`)는 절대 건드리지 않는다. 바뀐 값이 실측 개선인지
    회귀인지 단순 필드 추가인지는 사람이 코드 근거로 판단해 README("관측 재생성 이력")에
    남겨야 한다 — 이 함수는 그 판단을 하지 않는다.
    """
    cases = {c.case_id: c for c in load_cases()}
    rows = load_expected()
    updated_rows: list[ExpectedBehaviorRow] = []
    for row in rows:
        case = cases[row.case_id]
        if case.observation_mode != "ci":
            updated_rows.append(row)
            continue
        new_observed = await observe(case)
        updated_rows.append(row.model_copy(update={"observed": new_observed}))
    text = dump_expected_jsonl(updated_rows)
    if write:
        EXPECTED_PATH.write_text(text, encoding="utf-8", newline="\n")  # LF 고정(위 근거 참조)
    return text, updated_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.combo_matrix")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "regenerate", help="axes.json 에서 cases/combo_cases.jsonl + manifest.json 재생성"
    )
    sub.add_parser(
        "refresh-observed",
        help="ci 행의 expected_behavior.jsonl observed 필드만 러너 재실행으로 갱신(판정은 사람이)",
    )
    args = parser.parse_args(argv)
    if args.command == "regenerate":
        _, manifest = regenerate(write=True)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    if args.command == "refresh-observed":
        _, rows = asyncio.run(refresh_observed(write=True))
        print(f"refreshed observed for {sum(1 for r in rows if r.observed is not None)} ci rows")
        print(f"wrote {EXPECTED_PATH}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
