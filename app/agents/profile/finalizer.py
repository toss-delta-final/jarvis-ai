"""Spring I-20과 AI inactivity timeout이 공유하는 프로필 세션 finalizer (이슈 #79)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Awaitable, Callable

from app.agents.profile import processed_events, session_activity
from app.agents.profile.builder import ConsolidationResult, consolidate, generate_session_delta
from app.agents.profile.session_activity import ActivityClaim, SessionActivity
from app.agents.profile.store import ProfileStore, get_profile_store
from app.core.config import Settings, get_settings
from app.core.conversation import conversation_key
from app.core.llm import LLMClient, get_llm
from app.core.session_context import ProfileRecoveryCandidate, SessionContextRepository

logger = logging.getLogger(__name__)


class ProfilePhaseStatus(StrEnum):
    COMPLETED = "completed"
    NO_WORK = "no_work"
    DUPLICATE = "duplicate"
    RETRYABLE = "retryable"


@dataclass(frozen=True)
class ProfilePhaseResult:
    status: ProfilePhaseStatus


@dataclass(frozen=True)
class ProfileJoinResult:
    finalization_id: str
    event_id: str
    result: ProfilePhaseResult
    joined: bool


@dataclass(frozen=True)
class _ActiveProfileTask:
    finalization_id: str
    event_id: str
    task: asyncio.Task[ProfilePhaseResult]


class _ProfileClaimLost(Exception):
    """DB CAS를 얻지 못했음을 public profile 결과와 구분하는 내부 제어 신호."""


class ActiveProfileTaskRegistry:
    """한 프로세스에서 context별 profile LLM 작업을 하나로 직렬화한다."""

    def __init__(self) -> None:
        self._active: dict[str, _ActiveProfileTask] = {}

    def _task_done(
        self,
        context_id: str,
        active: _ActiveProfileTask,
        task: asyncio.Task[ProfilePhaseResult],
    ) -> None:
        if self._active.get(context_id) is active:
            del self._active[context_id]
        if not task.cancelled():
            task.exception()

    async def join_or_start(
        self,
        context_id: str,
        finalization_id: str,
        event_id: str,
        factory: Callable[[], Awaitable[ProfilePhaseResult]],
    ) -> ProfileJoinResult:
        active = self._active.get(context_id)
        joined = active is not None
        if active is None:
            task = asyncio.create_task(factory())
            active = _ActiveProfileTask(finalization_id, event_id, task)
            # create_task() 뒤 첫 await 전에 task와 journal identity를 함께 게시한다.
            self._active[context_id] = active
            task.add_done_callback(
                lambda done, item=active: self._task_done(context_id, item, done)
            )
        result = await asyncio.shield(active.task)
        return ProfileJoinResult(
            active.finalization_id,
            active.event_id,
            result,
            joined,
        )


_active_profile_tasks = ActiveProfileTaskRegistry()


class FinalizationStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    RETRYABLE = "retryable"


@dataclass(frozen=True)
class FinalizationResult:
    status: FinalizationStatus


async def process_profile_checkpoint(
    user_id: int,
    session_id: str,
    *,
    event_id: str,
    profile_watermark: int,
    settings: Settings,
) -> ProfilePhaseResult:
    """고정된 lifecycle watermark 하나를 generation-scoped event로 처리한다."""
    token: str | None = None
    completed = False
    try:
        token = await processed_events.claim_event(
            event_id,
            lease_s=settings.session_end_claim_ttl_s,
        )
        if token is None:
            status = await processed_events.get_status(event_id)
            return ProfilePhaseResult(
                ProfilePhaseStatus.DUPLICATE
                if status == "completed"
                else ProfilePhaseStatus.RETRYABLE
            )

        store = await get_profile_store()
        key = conversation_key(str(user_id), session_id)
        bounded = await store.get_session_ctx_upto(key, profile_watermark)
        if not bounded:
            completed = await processed_events.complete_claim(event_id, token)
            if not completed:
                return ProfilePhaseResult(ProfilePhaseStatus.RETRYABLE)
            return ProfilePhaseResult(ProfilePhaseStatus.NO_WORK)

        llm = get_llm()
        delta = await generate_session_delta(
            str(user_id),
            key,
            profile_watermark=profile_watermark,
            llm=llm,
            settings=settings,
        )
        if delta is None:
            # 최초 bounded read 뒤 cap trimming이 watermark 이하를 모두 밀어낸 경우도
            # 성공 NO_WORK다. watermark 밖 항목만 남은 상태를 LLM 장애로 오분류하지 않는다.
            if not await store.get_session_ctx_upto(key, profile_watermark):
                completed = await processed_events.complete_claim(event_id, token)
                if not completed:
                    return ProfilePhaseResult(ProfilePhaseStatus.RETRYABLE)
                return ProfilePhaseResult(ProfilePhaseStatus.NO_WORK)
            return ProfilePhaseResult(ProfilePhaseStatus.RETRYABLE)
        consolidation = await consolidate(str(user_id), llm=llm, settings=settings)
        if consolidation is ConsolidationResult.FAILED:
            return ProfilePhaseResult(ProfilePhaseStatus.RETRYABLE)
        await store.clear_session_ctx_upto(key, profile_watermark)
        completed = await processed_events.complete_claim(event_id, token)
        if not completed:
            return ProfilePhaseResult(ProfilePhaseStatus.RETRYABLE)
        return ProfilePhaseResult(ProfilePhaseStatus.COMPLETED)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("profile checkpoint 처리 실패 — 재시도 필요", exc_info=True)
        return ProfilePhaseResult(ProfilePhaseStatus.RETRYABLE)
    finally:
        if token is not None and not completed:
            await release_processed_claim_best_effort(event_id, token)


async def process_recoverable_profile_phases(
    batch_size: int,
) -> list[ProfilePhaseResult]:
    """완료된 transient journal의 profile phase를 context별 단일 task로 복구한다."""
    from app.core import session_context

    return await _process_recoverable_profile_phases(
        session_context._default_repository,
        batch_size,
    )


async def _process_recoverable_profile_phases(
    repository: SessionContextRepository,
    batch_size: int,
) -> list[ProfilePhaseResult]:
    """주입된 lifecycle repository에서 public recovery와 같은 순서를 실행한다."""
    settings = get_settings()
    candidates = await repository.list_recoverable_profile_phases(batch_size)
    semaphore = asyncio.Semaphore(settings.profile_idle_max_concurrency)

    async def process(candidate: ProfileRecoveryCandidate) -> ProfilePhaseResult | None:
        async with semaphore:
            try:
                return await _process_profile_candidate(candidate, repository, settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "profile recovery candidate 실패 finalization_id=%s",
                    candidate.finalization_id,
                    exc_info=True,
                )
                return None

    results = await asyncio.gather(*(process(candidate) for candidate in candidates))
    return [result for result in results if result is not None]


async def _process_profile_candidate(
    candidate: ProfileRecoveryCandidate,
    repository: SessionContextRepository,
    settings: Settings,
) -> ProfilePhaseResult | None:
    """후보 하나를 local slot→DB CAS 순서로 처리하고 다른 identity join 뒤 재검증한다."""
    from app.core.session_context import SessionClaimConflict

    while True:
        try:
            journal = await repository.get_finalization(candidate.finalization_id)
        except SessionClaimConflict:
            return None
        if journal.status == "superseded" or journal.profile_status not in (
            "pending",
            "retryable",
            "processing",
        ):
            return None
        event_id = processed_events.profile_phase_event_id(
            candidate.context_id,
            candidate.generation,
            journal.reason,
        )

        async def _claim_and_process() -> ProfilePhaseResult:
            claim = await repository.claim_profile_phase(
                candidate.finalization_id,
                settings.profile_idle_claim_ttl_s,
            )
            if claim is None:
                raise _ProfileClaimLost
            try:
                result = await process_profile_checkpoint(
                    int(candidate.owner_id),
                    candidate.session_id,
                    event_id=event_id,
                    profile_watermark=candidate.profile_watermark,
                    settings=settings,
                )
            except asyncio.CancelledError:
                retry_record = asyncio.create_task(
                    repository.record_claimed_profile_phase(
                        candidate.finalization_id,
                        claim.claim_token,
                        "retryable",
                    )
                )
                try:
                    await asyncio.shield(retry_record)
                except Exception:
                    logger.warning(
                        "취소된 profile phase retryable 기록 실패 — lease 만료 후 복구",
                        exc_info=True,
                    )
                raise
            phase_status = (
                "retryable" if result.status is ProfilePhaseStatus.RETRYABLE else "completed"
            )
            try:
                await repository.record_claimed_profile_phase(
                    candidate.finalization_id,
                    claim.claim_token,
                    phase_status,
                )
            except SessionClaimConflict:
                pass
            return result

        try:
            joined = await _active_profile_tasks.join_or_start(
                candidate.context_id,
                candidate.finalization_id,
                event_id,
                _claim_and_process,
            )
        except _ProfileClaimLost:
            try:
                refreshed = await repository.get_finalization(candidate.finalization_id)
            except SessionClaimConflict:
                return None
            if refreshed.status == "superseded" or refreshed.profile_status not in (
                "pending",
                "retryable",
                "processing",
            ):
                return None
            if await repository.is_profile_phase_recoverable(candidate.finalization_id):
                continue
            return None
        if joined.finalization_id == candidate.finalization_id and joined.event_id == event_id:
            return joined.result

        # 다른 세대 task를 join한 결과를 이 candidate에 기록하지 않는다.
        try:
            refreshed = await repository.get_finalization(candidate.finalization_id)
        except SessionClaimConflict:
            return None
        if refreshed.status == "superseded" or refreshed.profile_status not in (
            "pending",
            "retryable",
            "processing",
        ):
            return None


async def release_processed_claim_best_effort(
    event_id: str,
    token: str,
    *,
    log: logging.Logger | None = None,
) -> None:
    """취소 중에도 processed-event claim 해제를 마치고 DB 실패는 lease 복구에 맡긴다."""
    target_log = log or logger
    release_task = asyncio.create_task(processed_events.release_claim(event_id, token))
    try:
        await asyncio.shield(release_task)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        outer_cancelled = task is not None and task.cancelling() > 0
        try:
            await release_task
        except BaseException:  # stale cleanup result 회수; 실제 outer cancellation은 아래서 재전파
            pass
        if outer_cancelled:
            raise
        target_log.warning("session-end claim 해제 task 취소 — lease 만료 후 재시도")
    except Exception:
        target_log.warning("session-end claim 해제 실패 — lease 만료 후 재시도", exc_info=True)


async def _release_activity_claim_best_effort(
    claim: ActivityClaim,
    *,
    log: logging.Logger,
) -> None:
    try:
        await session_activity.release_claim(claim)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("profile idle activity claim 해제 실패 — lease 만료 후 재시도", exc_info=True)


async def _complete_activity_best_effort(
    user_id: int,
    session_id: str,
    claim: ActivityClaim | None,
    *,
    log: logging.Logger,
) -> bool:
    try:
        completed = await session_activity.complete_session(
            user_id,
            session_id,
            token=claim.claim_token if claim is not None else None,
        )
        if not completed:
            log.warning("profile session activity 완료 ownership 상실 — 다음 sweep이 복구")
        return completed
    except asyncio.CancelledError:
        raise
    except Exception:
        # 호출자가 retryable로 집계하고 finally에서 activity/processed claim을 해제한다.
        log.warning("profile session activity 완료 기록 실패 — 재시도 필요", exc_info=True)
        return False


async def _complete_terminal_activity_best_effort(
    user_id: int,
    session_id: str,
    observed: SessionActivity | None,
    *,
    log: logging.Logger,
) -> bool:
    try:
        completed = await session_activity.complete_terminal_session(
            user_id,
            session_id,
            observed=observed,
        )
        if not completed:
            log.info("session-end 처리 중 새 activity 감지 — terminal 완료 취소")
        return completed
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("profile terminal activity 완료 기록 실패 — 재시도 필요", exc_info=True)
        return False


async def finalize_profile_session(
    user_id: str | int,
    session_id: str,
    *,
    activity_claim: ActivityClaim | None = None,
    terminal: bool = True,
    settings: Settings | None = None,
    store_factory: Callable[[], Awaitable[ProfileStore]] | None = None,
    llm_factory: Callable[[], LLMClient | None] | None = None,
    log: logging.Logger | None = None,
) -> FinalizationResult:
    """한 세션 버퍼를 실패 안전 멱등 lifecycle로 처리한다.

    외부 I-20은 ``terminal=True``로 fixed dedup을 완료한다. idle scheduler는
    ``terminal=False``로 같은 claim을 세션 단위 mutex로만 쓰고 성공 뒤 해제하여, 같은
    sessionId가 재활동하면 다음 idle checkpoint를 다시 처리할 수 있게 한다.
    CancelledError만 호출자에게 재전파한다.
    """
    target_log = log or logger
    dedup_key: str | None = None
    processed_token: str | None = None
    processed_completed = False
    activity_completed = False
    terminal_activity: SessionActivity | None = None

    try:
        resolved_settings = settings or get_settings()
        numeric_user_id = int(user_id)
        user_key = str(numeric_user_id)
        key = conversation_key(user_key, session_id)
        dedup_key = processed_events.session_end_event_id(numeric_user_id, session_id)
        processed_token = await processed_events.claim_event(
            dedup_key,
            lease_s=resolved_settings.session_end_claim_ttl_s,
        )
        if processed_token is None:
            return FinalizationResult(FinalizationStatus.DUPLICATE)

        if terminal:
            terminal_activity = await session_activity.get_session(numeric_user_id, session_id)
            if not await processed_events.claim_is_current(dedup_key, processed_token):
                return FinalizationResult(FinalizationStatus.RETRYABLE)

        factory = store_factory or get_profile_store
        store = await factory()
        buffer, profile_watermark = await store.get_session_ctx_snapshot(key)
        if buffer:
            resolved_llm = (llm_factory or get_llm)()
            result = await generate_session_delta(
                user_key,
                key,
                profile_watermark=profile_watermark,
                llm=resolved_llm,
                settings=resolved_settings,
            )
            if result is None:
                return FinalizationResult(FinalizationStatus.RETRYABLE)
            _, watermark = result
            consolidation = await consolidate(
                user_key,
                llm=resolved_llm,
                settings=resolved_settings,
            )
            if consolidation is ConsolidationResult.FAILED:
                return FinalizationResult(FinalizationStatus.RETRYABLE)
            # 처리 중 추가된 새 발화(seq > watermark)는 보존한다.
            await store.clear_session_ctx_upto(key, watermark)

        if terminal:
            activity_completed = await _complete_terminal_activity_best_effort(
                numeric_user_id,
                session_id,
                terminal_activity,
                log=target_log,
            )
            if not activity_completed:
                return FinalizationResult(FinalizationStatus.RETRYABLE)
            processed_completed = await processed_events.complete_claim(dedup_key, processed_token)
            if not processed_completed:
                raise RuntimeError("session-end claim ownership lost")
        else:
            activity_completed = await _complete_activity_best_effort(
                numeric_user_id,
                session_id,
                activity_claim,
                log=target_log,
            )
            if not activity_completed:
                return FinalizationResult(FinalizationStatus.RETRYABLE)
        return FinalizationResult(FinalizationStatus.ACCEPTED)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - I-20 best-effort 및 idle 재시도 경계
        target_log.warning("session-end 내부 처리 실패 — 202 degrade", exc_info=True)
        return FinalizationResult(FinalizationStatus.RETRYABLE)
    finally:
        if dedup_key is not None and processed_token is not None and not processed_completed:
            await release_processed_claim_best_effort(dedup_key, processed_token, log=target_log)
        if activity_claim is not None and not activity_completed:
            await _release_activity_claim_best_effort(activity_claim, log=target_log)
