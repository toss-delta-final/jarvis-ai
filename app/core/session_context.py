"""Session-scoped buyer lifecycle authority.

PostgreSQL is authoritative outside development.  The in-memory implementation mirrors
the transition predicates and uses a monotonic clock plus one lock per session so local
development does not acquire weaker lifecycle semantics.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncContextManager, Literal

from app.core.config import get_settings

logger = logging.getLogger(__name__)

OwnerType = Literal["guest", "member"]
ContextState = Literal["active", "idle_finalizing", "idle_expired", "terminal"]
FinalizationReason = Literal["idle", "terminal"]
ProfilePhaseStatus = Literal["pending", "processing", "completed", "skipped", "retryable"]


class SessionContextError(Exception):
    """Base class for expected lifecycle rejections."""


class SessionForbidden(SessionContextError):
    pass


class SessionFinalizing(SessionContextError):
    pass


class SessionActive(SessionContextError):
    pass


class SessionClaimConflict(SessionContextError):
    pass


class SessionStateUnavailable(SessionContextError):
    pass


@dataclass(frozen=True)
class BuyerSessionInput:
    session_id: str
    thread_id: str
    owner_type: OwnerType
    owner_id: str


@dataclass(frozen=True)
class SessionContext:
    context_id: str
    session_id: str
    owner_type: OwnerType
    owner_id: str
    generation: int
    state: ContextState


@dataclass(frozen=True)
class FinalizationClaim:
    finalization_id: str
    context_id: str
    session_id: str
    owner_type: OwnerType
    owner_id: str
    generation: int
    reason: FinalizationReason
    claim_token: str
    lease_expires_at: datetime | float


@dataclass(frozen=True)
class SessionFinalization:
    finalization_id: str
    context_id: str
    generation: int
    reason: FinalizationReason
    status: Literal["pending", "processing", "completed", "superseded"]
    claim_token: str | None
    lease_expires_at: datetime | float | None
    watermark_status: Literal["pending", "captured", "skipped"]
    profile_watermark: int | None
    transient_status: Literal["pending", "completed"]
    profile_status: ProfilePhaseStatus


@dataclass(frozen=True)
class ProfileRecoveryCandidate:
    finalization_id: str
    context_id: str
    session_id: str
    owner_id: str
    generation: int
    profile_watermark: int


@dataclass(frozen=True)
class ClaimOutcome:
    context: SessionContext
    claimed: bool


@dataclass(frozen=True)
class TerminalOutcome:
    context: SessionContext
    finalization: SessionFinalization
    claim: FinalizationClaim | None
    duplicate: bool


@dataclass
class _MemoryContext:
    context_id: str
    session_id: str
    owner_type: OwnerType
    owner_id: str
    generation: int
    state: ContextState
    last_activity_at: float
    threads: set[str] = field(default_factory=set)


@dataclass
class _MemoryFinalization:
    finalization_id: str
    context_id: str
    generation: int
    reason: FinalizationReason
    status: Literal["pending", "processing", "completed", "superseded"] = "pending"
    claim_token: str | None = None
    lease_expires_at: float | None = None
    watermark_status: Literal["pending", "captured", "skipped"] = "pending"
    profile_watermark: int | None = None
    transient_status: Literal["pending", "completed"] = "pending"
    profile_status: ProfilePhaseStatus = "pending"


class SessionContextRepository:
    """Lifecycle repository backed by a psycopg async pool or an in-memory fallback."""

    def __init__(self, pool=None, *, clock=time.monotonic) -> None:  # noqa: ANN001
        self._pool = pool
        self._clock = clock
        self._contexts: dict[str, _MemoryContext] = {}
        self._finalizations: dict[str, _MemoryFinalization] = {}
        self._owner_claims: dict[str, tuple[str, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(session_id, asyncio.Lock())

    async def initialize(self) -> None:
        if self._pool is None:
            return
        sql = (
            Path(__file__).parents[2] / "db" / "profile" / "init" / "03_chat_session_contexts.sql"
        ).read_text()
        settings = get_settings()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(settings.state_store_migration_timeout_s * 1000))),),
                )
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("schema:chat_session_contexts",),
                )
                await conn.execute(sql)

    async def touch(self, input: BuyerSessionInput) -> SessionContext:
        if self._pool is not None:
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    return await self.resolve_touch_register_on_connection(conn, input)
        lock = await self._session_lock(input.session_id)
        async with lock:
            now = self._clock()
            row = self._contexts.get(input.session_id)
            if row is None:
                row = _MemoryContext(
                    context_id=str(uuid.uuid4()),
                    session_id=input.session_id,
                    owner_type=input.owner_type,
                    owner_id=input.owner_id,
                    generation=0,
                    state="active",
                    last_activity_at=now,
                )
                self._contexts[input.session_id] = row
            else:
                self._assert_owner(row, input.owner_type, input.owner_id)
                if row.state == "idle_finalizing":
                    raise SessionFinalizing
                if row.state == "terminal":
                    raise SessionForbidden
                invalidated = self._pending_idle(row)
                if row.state == "idle_expired" or invalidated is not None:
                    row.generation += 1
                if invalidated is not None:
                    invalidated.status = "superseded"
                    invalidated.claim_token = None
                    invalidated.lease_expires_at = None
                row.state = "active"
                row.last_activity_at = now
            row.threads.add(input.thread_id)
            return _memory_context(row)

    async def resolve_touch_register_on_connection(
        self,
        conn,
        input: BuyerSessionInput,  # noqa: ANN001
    ) -> SessionContext:
        await _advisory_lock(conn, input.session_id)
        row = await (
            await conn.execute(
                "SELECT context_id, session_id, owner_type, owner_id, generation, state "
                "FROM chat_session_contexts WHERE session_id=%s FOR UPDATE",
                (input.session_id,),
            )
        ).fetchone()
        if row is None:
            context_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO chat_session_contexts
                    (context_id, session_id, owner_type, owner_id, state)
                VALUES (%s, %s, %s, %s, 'active')
                ON CONFLICT (session_id) DO NOTHING
                """,
                (context_id, input.session_id, input.owner_type, input.owner_id),
            )
            row = await (
                await conn.execute(
                    "SELECT context_id, session_id, owner_type, owner_id, generation, state "
                    "FROM chat_session_contexts WHERE session_id=%s FOR UPDATE",
                    (input.session_id,),
                )
            ).fetchone()
        assert row is not None
        context = _row_to_context(row)
        if context.owner_type != input.owner_type or context.owner_id != input.owner_id:
            raise SessionForbidden
        if context.state == "idle_finalizing":
            raise SessionFinalizing
        if context.state == "terminal":
            raise SessionForbidden
        live_idle = await (
            await conn.execute(
                """
                UPDATE chat_session_finalizations
                SET status='superseded', claim_token=NULL, lease_expires_at=NULL,
                    superseded_at=now(), updated_at=now()
                WHERE context_id=%s AND generation=%s AND reason='idle'
                  AND status <> 'superseded'
                  AND transient_status='pending'
                RETURNING finalization_id
                """,
                (context.context_id, context.generation),
            )
        ).fetchone()
        generation = context.generation + int(
            context.state == "idle_expired" or live_idle is not None
        )
        row = await (
            await conn.execute(
                """
                UPDATE chat_session_contexts
                SET generation=%s, state='active', last_activity_at=now(), updated_at=now()
                WHERE context_id=%s
                RETURNING context_id, session_id, owner_type, owner_id, generation, state
                """,
                (generation, context.context_id),
            )
        ).fetchone()
        await conn.execute(
            """
            INSERT INTO chat_session_threads (context_id, thread_id, last_seen_at)
            VALUES (%s, %s, now())
            ON CONFLICT (context_id, thread_id)
            DO UPDATE SET last_seen_at=now()
            """,
            (context.context_id, input.thread_id),
        )
        return _row_to_context(row)

    async def claim_owner(self, session_id: str, guest_id: str, user_id: int) -> ClaimOutcome:
        if self._pool is not None:
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    await _advisory_lock(conn, session_id)
                    return await self._claim_owner_on_connection(
                        conn, session_id, guest_id, user_id
                    )
        lock = await self._session_lock(session_id)
        async with lock:
            target = str(user_id)
            history = self._owner_claims.get(session_id)
            row = self._contexts.get(session_id)
            if history == (guest_id, target) and row is not None:
                return ClaimOutcome(_memory_context(row), False)
            if history is not None or (row is not None and row.state == "terminal"):
                raise SessionClaimConflict
            if row is not None and row.state == "idle_finalizing":
                raise SessionFinalizing
            if row is None:
                row = _MemoryContext(
                    str(uuid.uuid4()), session_id, "member", target, 0, "active", self._clock()
                )
                self._contexts[session_id] = row
            else:
                if row.owner_type != "guest" or row.owner_id != guest_id:
                    raise SessionClaimConflict
                row.owner_type = "member"
                row.owner_id = target
                row.generation += 1
                row.state = "active"
            self._owner_claims[session_id] = (guest_id, target)
            return ClaimOutcome(_memory_context(row), True)

    async def _claim_owner_on_connection(
        self,
        conn,
        session_id: str,
        guest_id: str,
        user_id: int,  # noqa: ANN001
    ) -> ClaimOutcome:
        target = str(user_id)
        history = await (
            await conn.execute(
                "SELECT from_owner_id, to_owner_id FROM chat_session_owner_claims "
                "WHERE session_id=%s",
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
        if history == (guest_id, target) and row is not None:
            return ClaimOutcome(_row_to_context(row), False)
        if history is not None or (row is not None and row[5] == "terminal"):
            raise SessionClaimConflict
        if row is not None and row[5] == "idle_finalizing":
            raise SessionFinalizing
        if row is None:
            context_id = str(uuid.uuid4())
            row = await (
                await conn.execute(
                    """
                    INSERT INTO chat_session_contexts
                        (context_id, session_id, owner_type, owner_id, state)
                    VALUES (%s, %s, 'member', %s, 'active')
                    RETURNING context_id, session_id, owner_type, owner_id, generation, state
                    """,
                    (context_id, session_id, target),
                )
            ).fetchone()
        else:
            if row[2] != "guest" or row[3] != guest_id:
                raise SessionClaimConflict
            row = await (
                await conn.execute(
                    """
                    UPDATE chat_session_contexts
                    SET owner_type='member', owner_id=%s, generation=generation+1,
                        state='active', updated_at=now()
                    WHERE context_id=%s
                    RETURNING context_id, session_id, owner_type, owner_id, generation, state
                    """,
                    (target, row[0]),
                )
            ).fetchone()
        await conn.execute(
            """
            INSERT INTO chat_session_owner_claims
                (claim_id, context_id, session_id, from_owner_type, from_owner_id,
                 to_owner_type, to_owner_id)
            VALUES (%s, %s, %s, 'guest', %s, 'member', %s)
            """,
            (str(uuid.uuid4()), row[0], session_id, guest_id, target),
        )
        return ClaimOutcome(_row_to_context(row), True)

    async def claim_expired_contexts(
        self, idle_timeout_s: float, lease_s: float, batch_size: int
    ) -> list[FinalizationClaim]:
        _positive(idle_timeout_s, lease_s, batch_size)
        if self._pool is not None:
            token = uuid.uuid4().hex
            async with self._pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        """
                        WITH candidates AS (
                            SELECT context_id
                            FROM chat_session_contexts
                            WHERE state='active'
                              AND last_activity_at <= now() - make_interval(secs => %s)
                              AND NOT EXISTS (
                                  SELECT 1 FROM chat_session_finalizations f
                                  WHERE f.context_id=chat_session_contexts.context_id
                                    AND f.generation=chat_session_contexts.generation
                                    AND f.reason='idle' AND f.status <> 'superseded'
                              )
                            ORDER BY last_activity_at, context_id
                            FOR UPDATE SKIP LOCKED
                            LIMIT %s
                        ), inserted AS (
                            INSERT INTO chat_session_finalizations
                                (finalization_id, context_id, generation, reason, status,
                                 claim_token, lease_expires_at)
                            SELECT gen_random_uuid(), c.context_id, c.generation, 'idle',
                                   'processing', %s, now() + make_interval(secs => %s)
                            FROM chat_session_contexts c JOIN candidates USING (context_id)
                            RETURNING *
                        )
                        SELECT i.finalization_id, i.context_id, c.session_id, c.owner_type,
                               c.owner_id, i.generation, i.reason, i.claim_token,
                               i.lease_expires_at
                        FROM inserted i JOIN chat_session_contexts c USING (context_id)
                        """,
                        (idle_timeout_s, batch_size, token, lease_s),
                    )
                ).fetchall()
            return [_row_to_claim(row) for row in rows]
        now = self._clock()
        eligible = sorted(
            (
                row
                for row in self._contexts.values()
                if row.state == "active"
                and row.last_activity_at <= now - idle_timeout_s
                and self._live_idle(row) is None
            ),
            key=lambda row: (row.last_activity_at, row.context_id),
        )
        claims = []
        for row in eligible[:batch_size]:
            finalization = _MemoryFinalization(
                str(uuid.uuid4()),
                row.context_id,
                row.generation,
                "idle",
                status="processing",
                claim_token=uuid.uuid4().hex,
                lease_expires_at=now + lease_s,
            )
            self._finalizations[finalization.finalization_id] = finalization
            claims.append(_memory_claim(row, finalization))
        return claims

    async def claim_recoverable_finalizations(
        self, lease_s: float, batch_size: int
    ) -> list[FinalizationClaim]:
        _positive(lease_s, batch_size)
        if self._pool is not None:
            token = uuid.uuid4().hex
            async with self._pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        """
                        WITH candidates AS (
                            SELECT f.finalization_id
                            FROM chat_session_finalizations f
                            JOIN chat_session_contexts c USING (context_id)
                            WHERE f.transient_status='pending'
                              AND f.status <> 'superseded'
                              AND c.state IN ('idle_finalizing', 'terminal')
                              AND (f.lease_expires_at IS NULL OR f.lease_expires_at <= now())
                            ORDER BY f.lease_expires_at NULLS FIRST, f.finalization_id
                            FOR UPDATE OF f SKIP LOCKED LIMIT %s
                        ), claimed AS (
                            UPDATE chat_session_finalizations f
                            SET status='processing', claim_token=%s,
                                lease_expires_at=now() + make_interval(secs => %s), updated_at=now()
                            FROM candidates x
                            WHERE f.finalization_id=x.finalization_id
                              AND f.status <> 'superseded'
                            RETURNING f.*
                        )
                        SELECT f.finalization_id, f.context_id, c.session_id, c.owner_type,
                               c.owner_id, f.generation, f.reason, f.claim_token,
                               f.lease_expires_at
                        FROM claimed f JOIN chat_session_contexts c USING (context_id)
                        """,
                        (batch_size, token, lease_s),
                    )
                ).fetchall()
            return [_row_to_claim(row) for row in rows]
        now = self._clock()
        recoverable = []
        for finalization in self._finalizations.values():
            context = self._context_by_id(finalization.context_id)
            if (
                finalization.transient_status == "pending"
                and finalization.status != "superseded"
                and context.state in ("idle_finalizing", "terminal")
                and (finalization.lease_expires_at is None or finalization.lease_expires_at <= now)
            ):
                recoverable.append((finalization, context))
        recoverable.sort(key=lambda pair: (pair[0].lease_expires_at or -1, pair[0].finalization_id))
        claims = []
        for finalization, context in recoverable[:batch_size]:
            finalization.status = "processing"
            finalization.claim_token = uuid.uuid4().hex
            finalization.lease_expires_at = now + lease_s
            claims.append(_memory_claim(context, finalization))
        return claims

    async def begin_terminal(self, user_id: int, session_id: str) -> TerminalOutcome:
        if self._pool is not None:
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    await _advisory_lock(conn, session_id)
                    return await self._begin_terminal_on_connection(conn, user_id, session_id)
        lock = await self._session_lock(session_id)
        async with lock:
            row = self._contexts.get(session_id)
            if row is None or row.owner_type != "member" or row.owner_id != str(user_id):
                raise SessionForbidden
            if row.state == "idle_finalizing":
                raise SessionFinalizing
            if row.state == "terminal":
                current = self._current_finalization(row, "terminal")
                if current is None:
                    raise SessionClaimConflict
                now = self._clock()
                if current.transient_status == "completed" or (
                    current.lease_expires_at is not None and current.lease_expires_at > now
                ):
                    return TerminalOutcome(
                        _memory_context(row),
                        _memory_finalization(current),
                        None,
                        True,
                    )
                current.status = "processing"
                current.claim_token = uuid.uuid4().hex
                current.lease_expires_at = now + get_settings().profile_idle_claim_ttl_s
                return TerminalOutcome(
                    _memory_context(row),
                    _memory_finalization(current),
                    _memory_claim(row, current),
                    False,
                )
            for superseded in self._finalizations.values():
                if (
                    superseded.context_id == row.context_id
                    and superseded.reason == "idle"
                    and superseded.status != "superseded"
                ):
                    superseded.status = "superseded"
                    superseded.claim_token = None
                    superseded.lease_expires_at = None
            row.generation += 1
            row.state = "terminal"
            finalization = _MemoryFinalization(
                str(uuid.uuid4()),
                row.context_id,
                row.generation,
                "terminal",
                status="processing",
                claim_token=uuid.uuid4().hex,
                lease_expires_at=self._clock() + get_settings().profile_idle_claim_ttl_s,
            )
            self._finalizations[finalization.finalization_id] = finalization
            return TerminalOutcome(
                _memory_context(row),
                _memory_finalization(finalization),
                _memory_claim(row, finalization),
                False,
            )

    async def _begin_terminal_on_connection(
        self,
        conn,
        user_id: int,
        session_id: str,  # noqa: ANN001
    ) -> TerminalOutcome:
        row = await (
            await conn.execute(
                "SELECT context_id, session_id, owner_type, owner_id, generation, state "
                "FROM chat_session_contexts WHERE session_id=%s FOR UPDATE",
                (session_id,),
            )
        ).fetchone()
        if row is None or row[2] != "member" or row[3] != str(user_id):
            raise SessionForbidden
        context = _row_to_context(row)
        if context.state == "idle_finalizing":
            raise SessionFinalizing
        if context.state == "terminal":
            finalization = await self._get_current_finalization_on_connection(
                conn, context.context_id, context.generation, "terminal"
            )
            if finalization is None:
                raise SessionClaimConflict
            now = await _postgres_now(conn)
            if finalization.transient_status == "completed" or (
                finalization.lease_expires_at is not None and finalization.lease_expires_at > now
            ):
                return TerminalOutcome(context, finalization, None, True)
            token = uuid.uuid4().hex
            lease_s = get_settings().profile_idle_claim_ttl_s
            refreshed = await (
                await conn.execute(
                    """
                    UPDATE chat_session_finalizations
                    SET status='processing', claim_token=%s,
                        lease_expires_at=now() + make_interval(secs => %s), updated_at=now()
                    WHERE finalization_id=%s
                      AND transient_status='pending'
                      AND status <> 'superseded'
                      AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                    RETURNING finalization_id, context_id, generation, reason, status,
                              claim_token, lease_expires_at, watermark_status,
                              profile_watermark, transient_status, profile_status
                    """,
                    (token, lease_s, finalization.finalization_id),
                )
            ).fetchone()
            if refreshed is None:
                return TerminalOutcome(context, finalization, None, True)
            finalization = _row_to_finalization(refreshed)
            return TerminalOutcome(
                context,
                finalization,
                _claim_from_finalization(context, finalization),
                False,
            )
        await conn.execute(
            """
            UPDATE chat_session_finalizations
            SET status='superseded', claim_token=NULL, lease_expires_at=NULL,
                superseded_at=now(), updated_at=now()
            WHERE context_id=%s AND reason='idle' AND status <> 'superseded'
            """,
            (context.context_id,),
        )
        row = await (
            await conn.execute(
                """
                UPDATE chat_session_contexts
                SET generation=generation+1, state='terminal', updated_at=now()
                WHERE context_id=%s
                RETURNING context_id, session_id, owner_type, owner_id, generation, state
                """,
                (context.context_id,),
            )
        ).fetchone()
        context = _row_to_context(row)
        token = uuid.uuid4().hex
        lease_s = get_settings().profile_idle_claim_ttl_s
        finalization_id = str(uuid.uuid4())
        final_row = await (
            await conn.execute(
                """
                INSERT INTO chat_session_finalizations
                    (finalization_id, context_id, generation, reason, status,
                     claim_token, lease_expires_at)
                VALUES (%s, %s, %s, 'terminal', 'processing', %s,
                        now() + make_interval(secs => %s))
                RETURNING finalization_id, context_id, generation, reason, status,
                          claim_token, lease_expires_at, watermark_status,
                          profile_watermark, transient_status, profile_status
                """,
                (finalization_id, context.context_id, context.generation, token, lease_s),
            )
        ).fetchone()
        finalization = _row_to_finalization(final_row)
        return TerminalOutcome(
            context,
            finalization,
            _claim_from_finalization(context, finalization),
            False,
        )

    async def validate_for_delete(self, claim: FinalizationClaim) -> SessionContext:
        if self._pool is not None:
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    await _advisory_lock(conn, claim.session_id)
                    return await self._validate_claim_on_connection(conn, claim, for_idle=False)
        lock = await self._session_lock(claim.session_id)
        async with lock:
            return self._validate_memory_claim(claim, for_idle=False)

    async def mark_idle_finalizing(self, claim: FinalizationClaim) -> None:
        async with self.lock_session(claim.session_id) as uow:
            await uow.prepare_idle_finalizing(claim)

    async def complete_transient_phase(self, claim: FinalizationClaim) -> None:
        if claim.reason == "idle":
            async with self.lock_session(claim.session_id) as uow:
                await uow.complete_idle_delete(claim)
            return
        if self._pool is not None:
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    await _advisory_lock(conn, claim.session_id)
                    await self._validate_claim_on_connection(conn, claim, for_idle=False)
                    await conn.execute(
                        """
                        UPDATE chat_session_finalizations
                        SET transient_status='completed', status='completed',
                            claim_token=NULL, lease_expires_at=NULL, updated_at=now()
                        WHERE finalization_id=%s
                        """,
                        (claim.finalization_id,),
                    )
            return
        lock = await self._session_lock(claim.session_id)
        async with lock:
            self._validate_memory_claim(claim, for_idle=False)
            finalization = self._finalizations[claim.finalization_id]
            finalization.transient_status = "completed"
            finalization.status = "completed"
            finalization.claim_token = None
            finalization.lease_expires_at = None

    async def record_profile_phase(self, finalization_id: str, status: ProfilePhaseStatus) -> None:
        if status not in ("pending", "processing", "completed", "skipped", "retryable"):
            raise ValueError("invalid profile phase status")
        if self._pool is not None:
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        """
                        UPDATE chat_session_finalizations
                        SET profile_status=%s,
                            status=CASE WHEN %s IN ('completed','skipped') THEN 'completed'
                                        ELSE status END,
                            claim_token=CASE WHEN %s IN ('completed','skipped','retryable')
                                             THEN NULL ELSE claim_token END,
                            lease_expires_at=CASE WHEN %s IN ('completed','skipped','retryable')
                                                  THEN NULL ELSE lease_expires_at END,
                            completed_at=CASE WHEN %s IN ('completed','skipped') THEN now()
                                              ELSE completed_at END,
                            updated_at=now()
                        WHERE finalization_id=%s AND status <> 'superseded'
                        RETURNING finalization_id
                        """,
                        (status, status, status, status, status, finalization_id),
                    )
                ).fetchone()
            if row is None:
                raise SessionClaimConflict
            return
        finalization = self._finalizations.get(finalization_id)
        if finalization is None or finalization.status == "superseded":
            raise SessionClaimConflict
        finalization.profile_status = status
        if status in ("completed", "skipped"):
            finalization.status = "completed"
        if status in ("completed", "skipped", "retryable"):
            finalization.claim_token = None
            finalization.lease_expires_at = None

    async def list_recoverable_profile_phases(
        self, batch_size: int
    ) -> list[ProfileRecoveryCandidate]:
        _positive(batch_size)
        if self._pool is not None:
            async with self._pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        """
                        SELECT f.finalization_id, f.context_id, c.session_id, c.owner_id,
                               f.generation, f.profile_watermark
                        FROM chat_session_finalizations f
                        JOIN chat_session_contexts c USING (context_id)
                        WHERE c.owner_type='member'
                          AND f.transient_status='completed'
                          AND f.profile_status IN ('pending','retryable')
                          AND f.status <> 'superseded'
                          AND f.watermark_status='captured'
                          AND f.profile_watermark IS NOT NULL
                        ORDER BY f.updated_at, f.finalization_id LIMIT %s
                        """,
                        (batch_size,),
                    )
                ).fetchall()
            return [_row_to_profile_candidate(row) for row in rows]
        candidates = []
        for finalization in self._finalizations.values():
            context = self._context_by_id(finalization.context_id)
            if (
                context.owner_type == "member"
                and finalization.transient_status == "completed"
                and finalization.profile_status in ("pending", "retryable")
                and finalization.status != "superseded"
                and finalization.watermark_status == "captured"
                and finalization.profile_watermark is not None
            ):
                candidates.append(
                    ProfileRecoveryCandidate(
                        finalization.finalization_id,
                        context.context_id,
                        context.session_id,
                        context.owner_id,
                        finalization.generation,
                        finalization.profile_watermark,
                    )
                )
        return sorted(candidates, key=lambda item: item.finalization_id)[:batch_size]

    async def claim_profile_phase(
        self, finalization_id: str, lease_s: float
    ) -> FinalizationClaim | None:
        _positive(lease_s)
        token = uuid.uuid4().hex
        if self._pool is not None:
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        """
                        UPDATE chat_session_finalizations f
                        SET profile_status='processing', status='processing',
                            claim_token=%s,
                            lease_expires_at=now() + make_interval(secs => %s), updated_at=now()
                        FROM chat_session_contexts c
                        WHERE f.finalization_id=%s AND c.context_id=f.context_id
                          AND c.owner_type='member'
                          AND f.transient_status='completed'
                          AND f.profile_status IN ('pending','retryable')
                          AND f.status <> 'superseded'
                          AND f.watermark_status='captured'
                          AND f.profile_watermark IS NOT NULL
                        RETURNING f.finalization_id, f.context_id, c.session_id, c.owner_type,
                                  c.owner_id, f.generation, f.reason, f.claim_token,
                                  f.lease_expires_at
                        """,
                        (token, lease_s, finalization_id),
                    )
                ).fetchone()
            return _row_to_claim(row) if row else None
        finalization = self._finalizations.get(finalization_id)
        if finalization is None:
            return None
        context = self._context_by_id(finalization.context_id)
        if not (
            context.owner_type == "member"
            and finalization.transient_status == "completed"
            and finalization.profile_status in ("pending", "retryable")
            and finalization.status != "superseded"
            and finalization.watermark_status == "captured"
            and finalization.profile_watermark is not None
        ):
            return None
        finalization.profile_status = "processing"
        finalization.status = "processing"
        finalization.claim_token = token
        finalization.lease_expires_at = self._clock() + lease_s
        return _memory_claim(context, finalization)

    async def get_finalization(self, finalization_id: str) -> SessionFinalization:
        if self._pool is not None:
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        """
                        SELECT finalization_id, context_id, generation, reason, status,
                               claim_token, lease_expires_at, watermark_status,
                               profile_watermark, transient_status, profile_status
                        FROM chat_session_finalizations WHERE finalization_id=%s
                        """,
                        (finalization_id,),
                    )
                ).fetchone()
            if row is None:
                raise SessionClaimConflict
            return _row_to_finalization(row)
        row = self._finalizations.get(finalization_id)
        if row is None:
            raise SessionClaimConflict
        return _memory_finalization(row)

    async def get_context(self, session_id: str) -> SessionContext | None:
        if self._pool is not None:
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT context_id, session_id, owner_type, owner_id, generation, state "
                        "FROM chat_session_contexts WHERE session_id=%s",
                        (session_id,),
                    )
                ).fetchone()
            return _row_to_context(row) if row else None
        row = self._contexts.get(session_id)
        return _memory_context(row) if row else None

    async def get_threads(self, context_id: str) -> list[str]:
        if self._pool is not None:
            async with self._pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT thread_id FROM chat_session_threads WHERE context_id=%s "
                        "ORDER BY thread_id",
                        (context_id,),
                    )
                ).fetchall()
            return [str(row[0]) for row in rows]
        return sorted(self._context_by_id(context_id).threads)

    def lock_session(self, session_id: str) -> AsyncContextManager["SessionContextUnitOfWork"]:
        return SessionContextUnitOfWork(self, session_id)

    def _assert_owner(self, row: _MemoryContext, owner_type: OwnerType, owner_id: str) -> None:
        if row.owner_type != owner_type or row.owner_id != owner_id:
            raise SessionForbidden

    def _context_by_id(self, context_id: str) -> _MemoryContext:
        try:
            return next(row for row in self._contexts.values() if row.context_id == context_id)
        except StopIteration as exc:
            raise SessionClaimConflict from exc

    def _live_idle(self, row: _MemoryContext) -> _MemoryFinalization | None:
        return self._current_finalization(row, "idle")

    def _pending_idle(self, row: _MemoryContext) -> _MemoryFinalization | None:
        finalization = self._current_finalization(row, "idle")
        if finalization is None or finalization.transient_status != "pending":
            return None
        return finalization

    def _current_finalization(
        self, row: _MemoryContext, reason: FinalizationReason
    ) -> _MemoryFinalization | None:
        return next(
            (
                item
                for item in self._finalizations.values()
                if item.context_id == row.context_id
                and item.generation == row.generation
                and item.reason == reason
                and item.status != "superseded"
            ),
            None,
        )

    def _validate_memory_claim(self, claim: FinalizationClaim, *, for_idle: bool) -> SessionContext:
        row = self._contexts.get(claim.session_id)
        finalization = self._finalizations.get(claim.finalization_id)
        now = self._clock()
        valid_states = (
            ("active", "idle_finalizing") if for_idle else ("active", "idle_finalizing", "terminal")
        )
        if (
            row is None
            or finalization is None
            or claim.session_id != row.session_id
            or row.context_id != claim.context_id
            or row.generation != claim.generation
            or finalization.context_id != claim.context_id
            or finalization.generation != claim.generation
            or finalization.reason != claim.reason
            or row.state not in valid_states
            or finalization.status == "superseded"
            or finalization.claim_token != claim.claim_token
            or finalization.lease_expires_at is None
            or finalization.lease_expires_at <= now
        ):
            raise SessionClaimConflict
        return _memory_context(row)

    async def _validate_claim_on_connection(
        self,
        conn,
        claim: FinalizationClaim,
        *,
        for_idle: bool,  # noqa: ANN001
    ) -> SessionContext:
        states = (
            ("active", "idle_finalizing") if for_idle else ("active", "idle_finalizing", "terminal")
        )
        row = await (
            await conn.execute(
                """
                SELECT c.context_id, c.session_id, c.owner_type, c.owner_id,
                       c.generation, c.state
                FROM chat_session_contexts c
                JOIN chat_session_finalizations f USING (context_id)
                WHERE f.finalization_id=%s AND c.context_id=%s AND c.session_id=%s
                  AND c.generation=%s AND f.generation=%s AND f.reason=%s
                  AND c.state = ANY(%s)
                  AND f.status <> 'superseded' AND f.claim_token=%s
                  AND f.lease_expires_at > now()
                FOR UPDATE OF c, f
                """,
                (
                    claim.finalization_id,
                    claim.context_id,
                    claim.session_id,
                    claim.generation,
                    claim.generation,
                    claim.reason,
                    list(states),
                    claim.claim_token,
                ),
            )
        ).fetchone()
        if row is None:
            raise SessionClaimConflict
        return _row_to_context(row)

    async def _get_current_finalization_on_connection(
        self,
        conn,
        context_id: str,
        generation: int,
        reason: FinalizationReason,  # noqa: ANN001
    ) -> SessionFinalization | None:
        row = await (
            await conn.execute(
                """
                SELECT finalization_id, context_id, generation, reason, status,
                       claim_token, lease_expires_at, watermark_status,
                       profile_watermark, transient_status, profile_status
                FROM chat_session_finalizations
                WHERE context_id=%s AND generation=%s AND reason=%s
                  AND status <> 'superseded'
                """,
                (context_id, generation, reason),
            )
        ).fetchone()
        return _row_to_finalization(row) if row else None


