"""analysis/proportions.py — Wilson CI + two-proportion z-검정 테스트 (이슈 #290).

핵심 재현: 동일 모비율 두 표본 → 비유의 / 실제 차이 → 유의 / 저볼륨 큰 낙폭 →
비유의(구 drop_pct 임계가 내던 저볼륨 오탐을 통제). 결정론·경계도 검증한다.
"""

from __future__ import annotations

import pytest

from app.agents.seller.analysis import proportions


def test_wilson_interval_known_value() -> None:
    """교과서 검증값: 3/10, 95% → 대략 [0.108, 0.603] (Wilson score)."""
    est = proportions.wilson_interval(3, 10, confidence=0.95)
    assert est.rate == pytest.approx(0.3)
    assert est.ci_low == pytest.approx(0.1078, abs=0.001)
    assert est.ci_high == pytest.approx(0.6032, abs=0.001)
    assert est.successes == 3 and est.trials == 10  # 원수치 보존(judge 검증용)


def test_wilson_interval_extremes_stay_in_unit_range() -> None:
    """0%·100% 극단에서도 구간이 [0,1] 안이고 폭이 0 으로 붕괴하지 않는다(Wald 와의 차이)."""
    zero = proportions.wilson_interval(0, 20, confidence=0.95)
    full = proportions.wilson_interval(20, 20, confidence=0.95)
    assert zero.ci_low == 0.0 and zero.ci_high > 0.0
    assert full.ci_high == 1.0 and full.ci_low < 1.0


def test_wilson_interval_zero_trials_raises() -> None:
    """표본 0 은 비율 정의 불가 — ValueError(0% 위장 금지, 호출부가 보류 표기)."""
    with pytest.raises(ValueError):
        proportions.wilson_interval(0, 0, confidence=0.95)
    with pytest.raises(ValueError):
        proportions.wilson_interval(5, 3, confidence=0.95)  # successes > trials


def test_same_population_rates_not_significant() -> None:
    """논문 재현: 동일 모비율 두 표본(10%) → 유의하지 않음."""
    comparison = proportions.compare_rates(100, 1000, 98, 1000, alpha=0.05, confidence=0.95)
    assert comparison.verdict == "no_significant_change"
    assert comparison.p_value > 0.05


def test_real_difference_is_significant_drop() -> None:
    """논문 재현: 실제 차이(10% → 5%, n=1000) → 유의한 하락."""
    comparison = proportions.compare_rates(50, 1000, 100, 1000, alpha=0.05, confidence=0.95)
    assert comparison.verdict == "significant_drop"
    assert comparison.p_value < 0.05


def test_low_volume_large_drop_is_not_significant() -> None:
    """저볼륨 오탐 통제(#290 의 목적): 30%→10% 낙폭이라도 n=10 이면 비유의.

    구 drop_pct=20% 단순 임계는 이 케이스를 무조건 '하락 이상'으로 오탐했다.
    """
    comparison = proportions.compare_rates(1, 10, 3, 10, alpha=0.05, confidence=0.95)
    assert comparison.verdict == "no_significant_change"
    # 같은 비율 차이라도 표본이 크면 유의해진다 — 표본 크기가 판정을 가른다.
    scaled = proportions.compare_rates(100, 1000, 300, 1000, alpha=0.05, confidence=0.95)
    assert scaled.verdict == "significant_drop"


def test_rise_direction_verdict() -> None:
    """상승 방향도 유의성 판정된다(3분류 — 하락/상승/변화없음)."""
    comparison = proportions.compare_rates(300, 1000, 100, 1000, alpha=0.05, confidence=0.95)
    assert comparison.verdict == "significant_rise"


def test_pooled_extremes_are_no_change() -> None:
    """두 기간 모두 0%(또는 100%)면 분산 0 — z 정의 불가지만 비율 동일이므로 p=1.0."""
    both_zero = proportions.compare_rates(0, 50, 0, 40, alpha=0.05, confidence=0.95)
    assert both_zero.p_value == 1.0
    assert both_zero.verdict == "no_significant_change"
    both_full = proportions.compare_rates(50, 50, 40, 40, alpha=0.05, confidence=0.95)
    assert both_full.verdict == "no_significant_change"


def test_injected_alpha_changes_verdict() -> None:
    """다른 α 주입 → 판정이 달라진다(하드코딩 부재 — Settings 주입 원칙)."""
    strict = proportions.compare_rates(80, 1000, 100, 1000, alpha=0.01, confidence=0.95)
    lenient = proportions.compare_rates(80, 1000, 100, 1000, alpha=0.20, confidence=0.95)
    assert strict.verdict == "no_significant_change"
    assert lenient.verdict == "significant_drop"
    with pytest.raises(ValueError):
        proportions.compare_rates(1, 10, 1, 10, alpha=0.0, confidence=0.95)


def test_deterministic_same_input_same_output() -> None:
    """결정론(§10-②): 같은 입력 2회 호출은 완전히 같은 결과다."""
    first = proportions.compare_rates(37, 412, 55, 391, alpha=0.05, confidence=0.95)
    second = proportions.compare_rates(37, 412, 55, 391, alpha=0.05, confidence=0.95)
    assert first == second
