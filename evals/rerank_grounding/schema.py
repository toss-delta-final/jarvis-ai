"""Strict fixture schema for the rerank-grounding probe."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from app.agents.buyer.recommendation.overall_comment_grounding import (
    FinalRecommendationView,
    validate_and_render_overall_comment,
)
from app.core.config import Settings
from app.schemas.spring import SpringProduct

TestType = Literal["MFT", "INV", "DIR"]
MutationField = Literal["candidateName", "profileSummary"]

DEFAULT_FIXTURE_PATH = Path(__file__).with_name("fixtures") / "rerank_grounding_v2.json"

_SUPPORTED_OVERALL_CODES = {
    "TOP_REVIEW_COUNT",
    "ALL_RATING_HIGH",
    "ALL_WITHIN_TOTAL_BUDGET",
    "NO_VERIFIABLE_OVERALL_CLAIM",
}
_FORBIDDEN_ONLY_OVERALL_CODES = {"POPULARITY_TOP", "VALUE_FOR_MONEY_TOP"}
_ALL_OVERALL_ORACLE_CODES = _SUPPORTED_OVERALL_CODES | _FORBIDDEN_ONLY_OVERALL_CODES
_ORACLE_SETTINGS = Settings.model_construct(rating_tier_good=4.0)


@dataclass(frozen=True)
class CandidateFixture:
    product_id: int
    name: str
    price: int | None
    rating: float | None
    review_count: int | None
    category_name: str | None
    brand: str | None


@dataclass(frozen=True)
class FinalViewFixture:
    list_type: Literal["PICK_ONE", "BUY_ALL"]
    total_budget: int | None
    product_groups: tuple[tuple[int, ...], ...]

    def as_final_view(self) -> FinalRecommendationView:
        return FinalRecommendationView(
            list_type=self.list_type,
            total_budget=self.total_budget,
            product_groups=self.product_groups,
        )


@dataclass(frozen=True)
class OverallOracle:
    allowed_claim_codes: tuple[str, ...]
    forbidden_claim_codes: tuple[str, ...]


@dataclass(frozen=True)
class GroundingCase:
    case_id: str
    test_type: TestType
    pair_id: str | None
    mutation_field: MutationField | None
    query: str
    profile_summary: str | None
    candidates: tuple[CandidateFixture, ...]
    need_of: dict[int, str] | None
    per_need: int | None
    final_view: FinalViewFixture
    overall_oracle: OverallOracle


@dataclass(frozen=True)
class FixtureSet:
    fixture_version: str
    schema_version: str
    cases: tuple[GroundingCase, ...]


def fixture_sha256(path: Path = DEFAULT_FIXTURE_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _nullable_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value


def _nullable_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer or null")
    return value


def _candidate(payload: object) -> CandidateFixture:
    if not isinstance(payload, dict):
        raise ValueError("candidate must be an object")
    product_id = payload.get("productId")
    if isinstance(product_id, bool) or not isinstance(product_id, int):
        raise ValueError("productId must be an integer")
    rating_value = payload.get("rating")
    if rating_value is not None and (
        isinstance(rating_value, bool) or not isinstance(rating_value, (int, float))
    ):
        raise ValueError("rating must be numeric or null")
    return CandidateFixture(
        product_id=product_id,
        name=_required_string(payload, "name"),
        price=_nullable_int(payload, "price"),
        rating=float(rating_value) if rating_value is not None else None,
        review_count=_nullable_int(payload, "reviewCount"),
        category_name=_nullable_string(payload, "categoryName"),
        brand=_nullable_string(payload, "brand"),
    )


def _need_of(payload: object, candidate_ids: set[int]) -> dict[int, str] | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("needOf must be an object or null")
    result: dict[int, str] = {}
    for raw_id, raw_need in payload.items():
        try:
            product_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("needOf keys must be product IDs") from exc
        if product_id not in candidate_ids:
            raise ValueError(f"needOf productId is not a candidate: {product_id}")
        if not isinstance(raw_need, str) or not raw_need.strip():
            raise ValueError("needOf values must be non-empty strings")
        result[product_id] = raw_need
    return result


def _final_view(payload: object, candidate_ids: set[int]) -> FinalViewFixture:
    if not isinstance(payload, dict):
        raise ValueError("finalView must be an object")
    list_type = payload.get("listType")
    if list_type not in {"PICK_ONE", "BUY_ALL"}:
        raise ValueError(f"unknown listType: {list_type}")
    total_budget = _nullable_int(payload, "totalBudget")
    if list_type == "PICK_ONE" and total_budget is not None:
        raise ValueError("PICK_ONE finalView totalBudget must be null")
    raw_groups = payload.get("productGroups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("productGroups must be a non-empty list")
    groups: list[tuple[int, ...]] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, list) or not raw_group:
            raise ValueError("finalView group must be a non-empty list")
        if not all(
            isinstance(product_id, int) and not isinstance(product_id, bool)
            for product_id in raw_group
        ):
            raise ValueError("finalView productId must be an integer")
        if len(raw_group) != len(set(raw_group)):
            raise ValueError("duplicate productId in finalView group")
        for product_id in raw_group:
            if product_id not in candidate_ids:
                raise ValueError(f"finalView productId is not a candidate: {product_id}")
        groups.append(tuple(raw_group))
    return FinalViewFixture(
        list_type=list_type,
        total_budget=total_budget,
        product_groups=tuple(groups),
    )


def _claim_list(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(code, str) for code in value):
        raise ValueError(f"{key} must be a claim-code list")
    if len(value) != len(set(value)):
        raise ValueError(f"duplicate claim in {key}")
    unknown = set(value) - _ALL_OVERALL_ORACLE_CODES
    if unknown:
        raise ValueError(f"unknown overall oracle claim: {sorted(unknown)}")
    return tuple(value)


def _overall_oracle(payload: object) -> OverallOracle:
    if not isinstance(payload, dict):
        raise ValueError("overallOracle must be an object")
    allowed = _claim_list(payload, "allowedOverallClaims")
    forbidden = _claim_list(payload, "forbiddenOverallClaims")
    if set(allowed) & set(forbidden):
        raise ValueError("overall oracle claim overlap")
    if set(allowed) | set(forbidden) != _ALL_OVERALL_ORACLE_CODES:
        raise ValueError("overall oracle must classify every registered claim")
    if set(allowed) & _FORBIDDEN_ONLY_OVERALL_CODES:
        raise ValueError("overall oracle contradicts raw facts")
    return OverallOracle(allowed_claim_codes=allowed, forbidden_claim_codes=forbidden)


def _canonical_proposal(code: str, final_view: FinalViewFixture) -> dict[str, object]:
    final_ids = list(
        dict.fromkeys(product_id for group in final_view.product_groups for product_id in group)
    )
    if code == "TOP_REVIEW_COUNT":
        return {
            "claimCode": code,
            "scope": "FINAL_EXPOSED_PRODUCTS",
            "subjectProductIds": final_ids[:1],
            "evidenceFields": ["reviewCount"],
        }
    if code == "ALL_RATING_HIGH":
        return {
            "claimCode": code,
            "scope": "FINAL_EXPOSED_PRODUCTS",
            "subjectProductIds": final_ids,
            "evidenceFields": ["ratingLevel"],
        }
    if code == "ALL_WITHIN_TOTAL_BUDGET":
        return {
            "claimCode": code,
            "scope": "FINAL_RECOMMENDATION_LISTS",
            "subjectProductIds": final_ids,
            "evidenceFields": ["price", "totalBudget"],
        }
    if code == "NO_VERIFIABLE_OVERALL_CLAIM":
        return {
            "claimCode": code,
            "scope": "FINAL_EXPOSED_PRODUCTS",
            "subjectProductIds": [],
            "evidenceFields": [],
        }
    return {
        "claimCode": code,
        "scope": "FINAL_EXPOSED_PRODUCTS",
        "subjectProductIds": final_ids[:1],
        "evidenceFields": [],
    }


def _validate_overall_oracle(
    *,
    case_id: str,
    candidates: tuple[CandidateFixture, ...],
    final_view: FinalViewFixture,
    oracle: OverallOracle,
) -> None:
    products_by_id = {
        candidate.product_id: SpringProduct(
            product_id=candidate.product_id,
            name=candidate.name,
            price=candidate.price,
            rating=candidate.rating,
            review_count=candidate.review_count,
            category=candidate.category_name,
            brand=candidate.brand,
        )
        for candidate in candidates
    }
    actually_supported: set[str] = set()
    for code in _ALL_OVERALL_ORACLE_CODES:
        decision = validate_and_render_overall_comment(
            [_canonical_proposal(code, final_view)],
            final_view=final_view.as_final_view(),
            products_by_id=products_by_id,
            settings=_ORACLE_SETTINGS,
        )
        if code in decision.supported_claim_codes:
            actually_supported.add(code)
    if set(oracle.allowed_claim_codes) != actually_supported:
        raise ValueError(f"overall oracle contradicts raw facts: {case_id}")


def _case(payload: object) -> GroundingCase:
    if not isinstance(payload, dict):
        raise ValueError("case must be an object")
    test_type = payload.get("testType")
    if test_type not in {"MFT", "INV", "DIR"}:
        raise ValueError(f"unknown testType: {test_type}")
    mutation_field = payload.get("mutationField")
    if mutation_field not in {None, "candidateName", "profileSummary"}:
        raise ValueError(f"unknown mutationField: {mutation_field}")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("candidates must be a non-empty list")
    candidates = tuple(_candidate(value) for value in raw_candidates)
    candidate_ids = [candidate.product_id for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError(f"duplicate productId in case: {payload.get('caseId')}")
    pair_id = _nullable_string(payload, "pairId")
    if test_type == "INV" and (not pair_id or mutation_field is None):
        raise ValueError("INV case requires pairId and mutationField")
    if test_type != "INV" and (pair_id is not None or mutation_field is not None):
        raise ValueError("only INV cases may declare pairId or mutationField")
    per_need = _nullable_int(payload, "perNeed")
    need_of = _need_of(payload.get("needOf"), set(candidate_ids))
    if (need_of is None) != (per_need is None):
        raise ValueError("needOf and perNeed must be provided together")
    if per_need is not None and per_need <= 0:
        raise ValueError("perNeed must be positive")
    final_view = _final_view(payload.get("finalView"), set(candidate_ids))
    overall_oracle = _overall_oracle(payload.get("overallOracle"))
    case_id = _required_string(payload, "caseId")
    _validate_overall_oracle(
        case_id=case_id,
        candidates=candidates,
        final_view=final_view,
        oracle=overall_oracle,
    )
    return GroundingCase(
        case_id=case_id,
        test_type=test_type,
        pair_id=pair_id,
        mutation_field=mutation_field,
        query=_required_string(payload, "query"),
        profile_summary=_nullable_string(payload, "profileSummary"),
        candidates=candidates,
        need_of=need_of,
        per_need=per_need,
        final_view=final_view,
        overall_oracle=overall_oracle,
    )


def _without_declared_mutation(case: GroundingCase) -> GroundingCase:
    if case.mutation_field == "profileSummary":
        return replace(case, case_id="", profile_summary=None)
    if case.mutation_field == "candidateName":
        candidates = tuple(replace(candidate, name="") for candidate in case.candidates)
        return replace(case, case_id="", candidates=candidates)
    return replace(case, case_id="")


def _validate_pairs(cases: tuple[GroundingCase, ...]) -> None:
    pairs: dict[str, list[GroundingCase]] = {}
    for case in cases:
        if case.pair_id is not None:
            pairs.setdefault(case.pair_id, []).append(case)
    for pair_id, members in pairs.items():
        if len(members) != 2:
            raise ValueError(f"INV pair must contain exactly two cases: {pair_id}")
        left, right = members
        if left.mutation_field != right.mutation_field:
            raise ValueError(f"INV pair mutationField mismatch: {pair_id}")
        if _without_declared_mutation(left) != _without_declared_mutation(right):
            raise ValueError(f"INV pair changes undeclared fields: {pair_id}")


def load_fixture(path: Path = DEFAULT_FIXTURE_PATH) -> FixtureSet:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"fixture could not be read: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("fixture root must be an object")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("cases must be a non-empty list")
    cases = tuple(_case(value) for value in raw_cases)
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("duplicate caseId")
    _validate_pairs(cases)
    return FixtureSet(
        fixture_version=_required_string(payload, "fixtureVersion"),
        schema_version=_required_string(payload, "schemaVersion"),
        cases=cases,
    )
