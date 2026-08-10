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
        # [#84] 기본은 "카테고리 신호 없음 + 산출 없음" — resolve_category_action 의 폴백(carry)과
        # 같은 상태다. 카테고리 셀은 아래 _perfect_results 가 명시적으로 덮어쓴다.
        "has_category_signal": False,
        "scope_free": None,
        "category_legs_echo_prior": False,
        "category_legs": "",
        "resolved_category_action": "carry",
        # [#300] screen 셀이 아니면 해소기는 발동하지 않는다 — resolved == 원본(None), fired False.
        "resolved_product_id": None,
        "screen_resolver_fired": False,
        "screen_resolution_reason": None,
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
        if cell.utterance.group == "category_action":
            action = expected.category_action
            overrides["has_category_signal"] = action == "replace"
            overrides["scope_free"] = action == "clear"
            overrides["resolved_category_action"] = action
        if cell.utterance.group == "screen":
            # [#300] 채점은 해소기 통과 후 최종값을 본다 — screenExact 는 그 값이 productId 와
            # 일치, screenReask/screenNotHallucinated 는 None(비어 있음)이면 만점이다.
            overrides["resolved_product_id"] = (
                expected.product_id if expected.product_id_rule == "screenExact" else None
            )
        if cell.utterance.group == "named_category":
            # [#443] 만점 표본 = leg 이 1개 이상 나온 경우(반대 방향 condition_only 는 기본값
            # "" 이 그대로 만점이다 — 여기서만 덮어쓴다).
            overrides["category_legs"] = "과일|과일"
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
        # [#84] 카테고리 15발화 × categoryPrior 1종 × N=8(라운드 3 이 혼합 4발화를 더했다).
        "categoryAction3Way": 120,
        "categoryCarry": 32,
        "categoryClear": 32,
        "categoryReplace": 24,
        "categoryMixedReplace": 32,
        # [#300] screen 6발화 — 확정 4(32) · 되물음 1(8) · 확정금지 1(8) = 48.
        "screenExactPick": 32,
        "screenReask": 8,
        "screenNoHallucination": 8,
        "screenResolution": 48,
        # [#344 라운드 2] 조건 전용 5발화 × none 컨텍스트 × N=8.
        "conditionOnlyNoCategoryQuery": 40,
        # [#386] 찜 조회 6발화 × none 컨텍스트 × N=8 — 양성 3(24) · 음성 대조 3(24) = 48.
        "wishlistViewPositive": 24,
        "wishlistViewNoSteal": 24,
        "wishlistViewRouting": 48,
        # [#440] 찜 해제 4발화 × none 컨텍스트 × N=8 — 양성 2(16) · 음성 대조 2(16) = 32.
        "wishlistRemovePositive": 16,
        "wishlistRemoveNoSteal": 16,
        "wishlistRemoveRouting": 32,
        # [#285, I-25 §4.13 — 4단계] 수량 변경 6발화 × none 컨텍스트 × N=8 — 양성 3(24) ·
        # 음성 대조 3(24) = 48.
        "cartQuantityPositive": 24,
        "cartQuantityNoSteal": 24,
        "cartQuantityRouting": 48,
        # [#443] 상품군 명시 6발화 × none 컨텍스트 × N=8.
        "namedCategoryHasLeg": 48,
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


# ─────────── #84 카테고리 승계 3분기 축과 진단 ───────────


CATEGORY_CELLS = [cell for cell in CELLS if cell.utterance.group == "category_action"]


def _category_results(resolved_for) -> list[CellResult]:  # noqa: ANN001
    """카테고리 셀만 담은 가상 결과 — `resolved_for(expected)` 가 확정값을 정한다."""
    results = []
    for cell in CATEGORY_CELLS:
        expected = cell.utterance.expected.category_action
        resolved = resolved_for(expected)
        results.append(
            CellResult(
                cell_id=cell.cell_id,
                utterance_id=cell.utterance.utterance_id,
                context_id=cell.context.context_id,
                group="category_action",
                samples=[
                    _sample(
                        cell.cell_id,
                        index,
                        intent="recommend",
                        has_category_signal=resolved == "replace",
                        scope_free=resolved == "clear",
                        resolved_category_action=resolved,
                    )
                    for index in range(N)
                ],
                attempts=N,
                filled=True,
            )
        )
    return results


