"""Cross-component coordination for session lifecycle transitions."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, Protocol

from app.agents.buyer import session_state
from app.agents.buyer.session_state import CleanupCounts
from app.agents.profile.store import ProfileStore, get_profile_store
from app.agents.profile.finalizer import (
    _process_profile_candidate,
    _process_recoverable_profile_phases,
)
from app.core.config import get_settings
from app.core.conversation import conversation_key
from app.core import session_context
from app.core.observability import message_fingerprint
from app.core.session_context import (
    BuyerSessionInput,
    ClaimOutcome,
    FinalizationClaim,
    ProfileRecoveryCandidate,
    SessionActive,
    SessionClaimConflict,
    SessionContext,
    SessionContextRepository,
    SessionFinalizing,
    SessionForbidden,
    TerminalOutcome,
)
from app.core.stream import ActiveStreamRegistry, StreamScopeFence, get_registry
from app.schemas.events import SessionClaimEvent

logger = logging.getLogger(__name__)


class _ProfileStoreFactory(Protocol):
    def __call__(self) -> ProfileStore | Awaitable[ProfileStore]: ...


FinalizationStatus = Literal["completed", "retryable", "skipped"]
FinalizationSkipReason = Literal["active", "superseded", "invalid"] | None


@dataclass(frozen=True)
class FinalizationOutcome:
    """One transient-phase attempt, including safe retry/skip classification."""

    status: FinalizationStatus
    cleanup: CleanupCounts = CleanupCounts()
    skip_reason: FinalizationSkipReason = None


@dataclass(frozen=True)
class IdleSweepResult:
    """Bounded recovery-first lifecycle sweep evidence."""

    claimed: int = 0
    recovered: int = 0
    completed: int = 0
    retryable: int = 0
    skipped: int = 0
    superseded_skipped: int = 0
    invalid_recovery: int = 0
    examined: int = 0
    examined_limit_reached: bool = False


@dataclass(frozen=True)
class _ClaimSnapshot:
    context: SessionContext | None
    history: tuple[str, str] | None
    authority_source: Literal["runtime", "legacy_backfill"] | None = None


class SessionLifecycleCoordinator:
    """Serialize owner claims with lifecycle state and process-local active streams."""

    def __init__(
        self,
        repository: SessionContextRepository | None = None,
        registry: ActiveStreamRegistry | None = None,
        *,
        profile_store_factory: _ProfileStoreFactory | None = None,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._profile_store_factory = profile_store_factory

    async def claim_owner(self, event: SessionClaimEvent) -> ClaimOutcome:
        repository = self._repository or session_context._default_repository
        registry = self._registry or get_registry()
        snapshot = _ClaimSnapshot(None, None)
        fence: StreamScopeFence | None = None
        outcome_name = "error"
        try:
            # The repository's public claim_owner() acquires this same lock.  Coordinate
            # the state read, stream guard and its on-connection transition here instead
            # so PostgreSQL uses one advisory lock and one transaction throughout.
            async with repository.lock_session(event.session_id) as uow:
                snapshot = await _read_claim_snapshot(repository, uow.conn, event.session_id)
                target = str(event.user_id)
                context = snapshot.context

                # 멱등 duplicate 단축 반환에도 상태 게이트를 건다. 아래 else 절의
                # idle_finalizing/terminal 검사보다 먼저 평가되므로, 상태를 안 보면
                # 이 분기 자체가 게이트의 예외가 된다 — 정리 진행 중(idle_finalizing)이나
                # 종료된(terminal) 세션에 같은 claim 이 재전송되면 409 대신 조용히
                # 성공 처리된다(SPEC-CHAT-SESSION-CONTEXT-187 §상태기계: 새 touch/claim 은 409).
                if (
                    snapshot.history == (event.guest_id, target)
                    and context is not None
                    and context.owner_type == "member"
                    and context.owner_id == target
                    and context.state not in ("idle_finalizing", "terminal")
                ):
                    outcome = ClaimOutcome(context, False)
                    await repository._resolve_authoritative_conflict(uow.conn, context)
                    outcome_name = "duplicate"
                else:
                    # finalizing 게이트에는 legacy_backfill 예외를 두지 않는다. 아래 conflict
                    # 검사들의 예외는 마이그레이션 과도기 owner/terminal 불일치를 관용하려는
                    # 것이지만, idle_finalizing 은 소유권 문제가 아니라 진행 중인 정리 작업의
                    # 불변식이다. claim_expired_contexts 는 authority_source 를 거르지 않아
                    # legacy_backfill row 도 idle_finalizing 으로 전이되므로 이 경로는 도달
                    # 가능하고, 통과시키면 api-spec 의 "idle_finalizing 중 claim 은 409" 가
                    # 과도기 row 에서만 조용히 깨진다.
                    if context is not None and context.state == "idle_finalizing":
                        outcome_name = "finalizing"
                        raise SessionFinalizing
                    if (
                        snapshot.history is not None
                        or (
                            context is not None
                            and context.state == "terminal"
                            and snapshot.authority_source != "legacy_backfill"
                        )
                        or (
                            context is not None
                            and snapshot.authority_source != "legacy_backfill"
                            and (
                                context.owner_type != "guest" or context.owner_id != event.guest_id
                            )
                        )
                    ):
                        outcome_name = "claim_conflict"
                        raise SessionClaimConflict

                    # Fence atomicity: the in-memory backend keeps the original guarantee
                    # (acquire_fence() awaits nothing, so the active-scope check and the
                    # fence install cannot be preempted).  The shared backend cannot rely
                    # on the event loop, so it takes a pg_advisory_xact_lock on the scope
                    # key for the whole check-and-install transaction instead — see
                    # docs/specs/DESIGN-SHARED-STREAM-REGISTRY-476.md §3.  Either way the
                    # guarantee is independent of the DB rows this transaction holds.
                    fence = await registry.acquire_fence(event.guest_id, event.session_id)
                    if fence is None:
                        raise SessionActive
                    outcome = await _transition_claim(repository, uow.conn, event)
                    snapshot = _ClaimSnapshot(outcome.context, snapshot.history, "runtime")
                    outcome_name = "accepted"
            return outcome
        except SessionActive:
            outcome_name = "active"
            raise
        finally:
            if fence is not None:
                await registry.release_fence(fence)
            _log_claim(event, snapshot.context, outcome_name)

    async def process_transient_claim(
        self,
        claim: FinalizationClaim,
    ) -> FinalizationOutcome:
        """Dispatch a claim by its durable reason; idle and terminal never share transitions."""
        if claim.reason == "idle":
            return await self.process_idle_transient(claim)
        return await self.process_terminal_transient(claim)

    async def begin_terminal(self, user_id: int, session_id: str) -> TerminalOutcome:
        """I-20 gate를 먼저 세운 뒤 기존 stream, transient, profile 순서로 마감한다."""
        repository = self._repository or session_context._default_repository
        registry = self._registry or get_registry()
        try:
            outcome = await repository.begin_terminal(user_id, session_id)
        except SessionForbidden:
            # Task 8의 legacy backfill 전 롤링 배포에서도 I-20은 새 lifecycle authority로만
            # 처리한다. 기존 row가 전혀 없는 경우에만 member context를 만들어 즉시 terminal로
            # 전환하며, 다른 owner의 row는 절대 덮어쓰지 않는다.
            if await repository.get_context(session_id) is not None:
                raise
            await repository.touch(
                BuyerSessionInput(
                    session_id,
                    f"session-end:{session_id}",
                    "member",
                    str(user_id),
                )
            )
            outcome = await repository.begin_terminal(user_id, session_id)

        # terminal state가 새 touch/claim을 이미 막은 상태에서 기존 stream만 자연 종료시킨다.
        await registry.wait_for_scope_idle(str(user_id), session_id)

        claim = outcome.claim
        while claim is not None:
            transient = await self.process_terminal_transient(claim)
            if transient.status == "skipped" and transient.skip_reason in ("active", "invalid"):
                if transient.skip_reason == "active":
                    await registry.wait_for_scope_idle(str(user_id), session_id)
                # 재조회 결과로 outcome 까지 갱신한다. claim 만 받아오면, lease 가 아직
                # 살아있어 begin_terminal 이 (claim=None, duplicate=True) 를 돌려줄 때
                # 루프가 조용히 끝나고 최초의 stale outcome(duplicate=False)이 반환된다 —
                # transient 정리를 한 번도 끝내지 못했는데 I-20 이 "accepted" 로 응답한다.
                outcome = await repository.begin_terminal(user_id, session_id)
                claim = outcome.claim
                continue
            if transient.status != "completed":
                return outcome
            claim = None

        journal = await repository.get_finalization(outcome.finalization.finalization_id)
        if (
            journal.transient_status == "completed"
            and journal.watermark_status == "captured"
            and journal.profile_watermark is not None
            and journal.profile_status in ("pending", "retryable")
        ):
            await _process_profile_candidate(
                ProfileRecoveryCandidate(
                    journal.finalization_id,
                    outcome.context.context_id,
                    outcome.context.session_id,
                    outcome.context.owner_id,
                    journal.generation,
                    journal.profile_watermark,
                ),
                repository,
                get_settings(),
            )
        return outcome

    async def process_idle_transient(
        self,
        claim: FinalizationClaim,
    ) -> FinalizationOutcome:
        if claim.reason != "idle":
            return FinalizationOutcome("skipped", skip_reason="invalid")
        prepared = await self._prepare_transient(claim, idle=True)
        if prepared is not None:
            return prepared
        return await self._delete_transient(claim, idle=True)

    async def process_terminal_transient(
        self,
        claim: FinalizationClaim,
    ) -> FinalizationOutcome:
        if claim.reason != "terminal":
            return FinalizationOutcome("skipped", skip_reason="invalid")
        prepared = await self._prepare_transient(claim, idle=False)
        if prepared is not None:
            return prepared
        return await self._delete_transient(claim, idle=False)

    async def _prepare_transient(
        self,
        claim: FinalizationClaim,
        *,
        idle: bool,
    ) -> FinalizationOutcome | None:
        """Phase A: validate/fence, durably gate, and capture the real profile watermark."""
        repository = self._repository or session_context._default_repository
        registry = self._registry or get_registry()
        fence = await registry.acquire_fence(claim.owner_id, claim.session_id)
        if fence is None:
            if idle:
                await self._abandon_idle_prephase(claim)
            return FinalizationOutcome("skipped", skip_reason="active")
        try:
            async with repository.lock_session(claim.session_id) as uow:
                context = await _validate_in_uow(repository, uow.conn, claim, for_idle=idle)
                # Recovery starts in Phase B and must never repeat the Phase A snapshot.
                already_prepared = (
                    context.state == "idle_finalizing"
                    if idle
                    else await _watermark_prepared_in_uow(repository, uow.conn, claim)
                )
                if not idle and context.state != "terminal":
                    raise SessionClaimConflict
                if not already_prepared:
                    watermark = await self._read_profile_watermark(context)
                    if idle:
                        context = await uow.prepare_idle_finalizing_with_watermark(
                            claim,
                            watermark,
                        )
                    elif watermark is not None:
                        await uow.capture_profile_watermark(claim, watermark)
            return None
        except asyncio.CancelledError:
            if idle:
                await self._abandon_idle_prephase_shielded(claim)
            raise
        except SessionClaimConflict:
            return await _classify_conflict(repository, claim)
        except Exception:
            if idle:
                await self._abandon_idle_prephase(claim)
            session_fp = message_fingerprint(claim.session_id)[1]
            logger.warning(
                "session transient Phase A 실패 sessionFp=%s finalization_id=%s",
                session_fp,
                claim.finalization_id,
                exc_info=True,
            )
            return FinalizationOutcome("retryable")
        finally:
            await registry.release_fence(fence)

    async def _read_profile_watermark(self, context: SessionContext) -> int | None:
        if context.owner_type == "guest":
            return None
        factory = self._profile_store_factory or get_profile_store
        profile_store = factory()
        if inspect.isawaitable(profile_store):
            profile_store = await profile_store
        _, watermark = await profile_store.get_session_ctx_snapshot(
            conversation_key(context.owner_id, context.session_id)
        )
        return watermark

    async def _abandon_idle_prephase(self, claim: FinalizationClaim) -> bool:
        repository = self._repository or session_context._default_repository
        try:
            async with repository.lock_session(claim.session_id) as uow:
                abandoned = await uow.abandon_idle_prephase(claim)
        except Exception:
            abandoned = False
            logger.warning(
                "idle prephase abandon 실패 sessionFp=%s finalization_id=%s",
                message_fingerprint(claim.session_id)[1],
                claim.finalization_id,
                exc_info=True,
            )
        else:
            if not abandoned:
                logger.warning(
                    "idle prephase abandon 거부 sessionFp=%s finalization_id=%s",
                    message_fingerprint(claim.session_id)[1],
                    claim.finalization_id,
                )
        return abandoned

    async def _abandon_idle_prephase_shielded(self, claim: FinalizationClaim) -> None:
        abandon_task = asyncio.create_task(self._abandon_idle_prephase(claim))
        try:
            await asyncio.shield(abandon_task)
        except asyncio.CancelledError:
            try:
                await abandon_task
            except BaseException:
                # _abandon_idle_prephase already records ordinary failures. Preserve
                # the original caller cancellation even if cleanup itself is cancelled.
                pass

    async def _delete_transient(
        self,
        claim: FinalizationClaim,
        *,
        idle: bool,
    ) -> FinalizationOutcome:
        """Phase B: re-lock, revalidate, delete idempotently, then record completion."""
        repository = self._repository or session_context._default_repository
        registry = self._registry or get_registry()
        fence = await registry.acquire_fence(claim.owner_id, claim.session_id)
        if fence is None:
            return FinalizationOutcome("skipped", skip_reason="active")
        try:
            async with repository.lock_session(claim.session_id) as uow:
                context = await _validate_in_uow(repository, uow.conn, claim, for_idle=idle)
                required_state = "idle_finalizing" if idle else "terminal"
                if context.state != required_state:
                    raise SessionClaimConflict
                threads = await _threads_in_uow(repository, uow.conn, claim.context_id)
                cleanup = await session_state.clear_context(claim.context_id, threads)
                if idle:
                    await uow.complete_idle_delete(claim)
                else:
                    await _complete_terminal_in_uow(repository, uow.conn, claim)
            return FinalizationOutcome("completed", cleanup)
        except SessionClaimConflict:
            return await _classify_conflict(repository, claim)
        except Exception:
            # Phase A is already committed before this function starts.  Keeping the
            # finalization pending and the context gated is the crash-recovery journal.
            session_fp = message_fingerprint(claim.session_id)[1]
            logger.warning(
                "session transient Phase B 실패 sessionFp=%s finalization_id=%s",
                session_fp,
                claim.finalization_id,
                exc_info=True,
            )
            return FinalizationOutcome("retryable")
        finally:
            await registry.release_fence(fence)

    async def run_session_context_sweep(
        self,
        *,
        idle_timeout_s: float | None = None,
        lease_s: float | None = None,
        batch_size: int | None = None,
        max_examined: int | None = None,
    ) -> IdleSweepResult:
        """Recover committed Phase B work first, then claim only remaining idle capacity."""
        repository = self._repository or session_context._default_repository
        settings = get_settings()
        timeout = idle_timeout_s or settings.profile_session_idle_timeout_s
        lease = lease_s or settings.profile_idle_claim_ttl_s
        capacity = batch_size or settings.profile_idle_sweep_batch_size
        examined_limit = max_examined if max_examined is not None else capacity * 4
        if examined_limit <= 0:
            raise ValueError("max_examined must be positive")
        semaphore = asyncio.Semaphore(settings.profile_idle_max_concurrency)

        async def process_claim(
            claim: FinalizationClaim,
        ) -> FinalizationOutcome:
            async with semaphore:
                try:
                    return await self.process_transient_claim(claim)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "session lifecycle claim 처리 실패 finalization_id=%s",
                        claim.finalization_id,
                        exc_info=True,
                    )
                    return FinalizationOutcome("retryable")

        outcomes: list[tuple[FinalizationClaim, FinalizationOutcome]] = []
        recovered_ids: set[str] = set()
        attempted_recovery_ids: set[str] = set()
        productive = 0
        examined = 0
        while productive < capacity and examined < examined_limit:
            recovery = await repository.claim_recoverable_finalizations(
                lease,
                min(capacity - productive, examined_limit - examined),
            )
            recovery = [
                claim for claim in recovery if claim.finalization_id not in attempted_recovery_ids
            ]
            if not recovery:
                break
            attempted_recovery_ids.update(claim.finalization_id for claim in recovery)
            recovery = recovery[: min(capacity - productive, examined_limit - examined)]
            examined += len(recovery)
            wave = await asyncio.gather(*(process_claim(claim) for claim in recovery))
            for claim, outcome in zip(recovery, wave, strict=True):
                outcomes.append((claim, outcome))
                if outcome.skip_reason not in ("superseded", "invalid"):
                    recovered_ids.add(claim.finalization_id)
                    productive += 1

        attempted_fresh_ids: set[str] = set()
        while productive < capacity and examined < examined_limit:
            fresh = await repository.claim_expired_contexts(
                timeout,
                lease,
                min(capacity - productive, examined_limit - examined),
            )
            fresh = [claim for claim in fresh if claim.finalization_id not in attempted_fresh_ids]
            if not fresh:
                break
            attempted_fresh_ids.update(claim.finalization_id for claim in fresh)
            fresh = fresh[: min(capacity - productive, examined_limit - examined)]
            examined += len(fresh)
            wave = await asyncio.gather(*(process_claim(claim) for claim in fresh))
            for claim, outcome in zip(fresh, wave, strict=True):
                outcomes.append((claim, outcome))
                if outcome.skip_reason not in ("superseded", "invalid"):
                    productive += 1

        completed = sum(outcome.status == "completed" for _, outcome in outcomes)
        retryable = sum(outcome.status == "retryable" for _, outcome in outcomes)
        skipped = sum(outcome.status == "skipped" for _, outcome in outcomes)
        superseded = sum(outcome.skip_reason == "superseded" for _, outcome in outcomes)
        invalid_recovery = sum(
            claim.finalization_id in attempted_recovery_ids and outcome.skip_reason == "invalid"
            for claim, outcome in outcomes
        )
        await _process_recoverable_profile_phases(repository, capacity)
        return IdleSweepResult(
            claimed=productive,
            recovered=len(recovered_ids),
            completed=completed,
            retryable=retryable,
            skipped=skipped,
            superseded_skipped=superseded,
            invalid_recovery=invalid_recovery,
            examined=examined,
            examined_limit_reached=examined == examined_limit and productive < capacity,
        )


async def _read_claim_snapshot(
    repository: SessionContextRepository,
    conn,  # noqa: ANN001
    session_id: str,
) -> _ClaimSnapshot:
    if conn is None:
        row = repository._contexts.get(session_id)
        context = session_context._memory_context(row) if row is not None else None
        return _ClaimSnapshot(
            context,
            repository._owner_claims.get(session_id),
            row.authority_source if row is not None else None,
        )

    history = await (
        await conn.execute(
            "SELECT from_owner_id, to_owner_id FROM chat_session_owner_claims WHERE session_id=%s",
            (session_id,),
        )
    ).fetchone()
    row = await (
        await conn.execute(
            "SELECT context_id, session_id, owner_type, owner_id, generation, state, "
            "authority_source "
            "FROM chat_session_contexts WHERE session_id=%s FOR UPDATE",
            (session_id,),
        )
    ).fetchone()
    if row is None:
        return _ClaimSnapshot(None, history)
    context = session_context._row_to_context(row)
    return _ClaimSnapshot(context, history, row[6])


async def _transition_claim(
    repository: SessionContextRepository,
    conn,  # noqa: ANN001
    event: SessionClaimEvent,
) -> ClaimOutcome:
    if conn is None:
        return _claim_memory_under_lock(repository, event)
    return await repository._claim_owner_on_connection(
        conn,
        event.session_id,
        event.guest_id,
        event.user_id,
    )


def _claim_memory_under_lock(
    repository: SessionContextRepository,
    event: SessionClaimEvent,
) -> ClaimOutcome:
    target = str(event.user_id)
    row = repository._contexts.get(event.session_id)
    if row is not None and row.authority_source == "legacy_backfill":
        if row.owner_type != "member" or row.owner_id != target:
            repository._migration_conflicts[(event.session_id, row.owner_id)] = (
                "quarantined",
                None,
            )
            row = session_context._MemoryContext(
                context_id=str(uuid.uuid4()),
                session_id=event.session_id,
                owner_type="member",
                owner_id=target,
                generation=0,
                state="active",
                last_activity_at=repository._clock(),
            )
            repository._contexts[event.session_id] = row
        else:
            row.authority_source = "runtime"
            row.state = "active"
    elif row is None:
        row = session_context._MemoryContext(
            context_id=str(uuid.uuid4()),
            session_id=event.session_id,
            owner_type="member",
            owner_id=target,
            generation=0,
            state="active",
            last_activity_at=repository._clock(),
        )
        repository._contexts[event.session_id] = row
    else:
        # SQL 경로(_claim_owner_on_connection)·in-memory claim_owner 와 같은 순서 —
        # generation 을 올리기 전에 현재 세대의 진행 중 idle finalization 을 먼저 닫는다.
        # pool 미구성 환경에서 idle sweep 과 로그인 claim 이 겹치면 finalization 이
        # 이전 세대에 고아로 남아 회수되지 않는다.
        for stale in repository._finalizations.values():
            if (
                stale.context_id == row.context_id
                and stale.generation == row.generation
                and stale.reason == "idle"
                and stale.status != "superseded"
                and stale.transient_status == "pending"
            ):
                stale.status = "superseded"
                stale.claim_token = None
                stale.lease_expires_at = None
                stale.superseded_at = repository._clock()
        row.owner_type = "member"
        row.owner_id = target
        row.generation += 1
        row.state = "active"
        row.authority_source = "runtime"
    repository._owner_claims[event.session_id] = (event.guest_id, target)
    conflict_key = (event.session_id, target)
    conflict = repository._migration_conflicts.get(conflict_key)
    if conflict is not None and conflict[0] == "quarantined":
        repository._migration_conflicts[conflict_key] = ("resolved", row.context_id)
    return ClaimOutcome(session_context._memory_context(row), True)


async def _validate_in_uow(
    repository: SessionContextRepository,
    conn,  # noqa: ANN001
    claim: FinalizationClaim,
    *,
    for_idle: bool,
) -> SessionContext:
    if conn is None:
        return repository._validate_memory_claim(claim, for_idle=for_idle)
    return await repository._validate_claim_on_connection(
        conn,
        claim,
        for_idle=for_idle,
    )


async def _threads_in_uow(
    repository: SessionContextRepository,
    conn,  # noqa: ANN001
    context_id: str,
) -> list[str]:
    if conn is None:
        return sorted(repository._context_by_id(context_id).threads)
    rows = await (
        await conn.execute(
            "SELECT thread_id FROM chat_session_threads WHERE context_id=%s ORDER BY thread_id",
            (context_id,),
        )
    ).fetchall()
    return [str(row[0]) for row in rows]


async def _watermark_prepared_in_uow(
    repository: SessionContextRepository,
    conn,  # noqa: ANN001
    claim: FinalizationClaim,
) -> bool:
    if conn is None:
        finalization = repository._finalizations[claim.finalization_id]
        return finalization.watermark_status in ("captured", "skipped")
    row = await (
        await conn.execute(
            "SELECT watermark_status FROM chat_session_finalizations WHERE finalization_id=%s",
            (claim.finalization_id,),
        )
    ).fetchone()
    return row is not None and row[0] in ("captured", "skipped")


async def _complete_terminal_in_uow(
    repository: SessionContextRepository,
    conn,  # noqa: ANN001
    claim: FinalizationClaim,
) -> None:
    await _validate_in_uow(repository, conn, claim, for_idle=False)
    if conn is None:
        finalization = repository._finalizations[claim.finalization_id]
        finalization.transient_status = "completed"
        finalization.status = "completed"
        finalization.claim_token = None
        finalization.lease_expires_at = None
        repository._context_by_id(claim.context_id).threads.clear()
        return
    # Completion evidence is written before runtime thread metadata is removed.
    await conn.execute(
        """
        UPDATE chat_session_finalizations
        SET transient_status='completed', status='completed',
            claim_token=NULL, lease_expires_at=NULL, updated_at=now()
        WHERE finalization_id=%s
        """,
        (claim.finalization_id,),
    )
    await conn.execute(
        "DELETE FROM chat_session_threads WHERE context_id=%s",
        (claim.context_id,),
    )


async def _classify_conflict(
    repository: SessionContextRepository,
    claim: FinalizationClaim,
) -> FinalizationOutcome:
    try:
        finalization = await repository.get_finalization(claim.finalization_id)
    except SessionClaimConflict:
        return FinalizationOutcome("skipped", skip_reason="invalid")
    if finalization.status == "superseded":
        return FinalizationOutcome("skipped", skip_reason="superseded")
    return FinalizationOutcome("skipped", skip_reason="invalid")


def _log_claim(
    event: SessionClaimEvent,
    context: SessionContext | None,
    outcome: str,
) -> None:
    session_fp = message_fingerprint(event.session_id)[1]
    owner_fp = message_fingerprint(event.guest_id)[1]
    context_fp = message_fingerprint(context.context_id)[1] if context is not None else None
    logger.info(
        "session owner claim sessionFp=%s ownerFp=%s contextFp=%s generation=%s outcome=%s",
        session_fp,
        owner_fp,
        context_fp,
        context.generation if context is not None else None,
        outcome,
    )


async def claim_owner(event: SessionClaimEvent) -> ClaimOutcome:
    """Claim through the default lifecycle repository."""
    return await SessionLifecycleCoordinator().claim_owner(event)


async def process_transient_claim(claim: FinalizationClaim) -> FinalizationOutcome:
    """Process an idle or terminal transient claim through the default coordinator."""
    return await SessionLifecycleCoordinator().process_transient_claim(claim)


async def process_idle_transient(claim: FinalizationClaim) -> FinalizationOutcome:
    """Process idle transient state without running profile consolidation."""
    return await SessionLifecycleCoordinator().process_idle_transient(claim)


async def process_terminal_transient(claim: FinalizationClaim) -> FinalizationOutcome:
    """Process terminal transient state while preserving the terminal context gate."""
    return await SessionLifecycleCoordinator().process_terminal_transient(claim)


async def begin_terminal(user_id: int, session_id: str) -> TerminalOutcome:
    """Run terminal lifecycle through the default coordinator."""
    return await SessionLifecycleCoordinator().begin_terminal(user_id, session_id)


async def run_session_context_sweep() -> IdleSweepResult:
    """Run the configured recovery-first session lifecycle sweep."""
    return await SessionLifecycleCoordinator().run_session_context_sweep()
