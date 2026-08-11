"""analysis/scan.py — 트리거 7종 판정 테스트 (이슈 #595).

이 파일이 지키는 명제는 하나다: **고정 임계 AND 통계 유의, 한쪽만으로는 발동하지 않는다**
(`10-TRIGGER` 결정 94). 트리거마다 "임계만" · "유의만" · "둘 다" 세 축을 세워 그것을
증명하고, 임계·alpha 를 주입으로 바꿔 판정이 뒤집히는 것으로 하드코딩이 없음을 보인다.

판정 보류(`decided=False`)와 미발동(`fired=False`)을 가르는 축도 함께 본다 — 그 둘을
뭉개면 "표본이 부족해 못 봤다"가 "봤는데 정상이다"로 둔갑한다(#512 규약 승계).
"""

from __future__ import annotations

import pytest

from app.agents.seller.analysis import scan

# 기본 튜너블(Settings 기본값과 동일) — 모듈은 주입만 받으므로 테스트가 명시한다.
_BASE = {
    "sales_pct": 0.05,
    "conversion_pct": 0.10,
    "product_drop_pct": 0.30,
    "cart_abandon_pp": 0.10,
    "new_customer_drop_pct": 0.30,
    "repurchase_drop_pp": 0.10,
    "baseline_days": 7,
    "lookback_days": 28,
    "rate_alpha": 0.05,
    "wilson_confidence": 0.95,
}


def _thresholds(**overrides) -> scan.TriggerThresholds:
    return scan.TriggerThresholds(**{**_BASE, **overrides})


def _series(
    baseline_sales: float, baseline_orders: int, current_sales: float, current_orders: int
) -> tuple[list[str], list[float], list[int]]:
    """14일 시계열 — 앞 7일이 기준 구간, 뒤 7일이 현재 구간(일별 값은 균등 배분).

    2024-01-01 은 월요일이라 두 구간의 요일 구성이 같다 — 요일 효과가 비교에 섞이지
    않게 하는 앵커다(시뮬레이션 생성기와 같은 시작일 규약).
    """
    dates = [f"2024-01-{day:02d}" for day in range(1, 15)]
    sales = [baseline_sales] * 7 + [current_sales] * 7
    orders = [baseline_orders] * 7 + [current_orders] * 7
    return dates, sales, orders


def _sales(thresholds: scan.TriggerThresholds, *args) -> scan.TriggerEvaluation:
    dates, sales, orders = _series(*args)
    return scan.evaluate_sales_trigger(
        dates, sales, orders, target_date=dates[-1], thresholds=thresholds
    )


# ── 트리거 1: 매출 ────────────────────────────────────────────────────────────


def test_sales_threshold_only_does_not_fire() -> None:
    """매출 금액은 −20% 인데 주문 건수가 유의하지 않으면 발동하지 않는다.

    두 축을 나눈 설계의 요점 — 단가가 흔들려 금액만 움직인 날에 검정이 제동을 건다.
    """
    result = _sales(_thresholds(), 1000.0, 4, 800.0, 4)
    assert result.decided
    assert result.threshold_met
    assert result.significant is False
    assert not result.fired


def test_sales_significant_only_does_not_fire() -> None:
    """주문 건수는 반토막인데 금액 변화가 임계 미만이면 발동하지 않는다(객단가 상승)."""
    result = _sales(_thresholds(), 1000.0, 50, 1020.0, 25)
    assert result.decided
    assert result.significant
    assert result.threshold_met is False
    assert not result.fired


def test_sales_both_conditions_fire() -> None:
    """금액도 임계를 넘고 건수도 유의하면 발동한다."""
    result = _sales(_thresholds(), 1000.0, 50, 500.0, 25)
    assert result.fired
    assert result.direction == "drop"
    assert result.change == pytest.approx(-0.5)
    assert result.basis == "i6_order_count"


def test_sales_injected_threshold_changes_verdict() -> None:
    """임계를 주입으로 올리면 같은 입력이 미발동으로 뒤집힌다(하드코딩 없음)."""
    args = (1000.0, 50, 900.0, 25)  # 금액 −10%
    assert _sales(_thresholds(sales_pct=0.05), *args).fired
    assert not _sales(_thresholds(sales_pct=0.20), *args).fired