def test_category_axes_count_only_their_own_bucket() -> None:
    axes = score_all(_category_results(lambda expected: expected), ANCHORS, n=N)
    assert axes["categoryAction3Way"].numerator == 120
    assert axes["categoryCarry"].numerator == 32
    assert axes["categoryClear"].numerator == 32
    assert axes["categoryReplace"].numerator == 24
    # [라운드 3] 혼합 발화는 **따로** 센다 — categoryReplace 의 분모(24)를 유지해야 v2 기준선과
    # 비교가 되기 때문이다(실패의 모양을 갈라 세는 규약).
    assert axes["categoryMixedReplace"].numerator == 32
    # 기존 축은 카테고리 표본을 한 개도 세지 않는다 — 기준선과 비교 가능성이 이 격리에 달렸다.
    assert axes["mainIntent"].expected_denominator == 0
    assert axes["general"].expected_denominator == 0


def test_three_way_axis_is_the_sum_of_its_components() -> None:
    # 리셋만 전부 carry 로 무너진 런 — 합축이 부분축의 합이라는 관계가 유지되어야 한다.
    axes = score_all(
        _category_results(lambda expected: "carry" if expected == "clear" else expected),
        ANCHORS,
        n=N,
    )
    assert axes["categoryClear"].numerator == 0
    assert axes["categoryAction3Way"].numerator == (
        axes["categoryCarry"].numerator
        + axes["categoryClear"].numerator
        + axes["categoryReplace"].numerator
        + axes["categoryMixedReplace"].numerator
    )
    assert axes["categoryAction3Way"].components == (
        "categoryCarry",
        "categoryClear",
        "categoryReplace",
        "categoryMixedReplace",
    )


def test_category_axes_declare_they_are_absent_from_the_baseline() -> None:
    axes = score_all(_category_results(lambda expected: expected), ANCHORS, n=N)
    for axis_id in ("categoryAction3Way", "categoryCarry", "categoryClear", "categoryReplace"):
        assert any(
            "baselines/fast-2026-08-04" in note for note in axes[axis_id].not_comparable_with
        )


def test_category_axis_scores_the_resolved_value_not_the_raw_signal() -> None:
    """분류기 산출이 아니라 **확정값**을 센다 — 사용자가 겪는 동작은 가드가 고른 쪽이다."""
    cell = next(
        cell for cell in CATEGORY_CELLS if cell.utterance.expected.category_action == "clear"
    )
    result = CellResult(
        cell_id=cell.cell_id,
        utterance_id=cell.utterance.utterance_id,
        context_id=cell.context.context_id,
        group="category_action",
        samples=[
            _sample(
                cell.cell_id,
                index,
                intent="recommend",
                scope_free=False,
                has_category_signal=True,
                resolved_category_action="replace",
            )
            for index in range(N)
        ],
        attempts=N,
        filled=True,
    )
    axes = score_all([result], ANCHORS, n=N)
    assert axes["categoryClear"].numerator == 0  # 기대는 clear 인데 확정값이 replace 인 표본
    assert axes["categoryClear"].denominator == N


def test_diagnostics_count_refine_turns_that_got_cleared() -> None:
    carry_cell = next(
        cell for cell in CATEGORY_CELLS if cell.utterance.expected.category_action == "carry"
    )
    clear_cell = next(
        cell for cell in CATEGORY_CELLS if cell.utterance.expected.category_action == "clear"
    )
    results = [
        # 리파인 셀: 3건은 산출 누락(→ carry 폴백), 2건은 clear 로 풀림(새 회귀 모양).
        CellResult(
            cell_id=carry_cell.cell_id,
            utterance_id=carry_cell.utterance.utterance_id,
            context_id=carry_cell.context.context_id,
            group="category_action",
            samples=[_sample(carry_cell.cell_id, index) for index in range(3)]
            + [
                _sample(
                    carry_cell.cell_id,
                    index,
                    scope_free=True,
                    resolved_category_action="clear",
                )
                for index in range(3, 5)
            ],
            attempts=5,
            filled=False,
        ),
        # 리셋 셀이 clear 로 확정된 것은 정답이지 회귀가 아니다 — 카운터가 이를 섞으면 안 된다.
        CellResult(
            cell_id=clear_cell.cell_id,
            utterance_id=clear_cell.utterance.utterance_id,
            context_id=clear_cell.context.context_id,
            group="category_action",
            samples=[
                _sample(
                    clear_cell.cell_id,
                    index,
                    scope_free=True,
                    resolved_category_action="clear",
                )
                for index in range(4)
            ],
            attempts=4,
            filled=False,
        ),
    ]
    counts = diagnostics(results, ANCHORS)
    # 리파인 기대 셀이 clear 로 풀린 표본만 센다 — 이 변경이 만들 수 있는 유일한 새 회귀 모양이다.
    assert counts["categoryClearOnRefineCount"] == 2
    # 카테고리 셀은 전환 진단을 오염시키지 않는다.
    assert counts["reaskProductEchoCount"] == 0
    assert counts["productIdNullCount"] == 0


