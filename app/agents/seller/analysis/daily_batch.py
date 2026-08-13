"""무인 배치 브랜드 체인 — 스냅샷 → 스캔 → 심층 분석 → 실행 로그 (이슈 #601).

`10-TRIGGER.md` §5.1~5.2 결정 95·100: 잡을 2개(스냅샷/스캔)로 쪼개지 않고, 브랜드 1개당
"스냅샷 → 티어1 스캔 → (열리면 티어2 → 심층 분석) → 실행 로그"를 한 체인으로 묶는다.
스냅샷 최악 예산(브랜드당 최대 `seller_batch_brand_timeout_s`)이 스케줄러의 다음 잡
간격(20분)보다 길어질 수 있어도(§5.2), 스캔이 "그날 스냅샷 없는 브랜드"를 다음 주기로
미루는 대신 같은 체인 안에서 자연히 뒤이어 붙는다.

주간 정기(④)는 신호와 무관하게 `resident.run_analysis`를 직접 부른다 — 스캔 자체를
건너뛴다(`10-TRIGGER.md` §5.1 "④ 주간 정기 — 신호 무관 1건"). 수동 실행(⑤)도 같은
경로를 공유하지만 트리거 타입만 다르다(`app/api/internal.py`가 부른다).

브랜드 간 동시성은 `seller_batch_concurrency`로 제한하고(Spring 부하 억제), 브랜드 1개의
총 예산은 `seller_batch_brand_timeout_s`로 자른다 — 넘긴 브랜드는 이번 주기를 실패로
남기고 다음 주기(24시간 뒤)가 자연히 이어받는다(브랜드 단위 격리, `OPS-RUNTIME` F-8과
같은 원칙 — 브랜드 하나가 막혀도 나머지 브랜드는 계속 돈다).

수동 실행·예약 실행이 같은 브랜드를 동시에 건드릴 수 있어(운영이 단일 인스턴스라도 수동
호출은 임의 시점에 온다, `app/pipelines/scheduler.py` 모듈 docstring의 단일 인스턴스
전제) `mutation_lock`으로 브랜드별 직렬화한다 — `history.py`가 이미 쓰는 것과 같은
pg-profile store를 재사용한다(#585와 같은 pg-profile 인스턴스, 락 자체는 전용 advisory
pool을 쓰므로 BaseStore pool을 잠식하지 않는다, `pg_resilience.mutation_lock` docstring).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from weakref import WeakValueDictionary

from app.agents.seller import analysis_store, history, resident
from app.agents.seller.analysis.cleanup import run_cleanup_batch
from app.agents.seller.analysis.scan_wiring import fetch_scan_inputs
from app.agents.seller.analysis.snapshot_batch import run_snapshot_batch
from app.agents.seller.analysis_records import TriggerType
from app.core.clock import today_kst
from app.core.config import Settings, get_settings
from app.core.pg_resilience import mutation_lock
from app.services.spring_client import SpringClient, SpringUnavailableError, get_spring_client

logger = logging.getLogger(__name__)

# 브랜드별 로컬 lock — mutation_lock의 비-Postgres 폴백(dev no-op store)에서만 실제로
# 쓰인다. WeakValueDictionary라 더 이상 참조되지 않는 브랜드의 lock은 자연 GC된다
# (history._save_locks와 동일 패턴).
_brand_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def _brand_lock(brand_id: int) -> asyncio.Lock:
    lock = _brand_locks.get(brand_id)
    if lock is None:
        lock = asyncio.Lock()
        _brand_locks[brand_id] = lock
    return lock


@dataclass(frozen=True)
class BrandBatchOutcome:
    """브랜드 1개 배치 체인의 결과 — 로그 요약·테스트 검증용."""

    brand_id: int
    ran: bool  # False면 mutation_lock/timeout 밖에서 아예 시작도 못 했다(발생하지 않음, 방어값)
    report_generated: bool
    skip_reason: str | None


async def _record_run(brand_id: int, *, skip_reason: str | None) -> None:
    """`seller_analysis_targets.last_run_at`/`last_skip_reason` 갱신 — 실패해도 배치를 막지 않는다."""
    try:
        await analysis_store.update_target_run(
            brand_id, last_run_at=datetime.now(UTC), last_skip_reason=skip_reason
        )
    except Exception:  # noqa: BLE001 - 실행 로그 기록 실패가 배치 결과 자체를 죽이면 안 된다
        logger.warning("brand_id=%s 실행 로그 기록 실패", brand_id, exc_info=True)


async def _run_locked(
    brand_id: int, coro_factory, *, settings: Settings, timeout_skip_reason: str = "batch_timeout"
) -> BrandBatchOutcome:
    """mutation_lock + 브랜드 예산 타임아웃으로 감싸 실행한다 — 세 경로(일간·주간·수동) 공용."""
    # history._get_store() 재사용 — api/seller.py의 history._current_value_str 참조와
    # 같은 관행(비공개 접두어지만 같은 seller 서브패키지 내부 재사용은 이 repo 관례).
    store = await history._get_store()
    async with mutation_lock(store, f"seller:batch:{brand_id}", _brand_lock(brand_id)):
        try:
            return await asyncio.wait_for(
                coro_factory(), timeout=settings.seller_batch_brand_timeout_s
            )
        except TimeoutError:
            logger.error(
                "brand_id=%s 무인 배치 예산(%.0fs) 초과 — 이번 주기는 실패로 남기고"
                " 다음 주기가 이어받는다",
                brand_id,
                settings.seller_batch_brand_timeout_s,
            )
            await _record_run(brand_id, skip_reason=timeout_skip_reason)
            return BrandBatchOutcome(
                brand_id, ran=True, report_generated=False, skip_reason=timeout_skip_reason
            )
        except Exception as exc:  # noqa: BLE001 - 브랜드 단위 격리, 배치 전체를 죽이지 않는다
            logger.error("brand_id=%s 무인 배치 실패(%r) — 다음 브랜드로 계속", brand_id, exc)
            await _record_run(brand_id, skip_reason="batch_error")
            return BrandBatchOutcome(
                brand_id, ran=True, report_generated=False, skip_reason="batch_error"
            )


async def _run_daily_chain(
    brand_id: int,
    *,
    trigger_type: TriggerType,
    target_date: date,
    settings: Settings,
    client: SpringClient,
) -> BrandBatchOutcome:
    # ① 스냅샷 — 실패해도 브랜드 축 스캔은 계속한다(10-TRIGGER.md 결정 98 F-3,
    # "오늘 스냅샷 계산 실패"는 고객 축만 보류할 사유이지 전면 차단 사유가 아니다).
    await run_snapshot_batch(
        client, brand_id, target_date=target_date, settings=settings, retries=1
    )

    # ② 티어1 스캔 (+ 열리면 티어2)
    try:
        fetched = await fetch_scan_inputs(
            client, brand_id, target_date=target_date, settings=settings, retries=1
        )
    except SpringUnavailableError as exc:
        logger.warning(
            "brand_id=%s 무인 스캔 실패(Spring 도달 불가) — 보고서 미생성: %r", brand_id, exc
        )
        await _record_run(brand_id, skip_reason="scan_unavailable")
        return BrandBatchOutcome(
            brand_id, ran=True, report_generated=False, skip_reason="scan_unavailable"
        )

    if not fetched.result.opened:
        await _record_run(brand_id, skip_reason=None)
        return BrandBatchOutcome(brand_id, ran=True, report_generated=False, skip_reason=None)

    # F-9 일 상한 — 오늘 이미 상한만큼 보고서를 만들었으면 신호가 있어도 심층 분석을
    # 생략한다. scan 이 실패한 것과 구분하려고 별도 skip_reason 을 쓴다.
    today_count = await analysis_store.count_reports_today(brand_id)
    if today_count >= settings.seller_report_daily_cap:
        await _record_run(brand_id, skip_reason="daily_cap_reached")
        return BrandBatchOutcome(
            brand_id, ran=True, report_generated=False, skip_reason="daily_cap_reached"
        )

    # ③ 심층 분석 — resident.run_analysis 는 저장까지 스스로 담당하고 반환값이 없다(#598).
    # 분석 기간은 브랜드 축과 같은 "전날 1일"(10-TRIGGER.md §5.3) — 4워커(behavior·churn·
    # conversion·sales_anomaly)는 이 기간을 자기 SOP 안에서 필요한 비교 구간으로 확장한다.
    return await _run_resident(brand_id, trigger_type, target_date, target_date)


async def _run_resident(
    brand_id: int, trigger_type: TriggerType, period_from: date, period_to: date
) -> BrandBatchOutcome:
    try:
        await resident.run_analysis(
            brand_id, trigger_type=trigger_type, period_from=period_from, period_to=period_to
        )
    except Exception as exc:  # noqa: BLE001 - 심층 분석 실패는 스캔 성공과 별개로 기록한다
        logger.error("brand_id=%s 심층 분석 실패(%r)", brand_id, exc)
        await _record_run(brand_id, skip_reason="resident_failed")
        return BrandBatchOutcome(
            brand_id, ran=True, report_generated=False, skip_reason="resident_failed"
        )

    await _record_run(brand_id, skip_reason=None)
    # resident.run_analysis 자체가 "4워커 전부 finding 없음"이면 조용히 저장을 생략한다
    # (그 함수 docstring) — 이 배치 계층에서는 그 구분까지 관측하지 않는다(로그로 충분,
    # report_generated=True 는 "심층 분석을 시도했다"는 뜻으로 읽는다).
    return BrandBatchOutcome(brand_id, ran=True, report_generated=True, skip_reason=None)


async def run_daily_batch(*, trigger_type: TriggerType = "scheduled_daily") -> None:
    """무인 일일 배치 — 활성 브랜드를 동시성 상한 안에서 순회한다(`seller_analysis_daily_cron`)."""
    settings = get_settings()
    target_date = today_kst() - timedelta(days=1)  # "전날"(KST) — 결정 96, 컨테이너 TZ 무관
    brand_ids = await analysis_store.list_active_targets()
    if not brand_ids:
        logger.info("무인 일일 배치: 순회 대상 브랜드가 없다(list_active_targets 빈 목록)")
        return

    client = get_spring_client()
    semaphore = asyncio.Semaphore(settings.seller_batch_concurrency)

    async def _bounded(brand_id: int) -> BrandBatchOutcome:
        async with semaphore:
            return await _run_locked(
                brand_id,
                lambda: _run_daily_chain(
                    brand_id,
                    trigger_type=trigger_type,
                    target_date=target_date,
                    settings=settings,
                    client=client,
                ),
                settings=settings,
            )

    outcomes = await asyncio.gather(*(_bounded(bid) for bid in brand_ids), return_exceptions=True)
    _log_batch_summary("daily", target_date, brand_ids, outcomes)

    # 정리 단계(②~⑤, 08-PERSISTENCE.md §5 결정 68)는 브랜드 순회 밖에서 전역 1회 —
    # 스냅샷 저장(①)이 방금 위 루프에서 전부 끝난 뒤라야 "방금 만든 스냅샷을 정리가
    # 지운다"는 순서 착오가 안 생긴다. 정리 자체의 실패는 다음 예외에서 삼켜져 이 함수
    # 전체를 죽이지 않는다.
    try:
        await run_cleanup_batch()
    except Exception:  # noqa: BLE001 - 정리 실패가 이미 끝난 브랜드 분석 결과를 무효화하면 안 된다
        logger.error("무인 일일 배치: 정리 단계 실패", exc_info=True)


async def run_weekly_batch() -> None:
    """무인 주간 배치 — 신호와 무관하게 브랜드마다 1건(`seller_analysis_weekly_cron`, 월요일 05:00 KST).

    스캔(①②)을 건너뛰고 `resident.run_analysis`를 직접 부른다(`10-TRIGGER.md` §5.1
    "④ 주간 정기 — 신호 무관 1건"). 분석 기간은 지난 7일(월요일 실행이면 지난주
    월요일~일요일)이다. `resident.run_analysis`가 4워커 전부 finding 없음으로 판단하면
    이 배치도 조용히 보고서를 생략한다(그 함수 자체 규약, §5.1의 "무조건 생성"은
    "스캔 게이트를 안 거친다"는 뜻이지 "빈 보고서를 강제로 만든다"는 뜻이 아니다).
    """
    settings = get_settings()
    yesterday = today_kst() - timedelta(days=1)
    period_from = yesterday - timedelta(days=6)
    period_to = yesterday
    brand_ids = await analysis_store.list_active_targets()
    if not brand_ids:
        logger.info("무인 주간 배치: 순회 대상 브랜드가 없다(list_active_targets 빈 목록)")
        return

    semaphore = asyncio.Semaphore(settings.seller_batch_concurrency)

    async def _bounded(brand_id: int) -> BrandBatchOutcome:
        async with semaphore:
            return await _run_locked(
                brand_id,
                lambda: _run_resident(brand_id, "scheduled_weekly", period_from, period_to),
                settings=settings,
            )

    outcomes = await asyncio.gather(*(_bounded(bid) for bid in brand_ids), return_exceptions=True)
    _log_batch_summary("weekly", period_to, brand_ids, outcomes)


async def run_manual_analysis(
    brand_id: int, *, period_from: date | None = None, period_to: date | None = None
) -> BrandBatchOutcome:
    """수동 실행(⑤) — 스캔 게이트 없이 심층 분석을 즉시 1회 실행한다(`api/internal.py`가 부른다).

    기본 기간은 "전날 1일"(예약 일간 배치와 동일 정의) — 데모·재현 용도로 기간을 직접
    지정할 수 있게 `period_from`/`period_to`를 선택 인자로 열어 둔다(10-TRIGGER.md §7
    "데모는 수동 실행 API로 덮는다"). 예약 배치와 같은 `mutation_lock`을 타므로 마침
    그 브랜드의 예약 배치가 도는 중이면 그 배치가 끝날 때까지 대기 후 실행된다(직렬화,
    중복 실행 방지) — 요청을 막지 않고 최대 `seller_batch_brand_timeout_s`까지 대기한다.
    """
    settings = get_settings()
    target = today_kst() - timedelta(days=1)
    resolved_from = period_from if period_from is not None else target
    resolved_to = period_to if period_to is not None else target
    return await _run_locked(
        brand_id,
        lambda: _run_resident(brand_id, "manual", resolved_from, resolved_to),
        settings=settings,
    )


def _log_batch_summary(
    label: Literal["daily", "weekly"],
    as_of: date,
    brand_ids: list[int],
    outcomes: list[BrandBatchOutcome | BaseException],
) -> None:
    generated = sum(1 for o in outcomes if isinstance(o, BrandBatchOutcome) and o.report_generated)
    failed = sum(
        1
        for o in outcomes
        if isinstance(o, BaseException)
        or (isinstance(o, BrandBatchOutcome) and o.skip_reason in ("batch_error", "batch_timeout"))
    )
    logger.info(
        "무인 %s 배치 완료 as_of=%s brands=%d generated=%d failed=%d",
        label,
        as_of.isoformat(),
        len(brand_ids),
        generated,
        failed,
    )
    if failed:
        # [#325 선례] apscheduler/gather 는 개별 실패를 삼킨다 — 부분 실패도 운영 알람
        # 대상이라 error 로 다시 남긴다(conversation_retention_sweep 과 같은 관행).
        logger.error("무인 %s 배치 부분 실패 failed=%d/%d", label, failed, len(brand_ids))
