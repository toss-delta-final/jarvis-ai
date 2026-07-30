"""Buyer transient state adoption and context-scoped cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from weakref import WeakValueDictionary

from app.agents.buyer.cart import state as cart_state
from app.core import pg_store, session_context
from app.core.pg_resilience import run_with_query_timeout
from app.core.session_context import SessionContext, SessionStateUnavailable

_LEGACY_FILTER_ROOT = "buyer_thread_filters"
_LEGACY_CART_ROOT = "buyer_cart"
_LEGACY_REVERT_ROOT = "buyer_revert"
_FILTER_ROOT = "buyer_thread_filters_v2"
_CART_ROOT = "buyer_cart_v2"
_REVERT_ROOT = "buyer_revert_v2"
_FILTERS_KEY = "filters"
_PENDING_KEY = "pending"
_LAST_RECO_KEY = "last_reco"
_CATEGORIES_KEY = "categories"

_adoption_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True)
class CleanupCounts:
    filters: int = 0
    pending: int = 0
    last_recommendation: int = 0
    local_names: int = 0
    revert: int = 0

    def __add__(self, other: "CleanupCounts") -> "CleanupCounts":
        return CleanupCounts(
            self.filters + other.filters,
            self.pending + other.pending,
            self.last_recommendation + other.last_recommendation,
            self.local_names + other.local_names,
            self.revert + other.revert,
        )


@dataclass(frozen=True)
class AdoptionResult:
    adopted: bool
    copied: int = 0
    deleted: int = 0


def context_thread_key(context_id: str, thread_id: str) -> str:
    return f"{context_id}:{thread_id}"


def _lock_for(key: str) -> asyncio.Lock:
    lock = _adoption_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _adoption_locks[key] = lock
    return lock


def _column(row, index: int, name: str):  # noqa: ANN001
    return row[name] if isinstance(row, dict) else row[index]


async def _item(store, root: str, key: str, name: str):  # noqa: ANN001
    return await run_with_query_timeout(store.aget((root, key), name))


async def _delete(store, root: str, key: str, name: str) -> int:  # noqa: ANN001
    present = await _item(store, root, key, name)
    if present is None:
        return 0
    await run_with_query_timeout(store.adelete((root, key), name))
    return 1


async def _legacy_root_page(store, root: str, limit: int):  # noqa: ANN001
    """Return an exact namespace-segment page (Postgres prefix matching is textual)."""
    conn_or_pool = getattr(store, "conn", None)
    if conn_or_pool is None:
        rows = await run_with_query_timeout(store.asearch((root,), limit=limit))
        return [row for row in rows if row.namespace and row.namespace[0] == root][:limit]

    async def _query(conn):  # noqa: ANN001
        return await (
            await conn.execute(
                """
                SELECT prefix, key
                FROM store
                WHERE prefix LIKE %s
                ORDER BY prefix, key
                LIMIT %s
                """,
                (root.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%", limit),
            )
        ).fetchall()

    if callable(getattr(conn_or_pool, "connection", None)):
        async with conn_or_pool.connection() as conn:
            rows = await run_with_query_timeout(_query(conn))
    else:
        rows = await run_with_query_timeout(_query(conn_or_pool))
    return [
        (
            tuple(_column(row, 0, "prefix").split(".")),
            _column(row, 1, "key"),
        )
        for row in rows
    ]


def _legacy_prefix_pattern(root: str) -> str:
    return root.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%"


async def _delete_legacy_root_page(
    repo,
    store,
    root: str,
    counter_column: str,
    limit: int,  # noqa: ANN001
) -> tuple[int, bool, list[str]]:
    """Atomically delete/count a PostgreSQL page; retain a bounded generic fallback."""
    if getattr(store, "conn", None) is not None:
        async with repo._pool.connection() as conn:
            async with conn.transaction():
                deleted = await (
                    await conn.execute(
                        """
                        WITH page AS (
                            SELECT prefix, key
                            FROM store
                            WHERE prefix LIKE %s
                            ORDER BY prefix, key
                            LIMIT %s
                            FOR UPDATE SKIP LOCKED
                        )
                        DELETE FROM store s
                        USING page
                        WHERE s.prefix=page.prefix AND s.key=page.key
                        RETURNING s.prefix
                        """,
                        (_legacy_prefix_pattern(root), limit),
                    )
                ).fetchall()
                if deleted:
                    await conn.execute(
                        f"UPDATE chat_session_migrations "
                        f"SET {counter_column}={counter_column}+%s, updated_at=now() "
                        "WHERE migration_name=%s",
                        (len(deleted), "issue-187-session-context"),
                    )
                remaining = await (
                    await conn.execute(
                        "SELECT 1 FROM store WHERE prefix LIKE %s LIMIT 1",
                        (_legacy_prefix_pattern(root),),
                    )
                ).fetchone()
        keys = [str(row[0])[len(root) + 1 :] for row in deleted]
        return len(deleted), remaining is None, keys

    page = await _legacy_root_page(store, root, limit)
    keys: list[str] = []
    for item in page:
        namespace, key = (item.namespace, item.key) if hasattr(item, "namespace") else item
        if len(namespace) > 1:
            keys.append(namespace[1])
        await run_with_query_timeout(store.adelete(namespace, key))
    fresh = await _legacy_root_page(store, root, 1)
    return len(page), not fresh, keys


async def clear_thread(context_id: str, thread_id: str) -> CleanupCounts:
    store = await pg_store.get_store()
    key = context_thread_key(context_id, thread_id)
    filters = await _delete(store, _FILTER_ROOT, key, _FILTERS_KEY)
    pending = await _delete(store, _CART_ROOT, key, _PENDING_KEY)
    last_recommendation = await _delete(store, _CART_ROOT, key, _LAST_RECO_KEY)
    revert = await _delete(store, _REVERT_ROOT, key, _CATEGORIES_KEY)
    local_names = int(cart_state._last_reco_names.pop(key) is not None)
    return CleanupCounts(filters, pending, last_recommendation, local_names, revert)


async def clear_context(context_id: str, thread_ids: Sequence[str]) -> CleanupCounts:
    total = CleanupCounts()
    for thread_id in thread_ids:
        total += await clear_thread(context_id, thread_id)
    return total


def _memory_adoptions(repo) -> dict[tuple[str, str], tuple[str, str]]:  # noqa: ANN001
    states = getattr(repo, "_thread_adoptions", None)
    if states is None:
        states = {}
        setattr(repo, "_thread_adoptions", states)
    return states


async def _begin_adoption(
    context: SessionContext,
    thread_id: str,
    legacy_owner_id: str,
) -> bool:
    repo = session_context._default_repository
    if repo._pool is None:
        row = repo._context_by_id(context.context_id)
        if (
            row.session_id != context.session_id
            or row.owner_type != context.owner_type
            or row.owner_id != context.owner_id
            or row.generation != context.generation
            or thread_id not in row.threads
        ):
            raise RuntimeError("session thread is not registered")
        states = _memory_adoptions(repo)
        current = states.get((context.context_id, thread_id))
        if current is not None and current[0] == "complete":
            return False
        states[(context.context_id, thread_id)] = ("copying", legacy_owner_id)
        return True
    async with repo._pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                UPDATE chat_session_threads t
                SET adoption_status = CASE
                        WHEN t.adoption_status='complete' THEN 'complete'
                        ELSE 'copying'
                    END,
                    legacy_owner_type = CASE
                        WHEN t.adoption_status='complete' THEN t.legacy_owner_type
                        WHEN %s = c.owner_id THEN c.owner_type
                        ELSE 'guest'
                    END,
                    legacy_owner_id = CASE
                        WHEN t.adoption_status='complete' THEN t.legacy_owner_id
                        ELSE %s
                    END
                FROM chat_session_contexts c
                WHERE t.context_id=c.context_id AND t.context_id=%s AND t.thread_id=%s
                  AND c.session_id=%s AND c.owner_type=%s AND c.owner_id=%s
                  AND c.generation=%s
                RETURNING t.adoption_status
                """,
                (
                    legacy_owner_id,
                    legacy_owner_id,
                    context.context_id,
                    thread_id,
                    context.session_id,
                    context.owner_type,
                    context.owner_id,
                    context.generation,
                ),
            )
        ).fetchone()
        if row is None:
            raise RuntimeError("session thread is not registered")
        status = _column(row, 0, "adoption_status")
        return status != "complete"


