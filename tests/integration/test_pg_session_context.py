from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import pytest
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg.conninfo import make_conninfo
from psycopg.errors import CheckViolation, ForeignKeyViolation
from psycopg_pool import AsyncConnectionPool

from app.agents.buyer.cart import state as cart_state
from app.agents.buyer import session_state as buyer_session_state
from app.agents.buyer.session_state import context_thread_key
from app.agents.profile import finalizer as profile_finalizer
from app.agents.profile.store import get_profile_store
from app.core import pg_store as pg_store_module
from app.core import session_context as session_context_module
from app.core import session_lifecycle as session_lifecycle_module
from app.core.config import get_settings
from app.core.conversation import conversation_key
from app.core.session_context import (
    BuyerSessionInput,
    FinalizationClaim,
    SessionClaimConflict,
    SessionContextRepository,
    SessionContextUnitOfWork,
    SessionFinalizing,
    SessionForbidden,
    SessionStateUnavailable,
)
from app.core.session_lifecycle import SessionLifecycleCoordinator
from app.core.stream import ActiveStreamRegistry

pytestmark = pytest.mark.integration


class _ProfileLLM:
    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        if "델타 추출기" in system:
            return json.dumps(
                {
                    "deltas": [
                        {
                            "fact": "PG 복구 취향",
                            "salience": 0.9,
                            "explicit": True,
                            "repetitionEma": 0.0,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        return "# 취향\n- PG 복구 취향"

    async def stream(self, **kwargs):
        yield "x"


class _FailAfterFirstDeleteStore:
    def __init__(self, store) -> None:  # noqa: ANN001
        self.store = store
        self.delete_calls = 0

    def __getattr__(self, name: str):
        return getattr(self.store, name)

    async def adelete(self, namespace, key) -> None:  # noqa: ANN001
        self.delete_calls += 1
        if self.delete_calls == 2:
            raise RuntimeError("fault after first namespace delete")
        await self.store.adelete(namespace, key)


class _FaultAfterExecuteConnection:
    def __init__(self, conn, predicate) -> None:  # noqa: ANN001
        self._conn = conn
        self._predicate = predicate
        self.failed = False

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    async def execute(self, query, params=None, **kwargs):  # noqa: ANN001
        result = await self._conn.execute(query, params, **kwargs)
        if not self.failed and self._predicate(str(query)):
            self.failed = True
            raise RuntimeError("injected transaction fault")
        return result


class _FaultPool:
    def __init__(self, pool, predicate) -> None:  # noqa: ANN001
        self._pool = pool
        self._predicate = predicate
        self.connections: list[_FaultAfterExecuteConnection] = []

    @asynccontextmanager
    async def connection(self):
        async with self._pool.connection() as conn:
            wrapped = _FaultAfterExecuteConnection(conn, self._predicate)
            self.connections.append(wrapped)
            yield wrapped


class _CommitFaultTransaction:
    def __init__(self, conn, transaction) -> None:  # noqa: ANN001
        self._conn = conn
        self._transaction = transaction

    async def __aenter__(self):
        return await self._transaction.__aenter__()

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            return await self._transaction.__aexit__(exc_type, exc, traceback)
        await self._conn.execute(
            """
            CREATE TEMP TABLE commit_fault_parent (
                id integer PRIMARY KEY
            ) ON COMMIT DROP
            """
        )
        await self._conn.execute(
            """
            CREATE TEMP TABLE commit_fault_child (
                parent_id integer REFERENCES commit_fault_parent(id)
                    DEFERRABLE INITIALLY DEFERRED
            ) ON COMMIT DROP
            """
        )
        await self._conn.execute("INSERT INTO commit_fault_child VALUES (1)")
        return await self._transaction.__aexit__(exc_type, exc, traceback)


class _CommitFaultConnection:
    def __init__(self, conn) -> None:  # noqa: ANN001
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def transaction(self):
        return _CommitFaultTransaction(self._conn, self._conn.transaction())


class _CommitFaultPool:
    def __init__(self, pool) -> None:  # noqa: ANN001
        self._pool = pool

    @asynccontextmanager
    async def connection(self):
        async with self._pool.connection() as conn:
            yield _CommitFaultConnection(conn)


_SWEEP_BATCH = 100


_SWEEP_MAX_PAGES = 50


_RESIDUE_STALE_AFTER = "1 hour"
_residue_purged = False


async def _delete_stale_residue(conn) -> None:  # noqa: ANN001
    """죽은 통합 실행이 남긴 `it-*` 컨텍스트를 지운다 (#220).

    sweep 은 테이블 전역을 훑으므로 잔재가 쌓일수록 `_claim_own` 이 넘겨야 할 페이지가
    늘어난다. 잔재는 단조 증가하니 주기적으로 끊어준다.

    유휴 1시간을 넘긴 행만 지운다 — 같은 DB 를 쓰는 다른 worktree 가 지금 돌고 있어도
    그쪽 살아있는 컨텍스트는 건드리지 않는다.
    """
    await conn.execute(
        "DELETE FROM chat_session_contexts "
        "WHERE session_id LIKE 'it-%%' "
        f"AND last_activity_at <= now() - interval '{_RESIDUE_STALE_AFTER}'"
    )


async def _purge_stale_residue_once(pool) -> None:  # noqa: ANN001
    """세션당 한 번만 잔재를 정리한다.

    별도 세션 스코프 픽스처를 두면 풀이 자기 이벤트 루프 밖에서 닫히는 문제(#208)를
    다시 밟는다. `pg_repo` 자신의 풀에서 돌려 그 함정을 피한다.
    """
    global _residue_purged
    if _residue_purged:
        return
    _residue_purged = True
    async with pool.connection() as conn:
        await _delete_stale_residue(conn)


async def _claim_own_many(
    repo: SessionContextRepository,
    session_ids: list[str],
    idle_timeout_s: float = 10,
    lease_s: float = 30,
) -> dict[str, FinalizationClaim]:
    """전역 sweep 을 페이지 단위로 넘기며 요청한 세션들의 claim 을 모은다.

    `claim_expired_contexts` 는 `chat_session_contexts` **전역**을 last_activity_at
    오름차순으로 batch 만큼 claim 한다. 다른 worktree·이전 실행이 남긴 만료 잔재가
    batch 를 넘으면 자기 행이 batch 밖으로 밀려 결과가 비므로, batch 를 키우는 것으로는
    막을 수 없다(#220).

    claim 된 행은 lease 가 살아 있는 동안 다음 호출의 후보에서 빠지므로, 요청한 세션을
    모두 만날 때까지 페이지를 넘긴다. 잔재량과 무관하게 결정적이다.
    """
    wanted = set(session_ids)
    found: dict[str, FinalizationClaim] = {}
    for _ in range(_SWEEP_MAX_PAGES):
        if wanted <= found.keys():
            break
        claims = await repo.claim_expired_contexts(idle_timeout_s, lease_s, _SWEEP_BATCH)
        if not claims:
            break
        for claim in claims:
            if claim.session_id in wanted:
                found.setdefault(claim.session_id, claim)
    missing = wanted - found.keys()
    if missing:
        pytest.fail(f"만료 claim 페이지를 모두 넘겼지만 찾지 못한 세션: {sorted(missing)}")
    return found


async def _claim_own(
    repo: SessionContextRepository,
    session_id: str,
    idle_timeout_s: float = 10,
    lease_s: float = 30,
) -> FinalizationClaim:
    """전역 sweep 결과에서 자기 세션이 만든 claim 하나를 찾는다(#220)."""
    found = await _claim_own_many(repo, [session_id], idle_timeout_s, lease_s)
    return found[session_id]


async def _drain_claims(
    repo: SessionContextRepository,
    idle_timeout_s: float = 10,
    lease_s: float = 30,
) -> list[FinalizationClaim]:
    """전역 만료 후보를 페이지 끝까지 claim 해 전부 모은다(#220).

    "우리 세션이 후보에 없다"는 부정 단언은 자기 행이 batch 밖으로 밀려도 통과해버려
    거짓 음성이 된다. 끝까지 훑어야 단언이 실제 의미를 갖는다.
    """
    drained: list[FinalizationClaim] = []
    for _ in range(_SWEEP_MAX_PAGES):
        claims = await repo.claim_expired_contexts(idle_timeout_s, lease_s, _SWEEP_BATCH)
        if not claims:
            break
        drained.extend(claims)
    return drained


async def _seed_v2_state(store, key: str, label: str) -> None:  # noqa: ANN001
    await store.aput(("buyer_thread_filters_v2", key), "filters", {"category": label})
    await store.aput(
        ("buyer_cart_v2", key),
        "pending",
        {"product_id": 1, "quantity": 1, "options": [], "attempts": 0},
    )
    await store.aput(("buyer_cart_v2", key), "last_reco", {"product_ids": [1]})
    await store.aput(
        ("buyer_revert_v2", key),
        "categories",
        {"categories": [label]},
    )
    cart_state._last_reco_names[key] = {1: label}


async def _delete_seeded_v2_state(store, key: str) -> None:  # noqa: ANN001
    for root, name in (
        ("buyer_thread_filters_v2", "filters"),
        ("buyer_cart_v2", "pending"),
        ("buyer_cart_v2", "last_reco"),
        ("buyer_revert_v2", "categories"),
    ):
        await store.adelete((root, key), name)
    cart_state._last_reco_names.pop(key)


async def _assert_v2_deleted(store, key: str) -> None:  # noqa: ANN001
    assert await store.aget(("buyer_thread_filters_v2", key), "filters") is None
    assert await store.aget(("buyer_cart_v2", key), "pending") is None
    assert await store.aget(("buyer_cart_v2", key), "last_reco") is None
    assert await store.aget(("buyer_revert_v2", key), "categories") is None
    assert cart_state._last_reco_names.get(key) is None


async def _assert_v2_preserved(store, key: str) -> None:  # noqa: ANN001
    assert await store.aget(("buyer_thread_filters_v2", key), "filters") is not None
    assert await store.aget(("buyer_cart_v2", key), "pending") is not None
    assert await store.aget(("buyer_cart_v2", key), "last_reco") is not None
    assert await store.aget(("buyer_revert_v2", key), "categories") is not None
    assert cart_state._last_reco_names.get(key) is not None


class _EmptyProfile:
    async def get_session_ctx_snapshot(self, key: str) -> tuple[list[str], int]:
        return [], 0


@pytest.fixture
async def pg_repo():
    pool = AsyncConnectionPool(get_settings().profile_db_url, open=False, min_size=1, max_size=4)
    await pool.open(wait=True)
    repo = SessionContextRepository(pool=pool)
    await repo.initialize()
    await _purge_stale_residue_once(pool)
    prefix = f"it-{uuid.uuid4().hex}"
    try:
        yield repo, pool, prefix
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM chat_session_contexts WHERE session_id LIKE %s", (prefix + "%",)
            )
        await pool.close()


async def _expire_legacy_quiet(pool) -> None:  # noqa: ANN001
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_migrations "
            "SET legacy_quiet_until=now()-interval '1 second' "
            "WHERE migration_name='issue-187-session-context'"
        )


async def test_concurrent_first_messages_share_global_context(pg_repo) -> None:
    repo, _, prefix = pg_repo
    session_id = prefix + "-same"
    request = BuyerSessionInput(session_id, "T1", "guest", "G1")
    a, b = await asyncio.gather(repo.touch(request), repo.touch(request))
    assert a.context_id == b.context_id


async def test_touch_invalidates_idle_claim_and_owner_claim_records_history(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-claim"
    before = await repo.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at = now() - interval '1 hour' "
            "WHERE context_id = %s",
            (before.context_id,),
        )
    idle = await _claim_own(repo, session_id)
    after = await repo.touch(BuyerSessionInput(session_id, "T2", "guest", "G1"))
    assert after.generation == idle.generation + 1
    outcome = await repo.claim_owner(session_id, "G1", 7)
    assert outcome.context.owner_id == "7"
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT from_owner_id, to_owner_id FROM chat_session_owner_claims "
                "WHERE session_id = %s",
                (session_id,),
            )
        ).fetchone()
    assert row == ("G1", "7")


async def test_own_claim_found_when_residue_exceeds_sweep_batch(pg_repo) -> None:
    """전역 만료 후보가 sweep batch 를 넘어도 자기 세션 claim 을 찾아야 한다 (#220).

    잔재를 자기 행보다 오래된 것으로 만들어(sweep 은 last_activity_at 오름차순)
    자기 행이 batch 밖으로 밀리는 상황을 결정적으로 만든다.
    """
    repo, pool, prefix = pg_repo
    for index in range(_SWEEP_BATCH + 20):
        await repo.touch(BuyerSessionInput(f"{prefix}-residue-{index}", "T1", "guest", "G1"))
    session_id = prefix + "-own"
    own = await repo.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at = now() - interval '1 hour' "
            "WHERE session_id LIKE %s",
            (prefix + "-residue-%",),
        )
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at = now() - interval '30 minutes' "
            "WHERE context_id = %s",
            (own.context_id,),
        )
    claim = await _claim_own(repo, session_id)
    assert claim.context_id == own.context_id


async def test_stale_residue_purge_spares_live_rows(pg_repo) -> None:
    """죽은 실행의 it-* 잔재만 지우고, 동시 실행 중인 행은 남긴다 (#220)."""
    repo, pool, prefix = pg_repo
    stale = await repo.touch(BuyerSessionInput(prefix + "-stale", "T1", "guest", "G1"))
    live = await repo.touch(BuyerSessionInput(prefix + "-live", "T1", "guest", "G1"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at = now() - interval '2 hours' "
            "WHERE context_id = %s",
            (stale.context_id,),
        )
        await _delete_stale_residue(conn)
        remaining = await (
            await conn.execute(
                "SELECT context_id FROM chat_session_contexts WHERE session_id LIKE %s",
                (prefix + "%",),
            )
        ).fetchall()
    assert [str(row[0]) for row in remaining] == [live.context_id]


async def test_multiple_own_claims_found_when_residue_exceeds_sweep_batch(pg_repo) -> None:
    """한 sweep 으로는 못 모으는 복수 자기 세션도 페이지를 넘겨 전부 찾아야 한다 (#220)."""
    repo, pool, prefix = pg_repo
    for index in range(_SWEEP_BATCH + 20):
        await repo.touch(BuyerSessionInput(f"{prefix}-residue-{index}", "T1", "guest", "G1"))
    session_ids = [f"{prefix}-own-{suffix}" for suffix in ("a", "b", "c")]
    for index, session_id in enumerate(session_ids):
        await repo.touch(BuyerSessionInput(session_id, "T1", "member", str(10 + index)))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at = now() - interval '1 hour' "
            "WHERE session_id LIKE %s",
            (prefix + "-residue-%",),
        )
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at = now() - interval '30 minutes' "
            "WHERE session_id = ANY(%s)",
            (session_ids,),
        )
    claims = await _claim_own_many(repo, session_ids)
    assert sorted(claims) == sorted(session_ids)


