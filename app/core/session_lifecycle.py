"""Cross-component coordination for session lifecycle transitions."""

from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, Protocol

from app.agents.buyer import session_state
from app.agents.buyer.session_state import CleanupCounts
from app.agents.profile.store import ProfileStore, get_profile_store
from app.core.config import get_settings
from app.core.conversation import conversation_key
from app.core import session_context
from app.core.observability import message_fingerprint
from app.core.session_context import (
    ClaimOutcome,
    FinalizationClaim,
    SessionActive,
    SessionClaimConflict,
    SessionContext,
    SessionContextRepository,
    SessionFinalizing,
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


@dataclass(frozen=True)
class _ClaimSnapshot:
    context: SessionContext | None
    history: tuple[str, str] | None


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

                if (
                    snapshot.history == (event.guest_id, target)
                    and context is not None
                    and context.owner_type == "member"
                    and context.owner_id == target
                ):
                    outcome = ClaimOutcome(context, False)
                    outcome_name = "duplicate"
                else:
                    if context is not None and context.state == "idle_finalizing":
                        outcome_name = "finalizing"
                        raise SessionFinalizing
                    if (
                        snapshot.history is not None
                        or (context is not None and context.state == "terminal")
                        or (
                            context is not None
                            and (
                                context.owner_type != "guest" or context.owner_id != event.guest_id
                            )
                        )
                    ):
                        outcome_name = "claim_conflict"
                        raise SessionClaimConflict

                    # acquire_fence() has no await: active scope check and fence install
                    # are one event-loop atomic operation, independent of DB thread rows.
                    fence = registry.acquire_fence(event.guest_id, event.session_id)
                    if fence is None:
                        raise SessionActive
                    outcome = await _transition_claim(repository, uow.conn, event)
                    snapshot = _ClaimSnapshot(outcome.context, snapshot.history)
                    outcome_name = "accepted"
            return outcome
        except SessionActive:
            outcome_name = "active"
            raise
        finally:
            if fence is not None:
                registry.release_fence(fence)
            _log_claim(event, snapshot.context, outcome_name)

    async def process_transient_claim(
        self,
        claim: FinalizationClaim,
    ) -> FinalizationOutcome:
        """Dispatch a claim by its durable reason; idle and terminal never share transitions."""
        if claim.reason == "idle":
            return await self.process_idle_transient(claim)
        return await self.process_terminal_transient(claim)

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
        fence = registry.acquire_fence(claim.owner_id, claim.session_id)
        if fence is None:
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
                if idle and not already_prepared:
                    context = await uow.prepare_idle_finalizing(claim)
                if not idle and context.state != "terminal":
                    raise SessionClaimConflict
                if not already_prepared:
                    await self._capture_watermark(uow, claim, context)
            return None
        except SessionClaimConflict:
            return await _classify_conflict(repository, claim)
        except Exception:
            logger.warning(
                "session transient Phase A 실패 session_id=%s finalization_id=%s",
                claim.session_id,
                claim.finalization_id,
                exc_info=True,
            )
            return FinalizationOutcome("retryable")
        finally:
            registry.release_fence(fence)

    async def _capture_watermark(
        self, uow, claim: FinalizationClaim, context: SessionContext
    ) -> None:  # noqa: ANN001
        if context.owner_type == "guest":
            return
        factory = self._profile_store_factory or get_profile_store
        profile_store = factory()
        if inspect.isawaitable(profile_store):
            profile_store = await profile_store
        _, watermark = await profile_store.get_session_ctx_snapshot(
            conversation_key(context.owner_id, context.session_id)
        )
        await uow.capture_profile_watermark(claim, watermark)

    async def _delete_transient(
        self,
        claim: FinalizationClaim,
        *,
        idle: bool,
    ) -> FinalizationOutcome:
        """Phase B: re-lock, revalidate, delete idempotently, then record completion."""
        repository = self._repository or session_context._default_repository
        registry = self._registry or get_registry()
        fence = registry.acquire_fence(claim.owner_id, claim.session_id)
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
            logger.warning(
                "session transient Phase B 실패 session_id=%s finalization_id=%s",
                claim.session_id,
                claim.finalization_id,
                exc_info=True,
            )
            return FinalizationOutcome("retryable")
        finally:
            registry.release_fence(fence)

    async def run_session_context_sweep(
        self,
        *,
        idle_timeout_s: float | None = None,
        lease_s: float | None = None,
        batch_size: int | None = None,
    ) -> IdleSweepResult:
        """Recover committed Phase B work first, then claim only remaining idle capacity."""
        repository = self._repository or session_context._default_repository
        settings = get_settings()
        timeout = idle_timeout_s or settings.profile_session_idle_timeout_s
        lease = lease_s or settings.profile_idle_claim_ttl_s
        capacity = batch_size or settings.profile_idle_sweep_batch_size

        outcomes: list[tuple[FinalizationClaim, FinalizationOutcome]] = []
        recovered_ids: set[str] = set()
        attempted_recovery_ids: set[str] = set()
        productive = 0
        while productive < capacity:
            recovery = await repository.claim_recoverable_finalizations(
                lease,
                capacity - productive,
            )
            recovery = [
                claim for claim in recovery if claim.finalization_id not in attempted_recovery_ids
            ]
            if not recovery:
                break
            attempted_recovery_ids.update(claim.finalization_id for claim in recovery)
            for claim in recovery:
                outcome = await self.process_transient_claim(claim)
                outcomes.append((claim, outcome))
                if outcome.skip_reason not in ("superseded", "invalid"):
                    recovered_ids.add(claim.finalization_id)
                    productive += 1
                    if productive == capacity:
                        break

        attempted_fresh_ids: set[str] = set()
        while productive < capacity:
            fresh = await repository.claim_expired_contexts(
                timeout,
                lease,
                capacity - productive,
            )
            fresh = [claim for claim in fresh if claim.finalization_id not in attempted_fresh_ids]
            if not fresh:
                break
            attempted_fresh_ids.update(claim.finalization_id for claim in fresh)
            for claim in fresh:
                outcome = await self.process_transient_claim(claim)
                outcomes.append((claim, outcome))
                if outcome.skip_reason not in ("superseded", "invalid"):
                    productive += 1
                    if productive == capacity:
                        break

        completed = sum(outcome.status == "completed" for _, outcome in outcomes)
        retryable = sum(outcome.status == "retryable" for _, outcome in outcomes)
        skipped = sum(outcome.status == "skipped" for _, outcome in outcomes)
        superseded = sum(outcome.skip_reason == "superseded" for _, outcome in outcomes)
        invalid_recovery = sum(
            claim.finalization_id in attempted_recovery_ids and outcome.skip_reason == "invalid"
            for claim, outcome in outcomes
        )
        return IdleSweepResult(
            claimed=productive,
            recovered=len(recovered_ids),
            completed=completed,
            retryable=retryable,
            skipped=skipped,
            superseded_skipped=superseded,
            invalid_recovery=invalid_recovery,
        )


async def _read_claim_snapshot(
    repository: SessionContextRepository,
    conn,  # noqa: ANN001
    session_id: str,
) -> _ClaimSnapshot:
    if conn is None:
        row = repository._contexts.get(session_id)
        context = session_context._memory_context(row) if row is not None else None
        return _ClaimSnapshot(context, repository._owner_claims.get(session_id))

    history = await (
        await conn.execute(
            "SELECT from_owner_id, to_owner_id FROM chat_session_owner_claims WHERE session_id=%s",
            (session_id,),
        )
    ).fetchone()
    row = await (
        await conn.execute(
            "SELECT context_id, session_id, owner_type, owner_id, generation, state "
            "FROM chat_session_contexts WHERE session_id=%s FOR UPDATE",
            (session_id,),
        )
    ).fetchone()
    if row is None:
        return _ClaimSnapshot(None, history)
    context = session_context._row_to_context(row)
    return _ClaimSnapshot(context, history)


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
    if row is None:
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
        row.owner_type = "member"
        row.owner_id = target
        row.generation += 1
        row.state = "active"
    repository._owner_claims[event.session_id] = (event.guest_id, target)
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
    guest_fp = message_fingerprint(event.guest_id)[1]
    logger.info(
        "session owner claim session_fp=%s guest_fp=%s context_id=%s generation=%s outcome=%s",
        session_fp,
        guest_fp,
        context.context_id if context is not None else None,
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


async def run_session_context_sweep() -> IdleSweepResult:
    """Run the configured recovery-first session lifecycle sweep."""
    return await SessionLifecycleCoordinator().run_session_context_sweep()
