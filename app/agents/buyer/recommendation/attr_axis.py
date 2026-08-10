"""attrConditions 에 잘못 실린 제약 축을 결정론으로 제거한다.

가격·평점은 스키마에 전용 필드(`ProductSearchFilters.price_min`/`price_max`/`rating_min`)가
이미 있다. 따라서 `attrConditions` 에 `가격`·`평점`이라는 이름의 축이 실린 것은 구조적으로
오배치이며, 정당한 상품 속성일 수 없다. 소재·핏·용도·방수처럼 상품 자체의 이름을 가진 축은
그대로 보존한다.

축 이름만 `strip()`한 뒤 `casefold()`로 대소문자를 무시해 정확히 일치시킨다. 부분문자열·접두
매칭은 `가격대비만족도` 같은 정당한 다른 축까지 지울 수 있어 사용하지 않는다. 비교를 위한
정규화와 달리 보존·진단하는 축 표기는 원문을 유지한다.
"""

from __future__ import annotations


def strip_constraint_axes(
    conditions: dict[str, str] | None,
    *,
    enabled: bool,
    constraint_axes: frozenset[str],
) -> tuple[dict[str, str] | None, list[str]]:
    """제약 축(가격·평점·수량 등)을 attrConditions 에서 걷어낸다.

    반환: (정제된 conditions 또는 None, 제거된 축 이름 리스트)
    """
    if not enabled:
        return conditions, []
    if conditions is None:
        return None, []

    normalized_constraint_axes = {axis.strip().casefold() for axis in constraint_axes}
    retained: dict[str, str] = {}
    suppressed: list[str] = []
    for key, value in conditions.items():
        axis = key.strip()
        if axis.casefold() in normalized_constraint_axes:
            suppressed.append(axis)
        else:
            retained[axis] = value
    return retained or None, suppressed