async def test_schema_initialize_is_idempotent_and_upgrades_old_turn_table(pg_repo) -> None:
    repo, pool, _ = pg_repo
    await repo.initialize()
    await repo.initialize()
    async with pool.connection() as conn:
        columns = await (
            await conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='conversation_turns' AND column_name IN ('context_id','session_id')"
            )
        ).fetchall()
        constraint = await (
            await conn.execute(
                "SELECT 1 FROM pg_constraint WHERE conname='conversation_turns_context_fk'"
            )
        ).fetchone()
    assert {row[0] for row in columns} == {"context_id", "session_id"}
    assert constraint == (1,)


async def test_legacy_backfill_maps_states_and_quarantines_ambiguous_owners(
    pg_repo,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo, pool, prefix = pg_repo
    active = prefix + "-active"
    idle = prefix + "-idle"
    terminal = prefix + "-terminal-backfill"
    ambiguous = prefix + "-ambiguous"
    ambiguous_completed = prefix + "-ambiguous-completed"
    authoritative = prefix + "-authoritative"
    signed_context = await repo.touch(
        BuyerSessionInput(authoritative, "signed-thread", "member", "708")
    )
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
        async with conn.cursor() as cursor:
            await cursor.executemany(
                """
                INSERT INTO profile_session_activity
                    (user_id, session_id, last_activity_at, status, claim_token,
                     lease_expires_at)
                VALUES (%s, %s, now()-interval '1 hour', %s, 'legacy-token',
                        now()+interval '1 hour')
                """,
                (
                    (701, active, "processing"),
                    (702, idle, "completed"),
                    (703, terminal, "completed"),
                    (704, ambiguous, "active"),
                    (705, ambiguous, "active"),
                    (706, ambiguous_completed, "completed"),
                    (707, ambiguous_completed, "active"),
                    (708, authoritative, "completed"),
                    (709, authoritative, "active"),
                ),
            )
        async with conn.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO processed_events (event_id, status) VALUES (%s, 'completed')",
                (
                    (f"session-end:703:{terminal}",),
                    (f"session-end:706:{ambiguous_completed}",),
                ),
            )
    try:
        restarted_repo = SessionContextRepository(pool=pool)
        with caplog.at_level("INFO", logger="app.core.session_context"):
            await restarted_repo.backfill_legacy_activity()
            await repo.backfill_legacy_activity()
        assert "batch=" in caplog.text
        assert "pass=" in caplog.text
        assert "cursorFp=" in caplog.text
        assert "graceDeadlineSet=True" in caplog.text
        assert active not in caplog.text
        async with pool.connection() as conn:
            contexts = await (
                await conn.execute(
                    "SELECT session_id, owner_id, state FROM chat_session_contexts "
                    "WHERE session_id = ANY(%s) ORDER BY session_id",
                    (
                        [
                            active,
                            idle,
                            terminal,
                            ambiguous,
                            ambiguous_completed,
                            authoritative,
                        ],
                    ),
                )
            ).fetchall()
            conflicts = await (
                await conn.execute(
                    "SELECT owner_id, resolution_status "
                    "FROM chat_session_migration_conflicts "
                    "WHERE session_id = ANY(%s) ORDER BY session_id, owner_id",
                    ([ambiguous, ambiguous_completed, authoritative],),
                )
            ).fetchall()
        assert contexts == sorted(
            [
                (active, "701", "active"),
                (authoritative, "708", "active"),
                (idle, "702", "idle_expired"),
                (terminal, "703", "terminal"),
            ]
        )
        assert conflicts == [
            ("704", "quarantined"),
            ("705", "quarantined"),
            ("706", "quarantined"),
            ("707", "quarantined"),
            ("709", "quarantined"),
        ]
        assert (await repo.get_context(authoritative)).context_id == signed_context.context_id
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM processed_events WHERE event_id = ANY(%s)",
                (
                    [
                        f"session-end:703:{terminal}",
                        f"session-end:706:{ambiguous_completed}",
                    ],
                ),
            )
            await conn.execute(
                "DELETE FROM profile_session_activity WHERE session_id LIKE %s",
                (prefix + "%",),
            )
            await conn.execute(
                "DELETE FROM chat_session_migration_conflicts WHERE session_id LIKE %s",
                (prefix + "%",),
            )


async def test_backfill_provenance_quarantines_late_owner_including_original(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-late-owner"
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
        await conn.execute(
            "INSERT INTO profile_session_activity "
            "(user_id, session_id, status) VALUES (8101, %s, 'active')",
            (session_id,),
        )
    try:
        await repo.backfill_legacy_activity()
        async with pool.connection() as conn:
            first = await (
                await conn.execute(
                    "SELECT owner_id, authority_source FROM chat_session_contexts "
                    "WHERE session_id=%s",
                    (session_id,),
                )
            ).fetchone()
            await conn.execute(
                "INSERT INTO profile_session_activity "
                "(user_id, session_id, status) VALUES (8102, %s, 'active')",
                (session_id,),
            )
        assert first == ("8101", "legacy_backfill")

        await repo.backfill_legacy_activity()

        async with pool.connection() as conn:
            context = await (
                await conn.execute(
                    "SELECT 1 FROM chat_session_contexts WHERE session_id=%s",
                    (session_id,),
                )
            ).fetchone()
            owners = await (
                await conn.execute(
                    "SELECT owner_id FROM chat_session_migration_conflicts "
                    "WHERE session_id=%s ORDER BY owner_id",
                    (session_id,),
                )
            ).fetchall()
        assert context is None
        assert owners == [("8101",), ("8102",)]
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM profile_session_activity WHERE session_id=%s", (session_id,)
            )
            await conn.execute(
                "DELETE FROM chat_session_migration_conflicts WHERE session_id=%s",
                (session_id,),
            )


async def test_signed_touch_replaces_legacy_guess_and_runtime_owner_then_wins(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-touch-takeover"
    old_owner = str(uuid.uuid4().int % 1_000_000_000 + 1_000_000_000)
    new_owner = str(int(old_owner) + 1)
    legacy_context_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_session_contexts
                (context_id, session_id, owner_type, owner_id,
                 authority_source, state)
            VALUES (%s, %s, 'member', %s, 'legacy_backfill', 'active')
            """,
            (legacy_context_id, session_id, old_owner),
        )
        await conn.execute(
            """
            INSERT INTO chat_session_migration_conflicts
                (session_id, owner_id, legacy_status, legacy_last_activity_at,
                 resolution_status, resolved_context_id)
            VALUES (%s, %s, 'completed', now()-interval '1 day',
                    'resolved', %s)
            """,
            (session_id, old_owner, legacy_context_id),
        )
    try:
        context = await repo.touch(
            BuyerSessionInput(session_id, "signed-thread", "member", new_owner)
        )
        async with pool.connection() as conn:
            authority = await (
                await conn.execute(
                    "SELECT owner_id, authority_source FROM chat_session_contexts "
                    "WHERE session_id=%s",
                    (session_id,),
                )
            ).fetchone()
            conflict = await (
                await conn.execute(
                    "SELECT resolution_status FROM chat_session_migration_conflicts "
                    "WHERE session_id=%s AND owner_id=%s",
                    (session_id, old_owner),
                )
            ).fetchone()
        assert authority == (new_owner, "runtime")
        assert conflict == ("quarantined",)
        assert context.owner_id == new_owner
        with pytest.raises(SessionForbidden):
            await repo.touch(BuyerSessionInput(session_id, "old-thread", "member", old_owner))
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM chat_session_migration_conflicts WHERE session_id=%s",
                (session_id,),
            )


async def test_signed_claim_replaces_legacy_guess_and_quarantines_old_owner(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-claim-takeover"
    old_owner = uuid.uuid4().int % 1_000_000_000 + 1_000_000_000
    new_owner = old_owner + 1
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_session_contexts
                (context_id, session_id, owner_type, owner_id,
                 authority_source, state)
            VALUES (%s, %s, 'member', %s, 'legacy_backfill', 'active')
            """,
            (str(uuid.uuid4()), session_id, str(old_owner)),
        )
    try:
        outcome = await repo.claim_owner(session_id, "signed-guest", new_owner)
        async with pool.connection() as conn:
            authority = await (
                await conn.execute(
                    "SELECT owner_id, authority_source FROM chat_session_contexts "
                    "WHERE session_id=%s",
                    (session_id,),
                )
            ).fetchone()
            conflict = await (
                await conn.execute(
                    "SELECT resolution_status FROM chat_session_migration_conflicts "
                    "WHERE session_id=%s AND owner_id=%s",
                    (session_id, str(old_owner)),
                )
            ).fetchone()
        assert outcome.claimed is True
        assert authority == (str(new_owner), "runtime")
        assert conflict == ("quarantined",)
        with pytest.raises(SessionForbidden):
            await repo.touch(BuyerSessionInput(session_id, "old-thread", "member", str(old_owner)))
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM chat_session_migration_conflicts WHERE session_id=%s",
                (session_id,),
            )


async def test_noop_backfill_preserves_completed_gc_marker(pg_repo) -> None:
    repo, pool, _ = pg_repo
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
    await repo.backfill_legacy_activity()
    async with pool.connection() as conn:
        before = await (
            await conn.execute(
                """
                UPDATE chat_session_migrations
                SET gc_completed_at=now()
                WHERE migration_name='issue-187-session-context'
                RETURNING profile_backfill_completed_at, gc_completed_at,
                          profile_backfill_pass
                """
            )
        ).fetchone()

    await repo.backfill_legacy_activity()

    async with pool.connection() as conn:
        after = await (
            await conn.execute(
                "SELECT profile_backfill_completed_at, gc_completed_at, "
                "profile_backfill_pass FROM chat_session_migrations "
                "WHERE migration_name='issue-187-session-context'"
            )
        ).fetchone()
    assert after == before


async def test_backfill_emits_durable_completed_progress_on_completion_and_restart(
    pg_repo,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """첫 완료와 no-op 재시작은 invocation별 completed event를 내되 raw cursor는 숨긴다."""
    repo, pool, _ = pg_repo
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )

    with caplog.at_level("INFO", logger="app.core.session_context"):
        await repo.backfill_legacy_activity()
    first_events = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.core.session_context"
        and "session lifecycle backfill batch=" in record.getMessage()
    ]
    assert first_events[-1].find("completed=True") >= 0
    assert "cursorFp=" in first_events[-1]
    assert "profile_backfill_cursor" not in first_events[-1]

    caplog.clear()
    restarted = SessionContextRepository(pool=pool)
    with caplog.at_level("INFO", logger="app.core.session_context"):
        await restarted.backfill_legacy_activity()
    restart_events = [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.core.session_context"
        and "session lifecycle backfill batch=" in record.getMessage()
    ]
    assert len(restart_events) == 1, "no-op restart는 invocation당 완료 progress 1건을 허용한다"
    assert "completed=True" in restart_events[0]


async def test_backfill_commit_failure_rolls_back_without_completed_progress(
    pg_repo,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """완료 snapshot은 commit 성공 전 로그에 노출하지 않고 commit 실패 시 rollback한다."""
    _, pool, _ = pg_repo
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
    fault_repo = SessionContextRepository(pool=_CommitFaultPool(pool))

    with caplog.at_level("INFO", logger="app.core.session_context"):
        with pytest.raises(ForeignKeyViolation):
            await fault_repo.backfill_legacy_activity()

    assert "completed=True" not in caplog.text
    async with pool.connection() as conn:
        migration = await (
            await conn.execute(
                "SELECT profile_backfill_completed_at "
                "FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
            )
        ).fetchone()
    assert migration is None


async def test_signed_touch_resolves_owner_and_gc_preserves_authoritative_buffer(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-signed-release"
    key_a, key_b = f"8201:{session_id}", f"8202:{session_id}"
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
        async with conn.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO profile_session_activity "
                "(user_id, session_id, status) VALUES (%s, %s, 'active')",
                ((8201, session_id), (8202, session_id)),
            )
    await repo.backfill_legacy_activity()
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "8201"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_migrations SET grace_deadline=now()-interval '1 second', "
            "gc_completed_at=NULL WHERE migration_name='issue-187-session-context'"
        )
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        try:
            await store.aput(("session_ctx", key_a), "buffer", {"owner": "A"})
            await store.aput(("session_ctx", key_b), "buffer", {"owner": "B"})
            await _expire_legacy_quiet(pool)
            for _ in range(10):
                await buyer_session_state.run_legacy_gc_batch()
            assert await store.aget(("session_ctx", key_a), "buffer") is not None
            assert await store.aget(("session_ctx", key_b), "buffer") is None
        finally:
            await store.adelete(("session_ctx", key_a), "buffer")
            await store.adelete(("session_ctx", key_b), "buffer")
            pg_store_module.reset_store()
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT owner_id, resolution_status, resolved_context_id "
                "FROM chat_session_migration_conflicts WHERE session_id=%s ORDER BY owner_id",
                (session_id,),
            )
        ).fetchall()
        await conn.execute(
            "DELETE FROM chat_session_migration_conflicts WHERE session_id=%s", (session_id,)
        )
    assert rows == [
        ("8201", "resolved", uuid.UUID(context.context_id)),
        ("8202", "discarded", None),
    ]


async def test_backfill_cursor_reconciles_late_lower_key_and_index_exists(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    low, high = prefix + "-a-low", prefix + "-z-high"
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
        await conn.execute(
            "INSERT INTO profile_session_activity "
            "(user_id, session_id, status) VALUES (8302, %s, 'active')",
            (high,),
        )
    try:
        await repo.backfill_legacy_activity()
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO profile_session_activity "
                "(user_id, session_id, status) VALUES (8301, %s, 'active')",
                (low,),
            )
            await conn.execute(
                "UPDATE chat_session_migrations "
                "SET profile_backfill_cursor=%s, profile_backfill_completed_at=NULL "
                "WHERE migration_name='issue-187-session-context'",
                (high,),
            )
        restarted_repo = SessionContextRepository(pool=pool)
        await restarted_repo.backfill_legacy_activity()
        async with pool.connection() as conn:
            contexts = await (
                await conn.execute(
                    "SELECT session_id FROM chat_session_contexts "
                    "WHERE session_id = ANY(%s) ORDER BY session_id",
                    ([low, high],),
                )
            ).fetchall()
            migration = await (
                await conn.execute(
                    "SELECT profile_backfill_cursor, profile_backfill_pass "
                    "FROM chat_session_migrations "
                    "WHERE migration_name='issue-187-session-context'"
                )
            ).fetchone()
            index = await (
                await conn.execute(
                    "SELECT 1 FROM pg_indexes "
                    "WHERE indexname='idx_profile_session_activity_session_owner'"
                )
            ).fetchone()
        assert contexts == [(low,), (high,)]
        assert migration[0] is None and migration[1] >= 1
        assert index == (1,)
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM profile_session_activity WHERE session_id = ANY(%s)",
                ([low, high],),
            )