def test_carry_is_scored_correct_when_the_legs_merely_echo_the_prior() -> None:
    """[2차 리뷰 P2] 프롬프트가 시킨 prior 에코 leg 때문에 확정값이 replace 여도 정답이다.

    보정이 없으면 이 축은 **정상 동작을 오답으로** 센다(실측 1/32). 반대로 에코가 아닌 leg 는
    진짜 교체라 오답으로 남아야 한다 — 아래 대조군이 그 판별력을 준다.
    """
    cell = next(
        cell for cell in CATEGORY_CELLS if cell.utterance.expected.category_action == "carry"
    )

    def _result(echo: bool) -> CellResult:
        return CellResult(
            cell_id=cell.cell_id,
            utterance_id=cell.utterance.utterance_id,
            context_id=cell.context.context_id,
            group="category_action",
            samples=[
                _sample(
                    cell.cell_id,
                    index,
                    has_category_signal=True,
                    resolved_category_action="replace",
                    category_legs_echo_prior=echo,
                )
                for index in range(N)
            ],
            attempts=N,
            filled=True,
        )

    assert score_all([_result(True)], ANCHORS, n=N)["categoryCarry"].numerator == N
    assert score_all([_result(False)], ANCHORS, n=N)["categoryCarry"].numerator == 0


def test_scope_free_unresolved_is_counted() -> None:
    cell = next(
        cell for cell in CATEGORY_CELLS if cell.utterance.expected.category_action == "clear"
    )
    results = [
        CellResult(
            cell_id=cell.cell_id,
            utterance_id=cell.utterance.utterance_id,
            context_id=cell.context.context_id,
            group="category_action",
            samples=[_sample(cell.cell_id, index, scope_free=None) for index in range(3)]
            + [_sample(cell.cell_id, 3, scope_free=True, resolved_category_action="clear")],
            attempts=4,
            filled=True,
        )
    ]
    assert diagnostics(results, ANCHORS)["categoryScopeUnresolvedCount"] == 3


# ─────────── [#300] screen 지시어 해소(#118 이관) 축과 진단 ───────────

SCREEN_CELLS = [cell for cell in CELLS if cell.utterance.group == "screen"]


def _screen_result(
    cell, resolved_product_id: int | None, *, intent: str = "cart_add"
) -> CellResult:  # noqa: ANN001
    return CellResult(
        cell_id=cell.cell_id,
        utterance_id=cell.utterance.utterance_id,
        context_id=cell.context.context_id,
        group="screen",
        samples=[
            _sample(
                cell.cell_id,
                index,
                intent=intent,
                product_id=resolved_product_id,
                resolved_product_id=resolved_product_id,
            )
            for index in range(N)
        ],
        attempts=N,
        filled=True,
    )


def test_screen_axes_count_only_their_own_bucket() -> None:
    """새 셀이 기존 축의 분모를 늘리면 안 된다 — screen 셀만 채점해도 기존 축은 0이어야 한다."""
    results = [
        _screen_result(
            cell, cell.utterance.expected.product_id or cell.utterance.expected.forbidden_product_id
        )
        for cell in SCREEN_CELLS
    ]
    axes = score_all(results, ANCHORS, n=N)
    assert axes["mainIntent"].expected_denominator == 0
    assert axes["general"].expected_denominator == 0
    assert axes["categoryAction3Way"].expected_denominator == 0
    assert axes["screenExactPick"].expected_denominator == 32
    assert axes["screenReask"].expected_denominator == 8
    assert axes["screenNoHallucination"].expected_denominator == 8
    assert axes["screenResolution"].expected_denominator == 48


