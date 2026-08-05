"""조건 없는 발화 트리거 판정 — 이슈 #162, api-spec §4.17.

이 판정이 느슨하면 **사용자가 말한 조건을 버리고** 인기상품을 주게 되고, 빡빡하면 이슈가
고치려는 무필터 I-1 전량 호출(7,245건·13.33MB)이 그대로 남는다. 경계를 테스트로 고정한다.
"""

from __future__ import annotations

import json

import pytest

from app.agents.buyer.recommendation.decompose import decompose
from app.agents.buyer.recommendation.no_condition import (
    has_total_budget,
    is_no_condition_turn,
    within_budget,
)
from app.agents.buyer.recommendation.state import RouteDecision
from app.schemas.spring import ProductSearchFilters, SpringProduct


def _decision(*, semantic_query_is_fallback: bool = True, **filter_kwargs) -> RouteDecision:
    """조건 없는 턴의 기본형 — 필요한 축만 채워 "조건 있음"으로 만든다.

    `semantic_query_is_fallback=True` 가 기본인 이유: 실제 decompose 는 신호가 없을 때
    `semantic_query` 에 **발화 원문**을 넣으므로(값은 항상 참) 이 플래그가 "의미 신호 없음"의
    유일한 표현이다. 아래 `test_decompose_marks_...` 가 그 실제 경로를 따로 검증한다.
    """
    return RouteDecision(
        intent="recommend",
        filters=ProductSearchFilters(**filter_kwargs),
        semantic_query_is_fallback=semantic_query_is_fallback,
    )


def test_bare_recommend_utterance_triggers() -> None:
    """"아무거나 추천해줘" — 조건 축이 전부 비고 첫 턴이면 트리거된다."""
    assert is_no_condition_turn(_decision(), prior=None) is True


def test_real_semantic_signal_blocks_trigger() -> None:
    """**"여름에 시원한 거 추천해줘"** — filters 가 전부 null 이어도 트리거되면 안 된다.

    이슈 완료 조건에 명시된 회귀 항목이다. `semanticQuery` 는 "정형 제약을 제외한 벡터 검색용
    자연어"라 필터로 떨어지지 않는 의미가 여기 남는다. 카테고리 추측이 실패해도 이 값은 살아
    있으므로, 이걸 무시하면 **사용자 의도를 통째로 버리고** 인기상품을 주게 된다.
    """
    decision = _decision(semantic_query="여름에 시원한", semantic_query_is_fallback=False)
    assert is_no_condition_turn(decision, prior=None) is False


def test_utterance_fallback_semantic_query_does_not_block_trigger() -> None:
    """원문 폴백으로 채워진 `semantic_query` 는 조건이 아니다 — **이 판정의 핵심**.

    `is_no_condition_turn` 을 "semantic_query 가 비었는가"로 짜면 **영영 트리거되지 않는다.**
    decompose 가 `llm_sq or cat_signal or prior_sq or query` 로 채워(decompose.py) 아무 신호가
    없어도 발화 원문이 들어가기 때문이다. 값이 아니라 출처로 판정해야 한다.
    """
    decision = _decision(semantic_query="아무거나 추천해줘", semantic_query_is_fallback=True)
    assert is_no_condition_turn(decision, prior=None) is True


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
    decision = _decision(category="  ", keyword="\t", color=" ")
    assert is_no_condition_turn(decision, prior=None) is True


def test_condition_axes_track_decompose_filter_axes() -> None:
    """판정이 쓰는 축 목록은 decompose 의 `_FILTER_AXES` **그 자체**여야 한다.

    사본을 두면 새 하드필터가 생겼을 때 한쪽만 늘어나 조건 있는 턴이 조용히 "조건 없음"으로
    새어 들어온다. `_FILTER_AXES` 는 `ProductSearchFilters` 전체와 대조하는 드리프트 테스트가
    이미 지키고 있으므로(tests/unit/test_decompose.py) 거기 얹는다.
    """
    from app.agents.buyer.recommendation import no_condition
    from app.agents.buyer.recommendation.decompose import _FILTER_AXES

    assert no_condition._FILTER_AXES is _FILTER_AXES


