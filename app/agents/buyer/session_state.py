"""Buyer transient state adoption and context-scoped cleanup."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from weakref import WeakValueDictionary

from app.agents.buyer.cart import state as cart_state
from app.core import pg_store, session_context
from app.core.pg_resilience import is_state_store_unavailable, run_with_query_timeout
from app.core.session_context import SessionContext, SessionForbidden, SessionStateUnavailable

_LEGACY_FILTER_ROOT = "buyer_thread_filters"
_LEGACY_CART_ROOT = "buyer_cart"
_LEGACY_REVERT_ROOT = "buyer_revert"
_FILTER_ROOT = "buyer_thread_filters_v2"
_CART_ROOT = "buyer_cart_v2"
_REVERT_ROOT = "buyer_revert_v2"
_REPURCHASE_ROOT = "buyer_repurchase_v1"
_RELAX_ROOT = "buyer_relaxation_offers_v1"
_FILTERS_KEY = "filters"
_PENDING_KEY = "pending"
_LAST_RECO_KEY = "last_reco"
_CATEGORIES_KEY = "categories"
_PRODUCT_IDS_KEY = "product_ids"
# RelaxationOfferStore 는 offers·applied 를 한 덩어리로 이 키 하나에 쓴다(#113) — 정리도 1회다.
_SNAPSHOT_KEY = "turn"

_adoption_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_legacy_root_memory_fence = asyncio.Lock()
_LEGACY_ROOT_FENCE_KEY = "migration:buyer-legacy-roots"

# adoption_status='complete' 는 종단 상태다 — _begin_adoption 의 CASE 가 complete 를
# 다른 값으로 되돌리지 않는다. 그래서 완료를 한 번 관측한 (context_id, thread_id) 는
# 전역 fence 를 다시 잡지 않는다. 이 fence 는 고정 키 하나(_LEGACY_ROOT_FENCE_KEY)의
# 블로킹 advisory lock 이라, 캐시가 없으면 마이그레이션이 끝난 뒤에도 서로 무관한
# 사용자·세션·스레드의 모든 buyer 턴이 이 락 하나에서 직렬화된다.
# 캐시 미스는 기존 동작(락 획득)으로 떨어지므로 안전 방향으로만 틀린다.
_ADOPTED_CACHE_MAX = 8192
_adopted_threads: OrderedDict[tuple[str, str], None] = OrderedDict()


def _mark_adopted(key: tuple[str, str]) -> None:
    _adopted_threads[key] = None
    _adopted_threads.move_to_end(key)
    while len(_adopted_threads) > _ADOPTED_CACHE_MAX:
        _adopted_threads.popitem(last=False)


@dataclass(frozen=True)
class CleanupCounts:
    filters: int = 0
    pending: int = 0
    last_recommendation: int = 0
    local_names: int = 0
    revert: int = 0
    repurchase: int = 0
    relaxation_offers: int = 0

    def __add__(self, other: "CleanupCounts") -> "CleanupCounts":
        return CleanupCounts(
            self.filters + other.filters,
            self.pending + other.pending,
            self.last_recommendation + other.last_recommendation,
            self.local_names + other.local_names,
            self.revert + other.revert,
            self.repurchase + other.repurchase,
            self.relaxation_offers + other.relaxation_offers,
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
    *,
    fence_conn=None,  # noqa: ANN001
) -> tuple[int, bool, list[str]]:
    """Atomically delete/count a PostgreSQL page; retain a bounded generic fallback."""
    if getattr(store, "conn", None) is not None:

        async def delete_page(conn):  # noqa: ANN001
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
            return deleted, remaining

        if fence_conn is None:
            async with repo._pool.connection() as conn:
                async with conn.transaction():
                    deleted, remaining = await delete_page(conn)
        else:
            deleted, remaining = await delete_page(fence_conn)
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


@asynccontextmanager
async def _legacy_root_fence(repo, *, transaction_scoped: bool = True):  # noqa: ANN001
    """Serialize adoption and root GC.

    Adoption takes per-thread process lock -> global legacy-root fence. GC takes
    root fence -> migration advisory -> activity SHARE lock -> non-blocking session
    advisory locks; exact-prefix store triggers provide root-writer evidence.
    """
    if repo._pool is None:
        async with _legacy_root_memory_fence:
            yield None
        return
    async with repo._pool.connection() as conn:
        if transaction_scoped:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (_LEGACY_ROOT_FENCE_KEY,),
                )
                yield conn
            return
        await conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (_LEGACY_ROOT_FENCE_KEY,),
        )
        await conn.commit()
        try:
            yield conn
        finally:
            cleanup = asyncio.create_task(_release_legacy_root_fence(conn))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                try:
                    await cleanup
                except BaseException:
                    await _close_fence_connection(conn)
                raise
            except BaseException:
                await _close_fence_connection(conn)
                raise


async def _unlock_legacy_root_fence(conn) -> bool:  # noqa: ANN001
    row = await (
        await conn.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (_LEGACY_ROOT_FENCE_KEY,),
        )
    ).fetchone()
    return row is not None and bool(_column(row, 0, "pg_advisory_unlock"))


async def _release_legacy_root_fence(conn) -> None:  # noqa: ANN001
    await conn.rollback()
    if not await _unlock_legacy_root_fence(conn):
        raise RuntimeError("legacy root advisory unlock was not confirmed")
    await conn.commit()


async def _close_fence_connection(conn) -> None:  # noqa: ANN001
    close = asyncio.create_task(conn.close())
    try:
        await asyncio.shield(close)
    except asyncio.CancelledError:
        await close
        raise


async def clear_thread(context_id: str, thread_id: str) -> CleanupCounts:
    store = await pg_store.get_store()
    key = context_thread_key(context_id, thread_id)
    filters = await _delete(store, _FILTER_ROOT, key, _FILTERS_KEY)
    pending = await _delete(store, _CART_ROOT, key, _PENDING_KEY)
    last_recommendation = await _delete(store, _CART_ROOT, key, _LAST_RECO_KEY)
    revert = await _delete(store, _REVERT_ROOT, key, _CATEGORIES_KEY)
    repurchase = await _delete(store, _REPURCHASE_ROOT, key, _PRODUCT_IDS_KEY)
    relaxation_offers = await _delete(store, _RELAX_ROOT, key, _SNAPSHOT_KEY)
    local_names = int(cart_state._last_reco_names.pop(key) is not None)
    return CleanupCounts(
        filters,
        pending,
        last_recommendation,
        local_names,
        revert,
        repurchase,
        relaxation_offers,
    )


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
    conn=None,  # noqa: ANN001
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

    async def begin(connection):  # noqa: ANN001
        row = await (
            await connection.execute(
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

    if conn is not None:
        return await begin(conn)
    async with repo._pool.connection() as connection:
        return await begin(connection)


async def _complete_adoption(context_id: str, thread_id: str, conn=None) -> None:  # noqa: ANN001
    repo = session_context._default_repository
    if repo._pool is None:
        states = _memory_adoptions(repo)
        status = states.get((context_id, thread_id))
        if status is None:
            raise RuntimeError("adoption was not started")
        states[(context_id, thread_id)] = ("complete", status[1])
        return

    async def complete(connection):  # noqa: ANN001
        row = await (
            await connection.execute(
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

    if conn is not None:
        await complete(conn)
        return
    async with repo._pool.connection() as connection:
        await complete(connection)


async def _resolve_context_and_legacy_owner(
    context_id: str,
    thread_id: str,
    current_owner_id: str,
) -> tuple[SessionContext, str]:
    repo = session_context._default_repository
    if repo._pool is None:
        row = repo._context_by_id(context_id)
        if thread_id not in row.threads or row.owner_id != current_owner_id:
            raise SessionForbidden(
                # owner/thread 불일치는 신원 경계 위반이라 403 SESSION_FORBIDDEN 이다
                # (SPEC-CHAT-SESSION-CONTEXT-187 §5). RuntimeError 로 던지면
                # errors.py 의 _SESSION_ERROR_MAP 을 못 타고 500 INTERNAL 로 나간다.
                "session context owner or thread mismatch"
            )
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
        raise SessionForbidden(
            # owner/thread 불일치는 신원 경계 위반이라 403 SESSION_FORBIDDEN 이다
            # (SPEC-CHAT-SESSION-CONTEXT-187 §5). RuntimeError 로 던지면
            # errors.py 의 _SESSION_ERROR_MAP 을 못 타고 500 INTERNAL 로 나간다.
            "session context owner or thread mismatch"
        )
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
    cache_key = (context.context_id, thread_id)
    if cache_key in _adopted_threads:
        _adopted_threads.move_to_end(cache_key)
        return AdoptionResult(False)
    async with _lock_for(adoption_key):
        try:
            async with _legacy_root_fence(
                session_context._default_repository, transaction_scoped=False
            ) as fence_conn:
                if not await _begin_adoption(context, thread_id, legacy_owner_id, fence_conn):
                    _mark_adopted(cache_key)
                    return AdoptionResult(False)
                if fence_conn is not None:
                    await fence_conn.commit()
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
                await _complete_adoption(context.context_id, thread_id, fence_conn)
                if fence_conn is not None:
                    await fence_conn.commit()
                return AdoptionResult(True, copied, deleted)
        except SessionStateUnavailable:
            raise
        except Exception as exc:
            if is_state_store_unavailable(exc):
                raise SessionStateUnavailable from exc
            raise


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
        if is_state_store_unavailable(exc):
            raise SessionStateUnavailable from exc
        raise


async def run_legacy_gc_batch() -> int:
    """Delete one bounded legacy page behind one database-enforced writer barrier.

    Lock order is legacy-root fence -> migration advisory lock -> activity SHARE lock
    -> non-blocking session advisory locks. Backfill takes migration -> session;
    runtime touch/claim takes session only. Root-specific store triggers reopen the
    marker, so unrelated store DML never needs a table-wide lock.
    """
    repo = session_context._default_repository
    if repo._pool is None:
        return 0
    settings = session_context.get_settings()
    migration_name = "issue-187-session-context"
    async with repo._pool.connection() as conn:
        completed = await (
            await conn.execute(
                "SELECT gc_completed_at FROM chat_session_migrations WHERE migration_name=%s",
                (migration_name,),
            )
        ).fetchone()
    if completed is not None and completed[0] is not None:
        return 0

    store = await pg_store.get_store()
    roots = (
        (_LEGACY_FILTER_ROOT, "filters_deleted"),
        (_LEGACY_CART_ROOT, "cart_deleted"),
        (_LEGACY_REVERT_ROOT, "revert_deleted"),
    )
    deleted_total = 0
    reopened = False
    cart_keys_to_drop: list[str] = []

    async with _legacy_root_fence(repo) as fence_conn:
        await fence_conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("migration:issue-187-session-context",),
        )
        await fence_conn.execute("LOCK TABLE profile_session_activity IN SHARE MODE")

        migration = await (
            await fence_conn.execute(
                """
                SELECT gc_completed_at, profile_backfill_completed_at,
                       grace_deadline <= now() AS grace_elapsed,
                       legacy_quiet_until IS NULL
                           OR legacy_quiet_until <= now() AS quiet_elapsed,
                       conflict_gc_cursor
                FROM chat_session_migrations
                WHERE migration_name=%s
                FOR UPDATE
                """,
                (migration_name,),
            )
        ).fetchone()
        if migration is None or migration[1] is None or not migration[2] or not migration[3]:
            return 0
        if migration[0] is not None:
            return 0

        if await session_context._find_actionable_legacy_activity(fence_conn) is not None:
            await fence_conn.execute(
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
            reopened = True
        else:
            all_empty = True
            for root, column in roots:
                deleted, empty, deleted_keys = await _delete_legacy_root_page(
                    repo,
                    store,
                    root,
                    column,
                    settings.session_lifecycle_gc_batch_size,
                    fence_conn=fence_conn,
                )
                deleted_total += deleted
                all_empty = all_empty and empty
                if root == _LEGACY_CART_ROOT:
                    cart_keys_to_drop.extend(deleted_keys)
                if deleted and getattr(store, "conn", None) is None:
                    await fence_conn.execute(
                        f"UPDATE chat_session_migrations SET {column}={column}+%s, "
                        "updated_at=now() WHERE migration_name=%s",
                        (deleted, migration_name),
                    )

            conflict_cursor = int(migration[4])
            max_examined = max(
                settings.session_lifecycle_gc_batch_size + 1,
                settings.session_lifecycle_gc_batch_size * 10,
            )

            async def conflict_page(after_id: int) -> list:  # noqa: ANN001
                return await (
                    await fence_conn.execute(
                        """
                        SELECT conflict_id, session_id, owner_id
                        FROM chat_session_migration_conflicts
                        WHERE resolution_status='quarantined' AND conflict_id > %s
                        ORDER BY conflict_id
                        LIMIT %s
                        """,
                        (after_id, max_examined),
                    )
                ).fetchall()

            conflicts = await conflict_page(conflict_cursor)
            if not conflicts and conflict_cursor:
                conflict_cursor = 0
                conflicts = await conflict_page(conflict_cursor)
            processed_conflicts = 0
            for conflict_id, session_id, owner_id in conflicts:
                conflict_cursor = int(conflict_id)
                if not await _try_session_lock_for_gc(fence_conn, session_id):
                    continue
                context = await (
                    await fence_conn.execute(
                        "SELECT context_id, owner_type, owner_id, authority_source "
                        "FROM chat_session_contexts WHERE session_id=%s FOR UPDATE",
                        (session_id,),
                    )
                ).fetchone()
                conflict = await (
                    await fence_conn.execute(
                        "SELECT resolution_status FROM chat_session_migration_conflicts "
                        "WHERE conflict_id=%s FOR UPDATE",
                        (conflict_id,),
                    )
                ).fetchone()
                if conflict is None or conflict[0] != "quarantined":
                    continue
                if (
                    context is not None
                    and context[1] == "member"
                    and str(context[2]) == str(owner_id)
                    and context[3] == "runtime"
                ):
                    await fence_conn.execute(
                        """
                        UPDATE chat_session_migration_conflicts
                        SET resolution_status='resolved', resolved_context_id=%s,
                            updated_at=now()
                        WHERE conflict_id=%s AND resolution_status='quarantined'
                        """,
                        (context[0], conflict_id),
                    )
                    processed_conflicts += 1
                    if processed_conflicts >= settings.session_lifecycle_gc_batch_size:
                        break
                    continue
                key = f"{owner_id}:{session_id}"
                if getattr(store, "conn", None) is not None:
                    await fence_conn.execute(
                        "DELETE FROM store WHERE prefix=%s AND key=%s",
                        (f"session_ctx.{key}", "buffer"),
                    )
                else:
                    await _delete(store, "session_ctx", key, "buffer")
                await fence_conn.execute(
                    "DELETE FROM profile_session_activity WHERE user_id=%s AND session_id=%s",
                    (int(owner_id), session_id),
                )
                await fence_conn.execute(
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
                processed_conflicts += 1
                if processed_conflicts >= settings.session_lifecycle_gc_batch_size:
                    break
            await fence_conn.execute(
                "UPDATE chat_session_migrations "
                "SET conflict_gc_cursor=%s, updated_at=now() "
                "WHERE migration_name=%s",
                (conflict_cursor, migration_name),
            )

            deleted_activity = await (
                await fence_conn.execute(
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
                await fence_conn.execute("SELECT 1 FROM profile_session_activity LIMIT 1")
            ).fetchone()
            remaining_conflict = await (
                await fence_conn.execute(
                    "SELECT 1 FROM chat_session_migration_conflicts "
                    "WHERE resolution_status='quarantined' LIMIT 1"
                )
            ).fetchone()
            if all_empty and remaining_conflict is None and remaining_activity is None:
                await _mark_legacy_gc_completed(fence_conn, migration_name)

    if reopened:
        await repo.backfill_legacy_activity()
        return 0
    for key in cart_keys_to_drop:
        cart_state._last_reco_names.pop(key)
    return deleted_total


async def _try_session_lock_for_gc(conn, session_id: str) -> bool:  # noqa: ANN001
    row = await (
        await conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended('chat-session:' || %s, 0))",
            (session_id,),
        )
    ).fetchone()
    return row is not None and bool(row[0])


async def _mark_legacy_gc_completed(conn, migration_name: str) -> None:  # noqa: ANN001
    await conn.execute(
        """
        UPDATE chat_session_migrations
        SET gc_completed_at=COALESCE(gc_completed_at, now()), updated_at=now()
        WHERE migration_name=%s
        """,
        (migration_name,),
    )


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