class SessionContextUnitOfWork(AbstractAsyncContextManager["SessionContextUnitOfWork"]):
    """One connection, transaction and session advisory lock for a lifecycle phase."""

    def __init__(self, repository: SessionContextRepository, session_id: str) -> None:
        self.repository = repository
        self.session_id = session_id
        self.conn = None
        self._connection_cm = None
        self._transaction_cm = None
        self._memory_lock: asyncio.Lock | None = None

    async def __aenter__(self) -> "SessionContextUnitOfWork":
        if self.repository._pool is None:
            self._memory_lock = await self.repository._session_lock(self.session_id)
            await self._memory_lock.acquire()
            return self
        self._connection_cm = self.repository._pool.connection()
        transaction_entered = False
        try:
            self.conn = await self._connection_cm.__aenter__()
            self._transaction_cm = self.conn.transaction()
            await self._transaction_cm.__aenter__()
            transaction_entered = True
            await _advisory_lock(self.conn, self.session_id)
        except BaseException as exc:
            if self._transaction_cm is not None and transaction_entered:
                try:
                    await self._transaction_cm.__aexit__(type(exc), exc, exc.__traceback__)
                except BaseException:
                    logger.exception("session lifecycle transaction cleanup failed")
            if self._connection_cm is not None and self.conn is not None:
                try:
                    await self._connection_cm.__aexit__(type(exc), exc, exc.__traceback__)
                except BaseException:
                    logger.exception("session lifecycle connection cleanup failed")
            self.conn = None
            self._transaction_cm = None
            self._connection_cm = None
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self._memory_lock is not None:
            self._memory_lock.release()
            return
        assert self._transaction_cm is not None and self._connection_cm is not None
        try:
            await self._transaction_cm.__aexit__(exc_type, exc, tb)
        finally:
            await self._connection_cm.__aexit__(exc_type, exc, tb)

    async def prepare_idle_finalizing(self, claim: FinalizationClaim) -> SessionContext:
        self._bind_claim(claim)
        if self.conn is None:
            context = self.repository._validate_memory_claim(claim, for_idle=True)
            row = self.repository._contexts[self.session_id]
            row.state = "idle_finalizing"
            finalization = self.repository._finalizations[claim.finalization_id]
            if row.owner_type == "guest":
                finalization.watermark_status = "skipped"
                finalization.profile_status = "skipped"
            return _memory_context(row)
        context = await self.repository._validate_claim_on_connection(
            self.conn, claim, for_idle=True
        )
        row = await (
            await self.conn.execute(
                """
                UPDATE chat_session_contexts SET state='idle_finalizing', updated_at=now()
                WHERE context_id=%s
                RETURNING context_id, session_id, owner_type, owner_id, generation, state
                """,
                (claim.context_id,),
            )
        ).fetchone()
        if context.owner_type == "guest":
            await self.conn.execute(
                """
                UPDATE chat_session_finalizations
                SET watermark_status='skipped', profile_status='skipped', updated_at=now()
                WHERE finalization_id=%s
                """,
                (claim.finalization_id,),
            )
        return _row_to_context(row)

    async def prepare_idle_finalizing_with_watermark(
        self,
        claim: FinalizationClaim,
        watermark: int | None,
    ) -> SessionContext:
        """Atomically commit the idle gate and its real member snapshot watermark."""
        self._bind_claim(claim)
        if watermark is not None and watermark < 0:
            raise ValueError("profile watermark must be non-negative")
        if self.conn is None:
            context = self.repository._validate_memory_claim(claim, for_idle=True)
            if context.state != "active":
                raise SessionClaimConflict
            if (context.owner_type == "member") != (watermark is not None):
                raise SessionClaimConflict
            row = self.repository._contexts[self.session_id]
            finalization = self.repository._finalizations[claim.finalization_id]
            if finalization.watermark_status != "pending":
                raise SessionClaimConflict
            if context.owner_type == "member":
                finalization.watermark_status = "captured"
                finalization.profile_watermark = watermark
            else:
                finalization.watermark_status = "skipped"
                finalization.profile_status = "skipped"
            row.state = "idle_finalizing"
            return _memory_context(row)

        context = await self.repository._validate_claim_on_connection(
            self.conn,
            claim,
            for_idle=True,
        )
        if context.state != "active":
            raise SessionClaimConflict
        if (context.owner_type == "member") != (watermark is not None):
            raise SessionClaimConflict
        if context.owner_type == "member":
            finalization = await (
                await self.conn.execute(
                    """
                    UPDATE chat_session_finalizations
                    SET watermark_status='captured', profile_watermark=%s, updated_at=now()
                    WHERE finalization_id=%s AND context_id=%s AND generation=%s
                      AND reason='idle' AND watermark_status='pending'
                      AND transient_status='pending' AND claim_token=%s
                    RETURNING finalization_id
                    """,
                    (
                        watermark,
                        claim.finalization_id,
                        claim.context_id,
                        claim.generation,
                        claim.claim_token,
                    ),
                )
            ).fetchone()
        else:
            finalization = await (
                await self.conn.execute(
                    """
                    UPDATE chat_session_finalizations
                    SET watermark_status='skipped', profile_status='skipped', updated_at=now()
                    WHERE finalization_id=%s AND context_id=%s AND generation=%s
                      AND reason='idle' AND watermark_status='pending'
                      AND transient_status='pending' AND claim_token=%s
                    RETURNING finalization_id
                    """,
                    (
                        claim.finalization_id,
                        claim.context_id,
                        claim.generation,
                        claim.claim_token,
                    ),
                )
            ).fetchone()
        if finalization is None:
            raise SessionClaimConflict
        row = await (
            await self.conn.execute(
                """
                UPDATE chat_session_contexts SET state='idle_finalizing', updated_at=now()
                WHERE context_id=%s AND generation=%s AND state='active'
                RETURNING context_id, session_id, owner_type, owner_id, generation, state
                """,
                (claim.context_id, claim.generation),
            )
        ).fetchone()
        if row is None:
            raise SessionClaimConflict
        return _row_to_context(row)

    async def abandon_idle_prephase(self, claim: FinalizationClaim) -> bool:
        """Delete an exact idle journal only while no irreversible phase has begun."""
        self._bind_claim(claim)
        if claim.reason != "idle":
            return False
        if self.conn is None:
            row = self.repository._contexts.get(self.session_id)
            finalization = self.repository._finalizations.get(claim.finalization_id)
            if (
                row is None
                or finalization is None
                or row.context_id != claim.context_id
                or row.generation != claim.generation
                or row.state != "active"
                or finalization.context_id != claim.context_id
                or finalization.generation != claim.generation
                or finalization.reason != "idle"
                or finalization.status != "processing"
                or finalization.claim_token != claim.claim_token
                or finalization.watermark_status != "pending"
                or finalization.profile_watermark is not None
                or finalization.transient_status != "pending"
            ):
                return False
            del self.repository._finalizations[claim.finalization_id]
            return True
        row = await (
            await self.conn.execute(
                """
                DELETE FROM chat_session_finalizations f
                USING chat_session_contexts c
                WHERE f.finalization_id=%s AND f.context_id=%s
                  AND f.generation=%s AND f.reason='idle'
                  AND f.status='processing' AND f.claim_token=%s
                  AND f.watermark_status='pending' AND f.profile_watermark IS NULL
                  AND f.transient_status='pending'
                  AND c.context_id=f.context_id AND c.session_id=%s
                  AND c.generation=f.generation AND c.state='active'
                RETURNING f.finalization_id
                """,
                (
                    claim.finalization_id,
                    claim.context_id,
                    claim.generation,
                    claim.claim_token,
                    claim.session_id,
                ),
            )
        ).fetchone()
        return row is not None

    async def capture_profile_watermark(self, claim: FinalizationClaim, watermark: int) -> None:
        self._bind_claim(claim)
        if watermark < 0:
            raise ValueError("profile watermark must be non-negative")
        if self.conn is None:
            context = self.repository._validate_memory_claim(claim, for_idle=False)
            if context.owner_type != "member":
                raise SessionClaimConflict
            finalization = self.repository._finalizations[claim.finalization_id]
            if finalization.watermark_status == "captured":
                if finalization.profile_watermark != watermark:
                    raise SessionClaimConflict
                return
            if finalization.watermark_status != "pending":
                raise SessionClaimConflict
            finalization.watermark_status = "captured"
            finalization.profile_watermark = watermark
            return
        context = await self.repository._validate_claim_on_connection(
            self.conn, claim, for_idle=False
        )
        if context.owner_type != "member":
            raise SessionClaimConflict
        row = await (
            await self.conn.execute(
                """
                UPDATE chat_session_finalizations
                SET watermark_status='captured', profile_watermark=%s, updated_at=now()
                WHERE finalization_id=%s AND context_id=%s AND generation=%s
                  AND reason=%s AND watermark_status='pending'
                RETURNING finalization_id
                """,
                (
                    watermark,
                    claim.finalization_id,
                    claim.context_id,
                    claim.generation,
                    claim.reason,
                ),
            )
        ).fetchone()
        if row is not None:
            return
        existing = await (
            await self.conn.execute(
                """
                SELECT profile_watermark
                FROM chat_session_finalizations
                WHERE finalization_id=%s AND context_id=%s AND generation=%s
                  AND reason=%s AND watermark_status='captured'
                """,
                (
                    claim.finalization_id,
                    claim.context_id,
                    claim.generation,
                    claim.reason,
                ),
            )
        ).fetchone()
        if existing is None or int(existing[0]) != watermark:
            raise SessionClaimConflict

    async def validate_idle_delete(self, claim: FinalizationClaim) -> SessionContext:
        self._bind_claim(claim)
        if self.conn is None:
            context = self.repository._validate_memory_claim(claim, for_idle=True)
        else:
            context = await self.repository._validate_claim_on_connection(
                self.conn, claim, for_idle=True
            )
        if context.state != "idle_finalizing":
            raise SessionClaimConflict
        return context

    async def complete_idle_delete(self, claim: FinalizationClaim) -> None:
        self._bind_claim(claim)
        await self.validate_idle_delete(claim)
        if self.conn is None:
            row = self.repository._contexts[self.session_id]
            finalization = self.repository._finalizations[claim.finalization_id]
            finalization.transient_status = "completed"
            finalization.status = "completed"
            finalization.claim_token = None
            finalization.lease_expires_at = None
            row.state = "idle_expired"
            row.threads.clear()
            return
        finalization = await (
            await self.conn.execute(
                """
            UPDATE chat_session_finalizations
            SET transient_status='completed', status='completed',
                claim_token=NULL, lease_expires_at=NULL, updated_at=now()
            WHERE finalization_id=%s AND context_id=%s AND generation=%s
              AND reason='idle' AND transient_status='pending'
            RETURNING finalization_id
            """,
                (claim.finalization_id, claim.context_id, claim.generation),
            )
        ).fetchone()
        if finalization is None:
            raise SessionClaimConflict
        context = await (
            await self.conn.execute(
                """
                UPDATE chat_session_contexts SET state='idle_expired', updated_at=now()
                WHERE context_id=%s AND generation=%s AND state='idle_finalizing'
                RETURNING context_id
                """,
                (claim.context_id, claim.generation),
            )
        ).fetchone()
        if context is None:
            raise SessionClaimConflict
        await self.conn.execute(
            "DELETE FROM chat_session_threads WHERE context_id=%s", (claim.context_id,)
        )

    def _bind_claim(self, claim: FinalizationClaim) -> None:
        if claim.session_id != self.session_id:
            raise SessionClaimConflict


