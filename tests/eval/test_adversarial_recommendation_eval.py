from __future__ import annotations

import copy
from collections import Counter
import json
from pathlib import Path
import random

import pytest
from pydantic import ValidationError

from evals.adversarial_recommendation.generator import (
    CASES_PATH,
    MANIFEST_PATH,
    SEEDS_PATH,
    build_artifacts,
    compute_deterministic_oracle,
    load_cases,
)
from evals.adversarial_recommendation.schema import EvalCase, MutationChange, OracleInput
from evals.adversarial_recommendation.validation import validate_cases


pytestmark = pytest.mark.eval
REVIEW_PATH = Path("evals/adversarial_recommendation/reviews/manual_review_20pct.json")


def _messages(cases) -> list[str]:  # noqa: ANN001
    return [issue.message for issue in validate_cases(cases)]


def test_committed_adversarial_dataset_is_valid() -> None:
    cases = load_cases(CASES_PATH)

    assert validate_cases(cases) == []
    assert len({case.family_id for case in cases}) == 210
    assert len(cases) == 450
    assert Counter(case.category for case in cases) == {
        "missing_data": 60,
        "boundary": 90,
        "evidence_conflict": 60,
        "numeric_hallucination": 60,
        "prompt_injection": 60,
        "constraint_conflict": 60,
        "no_evidence": 60,
    }


def test_duplicate_case_id_is_rejected() -> None:
    cases = load_cases(CASES_PATH)
    duplicate = copy.deepcopy(cases[1])
    duplicate.case_id = cases[0].case_id

    assert any("duplicate caseId" in message for message in _messages([*cases, duplicate]))


def test_unintended_stimulus_mutation_is_rejected() -> None:
    cases = load_cases(CASES_PATH)
    mutated = copy.deepcopy(cases)
    case = next(item for item in mutated if item.mutation.role == "mutation")
    case.user_request["threadId"] = "unrecorded-change"

    assert any("declared mutation paths" in message for message in _messages(mutated))


def test_boundary_family_rejects_non_target_drift() -> None:
    cases = load_cases(CASES_PATH)
    mutated = copy.deepcopy(cases)
    case = next(
        item for item in mutated if item.category == "boundary" and item.mutation.role != "seed"
    )
    case.candidates[0]["brandName"] = "경계 외 변형"

    assert any("boundary family" in message for message in _messages(mutated))


def test_missing_data_target_must_be_absent_or_null() -> None:
    cases = load_cases(CASES_PATH)
    mutated = copy.deepcopy(cases)
    case = next(
        item
        for item in mutated
        if item.category == "missing_data" and item.mutation.role == "mutation"
    )
    target = case.mutation.target_field
    assert target is not None
    case.candidates[0][target] = 999

    assert any("missing_data target" in message for message in _messages(mutated))


def test_committed_artifacts_are_deterministically_regenerated() -> None:
    expected_cases, expected_manifest = build_artifacts(SEEDS_PATH)

    assert CASES_PATH.read_bytes() == expected_cases
    assert MANIFEST_PATH.read_bytes() == expected_manifest


def test_buyer_scope_rejects_seller_screen_and_non_i1_candidate_fields() -> None:
    case = load_cases(CASES_PATH)[0].model_dump(by_alias=True, mode="json")
    case["userRequest"]["screen"] = {"pageType": "seller_orders"}
    with pytest.raises(ValidationError, match="buyer request에서 유효하지 않은 screen"):
        EvalCase.model_validate(case)

    case = load_cases(CASES_PATH)[0].model_dump(by_alias=True, mode="json")
    case["candidates"][0]["originalPrice"] = 100_000
    with pytest.raises(ValidationError, match="I-1 후보 field가 아닌 키"):
        EvalCase.model_validate(case)


def test_boundary_family_rejects_threshold_drift_even_with_recomputed_oracle() -> None:
    cases = copy.deepcopy(load_cases(CASES_PATH))
    case = next(
        item for item in cases if item.category == "boundary" and item.mutation.role != "seed"
    )
    case.oracle.deterministic.constraints[0].threshold += 1
    source = OracleInput(
        constraints=case.oracle.deterministic.constraints,
        minimum_eligible_candidates=case.oracle.deterministic.minimum_eligible_candidates,
        behavioral=case.oracle.behavioral,
    )
    case.oracle.deterministic = compute_deterministic_oracle(case.candidates, source)

    assert any("boundary constraints" in message for message in _messages(cases))