async def test_backfill_uses_configured_grace_and_never_shortens_existing_deadline(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    settings_24h = get_settings().model_copy(update={"session_lifecycle_legacy_grace_s": 86400})
    monkeypatch.setattr(session_context_module, "get_settings", lambda: settings_24h)
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
        await conn.execute(
            """
            INSERT INTO chat_session_migrations
                (migration_name, rollout_started_at, grace_deadline)
            VALUES ('issue-187-session-context', now()-interval '2 days',
                    now()-interval '47 hours')
            """
        )

    await repo.backfill_legacy_activity()
    async with pool.connection() as conn:
        corrected_24h = await (
            await conn.execute(
                "SELECT extract(epoch FROM grace_deadline-rollout_started_at)::bigint "
                "FROM chat_session_migrations "
                "WHERE migration_name='issue-187-session-context'"
            )
        ).fetchone()
    assert corrected_24h == (86400,)

    settings_7d = settings_24h.model_copy(update={"session_lifecycle_legacy_grace_s": 7 * 86400})
    monkeypatch.setattr(session_context_module, "get_settings", lambda: settings_7d)
    await repo.backfill_legacy_activity()
    async with pool.connection() as conn:
        corrected_7d = await (
            await conn.execute(
                "SELECT extract(epoch FROM grace_deadline-rollout_started_at)::bigint "
                "FROM chat_session_migrations "
                "WHERE migration_name='issue-187-session-context'"
            )
        ).fetchone()
        await conn.execute(
            "UPDATE chat_session_migrations "
            "SET grace_deadline=rollout_started_at+interval '10 days' "
            "WHERE migration_name='issue-187-session-context'"
        )
    assert corrected_7d == (7 * 86400,)

    await repo.backfill_legacy_activity()
    async with pool.connection() as conn:
        preserved = await (
            await conn.execute(
                "SELECT extract(epoch FROM grace_deadline-rollout_started_at)::bigint "
                "FROM chat_session_migrations "
                "WHERE migration_name='issue-187-session-context'"
            )
        ).fetchone()
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
    assert preserved == (10 * 86400,)

    await repo.backfill_legacy_activity()
    async with pool.connection() as conn:
        created = await (
            await conn.execute(
                "SELECT extract(epoch FROM grace_deadline-rollout_started_at)::bigint "
                "FROM chat_session_migrations "
                "WHERE migration_name='issue-187-session-context'"
            )
        ).fetchone()
    assert created == (7 * 86400,)

    key = prefix + "-future-grace"
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        try:
            await store.aput(("buyer_thread_filters", key), "filters", {"legacy": True})
            assert await buyer_session_state.run_legacy_gc_batch() == 0
            assert await store.aget(("buyer_thread_filters", key), "filters") is not None
        finally:
            await store.adelete(("buyer_thread_filters", key), "filters")
            pg_store_module.reset_store()


async def test_backfill_bounds_many_owners_with_durable_composite_cursor(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-many-owners"
    limited = get_settings().model_copy(
        update={
            "session_lifecycle_gc_batch_size": 1,
            "session_lifecycle_backfill_max_batches": 1,
        }
    )
    monkeypatch.setattr(session_context_module, "get_settings", lambda: limited)
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
        async with conn.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO profile_session_activity "
                "(user_id, session_id, status) VALUES (%s, %s, 'active')",
                tuple((owner, session_id) for owner in range(9101, 9106)),
            )
    try:
        with pytest.raises(SessionStateUnavailable, match="batch limit"):
            await repo.backfill_legacy_activity()
        async with pool.connection() as conn:
            checkpoint = await (
                await conn.execute(
                    "SELECT profile_backfill_cursor, profile_backfill_owner_cursor, "
                    "profile_backfill_pass "
                    "FROM chat_session_migrations "
                    "WHERE migration_name='issue-187-session-context'"
                )
            ).fetchone()
            count = await (
                await conn.execute(
                    "SELECT count(*) FROM chat_session_contexts WHERE session_id=%s",
                    (session_id,),
                )
            ).fetchone()
        assert checkpoint == (session_id, 9101, 1)
        assert count == (1,)

        resumed_settings = limited.model_copy(update={"session_lifecycle_backfill_max_batches": 10})
        monkeypatch.setattr(session_context_module, "get_settings", lambda: resumed_settings)
        restarted = SessionContextRepository(pool=pool)
        await restarted.backfill_legacy_activity()
        async with pool.connection() as conn:
            resumed = await (
                await conn.execute(
                    "SELECT profile_backfill_cursor, profile_backfill_owner_cursor, "
                    "profile_backfill_pass "
                    "FROM chat_session_migrations "
                    "WHERE migration_name='issue-187-session-context'"
                )
            ).fetchone()
            count = await (
                await conn.execute(
                    "SELECT count(*) FROM chat_session_contexts WHERE session_id=%s",
                    (session_id,),
                )
            ).fetchone()
            conflicts = await (
                await conn.execute(
                    "SELECT owner_id FROM chat_session_migration_conflicts "
                    "WHERE session_id=%s ORDER BY owner_id",
                    (session_id,),
                )
            ).fetchall()
        assert resumed == (None, None, 1)
        assert count == (0,)
        assert conflicts == [(str(owner),) for owner in range(9101, 9106)]
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM profile_session_activity WHERE session_id=%s",
                (session_id,),
            )
            await conn.execute(
                "DELETE FROM chat_session_migration_conflicts WHERE session_id=%s",
                (session_id,),
            )


async def test_backfill_batch_fault_rolls_back_rows_and_restart_resumes(
    pg_repo, monkeypatch
) -> None:
    _, pool, prefix = pg_repo
    session_id = prefix + "-backfill-rollback"
    settings = get_settings().model_copy(
        update={
            "session_lifecycle_gc_batch_size": 1,
            "session_lifecycle_backfill_max_batches": 10,
        }
    )
    monkeypatch.setattr(session_context_module, "get_settings", lambda: settings)
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
        await conn.execute(
            """
            INSERT INTO chat_session_migrations
                (migration_name, rollout_started_at, grace_deadline,
                 profile_backfill_cursor, profile_backfill_pass)
            VALUES ('issue-187-session-context', now(), now()+interval '7 days', '', 5)
            """
        )
        await conn.execute(
            "INSERT INTO profile_session_activity "
            "(user_id, session_id, status) VALUES (9201, %s, 'active')",
            (session_id,),
        )
    fault_pool = _FaultPool(
        pool,
        lambda query: "SET profile_backfill_cursor=%s" in query,
    )
    fault_repo = SessionContextRepository(pool=fault_pool)
    try:
        with pytest.raises(RuntimeError, match="injected transaction fault"):
            await fault_repo.backfill_legacy_activity()
        async with pool.connection() as conn:
            checkpoint = await (
                await conn.execute(
                    "SELECT profile_backfill_cursor, profile_backfill_pass "
                    "FROM chat_session_migrations "
                    "WHERE migration_name='issue-187-session-context'"
                )
            ).fetchone()
            context = await (
                await conn.execute(
                    "SELECT context_id FROM chat_session_contexts WHERE session_id=%s",
                    (session_id,),
                )
            ).fetchone()
        assert checkpoint == ("", 5)
        assert context is None

        restarted = SessionContextRepository(pool=pool)
        await restarted.backfill_legacy_activity()
        async with pool.connection() as conn:
            checkpoint = await (
                await conn.execute(
                    "SELECT profile_backfill_cursor, profile_backfill_pass "
                    "FROM chat_session_migrations "
                    "WHERE migration_name='issue-187-session-context'"
                )
            ).fetchone()
            context = await (
                await conn.execute(
                    "SELECT authority_source FROM chat_session_contexts WHERE session_id=%s",
                    (session_id,),
                )
            ).fetchone()
        assert checkpoint == (None, 5)
        assert context == ("legacy_backfill",)
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM profile_session_activity WHERE session_id=%s",
                (session_id,),
            )