def test_screen_resolution_is_the_sum_of_its_components() -> None:
    results = [
        _screen_result(
            cell,
            cell.utterance.expected.product_id
            if cell.utterance.expected.product_id_rule == "screenExact"
            else None,
        )
        for cell in SCREEN_CELLS
    ]
    axes = score_all(results, ANCHORS, n=N)
    assert axes["screenResolution"].numerator == (
        axes["screenExactPick"].numerator
        + axes["screenReask"].numerator
        + axes["screenNoHallucination"].numerator
    )
    assert axes["screenResolution"].numerator == 48
    assert axes["screenResolution"].components == (
        "screenExactPick",
        "screenReask",
        "screenNoHallucination",
    )


def test_screen_exact_requires_cart_add_intent() -> None:
    """[§4.4] 원본 `_product()` 술어와 같다 — productId 만 맞고 intent 가 cart_add 가 아니면 오답."""
    cell = next(c for c in SCREEN_CELLS if c.utterance.expected.product_id_rule == "screenExact")
    result = _screen_result(cell, cell.utterance.expected.product_id, intent="recommend")
    axes = score_all([result], ANCHORS, n=N)
    assert axes["screenExactPick"].numerator == 0


def test_screen_reask_ignores_intent() -> None:
    """[§4.4] 원본 `_no_product()` 와 같다 — resolvedProductId 가 None 이면 intent 와 무관하게 정답."""
    cell = next(c for c in SCREEN_CELLS if c.utterance.expected.product_id_rule == "screenReask")
    result = _screen_result(cell, None, intent="general")
    axes = score_all([result], ANCHORS, n=N)
    assert axes["screenReask"].numerator == N


def test_screen_not_hallucinated_ignores_intent() -> None:
    """[§4.4] 원본 `_not_hallucinated()` 와 같다 — forbidden 과 다르면 intent 와 무관하게 정답."""
    cell = next(
        c for c in SCREEN_CELLS if c.utterance.expected.product_id_rule == "screenNotHallucinated"
    )
    result = _screen_result(cell, None, intent="recommend")
    axes = score_all([result], ANCHORS, n=N)
    assert axes["screenNoHallucination"].numerator == N


def test_screen_axes_declare_absence_from_the_committed_baselines() -> None:
    axes = score_all(
        [
            _screen_result(
                cell,
                cell.utterance.expected.product_id
                if cell.utterance.expected.product_id_rule == "screenExact"
                else None,
            )
            for cell in SCREEN_CELLS
        ],
        ANCHORS,
        n=N,
    )
    for axis_id in (
        "screenExactPick",
        "screenReask",
        "screenNoHallucination",
        "screenResolution",
    ):
        assert any(
            "baselines/fast-2026-08-04" in note for note in axes[axis_id].not_comparable_with
        )
        assert any("#118 원 프로브" in note for note in axes[axis_id].not_comparable_with)


def test_existing_axis_denominators_are_unaffected_by_screen_cells() -> None:
    """[§5.6] 기존 축 회귀 0 — screen 셀을 넣은 픽스처로 채점해도 기존 축의 기대 분모는 그대로다."""
    axes = score_all(_perfect_results(), ANCHORS, n=N)
    assert axes["mainIntent"].expected_denominator == 240
    assert axes["cartControl"].expected_denominator == 144
    assert axes["demonstrative"].expected_denominator == 96
    assert axes["optionAnswer"].expected_denominator == 32
    assert axes["switchAll7"].expected_denominator == 56
    assert axes["orderStatus"].expected_denominator == 48
    assert axes["general"].expected_denominator == 48
    assert axes["categoryAction3Way"].expected_denominator == 120


