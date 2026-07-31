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
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncContextManager, AsyncIterator, Literal

from app.core.config import get_settings
from app.core.logging import safe_fingerprint

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
    supersedes_finalization_id: str | None = None
    superseded_at: datetime | float | None = None


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
    authority_source: Literal["runtime", "legacy_backfill"] = "runtime"
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
    supersedes_finalization_id: str | None = None
    superseded_at: float | None = None


@dataclass(frozen=True)
class _BackfillProgress:
    batch: int
    backfill_pass: int
    cursor_fp: str | None
    completed: bool
    grace_deadline_set: bool


@asynccontextmanager
async def _backfill_transaction(conn) -> AsyncIterator[list[_BackfillProgress]]:  # noqa: ANN001
    """commit이 성공한 뒤에만 해당 트랜잭션에서 관찰한 진행 상태를 기록한다."""
    progress: list[_BackfillProgress] = []
    async with conn.transaction():
        yield progress
    for event in progress:
        logger.info(
            "session lifecycle backfill batch=%d pass=%d cursorFp=%s "
            "completed=%s graceDeadlineSet=%s",
            event.batch,
            event.backfill_pass,
            event.cursor_fp,
            event.completed,
            event.grace_deadline_set,
        )


class SessionContextRepository:
    """Lifecycle repository backed by a psycopg async pool or an in-memory fallback."""

    def __init__(self, pool=None, *, clock=time.monotonic) -> None:  # noqa: ANN001
        self._pool = pool
        self._clock = clock
        self._contexts: dict[str, _MemoryContext] = {}
        self._finalizations: dict[str, _MemoryFinalization] = {}
        self._owner_claims: dict[str, tuple[str, str]] = {}
        self._migration_conflicts: dict[tuple[str, str], tuple[str, str | None]] = {}
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

    async def ensure_legacy_store_trigger(self) -> None:
        """Install the exact-prefix legacy store trigger after lazy store DDL."""
        if self._pool is None:
            return
        async with self._pool.connection() as conn:
            await conn.execute("SELECT install_session_context_legacy_store_trigger()")

    async def backfill_legacy_activity(self) -> None:
        """Resume a durable bounded composite-key pass over legacy activity."""
        if self._pool is None:
            return
        settings = get_settings()
        migration_name = "issue-187-session-context"
        migrated = conflicts = 0
        batches = 0
        while True:
            if batches >= settings.session_lifecycle_backfill_max_batches:
                raise SessionStateUnavailable(
                    "session lifecycle backfill startup batch limit exceeded"
                )
            batches += 1
            async with self._pool.connection() as conn:
                async with _backfill_transaction(conn) as progress:
                    await conn.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(max(1, int(settings.state_store_migration_timeout_s * 1000))),),
                    )
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        ("migration:issue-187-session-context",),
                    )
                    await conn.execute(
                        """
                        INSERT INTO chat_session_migrations
                            (migration_name, rollout_started_at, grace_deadline,
                             profile_backfill_cursor, profile_backfill_owner_cursor,
                             profile_backfill_pass, legacy_quiet_window_s)
                        VALUES (
                            %s, now(), now() + make_interval(secs => %s), '', -1, 1, %s
                        )
                        ON CONFLICT (migration_name) DO UPDATE
                        SET grace_deadline=GREATEST(
                                chat_session_migrations.grace_deadline,
                                chat_session_migrations.rollout_started_at
                                    + make_interval(secs => %s),
                                chat_session_migrations.rollout_started_at
                                    + interval '24 hours'
                            ),
                            legacy_quiet_window_s=GREATEST(
                                chat_session_migrations.legacy_quiet_window_s,
                                %s,
                                90
                            ),
                            legacy_quiet_until=CASE
                                WHEN chat_session_migrations.legacy_writer_seen_at IS NULL
                                THEN chat_session_migrations.legacy_quiet_until
                                ELSE GREATEST(
                                    chat_session_migrations.legacy_quiet_until,
                                    chat_session_migrations.legacy_writer_seen_at
                                        + make_interval(secs => %s)
                                )
                            END,
                            updated_at=now()
                        """,
                        (
                            migration_name,
                            settings.session_lifecycle_legacy_grace_s,
                            settings.session_lifecycle_legacy_quiet_s,
                            settings.session_lifecycle_legacy_grace_s,
                            settings.session_lifecycle_legacy_quiet_s,
                            settings.session_lifecycle_legacy_quiet_s,
                        ),
                    )
                    migration = await (
                        await conn.execute(
                            "SELECT profile_backfill_cursor, profile_backfill_owner_cursor, "
                            "profile_backfill_completed_at, profile_backfill_pass, "
                            "grace_deadline "
                            "FROM chat_session_migrations WHERE migration_name=%s FOR UPDATE",
                            (migration_name,),
                        )
                    ).fetchone()
                    assert migration is not None
                    cursor, owner_cursor, completed_at, backfill_pass, grace_deadline = migration
                    cursor_fp = safe_fingerprint(str(cursor)) if cursor not in (None, "") else None
                    if cursor is None and completed_at is not None:
                        if await _find_actionable_legacy_activity(conn) is None:
                            progress.append(
                                _BackfillProgress(
                                    batches,
                                    backfill_pass,
                                    cursor_fp,
                                    True,
                                    grace_deadline is not None,
                                )
                            )
                            break
                        await conn.execute(
                            """
                            UPDATE chat_session_migrations
                            SET profile_backfill_cursor='',
                                profile_backfill_owner_cursor=-1,
                                profile_backfill_completed_at=NULL,
                                gc_completed_at=NULL,
                                profile_backfill_pass=profile_backfill_pass+1,
                                updated_at=now()
                            WHERE migration_name=%s
                            """,
                            (migration_name,),
                        )
                        cursor, owner_cursor = "", -1
                    rows = await (
                        await conn.execute(
                            """
                            SELECT a.user_id, a.session_id, a.last_activity_at, a.status,
                                   e.status='completed' AS fixed_event_completed
                            FROM profile_session_activity a
                            LEFT JOIN processed_events e
                              ON e.event_id='session-end:' || a.user_id::text || ':' || a.session_id
                            WHERE a.session_id > %s
                               OR (a.session_id = %s AND a.user_id > %s)
                            ORDER BY a.session_id, a.user_id
                            LIMIT %s
                            """,
                            (
                                cursor or "",
                                cursor or "",
                                owner_cursor if owner_cursor is not None else -1,
                                settings.session_lifecycle_gc_batch_size,
                            ),
                        )
                    ).fetchall()
                    if not rows:
                        if await _find_actionable_legacy_activity(conn) is not None:
                            progress.append(
                                _BackfillProgress(
                                    batches,
                                    backfill_pass,
                                    cursor_fp,
                                    completed_at is not None,
                                    grace_deadline is not None,
                                )
                            )
                            await conn.execute(
                                "UPDATE chat_session_migrations SET "
                                "profile_backfill_cursor='', "
                                "profile_backfill_owner_cursor=-1, "
                                "profile_backfill_pass=profile_backfill_pass+1, "
                                "updated_at=now() "
                                "WHERE migration_name=%s",
                                (migration_name,),
                            )
                            continue
                        await conn.execute(
                            """
                            UPDATE chat_session_migrations
                            SET profile_backfill_cursor=NULL,
                                profile_backfill_owner_cursor=NULL,
                                profile_backfill_completed_at=now(),
                                thread_hint_completed_at=COALESCE(thread_hint_completed_at, now()),
                                updated_at=now()
                            WHERE migration_name=%s
                            """,
                            (migration_name,),
                        )
                        completed_migration = await (
                            await conn.execute(
                                "SELECT profile_backfill_cursor, "
                                "profile_backfill_owner_cursor, "
                                "profile_backfill_completed_at, profile_backfill_pass, "
                                "grace_deadline "
                                "FROM chat_session_migrations WHERE migration_name=%s FOR UPDATE",
                                (migration_name,),
                            )
                        ).fetchone()
                        assert completed_migration is not None
                        (
                            completed_cursor,
                            _completed_owner_cursor,
                            completed_at,
                            completed_pass,
                            completed_grace_deadline,
                        ) = completed_migration
                        completed_cursor_fp = (
                            safe_fingerprint(str(completed_cursor))
                            if completed_cursor not in (None, "")
                            else None
                        )
                        progress.append(
                            _BackfillProgress(
                                batches,
                                completed_pass,
                                completed_cursor_fp,
                                completed_at is not None,
                                completed_grace_deadline is not None,
                            )
                        )
                        break
                    progress.append(
                        _BackfillProgress(
                            batches,
                            backfill_pass,
                            cursor_fp,
                            completed_at is not None,
                            grace_deadline is not None,
                        )
                    )
                    by_session: dict[str, list] = {}
                    for row in rows:
                        by_session.setdefault(row[1], []).append(row)
                    for session_id, candidates in by_session.items():
                        # Lock order: migration advisory lock -> session advisory lock.
                        # Runtime touch/claim takes only the session lock, so it never
                        # waits on migration state while holding the lower-order lock.
                        await _advisory_lock(conn, session_id)
                        existing = await (
                            await conn.execute(
                                "SELECT context_id, owner_type, owner_id, authority_source "
                                "FROM chat_session_contexts WHERE session_id=%s FOR UPDATE",
                                (session_id,),
                            )
                        ).fetchone()
                        quarantined = await (
                            await conn.execute(
                                "SELECT owner_id FROM chat_session_migration_conflicts "
                                "WHERE session_id=%s AND resolution_status='quarantined'",
                                (session_id,),
                            )
                        ).fetchall()
                        owners = {str(row[0]) for row in candidates}
                        selected = None
                        if existing is not None and existing[3] == "runtime":
                            selected = next(
                                (row for row in candidates if str(row[0]) == existing[2]), None
                            )
                        elif quarantined:
                            selected = None
                            if existing is not None and existing[3] == "legacy_backfill":
                                candidates = [
                                    (
                                        int(existing[2]),
                                        session_id,
                                        candidates[0][2],
                                        candidates[0][3],
                                        False,
                                    ),
                                    *candidates,
                                ]
                                await conn.execute(
                                    "DELETE FROM chat_session_contexts WHERE context_id=%s",
                                    (existing[0],),
                                )
                                existing = None
                        elif existing is not None and existing[3] == "legacy_backfill":
                            owners.add(existing[2])
                            if len(owners) == 1:
                                selected = next(
                                    (row for row in candidates if str(row[0]) == existing[2]), None
                                )
                            else:
                                # A migration guess is not authority. Quarantine even its
                                # originally selected owner and remove the guessed context.
                                synthetic = (
                                    int(existing[2]),
                                    session_id,
                                    candidates[0][2],
                                    candidates[0][3],
                                    False,
                                )
                                candidates = [synthetic, *candidates]
                                await conn.execute(
                                    "DELETE FROM chat_session_contexts WHERE context_id=%s",
                                    (existing[0],),
                                )
                                existing = None
                        elif existing is None and len(owners) == 1:
                            selected = candidates[0]
                        if selected is not None and existing is None:
                            state: ContextState = (
                                "active"
                                if selected[3] in ("active", "processing")
                                else "terminal"
                                if bool(selected[4])
                                else "idle_expired"
                            )
                            inserted = await (
                                await conn.execute(
                                    """
                                    INSERT INTO chat_session_contexts
                                        (context_id, session_id, owner_type, owner_id,
                                         authority_source, state, last_activity_at)
                                    VALUES (%s, %s, 'member', %s, 'legacy_backfill', %s, %s)
                                    ON CONFLICT (session_id) DO NOTHING RETURNING context_id
                                    """,
                                    (
                                        str(uuid.uuid4()),
                                        session_id,
                                        str(selected[0]),
                                        state,
                                        selected[2],
                                    ),
                                )
                            ).fetchone()
                            migrated += int(inserted is not None)
                        seen: set[str] = set()
                        for row in candidates:
                            owner = str(row[0])
                            if owner in seen or (
                                selected is not None and owner == str(selected[0])
                            ):
                                continue
                            seen.add(owner)
                            inserted = await (
                                await conn.execute(
                                    """
                                    INSERT INTO chat_session_migration_conflicts
                                        (session_id, owner_id, legacy_status, legacy_last_activity_at)
                                    VALUES (%s, %s, %s, %s)
                                    ON CONFLICT (session_id, owner_id) DO NOTHING
                                    RETURNING conflict_id
                                    """,
                                    (session_id, owner, row[3], row[2]),
                                )
                            ).fetchone()
                            conflicts += int(inserted is not None)
                    await conn.execute(
                        "UPDATE chat_session_migrations SET profile_backfill_cursor=%s, "
                        "profile_backfill_owner_cursor=%s, updated_at=now() "
                        "WHERE migration_name=%s",
                        (rows[-1][1], rows[-1][0], migration_name),
                    )
        logger.info(
            "session lifecycle legacy backfill completed migrated_count=%d conflict_count=%d",
            migrated,
            conflicts,
        )

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
                if row.owner_type != input.owner_type or row.owner_id != input.owner_id:
                    if row.authority_source != "legacy_backfill":
                        raise SessionForbidden
                    self._migration_conflicts[(row.session_id, row.owner_id)] = (
                        "quarantined",
                        None,
                    )
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
            row.authority_source = "runtime"
            context = _memory_context(row)
            await self._resolve_authoritative_conflict(None, context)
            return context

    async def resolve_touch_register_on_connection(
        self,
        conn,
        input: BuyerSessionInput,  # noqa: ANN001
    ) -> SessionContext:
        await _advisory_lock(conn, input.session_id)
        row = await (
            await conn.execute(
                "SELECT context_id, session_id, owner_type, owner_id, generation, state, "
                "authority_source, last_activity_at "
                "FROM chat_session_contexts WHERE session_id=%s FOR UPDATE",
                (input.session_id,),
            )
        ).fetchone()
        if row is None:
            context_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO chat_session_contexts
                    (context_id, session_id, owner_type, owner_id, authority_source, state)
                VALUES (%s, %s, %s, %s, 'runtime', 'active')
                ON CONFLICT (session_id) DO NOTHING
                """,
                (context_id, input.session_id, input.owner_type, input.owner_id),
            )
            row = await (
                await conn.execute(
                    "SELECT context_id, session_id, owner_type, owner_id, generation, state, "
                    "authority_source, last_activity_at "
                    "FROM chat_session_contexts WHERE session_id=%s FOR UPDATE",
                    (input.session_id,),
                )
            ).fetchone()
        assert row is not None
        context = _row_to_context(row)
        if context.owner_type != input.owner_type or context.owner_id != input.owner_id:
            if row[6] != "legacy_backfill":
                raise SessionForbidden
            await conn.execute(
                """
                INSERT INTO chat_session_migration_conflicts
                    (session_id, owner_id, legacy_status, legacy_last_activity_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (session_id, owner_id) DO UPDATE
                SET legacy_status=EXCLUDED.legacy_status,
                    legacy_last_activity_at=EXCLUDED.legacy_last_activity_at,
                    resolution_status='quarantined',
                    resolved_context_id=NULL,
                    updated_at=now()
                """,
                (
                    input.session_id,
                    context.owner_id,
                    "active" if context.state == "active" else "completed",
                    row[7],
                ),
            )
            await conn.execute(
                "DELETE FROM chat_session_contexts WHERE context_id=%s",
                (context.context_id,),
            )
            row = await (
                await conn.execute(
                    """
                    INSERT INTO chat_session_contexts
                        (context_id, session_id, owner_type, owner_id,
                         authority_source, state)
                    VALUES (%s, %s, %s, %s, 'runtime', 'active')
                    RETURNING context_id, session_id, owner_type, owner_id, generation, state
                    """,
                    (
                        str(uuid.uuid4()),
                        input.session_id,
                        input.owner_type,
                        input.owner_id,
                    ),
                )
            ).fetchone()
            context = _row_to_context(row)
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
                SET generation=%s, state='active', authority_source='runtime',
                    last_activity_at=now(), updated_at=now()
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
        resolved = _row_to_context(row)
        await self._resolve_authoritative_conflict(conn, resolved)
        return resolved

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
            legacy_claim = row is not None and row.authority_source == "legacy_backfill"
            if legacy_claim:
                if row.owner_type != "member" or row.owner_id != target:
                    self._migration_conflicts[(session_id, row.owner_id)] = (
                        "quarantined",
                        None,
                    )
                    row = _MemoryContext(
                        str(uuid.uuid4()),
                        session_id,
                        "member",
                        target,
                        0,
                        "active",
                        self._clock(),
                    )
                    self._contexts[session_id] = row
                else:
                    row.authority_source = "runtime"
                    row.state = "active"
            # duplicate 단축 반환도 아래 terminal/idle_finalizing 게이트를 통과해야 한다 —
            # 먼저 평가되므로 상태를 안 보면 이 분기가 게이트의 예외가 된다.
            if (
                history == (guest_id, target)
                and row is not None
                and row.state not in ("idle_finalizing", "terminal")
            ):
                context = _memory_context(row)
                await self._resolve_authoritative_conflict(None, context)
                return ClaimOutcome(context, False)
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
                if legacy_claim and row.owner_type == "member" and row.owner_id == target:
                    row.authority_source = "runtime"
                    row.state = "active"
                elif row.owner_type != "guest" or row.owner_id != guest_id:
                    raise SessionClaimConflict
                else:
                    # SQL 경로(_claim_owner_on_connection)와 같은 순서를 지킨다 —
                    # generation 을 올리기 전에 현재 세대의 진행 중 idle finalization 을
                    # 먼저 닫는다. 건너뛰면 row 는 이전 세대에 남고 어느 재수집 경로에도
                    # 걸리지 않는 고아가 된다.
                    for stale in self._finalizations.values():
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
                            stale.superseded_at = self._clock()
                    row.owner_type = "member"
                    row.owner_id = target
                    row.generation += 1
                    row.state = "active"
            self._owner_claims[session_id] = (guest_id, target)
            context = _memory_context(row)
            await self._resolve_authoritative_conflict(None, context)
            return ClaimOutcome(context, True)

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
                "SELECT context_id, session_id, owner_type, owner_id, generation, state, "
                "authority_source, last_activity_at "
                "FROM chat_session_contexts WHERE session_id=%s FOR UPDATE",
                (session_id,),
            )
        ).fetchone()
        legacy_claim = row is not None and row[6] == "legacy_backfill"
        if legacy_claim:
            if row[2] != "member" or row[3] != target:
                await conn.execute(
                    """
                    INSERT INTO chat_session_migration_conflicts
                        (session_id, owner_id, legacy_status, legacy_last_activity_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (session_id, owner_id) DO UPDATE
                    SET legacy_status=EXCLUDED.legacy_status,
                        legacy_last_activity_at=EXCLUDED.legacy_last_activity_at,
                        resolution_status='quarantined',
                        resolved_context_id=NULL,
                        updated_at=now()
                    """,
                    (
                        session_id,
                        str(row[3]),
                        "active" if row[5] == "active" else "completed",
                        row[7],
                    ),
                )
                await conn.execute(
                    "DELETE FROM chat_session_contexts WHERE context_id=%s", (row[0],)
                )
                row = await (
                    await conn.execute(
                        """
                        INSERT INTO chat_session_contexts
                            (context_id, session_id, owner_type, owner_id,
                             authority_source, state)
                        VALUES (%s, %s, 'member', %s, 'runtime', 'active')
                        RETURNING context_id, session_id, owner_type, owner_id,
                                  generation, state
                        """,
                        (str(uuid.uuid4()), session_id, target),
                    )
                ).fetchone()
            else:
                row = await (
                    await conn.execute(
                        """
                        UPDATE chat_session_contexts
                        SET authority_source='runtime', state='active', updated_at=now()
                        WHERE context_id=%s
                        RETURNING context_id, session_id, owner_type, owner_id,
                                  generation, state
                        """,
                        (row[0],),
                    )
                ).fetchone()
        # duplicate 단축 반환도 아래 terminal/idle_finalizing 게이트를 통과해야 한다 —
        # 먼저 평가되므로 상태를 안 보면 이 분기가 게이트의 예외가 된다.
        if (
            history == (guest_id, target)
            and row is not None
            and row[5]
            not in (
                "idle_finalizing",
                "terminal",
            )
        ):
            context = _row_to_context(row)
            await self._resolve_authoritative_conflict(conn, context)
            return ClaimOutcome(context, False)
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
                        (context_id, session_id, owner_type, owner_id,
                         authority_source, state)
                    VALUES (%s, %s, 'member', %s, 'runtime', 'active')
                    RETURNING context_id, session_id, owner_type, owner_id, generation, state
                    """,
                    (context_id, session_id, target),
                )
            ).fetchone()
        else:
            if legacy_claim and row[2] == "member" and row[3] == target:
                pass
            elif row[2] != "guest" or row[3] != guest_id:
                raise SessionClaimConflict
            else:
                # 재활성화(resolve_touch_register_on_connection)와 같은 순서를 지킨다 —
                # generation 을 올리기 전에 현재 세대의 진행 중 idle finalization 을 먼저
                # superseded 로 닫는다. 이 단계를 건너뛰면 row 는 이전 세대에 남고 context 는
                # 다음 세대로 가서, claim_expired_contexts(c.generation=f.generation)와
                # claim_recoverable_finalizations(state IN idle_finalizing/terminal) 어느
                # 재수집 경로에도 걸리지 않는 고아가 된다.
                await conn.execute(
                    """
                    UPDATE chat_session_finalizations
                    SET status='superseded', claim_token=NULL, lease_expires_at=NULL,
                        superseded_at=now(), updated_at=now()
                    WHERE context_id=%s AND generation=%s AND reason='idle'
                      AND status <> 'superseded'
                      AND transient_status='pending'
                    """,
                    (row[0], row[4]),
                )
                row = await (
                    await conn.execute(
                        """
                        UPDATE chat_session_contexts
                        SET owner_type='member', owner_id=%s, generation=generation+1,
                            authority_source='runtime', state='active', updated_at=now()
                        WHERE context_id=%s
                        RETURNING context_id, session_id, owner_type, owner_id,
                                  generation, state
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
        context = _row_to_context(row)
        await self._resolve_authoritative_conflict(conn, context)
        return ClaimOutcome(context, True)

    async def _resolve_authoritative_conflict(
        self,
        conn,
        context: SessionContext,  # noqa: ANN001
    ) -> None:
        """Resolve only a still-quarantined row while the caller holds the session lock."""
        key = (context.session_id, context.owner_id)
        if conn is None:
            current = self._migration_conflicts.get(key)
            if current is not None and current[0] == "quarantined":
                self._migration_conflicts[key] = ("resolved", context.context_id)
            return
        await conn.execute(
            """
            UPDATE chat_session_migration_conflicts
            SET resolution_status='resolved', resolved_context_id=%s, updated_at=now()
            WHERE session_id=%s AND owner_id=%s
              AND resolution_status='quarantined'
            """,
            (context.context_id, context.session_id, context.owner_id),
        )

    async def claim_expired_contexts(
        self, idle_timeout_s: float, lease_s: float, batch_size: int
    ) -> list[FinalizationClaim]:
        _positive(idle_timeout_s, lease_s, batch_size)
        if self._pool is not None:
            token = uuid.uuid4().hex
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    rows = await (
                        await conn.execute(
                            """
                            WITH prephase_candidates AS (
                                SELECT f.finalization_id
                                FROM chat_session_finalizations f
                                JOIN chat_session_contexts c USING (context_id)
                                WHERE c.state='active'
                                  AND c.generation=f.generation
                                  AND c.last_activity_at
                                      <= now() - make_interval(secs => %s)
                                  AND f.reason='idle'
                                  AND f.status <> 'superseded'
                                  AND f.transient_status='pending'
                                  AND f.watermark_status='pending'
                                  AND f.profile_watermark IS NULL
                                  AND (
                                      f.lease_expires_at IS NULL
                                      OR f.lease_expires_at <= now()
                                  )
                                ORDER BY f.lease_expires_at NULLS FIRST, f.finalization_id
                                FOR UPDATE OF f SKIP LOCKED
                                LIMIT %s
                            ), reclaimed AS (
                                UPDATE chat_session_finalizations f
                                SET status='processing', claim_token=%s,
                                    lease_expires_at=now() + make_interval(secs => %s),
                                    updated_at=now()
                                FROM prephase_candidates x, chat_session_contexts c
                                WHERE f.finalization_id=x.finalization_id
                                  AND c.context_id=f.context_id
                                  AND c.state='active'
                                  AND c.generation=f.generation
                                  AND f.reason='idle'
                                  AND f.status <> 'superseded'
                                  AND f.transient_status='pending'
                                  AND f.watermark_status='pending'
                                  AND f.profile_watermark IS NULL
                                  AND (
                                      f.lease_expires_at IS NULL
                                      OR f.lease_expires_at <= now()
                                  )
                                RETURNING f.*
                            ), slots AS (
                                SELECT GREATEST(%s - count(*), 0)::bigint AS remaining
                                FROM reclaimed
                            ), fresh_candidates AS (
                                SELECT c.context_id, c.generation
                                FROM chat_session_contexts c
                                WHERE c.state='active'
                                  AND c.last_activity_at
                                      <= now() - make_interval(secs => %s)
                                  AND NOT EXISTS (
                                      SELECT 1 FROM chat_session_finalizations f
                                      WHERE f.context_id=c.context_id
                                        AND f.generation=c.generation
                                        AND f.reason='idle'
                                  )
                                ORDER BY c.last_activity_at, c.context_id
                                FOR UPDATE OF c SKIP LOCKED
                                LIMIT (SELECT remaining FROM slots)
                            ), inserted AS (
                                INSERT INTO chat_session_finalizations
                                    (finalization_id, context_id, generation, reason, status,
                                     claim_token, lease_expires_at)
                                SELECT gen_random_uuid(), c.context_id, c.generation, 'idle',
                                       'processing', %s,
                                       now() + make_interval(secs => %s)
                                FROM chat_session_contexts c
                                JOIN fresh_candidates USING (context_id, generation)
                                RETURNING *
                            ), claimed AS (
                                SELECT * FROM reclaimed
                                UNION ALL
                                SELECT * FROM inserted
                            )
                            SELECT f.finalization_id, f.context_id, c.session_id, c.owner_type,
                                   c.owner_id, f.generation, f.reason, f.claim_token,
                                   f.lease_expires_at
                            FROM claimed f JOIN chat_session_contexts c USING (context_id)
                            """,
                            (
                                idle_timeout_s,
                                batch_size,
                                token,
                                lease_s,
                                batch_size,
                                idle_timeout_s,
                                token,
                                lease_s,
                            ),
                        )
                    ).fetchall()
            return [_row_to_claim(row) for row in rows]
        now = self._clock()
        due = sorted(
            (
                row
                for row in self._contexts.values()
                if row.state == "active" and row.last_activity_at <= now - idle_timeout_s
            ),
            key=lambda row: (row.last_activity_at, row.context_id),
        )
        claims = []
        prephase = sorted(
            (
                (finalization, row)
                for row in due
                for finalization in self._finalizations.values()
                if finalization.context_id == row.context_id
                and finalization.generation == row.generation
                and finalization.reason == "idle"
                and _is_expired_idle_prephase(finalization, now)
            ),
            key=lambda pair: (
                pair[0].lease_expires_at if pair[0].lease_expires_at is not None else -1,
                pair[0].finalization_id,
            ),
        )
        for finalization, row in prephase[:batch_size]:
            finalization.status = "processing"
            finalization.claim_token = uuid.uuid4().hex
            finalization.lease_expires_at = now + lease_s
            claims.append(_memory_claim(row, finalization))
        if len(claims) == batch_size:
            return claims
        for row in due:
            current = [
                finalization
                for finalization in self._finalizations.values()
                if finalization.context_id == row.context_id
                and finalization.generation == row.generation
                and finalization.reason == "idle"
            ]
            if current:
                continue
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
            if len(claims) == batch_size:
                break
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
            inherited = next(
                (
                    item
                    for item in sorted(
                        self._finalizations.values(),
                        key=lambda candidate: candidate.generation,
                        reverse=True,
                    )
                    if item.context_id == row.context_id
                    and item.generation == row.generation
                    and item.reason == "idle"
                    and item.status != "superseded"
                ),
                None,
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
                    superseded.superseded_at = self._clock()
            row.generation += 1
            row.state = "terminal"
            transient_completed = (
                inherited is not None and inherited.transient_status == "completed"
            )
            finalization = _MemoryFinalization(
                str(uuid.uuid4()),
                row.context_id,
                row.generation,
                "terminal",
                status="completed" if transient_completed else "processing",
                claim_token=None if transient_completed else uuid.uuid4().hex,
                lease_expires_at=(
                    None
                    if transient_completed
                    else self._clock() + get_settings().profile_idle_claim_ttl_s
                ),
                watermark_status=(
                    inherited.watermark_status if inherited is not None else "pending"
                ),
                profile_watermark=(inherited.profile_watermark if inherited is not None else None),
                transient_status="completed" if transient_completed else "pending",
                supersedes_finalization_id=(
                    inherited.finalization_id if inherited is not None else None
                ),
            )
            self._finalizations[finalization.finalization_id] = finalization
            return TerminalOutcome(
                _memory_context(row),
                _memory_finalization(finalization),
                None if transient_completed else _memory_claim(row, finalization),
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
                              profile_watermark, transient_status, profile_status,
                              supersedes_finalization_id, superseded_at
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
        inherited_row = await (
            await conn.execute(
                """
                SELECT finalization_id, watermark_status, profile_watermark, transient_status
                FROM chat_session_finalizations
                WHERE context_id=%s AND generation=%s AND reason='idle'
                  AND status <> 'superseded'
                ORDER BY updated_at DESC, finalization_id LIMIT 1
                FOR UPDATE
                """,
                (context.context_id, context.generation),
            )
        ).fetchone()
        # 세대 범위를 일부러 걸지 않는다 — 세션 종료는 이 context 의 모든 세대 idle
        # finalization 을 흡수한다. 상속(inherited_row)은 같은 세대만 보지만, 상속이
        # 없으면 terminal 이 Phase A 에서 **현재** watermark 를 새로 캡처하므로
        # (session_lifecycle `already_prepared` 분기) 이전 세대의 pending profile 작업은
        # 누적 버퍼째 terminal 스냅샷에 포함된다. 세대를 걸면 같은 구간이 두 번 처리된다.
        # 계약 고정: test_terminal_supersedes_previous_generation_completed_idle
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
        transient_completed = inherited_row is not None and inherited_row[3] == "completed"
        token = None if transient_completed else uuid.uuid4().hex
        lease_s = get_settings().profile_idle_claim_ttl_s
        finalization_id = str(uuid.uuid4())
        watermark_status = inherited_row[1] if inherited_row is not None else "pending"
        profile_watermark = inherited_row[2] if inherited_row is not None else None
        supersedes_id = str(inherited_row[0]) if inherited_row is not None else None
        final_row = await (
            await conn.execute(
                """
                INSERT INTO chat_session_finalizations
                    (finalization_id, context_id, generation, reason, status,
                     claim_token, lease_expires_at, watermark_status, profile_watermark,
                     transient_status, supersedes_finalization_id)
                VALUES (%s, %s, %s, 'terminal', %s, %s,
                        CASE WHEN %s THEN NULL
                             ELSE now() + make_interval(secs => %s) END,
                        %s, %s, %s, %s)
                RETURNING finalization_id, context_id, generation, reason, status,
                          claim_token, lease_expires_at, watermark_status,
                          profile_watermark, transient_status, profile_status,
                          supersedes_finalization_id, superseded_at
                """,
                (
                    finalization_id,
                    context.context_id,
                    context.generation,
                    "completed" if transient_completed else "processing",
                    token,
                    transient_completed,
                    lease_s,
                    watermark_status,
                    profile_watermark,
                    "completed" if transient_completed else "pending",
                    supersedes_id,
                ),
            )
        ).fetchone()
        finalization = _row_to_finalization(final_row)
        return TerminalOutcome(
            context,
            finalization,
            None if transient_completed else _claim_from_finalization(context, finalization),
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
        """Setup/migration용 tokenless 기록.

        이미 claim된 processing row는 이 호환 API로 변경할 수 없다. production processor는
        claim identity를 검증하는 ``record_claimed_profile_phase``만 사용한다.
        """
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
                        WHERE finalization_id=%s
                          AND status <> 'superseded'
                          AND profile_status <> 'processing'
                        RETURNING finalization_id
                        """,
                        (status, status, status, status, status, finalization_id),
                    )
                ).fetchone()
            if row is None:
                raise SessionClaimConflict
            return
        finalization = self._finalizations.get(finalization_id)
        if (
            finalization is None
            or finalization.status == "superseded"
            or finalization.profile_status == "processing"
        ):
            raise SessionClaimConflict
        finalization.profile_status = status
        if status in ("completed", "skipped"):
            finalization.status = "completed"
        if status in ("completed", "skipped", "retryable"):
            finalization.claim_token = None
            finalization.lease_expires_at = None

    async def record_claimed_profile_phase(
        self,
        finalization_id: str,
        claim_token: str,
        status: Literal["completed", "retryable"],
    ) -> bool:
        """현재 profile claim token 소유자만 phase 결과를 기록한다."""
        if status not in ("completed", "retryable"):
            raise ValueError("invalid claimed profile phase status")
        if self._pool is not None:
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        """
                        UPDATE chat_session_finalizations
                        SET profile_status=%s,
                            status=CASE WHEN %s='completed' THEN 'completed' ELSE status END,
                            claim_token=NULL, lease_expires_at=NULL,
                            completed_at=CASE WHEN %s='completed' THEN now()
                                              ELSE completed_at END,
                            updated_at=now()
                        WHERE finalization_id=%s
                          AND profile_status='processing'
                          AND claim_token=%s
                          AND status <> 'superseded'
                        RETURNING finalization_id
                        """,
                        (status, status, status, finalization_id, claim_token),
                    )
                ).fetchone()
            if row is None:
                raise SessionClaimConflict
            return True
        finalization = self._finalizations.get(finalization_id)
        if (
            finalization is None
            or finalization.status == "superseded"
            or finalization.profile_status != "processing"
            or finalization.claim_token != claim_token
        ):
            raise SessionClaimConflict
        finalization.profile_status = status
        if status == "completed":
            finalization.status = "completed"
        finalization.claim_token = None
        finalization.lease_expires_at = None
        return True

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
                          AND (
                              f.profile_status IN ('pending','retryable')
                              OR (
                                  f.profile_status='processing'
                                  AND (f.lease_expires_at IS NULL OR f.lease_expires_at <= now())
                              )
                          )
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
                and (
                    finalization.profile_status in ("pending", "retryable")
                    or (
                        finalization.profile_status == "processing"
                        and (
                            finalization.lease_expires_at is None
                            or finalization.lease_expires_at <= self._clock()
                        )
                    )
                )
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

    async def is_profile_phase_recoverable(self, finalization_id: str) -> bool:
        """단일 finalization의 profile claim 가능 여부를 authoritative clock으로 판정한다."""
        if self._pool is not None:
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM chat_session_finalizations f
                            JOIN chat_session_contexts c USING (context_id)
                            WHERE f.finalization_id=%s
                              AND c.owner_type='member'
                              AND f.transient_status='completed'
                              AND (
                                  f.profile_status IN ('pending','retryable')
                                  OR (
                                      f.profile_status='processing'
                                      AND (
                                          f.lease_expires_at IS NULL
                                          OR f.lease_expires_at <= now()
                                      )
                                  )
                              )
                              AND f.status <> 'superseded'
                              AND f.watermark_status='captured'
                              AND f.profile_watermark IS NOT NULL
                        )
                        """,
                        (finalization_id,),
                    )
                ).fetchone()
            return bool(row and row[0])
        finalization = self._finalizations.get(finalization_id)
        if finalization is None:
            return False
        context = self._context_by_id(finalization.context_id)
        return bool(
            context.owner_type == "member"
            and finalization.transient_status == "completed"
            and (
                finalization.profile_status in ("pending", "retryable")
                or (
                    finalization.profile_status == "processing"
                    and (
                        finalization.lease_expires_at is None
                        or finalization.lease_expires_at <= self._clock()
                    )
                )
            )
            and finalization.status != "superseded"
            and finalization.watermark_status == "captured"
            and finalization.profile_watermark is not None
        )

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
                          AND (
                              f.profile_status IN ('pending','retryable')
                              OR (
                                  f.profile_status='processing'
                                  AND (f.lease_expires_at IS NULL OR f.lease_expires_at <= now())
                              )
                          )
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
            and (
                finalization.profile_status in ("pending", "retryable")
                or (
                    finalization.profile_status == "processing"
                    and (
                        finalization.lease_expires_at is None
                        or finalization.lease_expires_at <= self._clock()
                    )
                )
            )
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
                               profile_watermark, transient_status, profile_status,
                               supersedes_finalization_id, superseded_at
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
                       profile_watermark, transient_status, profile_status,
                       supersedes_finalization_id, superseded_at
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
                or finalization.lease_expires_at != claim.lease_expires_at
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
                  AND f.lease_expires_at=%s
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
                    claim.lease_expires_at,
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


async def _find_actionable_legacy_activity(conn):  # noqa: ANN001
    return await (
        await conn.execute(
            """
            SELECT a.session_id, a.user_id
            FROM profile_session_activity a
            LEFT JOIN chat_session_contexts c USING (session_id)
            LEFT JOIN chat_session_migration_conflicts q
              ON q.session_id=a.session_id AND q.owner_id=a.user_id::text
            WHERE q.conflict_id IS NULL
              AND (c.context_id IS NULL OR c.owner_id<>a.user_id::text)
            ORDER BY a.session_id, a.user_id
            LIMIT 1
            """
        )
    ).fetchone()


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


def _is_expired_idle_prephase(
    finalization: _MemoryFinalization,
    now: float,
) -> bool:
    return (
        finalization.status != "superseded"
        and finalization.transient_status == "pending"
        and finalization.watermark_status == "pending"
        and finalization.profile_watermark is None
        and (finalization.lease_expires_at is None or finalization.lease_expires_at <= now)
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
        str(row[11]) if len(row) > 11 and row[11] is not None else None,
        row[12] if len(row) > 12 else None,
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
        row.supersedes_finalization_id,
        row.superseded_at,
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
    created_owned_pool = False
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
        created_owned_pool = True
    try:
        await _default_repository.initialize()
        await _default_repository.backfill_legacy_activity()
    except BaseException:
        if created_owned_pool:
            await close_session_lifecycle()
        raise


async def initialize_session_lifecycle() -> None:
    """Finish schema and restart-safe authority backfill before workers may start."""
    await initialize()


async def ensure_legacy_store_trigger() -> None:
    await _default_repository.ensure_legacy_store_trigger()


async def close_session_lifecycle() -> None:
    """Close only the module-owned pool and reset the default repository idempotently."""
    global _default_repository, _owned_pool
    pool = _owned_pool
    if pool is None:
        return
    _owned_pool = None
    _default_repository = SessionContextRepository()
    await pool.close()


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


async def record_claimed_profile_phase(
    finalization_id: str,
    claim_token: str,
    status: Literal["completed", "retryable"],
) -> bool:
    return await _default_repository.record_claimed_profile_phase(
        finalization_id,
        claim_token,
        status,
    )


async def get_finalization(finalization_id: str) -> SessionFinalization:
    return await _default_repository.get_finalization(finalization_id)


async def get_threads(context_id: str) -> list[str]:
    return await _default_repository.get_threads(context_id)


def lock_session(session_id: str) -> AsyncContextManager[SessionContextUnitOfWork]:
    return _default_repository.lock_session(session_id)
