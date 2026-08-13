"""Leakage, quota, referential, and draft-label validation."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field

from evals.rerank_holdout_v2.generator import GenerationBundle, QUOTAS
from evals.rerank_holdout_v2.schema import CamelModel, DraftLabels, RankingCaseCore

_TOKEN_RE = re.compile(r"[0-9a-z가-힣]+", re.IGNORECASE)
_STOP_WORDS = frozenset(
    {
        "상품",
        "추천",
        "추천해줘",
        "찾아줘",
        "비교",
        "비교해서",
        "카테고리",
        "브랜드",
        "하나",
        "이하",
        "이번에는",
        "전에",
        "다시",
        "산",
        "비슷한",
    }
)
_EXPECTED_STRATA = {stratum: sum(counts.values()) for stratum, counts in QUOTAS.items()}
_EXPECTED_IDENTITIES = {
    identity: sum(counts[identity] for counts in QUOTAS.values())
    for identity in ("guest", "member")
}
_EXPECTED_SAFETY = {
    "catalog_prompt_injection": 8,
    "hard_constraint_integrity": 8,
    "candidate_set_integrity": 8,
}


class AuditReport(CamelModel):
    passed: bool
    ranking_count: int
    safety_count: int
    stratum_counts: dict[str, int]
    identity_counts: dict[str, int]
    safety_scenario_counts: dict[str, int]
    candidate_count_min: int
    candidate_count_max: int
    unique_family_count: int
    unique_query_count: int
    positive_count_min: int
    positive_count_max: int
    legacy_query_count: int
    max_legacy_query_similarity: float
    max_legacy_similarity_case_id: str | None = None
    max_legacy_similarity_legacy_case_id: str | None = None
    catalog_sha256: str
    seed: int
    checks: list[str] = Field(min_length=1)


def normalized_query(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.casefold()))


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token for token in _TOKEN_RE.findall(value.casefold()) if token not in _STOP_WORDS
    )


def token_jaccard(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _catalog_product(
    catalog: Mapping[str, Mapping[str, object]], product_id: int
) -> Mapping[str, object]:
    product = catalog.get(str(product_id))
    if product is None:
        raise ValueError(f"catalog product missing: {product_id}")
    return product


def violates_constraints(product: Mapping[str, object], labels: DraftLabels) -> str | None:
    product_id = int(product["productId"])
    hard = labels.hard_constraints
    price = product.get("price")
    if hard.price_max is not None or hard.price_min is not None:
        if price is None:
            return "priceUnknown"
        try:
            price_value = float(price)
        except (TypeError, ValueError):
            return "priceUnknown"
        if hard.price_max is not None and price_value > hard.price_max:
            return "priceMax"
        if hard.price_min is not None and price_value < hard.price_min:
            return "priceMin"
    if product.get("categoryName") in set(hard.forbidden_categories):
        return "forbiddenCategory"
    if product_id in set(hard.forbidden_product_ids):
        return "forbiddenProductId"
    if product_id in set(labels.must_exclude_product_ids):
        return "mustExclude"
    return None


def _legacy_cores(legacy_root: Path) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for name in ("buyer_dev.jsonl", "buyer_holdout.jsonl"):
        path = legacy_root / "cases" / name
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{line_number}: legacy core must be an object")
                rows.append(value)
    return tuple(rows)


def _exact_counts(bundle: GenerationBundle) -> tuple[Counter[str], Counter[str], Counter[str]]:
    strata = Counter(case.stratum for case in bundle.ranking_cases)
    identities = Counter(case.identity.kind for case in bundle.ranking_cases)
    safety = Counter(case.scenario for case in bundle.safety_cases)
    if len(bundle.ranking_cases) != 200:
        raise ValueError("dataset must contain exactly 200 ranking cases")
    if len(bundle.draft_labels) != 200:
        raise ValueError("dataset must contain exactly 200 draft label rows")
    if len(bundle.safety_cases) != 24:
        raise ValueError("dataset must contain exactly 24 safety cases")
    if dict(strata) != _EXPECTED_STRATA:
        raise ValueError(f"stratum quotas do not match registered design: {dict(strata)}")
    if dict(identities) != _EXPECTED_IDENTITIES:
        raise ValueError(f"identity quotas do not match registered design: {dict(identities)}")
    if dict(safety) != _EXPECTED_SAFETY:
        raise ValueError(f"safety quotas do not match registered design: {dict(safety)}")
    return strata, identities, safety


def _unique_core_fields(cases: tuple[RankingCaseCore, ...]) -> tuple[set[str], set[str]]:
    case_ids = [case.case_id for case in cases]
    family_ids = [case.family_id for case in cases]
    queries = [normalized_query(case.query) for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("ranking case IDs must be unique")
    if len(family_ids) != len(set(family_ids)):
        raise ValueError("ranking family IDs must be unique")
    if len(queries) != len(set(queries)):
        raise ValueError("normalized ranking queries must be unique")
    return set(family_ids), set(queries)


def _labels_by_case(bundle: GenerationBundle) -> dict[str, DraftLabels]:
    label_ids = [labels.case_id for labels in bundle.draft_labels]
    if len(label_ids) != len(set(label_ids)):
        raise ValueError("draft label case IDs must be unique")
    positive_sets = [frozenset(labels.relevant_product_ids) for labels in bundle.draft_labels]
    if len(positive_sets) != len(set(positive_sets)):
        raise ValueError("duplicate positive label set across ranking cases")
    labels_by_case = {labels.case_id: labels for labels in bundle.draft_labels}
    case_ids = {case.case_id for case in bundle.ranking_cases}
    if set(labels_by_case) != case_ids:
        raise ValueError("draft labels must exactly cover ranking cases")
    return labels_by_case


def _validate_case(
    case: RankingCaseCore,
    labels: DraftLabels,
    catalog: Mapping[str, Mapping[str, object]],
    *,
    catalog_sha256: str,
) -> None:
    if case.catalog_sha256 != catalog_sha256:
        raise ValueError(f"{case.case_id}: catalog hash mismatch")
    candidate_ids = case.candidate_product_ids
    if len(candidate_ids) != 30 or len(set(candidate_ids)) != 30:
        raise ValueError(f"{case.case_id}: candidate depth must be 30 distinct products")
    if set(case.candidate_provenance) != set(candidate_ids):
        raise ValueError(f"{case.case_id}: candidate provenance does not cover candidates")
    for product_id in candidate_ids:
        _catalog_product(catalog, product_id)
    positives = set(labels.relevant_product_ids)
    if not positives <= set(candidate_ids):
        raise ValueError(f"{case.case_id}: positive labels must reference candidates")
    for product_id in positives:
        product = _catalog_product(catalog, product_id)
        violation = violates_constraints(product, labels)
        if violation is not None:
            raise ValueError(
                f"{case.case_id}: positive candidate violates hard constraint: "
                f"{product_id} ({violation})"
            )


def _validate_legacy_independence(
    cases: tuple[RankingCaseCore, ...], legacy_rows: tuple[dict[str, object], ...]
) -> tuple[float, str | None, str | None]:
    legacy_ids = {str(row.get("caseId", "")) for row in legacy_rows}
    legacy_families = {str(row.get("familyId", "")) for row in legacy_rows if row.get("familyId")}
    legacy_queries = {
        normalized_query(str(row.get("query", ""))): str(row.get("caseId", ""))
        for row in legacy_rows
    }
    maximum = 0.0
    maximum_case: str | None = None
    maximum_legacy: str | None = None
    for case in cases:
        if case.case_id in legacy_ids:
            raise ValueError(f"legacy case ID collision: {case.case_id}")
        if case.family_id in legacy_families:
            raise ValueError(f"legacy family collision: {case.family_id}")
        normalized = normalized_query(case.query)
        if normalized in legacy_queries:
            raise ValueError(
                f"legacy query collision: {case.case_id} and {legacy_queries[normalized]}"
            )
        for row in legacy_rows:
            similarity = token_jaccard(case.query, str(row.get("query", "")))
            if similarity > maximum:
                maximum = similarity
                maximum_case = case.case_id
                maximum_legacy = str(row.get("caseId", ""))
            if similarity >= 0.85:
                raise ValueError(
                    f"legacy query similarity >= 0.85: {case.case_id} and "
                    f"{row.get('caseId')} ({similarity:.4f})"
                )
    return maximum, maximum_case, maximum_legacy


def validate_bundle(
    bundle: GenerationBundle,
    catalog: Mapping[str, Mapping[str, object]],
    legacy_root: Path,
    *,
    catalog_sha256: str,
    seed: int,
) -> AuditReport:
    """Validate the complete registered draft without opening legacy holdout labels."""

    strata, identities, safety = _exact_counts(bundle)
    families, queries = _unique_core_fields(bundle.ranking_cases)
    labels_by_case = _labels_by_case(bundle)
    for case in bundle.ranking_cases:
        _validate_case(
            case,
            labels_by_case[case.case_id],
            catalog,
            catalog_sha256=catalog_sha256,
        )
    for case in bundle.safety_cases:
        if case.catalog_sha256 != catalog_sha256:
            raise ValueError(f"{case.case_id}: safety catalog hash mismatch")
        for product_id in case.candidate_product_ids:
            _catalog_product(catalog, product_id)

    legacy_rows = _legacy_cores(legacy_root)
    maximum, maximum_case, maximum_legacy = _validate_legacy_independence(
        bundle.ranking_cases, legacy_rows
    )
    candidate_counts = [len(case.candidate_product_ids) for case in bundle.ranking_cases]
    positive_counts = [len(labels.relevant_product_ids) for labels in bundle.draft_labels]
    return AuditReport(
        passed=True,
        ranking_count=len(bundle.ranking_cases),
        safety_count=len(bundle.safety_cases),
        stratum_counts=dict(strata),
        identity_counts=dict(identities),
        safety_scenario_counts=dict(safety),
        candidate_count_min=min(candidate_counts),
        candidate_count_max=max(candidate_counts),
        unique_family_count=len(families),
        unique_query_count=len(queries),
        positive_count_min=min(positive_counts),
        positive_count_max=max(positive_counts),
        legacy_query_count=len(legacy_rows),
        max_legacy_query_similarity=round(maximum, 6),
        max_legacy_similarity_case_id=maximum_case,
        max_legacy_similarity_legacy_case_id=maximum_legacy,
        catalog_sha256=catalog_sha256,
        seed=seed,
        checks=[
            "registered_counts_and_quotas",
            "unique_case_family_and_query",
            "candidate_catalog_and_provenance_integrity",
            "draft_label_coverage_and_uniqueness",
            "positive_hard_constraint_integrity",
            "legacy_core_query_independence",
            "legacy_holdout_labels_unopened",
            "safety_set_separate",
        ],
    )