async def test_legacy_gc_restarts_from_first_page_and_preserves_v2(pg_repo, monkeypatch) -> None:
    repo, pool, prefix = pg_repo
    owner_id = str(uuid.uuid4().int % 1_000_000_000 + 1_000_000_000)
    session_id = prefix + "-gc-conflict"
    migrated_session_id = prefix + "-gc-migrated"
    legacy_keys = [prefix + "-legacy-a", prefix + "-legacy-b"]
    v2_key = prefix + "-v2"
    settings = get_settings().model_copy(update={"session_lifecycle_gc_batch_size": 1})
    monkeypatch.setattr(session_context_module, "get_settings", lambda: settings)
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_session_migrations
                (migration_name, rollout_started_at, grace_deadline,
                 profile_backfill_completed_at)
            VALUES ('issue-187-session-context', now()-interval '2 days',
                    now()-interval '1 day', now()-interval '1 day')
            ON CONFLICT (migration_name) DO UPDATE
            SET grace_deadline=EXCLUDED.grace_deadline, gc_completed_at=NULL,
                filters_deleted=0, cart_deleted=0, revert_deleted=0
            """
        )
        await conn.execute(
            """
            INSERT INTO profile_session_activity
                (user_id, session_id, last_activity_at, status)
            VALUES (%s, %s, now()-interval '2 days', 'active')
            """,
            (int(owner_id), session_id),
        )
        await conn.execute(
            """
            INSERT INTO profile_session_activity
                (user_id, session_id, last_activity_at, status)
            VALUES (%s, %s, now()-interval '2 days', 'completed')
            """,
            (int(owner_id) + 1, migrated_session_id),
        )
        await conn.execute(
            """
            INSERT INTO chat_session_migration_conflicts
                (session_id, owner_id, legacy_status, legacy_last_activity_at)
            VALUES (%s, %s, 'active', now()-interval '2 days')
            """,
            (session_id, owner_id),
        )
    await repo.backfill_legacy_activity()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_migrations SET grace_deadline=now()-interval '1 day', "
            "gc_completed_at=NULL WHERE migration_name='issue-187-session-context'"
        )
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        try:
            for key in legacy_keys:
                await store.aput(("buyer_thread_filters", key), "filters", {"legacy": key})
            await store.aput(("buyer_cart", legacy_keys[0]), "last_reco", {"product_ids": [1]})
            cart_state._last_reco_names[legacy_keys[0]] = {1: "legacy"}
            await store.aput(("session_ctx", f"{owner_id}:{session_id}"), "buffer", {"items": []})
            await store.aput(("buyer_thread_filters_v2", v2_key), "filters", {"v2": True})
            await _expire_legacy_quiet(pool)

            await buyer_session_state.run_legacy_gc_batch()
            async with pool.connection() as conn:
                first = await (
                    await conn.execute(
                        "SELECT filters_deleted, gc_completed_at "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
            assert first[0] == 1
            assert first[1] is None

            for _ in range(200):
                await buyer_session_state.run_legacy_gc_batch()
                async with pool.connection() as conn:
                    done = await (
                        await conn.execute(
                            "SELECT gc_completed_at FROM chat_session_migrations "
                            "WHERE migration_name='issue-187-session-context'"
                        )
                    ).fetchone()
                if done[0] is not None:
                    break
            async with pool.connection() as conn:
                finished = await (
                    await conn.execute(
                        "SELECT filters_deleted, gc_completed_at "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
                conflict = await (
                    await conn.execute(
                        "SELECT resolution_status, profile_buffer_discarded_at "
                        "FROM chat_session_migration_conflicts "
                        "WHERE session_id=%s AND owner_id=%s",
                        (session_id, owner_id),
                    )
                ).fetchone()
                activity = await (
                    await conn.execute(
                        "SELECT session_id FROM profile_session_activity "
                        "WHERE session_id = ANY(%s)",
                        ([session_id, migrated_session_id],),
                    )
                ).fetchall()
            assert finished[0] >= 2
            assert finished[1] is not None
            assert conflict[0] == "discarded" and conflict[1] is not None
            assert activity == []
            assert await store.aget(("buyer_thread_filters_v2", v2_key), "filters") is not None
            assert cart_state._last_reco_names.get(legacy_keys[0]) is None
        finally:
            for key in legacy_keys:
                await store.adelete(("buyer_thread_filters", key), "filters")
            await store.adelete(("buyer_cart", legacy_keys[0]), "last_reco")
            cart_state._last_reco_names.pop(legacy_keys[0])
            await store.adelete(("session_ctx", f"{owner_id}:{session_id}"), "buffer")
            await store.adelete(("buyer_thread_filters_v2", v2_key), "filters")
            pg_store_module.reset_store()
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM profile_session_activity WHERE session_id = ANY(%s)",
            ([session_id, migrated_session_id],),
        )
        await conn.execute(
            "DELETE FROM chat_session_migration_conflicts WHERE session_id=%s",
            (session_id,),
        )


async def test_legacy_root_delete_fault_rolls_back_counter_and_restart_is_exact(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    key = prefix + "-root-rollback"
    settings = get_settings().model_copy(update={"session_lifecycle_gc_batch_size": 10})
    monkeypatch.setattr(session_context_module, "get_settings", lambda: settings)
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_session_migrations
                (migration_name, rollout_started_at, grace_deadline,
                 profile_backfill_completed_at)
            VALUES ('issue-187-session-context', now()-interval '2 days',
                    now()-interval '1 day', now()-interval '1 day')
            ON CONFLICT (migration_name) DO UPDATE
            SET grace_deadline=EXCLUDED.grace_deadline,
                profile_backfill_completed_at=EXCLUDED.profile_backfill_completed_at,
                gc_completed_at=NULL,
                filters_deleted=0, cart_deleted=0, revert_deleted=0
            """
        )
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        await store.aput(("buyer_thread_filters", key), "filters", {"category": "legacy"})
        await store.aput(
            ("buyer_cart", key),
            "pending",
            {"product_id": 1, "quantity": 1, "options": [], "attempts": 0},
        )
        await store.aput(("buyer_revert", key), "categories", {"categories": ["legacy"]})
        fault_pool = _FaultPool(
            pool,
            lambda query: "DELETE FROM store s" in query,
        )
        fault_repo = SessionContextRepository(pool=fault_pool)
        monkeypatch.setattr(session_context_module, "_default_repository", fault_repo)
        try:
            await _expire_legacy_quiet(pool)
            with pytest.raises(RuntimeError, match="injected transaction fault"):
                await buyer_session_state.run_legacy_gc_batch()
            async with pool.connection() as conn:
                counters = await (
                    await conn.execute(
                        "SELECT filters_deleted, cart_deleted, revert_deleted "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
            assert counters == (0, 0, 0)
            assert await store.aget(("buyer_thread_filters", key), "filters") is not None

            restarted = SessionContextRepository(pool=pool)
            monkeypatch.setattr(session_context_module, "_default_repository", restarted)
            await buyer_session_state.run_legacy_gc_batch()
            async with pool.connection() as conn:
                counters = await (
                    await conn.execute(
                        "SELECT filters_deleted, cart_deleted, revert_deleted "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
            assert counters == (1, 1, 1)
            assert await store.aget(("buyer_thread_filters", key), "filters") is None
            assert await store.aget(("buyer_cart", key), "pending") is None
            assert await store.aget(("buyer_revert", key), "categories") is None
        finally:
            await store.adelete(("buyer_thread_filters", key), "filters")
            await store.adelete(("buyer_cart", key), "pending")
            await store.adelete(("buyer_revert", key), "categories")
            pg_store_module.reset_store()


async def test_activity_delete_fault_keeps_gc_open_until_restart_observes_empty(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-activity-rollback"
    owner_id = str(uuid.uuid4().int % 1_000_000_000 + 1_000_000_000)
    await repo.touch(BuyerSessionInput(session_id, "thread", "member", owner_id))
    settings = get_settings().model_copy(update={"session_lifecycle_gc_batch_size": 10})
    monkeypatch.setattr(session_context_module, "get_settings", lambda: settings)
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_session_migrations
                (migration_name, rollout_started_at, grace_deadline,
                 profile_backfill_completed_at)
            VALUES ('issue-187-session-context', now()-interval '2 days',
                    now()-interval '1 day', now()-interval '1 day')
            ON CONFLICT (migration_name) DO UPDATE
            SET grace_deadline=EXCLUDED.grace_deadline,
                profile_backfill_completed_at=EXCLUDED.profile_backfill_completed_at,
                gc_completed_at=NULL,
                legacy_quiet_until=now()-interval '1 second',
                conflict_gc_cursor=0
            """
        )
        await conn.execute(
            "INSERT INTO profile_session_activity "
            "(user_id, session_id, status) VALUES (%s, %s, 'active')",
            (int(owner_id), session_id),
        )
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        fault_pool = _FaultPool(
            pool,
            lambda query: "DELETE FROM profile_session_activity a" in query,
        )
        fault_repo = SessionContextRepository(pool=fault_pool)
        monkeypatch.setattr(session_context_module, "_default_repository", fault_repo)
        try:
            await _expire_legacy_quiet(pool)
            with pytest.raises(RuntimeError, match="injected transaction fault"):
                await buyer_session_state.run_legacy_gc_batch()
            async with pool.connection() as conn:
                activity = await (
                    await conn.execute(
                        "SELECT status FROM profile_session_activity "
                        "WHERE user_id=%s AND session_id=%s",
                        (int(owner_id), session_id),
                    )
                ).fetchone()
                completed = await (
                    await conn.execute(
                        "SELECT gc_completed_at FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
            assert activity == ("active",)
            assert completed == (None,)

            restarted = SessionContextRepository(pool=pool)
            monkeypatch.setattr(session_context_module, "_default_repository", restarted)
            await buyer_session_state.run_legacy_gc_batch()
            async with pool.connection() as conn:
                activity = await (
                    await conn.execute(
                        "SELECT status FROM profile_session_activity "
                        "WHERE user_id=%s AND session_id=%s",
                        (int(owner_id), session_id),
                    )
                ).fetchone()
                completed = await (
                    await conn.execute(
                        "SELECT gc_completed_at IS NOT NULL "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
            assert activity is None
            assert completed == (True,)
        finally:
            pg_store_module.reset_store()
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM profile_session_activity WHERE session_id=%s",
            (session_id,),
        )


async def test_gc_statement_barrier_reconciles_late_writer_before_any_root_delete(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-barrier-late"
    owner_id = uuid.uuid4().int % 1_000_000_000 + 1_000_000_000
    root_key = prefix + "-legacy-root"
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
    await repo.backfill_legacy_activity()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_migrations "
            "SET grace_deadline=now()-interval '1 second', gc_completed_at=NULL "
            "WHERE migration_name='issue-187-session-context'"
        )

    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        try:
            await store.aput(("buyer_thread_filters", root_key), "filters", {"late": True})
            async with pool.connection() as blocker:
                async with blocker.transaction():
                    await blocker.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        ("migration:issue-187-session-context",),
                    )
                    gc_task = asyncio.create_task(buyer_session_state.run_legacy_gc_batch())
                    waiting = None
                    for _ in range(50):
                        async with pool.connection() as observer:
                            waiting = await (
                                await observer.execute(
                                    "SELECT 1 FROM pg_locks "
                                    "WHERE locktype='advisory' AND NOT granted LIMIT 1"
                                )
                            ).fetchone()
                        if waiting is not None:
                            break
                        await asyncio.sleep(0.01)
                    assert waiting is not None
                    async with pool.connection() as writer:
                        await writer.execute(
                            "INSERT INTO profile_session_activity "
                            "(user_id, session_id, status) VALUES (%s, %s, 'active')",
                            (owner_id, session_id),
                        )
                    await _expire_legacy_quiet(pool)
            assert await asyncio.wait_for(gc_task, timeout=5) == 0

            async with pool.connection() as conn:
                authority = await (
                    await conn.execute(
                        "SELECT owner_id, authority_source FROM chat_session_contexts "
                        "WHERE session_id=%s",
                        (session_id,),
                    )
                ).fetchone()
                migration = await (
                    await conn.execute(
                        "SELECT profile_backfill_completed_at IS NOT NULL, "
                        "gc_completed_at FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
            assert authority == (str(owner_id), "legacy_backfill")
            assert migration == (True, None)
            assert await store.aget(("buyer_thread_filters", root_key), "filters") is not None

            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE chat_session_migrations "
                    "SET grace_deadline=now()-interval '1 second', "
                    "legacy_quiet_until=now()-interval '1 second' "
                    "WHERE migration_name='issue-187-session-context'"
                )
            for _ in range(20):
                await buyer_session_state.run_legacy_gc_batch()
                if await store.aget(("buyer_thread_filters", root_key), "filters") is None:
                    break
            assert await store.aget(("buyer_thread_filters", root_key), "filters") is None
        finally:
            await store.adelete(("buyer_thread_filters", root_key), "filters")
            pg_store_module.reset_store()
    async with pool.connection() as conn:
        with pytest.raises(CheckViolation):
            async with conn.transaction():
                await conn.execute(
                    "UPDATE chat_session_migrations SET legacy_quiet_window_s=89 "
                    "WHERE migration_name='issue-187-session-context'"
                )
        await conn.execute(
            "DELETE FROM profile_session_activity WHERE session_id=%s", (session_id,)
        )


async def test_gc_empty_reconciliation_holds_writer_barrier_through_destructive_phase(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-gc-writer-barrier"
    owner_id = uuid.uuid4().int % 1_000_000_000 + 1_000_000_000
    root_key = prefix + "-gc-writer-root"
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
    await repo.backfill_legacy_activity()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_migrations "
            "SET grace_deadline=now()-interval '1 second', gc_completed_at=NULL "
            "WHERE migration_name='issue-187-session-context'"
        )

    empty_seen = asyncio.Event()
    resume_gc = asyncio.Event()
    original_find = session_context_module._find_actionable_legacy_activity

    async def pause_after_empty(conn):  # noqa: ANN001
        result = await original_find(conn)
        if result is None:
            empty_seen.set()
            await resume_gc.wait()
        return result

    monkeypatch.setattr(
        session_context_module, "_find_actionable_legacy_activity", pause_after_empty
    )
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        try:
            gc_task = asyncio.create_task(buyer_session_state.run_legacy_gc_batch())
            await asyncio.wait_for(empty_seen.wait(), timeout=2)

            async def late_writer() -> None:
                await store.aput(("buyer_thread_filters", root_key), "filters", {"late": True})
                async with pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO profile_session_activity "
                        "(user_id, session_id, status) VALUES (%s, %s, 'active')",
                        (owner_id, session_id),
                    )

            writer_task = asyncio.create_task(late_writer())
            await asyncio.sleep(0.1)
            assert not writer_task.done()
            async with pool.connection() as reader:
                readable = await (
                    await reader.execute("SELECT count(*) FROM profile_session_activity")
                ).fetchone()
            assert readable is not None

            resume_gc.set()
            await asyncio.wait_for(gc_task, timeout=5)
            await asyncio.wait_for(writer_task, timeout=5)
            assert await store.aget(("buyer_thread_filters", root_key), "filters") is not None
        finally:
            resume_gc.set()
            for task_name in ("gc_task", "writer_task"):
                task = locals().get(task_name)
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await store.adelete(("buyer_thread_filters", root_key), "filters")
            pg_store_module.reset_store()

    async with pool.connection() as conn:
        activity = await (
            await conn.execute(
                "SELECT status FROM profile_session_activity WHERE user_id=%s AND session_id=%s",
                (owner_id, session_id),
            )
        ).fetchone()
        marker = await (
            await conn.execute(
                "SELECT gc_completed_at FROM chat_session_migrations "
                "WHERE migration_name='issue-187-session-context'"
            )
        ).fetchone()
        await conn.execute(
            "DELETE FROM profile_session_activity WHERE session_id=%s", (session_id,)
        )
    assert activity == ("active",)
    assert marker == (None,)


async def test_gc_completion_marker_holds_writer_barrier_until_commit(pg_repo, monkeypatch) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-gc-completion-barrier"
    owner_id = uuid.uuid4().int % 1_000_000_000 + 1_000_000_000
    root_key = prefix + "-gc-completion-root"
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
    await repo.backfill_legacy_activity()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_migrations "
            "SET grace_deadline=now()-interval '1 second', gc_completed_at=NULL "
            "WHERE migration_name='issue-187-session-context'"
        )

    before_marker = asyncio.Event()
    resume_marker = asyncio.Event()
    original_mark = getattr(buyer_session_state, "_mark_legacy_gc_completed", None)

    async def paused_mark(conn, migration_name):  # noqa: ANN001
        before_marker.set()
        await resume_marker.wait()
        assert original_mark is not None
        await original_mark(conn, migration_name)

    monkeypatch.setattr(
        buyer_session_state, "_mark_legacy_gc_completed", paused_mark, raising=False
    )
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        try:
            gc_task = asyncio.create_task(buyer_session_state.run_legacy_gc_batch())
            await asyncio.wait_for(before_marker.wait(), timeout=2)

            async def late_writer() -> None:
                await store.aput(("buyer_thread_filters", root_key), "filters", {"late": True})
                async with pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO profile_session_activity "
                        "(user_id, session_id, status) VALUES (%s, %s, 'active')",
                        (owner_id, session_id),
                    )

            writer_task = asyncio.create_task(late_writer())
            await asyncio.sleep(0.1)
            assert not writer_task.done()
            resume_marker.set()
            await asyncio.wait_for(gc_task, timeout=5)
            await asyncio.wait_for(writer_task, timeout=5)
            assert await store.aget(("buyer_thread_filters", root_key), "filters") is not None
        finally:
            resume_marker.set()
            for task_name in ("gc_task", "writer_task"):
                task = locals().get(task_name)
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await store.adelete(("buyer_thread_filters", root_key), "filters")
            pg_store_module.reset_store()

    async with pool.connection() as conn:
        activity = await (
            await conn.execute(
                "SELECT status FROM profile_session_activity WHERE user_id=%s AND session_id=%s",
                (owner_id, session_id),
            )
        ).fetchone()
        marker = await (
            await conn.execute(
                "SELECT gc_completed_at FROM chat_session_migrations "
                "WHERE migration_name='issue-187-session-context'"
            )
        ).fetchone()
        await conn.execute(
            "DELETE FROM profile_session_activity WHERE session_id=%s", (session_id,)
        )
    assert activity == ("active",)
    assert marker == (None,)


async def test_activity_then_late_partial_root_write_extends_durable_gc_quiet_window(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-quiet-sequence"
    owner_id = uuid.uuid4().int % 1_000_000_000 + 1_000_000_000
    filter_key = prefix + "-quiet-filter"
    cart_key = prefix + "-quiet-cart"
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
    await repo.backfill_legacy_activity()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_migrations "
            "SET grace_deadline=now()-interval '1 second', gc_completed_at=NULL "
            "WHERE migration_name='issue-187-session-context'"
        )

    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        try:
            await store.aput(("buyer_thread_filters", filter_key), "filters", {"old": True})
            await store.aput(("buyer_cart", cart_key), "pending", {"old": True})
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO profile_session_activity "
                    "(user_id, session_id, status) VALUES (%s, %s, 'active')",
                    (owner_id, session_id),
                )
                activity_evidence = await (
                    await conn.execute(
                        "SELECT legacy_writer_seen_at, legacy_quiet_until "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()

            assert await buyer_session_state.run_legacy_gc_batch() == 0
            assert await store.aget(("buyer_thread_filters", filter_key), "filters") is not None
            assert await store.aget(("buyer_cart", cart_key), "pending") is not None

            await store.aput(("buyer_thread_filters", filter_key), "filters", {"late": True})
            async with pool.connection() as conn:
                root_evidence = await (
                    await conn.execute(
                        "SELECT legacy_writer_seen_at, legacy_quiet_until "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
            assert root_evidence[0] >= activity_evidence[0]
            assert root_evidence[1] >= activity_evidence[1]

            assert await buyer_session_state.run_legacy_gc_batch() == 0
            assert await store.aget(("buyer_thread_filters", filter_key), "filters") is not None
            assert await store.aget(("buyer_cart", cart_key), "pending") is not None

            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE chat_session_migrations "
                    "SET legacy_quiet_until=now()-interval '1 second' "
                    "WHERE migration_name='issue-187-session-context'"
                )
            await buyer_session_state.run_legacy_gc_batch()
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE chat_session_migrations "
                    "SET grace_deadline=now()-interval '1 second', "
                    "legacy_quiet_until=now()-interval '1 second' "
                    "WHERE migration_name='issue-187-session-context'"
                )
            for _ in range(20):
                await buyer_session_state.run_legacy_gc_batch()
                if (
                    await store.aget(("buyer_thread_filters", filter_key), "filters") is None
                    and await store.aget(("buyer_cart", cart_key), "pending") is None
                ):
                    break
            assert await store.aget(("buyer_thread_filters", filter_key), "filters") is None
            assert await store.aget(("buyer_cart", cart_key), "pending") is None
        finally:
            await store.adelete(("buyer_thread_filters", filter_key), "filters")
            await store.adelete(("buyer_cart", cart_key), "pending")
            pg_store_module.reset_store()
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM profile_session_activity WHERE session_id=%s", (session_id,)
        )


async def test_completed_gc_fast_path_does_not_wait_on_root_fence_or_v2_store_write(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    legacy_key = prefix + "-fast-legacy"
    v2_key = prefix + "-fast-v2"
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
    await repo.backfill_legacy_activity()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_migrations "
            "SET grace_deadline=now()-interval '1 second', gc_completed_at=now() "
            "WHERE migration_name='issue-187-session-context'"
        )

    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        try:
            async with pool.connection() as blocker:
                await blocker.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                    (buyer_session_state._LEGACY_ROOT_FENCE_KEY,),
                )
                try:
                    sweep = asyncio.create_task(buyer_session_state.run_legacy_gc_batch())
                    v2_write = asyncio.create_task(
                        store.aput(("buyer_thread_filters_v2", v2_key), "filters", {"v2": True})
                    )
                    assert await asyncio.wait_for(sweep, timeout=1) == 0
                    await asyncio.wait_for(v2_write, timeout=1)
                finally:
                    await blocker.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                        (buyer_session_state._LEGACY_ROOT_FENCE_KEY,),
                    )

            async with pool.connection() as conn:
                before_legacy = await (
                    await conn.execute(
                        "SELECT gc_completed_at, legacy_writer_seen_at "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
            assert before_legacy[0] is not None
            assert before_legacy[1] is None

            await store.aput(("buyer_thread_filters", legacy_key), "filters", {"legacy": True})
            async with pool.connection() as conn:
                reopened = await (
                    await conn.execute(
                        "SELECT gc_completed_at, legacy_writer_seen_at, "
                        "legacy_quiet_until > now() "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
                await conn.execute(
                    "UPDATE chat_session_migrations "
                    "SET legacy_quiet_until=now()-interval '1 second' "
                    "WHERE migration_name='issue-187-session-context'"
                )
            assert reopened[0] is None and reopened[1] is not None and reopened[2] is True
            for _ in range(10):
                await buyer_session_state.run_legacy_gc_batch()
                if await store.aget(("buyer_thread_filters", legacy_key), "filters") is None:
                    break
            assert await store.aget(("buyer_thread_filters", legacy_key), "filters") is None
            assert await store.aget(("buyer_thread_filters_v2", v2_key), "filters") is not None
        finally:
            await store.adelete(("buyer_thread_filters", legacy_key), "filters")
            await store.adelete(("buyer_thread_filters_v2", v2_key), "filters")
            pg_store_module.reset_store()


@pytest.mark.parametrize(
    ("root", "name", "value"),
    (
        ("buyer_thread_filters", "filters", {"category": "late"}),
        (
            "buyer_cart",
            "pending",
            {"product_id": 1, "quantity": 1, "options": [], "attempts": 0},
        ),
        ("buyer_revert", "categories", {"categories": ["late"]}),
    ),
)
async def test_store_only_late_legacy_write_reopens_completed_gc_and_is_reaped(
    pg_repo, monkeypatch, root: str, name: str, value: dict
) -> None:
    repo, pool, prefix = pg_repo
    key = f"{prefix}-{root}-late"
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_session_migrations
                (migration_name, rollout_started_at, grace_deadline,
                 profile_backfill_completed_at, gc_completed_at)
            VALUES ('issue-187-session-context', now()-interval '8 days',
                    now()-interval '1 day', now()-interval '1 day', now())
            ON CONFLICT (migration_name) DO UPDATE
            SET grace_deadline=EXCLUDED.grace_deadline,
                profile_backfill_completed_at=EXCLUDED.profile_backfill_completed_at,
                gc_completed_at=EXCLUDED.gc_completed_at
            """
        )
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        try:
            await store.aput((root, key), name, value)
            async with pool.connection() as conn:
                reopened = await (
                    await conn.execute(
                        "SELECT gc_completed_at FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
                await conn.execute(
                    "UPDATE chat_session_migrations "
                    "SET legacy_quiet_until=now()-interval '1 second' "
                    "WHERE migration_name='issue-187-session-context'"
                )
            assert reopened == (None,)

            for _ in range(10):
                await buyer_session_state.run_legacy_gc_batch()
                if await store.aget((root, key), name) is None:
                    break
            assert await store.aget((root, key), name) is None
            async with pool.connection() as conn:
                completed = await (
                    await conn.execute(
                        "SELECT gc_completed_at IS NOT NULL "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
            assert completed == (True,)
        finally:
            await store.adelete((root, key), name)
            pg_store_module.reset_store()


async def test_v2_store_write_does_not_reopen_completed_legacy_gc(pg_repo) -> None:
    _, pool, prefix = pg_repo
    key = prefix + "-v2-no-reopen"
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_migrations SET gc_completed_at=now() "
            "WHERE migration_name='issue-187-session-context'"
        )
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        try:
            await store.aput(("buyer_thread_filters_v2", key), "filters", {"category": "v2"})
            async with pool.connection() as conn:
                completed = await (
                    await conn.execute(
                        "SELECT gc_completed_at IS NOT NULL "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
            assert completed == (True,)
        finally:
            await store.adelete(("buyer_thread_filters_v2", key), "filters")


async def test_gc_barrier_does_not_block_unrelated_store_namespaces(pg_repo, monkeypatch) -> None:
    repo, pool, prefix = pg_repo
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_migrations "
            "SET grace_deadline=now()-interval '1 second', "
            "profile_backfill_completed_at=now(), gc_completed_at=NULL, "
            "legacy_quiet_until=now()-interval '1 second' "
            "WHERE migration_name='issue-187-session-context'"
        )
    barrier = asyncio.Event()
    release_gc = asyncio.Event()
    original_find = session_context_module._find_actionable_legacy_activity

    async def pause_with_gc_locks(conn):  # noqa: ANN001
        result = await original_find(conn)
        barrier.set()
        await release_gc.wait()
        return result

    monkeypatch.setattr(
        session_context_module, "_find_actionable_legacy_activity", pause_with_gc_locks
    )
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        writes = [
            (("buyer_thread_filters_v2", prefix + "-v2"), "filters"),
            (("session_ctx", prefix + "-profile"), "buffer"),
            (("seller_runtime", prefix + "-seller"), "state"),
        ]
        try:
            gc_task = asyncio.create_task(buyer_session_state.run_legacy_gc_batch())
            await asyncio.wait_for(barrier.wait(), timeout=2)
            writer_tasks = [
                asyncio.create_task(store.aput(namespace, name, {"value": name}))
                for namespace, name in writes
            ]
            await asyncio.sleep(0.1)
            assert all(task.done() and task.exception() is None for task in writer_tasks)
            release_gc.set()
            await asyncio.wait_for(gc_task, timeout=2)
            async with pool.connection() as conn:
                completed = await (
                    await conn.execute(
                        "SELECT gc_completed_at IS NOT NULL "
                        "FROM chat_session_migrations "
                        "WHERE migration_name='issue-187-session-context'"
                    )
                ).fetchone()
            assert completed == (True,)
        finally:
            release_gc.set()
            if "gc_task" in locals():
                await asyncio.gather(gc_task, return_exceptions=True)
            for task in locals().get("writer_tasks", []):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*locals().get("writer_tasks", []), return_exceptions=True)
            for namespace, name in writes:
                await store.adelete(namespace, name)
            pg_store_module.reset_store()


async def test_backfill_takes_session_lock_before_no_row_context_insert(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-backfill-claim-race"
    legacy_owner = uuid.uuid4().int % 1_000_000_000 + 1_000_000_000
    signed_owner = legacy_owner + 1
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migrations WHERE migration_name='issue-187-session-context'"
        )
        await conn.execute(
            "INSERT INTO profile_session_activity "
            "(user_id, session_id, status) VALUES (%s, %s, 'active')",
            (legacy_owner, session_id),
        )

    try:
        async with pool.connection() as claim_conn:
            async with claim_conn.transaction():
                await session_context_module._advisory_lock(claim_conn, session_id)
                no_row = await (
                    await claim_conn.execute(
                        "SELECT context_id FROM chat_session_contexts WHERE session_id=%s",
                        (session_id,),
                    )
                ).fetchone()
                assert no_row is None

                backfill_task = asyncio.create_task(repo.backfill_legacy_activity())
                await asyncio.sleep(0.1)
                assert not backfill_task.done()

                outcome = await repo._claim_owner_on_connection(
                    claim_conn, session_id, "signed-guest", signed_owner
                )
                assert outcome.context.owner_id == str(signed_owner)
        await asyncio.wait_for(backfill_task, timeout=5)

        async with pool.connection() as conn:
            authority = await (
                await conn.execute(
                    "SELECT owner_id, authority_source FROM chat_session_contexts "
                    "WHERE session_id=%s",
                    (session_id,),
                )
            ).fetchone()
            conflict = await (
                await conn.execute(
                    "SELECT resolution_status FROM chat_session_migration_conflicts "
                    "WHERE session_id=%s AND owner_id=%s",
                    (session_id, str(legacy_owner)),
                )
            ).fetchone()
        assert authority == (str(signed_owner), "runtime")
        assert conflict == ("quarantined",)
    finally:
        if "backfill_task" in locals() and not backfill_task.done():
            backfill_task.cancel()
            await asyncio.gather(backfill_task, return_exceptions=True)
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM profile_session_activity WHERE session_id=%s", (session_id,)
            )
            await conn.execute(
                "DELETE FROM chat_session_migration_conflicts WHERE session_id=%s",
                (session_id,),
            )


async def test_signed_touch_resolves_quarantine_before_gc_discards_other_owner(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-late-owner"
    owner = str(uuid.uuid4().int % 1_000_000_000 + 1_000_000_000)
    other = str(int(owner) + 1)
    settings = get_settings().model_copy(update={"session_lifecycle_gc_batch_size": 10})
    monkeypatch.setattr(session_context_module, "get_settings", lambda: settings)
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_session_migrations
                (migration_name, rollout_started_at, grace_deadline,
                 profile_backfill_completed_at)
            VALUES ('issue-187-session-context', now()-interval '2 days',
                    now()-interval '1 day', now()-interval '1 day')
            ON CONFLICT (migration_name) DO UPDATE
            SET grace_deadline=EXCLUDED.grace_deadline,
                profile_backfill_completed_at=EXCLUDED.profile_backfill_completed_at,
                gc_completed_at=NULL
            """
        )
        async with conn.cursor() as cursor:
            await cursor.executemany(
                """
                INSERT INTO chat_session_migration_conflicts
                    (session_id, owner_id, legacy_status, legacy_last_activity_at)
                VALUES (%s, %s, 'active', now()-interval '2 days')
                """,
                ((session_id, owner), (session_id, other)),
            )
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        owner_key = f"{owner}:{session_id}"
        other_key = f"{other}:{session_id}"
        try:
            await store.aput(("session_ctx", owner_key), "buffer", {"items": [[1, "keep"]]})
            await store.aput(("session_ctx", other_key), "buffer", {"items": [[1, "drop"]]})
            await _expire_legacy_quiet(pool)

            context = await repo.touch(
                BuyerSessionInput(session_id, "signed-thread", "member", owner)
            )
            await buyer_session_state.run_legacy_gc_batch()

            async with pool.connection() as conn:
                conflicts = await (
                    await conn.execute(
                        "SELECT owner_id, resolution_status, resolved_context_id "
                        "FROM chat_session_migration_conflicts WHERE session_id=%s "
                        "ORDER BY owner_id",
                        (session_id,),
                    )
                ).fetchall()
            assert conflicts == [
                (owner, "resolved", uuid.UUID(context.context_id)),
                (other, "discarded", None),
            ]
            assert await store.aget(("session_ctx", owner_key), "buffer") is not None
            assert await store.aget(("session_ctx", other_key), "buffer") is None
            assert (await repo.get_context(session_id)).owner_id == owner
        finally:
            await store.adelete(("session_ctx", owner_key), "buffer")
            await store.adelete(("session_ctx", other_key), "buffer")
            pg_store_module.reset_store()
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migration_conflicts WHERE session_id=%s",
            (session_id,),
        )


async def test_busy_first_conflict_does_not_starve_next_and_cursor_survives_restart(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    first_session = prefix + "-conflict-busy"
    second_session = prefix + "-conflict-next"
    first_owner = str(uuid.uuid4().int % 1_000_000_000 + 1_000_000_000)
    second_owner = str(int(first_owner) + 1)
    settings = get_settings().model_copy(update={"session_lifecycle_gc_batch_size": 1})
    monkeypatch.setattr(session_context_module, "get_settings", lambda: settings)
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_session_migrations
                (migration_name, rollout_started_at, grace_deadline,
                 profile_backfill_completed_at, gc_completed_at)
            VALUES ('issue-187-session-context', now()-interval '2 days',
                    now()-interval '1 day', now()-interval '1 day', NULL)
            ON CONFLICT (migration_name) DO UPDATE
            SET grace_deadline=EXCLUDED.grace_deadline,
                profile_backfill_completed_at=EXCLUDED.profile_backfill_completed_at,
                gc_completed_at=NULL,
                legacy_quiet_until=now()-interval '1 second',
                conflict_gc_cursor=0
            """
        )
        async with conn.cursor() as cursor:
            await cursor.executemany(
                """
                INSERT INTO chat_session_migration_conflicts
                    (session_id, owner_id, legacy_status, legacy_last_activity_at)
                VALUES (%s, %s, 'active', now()-interval '2 days')
                """,
                ((first_session, first_owner), (second_session, second_owner)),
            )
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        first_key = f"{first_owner}:{first_session}"
        second_key = f"{second_owner}:{second_session}"
        await store.aput(("session_ctx", first_key), "buffer", {"owner": "first"})
        await store.aput(("session_ctx", second_key), "buffer", {"owner": "second"})
        await _expire_legacy_quiet(pool)
        try:
            async with pool.connection() as blocker:
                async with blocker.transaction():
                    await blocker.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended('chat-session:' || %s, 0))",
                        (first_session,),
                    )
                    await buyer_session_state.run_legacy_gc_batch()
                    async with pool.connection() as observer:
                        rows = await (
                            await observer.execute(
                                "SELECT conflict_id, session_id, resolution_status "
                                "FROM chat_session_migration_conflicts "
                                "WHERE session_id = ANY(%s) ORDER BY conflict_id",
                                ([first_session, second_session],),
                            )
                        ).fetchall()
                        cursor = await (
                            await observer.execute(
                                "SELECT conflict_gc_cursor "
                                "FROM chat_session_migrations "
                                "WHERE migration_name='issue-187-session-context'"
                            )
                        ).fetchone()
                    assert [(row[1], row[2]) for row in rows] == [
                        (first_session, "quarantined"),
                        (second_session, "discarded"),
                    ]
                    assert cursor == (rows[1][0],)
                    assert await store.aget(("session_ctx", second_key), "buffer") is None

            restarted = SessionContextRepository(pool=pool)
            monkeypatch.setattr(session_context_module, "_default_repository", restarted)
            for _ in range(10):
                await buyer_session_state.run_legacy_gc_batch()
                async with pool.connection() as conn:
                    first_status = await (
                        await conn.execute(
                            "SELECT resolution_status "
                            "FROM chat_session_migration_conflicts "
                            "WHERE session_id=%s",
                            (first_session,),
                        )
                    ).fetchone()
                    completed = await (
                        await conn.execute(
                            "SELECT gc_completed_at IS NOT NULL "
                            "FROM chat_session_migrations "
                            "WHERE migration_name='issue-187-session-context'"
                        )
                    ).fetchone()
                if first_status == ("discarded",) and completed == (True,):
                    break
            assert first_status == ("discarded",)
            assert completed == (True,)
            assert await store.aget(("session_ctx", first_key), "buffer") is None
        finally:
            await store.adelete(("session_ctx", first_key), "buffer")
            await store.adelete(("session_ctx", second_key), "buffer")
            pg_store_module.reset_store()
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migration_conflicts WHERE session_id = ANY(%s)",
            ([first_session, second_session],),
        )


async def test_gc_rechecks_quarantine_after_late_authoritative_touch(pg_repo, monkeypatch) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-gc-touch-race"
    owner = str(uuid.uuid4().int % 1_000_000_000 + 1_000_000_000)
    settings = get_settings().model_copy(update={"session_lifecycle_gc_batch_size": 10})
    monkeypatch.setattr(session_context_module, "get_settings", lambda: settings)
    monkeypatch.setattr(session_context_module, "_default_repository", repo)
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_session_migrations
                (migration_name, rollout_started_at, grace_deadline,
                 profile_backfill_completed_at)
            VALUES ('issue-187-session-context', now()-interval '2 days',
                    now()-interval '1 day', now()-interval '1 day')
            ON CONFLICT (migration_name) DO UPDATE
            SET grace_deadline=EXCLUDED.grace_deadline,
                profile_backfill_completed_at=EXCLUDED.profile_backfill_completed_at,
                gc_completed_at=NULL
            """
        )
        await conn.execute(
            """
            INSERT INTO chat_session_migration_conflicts
                (session_id, owner_id, legacy_status, legacy_last_activity_at)
            VALUES (%s, %s, 'active', now()-interval '2 days')
            """,
            (session_id, owner),
        )

    selected = asyncio.Event()
    resume_gc = asyncio.Event()
    original_try_lock = buyer_session_state._try_session_lock_for_gc

    async def paused_try_lock(conn, locked_session_id: str):  # noqa: ANN001
        selected.set()
        await resume_gc.wait()
        return await original_try_lock(conn, locked_session_id)

    monkeypatch.setattr(buyer_session_state, "_try_session_lock_for_gc", paused_try_lock)
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        pg_store_module.set_store(store)
        key = f"{owner}:{session_id}"
        try:
            await store.aput(("session_ctx", key), "buffer", {"items": [[1, "keep"]]})
            await _expire_legacy_quiet(pool)
            gc_task = asyncio.create_task(buyer_session_state.run_legacy_gc_batch())
            await asyncio.wait_for(selected.wait(), timeout=2)

            context = await repo.touch(
                BuyerSessionInput(session_id, "signed-thread", "member", owner)
            )
            resume_gc.set()
            await asyncio.wait_for(gc_task, timeout=2)

            async with pool.connection() as conn:
                conflict = await (
                    await conn.execute(
                        "SELECT resolution_status, resolved_context_id "
                        "FROM chat_session_migration_conflicts "
                        "WHERE session_id=%s AND owner_id=%s",
                        (session_id, owner),
                    )
                ).fetchone()
            assert conflict == ("resolved", uuid.UUID(context.context_id))
            assert await store.aget(("session_ctx", key), "buffer") is not None
        finally:
            resume_gc.set()
            if "gc_task" in locals() and not gc_task.done():
                gc_task.cancel()
                await asyncio.gather(gc_task, return_exceptions=True)
            await store.adelete(("session_ctx", key), "buffer")
            pg_store_module.reset_store()
    async with pool.connection() as conn:
        await conn.execute(
            "DELETE FROM chat_session_migration_conflicts WHERE session_id=%s",
            (session_id,),
        )


async def test_member_claim_resolves_matching_quarantined_owner(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-claim-release"
    member_id = uuid.uuid4().int % 1_000_000_000 + 1_000_000_000
    guest_id = "guest-before-claim"
    await repo.touch(BuyerSessionInput(session_id, "guest-thread", "guest", guest_id))
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_session_migration_conflicts
                (session_id, owner_id, legacy_status, legacy_last_activity_at)
            VALUES (%s, %s, 'active', now()-interval '2 days')
            """,
            (session_id, str(member_id)),
        )
    try:
        outcome = await repo.claim_owner(session_id, guest_id, member_id)
        async with pool.connection() as conn:
            conflict = await (
                await conn.execute(
                    "SELECT resolution_status, resolved_context_id "
                    "FROM chat_session_migration_conflicts "
                    "WHERE session_id=%s AND owner_id=%s",
                    (session_id, str(member_id)),
                )
            ).fetchone()
        assert outcome.claimed is True
        assert conflict == ("resolved", uuid.UUID(outcome.context.context_id))
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM chat_session_migration_conflicts WHERE session_id=%s",
                (session_id,),
            )


async def test_brand_new_database_init_order_creates_lifecycle_schema() -> None:
    dsn = get_settings().profile_db_url
    schema = "it_new_" + uuid.uuid4().hex
    admin = AsyncConnectionPool(dsn, open=False)
    await admin.open(wait=True)
    try:
        async with admin.connection() as conn:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(f'SET search_path TO "{schema}"')
            await conn.execute(
                open("db/profile/init/01_conversation_turns.sql", encoding="utf-8").read()
            )
            await conn.execute(
                open("db/profile/init/03_chat_session_contexts.sql", encoding="utf-8").read()
            )
            tables = await (
                await conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
                    (schema,),
                )
            ).fetchall()
        assert "chat_session_contexts" in {row[0] for row in tables}
        assert "chat_session_finalizations" in {row[0] for row in tables}
    finally:
        async with admin.connection() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def test_schema_upgrade_classifies_existing_authority_and_restores_default() -> None:
    dsn = get_settings().profile_db_url
    schema = "it_upgrade_" + uuid.uuid4().hex
    pool = AsyncConnectionPool(dsn, open=False)
    await pool.open(wait=True)
    runtime_thread = str(uuid.uuid4())
    runtime_claim = str(uuid.uuid4())
    legacy = str(uuid.uuid4())
    try:
        async with pool.connection() as conn:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(f'SET search_path TO "{schema}"')
            await conn.execute(
                open("db/profile/init/01_conversation_turns.sql", encoding="utf-8").read()
            )
            lifecycle_sql = open(
                "db/profile/init/03_chat_session_contexts.sql", encoding="utf-8"
            ).read()
            await conn.execute(lifecycle_sql)
            await conn.execute(
                "ALTER TABLE chat_session_contexts DROP COLUMN authority_source CASCADE"
            )
            async with conn.cursor() as cursor:
                await cursor.executemany(
                    "INSERT INTO chat_session_contexts "
                    "(context_id, session_id, owner_type, owner_id, state) "
                    "VALUES (%s, %s, 'guest', %s, 'active')",
                    (
                        (runtime_thread, "runtime-thread", "guest-thread"),
                        (runtime_claim, "runtime-claim", "guest-claim"),
                        (legacy, "legacy-only", "legacy-owner"),
                    ),
                )
            await conn.execute(
                "INSERT INTO chat_session_threads (context_id, thread_id) VALUES (%s, 'thread')",
                (runtime_thread,),
            )
            await conn.execute(
                """
                INSERT INTO chat_session_owner_claims
                    (claim_id, context_id, session_id, from_owner_type, from_owner_id,
                     to_owner_type, to_owner_id)
                VALUES (%s, %s, 'runtime-claim', 'guest', 'guest-claim',
                        'member', '42')
                """,
                (str(uuid.uuid4()), runtime_claim),
            )

            await conn.execute(lifecycle_sql)

            rows = await (
                await conn.execute(
                    "SELECT session_id, authority_source FROM chat_session_contexts "
                    "ORDER BY session_id"
                )
            ).fetchall()
            column = await (
                await conn.execute(
                    "SELECT is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='chat_session_contexts' "
                    "AND column_name='authority_source'",
                    (schema,),
                )
            ).fetchone()
        assert rows == [
            ("legacy-only", "legacy_backfill"),
            ("runtime-claim", "runtime"),
            ("runtime-thread", "runtime"),
        ]
        assert column[0] == "NO"
        assert "runtime" in column[1]
    finally:
        async with pool.connection() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await pool.close()


