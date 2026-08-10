"""#465 단일 category leg 억제 — 총칭 head 와 조건어만 차단한다.

P5b 누출 표본의 ``가성비 좋은 거``·``평점 높은 상품``·``무료배송``·``5만원 이하 아무거나``는
발화 에코라 문자열 포함 검사가 아닌 마지막 공백 토큰(head)으로 구분한다. 상품명 ``과일``·``라면``·
``텐트``·``텀블러``·``유아용 물티슈``는 이 어휘에 포함하지 않아 보존된다.

실측에서 named_category 보호대상 77표본의 오발동은 0건, 조건전용 누출은 런당 2건 제거됐고
LLM 호출은 0이다. 발동률이 낮은 것은 표적이 작기 때문이며 결함이 아니다; primary missRate는
#466 병합 후 표적이 런당 2~3건이라 개선되지 않았다.
"""

from app.agents.buyer.recommendation.state import CategoryQuery, ProductSearchFilters


def suppress_generic_single_leg(
    legs: list[CategoryQuery],
    filters: ProductSearchFilters,
    *,
    enabled: bool,
    generic_heads: frozenset[str],
    condition_terms: frozenset[str],
) -> list[CategoryQuery]:
    """조건만 말한 단일 총칭 leg만 제거한다; 다중 leg은 승격 규약이 달라 그대로 보존한다."""
    if not enabled or len(legs) != 1 or _has_what_axis(filters):
        return legs
    query = (legs[0].query or "").strip()
    tokens = query.split()
    if not tokens:
        return legs
    if tokens[-1] in generic_heads or all(token in condition_terms for token in tokens):
        return []
    return legs


def _has_what_axis(filters: ProductSearchFilters) -> bool:
    return bool(
        filters.category
        or filters.brand
        or filters.keyword
        or filters.color
        or filters.attr_conditions
    )
