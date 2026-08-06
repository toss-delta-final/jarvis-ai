"""runner.evaluate()에 배선된 filterAxes/filterAxesSpec 테스트(#334).

`tests/unit/test_eval_metric_runner.py`는 수정하지 않는다(패킷 규약) — 이 파일에서 가짜
adapter(일부러 틀린 필터 반환)로 evaluate()를 돌려 filterAxes가 케이스·slice·overall에
나타나고 값이 손계산과 일치하는지 검증한다. 전부-1.0 vacuous 테스트를 피하려고 일부러
불일치·과추출·소추출이 섞이도록 필터를 설계한다.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from app.core.config import Settings
from evals.filter_axes.spec import load_axes_spec
from evals.goldenset.schema import DATASET_VERSION, SCHEMA_VERSION, GoldenCase
from evals.metrics.runner import EvaluationFixtures, evaluate

SPEC = load_axes_spec()


def _case(case_id: str, *, expected_filters: dict, slices: list[str] | None = None) -> GoldenCase:
    # v2(#333)는 니즈 슬라이스(single_need/multi_constraint/budget/repurchase)를 케이스당
    # 정확히 1개 요구한다 — 기본값에 single_need를 넣어 이 배선 테스트가 니즈 분류와 무관하게
    # 최소 조건만 만족하게 한다.
    resolved_slices = slices or ["search", "guest", "single_need"]
    return GoldenCase.model_validate(
        {
            "caseId": case_id,
            "schemaVersion": SCHEMA_VERSION,
            "datasetVersion": DATASET_VERSION,
            "split": "dev",
            "slices": resolved_slices,
            "query": "테스트 추천",
            "queryType": "simple",
            "identity": {"kind": "guest" if "guest" in resolved_slices else "member"},
            "expectedRoute": "recommend",
            "expectedFilters": expected_filters,
            "searchFixtureId": f"fixture-{case_id}",
            "provenance": "synthetic",
            "labeler": "labeler-01",
            "createdAt": "2026-08-02",
            "labelSource": "model",
            "labeledAt": "2026-08-06",
            "labelRationale": "테스트 라벨 근거.",
            "notes": "filterAxes 배선 테스트",
            "relevantProductIds": [1],
            "relevanceGrades": {1: 3},
            "idealOrder": [1],
            "hardConstraints": {},
            "mustExcludeProductIds": [],
        }
    )


def _fixtures(cases: list[GoldenCase]) -> EvaluationFixtures:
    catalog = {"1": {"productId": 1, "price": 10, "categoryName": "A"}}
    searches = {
        case.search_fixture_id: {"productIds": [1], "request": case.expected_filters}
        for case in cases
    }
    return EvaluationFixtures(
        catalog=catalog,
        search_responses=searches,
        purchase_history={},
        manifest={"datasetVersion": "2.1.0", "datasetHash": "abc"},
        non_discriminative_case_ids=frozenset(),
    )


def test_filter_axes_appears_in_case_slice_and_overall_with_nonvacuous_values() -> None:
    # 케이스1: category match, keyword 소추출(missing). 케이스2: category 과추출(spurious).
    cases = [
        _case("buy-srch-9101", expected_filters={"category": "가전", "keyword": "이어폰"}),
        _case("buy-srch-9102", expected_filters={}),
    ]
    outputs = {
        cases[0].case_id: {
            "rankedProductIds": [1],
            "extractedFilters": {"category": "가전"},
        },
        cases[1].case_id: {
            "rankedProductIds": [1],
            "extractedFilters": {"category": "가전"},
        },
    }

    report = evaluate(
        cases=cases,
        fixtures=_fixtures(cases),
        adapter=lambda case, fixtures: deepcopy(outputs[case.case_id]),
        k_list=(1,),
    )

    # 케이스 레벨 — bothEmpty가 아닌 실제 판정이 섞여 vacuous 하지 않다.
    case_axes = {row["caseId"]: row["filterAxes"] for row in report["cases"]}
    assert case_axes["buy-srch-9101"]["category"] == "match"
    assert case_axes["buy-srch-9101"]["keyword"] == "missing"
    assert case_axes["buy-srch-9102"]["category"] == "spurious"

    # overall micro 집계 — 손계산: category match=1, spurious=1 → precision=1/2, recall=1/1.
    overall_category = report["overall"]["filterAxes"]["category"]
    assert overall_category["counts"]["match"] == 1
    assert overall_category["counts"]["spurious"] == 1
    assert overall_category["valueStrict"]["precision"] == pytest.approx(0.5)
    assert overall_category["valueStrict"]["recall"] == pytest.approx(1.0)
    assert 0 < overall_category["valueStrict"]["precision"] < 1

    overall_keyword = report["overall"]["filterAxes"]["keyword"]
    assert overall_keyword["counts"]["missing"] == 1

    # slice 레벨에도 반영된다.
    assert report["slices"]["guest"]["filterAxes"]["category"]["counts"]["match"] == 1

    # filterAxesSpec 동봉.
    assert report["filterAxesSpec"]["version"] == SPEC["version"]
    assert report["filterAxesSpec"]["emptyAxisRule"] == SPEC["emptyAxisRule"]
    assert len(report["filterAxesSpec"]["sha256"]) == 64

    # 골든셋 v2(#333) 머지로 evaluate() 반환에 noopBaseline·behaviorChecks 가 추가됐다 —
    # filterAxesSpec 이 그와 공존하며 밀려나지 않았는지 확인(R4-1).
    assert "noopBaseline" in report
    assert "overall" in report["noopBaseline"]
    assert "behaviorChecks" in report


def test_existing_filter_accuracy_and_keys_are_unchanged_by_filter_axes_wiring() -> None:
    case = _case("buy-srch-9201", expected_filters={"category": "가전"})
    report = evaluate(
        cases=[case],
        fixtures=_fixtures([case]),
        adapter=lambda case, fixtures: {
            "rankedProductIds": [1],
            "extractedFilters": {"category": "잘못된값"},
        },
        k_list=(1,),
        config=Settings(_env_file=None, eval_buyer_k_list=(1,)),
    )

    # filterAccuracy(합집합 분모 단일값)는 예전 그대로 값이 나와야 한다(불일치 → 0.0).
    assert report["overall"]["filterAccuracy"] == pytest.approx(0.0)
    assert report["cases"][0]["filterAccuracy"] == pytest.approx(0.0)
    # filterAxes가 추가 키로만 존재하고 기존 키를 밀어내지 않았는지.
    assert "filterAccuracy" in report["overall"]
    assert "filterAxes" in report["overall"]
