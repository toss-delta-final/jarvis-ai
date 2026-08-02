"""0건/소량 조건 완화 후보 생성 (이슈 #113, api-spec §3.1 · SPEC-RECOMMEND-001 §6.6).

카테고리는 **유지한 채** 비카테고리 조건(가격 상한·평점 하한·브랜드·색상)만 한 단계 푸는 후보를
만든다. 카테고리 판단·승계는 #84 소관이라 여기서 건드리지 않는다 — 같이 풀면 "무선 이어폰이
없으니 유선 어때요"처럼 살 물건 자체를 바꾸는 제안이 되어 성격이 다른 결정이 된다.

여기는 **순수 함수만** 둔다(I/O 없음). 후보를 실제로 검색해 estCount 를 세는 probe 와 SSE emit 은
graph.py 소관이다 — 완화 폭 계산·문구 생성은 검색 왕복 없이 테스트할 수 있어야 한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.schemas.spring import ProductSearchFilters

# 와이어 필드명(camelCase) → ProductSearchFilters 속성명. 계약(§3.1 `relaxation.field`)은 camelCase
# 이고 내부 필터는 snake_case 라 **여기 한 곳에서만** 잇는다(config 목록도 와이어 표기를 쓴다).
# category 는 의도적으로 없다 — config 에 실수로 넣어도 후보가 되지 않는 이중 방어다(AC④).
_FIELD_TO_ATTR: dict[str, str] = {
    "priceMax": "price_max",
    "ratingMin": "rating_min",
    "brand": "brand",
    "color": "color",
}

# 안내·칩 문구용 한국어 이름.
_FIELD_LABEL: dict[str, str] = {
    "priceMax": "가격",
    "ratingMin": "평점",
    "brand": "브랜드",
    "color": "색상",
}


@dataclass(frozen=True)
class RelaxationCandidate:
    """완화 후보 1건 — 칩 1개 또는 자동 완화 1라운드의 재료."""

    field: str  # 와이어 필드명 — SuggestionChip.relaxation.field 로 그대로 나간다
    value: Any  # 제안 값. None 이면 '조건 해제'
    filters: ProductSearchFilters  # 이 완화를 적용한 검색 필터(probe 입력)
    label: str  # 칩 문구
    notice: str  # 자동 완화 채택 시 투명 안내 문구(REQ-REC-042)


def _relaxed_price_max(current: int, settings) -> int | None:  # noqa: ANN001
    """가격 상한을 config 비율만큼 올리고 올림 단위로 맞춘다 — 5만 → 6.5만.

    올림 단위는 "65,437원까지 볼까요?" 같은 읽기 힘든 제안을 막는 표시 목적이다.
    """
    widened = current * (1.0 + settings.relaxation_price_step_ratio)
    unit = settings.relaxation_price_round_unit
    value = int(math.ceil(widened / unit) * unit)
    # 반올림이 현재값을 못 넘으면 완화가 아니다(비율이 매우 작거나 단위가 클 때).
    return value if value > current else None


def _relaxed_rating_min(current: float, settings) -> float | None:
    """평점 하한을 config 폭만큼 내린다. 0 이하로 내려가면 값이 아니라 조건 해제다."""
    value = round(current - settings.relaxation_rating_step, 1)
    return value if value > 0 else None


def _price_text(value: int) -> str:
    return f"{value:,}원"


def build_relaxation_candidates(
    filters: ProductSearchFilters,
    settings,  # noqa: ANN001
) -> list[RelaxationCandidate]:
    """현재 필터에서 만들 수 있는 완화 후보를 config 우선순위 순서로 만든다.

    실제로 값이 설정된 필드만 후보가 된다 — 걸리지도 않은 조건을 "풀어 드릴까요?"라고 물으면
    사용자는 자기가 걸지 않은 제약이 있었다고 오해한다.
    """
    candidates: list[RelaxationCandidate] = []
    for field in settings.relaxation_chip_fields:
        attr = _FIELD_TO_ATTR.get(field)
        if attr is None:  # category 등 완화 대상이 아닌 필드는 조용히 건너뛴다(AC④)
            continue
        current = getattr(filters, attr, None)
        if current is None or current == [] or current == "":
            continue

        name = _FIELD_LABEL[field]
        if field == "priceMax":
            value = _relaxed_price_max(int(current), settings)
            if value is None:
                continue
            label = f"{_price_text(value)}까지 볼까요?"
            notice = (
                f"{_price_text(int(current))} 이하로는 찾지 못해 {_price_text(value)}까지 넓혔어요."
            )
        elif field == "ratingMin":
            value = _relaxed_rating_min(float(current), settings)
            if value is None:
                label = "평점 조건 없이 볼까요?"
                notice = f"평점 {current} 이상으로는 찾지 못해 평점 조건을 뺐어요."
            else:
                label = f"평점 {value} 이상까지 볼까요?"
                notice = f"평점 조건을 {current} 에서 {value} 로 조금 넓혔어요."
        else:  # brand·color — 값 완화가 아니라 조건 해제뿐이다
            value = None
            label = f"{name} 조건 없이 볼까요?"
            notice = f"{name} 조건을 빼고 찾았어요."

        candidates.append(
            RelaxationCandidate(
                field=field,
                value=value,
                filters=filters.model_copy(update={attr: value}),
                label=label,
                notice=notice,
            )
        )
    return candidates