async def _advisory_lock(conn, session_id: str) -> None:  # noqa: ANN001
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('chat-session:' || %s, 0))",
        (session_id,),
    )


async def _postgres_now(conn) -> datetime:  # noqa: ANN001
    row = await (await conn.execute("SELECT now()")).fetchone()
    return row[0]


def _positive(*values: float) -> None:
    if any(value <= 0 for value in values):
        raise ValueError("timeouts, leases and batch sizes must be positive")


def _row_to_context(row) -> SessionContext:  # noqa: ANN001
    return SessionContext(str(row[0]), str(row[1]), row[2], str(row[3]), int(row[4]), row[5])


def _memory_context(row: _MemoryContext) -> SessionContext:
    return SessionContext(
        row.context_id, row.session_id, row.owner_type, row.owner_id, row.generation, row.state
    )


def _row_to_claim(row) -> FinalizationClaim:  # noqa: ANN001
    return FinalizationClaim(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        row[3],
        str(row[4]),
        int(row[5]),
        row[6],
        str(row[7]),
        row[8],
    )


def _memory_claim(row: _MemoryContext, finalization: _MemoryFinalization) -> FinalizationClaim:
    assert finalization.claim_token is not None and finalization.lease_expires_at is not None
    return FinalizationClaim(
        finalization.finalization_id,
        row.context_id,
        row.session_id,
        row.owner_type,
        row.owner_id,
        finalization.generation,
        finalization.reason,
        finalization.claim_token,
        finalization.lease_expires_at,
    )


