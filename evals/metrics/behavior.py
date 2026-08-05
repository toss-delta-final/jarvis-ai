"""라벨 없이 도는 INV/DIR 행동 검사(#328 CheckList 공통 규약, 이슈 #333 §2.4).

`behavior_group_id`로 묶인 케이스 그룹을 시스템 노출 목록(``rankedProductIds``)만으로
비교한다. 순위 지표(nDCG 등)와 완전히 분리된 섹션(``behaviorChecks``)으로 산출하며, 이
모듈은 adapter를 직접 부르지 않고 `evals.metrics.runner`가 이미 계산한 case 결과 행을
받는다 — 실행 경로를 중복 구현하지 않는다.

- ``color_synonym``·``word_order``(INV): 그룹의 모든 케이스가 같은 search fixture를
  공유하고 노출 목록이 완전히 같아야 통과한다.
- ``constraint_subset``(DIR): ``expectedFilters``가 더 많은(제약이 강한) 케이스의 노출
  집합이 더 적은(완화된) 케이스의 노출 집합의 부분집합이면 통과한다. 강함/완화 관계는
  라벨이 아니라 ``expectedFilters`` 자체의 포함 관계로 판별한다(라벨 불필요 원칙 유지).
- ``member_recall_ge_guest``(DIR): 같은 그룹의 member 케이스 recall이 guest 케이스
  recall 이상이면 통과한다(#119 선례) — 이 검사만 라벨(``relevantProductIds``)이 필요하다.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from evals.goldenset.schema import GoldenCase

CaseRow = Mapping[str, Any]


def _exposure(row: CaseRow) -> list[int]:
    return list(row["rankedProductIds"])


def _group_cases(cases: Sequence[GoldenCase]) -> dict[str, list[GoldenCase]]:
    groups: dict[str, list[GoldenCase]] = defaultdict(list)
    for case in cases:
        if case.test_type in ("INV", "DIR") and case.behavior_group_id:
            groups[case.behavior_group_id].append(case)
    return dict(groups)


def _check_invariant_exposure(
    group_cases: Sequence[GoldenCase], rows_by_id: dict[str, CaseRow]
) -> dict[str, Any]:
    """color_synonym/word_order: 같은 fixture + 완전히 같은 노출 목록."""
    fixture_ids = {case.search_fixture_id for case in group_cases}
    if len(fixture_ids) != 1:
        return {"passed": False, "reason": "그룹 케이스가 같은 search fixture를 공유하지 않습니다"}
    exposures = [tuple(_exposure(rows_by_id[case.case_id])) for case in group_cases]
    if len(set(exposures)) > 1:
        return {"passed": False, "reason": "그룹 케이스의 노출 목록이 동일하지 않습니다"}
    return {"passed": True, "reason": None}


def _check_constraint_subset(
    group_cases: Sequence[GoldenCase], rows_by_id: dict[str, CaseRow]
) -> dict[str, Any]:
    """constraint_subset: 제약이 더 강한 케이스의 노출이 완화 케이스 노출의 부분집합."""
    if len(group_cases) != 2:
        return {
            "passed": False,
            "reason": f"constraint_subset은 정확히 2케이스(강화/완화) 그룹이어야 합니다: {len(group_cases)}건",
        }
    left, right = group_cases
    left_filters, right_filters = dict(left.expected_filters), dict(right.expected_filters)
    left_is_stricter = left_filters.items() > right_filters.items()
    right_is_stricter = right_filters.items() > left_filters.items()
    if left_is_stricter == right_is_stricter:
        return {
            "passed": False,
            "reason": "expectedFilters의 포함 관계로 제약 강화/완화 케이스를 판별할 수 없습니다",
        }
    stricter, relaxed = (left, right) if left_is_stricter else (right, left)
    stricter_exposure = set(_exposure(rows_by_id[stricter.case_id]))
    relaxed_exposure = set(_exposure(rows_by_id[relaxed.case_id]))
    passed = stricter_exposure <= relaxed_exposure
    return {
        "passed": passed,
        "reason": None
        if passed
        else "제약 강화 케이스의 노출이 완화 케이스 노출의 부분집합이 아닙니다",
        "stricterCaseId": stricter.case_id,
        "relaxedCaseId": relaxed.case_id,
    }


def _full_recall(row: CaseRow) -> float | None:
    relevant = set(row.get("relevantProductIds") or [])
    if not relevant:
        return None
    exposed = set(_exposure(row))
    return len(exposed & relevant) / len(relevant)


def _check_member_recall_ge_guest(
    group_cases: Sequence[GoldenCase], rows_by_id: dict[str, CaseRow]
) -> dict[str, Any]:
    """member_recall_ge_guest: 같은 그룹의 member recall이 guest recall 이상."""
    members = [case for case in group_cases if case.identity.kind == "member"]
    guests = [case for case in group_cases if case.identity.kind == "guest"]
    if len(members) != 1 or len(guests) != 1:
        return {
            "passed": False,
            "reason": (
                "member_recall_ge_guest는 member 1건·guest 1건 쌍이어야 합니다: "
                f"member={len(members)} guest={len(guests)}"
            ),
        }
    member_recall = _full_recall(rows_by_id[members[0].case_id])
    guest_recall = _full_recall(rows_by_id[guests[0].case_id])
    if member_recall is None or guest_recall is None:
        return {
            "passed": False,
            "reason": "recall 계산에 필요한 relevantProductIds가 비어 있습니다",
        }
    passed = member_recall >= guest_recall
    return {
        "passed": passed,
        "reason": None if passed else "member recall이 guest recall보다 낮습니다",
        "memberRecall": member_recall,
        "guestRecall": guest_recall,
    }


_CHECKERS = {
    "color_synonym": _check_invariant_exposure,
    "word_order": _check_invariant_exposure,
    "constraint_subset": _check_constraint_subset,
    "member_recall_ge_guest": _check_member_recall_ge_guest,
}


def evaluate_behavior_checks(
    cases: Sequence[GoldenCase], case_rows: Sequence[CaseRow]
) -> dict[str, Any]:
    """behavior_group_id로 묶인 INV/DIR 케이스를 그룹별로 검사해 순위 지표와 분리된 섹션을 만든다."""
    rows_by_id = {row["caseId"]: row for row in case_rows}
    groups = _group_cases(cases)
    group_results: dict[str, Any] = {}
    for group_id, group_cases in sorted(groups.items()):
        kinds = {case.behavior_kind for case in group_cases}
        case_ids = sorted(case.case_id for case in group_cases)
        if len(kinds) != 1 or None in kinds:
            group_results[group_id] = {
                "behaviorKind": sorted(str(kind) for kind in kinds),
                "caseIds": case_ids,
                "passed": False,
                "reason": "그룹 내 behaviorKind가 일치하지 않거나 비어 있습니다",
            }
            continue
        kind = kinds.pop()
        assert kind is not None  # 위에서 None 여부를 이미 걸렀다
        missing = [case.case_id for case in group_cases if case.case_id not in rows_by_id]
        if missing:
            group_results[group_id] = {
                "behaviorKind": kind,
                "caseIds": case_ids,
                "passed": False,
                "reason": f"case 결과가 없습니다: {sorted(missing)}",
            }
            continue
        checker = _CHECKERS.get(kind)
        if checker is None:
            group_results[group_id] = {
                "behaviorKind": kind,
                "caseIds": case_ids,
                "passed": False,
                "reason": f"알 수 없는 behaviorKind입니다: {kind}",
            }
            continue
        result = checker(group_cases, rows_by_id)
        group_results[group_id] = {"behaviorKind": kind, "caseIds": case_ids, **result}
    passed_count = sum(1 for result in group_results.values() if result["passed"])
    return {
        "groupCount": len(group_results),
        "passedCount": passed_count,
        "failedCount": len(group_results) - passed_count,
        "groups": group_results,
    }
