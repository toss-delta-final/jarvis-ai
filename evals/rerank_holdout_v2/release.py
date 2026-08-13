"""Dual-human agreement and adjudication gate for sealed labels."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import Field

from evals.rerank_holdout_v2.annotation import ReviewSubmission
from evals.rerank_holdout_v2.schema import (
    CamelModel,
    DraftLabels,
    RankingCaseCore,
    SealedLabels,
)


class Adjudication(CamelModel):
    grade: int = Field(ge=0, le=3)
    adjudicator_id: str = Field(min_length=1)
    adjudicated_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T")
    rationale: str = Field(min_length=1)


@dataclass(frozen=True)
class SealedRelease:
    labels: tuple[SealedLabels, ...]
    agreement_rate: float
    disagreement_count: int
    adjudicator_id: str | None


def _all_candidate_keys(cases: Sequence[RankingCaseCore]) -> set[tuple[str, int]]:
    return {
        (case.case_id, product_id) for case in cases for product_id in case.candidate_product_ids
    }


def _validated_adjudications(
    raw: Mapping[tuple[str, int], Mapping[str, object] | Adjudication],
) -> dict[tuple[str, int], Adjudication]:
    return {
        key: value if isinstance(value, Adjudication) else Adjudication.model_validate(value)
        for key, value in raw.items()
    }


def build_sealed_labels(
    cases: Sequence[RankingCaseCore],
    draft_labels: Sequence[DraftLabels],
    review_a: ReviewSubmission,
    review_b: ReviewSubmission,
    adjudications: Mapping[tuple[str, int], Mapping[str, object] | Adjudication],
    *,
    sealed_at: str = "2026-08-13T00:00:00+09:00",
) -> SealedRelease:
    """Seal final grades only after complete independent review and adjudication."""

    if review_a.reviewer_id == review_b.reviewer_id:
        raise ValueError("sealed release requires an independent reviewer identity for each packet")
    expected = _all_candidate_keys(cases)
    if set(review_a.grades) != expected or set(review_b.grades) != expected:
        raise ValueError("both reviews must exactly cover every candidate")
    drafts_by_case = {labels.case_id: labels for labels in draft_labels}
    case_ids = {case.case_id for case in cases}
    if set(drafts_by_case) != case_ids:
        raise ValueError("draft constraints must exactly cover ranking cases")

    disagreements = {key for key in expected if review_a.grades[key] != review_b.grades[key]}
    resolved = _validated_adjudications(adjudications)
    missing = disagreements - set(resolved)
    extra = set(resolved) - disagreements
    if missing:
        raise ValueError(f"unadjudicated disagreement count: {len(missing)}")
    if extra:
        raise ValueError(f"adjudication supplied for agreeing judgment count: {len(extra)}")
    adjudicator_ids = {row.adjudicator_id for row in resolved.values()}
    if len(adjudicator_ids) > 1:
        raise ValueError("one sealed release must use one adjudicator identity")
    adjudicator_id = next(iter(adjudicator_ids), None)

    final_grades = {
        key: (resolved[key].grade if key in disagreements else review_a.grades[key])
        for key in expected
    }
    labels: list[SealedLabels] = []
    for case in sorted(cases, key=lambda value: value.case_id):
        order = {product_id: rank for rank, product_id in enumerate(case.candidate_product_ids)}
        grades = {
            product_id: final_grades[(case.case_id, product_id)]
            for product_id in case.candidate_product_ids
            if final_grades[(case.case_id, product_id)] > 0
        }
        relevant = sorted(grades, key=lambda product_id: (-grades[product_id], order[product_id]))
        draft = drafts_by_case[case.case_id]
        labels.append(
            SealedLabels(
                case_id=case.case_id,
                label_status="sealed",
                label_source="human-reviewed",
                relevant_product_ids=relevant,
                relevance_grades=grades,
                ideal_order=relevant,
                hard_constraints=draft.hard_constraints,
                must_exclude_product_ids=draft.must_exclude_product_ids,
                label_rationale=(
                    "두 독립 사람 검수자의 후보별 relevance grade를 병합하고 "
                    f"불일치 {len([key for key in disagreements if key[0] == case.case_id])}건을 "
                    "명시적으로 adjudication한 결과다."
                ),
                reviewer_ids=[review_a.reviewer_id, review_b.reviewer_id],
                adjudicator_id=adjudicator_id,
                sealed_at=sealed_at,
            )
        )
    return SealedRelease(
        labels=tuple(labels),
        agreement_rate=(len(expected) - len(disagreements)) / len(expected),
        disagreement_count=len(disagreements),
        adjudicator_id=adjudicator_id,
    )
