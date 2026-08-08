"""app/agents/seller/calc.py 순수 함수 테스트 (DESIGN-SELLER-TOOLS-STAGE1 §6).

전부 stdlib 만으로 실행 가능 — 결정론(같은 입력 = 같은 출력)과 임계값 주입을 검증한다.
[#290] 구 SMA 계열(moving_average·is_anomaly·detect_sales_anomalies) 테스트는
S-H-ESD 교체로 test_seller_analysis_timeseries.py 로 이관됐다 — 무매출 규칙 3종
(0원 미판정·무매출 이력 직후 발생=이상·Spring 플래그 무시)도 그쪽이 계승 검증한다.
[#345] 구 normalize_period 테스트는 test_seller_period.py 로 이관됐다 — #269 P0 회귀
가드(침묵 폴백 금지·상한·자릿수·문구 형태)도 그쪽이 그대로 계승한다.
"""

from __future__ import annotations

import pytest

from app.agents.seller import calc
from app.schemas.spring import FunnelResult


def test_conversion_rates() -> None:
    """단계 전환율 계산 — 기간 비교 판정은 analysis.proportions(z-검정)로 이관됐다(#290)."""
    current = FunnelResult(view=1000, cart=100, checkout=50, purchase=40)
    rates = calc.conversion_rates(current)
    assert rates["view_to_cart"] == 10.0
    assert rates["cart_to_checkout"] == 50.0
    assert rates["checkout_to_purchase"] == 80.0


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
