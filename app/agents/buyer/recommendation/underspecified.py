"""과소지정(underspecified) 발화 처리 (이슈 #336, `docs/specs/SPEC-UNDERSPECIFIED-336.md`).

"5만원 이내로 아무거나 세트로"·"아무거나 추천해줘"·"5만원 이하로 아무거나" 처럼 **무엇을**
찾는지는 지정하지 않고 가격 같은 **제약**만(혹은 그마저도 없이) 준 발화다. #162(조건 없는
발화, `no_condition.py`)가 그 부분집합이다 — 이 모듈은 #162 위에 얹혀 "제약만 있는 턴"까지
넓히고, 노출 후보 기반으로 카테고리를 되묻는다.

와이어 계약은 절대 건드리지 않는다 — 되물음은 `token` 산문으로만 나가고 `SuggestionChip`
스키마는 그대로다(칩은 relaxation/revert 중 정확히 하나를 강제하므로 카테고리 되물음 칩은
계약 의미 확장이다, SPEC §4 참조).
"""

from __future__ import annotations

import logging

from app.agents.buyer.recommendation.decompose import _FILTER_AXES
from app.agents.buyer.recommendation.no_condition import _is_blank
from app.agents.buyer.recommendation.state import RouteDecision
from app.core.text import _strip_unsafe
from app.schemas.spring import ProductSearchFilters

logger = logging.getLogger(__name__)

# what-축 — 무엇을 찾는지 지정하는 축. 하나라도 있으면 "지목했다"는 뜻이라 과소지정이 아니다.
_WHAT_FILTER_AXES = ("category", "brand", "keyword", "color", "attr_conditions")
# 제약-축 — 있어도 과소지정 상태는 유지된다. 후보를 거르는 데만 쓴다(D2 `within_price_range`).
_CONSTRAINT_FILTER_AXES = ("price_min", "price_max")
# 차단-축 — `rating_min`. 보수적으로 과소지정에서 제외해 기존 동작(무필터 검색 경로)을 유지한다.
# 근거: I-3 인기 후보는 검색 서비스의 평점 사후필터(§"반증 필요" 규약, `search_service.py`)를
# 타지 않는데, 그 필터를 이 모듈에 복제하면 "같은 판정을 두 곳에 둔다"는 lessons 위반이 된다.
# "평점 4 이상 아무거나"는 드문 발화고, 여기서 놓쳐도(과소지정 미판정) 결과는 종전 동작 그대로라
# 미탐 비용이 0이다. 재판정 필요성은 후속 이슈로 남긴다(SPEC "알려진 한계").
_BLOCKING_FILTER_AXES = ("rating_min",)

# 드리프트 방지 — 새 하드필터가 생기면 위 세 그룹 중 정확히 하나에 배정해야 한다
# (`test_underspecified_axes_partition_filter_axes`, decompose._FILTER_AXES 와 대조).
# [리뷰 R2] `assert` 가 아니라 `if`+`raise` 다 — `python -O`(최적화 모드)는 모듈 최상위를
# 포함한 모든 `assert` 문을 제거하므로, 이 축 분류가 그 모드에서는 조용히 검증 없이 통과한다.
_configured_filter_axes = (
    set(_WHAT_FILTER_AXES) | set(_CONSTRAINT_FILTER_AXES) | set(_BLOCKING_FILTER_AXES)
)
if _configured_filter_axes != set(_FILTER_AXES):
    raise RuntimeError(
        "underspecified 축 분류가 decompose._FILTER_AXES 와 어긋난다 — 새 하드필터 축은 "
        "_WHAT_FILTER_AXES/_CONSTRAINT_FILTER_AXES/_BLOCKING_FILTER_AXES 세 그룹 중 "
        "하나에 배정하라."
    )


def is_underspecified_turn(
    decision: RouteDecision, prior: ProductSearchFilters | None, settings
) -> bool:
    """이번 턴이 "과소지정 발화"인가 — no_condition(#162)의 상위 집합.

    ① flag off 면 항상 False(AC: 한 번에 전체 롤백).
    ② 첫 턴 한정(`prior is None`) — 멀티턴 리파인·3의도 판정은 #84 소관.
    ③ `category_legs`·`category_queries` 가 비어 있어야 한다 — 매핑이 실패해도 원시 신호가
       있으면 사용자가 무언가를 지목한 것이다(no_condition ⑤와 같은 근거, PR #311 리뷰).
    ④ `semantic_query_is_fallback` — 의미 신호가 실제로 있으면(발화 원문 폴백이 아니면) 조건이
       있는 턴이다.
    ⑤ 재구매 지목·되돌리기 지목이 없어야 한다 — 둘 다 사용자가 명시적으로 무언가를 가리킨
       신호다. `scoped_to_previous`(직전 결과 지칭)는 **보지 않는다**(리뷰 F4) — 이 판정은
       첫 턴(`prior is None`) 한정인데, 첫 턴엔 "직전 결과"(#113 의 지시 대상)가 존재하지
       않아 이 축은 공허하다. `is_no_condition_turn`(#162)도 같은 이유로 이 축을 안 본다 —
       여기서 보면 no_condition=True·underspecified=False 인 반례가 생겨 "no_condition ⊂
       underspecified" 불변식이 깨진다.
    ⑥ `filters` 의 what-축·차단-축(rating_min)이 전부 비어 있어야 한다. 제약-축
       (`price_min`/`price_max`)은 값이 있어도 통과시킨다 — 그 값은 D2 후보 필터링에 쓴다.
    """
    if not settings.underspecified_reask_enabled:
        return False
    if prior is not None:
        return False
    if decision.category_legs or decision.category_queries:
        return False
    if not decision.semantic_query_is_fallback:
        return False
    if decision.repurchase_products or decision.revert_categories:
        return False
    blocked = _WHAT_FILTER_AXES + _BLOCKING_FILTER_AXES
    return all(_is_blank(getattr(decision.filters, field, None)) for field in blocked)