def _claim_from_finalization(
    context: SessionContext, finalization: SessionFinalization
) -> FinalizationClaim:
    assert finalization.claim_token is not None and finalization.lease_expires_at is not None
    return FinalizationClaim(
        finalization.finalization_id,
        context.context_id,
        context.session_id,
        context.owner_type,
        context.owner_id,
        finalization.generation,
        finalization.reason,
        finalization.claim_token,
        finalization.lease_expires_at,
    )


def _row_to_finalization(row) -> SessionFinalization:  # noqa: ANN001
    return SessionFinalization(
        str(row[0]),
        str(row[1]),
        int(row[2]),
        row[3],
        row[4],
        str(row[5]) if row[5] is not None else None,
        row[6],
        row[7],
        int(row[8]) if row[8] is not None else None,
        row[9],
        row[10],
    )


def _memory_finalization(row: _MemoryFinalization) -> SessionFinalization:
    return SessionFinalization(
        row.finalization_id,
        row.context_id,
        row.generation,
        row.reason,
        row.status,
        row.claim_token,
        row.lease_expires_at,
        row.watermark_status,
        row.profile_watermark,
        row.transient_status,
        row.profile_status,
    )


def _row_to_profile_candidate(row) -> ProfileRecoveryCandidate:  # noqa: ANN001
    return ProfileRecoveryCandidate(
        str(row[0]), str(row[1]), str(row[2]), str(row[3]), int(row[4]), int(row[5])
    )


