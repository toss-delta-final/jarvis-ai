"""라벨 없이 도는 INV/DIR 행동 검사(evals.metrics.behavior) 단위 테스트."""

from __future__ import annotations

from evals.goldenset.schema import DATASET_VERSION, SCHEMA_VERSION, GoldenCase
from evals.metrics.behavior import evaluate_behavior_checks


def _case(
    case_id: str,
    *,
    test_type: str,
    behavior_group_id: str,
    behavior_kind: str | None,
    identity_kind: str = "guest",
    search_fixture_id: str = "fixture-1",
    expected_filters: dict | None = None,
    relevant: list[int] | None = None,
) -> GoldenCase:
    slices = ["search"]
    slices.append(identity_kind if identity_kind in ("guest", "member") else "guest")
    slices.append("single_need")
    identity = {"kind": identity_kind}
    if identity_kind == "member":
        identity["personaId"] = "persona-1"
    raw = {
        "caseId": case_id,
        "schemaVersion": SCHEMA_VERSION,
        "datasetVersion": DATASET_VERSION,
        "split": "dev",
        "slices": slices,
        "query": "테스트 발화",
        "queryType": "simple",
        "identity": identity,
        "expectedRoute": "recommend",
        "expectedFilters": expected_filters or {},
        "searchFixtureId": search_fixture_id,
        "relevantProductIds": relevant or [],
        "relevanceGrades": {str(pid): 3 for pid in (relevant or [])},
        "idealOrder": relevant or [],
        "hardConstraints": {},
        "mustExcludeProductIds": [],
        "provenance": "synthetic",
        "labeler": "labeler-01",
        "createdAt": "2026-08-02",
        "labelSource": "model",
        "labeledAt": "2026-08-06",
        "labelRationale": "테스트 라벨 근거.",
        "notes": "behavior 검사용 합성 케이스",
        "testType": test_type,
        "behaviorGroupId": behavior_group_id,
        "behaviorKind": behavior_kind,
    }
    return GoldenCase.model_validate(raw)


def _row(case_id: str, ranked: list[int], relevant: list[int] | None = None) -> dict:
    return {"caseId": case_id, "rankedProductIds": ranked, "relevantProductIds": relevant or []}


def test_color_synonym_passes_when_same_fixture_and_exposure_match() -> None:
    cases = [
        _case(
            "buy-inv-0001", test_type="INV", behavior_group_id="g1", behavior_kind="color_synonym"
        ),
        _case(
            "buy-inv-0002", test_type="INV", behavior_group_id="g1", behavior_kind="color_synonym"
        ),
    ]
    rows = [_row("buy-inv-0001", [1, 2, 3]), _row("buy-inv-0002", [1, 2, 3])]

    report = evaluate_behavior_checks(cases, rows)

    assert report["groupCount"] == 1
    assert report["passedCount"] == 1
    assert report["groups"]["g1"]["passed"] is True


def test_word_order_fails_when_exposure_lists_differ() -> None:
    cases = [
        _case("buy-inv-0003", test_type="INV", behavior_group_id="g2", behavior_kind="word_order"),
        _case("buy-inv-0004", test_type="INV", behavior_group_id="g2", behavior_kind="word_order"),
    ]
    rows = [_row("buy-inv-0003", [1, 2, 3]), _row("buy-inv-0004", [3, 2, 1])]

    report = evaluate_behavior_checks(cases, rows)

    assert report["groups"]["g2"]["passed"] is False
    assert "노출 목록" in report["groups"]["g2"]["reason"]


def test_invariant_fails_when_fixtures_differ() -> None:
    cases = [
        _case(
            "buy-inv-0005",
            test_type="INV",
            behavior_group_id="g3",
            behavior_kind="color_synonym",
            search_fixture_id="fixture-a",
        ),
        _case(
            "buy-inv-0006",
            test_type="INV",
            behavior_group_id="g3",
            behavior_kind="color_synonym",
            search_fixture_id="fixture-b",
        ),
    ]
    rows = [_row("buy-inv-0005", [1]), _row("buy-inv-0006", [1])]

    report = evaluate_behavior_checks(cases, rows)

    assert report["groups"]["g3"]["passed"] is False
    assert "fixture" in report["groups"]["g3"]["reason"]


def test_constraint_subset_passes_when_stricter_exposure_is_subset() -> None:
    cases = [
        _case(
            "buy-dir-0001",
            test_type="DIR",
            behavior_group_id="g4",
            behavior_kind="constraint_subset",
            expected_filters={"keyword": "셔츠"},
        ),
        _case(
            "buy-dir-0002",
            test_type="DIR",
            behavior_group_id="g4",
            behavior_kind="constraint_subset",
            expected_filters={"keyword": "셔츠", "color": "파랑"},
        ),
    ]
    rows = [_row("buy-dir-0001", [1, 2, 3, 4]), _row("buy-dir-0002", [1, 3])]

    report = evaluate_behavior_checks(cases, rows)

    result = report["groups"]["g4"]
    assert result["passed"] is True
    assert result["stricterCaseId"] == "buy-dir-0002"
    assert result["relaxedCaseId"] == "buy-dir-0001"


