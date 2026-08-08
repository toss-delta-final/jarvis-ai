"""개인화 활성화 지표(Δranking rate) 단위 테스트 (이슈 #482)."""

from __future__ import annotations

import pytest

from evals.personalization.activation import ranking_change
from evals.personalization.cli import _rows_with_metrics


def _row(case_id: str, ranked: list[int], *, repeat: int = 0) -> dict:
    return {"caseId": case_id, "repeat": repeat, "rankedProductIds": ranked, "metrics": {}}


def test_classifies_same_order_and_set_changes() -> None:
    baseline = [
        _row("same", [1, 2, 3]),
        _row("order", [1, 2, 3]),
        _row("set", [1, 2, 3]),
    ]
    arm = [
        _row("same", [1, 2, 3]),
        _row("order", [3, 2, 1]),
        _row("set", [1, 2, 9]),
    ]
    result = ranking_change(baseline, arm)
    assert result["same"] == 1
    assert result["orderOnly"] == 1
    assert result["setChanged"] == 1
    assert result["pairedCount"] == 3
    assert result["changeRate"] == pytest.approx(2 / 3)


def test_both_empty_counts_as_same_and_is_reported_separately() -> None:
    """양쪽 빈 노출은 '프로필이 안 바꿨다'가 아니라 '비교할 노출이 없다'다.

    `same` 에 포함시키되 따로 세지 않으면, 검색 0건이 많은 데이터셋에서 changeRate 가
    조용히 희석돼 활성화가 낮은 것처럼 보인다.
    """
    result = ranking_change([_row("empty", [])], [_row("empty", [])])
    assert result["same"] == 1
    assert result["bothEmpty"] == 1
    assert result["changeRate"] == pytest.approx(0.0)


def test_one_side_empty_is_a_set_change() -> None:
    result = ranking_change([_row("c", [1, 2])], [_row("c", [])])
    assert result["setChanged"] == 1
    assert result["bothEmpty"] == 0


def test_pairs_by_case_and_repeat() -> None:
    """repeat 을 무시하고 caseId 로만 짝지으면 반복끼리 섞여 변화가 조작된다."""
    baseline = [_row("c", [1, 2], repeat=0), _row("c", [2, 1], repeat=1)]
    arm = [_row("c", [1, 2], repeat=0), _row("c", [2, 1], repeat=1)]
    result = ranking_change(baseline, arm)
    assert result["pairedCount"] == 2
    assert result["same"] == 2
    assert result["changeRate"] == pytest.approx(0.0)


def test_unpaired_rows_are_excluded_and_counted() -> None:
    """짝이 없는 행을 조용히 버리면 분모가 말없이 줄어든다 — 개수를 남긴다."""
    baseline = [_row("both", [1]), _row("baseline_only", [1])]
    arm = [_row("both", [1]), _row("arm_only", [1])]
    result = ranking_change(baseline, arm)
    assert result["pairedCount"] == 1
    assert result["unpairedBaseline"] == 1
    assert result["unpairedArm"] == 1


def test_duplicate_case_repeat_key_raises() -> None:
    """중복 키를 마지막 값으로 덮으면 한쪽 결과가 소리 없이 사라진다."""
    rows = [_row("c", [1]), _row("c", [2])]
    with pytest.raises(ValueError, match="중복"):
        ranking_change(rows, rows)


def test_product_ids_are_compared_as_integers() -> None:
    """산출물 JSON 은 id 를 문자열로 실을 수 있다 — 타입 차이가 변화로 오인되면 안 된다."""
    result = ranking_change(
        [_row("c", [1, 2])], [{"caseId": "c", "repeat": 0, "rankedProductIds": ["1", "2"]}]
    )
    assert result["same"] == 1


def test_empty_input_yields_zero_rate_not_division_error() -> None:
    result = ranking_change([], [])
    assert result["pairedCount"] == 0
    assert result["changeRate"] is None


def test_budget_exceeded_row_is_not_counted_as_a_ranking_change() -> None:
    """예산 소진 행(`metrics=None`·빈 노출)이 프로필 효과로 둔갑하면 안 된다 (PR #485 리뷰).

    `run_repeats` 는 실행 도중 예산이 소진되면 `metrics=None`·`rankedProductIds=[]` 인
    `failureReason="budgetExceeded"` 행을 남긴다. 이득 지표는 그 행을 거르는데 활성화 지표가
    raw 행을 읽으면 baseline 의 정상 행과 짝지어져 `setChanged` 로 잡힌다.
    """
    baseline = {"caseResults": [_row("a", [1, 2]), _row("b", [3, 4])]}
    arm = {
        "caseResults": [
            _row("a", [1, 2]),
            {
                "caseId": "b",
                "repeat": 0,
                "metrics": None,
                "rankedProductIds": [],
                "hardFailure": True,
                "failureReason": "budgetExceeded",
            },
        ]
    }

    naive = ranking_change(baseline["caseResults"], arm["caseResults"])
    assert naive["setChanged"] == 1  # 거르지 않으면 예산 소진이 '변화' 로 잡힌다

    filtered = ranking_change(_rows_with_metrics(baseline), _rows_with_metrics(arm))
    assert filtered["setChanged"] == 0
    assert filtered["pairedCount"] == 1
    assert filtered["unpairedBaseline"] == 1  # 측정이 중단된 자리는 분모에서 빠지고 개수로 남는다


def test_rows_with_metrics_keeps_hard_failure_rows_that_have_metrics() -> None:
    """adapter 예외로 빈 노출이 된 행은 실제 산출 결과이므로 거르지 않는다."""
    results = {
        "caseResults": [
            {
                "caseId": "a",
                "repeat": 0,
                "metrics": {},
                "rankedProductIds": [],
                "hardFailure": True,
            },
            {"caseId": "b", "repeat": 0, "metrics": None, "rankedProductIds": []},
            {"caseId": "c", "repeat": 0, "rankedProductIds": [1]},
        ]
    }
    assert [row["caseId"] for row in _rows_with_metrics(results)] == ["a"]
