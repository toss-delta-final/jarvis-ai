from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.llm import LLMError
from app.schemas.spring import ProductSearchFilters


class _LLM:
    def __init__(self, raw: str | Exception) -> None:
        self.raw = raw
        self.calls: list[tuple[str, str, str, int]] = []

    async def complete(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024, json_output: bool = True
    ) -> str:
        self.calls.append((system, user, tier, max_tokens))
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw


def _settings(**overrides):  # noqa: ANN001
    return SimpleNamespace(
        llm_provider="openai",
        openai_fast_model_id="gpt-5-nano",
        openai_smart_model_id="gpt-5.6-luna",
        underspecified_classifier_tier="smart",
        underspecified_classifier_max_tokens=48,
        **overrides,
    )


@pytest.mark.asyncio
async def test_classifier_returns_true_only_for_boolean_true() -> None:
    from app.agents.buyer.recommendation.underspecified_classifier import (
        classify_underspecified,
    )

    llm = _LLM('{"underspecified": true}')

    assert (
        await classify_underspecified(llm, message="5만원 이하 아무거나", settings=_settings())
        is True
    )
    assert llm.calls[0][1] == "사용자 발화: 5만원 이하 아무거나"
    assert llm.calls[0][2:] == ("smart", 48)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ['{"underspecified": false}', "{}", '{"underspecified": "true"}'])
async def test_classifier_keeps_specific_or_unparseable_answers_out(raw: str) -> None:
    from app.agents.buyer.recommendation.underspecified_classifier import (
        classify_underspecified,
    )

    assert (
        await classify_underspecified(_LLM(raw), message="무선 이어폰", settings=_settings())
        is False
    )


@pytest.mark.asyncio
async def test_classifier_degrades_to_none_on_provider_failure() -> None:
    from app.agents.buyer.recommendation.underspecified_classifier import (
        classify_underspecified,
    )

    assert (
        await classify_underspecified(
            _LLM(LLMError("provider unavailable")), message="아무거나", settings=_settings()
        )
        is None
    )


def test_low_information_gate_is_not_the_classifier() -> None:
    """명백한 상품/목적 요청에는 #463 smart 보조 호출 비용을 붙이지 않는다."""
    from app.agents.buyer.recommendation.underspecified_classifier import (
        could_be_underspecified_message,
    )

    assert could_be_underspecified_message("5만원 이하로 아무거나 추천해줘")
    assert could_be_underspecified_message("그냥 추천해줘")
    assert not could_be_underspecified_message("평점 4 이상 아무거나")
    assert not could_be_underspecified_message("무선 이어폰 추천해줘")
    assert not could_be_underspecified_message("유럽여행 준비물 추천해줘")


def test_classifier_settings_default_to_a_narrow_smart_call() -> None:
    settings = Settings(_env_file=None)

    assert settings.underspecified_classifier_enabled is True
    assert settings.underspecified_classifier_tier == "smart"
    assert settings.underspecified_classifier_max_tokens == 48


def test_true_verdict_restores_the_430_fallback_shape() -> None:
    from app.agents.buyer.recommendation.state import CategoryQuery, RouteDecision
    from app.agents.buyer.recommendation.underspecified_classifier import (
        apply_underspecified_classification,
    )

    decision = RouteDecision(
        intent="recommend",
        filters=ProductSearchFilters(
            semantic_query="지어낸 상품", brand=["지어낸 브랜드"], color="검정"
        ),
        case=1,
        category_queries=[CategoryQuery(raw_category="가전", query="지어낸 상품")],
        category_legs=[("가전", "지어낸 상품")],
        category_leg_injected=True,
        semantic_query_is_fallback=False,
    )

    assert apply_underspecified_classification(
        decision, message="5만원 이하 아무거나", verdict=True
    )
    assert decision.filters.semantic_query == "5만원 이하 아무거나"
    assert decision.filters.brand is decision.filters.color is None
    assert decision.category_queries == decision.category_legs == []
    assert decision.category_leg_injected is False
    assert decision.case == 2 and decision.semantic_query_is_fallback is True


@pytest.mark.parametrize("verdict", [False, None])
def test_false_or_unavailable_verdict_does_not_mutate_decision(verdict: bool | None) -> None:
    from app.agents.buyer.recommendation.state import RouteDecision
    from app.agents.buyer.recommendation.underspecified_classifier import (
        apply_underspecified_classification,
    )

    decision = RouteDecision(
        intent="recommend", filters=ProductSearchFilters(semantic_query="무선 이어폰")
    )
    assert not apply_underspecified_classification(decision, message="무선 이어폰", verdict=verdict)
    assert decision.filters.semantic_query == "무선 이어폰"
