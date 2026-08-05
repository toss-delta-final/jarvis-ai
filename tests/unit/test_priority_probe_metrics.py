"""priority 프로브 축 집계 — 손으로 센 값과 대조한다 (#281 TASK 3 §6).

전부 합성 픽스처·표본이라 CI 에서 API 콜이 0이다.
"""

from __future__ import annotations

from evals.priority_probe.metrics import (
    score_all,
    score_essential_protected,
    score_exact,
    score_order_pairs,
    score_order_pairs_by_index,
    score_signal_present,
)
from evals.priority_probe.runner import CellResult, Sample
from evals.priority_probe.schema import Channel, FixtureSet, PriorityCell


def _filler_cells(count: int) -> list[PriorityCell]:
    """스키마의 `cells` 최소 길이(10)를 채우는 미사용 셀 — 축 계산은 `results` 로만 도니
    이 셀들이 숫자에 영향을 주지 않는다."""
    return [_cell(f"filler-{i}", ["x", "y"], [1, 2]) for i in range(count)]


def _fixture(cells: list[PriorityCell]) -> FixtureSet:
    return FixtureSet(
        fixture_version="test-v1",
        channel=Channel(prior_filters={"category": None}, last_recommendations=[]),
        cells=[*cells, *_filler_cells(max(0, 10 - len(cells)))],
    )


def _cell(cell_id: str, needs: list[str], expected: list[int]) -> PriorityCell:
    return PriorityCell(
        cell_id=cell_id,
        utterance=f"{cell_id} 발화",
        needs=needs,
        expected_priorities=expected,
        rationale="테스트용 손 계산 근거 문장입니다 스무 자 이상.",
    )


def _sample(
    cell_id: str,
    index: int,
    priorities: tuple,
    *,
    priorities_by_index: tuple | None = None,
    length_mismatch: bool = False,
) -> Sample:
    return Sample(
        cell_id=cell_id,
        sample_index=index,
        priorities=priorities,
        priorities_by_index=priorities_by_index,
        raw_legs=(),
        length_mismatch=length_mismatch,
        latency_ms=0,
    )


def test_order_pairs_counts_only_pairs_with_different_expected_values() -> None:
    """3 니즈 [1,2,3] → 쌍 3개(0-1,0-2,1-2) 전부 기대값이 달라 분모에 들어간다."""
    cell = _cell("c1", ["a", "b", "c"], [1, 2, 3])
    fixture = _fixture([cell])
    result = CellResult(cell_id="c1", arm="classifier", samples=[_sample("c1", 0, (1, 2, 3))])

    metric = score_order_pairs([result], fixture)

    assert metric.denominator == 3  # (a,b) (a,c) (b,c) 전부 기대값이 다르다
    assert metric.numerator == 3  # 산출이 기대와 정확히 같은 순서


def test_order_pairs_ignores_pairs_with_equal_expected_values() -> None:
    """4 니즈 [1,1,2,3] — (텐트,침낭) 쌍은 기대값이 같아 분모에서 빠진다."""
    cell = _cell("c2", ["tent", "bag", "burner", "lantern"], [1, 1, 2, 3])
    fixture = _fixture([cell])
    # 산출도 기대와 동일 — 전부 정답이어야 한다.
    result = CellResult(cell_id="c2", arm="classifier", samples=[_sample("c2", 0, (1, 1, 2, 3))])

    metric = score_order_pairs([result], fixture)

    # 쌍 6개 중 (tent,bag) 1쌍만 기대값이 같아 제외 → 분모 5.
    assert metric.denominator == 5
    assert metric.numerator == 5


def test_order_pairs_penalizes_a_flipped_relative_order() -> None:
    """산출이 기대와 **반대** 순서면 그 쌍은 오답이다 — 절대값이 아니라 대소 관계를 본다."""
    cell = _cell("c3", ["a", "b"], [1, 3])  # 기대: a < b (a 가 더 필수)
    fixture = _fixture([cell])
    flipped = CellResult(
        cell_id="c3", arm="classifier", samples=[_sample("c3", 0, (3, 1))]
    )  # 산출: a > b

    metric = score_order_pairs([flipped], fixture)

    assert metric.denominator == 1
    assert metric.numerator == 0  # 대소 관계가 뒤집혔다


def test_order_pairs_absolute_value_shift_still_counts_as_correct() -> None:
    """절대값이 한 칸씩 밀려도(1,2→2,3) 순서만 맞으면 정답이다 — 이 이슈의 핵심 주장."""
    cell = _cell("c4", ["a", "b"], [1, 2])
    fixture = _fixture([cell])
    shifted = CellResult(cell_id="c4", arm="classifier", samples=[_sample("c4", 0, (2, 3))])

    metric = score_order_pairs([shifted], fixture)

    assert metric.numerator == metric.denominator == 1


