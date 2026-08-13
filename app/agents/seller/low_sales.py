"""상품 변경 시 저성과(최근 N일 판매량) 참고 문구 — 이슈 #659.

판매자가 상품을 변경(update)할 때 최근 `seller_low_sales_window_days`일 판매량이
`seller_low_sales_quantity_threshold` 이하이면 "재고나 가격을 검토해 보세요" 류
참고 문구를 덧붙인다. 신규 API·응답 필드·FE 컴포넌트 변경 없이 기존 자유 텍스트
필드(hitl._execute_draft 실행 결과 안내, DraftRecord.summary — 둘 다 이미 코드가
조립·FE 가 그대로 표시하는 필드)에 얹는 방식이다(실현가능성 실측 2026-08-12,
프로젝트 메모리 seller-product-low-sales-alert 참조).

판매량 원천은 I-13 행동 이벤트(`SpringClient.get_events`, groupBy=product) —
`tools.get_behavior_events`(LLM 도구)와 같은 엔드포인트를 코드가 직접 호출한다.
조회 실패(SpringUnavailableError)는 soft-fail 로 무시한다 — 참고 문구 하나 때문에
등록 반영이나 안내 자체를 막지 않는다(`api.seller._snapshot_before`·
`tools._point_spike_note` 와 동일 관용).

⚠️ 신규 상품 등록 직후처럼 관측 기간이 짧은 경우도 "판매량이 적다"로 판정된다 —
상품 나이(생성일)를 보정하지 않는 MVP 범위의 알려진 한계다(단순함 우선, 원 요청
"간단하게" 범위).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from app.core.clock import today_kst
from app.core.config import get_settings
from app.services.spring_client import SpringClient, SpringUnavailableError

logger = logging.getLogger(__name__)


async def low_sales_note(client: SpringClient, brand_id: int, product_id: int) -> str:
    """최근 N일 판매량이 임계 이하면 선행 공백 포함 참고 문구를, 아니면 "" 를 반환한다.

    호출부는 기존 안내 문자열 뒤에 그대로 이어 붙인다(hitl._execute_draft 의
    stock_note 와 동일한 "선행 공백 포함 문구 or 빈 문자열" 관용).
    """
    settings = get_settings()
    if not settings.seller_low_sales_alert_enabled:
        return ""

    window_days = settings.seller_low_sales_window_days
    to_date = today_kst()
    from_date = to_date - timedelta(days=window_days)
    try:
        result = await client.get_events(
            brand_id,
            from_=from_date.isoformat(),
            to=to_date.isoformat(),
            product_id=product_id,
            group_by="product",
        )
    except SpringUnavailableError as exc:
        logger.warning(
            "저성과 경고 조회 실패(soft-fail, 안내는 계속 진행) — productId=%s: %s",
            product_id,
            exc,
        )
        return ""

    row = next((r for r in result.rows if r.product_id == product_id), None)
    sales_quantity = row.sales_quantity if row is not None else 0
    if sales_quantity is None:
        # 이 호출은 event_type 을 좁히지 않으므로(전 5종 조회) 계약상 null 이 나오지
        # 않아야 하지만(schemas.BehaviorProductRow), 방어적으로 "판정 불가"는 스킵한다.
        return ""
    if sales_quantity > settings.seller_low_sales_quantity_threshold:
        return ""

    view_count = row.counts.get("productView", 0) if row is not None and row.counts else 0
    return (
        f" 참고: 최근 {window_days}일 판매량이 {sales_quantity}개로 적은 편입니다"
        f"(조회 {view_count}건) — 재고나 가격 조정을 검토해 보세요."
    )
