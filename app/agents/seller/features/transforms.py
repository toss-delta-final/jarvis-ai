"""고객 피처 변환 4종 + 보조 (이슈 #593, `03-FEATURES` 2부 §2).

전부 순수 함수이고 stdlib `math` 만 쓴다 — numpy 를 끌어오지 않아야 테스트가 fixture
하나로 끝난다(`03` §7 "전 함수 순수").

[결측 규약]
분모가 0 인 비율은 **None** 이다. 0.0 으로 위장하면 "단계 미발생"과 "관측 없음"이
같은 값이 되어 보고서가 없는 사실을 인용한다(`analysis/__init__` 원칙 승계).
군집 입력에 넣을 값은 `shrinkage` 가 따로 만든다 — 원값과 평활값을 분리 저장하는
이유가 이것이다(`03` §2.2).
"""

from __future__ import annotations

import math


def log1p(value: float) -> float:
    """카운트 롱테일 완화 — ``ln(1+x)``. x=0 은 0 이라 0 을 보존한다(결측 위장 아님).

    이커머스 이벤트 카운트는 멱법칙 분포라 StandardScaler 만 걸면 극단값 몇 명이 축을
    지배하고 나머지가 원점에 뭉친다. 음수는 계약상 오지 않지만(BE 가 전부 카운트다)
    로그가 정의되지 않는 값이 들어오면 조용히 NaN 이 퍼지므로 0 으로 막는다.
    """
    return math.log1p(max(0.0, float(value)))


def recency_score(days_ago: float) -> float:
    """R 축 — ``1/(1+경과일)``. 0(오래됨) ~ 1(오늘).

    ⚠️ I-38 의 `lastActivityDaysAgo` 는 **구매가 아니라 활동** 기준이라 전통적 RFM 의 R
    과 정확히 같지 않다(`03` §2.1 C08 — `ProxyValue.basis` 규약이 다루는 근사). 보고서
    인용은 변환값이 아니라 원값("마지막 활동 14일 전")으로 한다.
    """
    return 1.0 / (1.0 + max(0.0, float(days_ago)))


def amount_bucket_to_log(bucket: str, mapping: dict[str, float]) -> float | None:
    """금액 구간 → 대표값의 로그. **매핑에 없는 구간은 None** 이다.

    BE 가 구간을 늘리면(계약 드리프트) 조용히 0 으로 떨어뜨리는 대신 결측으로 남긴다 —
    0 은 `ZERO` 구간의 정당한 값이라 구분이 불가능해진다. 호출부가 `Hold` 를 남기고
    군집 입력에서만 관측치 평균으로 대체한다.
    """
    return mapping.get(bucket)


def safe_ratio(numerator: float, denominator: float) -> float | None:
    """비율 원값 — 분모 0 이면 None(관측 없음). 0.0 과 구분해야 한다."""
    if not denominator:
        return None
    return float(numerator) / float(denominator)


def shrinkage(numerator: float, denominator: float, *, prior: float, alpha: float) -> float:
    """베이지안 평활 — 소표본 비율을 브랜드 전체 비율 쪽으로 끌어당긴다.

    ``(numer + α×prior) / (denom + α)``. 조회 1번에 담기 1번 한 사람이 "전환율 100%
    고객"이 되어 충성형 군집에 들어가는 사고를 막는다(`03` §2.2). 분모가 0 이면 값은
    정확히 `prior` 가 된다 — 이 경우의 "관측 없음"은 `_raw=None`·`_denom=0` 이 따로
    보존하므로 여기서 결측을 표현할 필요가 없다.
    """
    if alpha <= 0:
        raise ValueError(f"shrinkage alpha 는 양수여야 한다 (got {alpha})")
    return (float(numerator) + alpha * prior) / (float(denominator) + alpha)


def percentile_of(sorted_values: list[float], value: float) -> float:
    """`value` 가 분포에서 차지하는 백분위(0~100, weak — 같은 값 이하의 비율).

    `sorted_values` 는 **오름차순 정렬돼 있어야 한다**(호출부가 한 번만 정렬해 재사용).
    빈 분포는 판정 불가라 50.0(중립)을 준다 — 라벨 판정이 빈 군집을 만들 수 없으므로
    실제로 도달하지 않는 방어 경로다.
    """
    total = len(sorted_values)
    if total == 0:
        return 50.0
    import bisect

    return 100.0 * bisect.bisect_right(sorted_values, value) / total


def quintile_ranks(values: list[float], *, bins: int = 5) -> tuple[list[int], int]:
    """오분위 등급 1~`bins` 와 **실제로 나온 구간 수**를 함께 돌려준다.

    동점은 `qcut` 관행대로 **낮은 등급 쪽으로 몬다**(경계값 자체는 아래 구간). 분포가
    치우쳐 5구간이 안 나오면 나오는 만큼만 쓰고 실제 구간 수를 각인한다 — 주문 0회가
    60% 인 브랜드에서 F 를 5등분하는 건 불가능한데, 조용히 뭉개면 "F=3 인데 주문 0회"
    같은 값이 나온다(`03` §2.6).

    경계는 그 스냅샷 안에서 계산한다(상대 기준) — 브랜드 규모와 무관하게 작동한다.
    """
    if bins < 2:
        raise ValueError(f"bins 는 2 이상이어야 한다 (got {bins})")
    total = len(values)
    if total == 0:
        return [], 0

    ordered = sorted(float(v) for v in values)
    edges: list[float] = []
    for index in range(1, bins):
        # 선형 보간 분위수 — numpy.percentile(interpolation="linear") 과 같은 정의.
        position = (total - 1) * index / bins
        low = math.floor(position)
        high = math.ceil(position)
        if low == high:
            edges.append(ordered[low])
        else:
            edges.append(ordered[low] + (ordered[high] - ordered[low]) * (position - low))

    ranks = [1 + sum(1 for edge in edges if value > edge) for value in values]
    return ranks, len(set(ranks))
