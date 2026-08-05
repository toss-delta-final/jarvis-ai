"""조건 없는 발화 트리거 판정 — 이슈 #162, api-spec §4.12.

이 판정이 느슨하면 **사용자가 말한 조건을 버리고** 인기상품을 주게 되고, 빡빡하면 이슈가
고치려는 무필터 I-1 전량 호출(7,245건·13.33MB)이 그대로 남는다. 경계를 테스트로 고정한다.
"""

from __future__ import annotations

import pytest

from app.agents.buyer.recommendation.no_condition import is_no_condition_turn
from app.agents.buyer.recommendation.state import RouteDecision
from app.schemas.spring import ProductSearchFilters


def _decision(**filter_kwargs) -> RouteDecision:
    return RouteDecision(intent="recommend", filters=ProductSearchFilters(**filter_kwargs))


def test_bare_recommend_utterance_triggers() -> None:
    """"아무거나 추천해줘" — 조건 축이 전부 비고 첫 턴이면 트리거된다."""
    assert is_no_condition_turn(_decision(), prior=None) is True


def test_semantic_query_blocks_trigger() -> None:
    """**"여름에 시원한 거 추천해줘"** — filters 가 전부 null 이어도 트리거되면 안 된다.

    이슈 완료 조건에 명시된 회귀 항목이다. `semanticQuery` 는 "정형 제약을 제외한 벡터 검색용
    자연어"라 필터로 떨어지지 않는 의미가 여기 남는다. 카테고리 추측이 실패해도 이 값은 살아
    있으므로, 이걸 무시하면 **사용자 의도를 통째로 버리고** 인기상품을 주게 된다.
    """
    assert is_no_condition_turn(_decision(semantic_query="여름에 시원한"), prior=None) is False


def test_category_legs_block_trigger() -> None:
    """카테고리가 매핑됐으면 조건이 있는 턴이다(멀티턴 승계도 이 경로로 들어온다).

    `_carry_prior_category`(buyer/graph.py)가 직전 턴 카테고리를 `category_legs` 로 승계하므로,
    이 검사 하나가 "이어폰 추천해줘 → 더 저렴한 걸로" 같은 리파인 턴까지 함께 막는다.
    """
    decision = _decision()
    decision.category_legs = [("가전 > 이어폰/헤드폰", None)]
    assert is_no_condition_turn(decision, prior=None) is False


@pytest.mark.parametrize(
    "filter_kwargs",
    [
        {"category": "가전 > 이어폰/헤드폰"},
        {"price_max": 50000},
        {"price_min": 10000},
        {"brand": ["나이키"]},
        {"rating_min": 4.0},
        {"keyword": "이어폰"},
        {"color": "네이비"},
        {"attr_conditions": {"소재": "린넨"}},
    ],
)
def test_any_single_condition_axis_blocks_trigger(filter_kwargs: dict) -> None:
    """사용자 조건 축이 **하나라도** 있으면 조건 있는 턴이다 — 축을 빠뜨리면 의도가 버려진다."""
    assert is_no_condition_turn(_decision(**filter_kwargs), prior=None) is False


def test_multiturn_prior_blocks_trigger() -> None:
    """직전 턴 상태(prior)가 있으면 첫 턴이 아니라 트리거하지 않는다.

    이슈 완료 조건 명시 항목. 멀티턴의 "리파인 / 칩 제거 / 카테고리-무관 리셋" 세 의도는 아직
    구분되지 않으므로(#84) 이 이슈는 **첫 턴에 한정**한다. prior 자체가 비어 있어도 마찬가지다 —
    빈 prior 를 트리거로 인정하면 #84 가 해소되기 전에 멀티턴으로 새는 구멍이 된다.
    """
    assert is_no_condition_turn(_decision(), prior=ProductSearchFilters()) is False


def test_whitespace_only_values_are_treated_as_empty() -> None:
    """공백-only 는 조건이 아니다 — `if x:` 는 ''(falsy)만 막고 ' '(truthy)는 통과시킨다.

    LLM 산출값이라 신뢰 경계 밖이고, 같은 함정을 `_search_query_params` 가 이미 밟았다
    (#127 리뷰 — 공백-only 가 Spring 에 빈값으로 나갔다).
    """
    decision = _decision(category="  ", keyword="\t", semantic_query="\n", color=" ")
    assert is_no_condition_turn(decision, prior=None) is True
