"""무인 배치 정리 단계 — 스냅샷 정리 → (성과 측정, 범위 밖) → 추천 만료 → checkpoint 정리
(이슈 #601, `08-PERSISTENCE.md` §5 결정 68).

**순서가 계약이다** — 성과 측정이 스냅샷 정리보다 앞이어야 방금 지운 스냅샷을 참조하지
않는다(`snapshot_id`가 `ON DELETE SET NULL`이라 죽지는 않지만, 측정 시점에 스냅샷이
아직 살아 있어야 그 안의 `feature_rows`를 참조할 수 있다). ②~⑤ 는 브랜드 순회 밖에서
**전역으로 1회** 실행한다 — 브랜드별로 반복할 이유가 없다(DELETE·UPDATE 조건 자체가
`brand_id`를 좁히지 않는다, `analysis_store.delete_expired_snapshots`·`expire_recommendations`
둘 다 전역 함수). 호출부(`daily_batch.run_daily_batch`)가 브랜드 루프가 끝난 뒤 이
모듈의 `run_cleanup_batch()` 하나를 부른다.

각 단계는 **독립 실패**한다(`08-PERSISTENCE.md` §5 "②가 죽어도 ③은 돈다" — 브랜드 단위
격리와 같은 원칙을 정리 단계에도 적용). 한 단계의 예외가 다음 단계를 막지 않는다.

[③ 성과 측정 — 이 이슈(#601) 범위 밖, placeholder]
`applied` 추천의 전후 전환율을 대조군과 비교해 `seller_analysis_outcomes`에 적는
계산(처리군/대조군 pre/post 성공·시행 카운트, p-value, verdict 판정)은 이 리포지토리
어디에도 아직 구현되어 있지 않다(`analysis_store.save_outcome`은 저장 함수만 있고
그 값을 만드는 계산 모듈이 없다) — `08-PERSISTENCE.md` §5의 ③번 자리를 여기 남겨
호출 순서 계약(②→③→④)을 문서화하지만, 실제 측정 로직은 별도 이슈로 미룬다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agents.seller import analysis_store, checkpoint
from app.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupOutcome:
    """정리 배치 1회 결과 — 로그·테스트 검증용. 실패한 단계는 0으로 남는다(예외를 삼킨 뒤)."""

    snapshots_deleted: int
    recommendations_expired: int
    draft_checkpoints_deleted: int
    thread_checkpoints_deleted: int


async def _measure_outcomes() -> None:
    """③ 성과 측정 — 범위 밖(placeholder). 모듈 docstring 참조.

    아무 것도 하지 않는다 — 이 이슈에서 측정 계산을 구현하면 검증되지 않은 통계 판정을
    조용히 얹는 위험이 있어, "만들지 않는다"를 명시적으로 남기는 편을 택했다(구현 대신
    빈 함수 + 근거 docstring, `run_analysis`가 후보 생성기 부재를 다루는 방식과 같은 결).
    """
    return None


async def run_cleanup_batch() -> CleanupOutcome:
    """무인 정리 배치 1회 — ② 스냅샷 정리 → ③(placeholder) → ④ 추천 만료 → ⑤ checkpoint 정리."""
    settings = get_settings()

    snapshots_deleted = 0
    try:
        snapshots_deleted = await analysis_store.delete_expired_snapshots(
            settings.seller_snapshot_retention_days
        )
    except Exception:  # noqa: BLE001 - 단계 독립 실패(08-PERSISTENCE.md §5)
        logger.error("무인 배치 정리 ② 스냅샷 정리 실패 — ③으로 계속", exc_info=True)

    try:
        await _measure_outcomes()
    except Exception:  # noqa: BLE001 - 단계 독립 실패
        logger.error("무인 배치 정리 ③ 성과 측정 실패 — ④로 계속", exc_info=True)

    recommendations_expired = 0
    try:
        recommendations_expired = await analysis_store.expire_recommendations(
            settings.seller_rec_expire_days, batch_size=settings.seller_cleanup_batch_size
        )
    except Exception:  # noqa: BLE001 - 단계 독립 실패
        logger.error("무인 배치 정리 ④ 추천 만료 실패 — ⑤로 계속", exc_info=True)

    draft_deleted = 0
    thread_deleted = 0
    try:
        draft_deleted, thread_deleted = await checkpoint.cleanup_expired_checkpoints(
            draft_retention_hours=settings.seller_draft_retention_hours,
            thread_retention_days=settings.seller_thread_retention_days,
            batch_size=settings.seller_cleanup_batch_size,
        )
    except Exception:  # noqa: BLE001 - 단계 독립 실패
        logger.error("무인 배치 정리 ⑤ checkpoint 정리 실패", exc_info=True)

    outcome = CleanupOutcome(
        snapshots_deleted=snapshots_deleted,
        recommendations_expired=recommendations_expired,
        draft_checkpoints_deleted=draft_deleted,
        thread_checkpoints_deleted=thread_deleted,
    )
    logger.info(
        "무인 배치 정리 완료 snapshots_deleted=%d recommendations_expired=%d "
        "draft_checkpoints_deleted=%d thread_checkpoints_deleted=%d",
        outcome.snapshots_deleted,
        outcome.recommendations_expired,
        outcome.draft_checkpoints_deleted,
        outcome.thread_checkpoints_deleted,
    )
    return outcome