class _RawLLM:
    """지정 raw JSON 을 fast tier 에서 돌려주는 최소 LLM (tests/unit/test_decompose.py 와 동형)."""

    def __init__(self, raw: str) -> None:
        self._raw = raw

    async def complete(
        self, *, system: str, user: str, tier: str, max_tokens: int = 1024, json_output: bool = True
    ) -> str:
        return self._raw

    async def stream(self, *, system: str, user: str, tier: str, max_tokens: int = 1024):
        yield "x"


async def _decompose_raw(payload: dict, utterance: str):
    return await decompose(
        _RawLLM(json.dumps(payload, ensure_ascii=False)),
        query=utterance,
        prior_filters=None,
        profile_summary=None,
        tier="fast",
    )


async def test_decompose_marks_utterance_fallback_for_bare_request() -> None:
    """**실제 decompose 경로** — 신호 없는 발화는 `semantic_query_is_fallback=True` 로 나온다.

    이 테스트가 없으면 판정 함수가 단위 테스트에서만 통과하고 프로덕션에서는 한 번도 발동하지
    않는 상태를 못 잡는다(구현 중 실제로 그 상태였다). 필터를 직접 만들지 않고 LLM 산출
    JSON 에서 출발하는 것이 요점이다.
    """
    decision = await _decompose_raw(
        {"intent": "recommend", "reply": "", "categoryQueries": [], "filters": {}},
        "아무거나 추천해줘",
    )

    assert decision.semantic_query_is_fallback is True
    assert decision.filters.semantic_query == "아무거나 추천해줘"  # 원문 폴백이 실제로 들어간다
    assert is_no_condition_turn(decision, prior=None) is True


async def test_decompose_does_not_mark_fallback_when_llm_gives_semantic_query() -> None:
    """LLM 이 의미를 냈으면 폴백이 아니다 — "여름에 시원한 거"가 트리거되지 않는 실제 경로."""
    decision = await _decompose_raw(
        {
            "intent": "recommend",
            "reply": "",
            "semanticQuery": "여름에 시원한 옷",
            "categoryQueries": [],
            "filters": {},
        },
        "여름에 시원한 거 추천해줘",
    )

    assert decision.semantic_query_is_fallback is False
    assert is_no_condition_turn(decision, prior=None) is False


# ─────────── RouteDecision 에 직접 실리는 축 (PR #311 리뷰) ───────────


def _decision_with(**decision_kwargs) -> RouteDecision:
    return RouteDecision(
        intent="recommend",
        filters=ProductSearchFilters(),
        semantic_query_is_fallback=True,
        **decision_kwargs,
    )


@pytest.mark.parametrize(
    "decision_kwargs",
    [
        {"repurchase_products": ["무선 이어폰"]},
        {"revert_categories": ["조미료"]},
    ],
)
def test_recent_purchase_pointers_block_trigger(decision_kwargs: dict) -> None:
    """재구매 지목·되돌리기는 사용자가 **무언가를 명시적으로 지목한** 발화라 조건이다.

    오늘은 그런 발화면 decompose 가 `semanticQuery` 를 채워 출처 검사에서도 걸리지만,
    그건 우연이라 여기서 못박는다.
    """
    assert is_no_condition_turn(_decision_with(**decision_kwargs), prior=None) is False


# ─────────── 총액 예산 — 판정은 통과시키고 **후보 확보 방식**을 가른다 ───────────


@pytest.mark.parametrize(
    "decision_kwargs",
    [
        {"total_budget": 50000},
        {"buy_all": True},
        {"buy_all": True, "total_budget": 50000},
    ],
)
def test_budget_intent_does_not_block_trigger(decision_kwargs: dict) -> None:
    """**"총 5만원 있어 아무거나 추천해줘"는 여전히 조건 없는 턴으로 본다.**

    여기서 막으면 그 턴이 무필터 I-1(실측 7,245건·13.33MB)로 되돌아가는데, 그래 봐야 예산이
    반영되지 않는다 — `build_budget_sets`(#60)는 니즈(leg) 2개 이상일 때만 돌고
    (`split_by_need`), 조건 없는 턴은 정의상 leg 가 비어 있다. 비용만 늘고 얻는 게 없다.
    """
    assert is_no_condition_turn(_decision_with(**decision_kwargs), prior=None) is True


