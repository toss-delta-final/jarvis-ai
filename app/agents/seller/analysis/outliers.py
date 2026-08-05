"""MAD 스파이크·Tukey fence·심야 비중 — abuse 3-트랙 계산 (이슈 #290).

근거(docs/worker-papers.md):
- Chandola et al. 2009: 이상 3유형 체계(Point/Contextual/Collective) — 트랙 구조.
- Tan & Kumar 2002: 봇 시그널 피처(요청량 급증·커버리지·심야 활동)를 현행 계약
  (I-13 일별/상품별 집계, I-8 hour/ip 집계) 단위로 번역했다 — 세션 단위 피처
  (이벤트 간격 규칙성 등)는 원시 데이터가 없어 Phase B.
- MAD 임계 3.5 는 Iglewicz & Hoaglin 권장값, Tukey k=1.5 는 표준 fence.

트랙 매핑(도구 배선은 tools.py):
- Point: I-13 date-groupBy 일별 이벤트량 → mad_spikes (봇 유입 = 볼륨 급증)
- Contextual: I-13 상품별 비율 → tukey_upper_outliers (조회 폭증+구매 0 등)
- Collective: I-8 hour-groupBy → night_activity_share (+ ip rows 랭킹은 도구 층)

순수 함수·결정론(§10-②). 튜너블(threshold·k·심야 시간대)은 전부 호출부 주입.
"""

from __future__ import annotations

import statistics

# robust 통계 헬퍼는 timeseries 와 공유한다(같은 계산 층 패키지 내부 재사용).
from app.agents.seller.analysis.timeseries import _median, _robust_scale
from app.agents.seller.analysis.types import OutlierFlag

# Tukey fence 는 사분위수가 필요하다 — 4점 미만이면 판정 보류(빈 결과).
_MIN_ITEMS_FOR_TUKEY = 4
# MAD 스파이크도 기준 분포가 필요하다 — 3점 미만이면 판정 보류.
_MIN_POINTS_FOR_MAD = 3


def mad_spikes(
    targets: list[str], values: list[float], *, threshold: float, metric: str
) -> list[OutlierFlag]:
    """robust z(|x-median| / 1.4826×MAD) ≥ threshold 인 **상방** 급증만 표기한다.

    하방(0건 날 등)은 트래픽 부재지 어뷰징 신호가 아니므로 제외한다 — 봇 유입은
    볼륨 급증으로 나타난다(Tan & Kumar). 상수 수열(scale=0)은 중앙값 초과 값만
    무한 편차 급증으로 처리한다. value 필드는 robust z 값이다(임계와 동일 눈금).
    """
    if len(targets) != len(values):
        raise ValueError(f"targets({len(targets)})/values({len(values)}) 길이가 다르다")
    if len(values) < _MIN_POINTS_FOR_MAD:
        return []
    center = _median(values)
    scale = _robust_scale(values, center)
    flags: list[OutlierFlag] = []
    for target, value in zip(targets, values, strict=True):
        if value <= center:
            continue  # 상방만 — 하방은 어뷰징 신호가 아니다
        z = (value - center) / scale if scale > 0 else float("inf")
        if z >= threshold:
            flags.append(
                OutlierFlag(
                    type="point", target=target, metric=metric, value=z, threshold=threshold
                )
            )
    return flags


def tukey_upper_outliers(
    items: list[tuple[str, float]], *, k: float, metric: str
) -> list[OutlierFlag]:
    """브랜드 내 분포의 상위 Tukey fence(Q3 + k×IQR) 초과 항목을 표기한다.

    대상 비율(조회/구매·담기율·방문자당 조회)은 전부 "높을수록 의심" 방향이라
    상위 fence 만 본다 — 하위는 저활동이지 어뷰징이 아니다. value 는 원 비율,
    threshold 는 fence 값이다(LLM 이 초과 폭을 그대로 서술할 수 있게).
    IQR=0(비율 대부분 동일)이면 fence = Q3 — 동일 다수에서 벗어난 값만 걸린다.
    """
    if len(items) < _MIN_ITEMS_FOR_TUKEY:
        return []
    values = [value for _, value in items]
    q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive")
    fence = q3 + k * (q3 - q1)
    return [
        OutlierFlag(type="contextual", target=target, metric=metric, value=value, threshold=fence)
        for target, value in items
        if value > fence
    ]


def night_activity_share(
    hour_rows: list[dict], *, start: int, end: int
) -> tuple[float, int, int] | None:
    """I-8 hour-groupBy rows({key,count})에서 심야 [start, end) 활동 비중을 계산한다.

    반환 (share, night_count, total). 총 0건·유효 행 없음이면 None(판정 보류 —
    0% 로 위장하지 않는다). key 가 시각으로 해석 불가한 행은 관대 수신으로 제외한다.
    """
    night = 0
    total = 0
    for row in hour_rows:
        try:
            hour = int(row.get("key"))
            count = int(row.get("count", 0))
        except (TypeError, ValueError):
            continue  # 비정상 행 관대 수신 — Collective 트랙만 보수적으로 좁힌다
        total += count
        if start <= hour < end:
            night += count
    if total <= 0:
        return None
    return night / total, night, total
