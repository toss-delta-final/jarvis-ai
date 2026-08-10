from app.agents.buyer.recommendation.state import CategoryQuery, RouteDecision
from app.schemas.spring import ProductSearchFilters
from evals.underspecified_probe.runner import dedicated_suppressed_decision


def _decision(*, semantic: str, fallback: bool) -> RouteDecision:
    return RouteDecision(
        intent="recommend", filters=ProductSearchFilters(semantic_query=semantic),
        category_queries=[CategoryQuery(raw_category=None, query="가성비 좋은 거")],
        semantic_query_is_fallback=fallback,
    )


def test_dedicated_false_leaves_original_untouched() -> None:
    original = _decision(semantic="다른 의미", fallback=False)
    assert original.category_queries and original.semantic_query_is_fallback is False


def test_dedicated_suppression_flips_only_cat_signal_fallback() -> None:
    clone, ambiguous = dedicated_suppressed_decision(_decision(semantic="가성비 좋은 거", fallback=False))
    assert clone.category_queries == []
    assert clone.semantic_query_is_fallback is True
    assert ambiguous is True


def test_dedicated_suppression_keeps_llm_semantic_signal() -> None:
    clone, ambiguous = dedicated_suppressed_decision(_decision(semantic="여름 선물", fallback=False))
    assert clone.category_queries == []
    assert clone.semantic_query_is_fallback is False
    assert ambiguous is False


def test_fallback_input_stays_fallback_after_suppression() -> None:
    clone, _ = dedicated_suppressed_decision(_decision(semantic="발화", fallback=True))
    assert clone.category_queries == []
    assert clone.semantic_query_is_fallback is True