def test_sales_injected_alpha_changes_verdict() -> None:
    """alpha 를 주입으로 낮추면 유의 판정이 뒤집힌다."""
    args = (1000.0, 20, 500.0, 10)
    assert _sales(_thresholds(rate_alpha=0.5), *args).significant
    assert not _sales(_thresholds(rate_alpha=1e-9), *args).significant


def test_sales_insufficient_history_is_undecided() -> None:
    """[#512 규약 승계] 2× baseline_days 이력이 없으면 보류다 — "이상 없음"이 아니다."""
    dates = ["2024-01-01", "2024-01-02"]
    result = scan.evaluate_sales_trigger(
        dates, [100.0, 90.0], [3, 2], target_date=dates[-1], thresholds=_thresholds()
    )
    assert not result.decided
    assert not result.fired
    assert result.hold_reason is not None and result.hold_reason.startswith("no_baseline")


def test_sales_zero_baseline_keeps_change_undefined() -> None:
    """무매출 이력 직후 매출 발생 — 변화율은 정의 불가(None)지만 임계는 통과한다."""
    result = _sales(_thresholds(), 0.0, 0, 1000.0, 40)
    assert result.decided
    assert result.threshold_met
    assert result.change is None  # 0 나눗셈을 0% 로 위장하지 않는다
    assert result.direction == "spike"
    assert result.fired


def test_sales_both_windows_empty_is_undecided() -> None:
    """두 구간 모두 매출이 없으면 변화 자체가 정의되지 않는다 — 보류."""
    result = _sales(_thresholds(), 0.0, 0, 0.0, 0)
    assert not result.decided
    assert result.hold_reason is not None and result.hold_reason.startswith("no_sales")


def test_sales_length_mismatch_raises() -> None:
    """길이가 어긋난 입력은 프로그래머 오류라 조용히 넘기지 않는다."""
    with pytest.raises(ValueError):
        scan.evaluate_sales_trigger(
            ["2024-01-01"], [1.0, 2.0], [1], target_date="2024-01-01", thresholds=_thresholds()
        )


def test_sales_deterministic_same_input_same_output() -> None:
    """결정론(§10-②) — 같은 입력이면 같은 판정."""
    args = (1000.0, 50, 500.0, 25)
    assert _sales(_thresholds(), *args) == _sales(_thresholds(), *args)


# ── 트리거 2: 전환율 ──────────────────────────────────────────────────────────


def _conversion(thresholds: scan.TriggerThresholds, cv, cp, bv, bp) -> scan.TriggerEvaluation:
    return scan.evaluate_conversion_trigger(
        current_view=cv,
        current_purchase=cp,
        baseline_view=bv,
        baseline_purchase=bp,
        thresholds=thresholds,
    )


def test_conversion_threshold_only_does_not_fire() -> None:
    """저볼륨의 큰 변동 — 임계는 넘지만 유의하지 않다(저볼륨 오탐 통제)."""
    result = _conversion(_thresholds(), 40, 1, 280, 9)
    assert result.threshold_met
    assert not result.significant
    assert not result.fired


def test_conversion_significant_only_does_not_fire() -> None:
    """대량 표본의 작은 변동 — 유의하지만 임계 미달이라 발동하지 않는다."""
    result = _conversion(_thresholds(), 200_000, 6400, 200_000, 6000)
    assert result.significant
    assert not result.threshold_met
    assert not result.fired


def test_conversion_both_conditions_fire() -> None:
    """3.0% → 4.8%(n=3000) 는 둘 다 넘어 발동한다."""
    result = _conversion(_thresholds(), 3000, 144, 3000, 90)
    assert result.fired
    assert result.direction == "spike"


