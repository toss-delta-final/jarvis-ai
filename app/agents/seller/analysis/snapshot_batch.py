"""I-38 조회 → 스냅샷 조립 → 저장 배선 (이슈 #601).

`features/snapshot.build_snapshot_record`는 I/O가 없는 순수 조립 함수다(그 모듈 docstring
— "Spring 조회도, `analysis_store.save_snapshot` 호출도 하지 않는다") — 이 모듈이 그 양옆에
Spring 조회(`get_customer_features`)와 저장(`analysis_store.save_snapshot`)을 붙인다. 무인
배치의 ① 스냅샷 계산 단계(`10-TRIGGER.md` §5.1 — "KST 00:20, 무겁다")가 이 함수를 부른다.

조회 기간은 고객 축 30일 창이다(`10-TRIGGER.md` §5.3 "고객 축: 최근 30일 스냅샷" — 브랜드 축
7일 창과는 다른 축, 같은 말로 부르지 않는다).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from uuid import UUID

from app.agents.seller.analysis_store import save_snapshot
from app.agents.seller.features.snapshot import build_snapshot_record
from app.core.config import Settings
from app.services.spring_client import SpringClient, SpringUnavailableError

logger = logging.getLogger(__name__)


async def run_snapshot_batch(
    client: SpringClient,
    brand_id: int,
    *,
    target_date: date,
    settings: Settings,
    retries: int = 0,
) -> UUID | None:
    """브랜드 1개의 스냅샷 계산 1회 — I-38 30일 조회 → 피처/군집 조립 → 저장.

    실패(Spring 도달 불가·응답 형식 오류)는 예외를 삼키고 `None`을 돌려준다 —
    `10-TRIGGER.md` 결정 98 F-3 규약대로 "오늘 스냅샷 계산 실패"는 브랜드 축만으로
    보고서를 계속 생성할 사유(`Hold("snapshot_failed")`)이지 그날 밤 배치 전체를
    막는 사유가 아니다. 호출부(`daily_batch.py`)가 `None`을 그 신호로 받는다.
    """
    period_to = target_date
    period_from = target_date - timedelta(days=settings.seller_snapshot_period_days - 1)
    try:
        result = await client.get_customer_features(
            brand_id, period_from.isoformat(), period_to.isoformat(), retries=retries
        )
    except SpringUnavailableError as exc:
        logger.warning(
            "brand_id=%s 스냅샷 계산 실패(Spring 도달 불가) — 브랜드 축만으로 계속 진행: %r",
            brand_id,
            exc,
        )
        return None

    record = build_snapshot_record(
        result,
        brand_id=brand_id,
        period_from=period_from,
        period_to=period_to,
        settings=settings,
    )
    return await save_snapshot(record)