@pytest.mark.parametrize(
    ("decision_kwargs", "expected"),
    [
        ({}, False),
        ({"total_budget": 50000}, True),
        ({"buy_all": True, "total_budget": 50000}, True),
        # 예산 없는 "다 사줘" 는 **취향 경로를 막지 않는다** — 확인할 가격이 애초에 없고,
        # leg 가 없어 어느 경로로 가도 PICK_ONE 이라 결과도 같다. 평소대로 취향·인기 최적
        # 상품을 추천한다.
        ({"buy_all": True}, False),
    ],
)
def test_has_total_budget(decision_kwargs: dict, expected: bool) -> None:
    """총액 예산 신호 — 취향 경로 차단과 예산 필터의 스위치다."""
    assert has_total_budget(_decision_with(**decision_kwargs)) is expected


def test_within_budget_drops_priceless_products() -> None:
    """가격을 **확인할 수 없는** 후보는 뺀다 — 예산은 반증이 아니라 입증이 필요하다.

    남겨 두면 "5만원이라 했는데 8만원짜리를 줬다"가 된다. `build_budget_sets` 가 가격 없는
    후보를 `unavailable_legs` 로 빼는 것과 같은 규약이다.
    """
    products = [
        SpringProduct(product_id=1, name="싼 것", price=30000),
        SpringProduct(product_id=2, name="비싼 것", price=80000),
        SpringProduct(product_id=3, name="가격 모름", price=None),
        SpringProduct(product_id=4, name="딱 맞는 것", price=50000),  # 경계 포함
    ]
    assert [p.product_id for p in within_budget(products, 50000)] == [1, 4]


def test_per_item_price_cap_still_blocks_trigger() -> None:
    """대조군 — "5만원 이하 아무거나"(상품당 상한)는 종전대로 `_FILTER_AXES` 가 잡는다."""
    decision = RouteDecision(
        intent="recommend",
        filters=ProductSearchFilters(price_max=50000),
        semantic_query_is_fallback=True,
    )
    assert is_no_condition_turn(decision, prior=None) is False


def test_route_decision_axes_are_all_classified() -> None:
    """`RouteDecision` 에 필드가 늘면 **분류를 강제한다**.

    PR #311 리뷰가 잡은 갭(`total_budget`·`buy_all` 이 판정에서 통째로 빠짐)은 "새 축이 생겼는데
    아무도 모른다"는 구조적 문제였다. `_FILTER_AXES` 가 `ProductSearchFilters` 전체와
    대조되듯(tests/unit/test_decompose.py), 여기서는 `RouteDecision` 전체를 세 집합으로 못박는다.
    """
    from dataclasses import fields

    from app.agents.buyer.recommendation import no_condition

    blocking = {  # 있으면 조건 있는 턴 — 트리거를 막는다
        "filters",  # _FILTER_AXES 로 축별 검사
        "category_legs",  # 매핑된 카테고리
        "semantic_query_is_fallback",  # 의미 신호의 출처
        *no_condition._DECISION_CONDITION_AXES,  # 재구매·되돌리기 지목
    }
    selects_source = {  # 트리거는 통과시키되 **후보 확보 방식**을 가른다(has_total_budget)
        "total_budget",
    }
    no_effect_on_sourcing = {  # 후보 소스 선택에 영향이 없다
        "intent",  # 레인 선택(recommend/cart_add/...)
        "case",  # 발화 유형(1/2/3)
        "reply",  # intent=general 전용 답변
        "cart",  # 담기 의도 — 추천 레인 밖
        "scoped_to_previous",  # 직전 결과 지칭 = 멀티턴이라 prior 가 막는다
        "category_queries",  # 매핑 **전** 추측 — 결과인 category_legs 가 대표한다
        # "다 사줘" — 세트 의도이지만 leg 없는 턴에서는 어느 경로도 세트를 만들지 못하고
        # (`buy_all_mode` 가 `split_by_need` 를 요구한다) 둘 다 PICK_ONE 으로 끝난다.
        # 확인할 가격도 없어 취향 경로를 막을 근거가 없다.
        "buy_all",
    }

    assert {f.name for f in fields(RouteDecision)} == (
        blocking | selects_source | no_effect_on_sourcing
    )
    assert not (blocking & selects_source)
    assert not (blocking & no_effect_on_sourcing)
    assert not (selects_source & no_effect_on_sourcing)
