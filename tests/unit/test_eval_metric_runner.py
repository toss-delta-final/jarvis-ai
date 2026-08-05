"""추천 metric runner의 집계·제외·결정론 테스트."""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.core.config import Settings
from evals.goldenset.schema import GoldenCase
from evals.metrics.runner import EvaluationFixtures, assert_pr_gate, critical_cases, evaluate


def _case(
    case_id: str,
    *,
    relevant: list[int],
    grades: dict[int, int],
    ideal: list[int] | None = None,
    slices: list[str] | None = None,
    hard: dict | None = None,
    must_exclude: list[int] | None = None,
) -> GoldenCase:
    resolved_slices = slices or ["search", "guest"]
    return GoldenCase.model_validate(
        {
            "caseId": case_id,
            "schemaVersion": "1.0.0",
            "datasetVersion": "1.0.0",
            "split": "dev",
            "slices": resolved_slices,
            "query": "테스트 추천",
            "queryType": "simple",
            "identity": {"kind": "guest" if "guest" in resolved_slices else "member"},
            "expectedRoute": "recommend",
            "expectedFilters": {"keyword": "테스트"},
            "searchFixtureId": f"fixture-{case_id}",
            "provenance": "synthetic",
            "labeler": "labeler-01",
            "createdAt": "2026-08-02",
            "notes": "runner 손계산 테스트",
            "relevantProductIds": relevant,
            "relevanceGrades": grades,
            "idealOrder": relevant if ideal is None else ideal,
            "hardConstraints": hard or {},
            "mustExcludeProductIds": must_exclude or [],
        }
    )


def _fixtures(cases: list[GoldenCase]) -> EvaluationFixtures:
    catalog = {
        "1": {"productId": 1, "price": 10, "categoryName": "A"},
        "2": {"productId": 2, "price": 20, "categoryName": "B"},
        "3": {"productId": 3, "price": 30, "categoryName": "B"},
    }
    searches = {
        case.search_fixture_id: {"productIds": [1, 2, 3], "request": case.expected_filters}
        for case in cases
    }
    return EvaluationFixtures(
        catalog=catalog,
        search_responses=searches,
        purchase_history={},
        manifest={"datasetVersion": "1.0.0", "datasetHash": "abc"},
        non_discriminative_case_ids=frozenset(),
    )


def test_runner_reports_macro_micro_slices_and_quality_violations() -> None:
    cases = [
        _case("buy-srch-9001", relevant=[1, 2], grades={1: 3, 2: 1}, slices=["search", "guest"]),
        _case(
            "buy-fail-9002",
            relevant=[1],
            grades={1: 3},
            slices=["search", "failure"],
            hard={"priceMax": 25},
        ),
    ]
    outputs = {
        cases[0].case_id: {
            "rankedProductIds": [1, 1, 999],
            "extractedFilters": {"keyword": "테스트"},
        },
        cases[1].case_id: {
            "rankedProductIds": [3],
            "extractedFilters": {"keyword": "테스트", "brand": ["과잉"]},
        },
    }

    report = evaluate(
        cases=cases,
        fixtures=_fixtures(cases),
        adapter=lambda case, fixtures: deepcopy(outputs[case.case_id]),
        k_list=(1, 2),
    )

    assert report["overall"]["caseCount"] == 2
    assert report["overall"]["rankingCaseCount"] == 2
    assert report["overall"]["precisionAtK"]["1"] == pytest.approx(0.5)
    assert report["overall"]["precisionAtK"]["2"] == pytest.approx(0.25)
    assert report["overall"]["microRecallAtK"]["2"] == pytest.approx(1 / 3)
    assert report["overall"]["filterAccuracy"] == pytest.approx(0.75)
    assert report["overall"]["hardConstraintViolationRate"] == pytest.approx(0.5)
    assert report["overall"]["duplicateCount"] == 1
    assert report["overall"]["unknownProductIds"] == [999]
    assert report["slices"]["guest"]["caseCount"] == 1
    assert report["slices"]["failure"]["hardConstraintViolationRate"] == 1.0
    assert report["violations"][0]["constraint"] == "priceMax"