def test_constraint_subset_fails_when_stricter_exposure_is_not_subset() -> None:
    cases = [
        _case(
            "buy-dir-0003",
            test_type="DIR",
            behavior_group_id="g5",
            behavior_kind="constraint_subset",
            expected_filters={"keyword": "셔츠"},
        ),
        _case(
            "buy-dir-0004",
            test_type="DIR",
            behavior_group_id="g5",
            behavior_kind="constraint_subset",
            expected_filters={"keyword": "셔츠", "color": "파랑"},
        ),
    ]
    rows = [_row("buy-dir-0003", [1, 2]), _row("buy-dir-0004", [1, 99])]

    report = evaluate_behavior_checks(cases, rows)

    assert report["groups"]["g5"]["passed"] is False


def test_constraint_subset_fails_when_filters_are_not_comparable() -> None:
    cases = [
        _case(
            "buy-dir-0005",
            test_type="DIR",
            behavior_group_id="g6",
            behavior_kind="constraint_subset",
            expected_filters={"keyword": "셔츠"},
        ),
        _case(
            "buy-dir-0006",
            test_type="DIR",
            behavior_group_id="g6",
            behavior_kind="constraint_subset",
            expected_filters={"category": "상의"},
        ),
    ]
    rows = [_row("buy-dir-0005", [1]), _row("buy-dir-0006", [2])]

    report = evaluate_behavior_checks(cases, rows)

    assert report["groups"]["g6"]["passed"] is False
    assert "판별할 수 없습니다" in report["groups"]["g6"]["reason"]


def test_member_recall_ge_guest_passes_when_member_recall_is_higher() -> None:
    cases = [
        _case(
            "buy-dir-0007",
            test_type="DIR",
            behavior_group_id="g7",
            behavior_kind="member_recall_ge_guest",
            identity_kind="guest",
            relevant=[1, 2],
        ),
        _case(
            "buy-dir-0008",
            test_type="DIR",
            behavior_group_id="g7",
            behavior_kind="member_recall_ge_guest",
            identity_kind="member",
            relevant=[1, 2],
        ),
    ]
    rows = [
        _row("buy-dir-0007", [1], relevant=[1, 2]),
        _row("buy-dir-0008", [1, 2], relevant=[1, 2]),
    ]

    report = evaluate_behavior_checks(cases, rows)

    result = report["groups"]["g7"]
    assert result["passed"] is True
    assert result["memberRecall"] == 1.0
    assert result["guestRecall"] == 0.5


def test_member_recall_ge_guest_fails_when_member_recall_is_lower() -> None:
    cases = [
        _case(
            "buy-dir-0009",
            test_type="DIR",
            behavior_group_id="g8",
            behavior_kind="member_recall_ge_guest",
            identity_kind="guest",
            relevant=[1, 2],
        ),
        _case(
            "buy-dir-0010",
            test_type="DIR",
            behavior_group_id="g8",
            behavior_kind="member_recall_ge_guest",
            identity_kind="member",
            relevant=[1, 2],
        ),
    ]
    rows = [
        _row("buy-dir-0009", [1, 2], relevant=[1, 2]),
        _row("buy-dir-0010", [1], relevant=[1, 2]),
    ]

    report = evaluate_behavior_checks(cases, rows)

    assert report["groups"]["g8"]["passed"] is False


def test_mismatched_behavior_kind_in_group_fails_explicitly() -> None:
    cases = [
        _case(
            "buy-inv-0009", test_type="INV", behavior_group_id="g9", behavior_kind="color_synonym"
        ),
        _case("buy-inv-0010", test_type="INV", behavior_group_id="g9", behavior_kind="word_order"),
    ]
    rows = [_row("buy-inv-0009", [1]), _row("buy-inv-0010", [1])]

    report = evaluate_behavior_checks(cases, rows)

    assert report["groups"]["g9"]["passed"] is False
    assert "behaviorKind" in report["groups"]["g9"]["reason"]


def test_mft_cases_and_ungrouped_cases_are_excluded_from_behavior_checks() -> None:
    cases = [
        _case(
            "buy-srch-0001", test_type="MFT", behavior_group_id="", behavior_kind=None, relevant=[1]
        ),
    ]
    rows = [_row("buy-srch-0001", [1], relevant=[1])]

    report = evaluate_behavior_checks(cases, rows)

    assert report["groupCount"] == 0
    assert report["groups"] == {}


def test_summary_counts_reflect_passed_and_failed_groups() -> None:
    cases = [
        _case(
            "buy-inv-0011",
            test_type="INV",
            behavior_group_id="pass-group",
            behavior_kind="color_synonym",
        ),
        _case(
            "buy-inv-0012",
            test_type="INV",
            behavior_group_id="pass-group",
            behavior_kind="color_synonym",
        ),
        _case(
            "buy-inv-0013",
            test_type="INV",
            behavior_group_id="fail-group",
            behavior_kind="word_order",
            search_fixture_id="fixture-x",
        ),
        _case(
            "buy-inv-0014",
            test_type="INV",
            behavior_group_id="fail-group",
            behavior_kind="word_order",
            search_fixture_id="fixture-y",
        ),
    ]
    rows = [
        _row("buy-inv-0011", [1, 2]),
        _row("buy-inv-0012", [1, 2]),
        _row("buy-inv-0013", [1]),
        _row("buy-inv-0014", [1]),
    ]

    report = evaluate_behavior_checks(cases, rows)

    assert report["groupCount"] == 2
    assert report["passedCount"] == 1
    assert report["failedCount"] == 1