async def test_initialize_upgrades_pre_187_conversation_turns() -> None:
    dsn = get_settings().profile_db_url
    schema = "it_old_" + uuid.uuid4().hex
    admin = AsyncConnectionPool(dsn, open=False)
    await admin.open(wait=True)
    try:
        async with admin.connection() as conn:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(
                f'CREATE TABLE "{schema}".conversation_turns (turn_id text PRIMARY KEY)'
            )
        scoped_pool = AsyncConnectionPool(
            make_conninfo(dsn, options=f"-c search_path={schema}"), open=False
        )
        await scoped_pool.open(wait=True)
        try:
            repo = SessionContextRepository(pool=scoped_pool)
            await repo.initialize()
            await repo.initialize()
            async with scoped_pool.connection() as conn:
                columns = await (
                    await conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name='conversation_turns'",
                        (schema,),
                    )
                ).fetchall()
                constraint = await (
                    await conn.execute(
                        "SELECT 1 FROM pg_constraint "
                        "WHERE conname='conversation_turns_context_fk' "
                        "AND conrelid='conversation_turns'::regclass"
                    )
                ).fetchone()
            assert {"context_id", "session_id"} <= {row[0] for row in columns}
            assert constraint == (1,)
        finally:
            await scoped_pool.close()
    finally:
        async with admin.connection() as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def test_touch_preserves_completed_idle_profile_candidate(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-profile"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    claim = await _claim_own(repo, session_id)
    async with repo.lock_session(session_id) as uow:
        await uow.prepare_idle_finalizing(claim)
        pending = await (
            await uow.conn.execute(
                "SELECT watermark_status, profile_watermark "
                "FROM chat_session_finalizations WHERE finalization_id=%s",
                (claim.finalization_id,),
            )
        ).fetchone()
        assert pending == ("pending", None)
        await uow.capture_profile_watermark(claim, 0)
        await uow.complete_idle_delete(claim)
    await repo.record_profile_phase(claim.finalization_id, "retryable")

    touched = await repo.touch(BuyerSessionInput(session_id, "T2", "member", "7"))
    candidates = await repo.list_recoverable_profile_phases(100)

    assert touched.generation == claim.generation + 1
    candidate = next(item for item in candidates if item.finalization_id == claim.finalization_id)
    assert candidate.profile_watermark == 0


async def test_terminal_supersedes_previous_generation_completed_idle(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-terminal-supersede"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    idle = await _claim_own(repo, session_id)
    async with repo.lock_session(session_id) as uow:
        await uow.prepare_idle_finalizing(idle)
        await uow.capture_profile_watermark(idle, 0)
        await uow.complete_idle_delete(idle)
    await repo.record_profile_phase(idle.finalization_id, "retryable")
    touched = await repo.touch(BuyerSessionInput(session_id, "T2", "member", "7"))
    assert touched.generation == idle.generation + 1
    assert any(
        item.finalization_id == idle.finalization_id
        for item in await repo.list_recoverable_profile_phases(100)
    )

    terminal = await repo.begin_terminal(7, session_id)

    assert terminal.claim is not None
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT status, claim_token, lease_expires_at FROM chat_session_finalizations "
                "WHERE finalization_id=%s",
                (idle.finalization_id,),
            )
        ).fetchone()
    assert row == ("superseded", None, None)
    assert all(
        item.finalization_id != idle.finalization_id
        for item in await repo.list_recoverable_profile_phases(100)
    )


