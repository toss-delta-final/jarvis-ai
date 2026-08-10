"""STL 분해 + robust GESD 이상 검정 — S-H-ESD (이슈 #290).

근거 논문(docs/worker-papers.md):
- Hochenbaum, Vallis & Kejariwal 2017 (Twitter S-H-ESD): STL 잔차에 robust
  Generalized ESD 검정. mean/std 대신 median/MAD 를 쓰는 이유는 이상점 자신이
  기준 통계를 오염(masking)시키는 것을 막기 위함이다(논문 §3).
- Cleveland et al. 1990 (STL): 추세·계절·잔차 분해. period=7(요일 효과)이
  기존 SMA 방식이 못 가르던 "주말이라 원래 낮음"과 "진짜 급락"을 가른다.

소비처: sales_anomaly(get_sales_timeseries)·abuse Point 트랙(I-13 일별 이벤트량).
순수 함수·결정론(§10-②) — STL 은 폐형 계산이라 seed 불요. 튜너블은 전부 인자 주입.
"""

from __future__ import annotations

import math

from app.agents.seller.analysis.types import SeasonalAnomaly, SeasonalAnomalyDetection

# MAD → σ 환산 상수(정규분포 가정, Iglewicz & Hoaglin). robust z = |x-med| / (1.4826×MAD).
_MAD_TO_SIGMA = 1.4826
# MAD=0 폴백용 — 평균절대편차(MeanAD) → σ 환산 상수(√(π/2)).
_MEANAD_TO_SIGMA = 1.2533
# GESD 는 표본에서 이상점을 제거해 가며 검정하므로 최소 3점(자유도 n-2 ≥ 1)이 필요하다.
_MIN_POINTS_FOR_GESD = 3
# 잔차 노이즈 플로어(값 스케일 대비 상대) — 완벽히 규칙적인 시계열은 STL 잔차가
# 부동소수 먼지(~1e-11)뿐인데, MAD 가 그 먼지를 표준화하면 GESD 가 수치 오차를
# 이상으로 오탐한다. 값 스케일의 1e-9 미만 잔차는 0 으로 간주한다(결정론 유지).
_RESIDUAL_NOISE_FLOOR_RATIO = 1e-9