def test_order_pairs_treats_a_missing_value_as_wrong_not_excluded() -> None:
    cell = _cell("c5", ["a", "b"], [1, 3])
    fixture = _fixture([cell])
    missing = CellResult(cell_id="c5", arm="classifier", samples=[_sample("c5", 0, (1, None))])

    metric = score_order_pairs([missing], fixture)

    assert metric.denominator == 1
    assert metric.numerator == 0


def test_essential_protected_requires_the_essential_value_to_be_the_sample_minimum() -> None:
    """기대 1(필수)이 **그 표본 안에서** 최소값이어야 보호된 것이다."""
    cell = _cell("c6", ["a", "b", "c"], [1, 2, 3])
    fixture = _fixture([cell])
    protected = CellResult(cell_id="c6", arm="classifier", samples=[_sample("c6", 0, (1, 2, 3))])
    not_protected = CellResult(
        cell_id="c6", arm="classifier", samples=[_sample("c6", 0, (3, 2, 1))]
    )

    assert score_essential_protected([protected], fixture).numerator == 1
    assert score_essential_protected([not_protected], fixture).numerator == 0


def test_essential_protected_handles_two_essential_needs() -> None:
    """essential 이 둘이면(캠핑: 텐트·침낭) 둘 다 표본 최소값과 같아야 각각 보호로 센다."""
    cell = _cell("c7", ["tent", "bag", "burner"], [1, 1, 2])
    fixture = _fixture([cell])
    both_min = CellResult(cell_id="c7", arm="classifier", samples=[_sample("c7", 0, (1, 1, 2))])

    metric = score_essential_protected([both_min], fixture)

    assert metric.denominator == 2  # essential 니즈 2개
    assert metric.numerator == 2


def test_signal_present_counts_non_none_slots_only() -> None:
    cell = _cell("c8", ["a", "b", "c"], [1, 2, 3])
    fixture = _fixture([cell])
    result = CellResult(cell_id="c8", arm="classifier", samples=[_sample("c8", 0, (1, None, 3))])

    metric = score_signal_present([result], fixture)

    assert metric.denominator == 3
    assert metric.numerator == 2


def test_exact_counts_value_equality_and_none_never_matches() -> None:
    cell = _cell("c9", ["a", "b"], [1, 2])
    fixture = _fixture([cell])
    result = CellResult(cell_id="c9", arm="classifier", samples=[_sample("c9", 0, (1, None))])

    metric = score_exact([result], fixture)

    assert metric.denominator == 2
    assert metric.numerator == 1  # a 만 일치, b 는 None 이라 불일치


def test_score_all_returns_all_five_metrics() -> None:
    cell = _cell("c10", ["a", "b"], [1, 3])
    fixture = _fixture([cell])
    result = CellResult(cell_id="c10", arm="classifier", samples=[_sample("c10", 0, (1, 3))])

    metrics = score_all([result], fixture)

    assert set(metrics) == {
        "priorityOrderPairs",
        "priorityOrderPairsByIndex",
        "essentialProtected",
        "prioritySignalPresent",
        "priorityExact",
    }


# ─────────── [TASK-3-CORRECTION-2] 보조 축 — 위치 매칭 ───────────


def test_order_pairs_by_index_excludes_samples_with_a_different_leg_count() -> None:
    """leg 개수가 needs 개수와 다르면(priorities_by_index=None) 그 표본은 분모에서 빠진다."""
    cell = _cell("c11", ["a", "b"], [1, 3])
    fixture = _fixture([cell])
    incomparable = CellResult(
        cell_id="c11", arm="inline", samples=[_sample("c11", 0, (None, None))]
    )  # priorities_by_index 기본값 None

    metric = score_order_pairs_by_index([incomparable], fixture)

    assert metric.denominator == 0
    assert metric.numerator == 0


def test_order_pairs_by_index_scores_only_comparable_samples() -> None:
    """leg 개수가 같으면(우연히) 이름과 무관하게 위치로 순서를 채점한다."""
    cell = _cell("c12", ["a", "b"], [1, 3])
    fixture = _fixture([cell])
    comparable = CellResult(
        cell_id="c12",
        arm="inline",
        samples=[_sample("c12", 0, (None, None), priorities_by_index=(1, 3))],
    )

    metric = score_order_pairs_by_index([comparable], fixture)

    assert metric.denominator == 1
    assert metric.numerator == 1