async def test_pg_begin_terminal_commit_failure_rolls_back_all_terminal_mutation(pg_repo) -> None:
    """terminal context/journal mutation 뒤 실제 PG commit 실패는 전부 rollback한다."""
    repo, pool, prefix = pg_repo
    session_id = prefix + "-terminal-commit-rollback"
    before = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    fault_repo = SessionContextRepository(pool=_CommitFaultPool(pool))

    with pytest.raises(ForeignKeyViolation):
        await fault_repo.begin_terminal(7, session_id)

    after = await repo.get_context(session_id)
    assert after == before
    async with pool.connection() as conn:
        terminal_rows = await (
            await conn.execute(
                """
                SELECT count(*)
                FROM chat_session_finalizations
                WHERE context_id=%s AND reason='terminal'
                """,
                (before.context_id,),
            )
        ).fetchone()
    assert terminal_rows == (0,)


async def test_pg_i20_supersedes_idle_between_phase_a_and_phase_b(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-i20-phase-barrier"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    idle = await _claim_own(repo, session_id)
    async with repo.lock_session(session_id) as uow:
        await uow.prepare_idle_finalizing_with_watermark(idle, 0)

    terminal = await repo.begin_terminal(7, session_id)

    assert terminal.context.state == "terminal"
    assert terminal.finalization.supersedes_finalization_id == idle.finalization_id
    assert terminal.finalization.watermark_status == "captured"
    assert terminal.finalization.profile_watermark == 0
    assert terminal.finalization.transient_status == "pending"
    old = await repo.get_finalization(idle.finalization_id)
    assert old.status == "superseded"
    assert old.claim_token is None and old.lease_expires_at is None
    assert old.superseded_at is not None
    with pytest.raises(SessionClaimConflict):
        await repo.validate_for_delete(idle)
    assert await repo.get_threads(context.context_id) == ["T1"]


async def test_pg_i20_gates_before_waiting_for_active_stream(pg_repo, monkeypatch) -> None:
    repo, _, prefix = pg_repo
    session_id = prefix + "-i20-active-stream"
    await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    registry = ActiveStreamRegistry()
    assert await registry.acquire("stream-1", owner_id="7", session_id=session_id)
    snapshot_started = asyncio.Event()

    class _SnapshotStore:
        async def get_session_ctx_snapshot(self, key: str):
            snapshot_started.set()
            return [], None

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr(session_lifecycle_module.session_state, "clear_context", clear_context)
    coordinator = SessionLifecycleCoordinator(
        repo,
        registry,
        profile_store_factory=lambda: _SnapshotStore(),
    )

    task = asyncio.create_task(coordinator.begin_terminal(7, session_id))
    for _ in range(100):
        context = await repo.get_context(session_id)
        if context is not None and context.state == "terminal":
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("terminal gate was not persisted before stream wait")

    assert not snapshot_started.is_set()
    assert not task.done()
    await registry.release("stream-1")
    outcome = await task

    assert snapshot_started.is_set()
    journal = await repo.get_finalization(outcome.finalization.finalization_id)
    assert journal.transient_status == "completed"
    assert (await coordinator.begin_terminal(7, session_id)).duplicate is True


async def test_pg_expired_processing_profile_rotates_token(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-expired-profile"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    idle = await _claim_own(repo, session_id)
    async with repo.lock_session(session_id) as uow:
        await uow.prepare_idle_finalizing_with_watermark(idle, 0)
        await uow.complete_idle_delete(idle)
    first = await repo.claim_profile_phase(idle.finalization_id, 30)
    assert first is not None
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (idle.finalization_id,),
        )

    candidates = await repo.list_recoverable_profile_phases(100)
    assert idle.finalization_id in {candidate.finalization_id for candidate in candidates}
    recovered = await repo.claim_profile_phase(idle.finalization_id, 30)

    assert recovered is not None
    assert recovered.claim_token != first.claim_token
    with pytest.raises(SessionClaimConflict):
        await repo.record_claimed_profile_phase(
            idle.finalization_id,
            first.claim_token,
            "completed",
        )
    assert (await repo.get_finalization(idle.finalization_id)).claim_token == (
        recovered.claim_token
    )


async def test_pg_terminal_expired_profile_lease_recovers_with_new_token(
    pg_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-terminal-expired-profile"
    await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    profile_store = await get_profile_store()
    await profile_store.append_session_ctx(conversation_key("7", session_id), "PG terminal 취향")

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr(session_lifecycle_module.session_state, "clear_context", clear_context)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    terminal = await repo.begin_terminal(7, session_id)
    assert terminal.claim is not None
    transient = await coordinator.process_terminal_transient(terminal.claim)
    assert transient.status == "completed"
    old_claim = await repo.claim_profile_phase(terminal.finalization.finalization_id, 30)
    assert old_claim is not None
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (terminal.finalization.finalization_id,),
        )

    candidates = await repo.list_recoverable_profile_phases(100)
    assert terminal.finalization.finalization_id in {
        candidate.finalization_id for candidate in candidates
    }
    entered = asyncio.Event()
    proceed = asyncio.Event()

    class _BlockingLLM(_ProfileLLM):
        async def complete(self, **kwargs):
            if "델타 추출기" in kwargs["system"]:
                entered.set()
                await proceed.wait()
            return await super().complete(**kwargs)

    issued_tokens: list[str] = []
    original_claim = repo.claim_profile_phase

    async def observed_claim(finalization_id: str, lease_s: float):
        claimed = await original_claim(finalization_id, lease_s)
        if claimed is not None:
            issued_tokens.append(claimed.claim_token)
        return claimed

    monkeypatch.setattr(repo, "claim_profile_phase", observed_claim)
    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _BlockingLLM())
    recovery = asyncio.create_task(profile_finalizer._process_recoverable_profile_phases(repo, 1))
    await entered.wait()

    assert issued_tokens and issued_tokens[-1] != old_claim.claim_token
    with pytest.raises(SessionClaimConflict):
        await repo.record_claimed_profile_phase(
            terminal.finalization.finalization_id,
            old_claim.claim_token,
            "completed",
        )
    with pytest.raises(SessionClaimConflict):
        await repo.record_profile_phase(terminal.finalization.finalization_id, "completed")

    proceed.set()
    [result] = await recovery
    assert result.status is profile_finalizer.ProfilePhaseStatus.COMPLETED
    journal = await repo.get_finalization(terminal.finalization.finalization_id)
    context = await repo.get_context(session_id)
    assert journal.reason == "terminal"
    assert journal.transient_status == "completed"
    assert journal.profile_status == "completed"
    assert journal.claim_token is None
    assert context is not None and context.state == "terminal"