async def _complete_adoption(context_id: str, thread_id: str) -> None:
    repo = session_context._default_repository
    if repo._pool is None:
        states = _memory_adoptions(repo)
        status = states.get((context_id, thread_id))
        if status is None:
            raise RuntimeError("adoption was not started")
        states[(context_id, thread_id)] = ("complete", status[1])
        return
    async with repo._pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                UPDATE chat_session_threads
                SET adoption_status='complete'
                WHERE context_id=%s AND thread_id=%s AND adoption_status='copying'
                RETURNING context_id
                """,
                (context_id, thread_id),
            )
        ).fetchone()
        if row is None:
            raise RuntimeError("adoption was not started")


async def _resolve_context_and_legacy_owner(
    context_id: str,
    thread_id: str,
    current_owner_id: str,
) -> tuple[SessionContext, str]:
    repo = session_context._default_repository
    if repo._pool is None:
        row = repo._context_by_id(context_id)
        if thread_id not in row.threads or row.owner_id != current_owner_id:
            raise RuntimeError("session context owner or thread mismatch")
        history = repo._owner_claims.get(row.session_id)
        legacy_owner = (
            history[0]
            if row.owner_type == "member" and history is not None and history[1] == current_owner_id
            else current_owner_id
        )
        return (
            SessionContext(
                row.context_id,
                row.session_id,
                row.owner_type,
                row.owner_id,
                row.generation,
                row.state,
            ),
            legacy_owner,
        )
    async with repo._pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                SELECT c.context_id, c.session_id, c.owner_type, c.owner_id,
                       c.generation, c.state, h.from_owner_id, h.to_owner_id
                FROM chat_session_contexts c
                JOIN chat_session_threads t USING (context_id)
                LEFT JOIN chat_session_owner_claims h USING (context_id, session_id)
                WHERE c.context_id=%s AND t.thread_id=%s AND c.owner_id=%s
                """,
                (context_id, thread_id, current_owner_id),
            )
        ).fetchone()
    if row is None:
        raise RuntimeError("session context owner or thread mismatch")
    context = SessionContext(
        str(_column(row, 0, "context_id")),
        _column(row, 1, "session_id"),
        _column(row, 2, "owner_type"),
        _column(row, 3, "owner_id"),
        int(_column(row, 4, "generation")),
        _column(row, 5, "state"),
    )
    from_owner_id = _column(row, 6, "from_owner_id")
    to_owner_id = _column(row, 7, "to_owner_id")
    legacy_owner = (
        str(from_owner_id)
        if context.owner_type == "member"
        and from_owner_id is not None
        and to_owner_id == current_owner_id
        else current_owner_id
    )
    return context, legacy_owner


