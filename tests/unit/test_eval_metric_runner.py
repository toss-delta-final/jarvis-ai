"""추천 metric runner의 집계·제외·결정론 테스트."""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.core.config import Settings
from evals.goldenset.schema import DATASET_VERSION, NEEDS_SLICES, SCHEMA_VERSION, GoldenCase
from evals.metrics.runner import (
    EvaluationFixtures,
    _candidate_depth_stats,
    _ndcg_cutoff_labels,
    assert_pr_gate,
    critical_cases,
    evaluate,
)


def _case(
    case_id: str,
    *,
    relevant: list[int],
    grades: dict[int, int],
    ideal: list[int] | None = None,
    slices: list[str] | None = None,
    hard: dict | None = None,
    must_exclude: list[int] | None = None,
    test_type: str = "MFT",
    behavior_group_id: str | None = None,
    behavior_kind: str | None = None,
) -> GoldenCase:
    resolved_slices = list(slices or ["search", "guest"])
    if not NEEDS_SLICES & set(resolved_slices):
        resolved_slices.append("single_need")
    if "guest" not in resolved_slices and "member" not in resolved_slices:
        resolved_slices.append("member")
    identity_kind = "guest" if "guest" in resolved_slices else "member"
    return GoldenCase.model_validate(
        {
            "caseId": case_id,
            "schemaVersion": SCHEMA_VERSION,
            "datasetVersion": DATASET_VERSION,
            "split": "dev",
            "slices": resolved_slices,
            "query": "테스트 추천",
            "queryType": "simple",
            "identity": {"kind": identity_kind},
            "expectedRoute": "recommend",
            "expectedFilters": {"keyword": "테스트"},
            "searchFixtureId": f"fixture-{case_id}",
            "provenance": "synthetic",
            "labeler": "labeler-01",
            "createdAt": "2026-08-02",
            "labelSource": "model",
            "labeledAt": "2026-08-06",
            "labelRationale": "테스트 라벨 근거.",
            "notes": "runner 손계산 테스트",
            "relevantProductIds": relevant,
            "relevanceGrades": grades,
            "idealOrder": relevant if ideal is None else ideal,
            "hardConstraints": hard or {},
            "mustExcludeProductIds": must_exclude or [],
            "testType": test_type,
            "behaviorGroupId": behavior_group_id,
            "behaviorKind": behavior_kind,
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
        manifest={"datasetVersion": "2.0.0", "datasetHash": "abc"},
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


def test_runner_excludes_non_mft_cases_even_when_labeled() -> None:
    # 실측 회귀(#333 Part 2): member_recall_ge_guest(DIR)는 recall 계산에 라벨이 필요해
    # relevantProductIds가 비어있지 않다 — emptyRelevance만으로는 걸러지지 않으므로
    # testType이 MFT가 아니면 명시적으로 순위 지표에서 제외해야 한다.
    cases = [
        _case("buy-srch-9001", relevant=[1], grades={1: 3}),
        _case(
            "buy-dirm-9002",
            relevant=[2],
            grades={2: 3},
            test_type="DIR",
            behavior_group_id="dir-recall-01",
            behavior_kind="member_recall_ge_guest",
        ),
    ]
    fixtures = _fixtures(cases)

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
    assert report["overall"]["rankingExcludedCaseIds"] == ["buy-dirm-9002"]


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


def test_noop_baseline_exposure_is_system_set_reordered_ascending() -> None:
    # F-4b(#333 리뷰): no-op은 fixture 후보가 아니라 "시스템이 실제로 낸 노출 집합"을
    # productId 오름차순으로 재정렬한 것이다. 시스템이 후보([1,2,3]) 중 2개만, 순서를 바꿔
    # ([3,1]) 냈다면 no-op은 그 두 개를 오름차순으로만 재배열한 [1,3]이어야 한다.
    case = _case("buy-srch-9001", relevant=[3], grades={3: 3})

    report = evaluate(
        cases=[case],
        fixtures=_fixtures([case]),
        adapter=lambda case, fixtures: {
            "rankedProductIds": [3, 1],
            "extractedFilters": case.expected_filters,
        },
        k_list=(1,),
    )

    noop_row = report["noopBaseline"]["cases"][0]
    assert noop_row["rankedProductIds"] == [1, 3]
    assert report["noopBaseline"]["definition"]


def test_noop_baseline_matches_system_exposure_set_even_when_dedup_drops_early_candidates() -> None:
    # F-4b가 대체하는 F-4의 실측 결함: 앱 dedup/필터가 후보 앞쪽(productId 1)을 걷어내고 뒤쪽
    # 상품(4)을 대신 노출하면, fixture 앞부분을 자르는 절단 정의는 no-op에 1을 넣어 시스템과
    # 다른 집합이 된다. F-4b는 시스템이 실제로 낸 집합([2,4])만 오름차순으로 재배열해야 한다.
    case = _case("buy-srch-9001", relevant=[4], grades={4: 3})
    fixtures = _fixtures([case])
    fixtures.search_responses[case.search_fixture_id]["productIds"] = [1, 2, 3, 4]

    report = evaluate(
        cases=[case],
        fixtures=fixtures,
        adapter=lambda case, fixtures: {
            "rankedProductIds": [4, 2],  # 1(dedup 제외)·3 은 노출되지 않음
            "extractedFilters": case.expected_filters,
        },
        k_list=(1,),
    )

    noop_row = report["noopBaseline"]["cases"][0]
    assert noop_row["rankedProductIds"] == [2, 4]
    assert 1 not in noop_row["rankedProductIds"]


def test_noop_baseline_deduplicates_system_output() -> None:
    case = _case("buy-srch-9001", relevant=[3], grades={3: 3})

    report = evaluate(
        cases=[case],
        fixtures=_fixtures([case]),
        adapter=lambda case, fixtures: {
            "rankedProductIds": [3, 1, 1],
            "extractedFilters": case.expected_filters,
        },
        k_list=(1,),
    )

    noop_row = report["noopBaseline"]["cases"][0]
    assert noop_row["rankedProductIds"] == [1, 3]


def test_noop_baseline_matches_system_exactly_when_system_echoes_ascending_order() -> None:
    # 시스템이 fixture 순서(오름차순)를 그대로 내면(기본 scripted adapter) no-op이 정확히
    # 같은 집합·같은 순서가 되어 모든 K의 nDCG가 완전히 같아야 한다.
    case = _case("buy-srch-9001", relevant=[2], grades={2: 3})

    report = evaluate(
        cases=[case],
        fixtures=_fixtures([case]),
        adapter=lambda case, fixtures: {
            "rankedProductIds": [1, 2, 3],
            "extractedFilters": case.expected_filters,
        },
        k_list=(1,),
    )

    assert report["overall"]["ndcgAtK"] == report["noopBaseline"]["overall"]["ndcgAtK"]
    assert (
        report["cases"][0]["rankedProductIds"]
        == report["noopBaseline"]["cases"][0]["rankedProductIds"]
    )


def test_candidate_depth_stats_reports_min_median_max_and_shallow_ratio() -> None:
    stats = _candidate_depth_stats([2, 9, 10, 15, 30])

    assert stats["min"] == 2
    assert stats["median"] == 10
    assert stats["max"] == 30
    assert stats["shallowCount"] == 3  # 2, 9, 10 <= 10
    assert stats["shallowRatio"] == pytest.approx(3 / 5)


def test_candidate_depth_stats_handles_even_count_median() -> None:
    stats = _candidate_depth_stats([10, 20])
    assert stats["median"] == 15


def test_candidate_depth_stats_empty_input() -> None:
    stats = _candidate_depth_stats([])
    assert stats == {
        "min": None,
        "median": None,
        "max": None,
        "shallowCount": 0,
        "shallowRatio": 0.0,
    }


def test_ndcg_cutoff_labels_marks_only_ten_as_primary() -> None:
    assert _ndcg_cutoff_labels((3, 5, 10, 20)) == {
        "3": "exploratory",
        "5": "exploratory",
        "10": "primary",
        "20": "exploratory",
    }


def test_evaluate_reports_candidate_depth_and_ndcg_cutoff_labels() -> None:
    case_a = _case("buy-srch-9001", relevant=[1], grades={1: 3})
    case_b = _case("buy-srch-9002", relevant=[2], grades={2: 3})
    fixtures = _fixtures([case_a, case_b])
    fixtures.search_responses[case_a.search_fixture_id]["productIds"] = [1, 2]
    fixtures.search_responses[case_b.search_fixture_id]["productIds"] = [1, 2, 3]

    report = evaluate(
        cases=[case_a, case_b],
        fixtures=fixtures,
        adapter=lambda case, fixtures: {
            "rankedProductIds": [1],
            "extractedFilters": case.expected_filters,
        },
        k_list=(1,),
    )

    assert report["ndcgCutoffLabels"] == {
        "1": "exploratory",
        "3": "exploratory",
        "5": "exploratory",
        "10": "primary",
    }
    depth = report["overall"]["candidateDepth"]
    assert depth["min"] == 2
    assert depth["max"] == 3
    assert depth["shallowCount"] == 2
