"""Cross-component coordination for session lifecycle transitions."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from app.core import session_context
from app.core.observability import message_fingerprint
from app.core.session_context import (
    ClaimOutcome,
    SessionActive,
    SessionClaimConflict,
    SessionContext,
    SessionContextRepository,
    SessionFinalizing,
)
from app.core.stream import ActiveStreamRegistry, StreamScopeFence, get_registry
from app.schemas.events import SessionClaimEvent

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._repository = repository
        self._registry = registry

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
