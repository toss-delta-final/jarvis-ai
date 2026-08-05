from __future__ import annotations

import pytest

from evals.personalization.stats import paired_metric_deltas


def _report(a: float, b: float, *, excluded: bool = False) -> dict:
    return {
        "datasetHash": "same",
        "kList": [10],
        "cases": [
            {
                "caseId": "a",
                "slices": ["personalization"],
                "rankingExcluded": excluded,
                "rankingExclusionReason": "nonDiscriminativeRanking" if excluded else None,
                "metrics": {"ndcgAtK": {"10": a}},
                "diversity": 0.25,
            },
            {
                "caseId": "b",
                "slices": ["personalization", "cold_start"],
                "rankingExcluded": False,
                "rankingExclusionReason": None,
                "metrics": {"ndcgAtK": {"10": b}},
                "diversity": 0.75,
            },
        ],
    }


def test_paired_stats_match_hand_calculation_and_report_denominator() -> None:
    result = paired_metric_deltas(
        _report(0.4, 0.9),
        _report(0.2, 0.5),
        k_list=(10,),
        resamples=2000,
        confidence=0.95,
        seed=20260803,
    )
    overall = result["overall"]
    assert overall["ndcgAtK"]["10"]["deltas"] == pytest.approx([0.2, 0.4])
    assert overall["ndcgAtK"]["10"]["meanDelta"] == pytest.approx(0.3)
    assert overall["diversity"]["meanDelta"] == pytest.approx(0.0)
    assert overall["ndcgAtK"]["10"]["denominator"]["includedCount"] == 2
    assert overall["diversity"]["denominator"]["includedCount"] == 2


def test_ranking_excluded_only_leaves_ndcg_denominator() -> None:
    result = paired_metric_deltas(
        _report(0.4, 0.9, excluded=True),
        _report(0.2, 0.5),
        k_list=(10,),
        resamples=20,
        confidence=0.95,
        seed=1,
    )
    ndcg = result["overall"]["ndcgAtK"]["10"]["denominator"]
    diversity = result["overall"]["diversity"]["denominator"]
    assert ndcg["includedCaseIds"] == ["b"]
    assert ndcg["excluded"] == [{"caseId": "a", "reason": "nonDiscriminativeRanking"}]
    assert diversity["includedCaseIds"] == ["a", "b"]
    assert diversity["excluded"] == []


@pytest.mark.parametrize("mutation", ["missing_case", "slice_drift", "dataset", "k_list"])
def test_paired_stats_fail_fast_on_unpaired_reports(mutation: str) -> None:
    current, baseline = _report(0.4, 0.9), _report(0.2, 0.5)
    if mutation == "missing_case":
        current["cases"].pop()
    elif mutation == "slice_drift":
        current["cases"][0]["slices"] = ["cold_start"]
    elif mutation == "dataset":
        current["datasetHash"] = "different"
    else:
        current["kList"] = [5]
    with pytest.raises(ValueError, match="paired"):
        paired_metric_deltas(current, baseline, k_list=(10,), resamples=20, confidence=0.95, seed=1)
