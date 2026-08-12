"""I-17 증분 pull + session lifecycle migration/sweep 스케줄러.

APScheduler AsyncIOScheduler는 FastAPI lifespan의 event loop에 결합한다. lifecycle sweep은
loop-bound pg-profile authority와 legacy GC를 같은 loop에서 안전하게 사용한다.
동기 I-17 job은 기본 executor에서 실행되고 내부 ``asyncio.run()``으로 독립 배치를 완결한다.

Session backfill은 lifespan 초기화가 scheduler 시작 전에 완료한다.

[MVP 단일 인스턴스 전제, PR #42 리뷰] AsyncIOScheduler는 프로세스 로컬 스케줄러라 분산
락·리더 선출이 없다 — 다중 인스턴스(uvicorn --workers, k8s replica 등)로 배포하면 인스턴스마다
독립적으로 같은 배치가 동시 실행돼 Google API 호출이 인스턴스 수만큼 배가된다. 이 리포는 아직
단일 인스턴스 배포만 지원한다(app/core/ratelimit.py·app/core/stream.py 와 동일 전제 — 두 곳 다
"다중 인스턴스 확장 시 Redis 이관" 문서화만 해두고 구현은 안 함). 다중 인스턴스 확장 시 이
스케줄러도 같은 방식(예: Redis 분산 락으로 리더만 잡 실행)으로 이관해야 한다.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.agents.buyer.session_state import get_legacy_gc_counters, run_legacy_gc_batch
from app.agents.seller.analysis.daily_batch import run_daily_batch, run_weekly_batch
from app.core.config import get_settings
from app.core.conversation import get_conversation_store
from app.core.session_lifecycle import (
    run_session_context_sweep as run_configured_session_context_sweep,
)
from app.pipelines.artifacts_batch import run_artifacts_batch

_log = logging.getLogger(__name__)
_I17_JOB_ID = "i17_incremental_pull"
_SESSION_CONTEXT_JOB_ID = "session_context_sweep"
_CONVERSATION_RETENTION_JOB_ID = "conversation_retention_sweep"
_SELLER_ANALYSIS_DAILY_JOB_ID = "seller_analysis_daily"
_SELLER_ANALYSIS_WEEKLY_JOB_ID = "seller_analysis_weekly"
# 하위 호환 — 기존 내부 테스트/운영 도구가 참조하던 이름.
_JOB_ID = _I17_JOB_ID


async def _run_seller_daily_batch() -> None:
    """무인 일일 배치 잡(#601) — 실패를 삼켜 스케줄러 프로세스를 지키고, 다음 주기가 이어받는다.

    `run_daily_batch` 내부가 브랜드 단위로 이미 격리돼 있으므로(하나 실패해도 나머지는
    계속) 여기서 잡는 예외는 브랜드 루프 밖(예: `list_active_targets` 자체 실패) 뿐이다.
    """
    try:
        await run_daily_batch()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - job 실패 격리, 다음 KST 00:20 주기가 재개
        _log.exception("무인 일일 배치(seller_analysis_daily) 실패 — 다음 주기 재개")


async def _run_seller_weekly_batch() -> None:
    """무인 주간 배치 잡(#601) — 실패를 삼켜 스케줄러 프로세스를 지킨다."""
    try:
        await run_weekly_batch()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - job 실패 격리, 다음 주 재개
        _log.exception("무인 주간 배치(seller_analysis_weekly) 실패 — 다음 주기 재개")


_scheduler: AsyncIOScheduler | None = None


def _run_incremental_batch() -> None:
    """AsyncIO scheduler 기본 executor에서 동기 호출 — 자체 이벤트루프로 증분 배치 1회 완결.

    잡 실패가 스케줄러 프로세스를 죽이면 안 되므로 예외를 삼키고 로그만 남긴다
    (다음 주기에 저장된 커서부터 자연 재개, §4.8).
    """
    try:
        result = asyncio.run(run_artifacts_batch(full_rebuild=False))
        _log.info(
            "scheduler 증분 배치 완료: processed=%d hidden=%d pages=%d failed=%d recovered=%d "
            "retry_pending=%d cursor=%s",
            result.processed,
            result.hidden,
            result.pages,
            result.failed,
            result.recovered,
            result.retry_pending,
            result.cursor,
        )
        if result.failed > 0:
            # [#325] apscheduler는 예외를 삼켜 ERROR 한 줄 외에 실패가 안 드러난다 — 부분 실패도
            # 운영 알람 대상이므로 error 로 남긴다(warning 아님).
            _log.error("증분 배치 부분 실패: failed=%d — dead-letter 로그 확인", result.failed)
    except Exception:  # noqa: BLE001 - 잡 실패 격리(다음 주기 자연 재개)
        _log.exception("scheduler 증분 배치 실패 — 다음 주기 재개")


async def run_session_context_sweep() -> None:
    """Run the sole lifecycle authority sweep plus one restart-safe legacy GC batch."""
    try:
        result = await run_configured_session_context_sweep()
        gc_deleted = await run_legacy_gc_batch()
        filters_deleted, cart_deleted, revert_deleted = await get_legacy_gc_counters()
        _log.info(
            "session context sweep 완료 claimed=%d examined=%d completed=%d retryable=%d "
            "skipped=%d superseded_skipped=%d invalid_recovery=%d recovered=%d "
            "examined_limit_reached=%s gc_deleted=%d filters_deleted=%d "
            "cart_deleted=%d revert_deleted=%d",
            result.claimed,
            result.examined,
            result.completed,
            result.retryable,
            result.skipped,
            result.superseded_skipped,
            result.invalid_recovery,
            result.recovered,
            result.examined_limit_reached,
            gc_deleted,
            filters_deleted,
            cart_deleted,
            revert_deleted,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - job 실패 격리, activity lease가 다음 sweep 복구
        _log.exception("session context sweep 실패 — 다음 주기 재개")


async def run_conversation_retention_sweep() -> None:
    """대화 전사록 보존 스윕(이슈 #321, SPEC-PROFILE-001 OPEN-P5 해소) — 별도 job.

    `run_session_context_sweep` 에 얹지 않는다 — 그 job 은 "sole lifecycle authority"이고
    except 경로의 의미가 "activity lease 가 다음 sweep 에서 복구한다"라 실패한 DELETE 의 의미와
    다르다. 게다가 그 job 은 60초 주기라(`profile_idle_sweep_interval_s`) 이 스윕에는 60배
    과하다.

    `ConversationStoreProtocol.purge_expired_turns` 는 **배치 한 번**만 지운다 —
    `conversation_retention_max_batches` 까지 반복 호출해 유계 배치 삭제를 구성한다(인메모리·
    pg-profile 두 구현이 같은 계약이라 isinstance 분기가 필요 없다).
    """
    settings = get_settings()
    if not settings.conversation_retention_sweep_enabled:
        return
    store = await get_conversation_store()
    deleted = 0
    batches = 0
    cap_reached = False
    try:
        for _ in range(settings.conversation_retention_max_batches):
            batch_deleted = await store.purge_expired_turns(settings.conversation_retention_days)
            deleted += batch_deleted
            batches += 1
            if batch_deleted < settings.conversation_retention_batch_size:
                break
        else:
            cap_reached = True
        if cap_reached:
            # [#325] apscheduler 는 예외를 삼켜 부분 실패가 드러나지 않는다 — 배치 상한 도달은
            # "지울 것이 더 남았을 수 있다"는 백로그 신호라 운영 알람 대상(error)이다.
            _log.error(
                "conversation retention sweep cap 도달(백로그 의심) "
                "deleted=%d batches=%d cap_reached=%s cutoff_days=%s",
                deleted,
                batches,
                cap_reached,
                settings.conversation_retention_days,
            )
        else:
            _log.info(
                "conversation retention sweep 완료 "
                "deleted=%d batches=%d cap_reached=%s cutoff_days=%s",
                deleted,
                batches,
                cap_reached,
                settings.conversation_retention_days,
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - job 실패 격리, DELETE 는 멱등이라 다음 주기가 이어받는다
        _log.exception("conversation retention sweep 실패 — 다음 주기 재개")


def start_scheduler() -> AsyncIOScheduler:
    """스케줄러를 시작하고 인스턴스를 반환한다 (멱등 — 이미 떠 있으면 그대로 반환).

    lifecycle sweep은 provider 키와 무관하게 항상 등록한다. GOOGLE_API_KEY가 없거나 scripted
    LLM이면 I-17 job만 생략한다. scripted 결과로 실 카탈로그 enrichment와 cursor를 오염시키지
    않기 위한 안전 경계다. 호출은 FastAPI lifespan 등 실행 중인 event loop 안에서 해야 한다.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    settings = get_settings()
    scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())
    scheduler.add_job(
        run_session_context_sweep,
        IntervalTrigger(seconds=settings.profile_idle_sweep_interval_s),
        id=_SESSION_CONTEXT_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.conversation_retention_sweep_enabled:
        scheduler.add_job(
            run_conversation_retention_sweep,
            IntervalTrigger(seconds=settings.conversation_retention_sweep_interval_s),
            id=_CONVERSATION_RETENTION_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    # [이슈 #601] 무인 판매자 분석 — 시각 고정 실행이라 IntervalTrigger가 아니라
    # CronTrigger가 필요하다(10-TRIGGER.md §2 결정 95). timezone을 **명시**한다 —
    # CronTrigger(hour=0, minute=20)만 쓰면 컨테이너 TZ(보통 UTC) 기준이 되어 KST 09:20에
    # 도는 사고가 난다(그 §2의 실측 경고와 동일).
    scheduler.add_job(
        _run_seller_daily_batch,
        CronTrigger.from_crontab(
            settings.seller_analysis_daily_cron, timezone=settings.seller_analysis_cron_timezone
        ),
        id=_SELLER_ANALYSIS_DAILY_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _run_seller_weekly_batch,
        CronTrigger.from_crontab(
            settings.seller_analysis_weekly_cron, timezone=settings.seller_analysis_cron_timezone
        ),
        id=_SELLER_ANALYSIS_WEEKLY_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.google_api_key and settings.llm_provider != "scripted":
        scheduler.add_job(
            _run_incremental_batch,
            IntervalTrigger(seconds=settings.catalog_batch_interval_s),
            id=_I17_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    elif not settings.google_api_key:
        _log.warning(
            "GOOGLE_API_KEY 미설정 — I-17 job만 비활성화합니다 "
            "(session lifecycle sweep은 계속 실행됩니다)"
        )
    else:
        _log.warning(
            "scripted LLM 부하 테스트 모드 — 카탈로그 오염 방지를 위해 I-17 job만 "
            "비활성화합니다 (session lifecycle sweep은 계속 실행됩니다)"
        )
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    """스케줄러를 정지한다 (앱 종료·테스트 격리 공용)."""
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except RuntimeError:
            # 테스트/비정상 종료에서 scheduler가 묶였던 loop가 먼저 닫힌 경우.
            pass
        _scheduler = None
