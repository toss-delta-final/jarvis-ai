from app.agents.buyer.recommendation.leg_head import suppress_generic_single_leg
from app.agents.buyer.recommendation.state import CategoryQuery, ProductSearchFilters


def test_suppresses_generic_single_leg_without_what_axis() -> None:
    assert suppress_generic_single_leg(
        [CategoryQuery(query="가성비 좋은 거", raw_category=None)],
        ProductSearchFilters(),
        enabled=True,
        generic_heads=frozenset({"거", "상품"}),
        condition_terms=frozenset({"가성비"}),
    ) == []


def test_keeps_product_heads_and_any_what_axis() -> None:
    for query in ("과일", "라면", "텐트", "텀블러", "유아용 물티슈"):
        assert suppress_generic_single_leg(
            [CategoryQuery(query=query, raw_category=None)], ProductSearchFilters(), enabled=True,
            generic_heads=frozenset({"거", "상품"}), condition_terms=frozenset({"가성비"}),
        )
    assert suppress_generic_single_leg(
        [CategoryQuery(query="가성비 좋은 거", raw_category=None)],
        ProductSearchFilters(brand=["x"]), enabled=True,
        generic_heads=frozenset({"거", "상품"}), condition_terms=frozenset({"가성비"}),
    )


def test_off_and_multiple_legs_are_byte_preserving() -> None:
    legs = [
        CategoryQuery(query="가성비 좋은 거", raw_category=None),
        CategoryQuery(query="라면", raw_category=None),
    ]
    assert suppress_generic_single_leg(
        legs, ProductSearchFilters(), enabled=False, generic_heads=frozenset({"거"}),
        condition_terms=frozenset({"가성비"}),
    ) is legs
    assert suppress_generic_single_leg(
        legs, ProductSearchFilters(), enabled=True, generic_heads=frozenset({"거"}),
        condition_terms=frozenset({"가성비"}),
    ) is legs
