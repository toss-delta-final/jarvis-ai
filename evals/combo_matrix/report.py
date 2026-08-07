"""커버리지 지표 계산 + 미정의 셀 목록 렌더 (§6).

지표는 분자·분모 정의를 동봉한다(evals/README.md 규약 8항):
  - **2-wise 커버율** = 커밋된 케이스가 실제로 덮은 (축,값,축,값) 쌍 수 / 제약 위반을 뺀 유효한
    쌍 전체 수. 유효 쌍은 `axes.json` 제약을 만족하는 모든 완전 할당(leaf)의 합집합으로 정의한다
    (generator.py `enumerate_leaves`+`all_pairs`, 사후 필터가 아니라 생성 단계 정의).
  - **3-wise 커버율(risk_triples)** = 같은 정의를 위험 축쌍 3개에 한정해 적용.
  - **1-wise 비교 참고선** = "축마다 값이 한 번씩만 등장하면 되는" 최소 스위트가 **같은 분모**로
    잰 2-wise 쌍 커버율 — pairwise 가 1-wise 대비 얼마나 더 촘촘한지의 정량 근거(evals/README.md
    규약 1항 정신).
"""

from __future__ import annotations

import random
from collections import defaultdict
from pathlib import Path

from evals.combo_matrix.generator import all_pairs, all_triples, enumerate_leaves, greedy_cover
from evals.combo_matrix.loader import load_axes, load_cases, load_expected
from evals.combo_matrix.schema import ComboCase, ExpectedBehaviorRow

_HERE = Path(__file__).resolve().parent
UNDEFINED_CELLS_PATH = _HERE / "UNDEFINED_CELLS.md"


def _one_wise_suite(leaves: list[dict[str, str]], seed: int) -> list[dict[str, str]]:
    """축마다 값이 최소 1회씩만 등장하면 되는 최소 스위트(비교 참고선 전용, §6)."""
    value_targets: set[tuple[str, str]] = set()
    for leaf in leaves:
        for axis, value in leaf.items():
            value_targets.add((axis, value))

    def value_hits(leaf: dict[str, str]) -> set[tuple[str, str]]:
        return set(leaf.items())

    rng = random.Random(seed)
    return greedy_cover(leaves, rng, set(value_targets), value_hits)


def coverage_report() -> dict:
    doc = load_axes()
    cases = load_cases()
    leaves = enumerate_leaves(doc)

    valid_pairs: set[tuple] = set()
    for leaf in leaves:
        valid_pairs |= all_pairs(leaf)

    covered_pairs: set[tuple] = set()
    for case in cases:
        covered_pairs |= all_pairs(case.axes)
    covered_pairs &= valid_pairs  # perturbation 파생 축은 분모 밖 조합을 만들지 않지만 방어적으로.

    one_wise = _one_wise_suite(leaves, doc.seed)
    one_wise_covered: set[tuple] = set()
    for leaf in one_wise:
        one_wise_covered |= all_pairs(leaf)
    one_wise_covered &= valid_pairs

    risk_triple_report = {}
    for rt in doc.risk_triples:
        axes = tuple(rt.axes)
        targets = {all_triples(leaf, axes) for leaf in leaves if all(a in leaf for a in axes)}
        covered = {
            all_triples(case.axes, axes) for case in cases if all(a in case.axes for a in axes)
        }
        covered &= targets
        risk_triple_report[rt.id] = {
            "axes": list(axes),
            "targetCount": len(targets),
            "coveredCount": len(covered),
            "ratio": len(covered) / len(targets) if targets else 1.0,
        }

    return {
        "caseCount": len(cases),
        "leavesTotal": len(leaves),
        "pairwise": {
            "numerator": len(covered_pairs),
            "denominator": len(valid_pairs),
            "ratio": len(covered_pairs) / len(valid_pairs) if valid_pairs else 1.0,
            "uncovered": sorted(str(p) for p in (valid_pairs - covered_pairs)),
        },
        "oneWiseBaseline": {
            "suiteSize": len(one_wise),
            "numerator": len(one_wise_covered),
            "denominator": len(valid_pairs),
            "ratio": len(one_wise_covered) / len(valid_pairs) if valid_pairs else 1.0,
        },
        "riskTriples": risk_triple_report,
    }


