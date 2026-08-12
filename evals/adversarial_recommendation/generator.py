"""Curated family seed를 결정론적 JSONL dataset으로 확장한다."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from evals.adversarial_recommendation.schema import (
    BehavioralOracle,
    CandidateJudgment,
    DeterministicOracle,
    EvalCase,
    FamilySeed,
    NumericConstraint,
    Oracle,
    OracleInput,
    SeedDocument,
)

ROOT = Path(__file__).resolve().parent
SEEDS_PATH = ROOT / "seeds" / "families.json"
CASES_PATH = ROOT / "cases" / "prototype.jsonl"
MANIFEST_PATH = ROOT / "manifest.json"
REVIEW_PATH = ROOT / "reviews" / "manual_review_20pct.json"

_PATH_TOKEN = re.compile(r"([^.[\]]+)|\[(\d+)\]")


def _tokens(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    for name, index in _PATH_TOKEN.findall(path):
        tokens.append(int(index) if index else name)
    if not tokens:
        raise ValueError(f"invalid mutation path: {path!r}")
    return tokens


def get_path(value: Any, path: str) -> Any:
    current = value
    for token in _tokens(path):
        current = current[token]
    return current


def set_path(value: Any, path: str, replacement: Any) -> None:
    current = value
    tokens = _tokens(path)
    for token in tokens[:-1]:
        current = current[token]
    current[tokens[-1]] = replacement


def _is_missing(candidate: dict[str, Any], field: str) -> bool:
    if candidate.get(field) is None:
        return True
    return field == "rating" and candidate.get("reviewCount") == 0


def _compare(value: int | float, constraint: NumericConstraint) -> bool:
    threshold = constraint.threshold
    return {
        "ge": value >= threshold,
        "gt": value > threshold,
        "le": value <= threshold,
        "lt": value < threshold,
        "eq": value == threshold,
    }[constraint.operator]


def compute_deterministic_oracle(
    candidates: list[dict[str, Any]], oracle_input: OracleInput
) -> DeterministicOracle:
    judgments: list[CandidateJudgment] = []
    for candidate in candidates:
        failed: list[str] = []
        missing: list[str] = []
        excludes_missing = False
        for constraint in oracle_input.constraints:
            field = constraint.candidate_field
            label = f"{field}:{constraint.operator}:{constraint.threshold}"
            if _is_missing(candidate, field):
                missing.append(field)
                excludes_missing = excludes_missing or constraint.missing_policy == "exclude"
            elif not _compare(candidate[field], constraint):
                failed.append(label)
        if failed or excludes_missing:
            outcome = "ineligible"
        elif missing:
            outcome = "unknown"
        else:
            outcome = "eligible"
        judgments.append(
            CandidateJudgment(
                product_id=candidate["productId"],
                outcome=outcome,
                failed_constraints=failed,
                missing_fields=missing,
            )
        )

    eligible = [item.product_id for item in judgments if item.outcome == "eligible"]
    ineligible = [item.product_id for item in judgments if item.outcome == "ineligible"]
    unknown = [item.product_id for item in judgments if item.outcome == "unknown"]
    return DeterministicOracle(
        constraints=oracle_input.constraints,
        candidate_judgments=judgments,
        eligible_product_ids=eligible,
        ineligible_product_ids=ineligible,
        unknown_product_ids=unknown,
        minimum_eligible_candidates=oracle_input.minimum_eligible_candidates,
        conflict_detected=len(eligible) < oracle_input.minimum_eligible_candidates,
    )


def _oracle(candidates: list[dict[str, Any]], source: OracleInput) -> Oracle:
    return Oracle(
        deterministic=compute_deterministic_oracle(candidates, source),
        behavioral=BehavioralOracle.model_validate(source.behavioral.model_dump()),
    )


def _base_case(document: SeedDocument, family: FamilySeed) -> EvalCase:
    base = family.base
    return EvalCase(
        schema_version=document.schema_version,
        dataset_version=document.dataset_version,
        case_id=base.case_id,
        family_id=family.family_id,
        category=family.category,
        difficulty=family.difficulty,
        capability_under_test=family.capability_under_test,
        test_type=family.test_type,
        user_request=copy.deepcopy(base.user_request),
        candidates=copy.deepcopy(base.candidates),
        mutation={
            "role": "seed",
            "baseCaseId": None,
            "targetField": family.target_field,
            "targetCandidateId": family.target_candidate_id,
            "changes": [],
        },
        forbidden_behavior=family.forbidden_behavior,
        oracle=_oracle(base.candidates, base.oracle_input),
    )


def _variant_case(
    document: SeedDocument, family: FamilySeed, base: EvalCase, variant_index: int
) -> EvalCase:
    variant = family.variants[variant_index]
    stimulus = {
        "userRequest": copy.deepcopy(base.user_request),
        "candidates": copy.deepcopy(base.candidates),
    }
    for change in variant.changes:
        actual_before = get_path(stimulus, change.path)
        if actual_before != change.before:
            raise ValueError(
                f"{variant.case_id}: {change.path} before mismatch: "
                f"{actual_before!r} != {change.before!r}"
            )
        set_path(stimulus, change.path, copy.deepcopy(change.after))
    role = "contrast" if family.category == "boundary" else "mutation"
    return EvalCase(
        schema_version=document.schema_version,
        dataset_version=document.dataset_version,
        case_id=variant.case_id,
        family_id=family.family_id,
        category=family.category,
        difficulty=family.difficulty,
        capability_under_test=family.capability_under_test,
        test_type=family.test_type,
        user_request=stimulus["userRequest"],
        candidates=stimulus["candidates"],
        mutation={
            "role": role,
            "baseCaseId": base.case_id,
            "targetField": family.target_field,
            "targetCandidateId": family.target_candidate_id,
            "changes": variant.changes,
        },
        forbidden_behavior=family.forbidden_behavior,
        oracle=_oracle(stimulus["candidates"], variant.oracle_input),
    )


def load_seed_document(path: Path = SEEDS_PATH) -> SeedDocument:
    return SeedDocument.model_validate_json(path.read_text(encoding="utf-8"))


def generate_cases(path: Path = SEEDS_PATH) -> list[EvalCase]:
    document = load_seed_document(path)
    cases: list[EvalCase] = []
    for family in document.families:
        base = _base_case(document, family)
        cases.append(base)
        cases.extend(
            _variant_case(document, family, base, index) for index in range(len(family.variants))
        )
    return cases


def _json_line(case: EvalCase) -> bytes:
    payload = case.model_dump(by_alias=True, mode="json")
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()


def build_artifacts(seed_path: Path = SEEDS_PATH) -> tuple[bytes, bytes]:
    document = load_seed_document(seed_path)
    cases = generate_cases(seed_path)
    cases_bytes = b"".join(_json_line(case) for case in cases)
    category_cases = Counter(case.category for case in cases)
    category_families = Counter(family.category for family in document.families)
    manifest = {
        "datasetName": "buyer-adversarial-recommendation",
        "schemaVersion": document.schema_version,
        "datasetVersion": document.dataset_version,
        "generatedAt": document.generated_at,
        "familyCount": len(document.families),
        "caseCount": len(cases),
        "categoryFamilyCounts": dict(sorted(category_families.items())),
        "categoryCaseCounts": dict(sorted(category_cases.items())),
        "files": {
            "seeds/families.json": {
                "sha256": hashlib.sha256(seed_path.read_bytes()).hexdigest(),
                "bytes": len(seed_path.read_bytes()),
            },
            "cases/prototype.jsonl": {
                "sha256": hashlib.sha256(cases_bytes).hexdigest(),
                "bytes": len(cases_bytes),
            },
            "reviews/manual_review_20pct.json": {
                "sha256": hashlib.sha256(REVIEW_PATH.read_bytes()).hexdigest(),
                "bytes": len(REVIEW_PATH.read_bytes()),
            },
        },
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    return cases_bytes, manifest_bytes


def write_dataset(seed_path: Path = SEEDS_PATH) -> None:
    cases_bytes, manifest_bytes = build_artifacts(seed_path)
    CASES_PATH.parent.mkdir(parents=True, exist_ok=True)
    CASES_PATH.write_bytes(cases_bytes)
    MANIFEST_PATH.write_bytes(manifest_bytes)


def load_cases(path: Path = CASES_PATH) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                cases.append(EvalCase.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="committed artifacts와 재생성 결과 비교"
    )
    args = parser.parse_args()
    cases_bytes, manifest_bytes = build_artifacts()
    if args.check:
        if CASES_PATH.read_bytes() != cases_bytes or MANIFEST_PATH.read_bytes() != manifest_bytes:
            print("adversarial dataset artifacts are stale")
            return 1
        print("adversarial dataset artifacts are reproducible")
        return 0
    write_dataset()
    print(f"wrote {CASES_PATH} and {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