_default_repository = SessionContextRepository()
_owned_pool = None


def set_pool(pool) -> None:  # noqa: ANN001
    global _default_repository, _owned_pool
    _owned_pool = None
    _default_repository = SessionContextRepository(pool=pool)


def reset() -> None:
    global _default_repository, _owned_pool
    _owned_pool = None
    _default_repository = SessionContextRepository()


async def initialize() -> None:
    global _default_repository, _owned_pool
    if _default_repository._pool is None:
        from psycopg_pool import AsyncConnectionPool, PoolTimeout  # noqa: PLC0415

        from app.core.pg_resilience import hardened_pg_conninfo  # noqa: PLC0415

        settings = get_settings()
        pool = AsyncConnectionPool(
            hardened_pg_conninfo(settings.profile_db_url),
            open=False,
            min_size=settings.state_store_pool_min_size,
            max_size=settings.state_store_pool_max_size,
            timeout=settings.state_store_query_timeout_s,
        )
        try:
            await asyncio.wait_for(
                pool.open(wait=True), timeout=settings.state_store_connect_timeout_s
            )
        except (TimeoutError, PoolTimeout) as exc:
            try:
                await pool.close()
            except Exception:
                logger.exception("session lifecycle timed-out pool cleanup failed")
            if settings.auth_mode == "jwks":
                raise SessionStateUnavailable from exc
            logger.warning(
                "session lifecycle profile DB timeout; fallback=memory auth_mode=%s error_type=%s",
                settings.auth_mode,
                type(exc).__name__,
            )
            return
        except BaseException:
            try:
                await pool.close()
            except Exception:
                logger.exception("session lifecycle failed pool cleanup failed")
            raise
        _owned_pool = pool
        _default_repository = SessionContextRepository(pool=pool)
    await _default_repository.initialize()


