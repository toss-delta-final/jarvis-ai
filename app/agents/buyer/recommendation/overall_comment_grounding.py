"""Deterministic grounding for buyer recommendation-level comments.

The reranker may propose whole-list claims, but only this module decides
whether the final exposed product groups support them and which fixed text is
safe to show.  Invalid claims never affect product IDs or rank.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from app.core.config import Settings
from app.schemas.spring import SpringProduct

OverallClaimCode = Literal[
    "TOP_REVIEW_COUNT",
    "ALL_RATING_HIGH",
    "ALL_WITHIN_TOTAL_BUDGET",
    "NO_VERIFIABLE_OVERALL_CLAIM",
]
RecommendationListType = Literal["PICK_ONE", "BUY_ALL"]

NEUTRAL_OVERALL_COMMENT = "요청과의 관련도를 기준으로 추천했어요."


@dataclass(frozen=True)
class FinalRecommendationView:
    list_type: RecommendationListType
    total_budget: int | None
    product_groups: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class OverallGroundingDecision:
    requested_claim_codes: tuple[str, ...]
    supported_claim_codes: tuple[OverallClaimCode, ...]
    rendered_comment: str
    downgraded: bool
    failure_reasons: tuple[str, ...]


@dataclass(frozen=True)
class _ClaimSpec:
    scope: str
    evidence_fields: tuple[str, ...]
    template: str


_CLAIM_SPECS: dict[str, _ClaimSpec] = {
    "TOP_REVIEW_COUNT": _ClaimSpec(
        scope="FINAL_EXPOSED_PRODUCTS",
        evidence_fields=("reviewCount",),
        template="리뷰 수가 가장 많은 상품부터 보여드렸어요.",
    ),
    "ALL_RATING_HIGH": _ClaimSpec(
        scope="FINAL_EXPOSED_PRODUCTS",
        evidence_fields=("ratingLevel",),
        template="평점 정보가 높은 상품들만 골랐어요.",
    ),
    "ALL_WITHIN_TOTAL_BUDGET": _ClaimSpec(
        scope="FINAL_RECOMMENDATION_LISTS",
        evidence_fields=("price", "totalBudget"),
        template="각 추천 조합이 모두 예산 안에 들어와요.",
    ),
    "NO_VERIFIABLE_OVERALL_CLAIM": _ClaimSpec(
        scope="FINAL_EXPOSED_PRODUCTS",
        evidence_fields=(),
        template=NEUTRAL_OVERALL_COMMENT,
    ),
}

_RENDER_PRIORITY: tuple[OverallClaimCode, ...] = (
    "ALL_WITHIN_TOTAL_BUDGET",
    "ALL_RATING_HIGH",
    "TOP_REVIEW_COUNT",
)


def _unique_final_ids(view: FinalRecommendationView) -> tuple[int, ...]:
    return tuple(dict.fromkeys(product_id for group in view.product_groups for product_id in group))


def _claim_code(proposal: Mapping[str, object]) -> str:
    value = proposal.get("claimCode")
    return value if isinstance(value, str) else ""


def _decision(
    *,
    requested: tuple[str, ...],
    supported: Sequence[OverallClaimCode] = (),
    failures: Sequence[str] = (),
) -> OverallGroundingDecision:
    supported_set = set(supported)
    ordered = tuple(code for code in _RENDER_PRIORITY if code in supported_set)
    neutral_supported = "NO_VERIFIABLE_OVERALL_CLAIM" in supported_set
    rendered = " ".join(_CLAIM_SPECS[code].template for code in ordered[:2])
    if not rendered:
        rendered = NEUTRAL_OVERALL_COMMENT
    supported_codes: tuple[OverallClaimCode, ...] = ordered
    if neutral_supported and not ordered:
        supported_codes = ("NO_VERIFIABLE_OVERALL_CLAIM",)
    return OverallGroundingDecision(
        requested_claim_codes=requested,
        supported_claim_codes=supported_codes,
        rendered_comment=rendered,
        downgraded=bool(failures),
        failure_reasons=tuple(dict.fromkeys(failures)),
    )


def _shape(
    proposal: Mapping[str, object],
) -> tuple[str, str, tuple[int, ...], tuple[str, ...]] | None:
    if "__invalidShape" in proposal:
        return None
    required_fields = {"claimCode", "scope", "subjectProductIds", "evidenceFields"}
    if not required_fields.issubset(proposal):
        return None
    code = proposal.get("claimCode")
    scope = proposal.get("scope")
    raw_subjects = proposal.get("subjectProductIds")
    raw_fields = proposal.get("evidenceFields")
    if not isinstance(code, str) or not isinstance(scope, str):
        return None
    if not isinstance(raw_subjects, list) or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in raw_subjects
    ):
        return None
    if len(raw_subjects) != len(set(raw_subjects)):
        return None
    if not isinstance(raw_fields, list) or not all(isinstance(value, str) for value in raw_fields):
        return None
    if len(raw_fields) != len(set(raw_fields)):
        return None
    return code, scope, tuple(raw_subjects), tuple(raw_fields)


def _truth_failure(
    code: OverallClaimCode,
    *,
    final_ids: tuple[int, ...],
    final_view: FinalRecommendationView,
    products_by_id: Mapping[int, SpringProduct],
    settings: Settings,
) -> str | None:
    products = [products_by_id.get(product_id) for product_id in final_ids]
    if any(product is None for product in products):
        return "missing_candidate_fact"
    concrete = cast(list[SpringProduct], products)

    if code == "TOP_REVIEW_COUNT":
        review_counts = [product.review_count for product in concrete]
        if not review_counts or any(value is None for value in review_counts):
            return "missing_candidate_fact"
        counts = cast(list[int], review_counts)
        if counts[0] != max(counts):
            return "candidate_fact_not_supported"
        return None

    if code == "ALL_RATING_HIGH":
        if not concrete:
            return "candidate_fact_not_supported"
        if any(
            product.rating is None
            or product.review_count == 0
            or product.rating < settings.rating_tier_good
            for product in concrete
        ):
            return "candidate_fact_not_supported"
        return None

    if code == "ALL_WITHIN_TOTAL_BUDGET":
        if final_view.list_type != "BUY_ALL" or final_view.total_budget is None:
            return "budget_context_not_supported"
        for group in final_view.product_groups:
            group_products = [products_by_id.get(product_id) for product_id in group]
            if any(product is None or product.price is None for product in group_products):
                return "missing_candidate_fact"
            total = sum(cast(SpringProduct, product).price or 0 for product in group_products)
            if total > final_view.total_budget:
                return "candidate_fact_not_supported"
        return None

    return None


def validate_and_render_overall_comment(
    proposals: Sequence[Mapping[str, object]],
    *,
    final_view: FinalRecommendationView,
    products_by_id: Mapping[int, SpringProduct],
    settings: Settings,
) -> OverallGroundingDecision:
    """Validate structured overall claims against the final recommendation view."""

    requested = tuple(_claim_code(proposal) for proposal in proposals)
    if len(proposals) > 2:
        return _decision(requested=requested, failures=("too_many_claims",))

    nonempty_codes = [code for code in requested if code]
    if len(nonempty_codes) != len(set(nonempty_codes)):
        return _decision(requested=requested, failures=("duplicate_claim_code",))
    if "NO_VERIFIABLE_OVERALL_CLAIM" in nonempty_codes and len(nonempty_codes) > 1:
        return _decision(requested=requested, failures=("neutral_claim_conflict",))

    final_ids = _unique_final_ids(final_view)
    final_id_set = set(final_ids)
    supported: list[OverallClaimCode] = []
    failures: list[str] = []

    for proposal in proposals:
        shaped = _shape(proposal)
        if shaped is None:
            failures.append("invalid_claim_shape")
            continue
        code, scope, subjects, evidence_fields = shaped
        spec = _CLAIM_SPECS.get(code)
        if spec is None:
            failures.append("unknown_claim_code")
            continue
        if scope != spec.scope:
            failures.append("scope_mismatch")
            continue
        if evidence_fields != spec.evidence_fields:
            failures.append("evidence_fields_mismatch")
            continue
        if any(product_id not in final_id_set for product_id in subjects):
            failures.append("subject_outside_final_view")
            continue

        expected_subjects = (
            ()
            if code == "NO_VERIFIABLE_OVERALL_CLAIM"
            else final_ids[:1]
            if code == "TOP_REVIEW_COUNT"
            else final_ids
        )
        if subjects != expected_subjects:
            failures.append("subject_ids_mismatch")
            continue

        typed_code = cast(OverallClaimCode, code)
        failure = _truth_failure(
            typed_code,
            final_ids=final_ids,
            final_view=final_view,
            products_by_id=products_by_id,
            settings=settings,
        )
        if failure is not None:
            failures.append(failure)
            continue
        supported.append(typed_code)

    return _decision(requested=requested, supported=supported, failures=failures)