async def test_pg_expired_live_profile_task_is_joined_without_parallel_llm(
    pg_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-live-expired-profile"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    profile_store = await get_profile_store()
    await profile_store.append_session_ctx(conversation_key("7", session_id), "PG 장기 취향")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    idle = await _claim_own(repo, session_id)

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr(session_lifecycle_module.session_state, "clear_context", clear_context)
    transient = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
    ).process_idle_transient(idle)
    assert transient.status == "completed"
    entered = asyncio.Event()
    proceed = asyncio.Event()
    second_joined = asyncio.Event()
    active = 0
    peak = 0
    join_calls = 0
    original_join = profile_finalizer._active_profile_tasks.join_or_start

    async def observed_join(*args, **kwargs):
        nonlocal join_calls
        join_calls += 1
        if join_calls == 2:
            second_joined.set()
        return await original_join(*args, **kwargs)

    class _BlockingLLM(_ProfileLLM):
        async def complete(self, **kwargs):
            nonlocal active, peak
            if "델타 추출기" in kwargs["system"]:
                active += 1
                peak = max(peak, active)
                entered.set()
                await proceed.wait()
                active -= 1
            return await super().complete(**kwargs)

    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _BlockingLLM())
    monkeypatch.setattr(
        profile_finalizer._active_profile_tasks,
        "join_or_start",
        observed_join,
    )
    first = asyncio.create_task(profile_finalizer._process_recoverable_profile_phases(repo, 1))
    await entered.wait()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (idle.finalization_id,),
        )
    second = asyncio.create_task(profile_finalizer._process_recoverable_profile_phases(repo, 1))
    await second_joined.wait()

    assert not second.done()
    assert peak == 1
    proceed.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert peak == 1
    assert first_result == second_result
    assert (await repo.get_finalization(idle.finalization_id)).profile_status == "completed"


async def test_pg_orphaned_processing_profile_public_recovery_completes(
    pg_repo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-orphaned-profile"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    profile_store = await get_profile_store()
    await profile_store.append_session_ctx(conversation_key("7", session_id), "PG orphan 취향")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    idle = await _claim_own(repo, session_id)

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr(session_lifecycle_module.session_state, "clear_context", clear_context)
    transient = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
    ).process_idle_transient(idle)
    assert transient.status == "completed"
    first = await repo.claim_profile_phase(idle.finalization_id, 30)
    assert first is not None
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (idle.finalization_id,),
        )
    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: _ProfileLLM())

    [result] = await profile_finalizer._process_recoverable_profile_phases(repo, 1)

    assert result.status is profile_finalizer.ProfilePhaseStatus.COMPLETED
    completed = await repo.get_finalization(idle.finalization_id)
    assert completed.profile_status == "completed"
    assert completed.claim_token is None


async def test_terminal_duplicate_and_expired_reissue_are_atomic(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-terminal"
    await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    first = await repo.begin_terminal(7, session_id)
    assert first.claim is not None
    duplicate = await repo.begin_terminal(7, session_id)
    assert duplicate.claim is None
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (first.claim.finalization_id,),
        )

    a, b = await asyncio.gather(
        repo.begin_terminal(7, session_id), repo.begin_terminal(7, session_id)
    )
    reissued = [outcome.claim for outcome in (a, b) if outcome.claim is not None]
    assert len(reissued) == 1
    assert reissued[0].finalization_id == first.claim.finalization_id
    assert reissued[0].claim_token != first.claim.claim_token
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT claim_token, generation, transient_status "
                "FROM chat_session_finalizations WHERE finalization_id=%s",
                (first.claim.finalization_id,),
            )
        ).fetchone()
    assert row == (reissued[0].claim_token, first.claim.generation, "pending")


async def test_pg_unit_of_work_rejects_other_session_claim(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    for suffix in ("a", "b"):
        context = await repo.touch(BuyerSessionInput(prefix + suffix, "T1", "guest", "G" + suffix))
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
                "WHERE context_id=%s",
                (context.context_id,),
            )
    claim_a = await _claim_own(repo, prefix + "a")
    async with repo.lock_session(prefix + "b") as uow:
        with pytest.raises(SessionClaimConflict):
            await uow.prepare_idle_finalizing(claim_a)


async def test_touch_serializes_after_idle_claim_and_rejects_stale_claim(
    pg_repo, monkeypatch
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-touch-race"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    lock_acquired = asyncio.Event()
    release_touch = asyncio.Event()
    original_lock = session_context_module._advisory_lock

    async def blocking_lock(conn, locked_session_id):
        await original_lock(conn, locked_session_id)
        if locked_session_id == session_id:
            lock_acquired.set()
            await release_touch.wait()

    monkeypatch.setattr(session_context_module, "_advisory_lock", blocking_lock)
    touch_task = asyncio.create_task(repo.touch(BuyerSessionInput(session_id, "T2", "guest", "G1")))
    await lock_acquired.wait()
    claim = await _claim_own(repo, session_id)
    release_touch.set()
    touched = await touch_task

    assert touched.generation == claim.generation + 1
    with pytest.raises(SessionClaimConflict):
        await repo.validate_for_delete(claim)
    async with pool.connection() as conn:
        context_row = await (
            await conn.execute(
                "SELECT generation, state FROM chat_session_contexts WHERE context_id=%s",
                (context.context_id,),
            )
        ).fetchone()
        finalization_row = await (
            await conn.execute(
                "SELECT status, claim_token, lease_expires_at "
                "FROM chat_session_finalizations WHERE finalization_id=%s",
                (claim.finalization_id,),
            )
        ).fetchone()
    assert context_row == (claim.generation + 1, "active")
    assert finalization_row == ("superseded", None, None)


async def test_competing_owner_claims_have_one_winner_and_one_history(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-owner-race"
    await repo.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    start = asyncio.Event()

    async def compete(user_id: int):
        await start.wait()
        try:
            return await repo.claim_owner(session_id, "G1", user_id)
        except SessionClaimConflict as exc:
            return exc

    tasks = [asyncio.create_task(compete(user_id)) for user_id in (7, 8)]
    start.set()
    outcomes = await asyncio.gather(*tasks)
    winners = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, SessionClaimConflict)]
    assert len(winners) == len(losers) == 1
    async with pool.connection() as conn:
        context_row = await (
            await conn.execute(
                "SELECT owner_type, owner_id, generation FROM chat_session_contexts "
                "WHERE session_id=%s",
                (session_id,),
            )
        ).fetchone()
        histories = await (
            await conn.execute(
                "SELECT from_owner_id, to_owner_id FROM chat_session_owner_claims "
                "WHERE session_id=%s",
                (session_id,),
            )
        ).fetchall()
    assert context_row == ("member", winners[0].context.owner_id, 1)
    assert histories == [("G1", winners[0].context.owner_id)]


async def test_recoverable_finalization_competition_has_single_token_winner(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-recover-race"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    first = await _claim_own(repo, session_id)
    await repo.mark_idle_finalizing(first)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (first.finalization_id,),
        )
    start = asyncio.Event()

    async def recover():
        await start.wait()
        return await repo.claim_recoverable_finalizations(30, 1)

    tasks = [asyncio.create_task(recover()) for _ in range(2)]
    start.set()
    results = await asyncio.gather(*tasks)
    claims = [claim for result in results for claim in result]
    assert len(claims) == 1
    recovered = claims[0]
    assert recovered.claim_token != first.claim_token
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT status, claim_token, lease_expires_at > now(), generation "
                "FROM chat_session_finalizations WHERE finalization_id=%s",
                (first.finalization_id,),
            )
        ).fetchone()
    assert row == ("processing", recovered.claim_token, True, first.generation)


async def test_profile_list_then_competing_claim_has_single_cas_winner(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-profile-cas"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    idle = await _claim_own(repo, session_id)
    async with repo.lock_session(session_id) as uow:
        await uow.prepare_idle_finalizing(idle)
        await uow.capture_profile_watermark(idle, 0)
        await uow.complete_idle_delete(idle)
    [candidate] = [
        item
        for item in await repo.list_recoverable_profile_phases(100)
        if item.finalization_id == idle.finalization_id
    ]
    start = asyncio.Event()

    async def claim_profile():
        await start.wait()
        return await repo.claim_profile_phase(candidate.finalization_id, 30)

    tasks = [asyncio.create_task(claim_profile()) for _ in range(2)]
    start.set()
    outcomes = await asyncio.gather(*tasks)
    winners = [outcome for outcome in outcomes if outcome is not None]
    assert len(winners) == 1
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT profile_status, claim_token, lease_expires_at > now(), generation "
                "FROM chat_session_finalizations WHERE finalization_id=%s",
                (idle.finalization_id,),
            )
        ).fetchone()
    assert row == ("processing", winners[0].claim_token, True, idle.generation)
    assert await repo.is_profile_phase_recoverable(idle.finalization_id) is False
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (idle.finalization_id,),
        )
    assert await repo.is_profile_phase_recoverable(idle.finalization_id) is True


async def test_advisory_lock_is_held_until_transaction_exit(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-lock-duration"
    await repo.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    async with pool.connection() as holder:
        async with holder.transaction():
            await session_context_module._advisory_lock(holder, session_id)
            mutation = asyncio.create_task(
                repo.touch(BuyerSessionInput(session_id, "T2", "guest", "G1"))
            )
            await asyncio.sleep(0.05)
            assert mutation.done() is False
            async with pool.connection() as observer:
                [locked] = await (
                    await observer.execute(
                        "SELECT pg_try_advisory_xact_lock("
                        "hashtextextended('chat-session:' || %s, 0))",
                        (session_id,),
                    )
                ).fetchone()
            assert locked is False
        touched = await asyncio.wait_for(mutation, timeout=1)
    assert touched.state == "active"
    async with pool.connection() as conn:
        context_row = await (
            await conn.execute(
                "SELECT generation, state FROM chat_session_contexts WHERE context_id=%s",
                (touched.context_id,),
            )
        ).fetchone()
        thread_rows = await (
            await conn.execute(
                "SELECT thread_id FROM chat_session_threads WHERE context_id=%s ORDER BY thread_id",
                (touched.context_id,),
            )
        ).fetchall()
    assert context_row == (touched.generation, "active")
    assert thread_rows == [("T1",), ("T2",)]


async def test_cleanup_crash_keeps_gate_and_public_sweep_recovers(pg_repo, monkeypatch) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-cleanup-crash"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    claim = await _claim_own(repo, session_id)
    calls = 0

    async def crashing_clear(context_id: str, thread_ids: list[str]):
        nonlocal calls
        from app.agents.buyer.session_state import CleanupCounts

        calls += 1
        if calls == 1:
            raise RuntimeError("process crash after namespace delete")
        return CleanupCounts(filters=1)

    monkeypatch.setattr(
        session_lifecycle_module.session_state,
        "clear_context",
        crashing_clear,
    )
    first = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    failed = await first.process_idle_transient(claim)
    assert failed.status == "retryable"

    restarted_repo = SessionContextRepository(pool=pool)
    with pytest.raises(SessionFinalizing):
        await restarted_repo.touch(BuyerSessionInput(session_id, "T2", "guest", "G1"))
    with pytest.raises(SessionFinalizing):
        await restarted_repo.claim_owner(session_id, "G1", 7)

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (claim.finalization_id,),
        )
    monkeypatch.setattr(session_context_module, "_default_repository", restarted_repo)

    result = await session_lifecycle_module.run_session_context_sweep()

    assert result.recovered == 1
    assert result.completed == 1
    assert calls == 2
    recovered = await restarted_repo.get_context(session_id)
    assert recovered is not None and recovered.state == "idle_expired"
    assert await restarted_repo.get_threads(context.context_id) == []