def test_diagnostics_count_prompt_layer_hits_resolver_overrides_and_out_of_list_confirms() -> None:
    results = []
    for cell in SCREEN_CELLS:
        expected = cell.utterance.expected
        if expected.product_id_rule == "screenExact":
            # 원본(해소기 전) 산출도 맞았고, 최종값도 해소기가 발동해 같은 값으로 나온 표본.
            results.append(
                CellResult(
                    cell_id=cell.cell_id,
                    utterance_id=cell.utterance.utterance_id,
                    context_id=cell.context.context_id,
                    group="screen",
                    samples=[
                        _sample(
                            cell.cell_id,
                            index,
                            intent="cart_add",
                            product_id=expected.product_id,
                            resolved_product_id=expected.product_id,
                            screen_resolver_fired=True,
                        )
                        for index in range(N)
                    ],
                    attempts=N,
                    filled=True,
                )
            )
        elif expected.product_id_rule == "screenNotHallucinated":
            # 원본은 두 목록 밖 id 를 확정(위험한 실패)했지만 해소기가 None 으로 되돌린 표본.
            results.append(
                CellResult(
                    cell_id=cell.cell_id,
                    utterance_id=cell.utterance.utterance_id,
                    context_id=cell.context.context_id,
                    group="screen",
                    samples=[
                        _sample(
                            cell.cell_id,
                            index,
                            intent="cart_add",
                            product_id=expected.forbidden_product_id,
                            resolved_product_id=None,
                            screen_resolver_fired=True,
                        )
                        for index in range(N)
                    ],
                    attempts=N,
                    filled=True,
                )
            )
        else:
            # screenReask: 해소기가 발동하지 않았고(fired False) 최종값이 두 목록 밖이라
            # screenOutOfListConfirmCount 가 잡아야 하는 위험한 실패.
            results.append(
                CellResult(
                    cell_id=cell.cell_id,
                    utterance_id=cell.utterance.utterance_id,
                    context_id=cell.context.context_id,
                    group="screen",
                    samples=[
                        _sample(
                            cell.cell_id,
                            index,
                            intent="cart_add",
                            product_id=999999,
                            resolved_product_id=999999,
                            screen_resolver_fired=False,
                        )
                        for index in range(N)
                    ],
                    attempts=N,
                    filled=True,
                )
            )
    counts = diagnostics(results, ANCHORS)
    # screenExact 4발화 전부 원본 산출만으로도 규칙을 만족했다.
    assert counts["screenPromptLayerHitCount"] == 4 * N
    # screenExact 4발화 + screenNotHallucinated 1발화 = 5발화가 해소기 발동으로 표시됐다.
    assert counts["screenResolverOverrideCount"] == 5 * N
    # screenReask 1발화가 두 목록 밖 id 를 확정한 채로 남았다 — 위험한 실패.
    assert counts["screenOutOfListConfirmCount"] == 1 * N


# ─────────── [#344 라운드 2] 조건 전용 발화 categoryQueries 비움 불변식 ───────────

CONDITION_ONLY_CELLS = [cell for cell in CELLS if cell.utterance.group == "condition_only"]


def _condition_only_result(cell, *, category_legs: str) -> CellResult:  # noqa: ANN001
    return CellResult(
        cell_id=cell.cell_id,
        utterance_id=cell.utterance.utterance_id,
        context_id=cell.context.context_id,
        group="condition_only",
        samples=[
            _sample(cell.cell_id, index, intent="recommend", category_legs=category_legs)
            for index in range(N)
        ],
        attempts=N,
        filled=True,
    )


def test_condition_only_axis_scores_full_marks_when_legs_stay_empty() -> None:
    assert len(CONDITION_ONLY_CELLS) == 5, "조건 전용 셀이 없다 — 픽스처가 어긋났다"
    results = [_condition_only_result(cell, category_legs="") for cell in CONDITION_ONLY_CELLS]
    axes = score_all(results, ANCHORS, n=N)
    assert axes["conditionOnlyNoCategoryQuery"].numerator == 40
    assert axes["conditionOnlyNoCategoryQuery"].denominator == 40


