"""Settings → `analysis.scan.TriggerThresholds` 어댑터 (이슈 #595).

**이 모듈의 존재 이유는 이중 구현 방지 하나다.** `analysis/scan.py` 는 계산 층 규약대로
Settings 를 읽지 않고 튜너블을 전부 주입받는데, 그러면 "누가 어떤 Settings 키를
어느 인자에 넣는가"가 호출부마다 흩어진다. 무인 스캔(#16)과 null 시뮬레이션이 그
매핑을 각각 적으면, 이슈 #595 가 판정 함수를 스케줄러보다 먼저 만들게 한 이유
(*"시뮬레이션이 검증한 것과 운영이 도는 것이 달라지는 이중 구현 사고"*)가 한 층
아래에서 그대로 재발한다. 매핑을 여기 하나로 모으고 단위 테스트가 못박는다.

`sop/` 에 두는 이유: `analysis/` 는 Settings 를 import 하지 않는다는 계층 규약이 있고
(`analysis/__init__` — *"튜너블 하드코딩 금지, 호출부가 Settings 에서 읽어 인자로 주입"*),
`sop/compute/*` 가 이미 `settings: Settings` 를 받아 계산 층에 넘기는 자리다.
"""

from __future__ import annotations

from app.agents.seller.analysis.scan import TriggerThresholds
from app.core.config import Settings


def thresholds_from_settings(settings: Settings) -> TriggerThresholds:
    """운영이 쓰는 판정 튜너블 묶음. 시뮬레이션·골든셋도 **이 함수를 거쳐** 만든다.

    고정 임계 6종은 `seller_trigger_*` 신설 키, 유의수준·구간은 기존 계산 층 키
    재사용이다(신설 금지 — `12-EVAL` §8). STL 계열 키는 넘기지 않는다 — 무인 스캔이
    S-H-ESD 를 쓰지 않기 때문이다(`analysis/scan.py` 모듈 docstring의 실측 근거).
    `abuse_min_members` 만 Settings 키가 없다:
    "isSuspicious 회원 1명이라도 있으면"은 BE 판정을 그대로 쓰는 규약(결정 103)이라
    우리가 조정할 값이 아니고, 튜너블로 열어 두면 남의 임계를 우리가 흔드는 모양이 된다.

    ``lookback_days`` 는 조회 창인 동시에 **비율 검정의 유의수준 보정 분모**다
    (`scan.TriggerThresholds.effective_rate_alpha`) — 새 alpha 키를 만들지 않기
    위해 기존 키 둘을 조합한다.
    """
    return TriggerThresholds(
        sales_pct=settings.seller_trigger_sales_pct,
        conversion_pct=settings.seller_trigger_conversion_pct,
        product_drop_pct=settings.seller_trigger_product_drop_pct,
        cart_abandon_pp=settings.seller_trigger_cart_abandon_pp,
        new_customer_drop_pct=settings.seller_trigger_new_customer_drop_pct,
        repurchase_drop_pp=settings.seller_trigger_repurchase_drop_pp,
        baseline_days=settings.seller_scan_baseline_days,
        lookback_days=settings.seller_analysis_lookback_days,
        rate_alpha=settings.seller_rate_test_alpha,
        wilson_confidence=settings.seller_wilson_confidence,
    )