def within_price_range(products: list, price_min: int | None, price_max: int | None) -> list:
    """가격이 `[price_min, price_max]`(양끝 포함, 미지정 경계는 무제한) 안인 상품만 남긴다.

    `no_condition.within_budget` 과 같은 **입증 필요** 규약이다 — 가격을 모르는 상품
    (`price is None`)은 가격 조건이 하나라도 걸린 턴에서는 뺀다. 가격 조건이 둘 다 없으면
    그대로(사본) 돌려준다. **순서는 보존한다**(stable) — BE 인기 순위·productId tiebreak 를
    유지해야 하므로 재정렬하지 않는다.

    [리뷰 F1] `price_min`/`price_max` 는 **`no_condition._is_blank` 와 같은 falsy 규약**으로
    "미지정" 을 판정한다 — `0` 도 미지정이다(`_is_blank` docstring: `price_min=0`("0원 이상")
    은 실제로 제약이 아니고, `price_max=0` 은 무의미한 값이라 어차피 0건인데 그 턴이 인기상품
    으로 폴백되는 편이 빈손보다 낫다). 이 규약을 어기면(0 을 유효 상한으로 취급하면)
    `is_underspecified_turn`/`is_no_condition_turn` 은 `price_max=0` 을 "조건 없음"으로
    판정해 popular 경로를 태우면서, 이 함수는 그 0 을 진짜 상한으로 걸러 전량 탈락시키는
    **판정-필터 비대칭**이 생긴다 — flag off 에서도(#162 no_condition 경로가 공유하므로)
    회귀한다.
    """
    if _is_blank(price_min):
        price_min = None
    if _is_blank(price_max):
        price_max = None
    if price_min is None and price_max is None:
        return list(products)
    result = []
    for product in products:
        if product.price is None:
            continue
        if price_min is not None and product.price < price_min:
            continue
        if price_max is not None and product.price > price_max:
            continue
        result.append(product)
    return result


def build_reask_question(products: list, settings) -> str:
    """노출 후보에서 카테고리를 뽑아 되묻는 질문을 만든다 — `token` 산문 전용(칩 아님, D3).

    노출 순서를 보존한 채 카테고리명을 **dedup** 하며 최대 `underspecified_reask_examples_max`
    개까지만 예시로 쓴다(0이면 예시 없이 generic). 각 카테고리명은 `_strip_unsafe` 로 정제한다
    (Spring 응답이라 신뢰 경계 밖). 예시가 있으면 `{categories}` 자리표시자를 가진 템플릿을
    쓰고, 없거나(0건) 템플릿에 자리표시자가 없거나 포맷이 실패하면 generic 질문으로 폴백한다
    (no_condition 의 예산 고지와 같은 방어 패턴 — `str.format` 은 안 쓰는 키워드를 조용히
    무시하므로 자리표시자 존재를 먼저 검사해야 한다).

    문구에는 가격·평점 값을 넣지 않는다(#171/#173 경계 — 예산 복창은 #162 고지 소관이라 여기서
    중복하지 않는다) — 어떤 조건이 적용됐다고 주장하지도 않는다(#132 거짓 주장 금지).

    [리뷰 F3] `underspecified_reask_question` 이 빈 값(정제 후 포함)이면 이 함수도 빈 문자열을
    돌려주고, 호출부는 그 값으로 되물음 token 만 조용히 낼 뿐이다 — 기동 검증은 없다(config
    주석 참조). 후보 스왑·완화 억제 등 다른 동작에는 영향이 없다.
    """
    generic = _strip_unsafe(settings.underspecified_reask_question)
    cap = settings.underspecified_reask_examples_max
    categories: list[str] = []
    seen: set[str] = set()
    if cap > 0:
        for product in products:
            raw_category = getattr(product, "category", None)
            if not isinstance(raw_category, str) or _is_blank(raw_category):
                continue
            cleaned = _strip_unsafe(raw_category)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            categories.append(cleaned)
            if len(categories) >= cap:
                break
    if not categories:
        return generic
    template = settings.underspecified_reask_question_examples
    if "{categories}" not in template:
        logger.warning("underspecified_reask_question_examples_missing_placeholder")
        return generic
    try:
        question = template.format(categories=" · ".join(categories))
    except (KeyError, IndexError, ValueError):
        logger.warning("underspecified_reask_question_examples_invalid")
        return generic
    return _strip_unsafe(question) or generic