def test_declared_multi_path_mutation_is_rejected_as_non_atomic() -> None:
    cases = copy.deepcopy(load_cases(CASES_PATH))
    case = next(
        item
        for item in cases
        if item.category == "no_evidence" and item.mutation.role == "mutation"
    )
    seed = next(item for item in cases if item.case_id == case.mutation.base_case_id)
    before = seed.user_request["threadId"]
    after = f"{before}-extra"
    case.user_request["threadId"] = after
    case.mutation.changes.append(
        MutationChange(path="userRequest.threadId", before=before, after=after)
    )

    assert any("atomic mutation" in message for message in _messages(cases))


def test_mutation_target_metadata_must_match_changed_candidate() -> None:
    cases = copy.deepcopy(load_cases(CASES_PATH))
    case = next(
        item
        for item in cases
        if item.category == "prompt_injection" and item.mutation.role == "mutation"
    )
    case.mutation.target_candidate_id = case.candidates[1]["productId"]

    assert any("targetCandidateId" in message for message in _messages(cases))


def test_family_metadata_and_id_components_cannot_drift() -> None:
    cases = copy.deepcopy(load_cases(CASES_PATH))
    case = next(item for item in cases if item.mutation.role == "mutation")
    case.difficulty = "medium" if case.difficulty != "medium" else "hard"
    case.mutation.target_field = "wrong"
    case.case_id = case.case_id.replace("-01-", "-99-", 1)

    messages = _messages(cases)
    assert any("family difficulty mismatch" in message for message in messages)
    assert any("family target metadata mismatch" in message for message in messages)
    assert any("caseId/familyId components" in message for message in messages)


def test_numeric_oracle_corruption_is_rejected() -> None:
    cases = copy.deepcopy(load_cases(CASES_PATH))
    case = next(item for item in cases if item.oracle.deterministic.eligible_product_ids)
    case.oracle.deterministic.eligible_product_ids = []

    assert any("deterministic oracle" in message for message in _messages(cases))


def test_committed_dataset_has_no_generic_test_brand_placeholder() -> None:
    assert "테스트브랜드" not in SEEDS_PATH.read_text(encoding="utf-8")
    assert "테스트브랜드" not in CASES_PATH.read_text(encoding="utf-8")


def test_duplicate_capability_within_category_is_rejected() -> None:
    cases = copy.deepcopy(load_cases(CASES_PATH))
    first, second = list(dict.fromkeys(case.family_id for case in cases))[:2]
    capability = next(case.capability_under_test for case in cases if case.family_id == first)
    for case in cases:
        if case.family_id == second:
            case.capability_under_test = capability

    assert any("duplicate capabilityUnderTest" in message for message in _messages(cases))


def test_manual_review_covers_twenty_percent_of_each_category() -> None:
    review = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
    entries = review["reviews"]
    seeds = json.loads(SEEDS_PATH.read_text(encoding="utf-8"))["families"]
    by_category: dict[str, list[str]] = {}
    for family in seeds:
        by_category.setdefault(family["category"], []).append(family["familyId"])
    rng = random.Random(20260812)
    expected_sample = {
        family_id
        for category in sorted(by_category)
        for family_id in rng.sample(sorted(by_category[category]), 6)
    }

    assert review["sampling"]["seed"] == 20260812
    assert len(entries) == 42
    assert Counter(entry["category"] for entry in entries) == {
        "missing_data": 6,
        "boundary": 6,
        "evidence_conflict": 6,
        "numeric_hallucination": 6,
        "prompt_injection": 6,
        "constraint_conflict": 6,
        "no_evidence": 6,
    }
    assert {entry["familyId"] for entry in entries} == expected_sample
    assert all(
        entry[check]["status"] == "pass"
        for entry in entries
        for check in (
            "failureModePresent",
            "atomicity",
            "evidenceSufficiency",
            "nonTriviality",
        )
    )
