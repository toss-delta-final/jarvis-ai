"""Deterministic rescoring for archived arm-A rerank samples."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from app.agents.buyer.recommendation.overall_comment_grounding import (
    FinalRecommendationView,
    validate_and_render_overall_comment,
)
from app.core.config import Settings
from app.schemas.spring import SpringProduct
from evals.rerank_grounding.metrics import detect_overall_claims
from evals.rerank_grounding.schema import FixtureSet, GroundingCase

_HISTORICAL_SETTINGS = Settings.model_construct(rating_tier_good=4.0)


@dataclass(frozen=True)
class HistoricalARescore:
    run_name: str
    sample_count: int
    detected_by_family: dict[str, int]
    scored_claim_count: int
    violation_count: int
    unscored_budget_claim_count: int

    @property
    def violation_rate(self) -> float | None:
        return self.violation_count / self.scored_claim_count if self.scored_claim_count else None

    def as_dict(self) -> dict[str, object]:
        return {
            "run": self.run_name,
            "aSamples": self.sample_count,
            "detectedByFamily": dict(self.detected_by_family),
            "scoredClaims": self.scored_claim_count,
            "violations": self.violation_count,
            "rate": self.violation_rate,
            "unscoredBudgetClaims": self.unscored_budget_claim_count,
        }


def _products(case: GroundingCase) -> dict[int, SpringProduct]:
    return {
        candidate.product_id: SpringProduct(
            product_id=candidate.product_id,
            name=candidate.name,
            price=candidate.price,
            rating=candidate.rating,
            review_count=candidate.review_count,
            category=candidate.category_name,
            brand=candidate.brand,
        )
        for candidate in case.candidates
    }


def _proposal(code: str, ranked_ids: list[int]) -> dict[str, object]:
    if code == "TOP_REVIEW_COUNT":
        return {
            "claimCode": code,
            "scope": "FINAL_EXPOSED_PRODUCTS",
            "subjectProductIds": ranked_ids[:1],
            "evidenceFields": ["reviewCount"],
        }
    if code == "ALL_RATING_HIGH":
        return {
            "claimCode": code,
            "scope": "FINAL_EXPOSED_PRODUCTS",
            "subjectProductIds": ranked_ids,
            "evidenceFields": ["ratingLevel"],
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
        "subjectProductIds": ranked_ids[:1],
        "evidenceFields": [],
    }


def rescore_historical_current_run(
    run_dir: Path,
    *,
    fixture: FixtureSet,
) -> HistoricalARescore:
    """Rescore registered claims in one archived `samples.csv` without LLM calls.

    Archived #632 rows have no budget/list oracle, so detected budget claims are
    counted separately and excluded from the correctness denominator.
    """

    cases = {case.case_id: case for case in fixture.cases}
    detected: Counter[str] = Counter()
    sample_count = 0
    scored = 0
    violations = 0
    unscored_budget = 0
    try:
        handle = (run_dir / "samples.csv").open(encoding="utf-8", newline="")
    except OSError as exc:
        raise ValueError(f"historical samples could not be read: {run_dir}") from exc
    with handle:
        for row_number, row in enumerate(csv.DictReader(handle), start=2):
            if row.get("arm") != "current":
                continue
            case_id = row.get("caseId") or ""
            case = cases.get(case_id)
            if case is None:
                raise ValueError(f"unknown historical caseId at row {row_number}: {case_id}")
            try:
                ranked_ids = json.loads(row.get("rankedProductIds") or "")
                raw_response = json.loads(row.get("rawResponse") or "")
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid historical JSON at row {row_number}") from exc
            if not isinstance(ranked_ids, list) or not all(
                isinstance(product_id, int) and not isinstance(product_id, bool)
                for product_id in ranked_ids
            ):
                raise ValueError(f"invalid historical rankedProductIds at row {row_number}")
            if not isinstance(raw_response, dict):
                raise ValueError(f"invalid historical rawResponse at row {row_number}")

            sample_count += 1
            comment = raw_response.get("overallComment")
            claims = detect_overall_claims(comment if isinstance(comment, str) else "")
            detected.update(claims)
            final_view = FinalRecommendationView(
                list_type="PICK_ONE",
                total_budget=None,
                product_groups=(tuple(ranked_ids),),
            )
            products_by_id = _products(case)
            for code in claims:
                if code == "ALL_WITHIN_TOTAL_BUDGET":
                    unscored_budget += 1
                    continue
                scored += 1
                decision = validate_and_render_overall_comment(
                    [_proposal(code, ranked_ids)],
                    final_view=final_view,
                    products_by_id=products_by_id,
                    settings=_HISTORICAL_SETTINGS,
                )
                violations += int(code not in decision.supported_claim_codes)

    return HistoricalARescore(
        run_name=run_dir.name,
        sample_count=sample_count,
        detected_by_family=dict(sorted(detected.items())),
        scored_claim_count=scored,
        violation_count=violations,
        unscored_budget_claim_count=unscored_budget,
    )


def rescore_historical_current_runs(
    run_dirs: list[Path],
    *,
    fixture: FixtureSet,
) -> dict[str, object]:
    summaries = [rescore_historical_current_run(path, fixture=fixture) for path in run_dirs]
    detected: Counter[str] = Counter()
    for summary in summaries:
        detected.update(summary.detected_by_family)
    scored = sum(summary.scored_claim_count for summary in summaries)
    violations = sum(summary.violation_count for summary in summaries)
    return {
        "detector": "bounded-overall-korean-v1",
        "runs": [summary.as_dict() for summary in summaries],
        "total": {
            "aSamples": sum(summary.sample_count for summary in summaries),
            "detectedByFamily": dict(sorted(detected.items())),
            "scoredClaims": scored,
            "violations": violations,
            "rate": violations / scored if scored else None,
            "unscoredBudgetClaims": sum(
                summary.unscored_budget_claim_count for summary in summaries
            ),
        },
        "limits": [
            "Only registered Korean lexical families are detected.",
            "Archived rows use rankedProductIds as a one-group PICK_ONE final-view proxy.",
            "Budget claims are unscored because archived #632 rows have no budget/list oracle.",
        ],
    }