async def test_terminal_recovery_ignores_superseded_idle_batch_row(pg_repo, monkeypatch) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-terminal-recovery"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    idle = await _claim_own(repo, session_id)
    terminal = await repo.begin_terminal(7, session_id)
    assert terminal.claim is not None

    class EmptyProfile:
        def __init__(self) -> None:
            self.snapshot_calls = 0

        async def get_session_ctx_snapshot(self, key: str) -> tuple[list[str], int]:
            self.snapshot_calls += 1
            return [], 0

    profile = EmptyProfile()
    calls = 0

    async def crashing_clear(context_id: str, thread_ids: list[str]):
        nonlocal calls
        from app.agents.buyer.session_state import CleanupCounts

        calls += 1
        if calls == 1:
            raise RuntimeError("terminal process crash")
        return CleanupCounts()

    monkeypatch.setattr(
        session_lifecycle_module.session_state,
        "clear_context",
        crashing_clear,
    )
    first = SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: profile,
    )
    failed = await first.process_terminal_transient(terminal.claim)
    assert failed.status == "retryable"
    assert (await repo.get_context(session_id)).state == "terminal"

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (terminal.claim.finalization_id,),
        )
    restarted_repo = SessionContextRepository(pool=pool)
    monkeypatch.setattr(session_context_module, "_default_repository", restarted_repo)

    async def empty_profile_factory():
        return profile

    monkeypatch.setattr(session_lifecycle_module, "get_profile_store", empty_profile_factory)
    result = await session_lifecycle_module.run_session_context_sweep()

    assert result.recovered == 1
    assert result.completed == 1
    assert (await restarted_repo.get_finalization(idle.finalization_id)).status == "superseded"
    terminal_row = await restarted_repo.get_finalization(terminal.claim.finalization_id)
    assert terminal_row.transient_status == "completed"
    assert (await restarted_repo.get_context(session_id)).state == "terminal"
    assert profile.snapshot_calls == 1


@pytest.mark.parametrize("failure", ["active", "snapshot", "cancel"])
async def test_pg_idle_prephase_failure_is_abandoned_and_fresh_sweep_completes(
    pg_repo,
    monkeypatch,
    failure: str,
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-prephase-" + failure
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    claim = await _claim_own(repo, session_id)
    registry = ActiveStreamRegistry()

    class BrokenProfile:
        async def get_session_ctx_snapshot(self, key: str):
            if failure == "cancel":
                raise asyncio.CancelledError
            raise RuntimeError("snapshot unavailable")

    if failure == "active":
        assert await registry.acquire("active", owner_id="7", session_id=session_id)
    coordinator = SessionLifecycleCoordinator(
        repo,
        registry,
        profile_store_factory=BrokenProfile,
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await coordinator.process_idle_transient(claim)
    else:
        outcome = await coordinator.process_idle_transient(claim)
        assert outcome.status == ("skipped" if failure == "active" else "retryable")
    if failure == "active":
        await registry.release("active")

    assert (await repo.get_context(session_id)).state == "active"
    async with pool.connection() as conn:
        count = await (
            await conn.execute(
                "SELECT count(*) FROM chat_session_finalizations WHERE finalization_id=%s",
                (claim.finalization_id,),
            )
        ).fetchone()
    assert count == (0,)

    restarted = SessionContextRepository(pool=pool)
    monkeypatch.setattr(session_context_module, "_default_repository", restarted)

    async def empty_profile_factory():
        return _EmptyProfile()

    monkeypatch.setattr(session_lifecycle_module, "get_profile_store", empty_profile_factory)
    result = await session_lifecycle_module.run_session_context_sweep()

    assert result.completed == 1
    assert (await restarted.get_context(session_id)).state == "idle_expired"


async def test_pg_actual_partial_idle_delete_recovers_and_preserves_other_context(
    pg_repo,
    monkeypatch,
) -> None:
    repo, pool, prefix = pg_repo
    target = await repo.touch(BuyerSessionInput(prefix + "-target", "T1", "guest", "G1"))
    other = await repo.touch(BuyerSessionInput(prefix + "-other", "T1", "guest", "G2"))
    target_key = context_thread_key(target.context_id, "T1")
    other_key = context_thread_key(other.context_id, "T1")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (target.context_id,),
        )
    claim = await _claim_own(repo, prefix + "-target")

    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        await _seed_v2_state(store, target_key, "target")
        await _seed_v2_state(store, other_key, "other")
        fault_store = _FailAfterFirstDeleteStore(store)
        pg_store_module.set_store(fault_store)
        try:
            failed = await SessionLifecycleCoordinator(
                repo,
                ActiveStreamRegistry(),
            ).process_idle_transient(claim)
            assert failed.status == "retryable"
            assert fault_store.delete_calls == 2
            assert await store.aget(("buyer_thread_filters_v2", target_key), "filters") is None
            assert await store.aget(("buyer_cart_v2", target_key), "pending") is not None
            assert (await repo.get_context(prefix + "-target")).state == "idle_finalizing"
            assert (
                await repo.get_finalization(claim.finalization_id)
            ).transient_status == "pending"
            await _assert_v2_preserved(store, other_key)

            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE chat_session_finalizations "
                    "SET lease_expires_at=now()-interval '1 second' "
                    "WHERE finalization_id=%s",
                    (claim.finalization_id,),
                )
            restarted = SessionContextRepository(pool=pool)
            monkeypatch.setattr(session_context_module, "_default_repository", restarted)
            pg_store_module.set_store(store)

            result = await session_lifecycle_module.run_session_context_sweep()

            assert result.completed == 1
            await _assert_v2_deleted(store, target_key)
            await _assert_v2_preserved(store, other_key)
        finally:
            await _delete_seeded_v2_state(store, target_key)
            await _delete_seeded_v2_state(store, other_key)
            pg_store_module.reset_store()


async def test_pg_actual_partial_terminal_delete_recovers_and_keeps_terminal(
    pg_repo,
    monkeypatch,
) -> None:
    repo, pool, prefix = pg_repo
    target_session = prefix + "-terminal-target"
    target = await repo.touch(BuyerSessionInput(target_session, "T1", "member", "7"))
    other = await repo.touch(BuyerSessionInput(prefix + "-terminal-other", "T1", "guest", "G2"))
    terminal = await repo.begin_terminal(7, target_session)
    assert terminal.claim is not None
    target_key = context_thread_key(target.context_id, "T1")
    other_key = context_thread_key(other.context_id, "T1")

    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        await _seed_v2_state(store, target_key, "target")
        await _seed_v2_state(store, other_key, "other")
        fault_store = _FailAfterFirstDeleteStore(store)
        pg_store_module.set_store(fault_store)
        try:
            failed = await SessionLifecycleCoordinator(
                repo,
                ActiveStreamRegistry(),
                profile_store_factory=_EmptyProfile,
            ).process_terminal_transient(terminal.claim)
            assert failed.status == "retryable"
            assert await store.aget(("buyer_thread_filters_v2", target_key), "filters") is None
            assert await store.aget(("buyer_cart_v2", target_key), "pending") is not None
            assert (await repo.get_context(target_session)).state == "terminal"
            await _assert_v2_preserved(store, other_key)

            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE chat_session_finalizations "
                    "SET lease_expires_at=now()-interval '1 second' "
                    "WHERE finalization_id=%s",
                    (terminal.claim.finalization_id,),
                )
            restarted = SessionContextRepository(pool=pool)
            monkeypatch.setattr(session_context_module, "_default_repository", restarted)
            pg_store_module.set_store(store)

            async def empty_profile_factory():
                return _EmptyProfile()

            monkeypatch.setattr(
                session_lifecycle_module,
                "get_profile_store",
                empty_profile_factory,
            )
            result = await session_lifecycle_module.run_session_context_sweep()

            assert result.completed == 1
            assert (await restarted.get_context(target_session)).state == "terminal"
            await _assert_v2_deleted(store, target_key)
            await _assert_v2_preserved(store, other_key)
        finally:
            await _delete_seeded_v2_state(store, target_key)
            await _delete_seeded_v2_state(store, other_key)
            pg_store_module.reset_store()


async def test_pg_failed_abandon_is_self_healed_by_public_sweep(
    pg_repo,
    monkeypatch,
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-failed-abandon"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    orphan = await _claim_own(repo, session_id)

    class BrokenProfile:
        async def get_session_ctx_snapshot(self, key: str):
            raise RuntimeError("snapshot unavailable")

    async def failed_abandon(self, claim):
        raise RuntimeError("lifecycle delete unavailable")

    monkeypatch.setattr(
        SessionContextUnitOfWork,
        "abandon_idle_prephase",
        failed_abandon,
    )
    outcome = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=BrokenProfile,
    ).process_idle_transient(orphan)
    assert outcome.status == "retryable"
    assert (await repo.get_context(session_id)).state == "active"
    assert (await repo.get_finalization(orphan.finalization_id)).claim_token == (orphan.claim_token)

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (orphan.finalization_id,),
        )
    restarted = SessionContextRepository(pool=pool)
    monkeypatch.setattr(session_context_module, "_default_repository", restarted)

    async def empty_profile_factory():
        return _EmptyProfile()

    monkeypatch.setattr(session_lifecycle_module, "get_profile_store", empty_profile_factory)
    result = await session_lifecycle_module.run_session_context_sweep()

    assert result.completed == 1
    completed = await restarted.get_finalization(orphan.finalization_id)
    assert completed.transient_status == "completed"
    assert (await restarted.get_context(session_id)).state == "idle_expired"


async def test_pg_claim_only_process_loss_is_reissued_by_public_fresh_stage(
    pg_repo,
    monkeypatch,
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-claim-only-loss"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    orphan = await _claim_own(repo, session_id)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (orphan.finalization_id,),
        )

    restarted = SessionContextRepository(pool=pool)
    monkeypatch.setattr(session_context_module, "_default_repository", restarted)
    result = await session_lifecycle_module.run_session_context_sweep()

    assert result.completed == 1
    completed = await restarted.get_finalization(orphan.finalization_id)
    assert completed.transient_status == "completed"
    assert (await restarted.get_context(session_id)).state == "idle_expired"


async def test_pg_abandon_rejects_same_token_with_changed_lease(pg_repo) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-lease-fence"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    stale = await _claim_own(repo, session_id)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations "
            "SET lease_expires_at=lease_expires_at+interval '1 second' "
            "WHERE finalization_id=%s",
            (stale.finalization_id,),
        )

    async with repo.lock_session(session_id) as uow:
        assert await uow.abandon_idle_prephase(stale) is False

    remaining = await repo.get_finalization(stale.finalization_id)
    assert remaining.claim_token == stale.claim_token
    assert remaining.lease_expires_at != stale.lease_expires_at


async def test_pg_concurrent_sweeps_reissue_one_orphan_to_one_fresh_winner(
    pg_repo,
) -> None:
    repo, pool, prefix = pg_repo
    session_id = prefix + "-orphan-race"
    context = await repo.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE context_id=%s",
            (context.context_id,),
        )
    orphan = await _claim_own(repo, session_id)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_finalizations SET lease_expires_at=now()-interval '1 second' "
            "WHERE finalization_id=%s",
            (orphan.finalization_id,),
        )

    first, second = await asyncio.gather(
        repo.claim_expired_contexts(10, 30, 1),
        repo.claim_expired_contexts(10, 30, 1),
    )
    winners = [
        claim
        for batch in (first, second)
        for claim in batch
        if claim.finalization_id == orphan.finalization_id
    ]

    assert len(winners) == 1
    winner = winners[0]
    assert winner.finalization_id == orphan.finalization_id
    assert winner.claim_token != orphan.claim_token
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT finalization_id, claim_token FROM chat_session_finalizations "
                "WHERE context_id=%s AND generation=%s AND reason='idle'",
                (context.context_id, context.generation),
            )
        ).fetchall()
    assert [(str(row[0]), row[1]) for row in rows] == [(winner.finalization_id, winner.claim_token)]


async def test_pg_self_healing_preserves_started_and_superseded_idle_rows(
    pg_repo,
) -> None:
    repo, pool, prefix = pg_repo
    sessions = {
        "captured": prefix + "-captured-protected",
        "superseded": prefix + "-superseded-protected",
        "completed": prefix + "-completed-protected",
    }
    for index, session_id in enumerate(sessions.values()):
        await repo.touch(BuyerSessionInput(session_id, "T1", "member", str(10 + index)))
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE chat_session_contexts SET last_activity_at=now()-interval '1 hour' "
            "WHERE session_id = ANY(%s)",
            (list(sessions.values()),),
        )
    by_session = await _claim_own_many(repo, list(sessions.values()))
    captured = by_session[sessions["captured"]]
    superseded = by_session[sessions["superseded"]]
    completed = by_session[sessions["completed"]]
    async with repo.lock_session(captured.session_id) as uow:
        await uow.capture_profile_watermark(captured, 0)
    async with pool.connection() as conn:
        await conn.execute(
            """
            UPDATE chat_session_finalizations
            SET status=CASE WHEN finalization_id=%s THEN 'superseded' ELSE status END,
                transient_status=CASE
                    WHEN finalization_id=%s THEN 'completed'
                    ELSE transient_status
                END,
                lease_expires_at=now()-interval '1 second'
            WHERE finalization_id = ANY(%s)
            """,
            (
                superseded.finalization_id,
                completed.finalization_id,
                [
                    captured.finalization_id,
                    superseded.finalization_id,
                    completed.finalization_id,
                ],
            ),
        )

    replacements = await _drain_claims(repo)

    assert not set(sessions.values()) & {claim.session_id for claim in replacements}
    assert (await repo.get_finalization(captured.finalization_id)).watermark_status == "captured"
    assert (await repo.get_finalization(superseded.finalization_id)).status == "superseded"
    assert (await repo.get_finalization(completed.finalization_id)).transient_status == "completed"
