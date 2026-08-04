"""축 정의와 채점 (#260).

이 파일의 존재 이유: **같은 표본을 다른 정의로 세면 숫자가 갈린다**는 사실을 코드로 못 박는 것.
#234 의 `productId 7/8`("목록 안에 있으면 정답")과 #240 의 같은 이름 지표("되물음 상품이 아니어야
정답")가 뜻이 달라 두 표의 비교가 깨졌다.
"""

from __future__ import annotations

from evals.intent_probe.loader import build_cells, load_anchor_set
from evals.intent_probe.metrics import (
    AXES,
    ISSUE_240_AXIS_ORDER,
    diagnostics,
    issue240_line,
    score_all,
)
from evals.intent_probe.runner import CellResult, Sample
from evals.intent_probe.schema import AXIS_IDS

ANCHORS = load_anchor_set("b")
CELLS = build_cells(ANCHORS)
N = 8
OTHER_PRODUCT = 104  # 무선 블루투스 이어폰 — 되물음 상품(102)이 아닌 상품


def _sample(cell_id: str, index: int, **overrides: object) -> Sample:
    base = {
        "cell_id": cell_id,
        "sample_index": index,
        "intent": "recommend",
        "product_id": None,
        "option_id": None,
        "quantity": 1,
        "case": 2,
        "scoped_to_previous": False,
        "latency_ms": 10,
    }
    base.update(overrides)
    return Sample(**base)  # type: ignore[arg-type]


def _perfect_results(n: int = N) -> list[CellResult]:
    """모든 셀이 정답을 n번 낸 가상 결과."""
    results: list[CellResult] = []
    for cell in CELLS:
        expected = cell.utterance.expected
        overrides: dict[str, object] = {"intent": expected.intent}
        if cell.utterance.group == "option_answer":
            overrides["option_id"] = expected.option_id
            overrides["product_id"] = ANCHORS.reask_product_id
        if cell.utterance.group == "switch":
            overrides["product_id"] = OTHER_PRODUCT
        results.append(
            CellResult(
                cell_id=cell.cell_id,
                utterance_id=cell.utterance.utterance_id,
                context_id=cell.context.context_id,
                group=cell.utterance.group,
                samples=[_sample(cell.cell_id, index, **overrides) for index in range(n)],
                attempts=n,
                filled=True,
            )
        )
    return results


def test_axis_ids_match_the_fixture_allowlist() -> None:
    assert {spec.axis_id for spec in AXES} == AXIS_IDS


def test_expected_denominators_match_issue_240_shape() -> None:
    axes = score_all(_perfect_results(), ANCHORS, n=N)
    assert {axis_id: axis.expected_denominator for axis_id, axis in axes.items()} == {
        "mainIntent": 240,
        "cartControl": 144,
        "demonstrative": 96,
        "optionAnswer": 32,
        "switchLegacy2": 16,
        "switchAll7": 56,
        "cartAddProductIdLegacy2": 16,
        "orderStatus": 48,
        "general": 48,
    }


def test_perfect_run_scores_full_marks() -> None:
    axes = score_all(_perfect_results(), ANCHORS, n=N)
    for axis in axes.values():
        assert axis.numerator == axis.expected_denominator == axis.denominator
        assert axis.ratio == 1.0


def test_main_intent_is_the_sum_of_its_components() -> None:
    axes = score_all(_perfect_results(), ANCHORS, n=N)
    assert (
        axes["mainIntent"].numerator
        == axes["cartControl"].numerator + axes["demonstrative"].numerator
    )