def test_conversion_uncomputable_stage_is_undecided() -> None:
    """count=null·computable=false 는 "0건"이 아니다(`02` §4) — 보류."""
    result = scan.evaluate_conversion_trigger(
        current_view=3000,
        current_purchase=144,
        baseline_view=3000,
        baseline_purchase=90,
        thresholds=_thresholds(),
        uncomputable_stages=("view",),
    )
    assert not result.decided
    assert result.hold_reason is not None and result.hold_reason.startswith("uncomputable_stage")


def test_conversion_zero_trials_is_undecided() -> None:
    """표본 0 은 비율이 정의되지 않는다 — 0% 로 위장하지 않고 보류."""
    result = _conversion(_thresholds(), 0, 0, 3000, 90)
    assert not result.decided


def test_conversion_inconsistent_counts_is_undecided() -> None:
    """purchase > view 역전(이벤트 카운트 계약의 실제 사례)은 clamp 하지 않고 보류한다."""
    result = _conversion(_thresholds(), 100, 150, 3000, 90)
    assert not result.decided
    assert result.hold_reason is not None and "inconsistent_counts" in result.hold_reason


# ── 트리거 7: 이상 주문 (AND 예외) ────────────────────────────────────────────


def test_abuse_uses_backend_flag_without_statistics() -> None:
    """BE `isSuspicious` 를 그대로 쓴다(결정 103) — 검정을 붙일 대상이 없다."""
    result = scan.evaluate_abuse_trigger(
        suspicious_current=1, suspicious_baseline=0, thresholds=_thresholds()
    )
    assert result.fired
    assert result.significant is None  # 검정을 돌리지 않았다는 표기
    assert result.method == "be_flag"


def test_abuse_does_not_fire_without_suspicious_members() -> None:
    result = scan.evaluate_abuse_trigger(
        suspicious_current=0, suspicious_baseline=0, thresholds=_thresholds()
    )
    assert not result.fired
    assert result.decided


# ── 트리거 3~6: 티어2 ─────────────────────────────────────────────────────────


def test_product_sales_threshold_only_does_not_fire() -> None:
    """조회가 적은 상품의 −50% 는 임계를 넘어도 유의하지 않다."""
    result = scan.evaluate_product_sales_trigger(
        {1: (2, 40)}, {1: (28, 280)}, thresholds=_thresholds()
    )
    assert result.threshold_met
    assert not result.significant
    assert not result.fired


def test_product_sales_both_conditions_fire() -> None:
    """표본이 충분한 −50% 급감은 발동하고, 최악 상품 id 를 근거로 남긴다."""
    result = scan.evaluate_product_sales_trigger(
        {1: (50, 2000), 2: (60, 2000)},
        {1: (700, 14000), 2: (420, 14000)},
        thresholds=_thresholds(),
    )
    assert result.fired
    assert result.detail["fired_count"] >= 1
    assert result.detail["worst_product_id"] in (1.0, 2.0)


def test_product_sales_only_common_products_are_judged() -> None:
    """한쪽 기간에만 있는 상품은 판정하지 않는다 — 신규·단종을 급감으로 읽지 않는다."""
    rows = scan.evaluate_product_sales_rows(
        {1: (50, 2000), 9: (0, 100)}, {1: (700, 14000)}, thresholds=_thresholds()
    )
    assert [row.detail["product_id"] for row in rows] == [1.0]


def test_cart_abandon_uses_percentage_points() -> None:
    """이탈률 임계는 퍼센트포인트다 — 상대 변화율로 재면 저이탈 브랜드가 상시 발동한다."""
    result = scan.evaluate_cart_abandon_trigger(
        current_removes=160,
        current_adds=500,
        baseline_removes=700,
        baseline_adds=3500,
        thresholds=_thresholds(),
    )
    assert result.change_unit == "pp"
    assert result.change == pytest.approx(0.12)
    assert result.fired


def test_cart_abandon_ignores_improvement() -> None:
    """이탈률이 **개선**되면 발동하지 않는다 — 상승 전용 트리거다."""
    result = scan.evaluate_cart_abandon_trigger(
        current_removes=40,
        current_adds=500,
        baseline_removes=700,
        baseline_adds=3500,
        thresholds=_thresholds(),
    )
    assert not result.threshold_met
    assert not result.fired