def _robust_scale(values: list[float], center: float) -> float:
    """robust σ 추정 — 1.4826×MAD, MAD=0 이면 1.2533×MeanAD 폴백.

    둘 다 0 이면(상수 수열) 0.0 을 반환한다 — 호출부가 "중앙값과 다른 값 = 무한 편차"
    로 처리한다(무매출 구간 직후 매출 발생 같은 케이스가 여기로 온다).
    """
    abs_dev = sorted(abs(v - center) for v in values)
    n = len(abs_dev)
    mad = abs_dev[n // 2] if n % 2 == 1 else (abs_dev[n // 2 - 1] + abs_dev[n // 2]) / 2
    if mad > 0:
        return _MAD_TO_SIGMA * mad
    mean_ad = sum(abs_dev) / n
    return _MEANAD_TO_SIGMA * mean_ad if mean_ad > 0 else 0.0


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    return ordered[n // 2] if n % 2 == 1 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def _gesd_outlier_indices(residuals: list[float], *, alpha: float, max_outliers: int) -> list[int]:
    """robust Generalized ESD — 이상점 인덱스 목록 (S-H-ESD §3).

    각 반복에서 남은 표본의 median/MAD 로 검정통계량 R_i 를 만들고, t-분포 임계값
    λ_i 와 비교한다. 최종 이상점 수는 "R_i > λ_i 인 최대 i"(ESD 정의) — 중간에
    한 번 임계 미달이 나와도 뒤 반복에서 초과하면 앞 후보까지 전부 이상점이다
    (swamping 이 아니라 masking 해소 — Rosner 1983).

    상수 수열(scale=0)은 t-검정이 정의되지 않으므로, 중앙값과 다른 값을
    앞에서부터 최대 max_outliers 개 이상점으로 처리한다(무한 편차 케이스).
    """
    from scipy.stats import t as t_dist

    n = len(residuals)
    if n < _MIN_POINTS_FOR_GESD or max_outliers < 1:
        return []
    # 자유도 n-i-1 >= 1 보장 — i 는 1..max_outliers.
    max_outliers = min(max_outliers, n - 2)

    remaining = list(enumerate(residuals))
    candidates: list[int] = []
    last_exceeding = 0
    for i in range(1, max_outliers + 1):
        values = [v for _, v in remaining]
        center = _median(values)
        scale = _robust_scale(values, center)
        if scale == 0.0:
            # 상수 수열 — 중앙값과 다른 값만 무한 편차 이상점으로 채택하고 종료.
            for idx, v in remaining:
                if v != center and len(candidates) < max_outliers:
                    candidates.append(idx)
            last_exceeding = len(candidates)
            break
        far_pos = max(range(len(remaining)), key=lambda k: abs(remaining[k][1] - center))
        far_idx, far_value = remaining[far_pos]
        r_stat = abs(far_value - center) / scale
        m = len(remaining)
        p = 1.0 - alpha / (2.0 * m)
        t_crit = float(t_dist.ppf(p, m - 2))
        lam = (m - 1) * t_crit / math.sqrt((m - 2 + t_crit**2) * m)
        candidates.append(far_idx)
        if r_stat > lam:
            last_exceeding = i
        remaining.pop(far_pos)

    return candidates[:last_exceeding]


def detect_seasonal_anomalies(
    dates: list[str],
    values: list[float],
    *,
    period: int,
    alpha: float,
    max_anomalies_ratio: float,
    min_history_for_stl: int,
) -> SeasonalAnomalyDetection:
    """일별 시계열에서 계절성 인지 이상점을 검출한다 (S-H-ESD).

    이력이 min_history_for_stl 이상이면 STL(period, robust) 분해 잔차에,
    미만이면 원값의 중앙값 편차에 GESD 를 건다(계절 미조정 폴백 — 어느 분기를
    탔는지는 반환값 ``seasonal_adjusted`` 가 알린다, #512).

    [무매출 규칙 계승(#194)] 값이 0 인 포인트는 이상으로 판정하지 않는다 —
    저볼륨 판매자의 무판매일이 전부 급락 판정되는 노이즈 방지. 반대로 무매출
    이력(전부 0) 직후의 매출 발생은 상수 수열의 무한 편차로 검출된다(sigma=inf).

    [#512] 표본이 _MIN_POINTS_FOR_GESD 미만이면 검정 자체가 불능이라
    ``decided=False`` 로 돌려준다 — 종전엔 이 경우와 "검정했고 이상 0건"이
    똑같이 빈 리스트라, 호출부가 표본 2개짜리 확정적 "이상 감지 없음"을
    내보내고 있었다. 실패를 기본값(빈 리스트)으로 환원하지 않는다.

    anomalies 는 날짜 오름차순. expected(STL 재구성 기대값)가 0 이하면 편차%는
    정의 불가라 deviation_pct=None 이다(0 나눗셈 위장 금지).
    """
    if len(dates) != len(values):
        raise ValueError(f"dates({len(dates)})/values({len(values)}) 길이가 다르다")
    n = len(values)
    if n < _MIN_POINTS_FOR_GESD:
        # 검정 불능 — "이상 없음"이 아니라 판정 보류. decided=False 가 그 구분이다.
        return SeasonalAnomalyDetection(
            anomalies=[],
            decided=False,
            sample_size=n,
            min_samples=_MIN_POINTS_FOR_GESD,
            seasonal_adjusted=False,
        )

    seasonal_adjusted = n >= min_history_for_stl
    if seasonal_adjusted:
        # STL 은 statsmodels 구현(Cleveland 1990 원 알고리즘, robust=True 로
        # 이상점의 계절/추세 오염을 억제). 결정론 — 같은 입력 = 같은 분해.
        import pandas as pd
        from statsmodels.tsa.seasonal import STL

        fit = STL(pd.Series(values, dtype=float), period=period, robust=True).fit()
        expected_series = [float(t + s) for t, s in zip(fit.trend, fit.seasonal, strict=True)]
        residuals = [v - e for v, e in zip(values, expected_series, strict=True)]
    else:
        center = _median(values)
        expected_series = [center] * n
        residuals = [v - center for v in values]

    # 부동소수 먼지 제거 — 값 스케일 대비 무의미한 잔차는 0 으로 눌러 GESD 가
    # 수치 오차를 이상으로 오탐하지 않게 한다(완전 규칙 시계열의 degenerate 케이스).
    noise_floor = _RESIDUAL_NOISE_FLOOR_RATIO * max(1.0, max(abs(v) for v in values))
    residuals = [0.0 if abs(r) < noise_floor else r for r in residuals]

    max_outliers = max(1, math.floor(n * max_anomalies_ratio))
    flagged = _gesd_outlier_indices(residuals, alpha=alpha, max_outliers=max_outliers)

    resid_center = _median(residuals)
    resid_scale = _robust_scale(residuals, resid_center)
    anomalies: list[SeasonalAnomaly] = []
    for idx in flagged:
        if values[idx] == 0:
            continue  # 무매출 포인트는 판정 제외(#194 규칙 3)
        expected = expected_series[idx]
        deviation_pct = (values[idx] - expected) / expected * 100.0 if expected > 0 else None
        sigma = abs(residuals[idx] - resid_center) / resid_scale if resid_scale > 0 else math.inf
        anomalies.append(
            SeasonalAnomaly(
                date=dates[idx],
                actual=values[idx],
                expected=expected,
                deviation_pct=deviation_pct,
                sigma=sigma,
                direction="drop" if residuals[idx] < resid_center else "spike",
            )
        )
    anomalies.sort(key=lambda a: a.date)
    return SeasonalAnomalyDetection(
        anomalies=anomalies,
        decided=True,
        sample_size=n,
        min_samples=_MIN_POINTS_FOR_GESD,
        seasonal_adjusted=seasonal_adjusted,
    )
