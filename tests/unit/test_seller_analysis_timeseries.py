"""analysis/timeseries.py — STL+GESD (S-H-ESD) 검정 테스트 (이슈 #290).

논문 재현(합성 시계열에 이상 주입 → 그 지점만 검출)·경계·결정론을 검증한다.
구 calc.detect_sales_anomalies(SMA) 테스트의 무매출 규칙 3종도 여기서 계승한다.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from app.agents.seller.analysis import timeseries

# 기본 튜너블(Settings 기본값과 동일) — 모듈은 주입만 받으므로 테스트가 명시한다.
_PARAMS = {
    "period": 7,
    "alpha": 0.05,
    "max_anomalies_ratio": 0.2,
    "min_history_for_stl": 14,
}


def _dates(n: int, start: str = "2026-07-01") -> list[str]:
    first = date.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(n)]


def _weekly_series(n: int, *, weekday_sales: float = 100_000.0, weekend_ratio: float = 0.4):
    """요일 계절성 합성 시계열 — 월~금 weekday_sales, 토·일은 weekend_ratio 배.

    2026-07-01 은 수요일. SMA 방식이라면 매주 토요일(-60%)이 오탐으로 걸린다.
    """
    dates = _dates(n)
    values = []
    for d in dates:
        weekday = date.fromisoformat(d).weekday()
        values.append(weekday_sales * (weekend_ratio if weekday >= 5 else 1.0))
    return dates, values


def test_reproduces_paper_case_injected_drop_detected_without_weekend_false_positives() -> None:
    """논문 재현(핵심): 요일 계절성 + 평일 -40% 주입 → 그 날만 검출, 주말 오탐 0.

    구 SMA(직전 평균 ±30%)는 모든 토·일(-60%)을 급락으로 오탐했다 — STL 이 요일
    효과를 분해로 걷어내므로 "주말이라 원래 낮음"은 이상이 아니다(worker-papers.md).
    """
    dates, values = _weekly_series(42)  # lookback 28 + 요청 14 상당
    target = dates[35]  # 평일(2026-08-05 수) — 주입 지점
    assert date.fromisoformat(target).weekday() < 5, "주입 지점은 평일이어야 한다"
    values[35] *= 0.6  # -40%

    anomalies = timeseries.detect_seasonal_anomalies(dates, values, **_PARAMS).anomalies

    flagged_dates = {a.date for a in anomalies}
    assert target in flagged_dates
    weekend_dates = {d for d in dates if date.fromisoformat(d).weekday() >= 5}
    assert not (flagged_dates & weekend_dates), "주말 정상 저매출이 오탐되면 안 된다"
    hit = next(a for a in anomalies if a.date == target)
    assert hit.direction == "drop"
    assert hit.deviation_pct is not None and -55.0 < hit.deviation_pct < -25.0
    assert hit.sigma > 0
    assert hit.expected > hit.actual


def test_clean_seasonal_series_has_no_anomalies() -> None:
    """주입 없는 순수 계절 시계열은 이상 0건 — 검정 유의수준이 오탐을 통제한다."""
    dates, values = _weekly_series(42)
    detection = timeseries.detect_seasonal_anomalies(dates, values, **_PARAMS)
    assert detection.decided is True, "42점은 검정 가능 — 보류가 아니라 이상 0건이다"
    assert detection.anomalies == []


def test_deterministic_same_input_same_output() -> None:
    """결정론(§10-②): 같은 입력 2회 호출은 완전히 같은 결과다."""
    dates, values = _weekly_series(35)
    values[20] *= 3.0
    first = timeseries.detect_seasonal_anomalies(dates, values, **_PARAMS)
    second = timeseries.detect_seasonal_anomalies(dates, values, **_PARAMS)
    assert first == second
    assert len(first.anomalies) >= 1


def test_short_history_falls_back_to_robust_detection() -> None:
    """이력 < min_history_for_stl 이면 STL 생략(계절 미조정 robust 판정)으로도 급증을 잡는다."""
    dates = _dates(8)
    values = [100.0] * 7 + [10_000.0]
    anomalies = timeseries.detect_seasonal_anomalies(dates, values, **_PARAMS).anomalies
    assert [a.date for a in anomalies] == [dates[7]]
    assert anomalies[0].direction == "spike"


def test_zero_sales_point_never_flagged() -> None:
    """[#194 규칙 계승] 값 0 포인트는 급락 신호여도 판정하지 않는다(무판매일 노이즈 방지)."""
    dates = _dates(10)
    values = [100.0] * 9 + [0.0]
    anomalies = timeseries.detect_seasonal_anomalies(dates, values, **_PARAMS).anomalies
    assert all(a.actual != 0 for a in anomalies)
    assert dates[9] not in {a.date for a in anomalies}


def test_sales_after_all_zero_history_is_anomaly_with_undefined_pct() -> None:
    """[#194 규칙 계승] 무매출 이력 직후 매출 발생 = 이상 — 기대값 0 이라 편차%는
    None(0 나눗셈 위장 금지), σ는 MeanAD 폴백으로 강한 신호(>3)가 잡힌다."""
    dates = _dates(10)
    values = [0.0] * 9 + [50_000.0]
    anomalies = timeseries.detect_seasonal_anomalies(dates, values, **_PARAMS).anomalies
    assert [a.date for a in anomalies] == [dates[9]]
    assert anomalies[0].sigma > 3.0
    assert anomalies[0].deviation_pct is None
    # 전부 0 이면 정상(발생 자체가 없다).
    assert timeseries.detect_seasonal_anomalies(dates, [0.0] * 10, **_PARAMS).anomalies == []


def test_max_anomalies_ratio_caps_flag_count() -> None:
    """이상점 수는 기간의 max_anomalies_ratio 를 넘지 않는다(GESD 상한 — S-H-ESD §3)."""
    dates, values = _weekly_series(30)
    for i in (3, 8, 15, 22, 24, 28):  # 6건 주입 — 상한 floor(30×0.2)=6 경계
        values[i] *= 4.0
    anomalies = timeseries.detect_seasonal_anomalies(dates, values, **_PARAMS).anomalies
    assert len(anomalies) <= math.floor(30 * _PARAMS["max_anomalies_ratio"])


def test_injected_alpha_changes_verdict() -> None:
    """[이관: test_calc_uses_injected_thresholds] 다른 α를 주입하면 판정이 달라진다 —
    임계값 하드코딩 부재 확인(Settings 주입 원칙).

    마지막 값 108 은 robust z 로 경계급(≈2.5σ) — GESD 임계 λ 는 엄격 α(1e-7)에서
    ≈2.8, 느슨한 α(0.4)에서 ≈1.9 라 이 사이 크기만 판정이 갈린다.
    """
    dates = _dates(10)
    values = [100.0, 103.0, 97.0, 101.0, 99.0, 102.0, 98.0, 100.0, 104.0, 108.0]
    strict = timeseries.detect_seasonal_anomalies(
        dates, values, period=7, alpha=1e-7, max_anomalies_ratio=0.2, min_history_for_stl=14
    ).anomalies
    lenient = timeseries.detect_seasonal_anomalies(
        dates, values, period=7, alpha=0.4, max_anomalies_ratio=0.2, min_history_for_stl=14
    ).anomalies
    assert dates[9] not in {a.date for a in strict}
    assert dates[9] in {a.date for a in lenient}


def test_mismatched_lengths_raise_value_error() -> None:
    """dates/values 길이 불일치는 호출부 프로그래밍 오류 — ValueError."""
    with pytest.raises(ValueError):
        timeseries.detect_seasonal_anomalies(["2026-07-01"], [1.0, 2.0], **_PARAMS)


def test_too_few_points_is_undecided_not_clean() -> None:
    """[#512] 3점 미만은 검정 불능 — 빈 목록이 아니라 decided=False 로 판정 보류를 알린다.

    종전엔 이 경우와 "검정했고 이상 0건"이 똑같이 `[]` 라, 호출부가 표본 2개짜리
    확정적 "이상 감지 없음"을 판매자에게 내보내고 있었다.
    """
    detection = timeseries.detect_seasonal_anomalies(_dates(2), [1.0, 900.0], **_PARAMS)
    assert detection.decided is False
    assert detection.anomalies == []
    assert detection.sample_size == 2
    assert detection.min_samples == 3


def test_minimum_sample_is_decided() -> None:
    """[#512] 경계 — 정확히 3점이면 검정 가능(decided=True)이다."""
    detection = timeseries.detect_seasonal_anomalies(_dates(3), [100.0, 101.0, 99.0], **_PARAMS)
    assert detection.decided is True
    assert detection.sample_size == 3


def test_seasonal_adjusted_flag_follows_stl_branch() -> None:
    """[#512] 계절조정 여부는 판정 모듈이 직접 알린다 — 호출부가 임계를 재계산하지 않도록.

    min_history_for_stl=14 경계에서 뒤집힌다(13점=robust 폴백, 14점=STL).
    """
    short = timeseries.detect_seasonal_anomalies(_dates(13), [100.0] * 13, **_PARAMS)
    long = timeseries.detect_seasonal_anomalies(_dates(14), [100.0] * 14, **_PARAMS)
    assert short.seasonal_adjusted is False
    assert long.seasonal_adjusted is True


def test_ignores_spring_reference_flags_by_construction() -> None:
    """[§0.1 D 계승] 입력이 원시 값 배열이라 Spring isAnomaly/deviationPct 참고치가
    끼어들 자리가 구조적으로 없다 — 도구 층(tools.py)이 sales 원시값만 넘긴다."""
    dates = _dates(20)
    values = [100.0] * 19 + [400.0]
    anomalies = timeseries.detect_seasonal_anomalies(dates, values, **_PARAMS).anomalies
    assert dates[19] in {a.date for a in anomalies}