def test_two_product_id_definitions_diverge_on_the_same_samples() -> None:
    # 되물음 상품을 그대로 에코한 표본: #240 정의로는 전부 오답, #234 정의로는 전부 정답이다.
    results = []
    for cell in CELLS:
        if cell.utterance.group != "switch":
            continue
        results.append(
            CellResult(
                cell_id=cell.cell_id,
                utterance_id=cell.utterance.utterance_id,
                context_id=cell.context.context_id,
                group="switch",
                samples=[
                    _sample(
                        cell.cell_id,
                        index,
                        intent="cart_add",
                        product_id=ANCHORS.reask_product_id,
                    )
                    for index in range(N)
                ],
                attempts=N,
                filled=True,
            )
        )
    axes = score_all(results, ANCHORS, n=N)
    assert axes["switchLegacy2"].numerator == 0
    assert axes["cartAddProductIdLegacy2"].numerator == 16
    assert "cartAddProductIdLegacy2" in axes["switchLegacy2"].not_comparable_with
    assert "switchLegacy2" in axes["cartAddProductIdLegacy2"].not_comparable_with


def test_option_axis_requires_both_intent_and_option_id() -> None:
    cell = next(cell for cell in CELLS if cell.utterance.group == "option_answer")
    wrong_option = next(
        option.option_id
        for option in ANCHORS.options
        if option.option_id != cell.utterance.expected.option_id
    )
    result = CellResult(
        cell_id=cell.cell_id,
        utterance_id=cell.utterance.utterance_id,
        context_id=cell.context.context_id,
        group="option_answer",
        samples=[
            _sample(cell.cell_id, index, intent="cart_add", option_id=wrong_option)
            for index in range(N)
        ],
        attempts=N,
        filled=True,
    )
    axes = score_all([result], ANCHORS, n=N)
    assert axes["optionAnswer"].numerator == 0
    assert axes["optionAnswer"].denominator == N


def test_unfilled_cell_shrinks_denominator_but_not_expected_denominator() -> None:
    results = _perfect_results()
    starved = next(result for result in results if result.group == "general")
    starved.samples = starved.samples[:3]
    starved.filled = False
    axes = score_all(results, ANCHORS, n=N)
    assert axes["general"].denominator == 48 - 5
    assert axes["general"].expected_denominator == 48
    assert axes["general"].unfilled_sample_count == 5


def test_issue_240_line_keeps_the_original_axis_order() -> None:
    axes = score_all(_perfect_results(), ANCHORS, n=N)
    assert issue240_line(axes) == "240/144/96/32/16/48/48/16"
    assert ISSUE_240_AXIS_ORDER == (
        "mainIntent",
        "cartControl",
        "demonstrative",
        "optionAnswer",
        "switchLegacy2",
        "orderStatus",
        "general",
        "cartAddProductIdLegacy2",
    )


def test_diagnostics_separate_dangerous_echo_from_safe_null() -> None:
    # #240 §5: fast 는 되물음 상품을 12회 에코했고 smart 는 0/56 이었다. 에코는 사용자가 고르지
    # 않은 옵션으로 옛 상품이 담기는 경로라, null(안전한 퇴화)과 같이 세면 안 된다.
    switch_cells = [cell for cell in CELLS if cell.utterance.group == "switch"][:2]
    results = [
        CellResult(
            cell_id=switch_cells[0].cell_id,
            utterance_id=switch_cells[0].utterance.utterance_id,
            context_id="pendingCart",
            group="switch",
            samples=[
                _sample(
                    switch_cells[0].cell_id,
                    index,
                    intent="cart_add",
                    product_id=ANCHORS.reask_product_id,
                )
                for index in range(3)
            ],
            attempts=3,
            filled=False,
        ),
        CellResult(
            cell_id=switch_cells[1].cell_id,
            utterance_id=switch_cells[1].utterance.utterance_id,
            context_id="pendingCart",
            group="switch",
            samples=[
                _sample(switch_cells[1].cell_id, index, intent="cart_add", product_id=None)
                for index in range(2)
            ],
            attempts=2,
            filled=False,
        ),
    ]
    counts = diagnostics(results, ANCHORS)
    assert counts["reaskProductEchoCount"] == 3
    assert counts["productIdNullCount"] == 2


def test_every_axis_carries_its_definition_into_the_result() -> None:
    axes = score_all(_perfect_results(), ANCHORS, n=N)
    for axis in axes.values():
        payload = axis.as_dict()
        assert payload["definition"]["numerator"]
        assert payload["definition"]["denominator"]
