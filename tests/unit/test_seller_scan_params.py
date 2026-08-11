"""sop/scan_params.py — Settings ↔ TriggerThresholds 매핑 테스트 (이슈 #595).

이 파일이 존재하는 이유는 이중 구현 방지다. `analysis/scan.py` 는 계층 규약대로
Settings 를 읽지 않으므로, "어떤 Settings 키가 어느 판정 인자로 가는가"는 어댑터
하나에만 적혀 있다. 그 매핑이 조용히 틀리면 **운영과 시뮬레이션이 다른 숫자로 돈다** —
이슈 #595 가 판정 함수를 스케줄러보다 먼저 만들게 한 바로 그 사고다.
"""

from __future__ import annotations

import pytest

from app.agents.seller.analysis.scan import TriggerThresholds
from app.agents.seller.sop.scan_params import thresholds_from_settings
from app.core.config import Settings


def test_every_field_mirrors_a_settings_key() -> None:
    """필드 하나하나가 어느 Settings 키에서 왔는지 못박는다."""
    settings = Settings(_env_file=None)
    thresholds = thresholds_from_settings(settings)
    assert thresholds.sales_pct == settings.seller_trigger_sales_pct
    assert thresholds.conversion_pct == settings.seller_trigger_conversion_pct
    assert thresholds.product_drop_pct == settings.seller_trigger_product_drop_pct
    assert thresholds.cart_abandon_pp == settings.seller_trigger_cart_abandon_pp
    assert thresholds.new_customer_drop_pct == settings.seller_trigger_new_customer_drop_pct
    assert thresholds.repurchase_drop_pp == settings.seller_trigger_repurchase_drop_pp
    assert thresholds.baseline_days == settings.seller_scan_baseline_days
    assert thresholds.lookback_days == settings.seller_analysis_lookback_days
    assert thresholds.rate_alpha == settings.seller_rate_test_alpha
    assert thresholds.wilson_confidence == settings.seller_wilson_confidence


def test_effective_alpha_is_composed_from_existing_keys() -> None:
    """창 보정은 **새 키가 아니라** 기존 키 둘의 조합이다(신설 금지 — `12-EVAL` §8)."""
    settings = Settings(_env_file=None)
    thresholds = thresholds_from_settings(settings)
    assert thresholds.effective_rate_alpha == pytest.approx(
        settings.seller_rate_test_alpha / settings.seller_analysis_lookback_days
    )


def test_no_stl_tunable_leaks_into_scan() -> None:
    """무인 스캔은 S-H-ESD 를 쓰지 않는다 — STL 튜너블이 다시 들어오면 여기서 걸린다.

    되돌아오면 창 경계 무검출(실측 168창 중 0회)이 조용히 재발한다.
    """
    assert set(TriggerThresholds.__dataclass_fields__) == {
        "sales_pct",
        "conversion_pct",
        "product_drop_pct",
        "cart_abandon_pp",
        "new_customer_drop_pct",
        "repurchase_drop_pp",
        "baseline_days",
        "lookback_days",
        "rate_alpha",
        "wilson_confidence",
        "abuse_min_members",
    }


def test_settings_override_flows_through() -> None:
    """env 로 임계를 바꾸면 판정 인자까지 그대로 흐른다(중간에 하드코딩이 없다)."""
    settings = Settings(_env_file=None, seller_trigger_sales_pct=0.12, seller_scan_baseline_days=5)
    thresholds = thresholds_from_settings(settings)
    assert thresholds.sales_pct == 0.12
    assert thresholds.baseline_days == 5


def test_abuse_minimum_is_not_a_settings_key() -> None:
    """BE 소유 판정(결정 103)이라 우리가 흔들 값이 아니다 — 기본값 1 고정."""
    assert thresholds_from_settings(Settings(_env_file=None)).abuse_min_members == 1
