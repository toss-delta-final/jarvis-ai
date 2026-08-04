"""app/agents/seller/calc.py 순수 함수 테스트 (DESIGN-SELLER-TOOLS-STAGE1 §6).

전부 stdlib 만으로 실행 가능 — 결정론(같은 입력 = 같은 출력)과 임계값 주입을 검증한다.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.agents.seller import calc
from app.schemas.spring import FunnelResult, SalesSeriesPoint


def test_moving_average_window_boundary() -> None:
    """len < window 구간은 None, 이후는 정확한 평균값."""
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    result = calc.moving_average(values, window=3)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == 20.0  # (10+20+30)/3
    assert result[3] == 30.0  # (20+30+40)/3
    assert result[4] == 40.0  # (30+40+50)/3


def test_deviation_pct_sign_and_zero_baseline() -> None:
    """양/음 부호가 실측-기준 방향과 일치하고, baseline==0 이면 0.0."""
    assert calc.deviation_pct(120.0, 100.0) == 20.0
    assert calc.deviation_pct(80.0, 100.0) == -20.0
    assert calc.deviation_pct(50.0, 0.0) == 0.0


def test_is_anomaly_threshold_boundary() -> None:
    """편차 절대값이 임계값과 같으면 이상(True), 미만이면 False."""
    assert calc.is_anomaly(30.0, threshold_pct=30.0) is True
    assert calc.is_anomaly(-30.0, threshold_pct=30.0) is True
    assert calc.is_anomaly(29.9, threshold_pct=30.0) is False


def test_detect_sales_anomalies_ignores_spring_flags() -> None:
    """Spring 이 준 isAnomaly 가 반대여도 원시 sales 로 재판정한다(§0.1 D)."""
    series = [
        SalesSeriesPoint(
            date="2026-07-01", sales=100, order_count=10, is_anomaly=True, deviation_pct=999.0
        ),
        SalesSeriesPoint(
            date="2026-07-02", sales=100, order_count=10, is_anomaly=True, deviation_pct=999.0
        ),
        SalesSeriesPoint(
            date="2026-07-03", sales=100, order_count=10, is_anomaly=True, deviation_pct=999.0
        ),
        # 이동평균(100) 대비 300% 급증 — 실제로 이상이어야 함.
        SalesSeriesPoint(
            date="2026-07-04", sales=400, order_count=40, is_anomaly=False, deviation_pct=0.0
        ),
    ]
    results = calc.detect_sales_anomalies(series, window=3, min_window=3, threshold_pct=30.0)
    dates = [r[0] for r in results]
    assert dates == ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"]
    # 경계 구간(직전 min_window 미만)은 판정 보류 — deviation 도 None(#194, 구 0.0 과 구분).
    assert results[0] == ("2026-07-01", None, False)
    assert results[1] == ("2026-07-02", None, False)
    assert results[2] == ("2026-07-03", None, False)
    # 4번째: 직전 3일(100,100,100) 평균=100 대비 actual=400 → deviation=300% → 이상.
    assert results[3][1] == 300.0
    assert results[3][2] is True


def _point(day: int, sales: int) -> SalesSeriesPoint:
    """detect_sales_anomalies 테스트용 시계열 포인트 헬퍼(#194)."""
    return SalesSeriesPoint(date=f"2026-07-{day:02d}", sales=sales, order_count=1)


def test_detect_sales_anomalies_adaptive_min_window() -> None:
    """[#194] 직전 3점부터 판정한다(Spring MIN_WINDOW=3 정렬) — 구 로직(직전 7점 필수)은
    최근 기간 질의에서 window 미달로 이상을 통째로 놓쳤다(회귀 방지)."""
    series = [_point(1, 100), _point(2, 100), _point(3, 100), _point(4, 200)]
    results = calc.detect_sales_anomalies(series, window=7, min_window=3, threshold_pct=30.0)
    # 4번째: 직전 이력 3점(window 7 미달)이어도 평균 100 대비 +100% → 이상.
    assert results[3][1] == 100.0
    assert results[3][2] is True


def test_detect_sales_anomalies_zero_baseline_sales_is_anomaly() -> None:
    """[#194] 무매출 구간 직후 매출 발생 = 이상(Spring 정렬) — 기준선 0 이라 편차는
    정의 불가(None)지만 발생 자체가 이상이다. 구 로직은 deviation 0.0 → 정상으로 놓쳤다."""
    series = [_point(1, 0), _point(2, 0), _point(3, 0), _point(4, 50000)]
    results = calc.detect_sales_anomalies(series, window=7, min_window=3, threshold_pct=30.0)
    assert results[3] == ("2026-07-04", None, True)
    # 기준선 0 + 매출도 0 이면 정상.
    all_zero = [_point(d, 0) for d in range(1, 5)]
    calm = calc.detect_sales_anomalies(all_zero, window=7, min_window=3, threshold_pct=30.0)
    assert calm[3] == ("2026-07-04", None, False)


def test_detect_sales_anomalies_zero_sales_never_anomaly() -> None:
    """[#194] 매출 0원 포인트는 편차 -100% 여도 이상 아님(Spring `sales > 0` 가드 정렬) —
    저볼륨 브랜드의 무판매일이 전부 이상으로 판정되는 노이즈 방지."""
    series = [_point(1, 100), _point(2, 100), _point(3, 100), _point(4, 0)]
    results = calc.detect_sales_anomalies(series, window=7, min_window=3, threshold_pct=30.0)
    assert results[3][1] == -100.0  # 편차는 계산되지만
    assert results[3][2] is False  # 이상은 아니다


def test_detect_sales_anomalies_rejects_invalid_window_config() -> None:
    """[#194] min_window ≤ 0 이거나 window < min_window 면 설정 오류로 ValueError."""
    series = [_point(1, 100)]
    with pytest.raises(ValueError):
        calc.detect_sales_anomalies(series, window=7, min_window=0, threshold_pct=30.0)
    with pytest.raises(ValueError):
        calc.detect_sales_anomalies(series, window=2, min_window=3, threshold_pct=30.0)


def test_conversion_rates_and_drop() -> None:
    """단계 전환율 계산과 baseline 대비 하락 임계 판정."""
    current = FunnelResult(view=1000, cart=100, checkout=50, purchase=40)
    rates = calc.conversion_rates(current)
    assert rates["view_to_cart"] == 10.0
    assert rates["cart_to_checkout"] == 50.0
    assert rates["checkout_to_purchase"] == 80.0

    baseline = FunnelResult(view=1000, cart=200, checkout=100, purchase=90)
    drop = calc.compare_conversion(current, baseline, drop_pct=20.0)
    # view_to_cart: baseline 20% → current 10% → -50% 하락 → 이상.
    assert drop["view_to_cart"] is True
    # cart_to_checkout: baseline 50% → current 50% → 하락 없음.
    assert drop["cart_to_checkout"] is False


def test_compare_conversion_baseline_zero_no_drop() -> None:
    """baseline 전환율이 0(분모 0)이면 비교 기준이 없어 하락으로 판정하지 않는다(opus 리뷰 m6)."""
    # baseline.cart == 0 → cart_to_checkout 의 baseline 전환율(=checkout/cart)이 0.
    baseline = FunnelResult(view=1000, cart=0, checkout=0, purchase=0)
    current = FunnelResult(view=1000, cart=100, checkout=50, purchase=40)

    drop = calc.compare_conversion(current, baseline, drop_pct=20.0)

    assert drop["view_to_cart"] is False
    assert drop["cart_to_checkout"] is False
    assert drop["checkout_to_purchase"] is False


def test_normalize_period_last_month_year_rollover() -> None:
    """1월 today → 전년 12/1~12/31 로 롤오버한다."""
    today = dt.date(2026, 1, 15)
    start, end = calc.normalize_period("지난달", today=today, recent_default_days=7)
    assert start == dt.date(2025, 12, 1)
    assert end == dt.date(2025, 12, 31)


def test_normalize_period_recent_n_excludes_today() -> None:
    """ "최근 N일"은 (today-N)~(today-1) — 오늘은 포함하지 않는다."""
    today = dt.date(2026, 7, 17)
    start, end = calc.normalize_period("최근 7일", today=today, recent_default_days=3)
    assert start == dt.date(2026, 7, 10)
    assert end == dt.date(2026, 7, 16)

    # N 미지정("최근") 이면 recent_default_days 사용.
    start2, end2 = calc.normalize_period("최근", today=today, recent_default_days=3)
    assert start2 == dt.date(2026, 7, 14)
    assert end2 == dt.date(2026, 7, 16)


def test_normalize_period_explicit_range() -> None:
    """ "YYYY-MM-DD~YYYY-MM-DD" 명시 범위는 그대로 반환한다(3-1 확장, 공백 허용)."""
    today = dt.date(2026, 7, 18)
    start, end = calc.normalize_period("2026-06-01~2026-06-15", today=today, recent_default_days=7)
    assert start == dt.date(2026, 6, 1)
    assert end == dt.date(2026, 6, 15)

    start2, end2 = calc.normalize_period(
        "2026-06-01 ~ 2026-06-15", today=today, recent_default_days=7
    )
    assert (start2, end2) == (start, end)


def test_normalize_period_explicit_range_rejects_invalid() -> None:
    """명시 범위의 역전(from>to)·달력에 없는 날짜는 ValueError(되묻기 경로)."""
    today = dt.date(2026, 7, 18)
    with pytest.raises(ValueError):
        calc.normalize_period("2026-06-15~2026-06-01", today=today, recent_default_days=7)
    with pytest.raises(ValueError):
        calc.normalize_period("2026-02-30~2026-03-01", today=today, recent_default_days=7)


def test_normalize_period_recent_nonpositive_days_raises() -> None:
    """ "최근 0일" 등 N≤0 은 역전 범위(from>to)가 되므로 ValueError(마감 리뷰 M3)."""
    today = dt.date(2026, 7, 18)
    with pytest.raises(ValueError):
        calc.normalize_period("최근 0일", today=today, recent_default_days=7)
    with pytest.raises(ValueError):
        calc.normalize_period("최근", today=today, recent_default_days=0)  # 설정 오류 방어


def test_normalize_period_unsupported_expr_raises() -> None:
    """미지원 표현("이번 달" 등)은 ValueError — 되묻기로 처리한다(2026-07-18 확정)."""
    today = dt.date(2026, 7, 18)
    for expr in ("이번 달", "이번달", "올해", "작년 여름"):
        with pytest.raises(ValueError):
            calc.normalize_period(expr, today=today, recent_default_days=7)


# ── #269 "최근 …" 침묵 폴백 제거 ──────────────────────────────────────────────

# sellerperiodtestcases.md B-1 표 이식. 종전 구조(`"최근" in text` 부분 일치 + 정규식
# 실패 시 기본값)에서는 아래가 **전부 조용히 7일**로 통과했다 — 되묻기도 경고도 없이.
_SILENT_FALLBACK_EXPRS = (
    "최근 2주",
    "최근 3개월",
    "최근 한 달",
    "최근 1주일",
    "최근 반년",
    "최근 -3일",
    "최근에",
    "최근 며칠",
    "이번 달 들어 최근 7일",  # 미지원 어휘가 앞에 붙어도 부분 일치로 통과하던 구멍
)


@pytest.mark.parametrize("expr", _SILENT_FALLBACK_EXPRS)
def test_normalize_period_no_silent_default_fallback(expr: str) -> None:
    """인식하지 못한 "최근 …" 표현은 기본 일수로 떨어지지 않고 되묻기로 간다(#269)."""
    today = dt.date(2026, 8, 2)
    with pytest.raises(ValueError):
        calc.normalize_period(expr, today=today, recent_default_days=7, max_days=731)


def test_normalize_period_upper_bound_raises_value_error() -> None:
    """ "최근 999999일" 은 OverflowError 가 아니라 ValueError 다(#269).

    호출부(orchestrator)는 except ValueError 만 잡으므로, OverflowError 면 되묻기가
    아니라 파이프라인 예외로 전파돼 사과/error 경로로 샌다.
    """
    today = dt.date(2026, 8, 2)
    with pytest.raises(ValueError):
        calc.normalize_period("최근 999999일", today=today, recent_default_days=7, max_days=731)

    # 상한 이내는 정상 통과 — 가드가 정상 범위를 막지 않는다.
    start, end = calc.normalize_period(
        "최근 731일", today=today, recent_default_days=7, max_days=731
    )
    assert (end - start).days + 1 == 731


def test_normalize_period_normalizes_fullwidth_digits() -> None:
    """전각 숫자("최근 ７일")는 NFKC 정규화로 반각과 같게 해석한다(#269)."""
    today = dt.date(2026, 8, 2)
    kwargs = {"today": today, "recent_default_days": 3, "max_days": 731}
    assert calc.normalize_period("최근 ７일", **kwargs) == calc.normalize_period(
        "최근 7일", **kwargs
    )


def test_normalize_period_canonical_vocab_regression_guard() -> None:
    """회귀 가드 — 정규 어휘 4종의 결과는 #269 전후로 동일하다.

    이 테스트가 깨지면 기존 판매자가 잘 쓰던 경로를 건드린 것이다.
    """
    kwargs = {"today": dt.date(2026, 8, 2), "recent_default_days": 7, "max_days": 731}
    assert calc.normalize_period("지난달", **kwargs) == (
        dt.date(2026, 7, 1),
        dt.date(2026, 7, 31),
    )
    assert calc.normalize_period("최근 7일", **kwargs) == (
        dt.date(2026, 7, 26),
        dt.date(2026, 8, 1),
    )
    # N 미지정("최근")은 정확히 이 두 글자일 때만 기본 일수를 쓴다.
    assert calc.normalize_period("최근", **kwargs) == (dt.date(2026, 7, 26), dt.date(2026, 8, 1))
    assert calc.normalize_period("어제", **kwargs) == (dt.date(2026, 8, 1), dt.date(2026, 8, 1))
    assert calc.normalize_period("2026-06-01~2026-06-30", **kwargs) == (
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
    )


def test_normalize_period_error_message_is_user_facing() -> None:
    """되묻기 메시지는 판매자에게 그대로 노출된다 — 개발자 문자열을 쓰지 않는다(#269).

    resolve_plan 이 이 ValueError 메시지를 PipelineResult(kind="clarification").text 로
    그대로 흘리므로, 메시지 자체가 사용자 대면 문구여야 한다.
    """
    kwargs = {"today": dt.date(2026, 8, 2), "recent_default_days": 7, "max_days": 731}

    with pytest.raises(ValueError) as unsupported:
        calc.normalize_period("이번 달", **kwargs)
    message = str(unsupported.value)
    assert "파싱" not in message
    assert "period_expr" not in message
    assert "지난달" in message  # 지원 어휘 안내를 포함한다

    with pytest.raises(ValueError) as wrong_unit:
        calc.normalize_period("최근 3개월", **kwargs)
    assert "일 단위" in str(wrong_unit.value)  # 단위를 짚어 안내한다


# 단위 어휘가 실제 기간 단위로 쓰인 경우 — 단위를 짚는 안내가 나가야 한다.
_REAL_UNIT_EXPRS = ("최근 2주", "최근 3개월", "최근 한 달", "최근 1주일", "최근 반년", "최근 5년")

# 단위 글자가 **다른 단어 안에 우연히 포함**된 경우 — 판매자는 단위를 쓴 적이 없다.
# 부분 문자열 검사(`"달" in text`)면 여기까지 "일 단위로 말씀해 주세요" 가 나가 되묻기
# 이유를 잘못 짚는다(#269 리뷰).
_INCIDENTAL_UNIT_EXPRS = ("최근 목표 달성 현황", "최근 주말 프로모션", "최근 분기점 지표")


@pytest.mark.parametrize("expr", _REAL_UNIT_EXPRS)
def test_normalize_period_real_unit_gets_unit_guidance(expr: str) -> None:
    """실제 주·개월·년 단위 표현은 단위를 짚는 안내로 되묻는다."""
    kwargs = {"today": dt.date(2026, 8, 2), "recent_default_days": 7, "max_days": 731}
    with pytest.raises(ValueError) as exc:
        calc.normalize_period(expr, **kwargs)
    assert "일 단위" in str(exc.value)


@pytest.mark.parametrize("expr", _INCIDENTAL_UNIT_EXPRS)
def test_normalize_period_incidental_unit_char_gets_generic_guidance(expr: str) -> None:
    """단위 글자가 다른 단어에 섞였을 뿐이면 단위 안내가 아니라 지원 어휘 안내다(#269 리뷰).

    되묻기라는 결론은 같아도 이유가 틀리면 되묻기 대화가 더 꼬인다 — 이 PR 의 목적이
    "판매자가 왜 되물어지는지 알게 하는 것" 이므로 이유를 정확히 짚어야 한다.
    """
    kwargs = {"today": dt.date(2026, 8, 2), "recent_default_days": 7, "max_days": 731}
    with pytest.raises(ValueError) as exc:
        calc.normalize_period(expr, **kwargs)
    message = str(exc.value)
    assert "일 단위" not in message
    assert "지난달" in message  # 지원 어휘 안내


def test_normalize_period_never_raises_overflow_error() -> None:
    """max_days 를 크게 넘겨도 OverflowError 가 밖으로 나가지 않는다(#269 리뷰).

    Settings 가 seller_period_max_days 를 10년으로 묶지만 max_days 는 함수 인자라
    호출부가 직접 큰 값을 넘길 수 있다. 약 74만일부터 date 연산이 date.min 을 넘는데,
    OverflowError 가 새면 호출부의 except ValueError 를 빠져나가 되묻기 대신 에러
    경로가 된다 — 설정 검증과 별개로 함수 자체가 이 계약을 지켜야 한다.
    """
    today = dt.date(2026, 8, 2)
    # 상한 이내지만 date 연산 한계를 넘는 구간.
    with pytest.raises(ValueError):
        calc.normalize_period(
            "최근 800000일", today=today, recent_default_days=7, max_days=999_999_999
        )


def test_normalize_period_huge_digit_count_is_wrapped() -> None:
    """자릿수가 터무니없이 많아도 Python 내부 예외 메시지가 새지 않는다(#269 리뷰).

    Python 3.11+ 는 4300자리 초과 문자열→int 변환에서 영어 메시지 ValueError 를 낸다
    ("Exceeds the limit (4300) for integer string conversion…"). 그 ValueError 는
    resolve_plan → orchestrator 의 except ValueError 를 그대로 통과해 되묻기 문구로
    판매자에게 노출된다 — max_days 가 막으려던 것과 같은 실패 양상이다.
    """
    kwargs = {"today": dt.date(2026, 8, 2), "recent_default_days": 7, "max_days": 731}
    expr = "최근 " + "9" * 4301 + "일"
    with pytest.raises(ValueError) as exc:
        calc.normalize_period(expr, **kwargs)
    message = str(exc.value)
    assert "Exceeds the limit" not in message
    assert "integer string conversion" not in message
    assert "기간이 너무 깁니다" in message


def test_safe_eval_basic_arithmetic() -> None:
    """사칙연산·거듭제곱·round() 는 허용된다 (calculate 도구 기반)."""
    assert calc.safe_eval("1200000 / 45 * 100") == 1200000 / 45 * 100
    assert calc.safe_eval("round(1234.5678, 2)") == 1234.57
    assert calc.safe_eval("2 ** 10") == 1024


def test_safe_eval_blocks_import_attribute_and_names() -> None:
    """__import__·속성 접근·변수 참조는 전부 ValueError 로 차단된다(보안, LLM 임의 코드 방지)."""
    with pytest.raises(ValueError):
        calc.safe_eval("__import__('os').system('ls')")
    with pytest.raises(ValueError):
        calc.safe_eval("(1).__class__")
    with pytest.raises(ValueError):
        calc.safe_eval("x + 1")


def test_calc_uses_injected_thresholds() -> None:
    """다른 임계값을 주입하면 결과가 달라진다(하드코딩 부재 확인)."""
    series = [
        SalesSeriesPoint(date="2026-07-01", sales=100, order_count=10),
        SalesSeriesPoint(date="2026-07-02", sales=100, order_count=10),
        SalesSeriesPoint(date="2026-07-03", sales=115, order_count=11),
    ]
    strict = calc.detect_sales_anomalies(series, window=2, min_window=2, threshold_pct=10.0)
    lenient = calc.detect_sales_anomalies(series, window=2, min_window=2, threshold_pct=50.0)
    # 동일 데이터, 다른 threshold_pct → 이상 판정이 달라져야 한다.
    assert strict[2][2] is True
    assert lenient[2][2] is False


def test_safe_eval_rejects_giant_power_dos() -> None:
    """LLM 생성식의 거대 거듭제곱은 평가 전에 ValueError 로 차단한다(DoS 방어, 리뷰 반영)."""
    for expr in ("9**9**9**9", "10**9999999", "2**1000000"):
        with pytest.raises(ValueError):
            calc.safe_eval(expr)


def test_safe_eval_allows_normal_power() -> None:
    """정상 범위 거듭제곱은 그대로 평가된다(가드 오탐 없음)."""
    assert calc.safe_eval("2**10") == 1024
    assert calc.safe_eval("1000**3") == 1_000_000_000


def test_safe_eval_float_base_not_false_rejected() -> None:
    """float 밑수는 C pow(O(1))라 DoS 가 아니다 — 큰 지수여도 오탐 거부하지 않는다(리뷰 반영).

    1.1**5000 ≈ 10^207 은 유한 float 이고 즉시 계산된다(int**int 만 가드 대상)."""
    result = calc.safe_eval("1.1**5000")
    assert result > 0  # ValueError 로 오탐 거부되지 않고 유한값 반환