initialize_session_lifecycle = initialize


async def resolve_touch_register_on_connection(
    conn,
    input: BuyerSessionInput,  # noqa: ANN001
) -> SessionContext:
    return await _default_repository.resolve_touch_register_on_connection(conn, input)


async def claim_owner(session_id: str, guest_id: str, user_id: int) -> ClaimOutcome:
    return await _default_repository.claim_owner(session_id, guest_id, user_id)


async def claim_expired_contexts(
    idle_timeout_s: float, lease_s: float, batch_size: int
) -> list[FinalizationClaim]:
    return await _default_repository.claim_expired_contexts(idle_timeout_s, lease_s, batch_size)


async def claim_recoverable_finalizations(
    lease_s: float, batch_size: int
) -> list[FinalizationClaim]:
    return await _default_repository.claim_recoverable_finalizations(lease_s, batch_size)


async def list_recoverable_profile_phases(
    batch_size: int,
) -> list[ProfileRecoveryCandidate]:
    return await _default_repository.list_recoverable_profile_phases(batch_size)


async def claim_profile_phase(finalization_id: str, lease_s: float) -> FinalizationClaim | None:
    return await _default_repository.claim_profile_phase(finalization_id, lease_s)


async def begin_terminal(user_id: int, session_id: str) -> TerminalOutcome:
    return await _default_repository.begin_terminal(user_id, session_id)


async def validate_for_delete(claim: FinalizationClaim) -> SessionContext:
    return await _default_repository.validate_for_delete(claim)


async def mark_idle_finalizing(claim: FinalizationClaim) -> None:
    await _default_repository.mark_idle_finalizing(claim)


async def complete_transient_phase(claim: FinalizationClaim) -> None:
    await _default_repository.complete_transient_phase(claim)


async def record_profile_phase(finalization_id: str, status: ProfilePhaseStatus) -> None:
    await _default_repository.record_profile_phase(finalization_id, status)


async def get_finalization(finalization_id: str) -> SessionFinalization:
    return await _default_repository.get_finalization(finalization_id)


async def get_threads(context_id: str) -> list[str]:
    return await _default_repository.get_threads(context_id)


def lock_session(session_id: str) -> AsyncContextManager[SessionContextUnitOfWork]:
    return _default_repository.lock_session(session_id)
