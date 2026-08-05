"""analysis/outliers.py — MAD 스파이크·Tukey fence·심야 비중 테스트 (이슈 #290).

논문 재현: 스파이크 주입 → MAD 검출(상방만). Tukey 상위 fence·심야 비중 산식과
경계(표본 부족 = 판정 보류)·결정론을 검증한다.
"""

from __future__ import annotations

import pytest

from app.agents.seller.analysis import outliers


def test_mad_detects_injected_spike_only() -> None:
    """논문 재현: 평탄 시계열에 급증 1건 주입 → 그 날만 검출된다."""
    targets = [f"d{i}" for i in range(10)]
    values = [100.0, 105.0, 98.0, 102.0, 99.0, 101.0, 97.0, 103.0, 100.0, 900.0]
    flags = outliers.mad_spikes(targets, values, threshold=3.5, metric="daily")
    assert [f.target for f in flags] == ["d9"]
    assert flags[0].type == "point"
    assert flags[0].value >= 3.5  # robust z 가 임계 이상


def test_mad_ignores_downward_drops() -> None:
    """하방(0건 날)은 트래픽 부재지 어뷰징 신호가 아니다 — 상방만 표기한다."""
    targets = [f"d{i}" for i in range(8)]
    values = [100.0] * 7 + [0.0]
    assert outliers.mad_spikes(targets, values, threshold=3.5, metric="daily") == []


def test_mad_constant_series_flags_only_exceeding_values() -> None:
    """상수 수열 + 급증은 무한 편차로 검출된다(무변동 이력 직후 봇 유입 케이스)."""
    targets = [f"d{i}" for i in range(6)]
    values = [0.0] * 5 + [500.0]
    flags = outliers.mad_spikes(targets, values, threshold=3.5, metric="daily")
    assert [f.target for f in flags] == ["d5"]


def test_mad_too_few_points_returns_empty() -> None:
    """3점 미만은 판정 보류 — 이상 없음이 아니라 검정 불능(빈 결과)."""
    assert outliers.mad_spikes(["d0", "d1"], [1.0, 999.0], threshold=3.5, metric="m") == []
    with pytest.raises(ValueError):
        outliers.mad_spikes(["d0"], [1.0, 2.0], threshold=3.5, metric="m")


def test_tukey_flags_upper_fence_only() -> None:
    """상위 fence(Q3+1.5×IQR) 초과만 표기 — 하위(저활동)는 어뷰징 신호가 아니다."""
    items = [(f"[{i}]", v) for i, v in enumerate([10.0, 12.0, 11.0, 13.0, 9.0, 60.0, 0.1])]
    flags = outliers.tukey_upper_outliers(items, k=1.5, metric="조회/구매")
    assert [f.target for f in flags] == ["[5]"]
    assert flags[0].type == "contextual"
    assert flags[0].value == 60.0
    assert flags[0].value > flags[0].threshold  # value 는 원 비율, threshold 는 fence


def test_tukey_few_items_returns_empty() -> None:
    """4개 미만은 사분위수 불능 — 판정 보류(빈 결과)."""
    assert outliers.tukey_upper_outliers([("a", 1.0), ("b", 99.0)], k=1.5, metric="m") == []


def test_tukey_zero_iqr_flags_deviants() -> None:
    """IQR=0(대부분 동일 비율)이면 fence=Q3 — 동일 다수에서 벗어난 큰 값만 걸린다."""
    items = [(f"[{i}]", 5.0) for i in range(8)] + [("[hot]", 50.0)]
    flags = outliers.tukey_upper_outliers(items, k=1.5, metric="m")
    assert [f.target for f in flags] == [("[hot]")]


def test_night_activity_share_computes_ratio() -> None:
    """I-8 hour rows({key,count})에서 심야 [0,6) 비중을 계산한다."""
    rows = [
        {"key": 0, "count": 30},
        {"key": 3, "count": 20},
        {"key": 6, "count": 10},  # end 경계 — 심야 제외
        {"key": 14, "count": 40},
    ]
    share = outliers.night_activity_share(rows, start=0, end=6)
    assert share == (0.5, 50, 100)


def test_night_activity_share_holds_judgment_without_valid_rows() -> None:
    """총 0건·해석 불가 행뿐이면 None — 0% 로 위장하지 않는다(판정 보류)."""
    assert outliers.night_activity_share([], start=0, end=6) is None
    assert outliers.night_activity_share([{"key": "?", "count": "x"}], start=0, end=6) is None
    # 해석 불가 행이 섞여도 유효 행만으로 계산한다(관대 수신).
    mixed = [{"key": "bad"}, {"key": 1, "count": 5}, {"key": 12, "count": 5}]
    assert outliers.night_activity_share(mixed, start=0, end=6) == (0.5, 5, 10)


def test_deterministic_same_input_same_output() -> None:
    """결정론(§10-②): 같은 입력 2회 호출은 완전히 같은 결과다."""
    targets = [f"d{i}" for i in range(12)]
    values = [float(100 + (i * 7) % 13) for i in range(11)] + [777.0]
    first = outliers.mad_spikes(targets, values, threshold=3.5, metric="daily")
    second = outliers.mad_spikes(targets, values, threshold=3.5, metric="daily")
    assert first == second
