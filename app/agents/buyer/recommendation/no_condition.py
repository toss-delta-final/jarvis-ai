"""조건이 하나도 없는 추천 발화 처리 (이슈 #162, api-spec §4.12).

"아무거나 추천해줘"·"뭐 살까?" 는 decompose 가 `general` 이 아니라 **추천 레인**으로 보낸다
(프롬프트: "상품을 찾아달라는 요청이면 recommend"). 그런데 필터가 전부 비어 `_search_query_params`
가 파라미터 0개를 만들고, 그대로 I-1 에 나가 매칭 전량(실측 7,245건·13.33MB·1.112s,
`docs/specs/MEASURE-I1-RESPONSE-132.md`)을 받는다.

**이건 계약 위반이다** — I-1 정본은 후보 수 상한을 폐지하며 "정형조건이 하나도 없는 요청은
LLM 단에서 차단하므로 BE 는 별도 가드를 두지 않는다"를 전제로 걸었고, 0건 시 폴백 대상으로
I-3(§4.12)를 지목했다. 이 모듈이 그 차단을 구현한다.

에러도 0건도 아니라 **겉보기엔 정상**이라는 점이 이 결함의 성질이다 — 후보가 비지 않아
zero-result 분기도 degrade 고지도 타지 않는다.
"""

from __future__ import annotations

from app.agents.buyer.recommendation.decompose import _FILTER_AXES
from app.agents.buyer.recommendation.state import RouteDecision
from app.schemas.spring import ProductSearchFilters

# 하드필터 축 목록은 **decompose 의 `_FILTER_AXES` 를 그대로 쓴다** — 사본을 두면 새 필터가
# 생겼을 때 한쪽만 늘어나 조건 있는 턴이 조용히 "조건 없음"으로 새어 들어온다. 그 목록은
# `ProductSearchFilters` 전체 필드와 대조하는 드리프트 테스트가 지키고 있다
# (`tests/unit/test_decompose.py`). `semantic_query` 는 이 목록에 **의도적으로 없고**
# 아래에서 출처(`semantic_query_is_fallback`)로 따로 판정한다.


def _is_blank(value: object) -> bool:
    """값이 "조건 없음"인가. 공백-only 문자열도 빈 값으로 본다.

    `if value:` 만 쓰면 `''`(falsy)는 막아도 `' '`(truthy)는 통과한다 — LLM 산출값이라 신뢰
    경계 밖이고, `_search_query_params` 가 같은 함정을 이미 밟았다(#127 리뷰: 공백-only 가
    Spring 에 빈값으로 나갔다).
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    # 빈 list(brand)·빈 dict(attr_conditions)는 조건 없음이다.
    # 수치 0 도 조건 없음으로 본다 — `price_min=0`("0원 이상")은 실제로 제약이 아니고
    # `rating_min=0` 도 마찬가지다. `price_max=0` 은 무의미한 값이라 어차피 0건인데, 그 턴이
    # 인기상품으로 폴백되는 편이 빈손보다 낫다.
    return not value


def is_no_condition_turn(
    decision: RouteDecision, prior: ProductSearchFilters | None
) -> bool:
    """이번 턴이 "조건이 하나도 없는 추천 발화"인가.

    넷을 **모두** 만족해야 한다:
      ① 첫 턴 (`prior is None`)
      ② `category_legs` 가 빔 — 카테고리가 매핑됐으면 조건이 있는 턴이다
      ③ **의미 신호가 없음** (`semantic_query_is_fallback`)
      ④ `filters` 의 하드필터 축이 전부 빔 (`_FILTER_AXES`)

    ③을 값의 유무로 판정하면 **영영 트리거되지 않는다.** decompose 는 `semantic_query` 를
    `llm_sq or cat_signal or prior_sq or query` 로 채워(decompose.py) 아무 신호가 없어도
    **이번 턴 원문**이 들어가기 때문이다 — "아무거나 추천해줘"에서도 값은 "아무거나 추천해줘"다.
    그래서 값이 아니라 **출처**를 본다.

    ②가 멀티턴 리파인도 함께 막는다 — `_carry_prior_category`(buyer/graph.py)가 직전 턴
    카테고리를 `category_legs` 로 승계하기 때문이다. ①은 그 승계가 없는 경우까지 막는
    이중 방어다: 멀티턴의 "리파인 / 칩 제거 / 카테고리-무관 리셋" 세 의도는 아직 구분되지
    않으므로(#84) 이 경로는 **첫 턴에 한정**한다. #84 해소 후 확장 대상이다.

    **애매하면 False 로 기운다** — 오탐(조건 있는 턴을 조건 없음으로 봄)은 사용자가 말한
    조건을 버리는 반면, 미탐은 종전 동작(무필터 검색)이라 새로 나빠지지 않는다.
    """
    if prior is not None:
        return False
    if decision.category_legs:
        return False
    if not decision.semantic_query_is_fallback:
        return False
    return all(_is_blank(getattr(decision.filters, field, None)) for field in _FILTER_AXES)
