"""Adversarial dataset의 구조·가족·mutation·결정론 oracle 검증."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import random
import re
from typing import Any

from evals.adversarial_recommendation.generator import (
    compute_deterministic_oracle,
    get_path,
)
from evals.adversarial_recommendation.schema import EvalCase, OracleInput

_CATEGORIES = {
    "missing_data",
    "boundary",
    "evidence_conflict",
    "numeric_hallucination",
    "prompt_injection",
    "constraint_conflict",
    "no_evidence",
}
_CASE_ID = re.compile(r"^adv-(?P<category>[a-z_]+)-(?P<index>[0-9]{2})-[a-z0-9_]+$")
_FAMILY_ID = re.compile(r"^fam-(?P<category>[a-z_]+)-(?P<index>[0-9]{2})$")


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    message: str


def _diff_paths(left: Any, right: Any, prefix: str = "") -> set[str]:
    if type(left) is not type(right):
        return {prefix}
    if isinstance(left, dict):
        paths: set[str] = set()
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in left or key not in right:
                paths.add(path)
            else:
                paths |= _diff_paths(left[key], right[key], path)
        return paths
    if isinstance(left, list):
        if len(left) != len(right):
            return {prefix}
        paths: set[str] = set()
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            paths |= _diff_paths(a, b, f"{prefix}[{index}]")
        return paths
    return set() if left == right else {prefix}


def _stimulus(case: EvalCase) -> dict[str, Any]:
    return {"userRequest": case.user_request, "candidates": case.candidates}


def _validate_family(family_id: str, cases: list[EvalCase]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seeds = [case for case in cases if case.mutation.role == "seed"]
    if len(seeds) != 1:
        return [ValidationIssue(f"{family_id}: family must have exactly one seed")]
    seed = seeds[0]
    if len({case.category for case in cases}) != 1:
        issues.append(ValidationIssue(f"{family_id}: family category mismatch"))
    if len({case.capability_under_test for case in cases}) != 1:
        issues.append(ValidationIssue(f"{family_id}: family capability mismatch"))
    if len({case.difficulty for case in cases}) != 1:
        issues.append(ValidationIssue(f"{family_id}: family difficulty mismatch"))
    if len({case.test_type for case in cases}) != 1:
        issues.append(ValidationIssue(f"{family_id}: family testType mismatch"))
    target_metadata = {
        (case.mutation.target_field, case.mutation.target_candidate_id) for case in cases
    }
    if len(target_metadata) != 1:
        issues.append(ValidationIssue(f"{family_id}: family target metadata mismatch"))
    if len({tuple(case.forbidden_behavior) for case in cases}) != 1:
        issues.append(ValidationIssue(f"{family_id}: family forbiddenBehavior mismatch"))
    expected_size = 3 if seed.category == "boundary" else 2
    if len(cases) != expected_size:
        issues.append(
            ValidationIssue(f"{family_id}: expected {expected_size} cases, found {len(cases)}")
        )

    boundary_paths: set[str] = set()
    for case in cases:
        if case is seed:
            continue
        if case.mutation.base_case_id != seed.case_id:
            issues.append(ValidationIssue(f"{case.case_id}: invalid family baseCaseId"))
        declared = {change.path for change in case.mutation.changes}
        if len(case.mutation.changes) != 1:
            issues.append(
                ValidationIssue(
                    f"{case.case_id}: atomic mutation requires exactly one changed path"
                )
            )
        actual = _diff_paths(_stimulus(seed), _stimulus(case))
        if declared != actual:
            issues.append(
                ValidationIssue(
                    f"{case.case_id}: declared mutation paths {sorted(declared)} "
                    f"do not match actual {sorted(actual)}"
                )
            )
        for change in case.mutation.changes:
            try:
                before = get_path(_stimulus(seed), change.path)
                after = get_path(_stimulus(case), change.path)
            except (KeyError, IndexError, TypeError) as exc:
                issues.append(ValidationIssue(f"{case.case_id}: invalid mutation path: {exc}"))
                continue
            if before != change.before or after != change.after:
                issues.append(ValidationIssue(f"{case.case_id}: mutation before/after mismatch"))
        target_field = case.mutation.target_field
        target_id = case.mutation.target_candidate_id
        expected_target_path: str | None = None
        if target_id is None and target_field is not None:
            expected_target_path = f"userRequest.{target_field}"
        elif target_id is not None and target_field is not None:
            target_index = next(
                (
                    index
                    for index, candidate in enumerate(seed.candidates)
                    if candidate.get("productId") == target_id
                ),
                None,
            )
            if target_index is None:
                issues.append(
                    ValidationIssue(f"{case.case_id}: targetCandidateId is not in seed candidates")
                )
            else:
                expected_target_path = f"candidates[{target_index}].{target_field}"
        if expected_target_path is None or declared != {expected_target_path}:
            issues.append(
                ValidationIssue(
                    f"{case.case_id}: targetField/targetCandidateId do not bind mutation path"
                )
            )
        if seed.category == "boundary":
            boundary_paths |= actual

    if seed.category == "boundary":
        if len(boundary_paths) != 1:
            issues.append(
                ValidationIssue(
                    f"{family_id}: boundary family changed non-target data: {sorted(boundary_paths)}"
                )
            )
        elif seed.mutation.target_field and not next(iter(boundary_paths)).endswith(
            f".{seed.mutation.target_field}"
        ):
            issues.append(ValidationIssue(f"{family_id}: boundary family target field mismatch"))
        constraints = [
            tuple(
                (
                    item.candidate_field,
                    item.operator,
                    item.threshold,
                    item.missing_policy,
                )
                for item in case.oracle.deterministic.constraints
            )
            for case in cases
        ]
        if len(set(constraints)) != 1:
            issues.append(ValidationIssue(f"{family_id}: boundary constraints must be identical"))
        else:
            target_constraints = [
                item for item in constraints[0] if item[0] == seed.mutation.target_field
            ]
            if len(target_constraints) != 1:
                issues.append(
                    ValidationIssue(
                        f"{family_id}: boundary family must have exactly one target constraint"
                    )
                )
                return issues
            field, _operator, threshold, _missing_policy = target_constraints[0]
            target_id = seed.mutation.target_candidate_id
            values = []
            for case in cases:
                candidate = next(
                    (item for item in case.candidates if item.get("productId") == target_id),
                    None,
                )
                if candidate is None:
                    values = []
                    break
                values.append(candidate.get(field))
            numeric = all(
                isinstance(value, (int, float)) and not isinstance(value, bool) for value in values
            )
            if (
                field != seed.mutation.target_field
                or not numeric
                or len(set(values)) != 3
                or threshold not in values
                or not min(values) < threshold < max(values)
            ):
                issues.append(
                    ValidationIssue(
                        f"{family_id}: boundary family must contain below/equal/above target values"
                    )
                )
    return issues


def _validate_oracle(case: EvalCase) -> list[ValidationIssue]:
    source = OracleInput(
        constraints=case.oracle.deterministic.constraints,
        minimum_eligible_candidates=case.oracle.deterministic.minimum_eligible_candidates,
        behavioral=case.oracle.behavioral,
    )
    recomputed = compute_deterministic_oracle(case.candidates, source)
    if recomputed != case.oracle.deterministic:
        return [ValidationIssue(f"{case.case_id}: deterministic oracle does not match raw data")]
    return []


def _validate_missing_target(case: EvalCase) -> list[ValidationIssue]:
    if case.category != "missing_data" or case.mutation.role == "seed":
        return []
    target_field = case.mutation.target_field
    target_id = case.mutation.target_candidate_id
    if target_id is None or target_field is None:
        return [ValidationIssue(f"{case.case_id}: missing_data target is not resolvable")]
    target_index = next(
        (index for index, item in enumerate(case.candidates) if item.get("productId") == target_id),
        None,
    )
    if target_index is None:
        return [ValidationIssue(f"{case.case_id}: missing_data target is not resolvable")]
    path = f"candidates[{target_index}].{target_field}"
    try:
        value = get_path(_stimulus(case), path)
    except (KeyError, IndexError, TypeError):
        value = None
    if value is not None:
        return [ValidationIssue(f"{case.case_id}: missing_data target must be absent or null")]
    return []


def validate_cases(cases: list[EvalCase]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    case_ids = [case.case_id for case in cases]
    for case_id, count in Counter(case_ids).items():
        if count > 1:
            issues.append(ValidationIssue(f"duplicate caseId: {case_id}"))

    unknown_categories = {case.category for case in cases} - _CATEGORIES
    if unknown_categories:
        issues.append(ValidationIssue(f"unknown categories: {sorted(unknown_categories)}"))

    families: dict[str, list[EvalCase]] = defaultdict(list)
    for case in cases:
        case_match = _CASE_ID.fullmatch(case.case_id)
        family_match = _FAMILY_ID.fullmatch(case.family_id)
        if (
            case_match is None
            or family_match is None
            or case_match.group("category") != case.category
            or family_match.group("category") != case.category
            or case_match.group("index") != family_match.group("index")
        ):
            issues.append(
                ValidationIssue(
                    f"{case.case_id}: caseId/familyId components disagree with metadata"
                )
            )
        families[case.family_id].append(case)
        issues.extend(_validate_oracle(case))
        issues.extend(_validate_missing_target(case))
    for family_id, family_cases in families.items():
        issues.extend(_validate_family(family_id, family_cases))

    capabilities: dict[tuple[str, str], list[str]] = defaultdict(list)
    for family_id, family_cases in families.items():
        first = family_cases[0]
        normalized = " ".join(first.capability_under_test.lower().split())
        capabilities[(first.category, normalized)].append(family_id)
    for (category, capability), family_ids in sorted(capabilities.items()):
        if len(family_ids) > 1:
            issues.append(
                ValidationIssue(
                    f"duplicate capabilityUnderTest in {category}: {capability!r} "
                    f"used by {sorted(family_ids)}"
                )
            )

    category_family_counts = Counter(family_cases[0].category for family_cases in families.values())
    for category in sorted(_CATEGORIES):
        if category_family_counts[category] != 30:
            issues.append(
                ValidationIssue(
                    f"category {category} must have 30 families, "
                    f"found {category_family_counts[category]}"
                )
            )
    return issues


def validate_manual_review(cases: list[EvalCase], path: Path) -> list[ValidationIssue]:
    """20% stratified review artifact가 고정 표본과 완료 상태를 보존하는지 검증한다."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        sampling = document["sampling"]
        reviews = document["reviews"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return [ValidationIssue(f"manual review artifact is invalid: {exc}")]

    families: dict[str, list[EvalCase]] = defaultdict(list)
    by_category: dict[str, list[str]] = defaultdict(list)
    for case in cases:
        families[case.family_id].append(case)
    for family_id, family_cases in families.items():
        by_category[family_cases[0].category].append(family_id)

    seed = sampling.get("seed")
    per_category = sampling.get("familiesPerCategory")
    if not isinstance(seed, int) or not isinstance(per_category, int):
        return [ValidationIssue("manual review sampling seed/count must be integers")]
    issues: list[ValidationIssue] = []
    if sampling.get("fraction") != 0.2 or any(
        len(family_ids) * 0.2 != per_category for family_ids in by_category.values()
    ):
        issues.append(ValidationIssue("manual review must cover exactly 20% of every category"))
    rng = random.Random(seed)
    expected = {
        family_id
        for category in sorted(by_category)
        for family_id in rng.sample(sorted(by_category[category]), per_category)
    }
    actual = {entry.get("familyId") for entry in reviews if isinstance(entry, dict)}
    if actual != expected or len(reviews) != len(expected):
        issues.append(ValidationIssue("manual review does not match deterministic 20% sample"))

    checks = (
        "failureModePresent",
        "atomicity",
        "evidenceSufficiency",
        "nonTriviality",
    )
    for entry in reviews:
        if not isinstance(entry, dict):
            issues.append(ValidationIssue("manual review entry must be an object"))
            continue
        family_id = entry.get("familyId")
        family_cases = families.get(family_id, [])
        if not family_cases:
            issues.append(ValidationIssue(f"manual review references unknown family: {family_id}"))
            continue
        expected_case_ids = {case.case_id for case in family_cases}
        if set(entry.get("reviewedCaseIds", [])) != expected_case_ids:
            issues.append(ValidationIssue(f"{family_id}: manual review case coverage mismatch"))
        if entry.get("category") != family_cases[0].category:
            issues.append(ValidationIssue(f"{family_id}: manual review category mismatch"))
        for check in checks:
            result = entry.get(check)
            if (
                not isinstance(result, dict)
                or result.get("status") != "pass"
                or not result.get("note")
            ):
                issues.append(ValidationIssue(f"{family_id}: incomplete manual review {check}"))
    if document.get("openIssues"):
        issues.append(ValidationIssue("manual review has unresolved openIssues"))
    return issues