def test_runner_excludes_non_discriminative_and_empty_relevance_explicitly() -> None:
    cases = [
        _case("buy-srch-9001", relevant=[1], grades={1: 3}),
        _case("buy-srch-9002", relevant=[2], grades={2: 3}),
        _case("buy-fail-9003", relevant=[], grades={}, slices=["failure"]),
    ]
    fixtures = _fixtures(cases)
    fixtures = EvaluationFixtures(
        **{
            **fixtures.__dict__,
            "non_discriminative_case_ids": frozenset({"buy-srch-9002"}),
        }
    )

    report = evaluate(
        cases=cases,
        fixtures=fixtures,
        adapter=lambda case, fixtures: {
            "rankedProductIds": [1, 2],
            "extractedFilters": case.expected_filters,
        },
        k_list=(1,),
    )

    assert report["overall"]["rankingCaseCount"] == 1
    assert report["overall"]["rankingExcludedCount"] == 2
    assert report["overall"]["rankingExcludedCaseIds"] == [
        "buy-fail-9003",
        "buy-srch-9002",
    ]
    assert report["overall"]["caseCount"] == 3
    assert report["overall"]["filterAccuracy"] == 1.0


def test_micro_recall_counts_each_relevant_product_once() -> None:
    case = _case("buy-srch-9001", relevant=[1, 1], grades={1: 3}, ideal=[1])

    report = evaluate(
        cases=[case],
        fixtures=_fixtures([case]),
        adapter=lambda case, fixtures: {
            "rankedProductIds": [1],
            "extractedFilters": case.expected_filters,
        },
        k_list=(1,),
    )

    assert report["overall"]["recallAtK"]["1"] == 1.0
    assert report["overall"]["microRecallAtK"]["1"] == 1.0


def test_holdout_execution_is_sealed_for_issue_144() -> None:
    with pytest.raises(NotImplementedError, match="#144"):
        evaluate(split="holdout", adapter=lambda case, fixtures: {})


def test_configured_k_list_changes_reported_metrics() -> None:
    case = _case("buy-srch-9001", relevant=[2], grades={2: 3})
    fixtures = _fixtures([case])

    def adapter(case, fixtures):
        return {
            "rankedProductIds": [1, 2],
            "extractedFilters": case.expected_filters,
        }

    report = evaluate(
        cases=[case],
        fixtures=fixtures,
        adapter=adapter,
        config=Settings(_env_file=None, eval_buyer_k_list=(1, 2)),
    )

    assert report["overall"]["recallAtK"] == {"1": 0.0, "2": 1.0}


@pytest.mark.parametrize("k_list", [(), (0,), (-1, 5)])
def test_settings_reject_invalid_eval_buyer_k_list(k_list: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="평가 K 목록"):
        Settings(_env_file=None, eval_buyer_k_list=k_list)


def test_critical_subset_is_constraint_or_failure_union() -> None:
    cases = [
        _case("buy-srch-9001", relevant=[1], grades={1: 3}),
        _case(
            "buy-srch-9002",
            relevant=[2],
            grades={2: 3},
            hard={"forbiddenProductIds": [3]},
        ),
        _case("buy-fail-9003", relevant=[], grades={}, slices=["failure"]),
    ]
    assert [case.case_id for case in critical_cases(cases)] == [
        "buy-srch-9002",
        "buy-fail-9003",
    ]


def test_pr_gate_fails_when_adapter_injects_price_violation() -> None:
    case = _case(
        "buy-fail-9001",
        relevant=[1],
        grades={1: 3},
        slices=["failure"],
        hard={"priceMax": 15},
    )
    report = evaluate(
        cases=[case],
        fixtures=_fixtures([case]),
        adapter=lambda case, fixtures: {
            "rankedProductIds": [2],
            "extractedFilters": case.expected_filters,
        },
        k_list=(1,),
    )

    with pytest.raises(AssertionError, match="priceMax"):
        assert_pr_gate(report)
