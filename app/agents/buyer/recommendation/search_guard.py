"""최종 payload 기준 검색 가드 (이슈 #393, api-spec §4.17).

`no_condition.py`(#162)·`underspecified.py`(#336)는 **decompose 산출**(`category_queries` 등
원시 신호)로 "조건이 있는 턴인가"를 판정한다. 그런데 실제로 Spring 에 나가는 것은 카테고리
매핑을 거쳐 조립된 **최종 payload** 다 — 그 사이에 매핑 드롭(거리컷 등)이 끼면 두 판정이
어긋난다:

    "신발 추천해줘"
      → decompose: category_queries=[신발]         → 원시 신호 있음 → no_condition/underspecified 둘 다 False
      → category_mapping: 거리컷 드롭               → category_legs=[] → filters.category=None
      → 실제 I-1 payload: keyword="신발" 뿐 (또는 keyword 도 없으면 파라미터 0개 = 12MB)

운영 실측(2026-08-06): 무필터 I-1 은 7.74초·12.3MB(AI→Spring 3초 예산 초과) → SEARCH_FAILED.
이 모듈은 no_condition/underspecified 의 기존 판정을 대체하지 않고(그 축은 `category_queries`
원시 신호를 여전히 지킨다, PR #311 리뷰), **최종 payload 기준의 세 번째 판정**을 더한다.
"""

from __future__ import annotations

from app.agents.buyer.recommendation.state import RouteDecision
from app.services import spring_client


def is_unfiltered_payload(decision: RouteDecision) -> bool:
    """이번 턴이 Spring 에 **파라미터 0개**로 나갈 턴인가 — 매칭 전량(실측 12.3MB)을 받는 턴이다.

    `category_legs` 가 있으면 leg 마다 `categoryName` 이 실려 나가므로(`_run_search` fan-out)
    절대 무필터가 아니다 — leg 검색은 `decision.filters` 가 아니라 leg 별로 조립된 필터를
    쓰므로 `search_filter_axes(decision.filters)` 만으로는 fan-out 유무를 알 수 없다.

    마스터 스위치(`search_filter_guard_enabled`) 검사는 **호출부 책임**이다 — 이 함수는 순수
    판정만 한다.
    """
    if decision.category_legs:
        return False
    return not spring_client.search_filter_axes(decision.filters)


def is_category_mapping_dropped(decision: RouteDecision) -> bool:
    """사용자가 카테고리를 지목했는데(`category_queries`) 매핑이 leg 를 하나도 못 냈는가.

    매핑 성공 여부와 무관하게 **신호의 유무**로 본다(`no_condition._DECISION_CONDITION_AXES`
    와 같은 근거) — `category_queries` 가 비어 있으면(애초에 카테고리를 지목하지 않은 턴) 이
    함수는 항상 False 다.
    """
    return bool(decision.category_queries) and not decision.category_legs


# [PR #411 Claude 리뷰] B(매핑 드롭 0건 폴백)가 인기 후보로 대체해도 되는 payload 축 — Spring
# 쿼리 파라미터 **키**(`_search_query_params`/`search_filter_axes` 가 돌려주는 값) 기준이다,
# 필드명이 아니다. 이 세 개만 안전한 이유:
#   - `keyword` — B 의 고지(`category_unmapped_notice`)가 "말씀하신 상품을 정확히 찾지 못해"라고
#     **명시적으로 실패를 밝히는** 축이라, keyword 미달 인기 대체가 거짓 주장이 되지 않는다.
#   - `minPrice`/`maxPrice` — 인기 후보에 `within_price_range` 로 **실제로 적용되는** 축이다.
# `brandName`·`categoryName`·`color` 는 여기 없다 — `brand=["나이키"]` 처럼 이 축이 남은 채로
# B 가 발동하면 인기 상품은 브랜드를 전혀 안 걸렀는데 `conditions` 칩엔 "나이키"가 그대로 떠
# 표시-실제가 어긋난다(C 가 막는 rating/attr 표시-실제 위반과 같은 성질). **인기 후보에
# brandName 을 클라이언트 사이드로 다시 거르는 방식은 채택하지 않는다** — (1) Spring 의
# brandName 매칭은 서버 IN 매칭이라 대소문자·공백 정규화 규약을 AI 가 재현할 근거가 없다(재현하면
# "같은 판정을 두 곳에 둔다" 위반), (2) I-3 는 인기 상위 N(기본 30)이라 브랜드로 거르면 대개
# 0건이 되어 "인기 상품으로 골라봤어요" 고지만 부정확해진다, (3) `color` 는 `SpringProduct` 에
# 필드 자체가 없어(`app/schemas/spring.py`) 거를 방법이 아예 없다 — 축마다 처치가 갈리면 그
# 비대칭이 다음 결함이 된다. 그래서 이 세 축 밖이면 B 를 아예 발동하지 않고 종전 동작(0건 응답)
# 그대로 둔다.
_POPULAR_SAFE_AXES = frozenset({"keyword", "minPrice", "maxPrice"})


def is_popular_fallback_safe(decision: RouteDecision) -> bool:
    """B(매핑 드롭 0건 폴백)가 인기 후보로 대체해도 표시-실제가 어긋나지 않는 턴인가.

    payload 축이 `_POPULAR_SAFE_AXES` 의 부분집합일 때만 True 다 — `keyword`/가격만 있으면
    인기 후보로 안전하게 대체할 수 있고, `brandName`/`categoryName`/`color` 가 하나라도 있으면
    (매핑이 leg 를 못 낸 카테고리 축은 이미 빠져 있으므로 여기 걸리는 건 brand·color) False 다.
    """
    return spring_client.search_filter_axes(decision.filters) <= _POPULAR_SAFE_AXES
