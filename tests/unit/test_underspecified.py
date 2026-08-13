"""과소지정 발화 판정·후보 필터·되물음 문구 — 이슈 #336, `docs/specs/SPEC-UNDERSPECIFIED-336.md`.

`no_condition.py`(#162)의 상위 집합이라 판정 테스트는 그 파일의 패턴을 그대로 따른다.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.buyer.recommendation.attr_axis import strip_constraint_axes
from app.agents.buyer.recommendation.decompose import _FILTER_AXES
from app.agents.buyer.recommendation.no_condition import is_no_condition_turn
from app.agents.buyer.recommendation.state import CategoryQuery, RouteDecision
from app.agents.buyer.recommendation.underspecified import (
    _BLOCKING_FILTER_AXES,
    _CONSTRAINT_FILTER_AXES,
    _WHAT_FILTER_AXES,
    build_reask_question,
    is_underspecified_turn,
    within_price_range,
)
from app.schemas.spring import ProductSearchFilters, SpringProduct


def _settings(**overrides):
    defaults = dict(
        underspecified_reask_enabled=True,
        underspecified_notice="조건에 맞는 인기 상품으로 골라봤어요.",
        underspecified_reask_question="어떤 상품을 찾으시는지 조금 더 알려주시겠어요?",
        underspecified_reask_question_examples="{categories} 중에 찾으시는 게 있을까요?",
        underspecified_reask_examples_max=3,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _decision(*, semantic_query_is_fallback: bool = True, **filter_kwargs) -> RouteDecision:
    return RouteDecision(
        intent="recommend",
        filters=ProductSearchFilters(**filter_kwargs),
        semantic_query_is_fallback=semantic_query_is_fallback,
    )


# ─────────── D1 판정 — cases.json 셀 재현 (buy-under-0001~0008) ───────────


def test_flag_off_never_triggers() -> None:
    """buy-under-0008 — flag off 는 롤백 경로: 어떤 발화도 트리거하지 않는다."""
    assert (
        is_underspecified_turn(
            _decision(), prior=None, settings=_settings(underspecified_reask_enabled=False)
        )
        is False
    )


def test_bare_recommend_utterance_triggers() -> None:
    """buy-under-0002 — "아무거나 추천해줘": 축이 전부 비면 트리거된다."""
    assert is_underspecified_turn(_decision(), prior=None, settings=_settings()) is True


def test_total_budget_and_buy_all_alone_trigger() -> None:
    """buy-under-0001 — "5만원 이내로 아무거나 세트로": total_budget·buy_all 은 차단하지 않는다."""
    decision = _decision()
    decision.total_budget = 50000
    decision.buy_all = True
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is True


def test_price_max_alone_triggers() -> None:
    """buy-under-0003 — "5만원 이하로 아무거나": 제약-축(price_max)만 있어도 트리거된다."""
    decision = _decision(price_max=50000)
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is True


def test_price_min_alone_triggers() -> None:
    decision = _decision(price_min=10000)
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is True


def test_price_attr_axis_after_postprocess_triggers_reask() -> None:
    """가격 제약이 attrConditions 에서 제거되면 무엇을 찾는지 되물을 수 있다."""
    conditions, suppressed = strip_constraint_axes(
        {"가격": "5만원 이하"}, enabled=True, constraint_axes=frozenset({"가격"})
    )
    decision = _decision(attr_conditions=conditions)
    decision.attr_conditions_suppressed_axes = suppressed
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is True


def test_category_queries_block_trigger() -> None:
    """buy-under-0004 — "이어폰 추천해줘": 매핑 전 원시 카테고리 신호도 지목이다."""
    decision = _decision()
    decision.category_queries = [CategoryQuery(raw_category="이어폰", query="이어폰")]
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is False


def test_category_legs_block_trigger() -> None:
    decision = _decision()
    decision.category_legs = [("가전 > 이어폰/헤드폰", None)]
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is False


def test_brand_blocks_trigger() -> None:
    """buy-under-0005 — "삼성 제품 아무거나": what-축(brand)이 있으면 지목이다."""
    decision = _decision(brand=["삼성"])
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is False


def test_multiturn_prior_blocks_trigger() -> None:
    """buy-under-0006 — 멀티턴(prior 존재)은 트리거하지 않는다."""
    assert (
        is_underspecified_turn(_decision(), prior=ProductSearchFilters(), settings=_settings())
        is False
    )


def test_rating_min_blocks_trigger_conservatively() -> None:
    """buy-under-0007 — "평점 4 이상 아무거나": 보수 경계, rating_min 은 차단-축이다."""
    decision = _decision(rating_min=4.0)
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is False


@pytest.mark.parametrize(
    "filter_kwargs",
    [
        {"category": "가전 > 이어폰/헤드폰"},
        {"brand": ["나이키"]},
        {"keyword": "이어폰"},
        {"color": "네이비"},
        {"attr_conditions": {"소재": "린넨"}},
    ],
)
def test_any_what_axis_blocks_trigger(filter_kwargs: dict) -> None:
    assert (
        is_underspecified_turn(_decision(**filter_kwargs), prior=None, settings=_settings())
        is False
    )


def test_real_semantic_signal_blocks_trigger() -> None:
    decision = _decision(semantic_query="여름에 시원한", semantic_query_is_fallback=False)
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is False


@pytest.mark.parametrize(
    "decision_kwargs",
    [
        {"repurchase_products": ["작년에 산 그 신발"]},
        {"revert_categories": ["조미료"]},
    ],
)
def test_pointer_axes_block_trigger(decision_kwargs: dict) -> None:
    decision = _decision()
    for key, value in decision_kwargs.items():
        setattr(decision, key, value)
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is False


def test_scoped_to_previous_does_not_block_trigger() -> None:
    """[리뷰 F4] `scoped_to_previous` 는 보지 않는다 — 첫 턴엔 "직전 결과"가 존재하지 않아
    이 축이 공허하다(#162 도 같은 이유로 무시). 봤다면 `is_no_condition_turn`=True 인데
    `is_underspecified_turn`=False 인 반례가 생겨 상위집합 불변식이 깨진다.
    """
    decision = _decision()
    decision.scoped_to_previous = True
    assert is_no_condition_turn(decision, prior=None) is True
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is True


# ─────────── 불변식 — no_condition ⊂ underspecified ───────────


@pytest.mark.parametrize(
    "decision_factory",
    [
        lambda: _decision(),
        lambda: _decision(category="  ", keyword="\t", color=" "),  # 공백-only 는 빈 값
    ],
)
def test_no_condition_implies_underspecified_when_flag_on(decision_factory) -> None:
    """`is_no_condition_turn` 이 True 인 모든 턴은 flag on 에서 `is_underspecified_turn` 도 True.

    no_condition ⊂ underspecified 불변식(D1) — no_condition 이 요구하는 축 집합
    (`_FILTER_AXES` 전체 빈값)이 underspecified 의 요구(what-축·rating_min 빈값)보다
    엄격하므로, no_condition 트리거는 항상 underspecified 트리거를 함의해야 한다.
    """
    decision = decision_factory()
    assert is_no_condition_turn(decision, prior=None) is True
    assert is_underspecified_turn(decision, prior=None, settings=_settings()) is True


# ─────────── 축 드리프트 ───────────


def test_underspecified_axes_partition_filter_axes() -> None:
    """세 그룹의 합집합이 `decompose._FILTER_AXES` 와 정확히 일치하고 서로 겹치지 않는다."""
    assert set(_WHAT_FILTER_AXES) | set(_CONSTRAINT_FILTER_AXES) | set(
        _BLOCKING_FILTER_AXES
    ) == set(_FILTER_AXES)
    assert not (set(_WHAT_FILTER_AXES) & set(_CONSTRAINT_FILTER_AXES))
    assert not (set(_WHAT_FILTER_AXES) & set(_BLOCKING_FILTER_AXES))
    assert not (set(_CONSTRAINT_FILTER_AXES) & set(_BLOCKING_FILTER_AXES))


def test_route_decision_axes_are_all_classified() -> None:
    """`RouteDecision` 에 필드가 늘면 분류를 강제한다(#162 의 같은 이름 테스트와 동형)."""
    blocking = {
        "filters",
        "category_legs",
        "category_queries",
        "semantic_query_is_fallback",
        "repurchase_products",
        "revert_categories",
    }
    constraint_not_blocking = {"total_budget", "buy_all"}
    # [리뷰 F4] `scoped_to_previous` 는 "무관" — 첫 턴 한정 판정이라 직전 결과 지칭이 공허하다.
    # [이슈 #434 라운드2] `category_legs_restored` 도 `category_expanded` 와 동형 — True 면
    # 정의상 `category_legs` 가 이미 채워져 있어(blocking) 중복 계상이라 여기가 맞다.
    # [#443] `category_leg_injected` 는 **진단 플래그**라 판정에 영향이 없다 — 사전 기반 보강이
    # 실제로 판정을 움직이는 경로는 `category_queries`(위 blocking)를 채우는 것이고, 이 불리언은
    # "모델이 냈나 보강이 채웠나"를 산출물에서 가르기 위한 표식일 뿐이다. 그래서 no_effect 다.
    no_effect = {
        "intent",
        "case",
        "reply",
        "cart",
        "screen_reference",  # 장바구니 화면 지목이라 추천 조건 판정과 무관
        "category_expanded",
        "category_legs_restored",
        "scoped_to_previous",
        "category_leg_injected",
        "attr_conditions_suppressed_axes",
    }

    assert {f.name for f in fields(RouteDecision)} == (
        blocking | constraint_not_blocking | no_effect
    )
    assert not (blocking & constraint_not_blocking)
    assert not (blocking & no_effect)
    assert not (constraint_not_blocking & no_effect)


# ─────────── D2 within_price_range ───────────


def _product(product_id: int, price: int | None) -> SpringProduct:
    return SpringProduct(product_id=product_id, name=f"상품{product_id}", price=price)


def test_within_price_range_drops_priceless_products_when_constrained() -> None:
    """입증 규약 — 가격 조건이 하나라도 걸리면 가격 모르는 상품은 뺀다."""
    products = [_product(1, 10000), _product(2, None), _product(3, 90000)]
    assert [p.product_id for p in within_price_range(products, None, 50000)] == [1]


def test_within_price_range_no_constraint_keeps_priceless_products() -> None:
    """가격 조건이 둘 다 없으면 원본 그대로(사본) — 가격 모르는 상품도 유지."""
    products = [_product(1, 10000), _product(2, None)]
    result = within_price_range(products, None, None)
    assert [p.product_id for p in result] == [1, 2]
    assert result is not products


def test_within_price_range_boundaries_are_inclusive() -> None:
    products = [_product(1, 50000), _product(2, 50001), _product(3, 9999), _product(4, 10000)]
    result = within_price_range(products, 10000, 50000)
    assert [p.product_id for p in result] == [1, 4]


def test_within_price_range_preserves_order() -> None:
    """stable — BE 인기 순위·productId tiebreak 를 재정렬하지 않는다."""
    products = [_product(5, 20000), _product(2, 10000), _product(9, 30000)]
    result = within_price_range(products, 5000, 40000)
    assert [p.product_id for p in result] == [5, 2, 9]


def test_within_price_range_min_only() -> None:
    products = [_product(1, 4000), _product(2, 6000)]
    assert [p.product_id for p in within_price_range(products, 5000, None)] == [2]


def test_within_price_range_max_only() -> None:
    products = [_product(1, 4000), _product(2, 6000)]
    assert [p.product_id for p in within_price_range(products, None, 5000)] == [1]


def test_within_price_range_zero_bounds_are_blank_like_is_blank() -> None:
    """[리뷰 F1] `price_max=0`/`price_min=0` 은 `_is_blank` 규약대로 "미지정" — 유효 경계가
    아니다. 유효 경계로 취급하면 flag off 의 no_condition 경로에서도 회귀한다(F1 재현 근거).
    """
    products = [_product(1, 10000), _product(2, None), _product(3, 90000)]
    result = within_price_range(products, 0, 0)
    assert [p.product_id for p in result] == [1, 2, 3]  # 사본 그대로 — 아무것도 걸리지 않는다


def test_within_price_range_zero_max_does_not_zero_out_positive_prices() -> None:
    """`price_max=0` 하나만 와도(price_min 은 실제 하한) 0 은 무시하고 하한만 적용한다."""
    products = [_product(1, 4000), _product(2, 6000)]
    assert [p.product_id for p in within_price_range(products, 5000, 0)] == [2]


# ─────────── D3 build_reask_question ───────────


def _cat_product(product_id: int, category: str | None) -> SpringProduct:
    return SpringProduct(product_id=product_id, name=f"상품{product_id}", category=category)


def test_build_reask_question_empty_candidates_is_generic() -> None:
    assert build_reask_question([], _settings()) == "어떤 상품을 찾으시는지 조금 더 알려주시겠어요?"


def test_build_reask_question_dedups_preserving_order() -> None:
    products = [
        _cat_product(1, "이어폰"),
        _cat_product(2, "이어폰"),
        _cat_product(3, "노트북"),
    ]
    question = build_reask_question(products, _settings())
    assert question == "이어폰 · 노트북 중에 찾으시는 게 있을까요?"


def test_build_reask_question_caps_examples() -> None:
    products = [_cat_product(i, f"카테고리{i}") for i in range(1, 6)]
    question = build_reask_question(products, _settings(underspecified_reask_examples_max=2))
    assert question == "카테고리1 · 카테고리2 중에 찾으시는 게 있을까요?"


def test_build_reask_question_zero_cap_is_generic() -> None:
    products = [_cat_product(1, "이어폰")]
    question = build_reask_question(products, _settings(underspecified_reask_examples_max=0))
    assert question == "어떤 상품을 찾으시는지 조금 더 알려주시겠어요?"


def test_build_reask_question_blank_categories_are_skipped() -> None:
    products = [_cat_product(1, None), _cat_product(2, "  "), _cat_product(3, "이어폰")]
    question = build_reask_question(products, _settings())
    assert question == "이어폰 중에 찾으시는 게 있을까요?"


def test_build_reask_question_missing_placeholder_falls_back_to_generic() -> None:
    products = [_cat_product(1, "이어폰")]
    settings = _settings(underspecified_reask_question_examples="가격도 알려주세요")
    assert (
        build_reask_question(products, settings) == "어떤 상품을 찾으시는지 조금 더 알려주시겠어요?"
    )


def test_build_reask_question_format_exception_falls_back_to_generic() -> None:
    products = [_cat_product(1, "이어폰")]
    settings = _settings(underspecified_reask_question_examples="{categories} {unknown_key}")
    assert (
        build_reask_question(products, settings) == "어떤 상품을 찾으시는지 조금 더 알려주시겠어요?"
    )


# ─────────── cases.json 앵커 (#328 공통 규약, evals/underspecified_cases/README.md) ───────────

_CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "underspecified_cases" / "cases.json"


def _load_cases() -> list[dict]:
    return json.loads(_CASES_PATH.read_text(encoding="utf-8"))


def _decision_from_fixture(fixture: dict) -> RouteDecision:
    kwargs = dict(fixture)
    filter_kwargs = kwargs.pop("filters", {})
    category_queries = kwargs.pop("category_queries", None)
    if category_queries is not None:
        kwargs["category_queries"] = [CategoryQuery(**cq) for cq in category_queries]
    return RouteDecision(
        intent="recommend",
        filters=ProductSearchFilters(**filter_kwargs),
        **kwargs,
    )


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["caseId"])
def test_cases_json_anchor(case: dict) -> None:
    """`evals/underspecified_cases/cases.json` 의 각 셀이 실제 판정과 일치한다."""
    decision = _decision_from_fixture(case["decomposeFixture"])
    prior = ProductSearchFilters() if case.get("priorExists") else None
    settings = _settings(underspecified_reask_enabled=case.get("flagEnabled", True))
    assert is_underspecified_turn(decision, prior, settings) is case["expected"]["reask"], case[
        "caseId"
    ]
