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

from app.agents.buyer.recommendation.state import RouteDecision
from app.schemas.spring import ProductSearchFilters

# 사용자가 준 **조건**으로 볼 축. `exclude_product_ids`(최근구매 dedup)·`limit`(AI 후보 상한)은
# 여기 없다 — 발화에서 온 것이 아니라 AI 가 내부에서 채우는 값이라, 포함시키면 조건 없는 턴이
# 영영 트리거되지 않는다.
_CONDITION_FIELDS = (
    "category",
    "price_min",
    "price_max",
    "brand",
    "rating_min",
    "keyword",
    "color",
    "attr_conditions",
    # `semantic_query` 는 Spring 에 나가지 않는 AI 내부 필드지만 **사용자 의도의 담지자**라
    # 반드시 조건으로 센다. "여름에 시원한 거 추천해줘"는 filters 가 전부 null 이고 카테고리
    # 추측이 실패해도 semanticQuery="여름에 시원한" 이 남는다 — 이걸 빠뜨리면 사용자가 말한
    # 의미를 통째로 버리고 인기상품을 주게 된다(이슈 완료 조건의 회귀 항목).
    "semantic_query",
)


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

    셋을 **모두** 만족해야 한다:
      ① `filters` 의 사용자 조건 축이 전부 빔 (`_CONDITION_FIELDS`)
      ② `category_legs` 가 빔 — 카테고리가 매핑됐으면 조건이 있는 턴이다
      ③ 첫 턴 (`prior is None`)

    ②가 멀티턴 리파인도 함께 막는다 — `_carry_prior_category`(buyer/graph.py)가 직전 턴
    카테고리를 `category_legs` 로 승계하기 때문이다. ③은 그 승계가 없는 경우까지 막는
    이중 방어다: 멀티턴의 "리파인 / 칩 제거 / 카테고리-무관 리셋" 세 의도는 아직 구분되지
    않으므로(#84) 이 경로는 **첫 턴에 한정**한다. #84 해소 후 확장 대상이다.

    **애매하면 False 로 기운다** — 오탐(조건 있는 턴을 조건 없음으로 봄)은 사용자가 말한
    조건을 버리는 반면, 미탐은 종전 동작(무필터 검색)이라 새로 나빠지지 않는다.
    """
    if prior is not None:
        return False
    if decision.category_legs:
        return False
    return all(_is_blank(getattr(decision.filters, field, None)) for field in _CONDITION_FIELDS)
