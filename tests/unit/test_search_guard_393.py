"""최종 payload 기준 검색 가드 판정 — 이슈 #393, api-spec §4.17.

`search_filter_axes`(spring_client)가 `_search_query_params` 와 어긋나지 않음을 고정하고,
`search_guard.is_unfiltered_payload`/`is_category_mapping_dropped` 의 입출력을 단위로 고정한다.
그래프 배선(A/B/C 실제 라우팅)은 `tests/unit/test_recommendation.py` 가 진다 — 여기는 순수 함수만.
"""

from __future__ import annotations

from app.agents.buyer.recommendation.search_guard import (
    is_category_mapping_dropped,
    is_unfiltered_payload,
)
from app.agents.buyer.recommendation.state import CategoryQuery, RouteDecision
from app.schemas.spring import ProductSearchFilters
from app.services.spring_client import search_filter_axes


def _decision(*, category_legs=(), category_queries=(), **filter_kwargs) -> RouteDecision:
    return RouteDecision(
        intent="recommend",
        filters=ProductSearchFilters(**filter_kwargs),
        category_legs=list(category_legs),
        category_queries=list(category_queries),
    )


# ─────────── search_filter_axes — payload 축 드리프트 방지 ───────────


def test_blank_filters_have_no_axes():
    """아무 필터도 없으면 축이 비어야 한다 — 이것이 이 이슈가 막으려는 무필터 payload 다."""
    assert search_filter_axes(ProductSearchFilters()) == set()


def test_keyword_is_an_axis():
    assert search_filter_axes(ProductSearchFilters(keyword="신발")) == {"keyword"}


def test_category_is_an_axis():
    assert search_filter_axes(ProductSearchFilters(category="신발")) == {"categoryName"}


def test_price_min_is_an_axis():
    assert search_filter_axes(ProductSearchFilters(price_min=1000)) == {"minPrice"}


def test_price_max_is_an_axis():
    assert search_filter_axes(ProductSearchFilters(price_max=50000)) == {"maxPrice"}


def test_brand_is_an_axis():
    assert search_filter_axes(ProductSearchFilters(brand=["나이키"])) == {"brandName"}


def test_color_is_an_axis():
    assert search_filter_axes(ProductSearchFilters(color="검정")) == {"color"}


def test_rating_min_is_not_an_axis():
    """rating_min 은 AI 사후필터다 — Spring payload 축이 아니다(#393 결함의 핵심 전제)."""
    assert search_filter_axes(ProductSearchFilters(rating_min=4.5)) == set()


def test_attr_conditions_is_not_an_axis():
    """attr_conditions 도 AI 사후필터다 — Spring payload 축이 아니다."""
    assert search_filter_axes(ProductSearchFilters(attr_conditions={"소재": "린넨"})) == set()


def test_exclude_product_ids_is_not_an_axis():
    """dedup 제외도 AI 사후필터다 — Spring payload 축이 아니다."""
    assert search_filter_axes(ProductSearchFilters(exclude_product_ids=[1, 2, 3])) == set()


def test_blank_strings_do_not_count_as_axes():
    """공백-only 값은 빈 값이다(#127 리뷰 규약) — `_search_query_params` 와 같은 판정을 공유한다."""
    assert search_filter_axes(ProductSearchFilters(keyword="   ", category="  ")) == set()


# ─────────── is_unfiltered_payload ───────────


def test_unfiltered_payload_true_when_everything_blank():
    assert is_unfiltered_payload(_decision()) is True


def test_unfiltered_payload_false_with_keyword():
    """ "신발" 처럼 keyword 하나만 있어도 무필터가 아니다 — 실측 435 bytes."""
    decision = _decision(keyword="신발")
    assert is_unfiltered_payload(decision) is False


def test_unfiltered_payload_false_with_rating_min_but_category_legs_present():
    """`category_legs` 가 있으면 leg 마다 categoryName 이 실려 나가 절대 무필터가 아니다 —
    `search_filter_axes(decision.filters)` 만 보면 놓치는 부분(fan-out 은 leg 별 필터를 쓴다)."""
    decision = _decision(category_legs=[("신발", None)], rating_min=4.5)
    assert is_unfiltered_payload(decision) is False


def test_unfiltered_payload_true_for_rating_min_only_turn():
    """[#393 A/C 핵심] rating_min 만 있는 턴("평점 4 이상 아무거나")은 payload 기준 무필터다 —
    no_condition/underspecified 축은 안 걸리지만(rating_min 이 두 판정을 모두 블록) 실제 I-1
    호출은 파라미터 0개다."""
    decision = _decision(rating_min=4.5)
    assert is_unfiltered_payload(decision) is True


def test_unfiltered_payload_true_for_attr_conditions_only_turn():
    """attr_conditions 만 있는 턴("방수되는 거")도 같은 이유로 무필터다."""
    decision = _decision(attr_conditions={"방수": "true"})
    assert is_unfiltered_payload(decision) is True


# ─────────── is_category_mapping_dropped ───────────


def test_mapping_dropped_true_when_queries_present_and_legs_empty():
    """ "신발 추천해줘" — 카테고리를 지목했는데 매핑이 leg 를 하나도 못 냈다."""
    decision = _decision(category_queries=[CategoryQuery(raw_category="신발")])
    assert is_category_mapping_dropped(decision) is True


def test_mapping_dropped_false_when_no_category_signal():
    """카테고리를 애초에 지목하지 않은 턴은 "드롭"이 아니다 — 매핑할 신호 자체가 없었다."""
    decision = _decision()
    assert is_category_mapping_dropped(decision) is False


def test_mapping_dropped_false_when_mapping_succeeded():
    decision = _decision(
        category_queries=[CategoryQuery(raw_category="신발")],
        category_legs=[("신발/스니커즈", "신발")],
    )
    assert is_category_mapping_dropped(decision) is False


def test_mapping_dropped_true_for_multi_item_drop_too():
    """[PR #311 리뷰 전제 보존] 카테고리 2개가 모두 매핑 실패해도(멀티 아이템) 이 함수는 True 다
    — "드롭됐는가"는 개수와 무관하다. 그래프가 이 신호를 A(unfiltered_bypass)에서 제외하고
    B(사후 폴백)로만 쓰는 것은 배선(graph.py) 책임이지 이 판정의 책임이 아니다."""
    decision = _decision(
        category_queries=[
            CategoryQuery(query="무선 이어폰"),
            CategoryQuery(query="노트북"),
        ]
    )
    assert is_category_mapping_dropped(decision) is True