async def adopt_legacy_thread(
    context: SessionContext,
    thread_id: str,
    legacy_owner_id: str,
) -> AdoptionResult:
    adoption_key = context_thread_key(context.context_id, thread_id)
    legacy_key = f"{legacy_owner_id}:{thread_id}"
    async with _lock_for(adoption_key):
        try:
            if not await _begin_adoption(context, thread_id, legacy_owner_id):
                return AdoptionResult(False)
            store = await pg_store.get_store()
            scalar_specs = (
                (_LEGACY_FILTER_ROOT, _FILTER_ROOT, _FILTERS_KEY),
                (_LEGACY_CART_ROOT, _CART_ROOT, _PENDING_KEY),
                (_LEGACY_CART_ROOT, _CART_ROOT, _LAST_RECO_KEY),
            )
            intended: list[tuple[str, str, object | None]] = []
            copied = 0
            copy_legacy_names = False
            for legacy_root, target_root, name in scalar_specs:
                target = await _item(store, target_root, adoption_key, name)
                legacy = await _item(store, legacy_root, legacy_key, name)
                value = (
                    target.value
                    if target is not None
                    else (legacy.value if legacy is not None else None)
                )
                if target is None and legacy is not None:
                    await run_with_query_timeout(
                        store.aput((target_root, adoption_key), name, legacy.value)
                    )
                    copied += 1
                    copy_legacy_names = name == _LAST_RECO_KEY
                intended.append((target_root, name, value))
            target_revert = await _item(store, _REVERT_ROOT, adoption_key, _CATEGORIES_KEY)
            legacy_revert = await _item(store, _LEGACY_REVERT_ROOT, legacy_key, _CATEGORIES_KEY)
            categories = sorted(
                set(target_revert.value[_CATEGORIES_KEY] if target_revert else ())
                | set(legacy_revert.value[_CATEGORIES_KEY] if legacy_revert else ())
            )
            revert_value = {_CATEGORIES_KEY: categories} if categories else None
            if revert_value is not None and (
                target_revert is None or target_revert.value != revert_value
            ):
                await run_with_query_timeout(
                    store.aput(
                        (_REVERT_ROOT, adoption_key),
                        _CATEGORIES_KEY,
                        revert_value,
                    )
                )
                copied += 1
            intended.append((_REVERT_ROOT, _CATEGORIES_KEY, revert_value))
            if copy_legacy_names:
                names = cart_state._last_reco_names.get(legacy_key)
                if names is not None:
                    cart_state._last_reco_names[adoption_key] = dict(names)
            for target_root, name, expected in intended:
                actual = await _item(store, target_root, adoption_key, name)
                if (actual.value if actual is not None else None) != expected:
                    raise RuntimeError(f"adoption verification failed for {target_root}/{name}")
            deleted = 0
            for legacy_root, _, name in scalar_specs:
                deleted += await _delete(store, legacy_root, legacy_key, name)
            deleted += await _delete(store, _LEGACY_REVERT_ROOT, legacy_key, _CATEGORIES_KEY)
            cart_state._last_reco_names.pop(legacy_key)
            await _complete_adoption(context.context_id, thread_id)
            return AdoptionResult(True, copied, deleted)
        except SessionStateUnavailable:
            raise
        except Exception as exc:
            raise SessionStateUnavailable from exc