def status_counts(rows: list[ExpectedBehaviorRow]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.status] += 1
    return dict(counts)


def _tuple_key(tup: dict[str, str]) -> tuple:
    return tuple(sorted(tup.items()))


def render_undefined_cells(
    rows: list[ExpectedBehaviorRow], cases_by_id: dict[str, ComboCase]
) -> str:
    """`expected_behavior.jsonl` 의 undefined/partial 을 셀 좌표(undefined_tuple) 기준으로
    중복 제거해 표로 렌더한다(§6) — **여기서 생성만 하고 손으로 쓰지 않는다.**"""
    groups: dict[tuple, list[ExpectedBehaviorRow]] = defaultdict(list)
    for row in rows:
        if row.status in ("undefined", "partial") and row.undefined_tuple:
            groups[_tuple_key(row.undefined_tuple)].append(row)

    lines = [
        "# 미정의 셀 목록 (이슈 #335)",
        "",
        "> 생성: `evals/combo_matrix/report.py::render_undefined_cells` — "
        "`expected_behavior.jsonl` 에서 자동 생성된다. 손으로 고치지 말 것(드리프트 방지).",
        "",
        "이 표의 각 행은 **후속 스펙 이슈로 바로 쓸 수 있는 형식**이다 — 셀 좌표(축값 조합) ·",
        "대표 발화 예시 · 현행 동작 관측(있으면) · 근거 부재 지점 · tracking.",
        "",
        f"총 {len(groups)}개 셀(케이스 {sum(len(v) for v in groups.values())}건에서 발견).",
        "",
    ]

    for key, group_rows in sorted(groups.items()):
        first = group_rows[0]
        example_case = cases_by_id.get(first.case_id)
        cell_desc = ", ".join(f"`{k}={v}`" for k, v in key)
        lines.append(f"## {cell_desc}")
        lines.append("")
        case_ids = ", ".join(r.case_id for r in group_rows)
        lines.append(f"- **케이스**: {case_ids}")
        if example_case:
            lines.append(f"- **대표 발화**: {example_case.utterance}")
        lines.append(f"- **상태**: {first.status}")
        if first.aspect:
            lines.append(f"- **세부(aspect, 좌표 아님)**: {first.aspect}")
        if first.expected:
            lines.append(f"- **현행 동작(정의된 부분)**: {first.expected}")
        for observed_row in group_rows:
            if observed_row.observed:
                lines.append(f"- **관측**({observed_row.case_id}): `{observed_row.observed}`")
        if first.evidence:
            ev = "; ".join(f"{e.kind}:{e.ref}" for e in first.evidence)
            lines.append(f"- **근거(있는 부분)**: {ev}")
        tracking = first.tracking or "미배정 — 후속 스펙 이슈 필요"
        lines.append(f"- **tracking**: {tracking}")
        lines.append("")

    return "\n".join(lines)


def generate_undefined_cells_md(write: bool = True) -> str:
    rows = load_expected()
    cases_by_id = {c.case_id: c for c in load_cases()}
    text = render_undefined_cells(rows, cases_by_id)
    if write:
        # LF 고정 — 저장소 표준(`.gitattributes`)이자, CRLF 로 쓰면 재생성한 사람의 워킹카피만
        # 커밋본과 달라진다(`evals/combo_matrix/__main__.py::regenerate` 주석 참조).
        UNDEFINED_CELLS_PATH.write_text(text, encoding="utf-8", newline="\n")
    return text


if __name__ == "__main__":
    import json

    report = coverage_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    generate_undefined_cells_md(write=True)
    print(f"wrote {UNDEFINED_CELLS_PATH}")