def test_condition_only_axis_is_not_vacuous_a_single_leg_fails_the_sample() -> None:
    """[A-2 와 같은 이유] 엔진을 무조건 통과로 변이시켜도 이 축이 그걸 잡는가를 자문하고
    실제로 확인한다 — leg 을 **하나라도** 내면 그 표본은 불충족이어야 한다."""
    cell = CONDITION_ONLY_CELLS[0]
    result = _condition_only_result(cell, category_legs="음향가전 > 이어폰|무선 이어폰")
    axes = score_all([result], ANCHORS, n=N)
    assert axes["conditionOnlyNoCategoryQuery"].numerator == 0
    assert axes["conditionOnlyNoCategoryQuery"].denominator == N


def test_condition_only_axis_counts_only_its_own_bucket() -> None:
    """새 셀이 기존 축의 분모를 늘리면 안 된다 — 조건 전용 셀만 채점해도 기존 축은 0이어야 한다."""
    results = [_condition_only_result(cell, category_legs="") for cell in CONDITION_ONLY_CELLS]
    axes = score_all(results, ANCHORS, n=N)
    assert axes["mainIntent"].expected_denominator == 0
    assert axes["general"].expected_denominator == 0
    assert axes["categoryAction3Way"].expected_denominator == 0
    assert axes["screenResolution"].expected_denominator == 0


# ─────────── [#443] 상품군 명시 첫 턴 leg 산출(condition_only 의 반대 방향, #465) ───────────

NAMED_CATEGORY_CELLS = [cell for cell in CELLS if cell.utterance.group == "named_category"]


def _named_category_result(cell, *, category_legs: str, case: int = 2) -> CellResult:  # noqa: ANN001
    return CellResult(
        cell_id=cell.cell_id,
        utterance_id=cell.utterance.utterance_id,
        context_id=cell.context.context_id,
        group="named_category",
        samples=[
            _sample(cell.cell_id, index, intent="recommend", category_legs=category_legs, case=case)
            for index in range(N)
        ],
        attempts=N,
        filled=True,
    )


def test_named_category_axis_scores_full_marks_when_a_leg_is_present() -> None:
    assert len(NAMED_CATEGORY_CELLS) == 6, "상품군 명시 셀이 없다 — 픽스처가 어긋났다"
    results = [
        _named_category_result(cell, category_legs="음향가전 > 이어폰|과일")
        for cell in NAMED_CATEGORY_CELLS
    ]
    axes = score_all(results, ANCHORS, n=N)
    assert axes["namedCategoryHasLeg"].numerator == 48
    assert axes["namedCategoryHasLeg"].denominator == 48


def test_named_category_axis_distinguishes_zero_legs_from_a_present_leg() -> None:
    """[#443 이슈 실측 재현] leg 이 0개면(D1 no_legs) 그 표본은 불충족이어야 한다 — 이슈가 지목한
    실패(사용자가 상품군을 말했는데 파라미터 0개 payload)가 이 술어에 그대로 잡힌다.

    [A-2 와 같은 이유] leg 있음/없음 **양쪽을 같은 테스트 안에서** 단언해 술어가 상수 함수가
    아님을 못 박는다 — 두 방향을 한 테스트에 묶어야 "항상 True"·"항상 False" 두 변이가 모두 이
    테스트 하나로 잡힌다(따로 떨어뜨리면 한쪽만 재는 사본이 된다)."""
    cell = NAMED_CATEGORY_CELLS[0]
    without_leg = _named_category_result(cell, category_legs="")
    with_leg = _named_category_result(cell, category_legs="식품 > 과일|과일")
    scored_without = score_all([without_leg], ANCHORS, n=N)["namedCategoryHasLeg"]
    scored_with = score_all([with_leg], ANCHORS, n=N)["namedCategoryHasLeg"]
    assert scored_without.numerator == 0
    assert scored_without.denominator == N
    assert scored_with.numerator == N
    assert scored_with.denominator == N


def test_named_category_axis_passes_a_sample_with_two_legs() -> None:
    """leg 이 여러 개(#217 전개 산출)라도 "1개 이상" 술어는 여전히 정답이다."""
    cell = NAMED_CATEGORY_CELLS[0]
    result = _named_category_result(cell, category_legs="식품 > 과일|오렌지;식품 > 과일|귤")
    axes = score_all([result], ANCHORS, n=N)
    assert axes["namedCategoryHasLeg"].numerator == N