async def ensure_thread_adopted(
    context_id: str,
    thread_id: str,
    current_owner_id: str,
) -> AdoptionResult:
    try:
        context, legacy_owner = await _resolve_context_and_legacy_owner(
            context_id, thread_id, current_owner_id
        )
        return await adopt_legacy_thread(context, thread_id, legacy_owner)
    except SessionStateUnavailable:
        raise
    except Exception as exc:
        raise SessionStateUnavailable from exc


async def run_legacy_gc_batch() -> int:
    """Delete one bounded first page per legacy root after the durable grace deadline."""
    repo = session_context._default_repository
    if repo._pool is None:
        return 0
    settings = session_context.get_settings()
    migration_name = "issue-187-session-context"
    async with repo._pool.connection() as conn:
        migration = await (
            await conn.execute(
                """
                SELECT gc_completed_at, profile_backfill_completed_at,
                       grace_deadline <= now() AS grace_elapsed
                FROM chat_session_migrations
                WHERE migration_name=%s
                """,
                (migration_name,),
            )
        ).fetchone()
    if migration is None or migration[1] is None or not migration[2]:
        return 0
    if migration[0] is not None:
        return 0

    store = await pg_store.get_store()
    roots = (
        (_LEGACY_FILTER_ROOT, "filters_deleted"),
        (_LEGACY_CART_ROOT, "cart_deleted"),
        (_LEGACY_REVERT_ROOT, "revert_deleted"),
    )
    deleted_total = 0
    all_empty = True
    for root, column in roots:
        deleted, empty, deleted_keys = await _delete_legacy_root_page(
            repo,
            store,
            root,
            column,
            settings.session_lifecycle_gc_batch_size,
        )
        deleted_total += deleted
        all_empty = all_empty and empty
        if root == _LEGACY_CART_ROOT:
            for key in deleted_keys:
                cart_state._last_reco_names.pop(key)
        if deleted and getattr(store, "conn", None) is None:
            async with repo._pool.connection() as conn:
                await conn.execute(
                    f"UPDATE chat_session_migrations SET {column}={column}+%s, "
                    "updated_at=now() WHERE migration_name=%s",
                    (deleted, migration_name),
                )

    # Quarantined profile rows have no authoritative owner. Their raw identifiers stay
    # in durable rows only and are never emitted in logs.
    async with repo._pool.connection() as conn:
        conflicts = await (
            await conn.execute(
                """
                SELECT conflict_id, session_id, owner_id
                FROM chat_session_migration_conflicts
                WHERE resolution_status='quarantined'
                ORDER BY conflict_id
                LIMIT %s
                """,
                (settings.session_lifecycle_gc_batch_size,),
            )
        ).fetchall()
    for conflict_id, session_id, owner_id in conflicts:
        async with repo.lock_session(session_id) as uow:
            conflict = await (
                await uow.conn.execute(
                    "SELECT resolution_status FROM chat_session_migration_conflicts "
                    "WHERE conflict_id=%s FOR UPDATE",
                    (conflict_id,),
                )
            ).fetchone()
            if conflict is None or conflict[0] != "quarantined":
                continue
            context = await (
                await uow.conn.execute(
                    "SELECT context_id, owner_type, owner_id "
                    "FROM chat_session_contexts WHERE session_id=%s FOR UPDATE",
                    (session_id,),
                )
            ).fetchone()
            if context is not None and context[1] == "member" and str(context[2]) == str(owner_id):
                await uow.conn.execute(
                    """
                    UPDATE chat_session_migration_conflicts
                    SET resolution_status='resolved', resolved_context_id=%s,
                        updated_at=now()
                    WHERE conflict_id=%s AND resolution_status='quarantined'
                    """,
                    (context[0], conflict_id),
                )
                continue
            key = f"{owner_id}:{session_id}"
            await _delete(store, "session_ctx", key, "buffer")
            await uow.conn.execute(
                "DELETE FROM profile_session_activity WHERE user_id=%s AND session_id=%s",
                (int(owner_id), session_id),
            )
            await uow.conn.execute(
                """
                UPDATE chat_session_migration_conflicts
                SET resolution_status='discarded',
                    profile_buffer_discarded_at=COALESCE(
                        profile_buffer_discarded_at, now()
                    ),
                    updated_at=now()
                WHERE conflict_id=%s AND resolution_status='quarantined'
                """,
                (conflict_id,),
            )
            deleted_total += 1
    async with repo._pool.connection() as conn:
        deleted_activity = await (
            await conn.execute(
                """
                WITH page AS (
                    SELECT a.ctid
                    FROM profile_session_activity a
                    WHERE EXISTS (
                        SELECT 1 FROM chat_session_contexts c
                        WHERE c.session_id=a.session_id
                    )
                       OR EXISTS (
                        SELECT 1 FROM chat_session_migration_conflicts q
                        WHERE q.session_id=a.session_id
                          AND q.owner_id=a.user_id::text
                    )
                    ORDER BY user_id, session_id
                    LIMIT %s
                )
                DELETE FROM profile_session_activity a
                USING page
                WHERE a.ctid=page.ctid
                RETURNING a.user_id
                """,
                (settings.session_lifecycle_gc_batch_size,),
            )
        ).fetchall()
        deleted_total += len(deleted_activity)
        remaining_activity = await (
            await conn.execute("SELECT 1 FROM profile_session_activity LIMIT 1")
        ).fetchone()
        remaining_conflict = await (
            await conn.execute(
                "SELECT 1 FROM chat_session_migration_conflicts "
                "WHERE resolution_status='quarantined' LIMIT 1"
            )
        ).fetchone()
        if all_empty and remaining_conflict is None and remaining_activity is None:
            # Fresh emptiness was observed after deletes; only now close the GC gate.
            await conn.execute(
                """
                UPDATE chat_session_migrations
                SET gc_completed_at=COALESCE(gc_completed_at, now()), updated_at=now()
                WHERE migration_name=%s
                """,
                (migration_name,),
            )
    return deleted_total


async def get_legacy_gc_counters() -> tuple[int, int, int]:
    """Return durable legacy-root deletion counters without exposing raw identifiers."""
    repo = session_context._default_repository
    if repo._pool is None:
        return (0, 0, 0)
    async with repo._pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                SELECT filters_deleted, cart_deleted, revert_deleted
                FROM chat_session_migrations
                WHERE migration_name=%s
                """,
                ("issue-187-session-context",),
            )
        ).fetchone()
    return tuple(int(value) for value in row) if row is not None else (0, 0, 0)