def test_cart_abandon_boundary_blocks_judgement() -> None:
    """편입일을 가로지르면 같은 입력이라도 판정하지 않는다."""
    result = scan.evaluate_cart_abandon_trigger(
        current_removes=160,
        current_adds=500,
        baseline_removes=700,
        baseline_adds=3500,
        thresholds=_thresholds(),
        boundary_blocked=True,
    )
    assert not result.decided
    assert result.hold_reason is not None
    assert result.hold_reason.startswith("remove_from_cart_boundary")


def test_new_customer_uses_poisson_rate_test() -> None:
    """신규 고객은 분모가 없어 포아송 검정을 쓴다 — 기대값은 기준 구간 일평균이다."""
    result = scan.evaluate_new_customer_trigger(
        current_signups=10, baseline_signups=200, thresholds=_thresholds()
    )
    assert result.method == "poisson_rate"
    assert result.detail["expected"] == pytest.approx(200 / 7)
    assert result.fired


def test_new_customer_threshold_only_does_not_fire() -> None:
    """−30% 딱 맞는 감소는 임계를 넘어도 저빈도에서는 유의하지 않다."""
    result = scan.evaluate_new_customer_trigger(
        current_signups=20, baseline_signups=200, thresholds=_thresholds()
    )
    assert result.threshold_met
    assert not result.significant
    assert not result.fired


def test_new_customer_without_baseline_is_undecided() -> None:
    """기준 구간 가입이 0 이면 감소율이 정의되지 않는다 — 보류."""
    result = scan.evaluate_new_customer_trigger(
        current_signups=5, baseline_signups=0, thresholds=_thresholds()
    )
    assert not result.decided


def test_repurchase_records_i14_basis() -> None:
    """재구매율 정의를 각인한다(결정 101) — I-38 이 붙어 값이 달라져도 버그가 아니다."""
    result = scan.evaluate_repurchase_trigger(
        current_repeat_members=60,
        current_members=300,
        baseline_repeat_members=672,
        baseline_members=2100,
        thresholds=_thresholds(),
    )
    assert result.basis == "i14_transition"
    assert result.change_unit == "pp"
    assert result.fired


# ── 2티어 조립 ────────────────────────────────────────────────────────────────


def _tier1_inputs(**overrides) -> scan.Tier1Inputs:
    dates, sales, orders = _series(1000.0, 50, 500.0, 25)
    base = {
        "sales_dates": dates,
        "sales_values": sales,
        "sales_order_counts": orders,
        "target_date": dates[-1],
        "current_view": 3000,
        "current_purchase": 144,
        "baseline_view": 3000,
        "baseline_purchase": 90,
    }
    return scan.Tier1Inputs(**{**base, **overrides})


def test_scan_runs_tier2_only_when_tier1_opens() -> None:
    """티어2 는 문이 열렸을 때만 돈다 — 매일 돌리면 3콜이 8콜이 된다(결정 100)."""
    thresholds = _thresholds()
    opened = scan.scan(_tier1_inputs(), scan.Tier2Inputs(), thresholds=thresholds)
    assert opened.opened
    assert len(opened.tier2) == 4

    quiet = scan.scan(
        _tier1_inputs(
            sales_values=[1000.0] * 14,
            sales_order_counts=[50] * 14,
            current_purchase=90,
        ),
        scan.Tier2Inputs(),
        thresholds=thresholds,
    )
    assert not quiet.opened
    assert quiet.tier2 == []


def test_scan_result_lists_fired_triggers() -> None:
    result = scan.scan(_tier1_inputs(), thresholds=_thresholds())
    assert scan.TRIGGER_SALES in result.fired
    assert set(result.fired) <= set(scan.TIER1_TRIGGERS) | set(scan.TIER2_TRIGGERS)


def test_effective_alpha_is_window_corrected() -> None:
    """비율 검정의 유의수준은 lookback 으로 나눈 값이다 — 새 Settings 키를 만들지 않는다."""
    thresholds = _thresholds()
    assert thresholds.effective_rate_alpha == pytest.approx(0.05 / 28)