def test_named_category_axis_counts_only_its_own_bucket() -> None:
    """새 셀이 기존 축의 분모를 늘리면 안 된다 — 상품군 명시 셀만 채점해도 기존 축은 0이어야 한다."""
    results = [
        _named_category_result(cell, category_legs="과일|과일") for cell in NAMED_CATEGORY_CELLS
    ]
    axes = score_all(results, ANCHORS, n=N)
    assert axes["mainIntent"].expected_denominator == 0
    assert axes["general"].expected_denominator == 0
    assert axes["categoryAction3Way"].expected_denominator == 0
    assert axes["screenResolution"].expected_denominator == 0
    assert axes["conditionOnlyNoCategoryQuery"].expected_denominator == 0


def test_named_category_and_condition_only_axes_mirror_each_other_on_the_same_sample_shape() -> (
    None
):
    """[#465] 두 축이 정확히 거울이라는 사실을 코드로 못 박는다 — 같은 leg 값이 한쪽에서 정답이면
    반대쪽에서는 반드시 오답이어야 한다(같은 필드의 양쪽 끝이라 동시에 만점일 수 없다)."""
    empty_result = _named_category_result(NAMED_CATEGORY_CELLS[0], category_legs="")
    filled_result = _named_category_result(NAMED_CATEGORY_CELLS[0], category_legs="식품|과일")
    condition_cell = CONDITION_ONLY_CELLS[0]
    empty_condition = _condition_only_result(condition_cell, category_legs="")
    filled_condition = _condition_only_result(condition_cell, category_legs="식품|과일")

    assert score_all([empty_result], ANCHORS, n=N)["namedCategoryHasLeg"].numerator == 0
    assert score_all([filled_result], ANCHORS, n=N)["namedCategoryHasLeg"].numerator == N
    assert score_all([empty_condition], ANCHORS, n=N)["conditionOnlyNoCategoryQuery"].numerator == N
    assert (
        score_all([filled_condition], ANCHORS, n=N)["conditionOnlyNoCategoryQuery"].numerator == 0
    )


def test_diagnostics_count_named_category_empty_legs_and_case3() -> None:
    """[#443] 진단 카운터 2개 — 요인 분리(공백률 × case 분포 상관)의 계측기."""
    cell_a, cell_b = NAMED_CATEGORY_CELLS[0], NAMED_CATEGORY_CELLS[1]
    results = [
        # 4건은 leg 있음(case=2), 4건은 leg 없음(case=3) — #443 실측이 지목한 모양.
        CellResult(
            cell_id=cell_a.cell_id,
            utterance_id=cell_a.utterance.utterance_id,
            context_id=cell_a.context.context_id,
            group="named_category",
            samples=[
                _sample(cell_a.cell_id, index, category_legs="식품|과일", case=2)
                for index in range(4)
            ]
            + [_sample(cell_a.cell_id, index, category_legs="", case=3) for index in range(4, 8)],
            attempts=8,
            filled=True,
        ),
        # 다른 셀에서 leg 없이 case=2 로 나온 표본 — 공백이 case=3 판정과 항상 같이 가지는 않는다.
        CellResult(
            cell_id=cell_b.cell_id,
            utterance_id=cell_b.utterance.utterance_id,
            context_id=cell_b.context.context_id,
            group="named_category",
            samples=[
                _sample(cell_b.cell_id, index, category_legs="", case=2) for index in range(2)
            ],
            attempts=2,
            filled=True,
        ),
    ]
    counts = diagnostics(results, ANCHORS)
    assert counts["namedCategoryEmptyLegsCount"] == 4 + 2
    assert counts["namedCategoryCase3Count"] == 4
    # 다른 그룹의 진단 카운터를 오염시키지 않는다.
    assert counts["reaskProductEchoCount"] == 0
    assert counts["categoryScopeUnresolvedCount"] == 0


def test_named_category_diagnostics_do_not_leak_into_other_groups() -> None:
    """조건 전용 셀만 채점해도 상품군 명시 진단 카운터는 0이어야 한다 — 역방향 오염도 없다."""
    results = [_condition_only_result(cell, category_legs="") for cell in CONDITION_ONLY_CELLS]
    counts = diagnostics(results, ANCHORS)
    assert counts["namedCategoryEmptyLegsCount"] == 0
    assert counts["namedCategoryCase3Count"] == 0
