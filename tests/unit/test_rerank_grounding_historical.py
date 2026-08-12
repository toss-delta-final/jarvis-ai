from __future__ import annotations

import csv
import json
from pathlib import Path

from evals.rerank_grounding.historical import rescore_historical_current_run
from evals.rerank_grounding.schema import DEFAULT_FIXTURE_PATH, load_fixture


def test_historical_a_rescore_is_bounded_and_marks_budget_unscored(tmp_path: Path) -> None:
    run_dir = tmp_path / "historical"
    run_dir.mkdir()
    comments = [
        "평점이 높은 상품들만 골랐어요",
        "가장 인기 있는 상품이에요",
        "모두 예산 안에 들어와요",
        "조건에 맞춰 골랐어요",
    ]
    with (run_dir / "samples.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("caseId", "arm", "rankedProductIds", "rawResponse"),
        )
        writer.writeheader()
        for comment in comments:
            writer.writerow(
                {
                    "caseId": "overall_all_rating_high",
                    "arm": "current",
                    "rankedProductIds": json.dumps([1031, 1032]),
                    "rawResponse": json.dumps({"overallComment": comment}, ensure_ascii=False),
                }
            )

    result = rescore_historical_current_run(
        run_dir,
        fixture=load_fixture(DEFAULT_FIXTURE_PATH),
    )

    assert result.sample_count == 4
    assert result.detected_by_family == {
        "ALL_RATING_HIGH": 1,
        "POPULARITY_TOP": 1,
        "ALL_WITHIN_TOTAL_BUDGET": 1,
    }
    assert result.scored_claim_count == 2
    assert result.violation_count == 1
    assert result.violation_rate == 0.5
    assert result.unscored_budget_claim_count == 1
