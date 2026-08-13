"""Adapter from prospective holdout rows to the existing rerank evaluation boundary."""

from __future__ import annotations

from collections.abc import Mapping

from app.schemas.spring import SpringProduct
from evals.rerank_holdout_v2.schema import (
    DraftLabels,
    RankingCaseCore,
    SealedLabels,
)
from evals.rerank_scoring.schema import RankingCaseInput
from evals.scoring.hard_filter import HardConstraints, apply_hard_filters


def build_case_input(
    case: RankingCaseCore,
    labels: DraftLabels | SealedLabels,
    catalog: Mapping[str, Mapping[str, object]],
) -> RankingCaseInput:
    """Apply deterministic exclusions and preserve the generated search-order baseline."""

    if case.case_id != labels.case_id:
        raise ValueError("case and labels must share a caseId")
    products: list[Mapping[str, object]] = []
    for product_id in case.candidate_product_ids:
        product = catalog.get(str(product_id))
        if product is None:
            raise ValueError(f"{case.case_id}: catalog product missing: {product_id}")
        products.append(product)
    hard = labels.hard_constraints
    filtered = apply_hard_filters(
        products,
        HardConstraints(
            price_max=hard.price_max,
            price_min=hard.price_min,
            forbidden_categories=frozenset(hard.forbidden_categories),
            forbidden_product_ids=frozenset(hard.forbidden_product_ids),
            must_exclude_product_ids=frozenset(labels.must_exclude_product_ids),
        ),
    )
    candidates = tuple(SpringProduct.model_validate(product) for product in filtered.products)
    search_rank_by_id = {product.product_id: rank for rank, product in enumerate(candidates, 1)}
    if not candidates:
        raise ValueError(f"{case.case_id}: hard filters removed every candidate")
    return RankingCaseInput(
        case_id=case.case_id,
        query=case.query,
        candidates=candidates,
        search_rank_by_id=search_rank_by_id,
        profile_summary=case.profile_summary,
        relevance_grades=dict(labels.relevance_grades),
        hard_constraints=hard.model_dump(by_alias=True),
        must_exclude_product_ids=tuple(labels.must_exclude_product_ids),
        slices=tuple(case.slices),
    )
