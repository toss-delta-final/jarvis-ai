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
