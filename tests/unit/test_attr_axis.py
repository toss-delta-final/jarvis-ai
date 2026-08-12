"""attrConditions 제약 축 후처리 순수 함수 테스트."""

from __future__ import annotations

import pytest

from app.agents.buyer.recommendation.attr_axis import strip_constraint_axes
from app.core.config import Settings


CONSTRAINT_AXES = frozenset(
    {
        "가격",
        "가격대",
        "예산",
        "금액",
        "평점",
        "별점",
        "수량",
        "개수",
        "price",
        "price_range",
        "pricerange",
        "budget",
        "amount",
        "cost",
        "rating",
        "stars",
        "quantity",
        "count",
    }
)


def test_constraint_only_conditions_are_removed() -> None:
    """제약 축만 있으면 attrConditions 전체가 비워지고 제거 축을 기록한다."""
    assert strip_constraint_axes(
        {"가격": "5만원 이하"}, enabled=True, constraint_axes=CONSTRAINT_AXES
    ) == (None, ["가격"])


def test_mixed_conditions_keep_product_attribute() -> None:
    """상품 속성은 유지하고 제약 축만 제거한다."""
    assert strip_constraint_axes(
        {"소재": "린넨", "가격": "5만원 이하"},
        enabled=True,
        constraint_axes=CONSTRAINT_AXES,
    ) == ({"소재": "린넨"}, ["가격"])


@pytest.mark.parametrize(
    "conditions",
    [
        {"용량": "500ml"},
        {"무게": "2kg"},
        {"소재": "린넨"},
        {"핏": "오버핏"},
        {"방수": "true"},
    ],
)
def test_product_attribute_names_are_preserved(conditions: dict[str, str]) -> None:
    """정당한 상품 속성 축은 제약 축 어휘에 포함되지 않아 보존된다."""
    assert strip_constraint_axes(conditions, enabled=True, constraint_axes=CONSTRAINT_AXES) == (
        conditions,
        [],
    )


def test_disabled_suppression_returns_input_unchanged() -> None:
    """스위치가 꺼지면 입력과 제거 진단을 그대로 보존한다."""
    conditions = {"가격": "5만원 이하"}
    cleaned, suppressed = strip_constraint_axes(
        conditions, enabled=False, constraint_axes=CONSTRAINT_AXES
    )
    assert cleaned is conditions
    assert suppressed == []


def test_substring_axis_is_not_suppressed() -> None:
    """제약 축 이름의 부분문자열인 다른 축은 오발동 없이 보존한다."""
    conditions = {"가격대비만족도": "높음"}
    assert strip_constraint_axes(conditions, enabled=True, constraint_axes=CONSTRAINT_AXES) == (
        conditions,
        [],
    )


def test_axis_matching_strips_key_before_exact_comparison() -> None:
    """축 이름 양끝 공백만 제거하고 정확히 비교한다."""
    assert strip_constraint_axes(
        {" 가격 ": "5만원 이하", " 소재 ": "린넨"},
        enabled=True,
        constraint_axes=CONSTRAINT_AXES,
    ) == ({"소재": "린넨"}, ["가격"])


@pytest.mark.parametrize("axis", ["price", "Price", "PRICE"])
def test_english_price_axes_are_suppressed_case_insensitively(axis: str) -> None:
    """영문 가격 축은 대소문자와 무관하게 제거한다."""
    assert strip_constraint_axes(
        {axis: "20000-50000"}, enabled=True, constraint_axes=CONSTRAINT_AXES
    ) == (None, [axis])


@pytest.mark.parametrize("axis", ["color", "size", "weight", "capacity", "용량", "중량", "크기"])
def test_product_attribute_axes_are_preserved(axis: str) -> None:
    """정당한 상품 속성 축은 영문 어휘 확장에도 보존한다."""
    assert strip_constraint_axes(
        {axis: "black"}, enabled=True, constraint_axes=CONSTRAINT_AXES
    ) == ({axis: "black"}, [])


def test_retained_axis_spelling_is_preserved_after_casefold_comparison() -> None:
    """비교 정규화는 보존하는 원문 축 표기를 바꾸지 않는다."""
    assert strip_constraint_axes(
        {"Color": "black", "PRICE": "20000-50000"},
        enabled=True,
        constraint_axes=CONSTRAINT_AXES,
    ) == ({"Color": "black"}, ["PRICE"])


def test_settings_constraint_vocabulary_contains_only_requested_english_axes() -> None:
    """운영 설정에는 가격·예산 등 제약 대응어만 추가한다."""
    axes = set(Settings(_env_file=None).attr_condition_constraint_axes)
    assert {
        "price",
        "price_range",
        "pricerange",
        "budget",
        "amount",
        "cost",
        "rating",
        "stars",
        "quantity",
        "count",
    } <= axes
    assert not axes.intersection({"color", "size", "weight", "capacity"})


@pytest.mark.parametrize(
    ("field_name", "variants"),
    [
        ("price_max", ("price_max", "priceMax", "pricemax", "PRICE_MAX", "PRICEMAX")),
        ("price_min", ("price_min", "priceMin", "pricemin", "PRICE_MIN", "PRICEMIN")),
        ("max_price", ("max_price", "maxPrice", "maxprice", "MAX_PRICE", "MAXPRICE")),
        ("min_price", ("min_price", "minPrice", "minprice", "MIN_PRICE", "MINPRICE")),
        ("rating_min", ("rating_min", "ratingMin", "ratingmin", "RATING_MIN", "RATINGMIN")),
        ("min_rating", ("min_rating", "minRating", "minrating", "MIN_RATING", "MINRATING")),
    ],
)
def test_numeric_product_filter_axis_variants_are_guarded(
    field_name: str, variants: tuple[str, ...]
) -> None:
    """ProductSearchFilters 수치 범위 필드는 부등호를 표현할 수 없어 모든 표기를 억제한다.

    color·brand 도 ProductSearchFilters 필드지만 정당한 상품 속성이므로 이 드리프트 가드의
    대상이 아니다.
    """
    axes = frozenset(Settings(_env_file=None).attr_condition_constraint_axes)
    assert field_name in axes
    for axis in variants:
        assert strip_constraint_axes(
            {axis: "100-200"}, enabled=True, constraint_axes=axes
        ) == (None, [axis])


def test_color_and_brand_are_not_numeric_constraint_axes() -> None:
    """color·brand 는 정당한 상품 속성이므로 수치 제약 어휘 드리프트 가드에 넣지 않는다."""
    axes = frozenset(Settings(_env_file=None).attr_condition_constraint_axes)
    assert strip_constraint_axes(
        {"color": "black", "brand": "Acme"}, enabled=True, constraint_axes=axes
    ) == ({"color": "black", "brand": "Acme"}, [])
