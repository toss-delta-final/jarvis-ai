"""buyer 스레드 상태(ThreadFilter/Cart/Revert) AsyncPostgresStore 통합 테스트 (이슈 #33).

`docker compose up -d pg-profile` 로 컨테이너가 떠 있어야 통과한다. 기본 pytest 실행에서는
@pytest.mark.integration 으로 제외된다(pyproject.toml addopts) — 명시적으로
`uv run pytest tests/integration -m integration` 로 실행한다.

InMemoryStore 는 유닛 테스트가 계속 쓰므로 여기서 건드리지 않는다(tests/conftest.py InMemory
격리 컨벤션, test_pg_artifact_store.py 와 동일 원칙 — 실 인프라 테스트는 분리). 키는 매 테스트
uuid 로 발급해 재실행 간 충돌·잔여 데이터 간섭을 피한다(로컬 dev 볼륨은 소모성).
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
import psycopg
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool

from app.agents.buyer import session_state as session_state_module
from app.agents.buyer.cart import state as cart_state
from app.agents.buyer.cart.state import CartStateStore, PendingAdd
from app.agents.buyer.graph import ThreadFilterStore
from app.agents.buyer.recommendation.state import (
    RelaxationOfferStore,
    RepurchaseStore,
    RevertStore,
)
from app.agents.buyer.session_state import (
    adopt_legacy_thread,
    clear_context,
    context_thread_key,
    ensure_thread_adopted,
    run_legacy_gc_batch,
)
from app.core import pg_store as pg_store_module
from app.core import session_context
from app.core.config import get_settings
from app.core.pg_resilience import hardened_pg_conninfo, state_store_pool_config
from app.core.session_context import SessionContext, SessionContextRepository
from app.schemas.spring import CartOption, ProductSearchFilters

pytestmark = pytest.mark.integration


def _key() -> str:
    return f"it:{uuid.uuid4().hex}"


class _ConnectionAdapter:
    def __init__(self, conn) -> None:  # noqa: ANN001
        self._conn = conn

    @asynccontextmanager
    async def connection(self):
        yield self._conn


class _FailLegacyDeleteStore:
    def __init__(self, store) -> None:  # noqa: ANN001
        self._store = store
        self.failed = False

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    async def adelete(self, namespace, key) -> None:  # noqa: ANN001
        if not self.failed and namespace[0] == "buyer_thread_filters":
            self.failed = True
            raise psycopg.OperationalError("legacy delete boundary unavailable")
        await self._store.adelete(namespace, key)


@pytest.fixture
async def pg_store():
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store:
        await store.setup()
        yield store


async def test_thread_filter_store_roundtrip(pg_store) -> None:
    wrapper = ThreadFilterStore(pg_store)
    key = _key()
    await wrapper.put(key, ProductSearchFilters(category="이어폰", price_max=50000))
    fetched = await wrapper.get(key)
    assert fetched is not None
    assert fetched.category == "이어폰"
    assert fetched.price_max == 50000


async def test_thread_filter_store_missing_key_returns_none(pg_store) -> None:
    wrapper = ThreadFilterStore(pg_store)
    assert await wrapper.get(_key()) is None


async def test_cart_state_store_last_reco_roundtrip(pg_store) -> None:
    wrapper = CartStateStore(pg_store)
    key = _key()
    await wrapper.set_last_reco(key, [(101, "이어폰"), (102, "케이스")])
    reco = await wrapper.get_last_reco(key)
    assert reco == [(101, "이어폰"), (102, "케이스")]


async def test_cart_state_store_pending_roundtrip(pg_store) -> None:
    wrapper = CartStateStore(pg_store)
    key = _key()
    pending = PendingAdd(
        product_id=101,
        quantity=2,
        options=[CartOption(option_id=3, name="블루", extra_price=1000)],
        attempts=1,
    )
    await wrapper.set_pending(key, pending)
    fetched = await wrapper.get_pending(key)
    assert fetched is not None
    assert fetched.product_id == 101 and fetched.quantity == 2 and fetched.attempts == 1
    assert fetched.options[0].name == "블루" and fetched.options[0].extra_price == 1000

    await wrapper.clear_pending(key)
    assert await wrapper.get_pending(key) is None


async def test_revert_store_accumulates_categories(pg_store) -> None:
    wrapper = RevertStore(pg_store)
    key = _key()
    await wrapper.add(key, ["조미료"])
    await wrapper.add(key, ["세제"])
    assert await wrapper.get(key) == {"조미료", "세제"}


async def test_context_cleanup_removes_only_selected_v2_thread(pg_store) -> None:
    context_id = uuid.uuid4().hex
    target = context_thread_key(context_id, "target")
    other = context_thread_key(context_id, "other")
    filters = ThreadFilterStore(pg_store)
    cart = CartStateStore(pg_store)
    revert = RevertStore(pg_store)
    repurchase = RepurchaseStore(pg_store)
    relaxation = RelaxationOfferStore(pg_store)
    await filters.put(target, ProductSearchFilters(category="삭제"))
    await filters.put(other, ProductSearchFilters(category="보존"))
    await cart.set_last_reco(target, [(1, "삭제")])
    await cart.set_last_reco(other, [(2, "보존")])
    await cart.set_pending(target, PendingAdd(product_id=1, quantity=1))
    await revert.add(target, ["A"])
    await revert.add(other, ["B"])
    await repurchase.add(target, [1], cap=20)
    await repurchase.add(other, [2], cap=20)
    await relaxation.put(target, {"A칩": {"field": "max_price", "value": 1}}, None)
    await relaxation.put(other, {"B칩": {"field": "max_price", "value": 2}}, None)
    pg_store_module.set_store(pg_store)

    counts = await clear_context(context_id, ["target"])

    assert counts.filters == counts.pending == counts.last_recommendation == 1
    assert counts.local_names == counts.revert == 1
    assert counts.repurchase == counts.relaxation_offers == 1
    assert await filters.get(target) is None
    assert (await filters.get(other)).category == "보존"
    assert await cart.get_last_reco(other) == [(2, "보존")]
    assert await revert.get(other) == {"B"}
    assert await repurchase.get(target) == []
    assert await repurchase.get(other) == [2]
    assert await relaxation.get_snapshot(target) == ({}, None)
    assert await relaxation.get_snapshot(other) == (
        {"B칩": {"field": "max_price", "value": 2}},
        None,
    )


async def test_verified_adoption_marks_complete_after_legacy_keys_are_deleted(pg_store) -> None:
    context_id = str(uuid.uuid4())
    session_id = f"it-session-{uuid.uuid4().hex}"
    thread_id = "thread"
    legacy_owner = f"guest-{uuid.uuid4().hex}"
    legacy_key = f"{legacy_owner}:{thread_id}"
    target_key = context_thread_key(context_id, thread_id)
    conn = pg_store.conn.connection
    await conn.execute(
        """
        INSERT INTO chat_session_contexts
            (context_id, session_id, owner_type, owner_id, state)
        VALUES (%s, %s, 'guest', %s, 'active')
        """,
        (context_id, session_id, legacy_owner),
    )
    await conn.execute(
        "INSERT INTO chat_session_threads (context_id, thread_id) VALUES (%s, %s)",
        (context_id, thread_id),
    )
    await pg_store.aput(
        ("buyer_thread_filters", legacy_key),
        "filters",
        {"category": "legacy"},
    )
    await pg_store.aput(
        ("buyer_revert", legacy_key),
        "categories",
        {"categories": ["A"]},
    )
    await RevertStore(pg_store).add(target_key, ["B"])
    pg_store_module.set_store(pg_store)

    session_context.set_pool(_ConnectionAdapter(conn))
    context = SessionContext(context_id, session_id, "guest", legacy_owner, 0, "active")
    try:
        result = await adopt_legacy_thread(context, thread_id, legacy_owner)

        status = await (
            await conn.execute(
                "SELECT adoption_status FROM chat_session_threads "
                "WHERE context_id=%s AND thread_id=%s",
                (context_id, thread_id),
            )
        ).fetchone()
        assert result.adopted
        assert status["adoption_status"] == "complete"
        assert (await ThreadFilterStore(pg_store).get(target_key)).category == "legacy"
        assert await RevertStore(pg_store).get(target_key) == {"A", "B"}
        assert await pg_store.aget(("buyer_thread_filters", legacy_key), "filters") is None
        assert await pg_store.aget(("buyer_revert", legacy_key), "categories") is None
    finally:
        await conn.execute("DELETE FROM chat_session_contexts WHERE context_id=%s", (context_id,))
        session_context.reset()


async def test_pg_adoption_failure_stays_copying_and_retries_with_new_objects(pg_store) -> None:
    context_id = str(uuid.uuid4())
    session_id = f"it-retry-{uuid.uuid4().hex}"
    thread_id = "thread"
    legacy_owner = f"guest-{uuid.uuid4().hex}"
    legacy_key = f"{legacy_owner}:{thread_id}"
    target_key = context_thread_key(context_id, thread_id)
    conn = pg_store.conn.connection
    await conn.execute(
        "INSERT INTO chat_session_contexts "
        "(context_id, session_id, owner_type, owner_id, state) "
        "VALUES (%s, %s, 'guest', %s, 'active')",
        (context_id, session_id, legacy_owner),
    )
    await conn.execute(
        "INSERT INTO chat_session_threads (context_id, thread_id) VALUES (%s, %s)",
        (context_id, thread_id),
    )
    await pg_store.aput(
        ("buyer_thread_filters", legacy_key),
        "filters",
        {"category": "legacy"},
    )
    context = SessionContext(context_id, session_id, "guest", legacy_owner, 0, "active")
    fault_store = _FailLegacyDeleteStore(pg_store)
    pg_store_module.set_store(fault_store)
    session_context.set_pool(_ConnectionAdapter(conn))
    try:
        with pytest.raises(session_context.SessionStateUnavailable):
            await adopt_legacy_thread(context, thread_id, legacy_owner)

        status = await (
            await conn.execute(
                "SELECT adoption_status FROM chat_session_threads "
                "WHERE context_id=%s AND thread_id=%s",
                (context_id, thread_id),
            )
        ).fetchone()
        assert status["adoption_status"] == "copying"
        assert await pg_store.aget(("buyer_thread_filters", legacy_key), "filters") is not None
        assert (await ThreadFilterStore(pg_store).get(target_key)).category == "legacy"

        async with AsyncPostgresStore.from_conn_string(
            get_settings().profile_db_url
        ) as retry_store:
            await retry_store.setup()
            retry_conn = retry_store.conn.connection
            pg_store_module.set_store(retry_store)
            session_context.set_pool(_ConnectionAdapter(retry_conn))

            result = await adopt_legacy_thread(context, thread_id, legacy_owner)

            retry_status = await (
                await retry_conn.execute(
                    "SELECT adoption_status FROM chat_session_threads "
                    "WHERE context_id=%s AND thread_id=%s",
                    (context_id, thread_id),
                )
            ).fetchone()
            assert result.adopted
            assert retry_status["adoption_status"] == "complete"
            assert await retry_store.aget(("buyer_thread_filters", legacy_key), "filters") is None
    finally:
        await conn.execute("DELETE FROM chat_session_contexts WHERE context_id=%s", (context_id,))
        session_context.reset()


async def test_pg_member_adoption_reads_guest_owner_claim_history(pg_store) -> None:
    context_id = str(uuid.uuid4())
    claim_id = str(uuid.uuid4())
    session_id = f"it-member-{uuid.uuid4().hex}"
    thread_id = "thread"
    guest_owner = f"guest-{uuid.uuid4().hex}"
    member_owner = "42"
    legacy_key = f"{guest_owner}:{thread_id}"
    target_key = context_thread_key(context_id, thread_id)
    conn = pg_store.conn.connection
    await conn.execute(
        "INSERT INTO chat_session_contexts "
        "(context_id, session_id, owner_type, owner_id, generation, state) "
        "VALUES (%s, %s, 'member', %s, 1, 'active')",
        (context_id, session_id, member_owner),
    )
    await conn.execute(
        "INSERT INTO chat_session_threads (context_id, thread_id) VALUES (%s, %s)",
        (context_id, thread_id),
    )
    await conn.execute(
        """
        INSERT INTO chat_session_owner_claims
            (claim_id, context_id, session_id, from_owner_type, from_owner_id,
             to_owner_type, to_owner_id)
        VALUES (%s, %s, %s, 'guest', %s, 'member', %s)
        """,
        (claim_id, context_id, session_id, guest_owner, member_owner),
    )
    await pg_store.aput(
        ("buyer_thread_filters", legacy_key),
        "filters",
        {"category": "guest-state"},
    )
    pg_store_module.set_store(pg_store)
    session_context.set_pool(_ConnectionAdapter(conn))
    try:
        result = await ensure_thread_adopted(context_id, thread_id, member_owner)

        assert result.adopted
        assert (await ThreadFilterStore(pg_store).get(target_key)).category == "guest-state"
        assert await pg_store.aget(("buyer_thread_filters", legacy_key), "filters") is None
    finally:
        await conn.execute("DELETE FROM chat_session_contexts WHERE context_id=%s", (context_id,))
        session_context.reset()


async def test_pg_adoption_fence_blocks_root_gc_after_legacy_read(pg_store, monkeypatch) -> None:
    context_id = str(uuid.uuid4())
    session_id = f"it-fence-adopt-{uuid.uuid4().hex}"
    thread_id = "thread"
    legacy_owner = f"guest-{uuid.uuid4().hex}"
    legacy_key = f"{legacy_owner}:{thread_id}"
    target_key = context_thread_key(context_id, thread_id)
    conn = pg_store.conn.connection
    await conn.execute(
        "INSERT INTO chat_session_contexts "
        "(context_id, session_id, owner_type, owner_id, state) "
        "VALUES (%s, %s, 'guest', %s, 'active')",
        (context_id, session_id, legacy_owner),
    )
    await conn.execute(
        "INSERT INTO chat_session_threads (context_id, thread_id) VALUES (%s, %s)",
        (context_id, thread_id),
    )
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
    await pg_store.aput(("buyer_thread_filters", legacy_key), "filters", {"category": "legacy"})
    await pg_store.aput(
        ("buyer_cart", legacy_key),
        "pending",
        {"product_id": 1, "quantity": 1, "options": [], "attempts": 0},
    )
    await pg_store.aput(("buyer_cart", legacy_key), "last_reco", {"product_ids": [1]})
    await pg_store.aput(
        ("buyer_revert", legacy_key),
        "categories",
        {"categories": ["legacy", "shared"]},
    )
    await RevertStore(pg_store).add(target_key, ["v2", "shared"])
    await conn.execute(
        "UPDATE chat_session_migrations "
        "SET legacy_quiet_until=now()-interval '1 second' "
        "WHERE migration_name='issue-187-session-context'"
    )
    read_legacy = asyncio.Event()
    resume_adoption = asyncio.Event()
    original_item = session_state_module._item

    async def paused_item(store, root, key, name):  # noqa: ANN001
        item = await original_item(store, root, key, name)
        if root == "buyer_thread_filters" and key == legacy_key and name == "filters":
            read_legacy.set()
            await resume_adoption.wait()
        return item

    monkeypatch.setattr(session_state_module, "_item", paused_item)
    pool = AsyncConnectionPool(get_settings().profile_db_url, open=False, min_size=1, max_size=4)
    await pool.open(wait=True)
    pg_store_module.set_store(pg_store)
    session_context.set_pool(pool)
    context = SessionContext(context_id, session_id, "guest", legacy_owner, 0, "active")
    try:
        adoption = asyncio.create_task(adopt_legacy_thread(context, thread_id, legacy_owner))
        await asyncio.wait_for(read_legacy.wait(), timeout=2)
        gc = asyncio.create_task(run_legacy_gc_batch())
        await asyncio.sleep(0.1)
        assert not gc.done()

        resume_adoption.set()
        result = await asyncio.wait_for(adoption, timeout=2)
        await asyncio.wait_for(gc, timeout=2)

        status = await (
            await conn.execute(
                "SELECT adoption_status FROM chat_session_threads "
                "WHERE context_id=%s AND thread_id=%s",
                (context_id, thread_id),
            )
        ).fetchone()
        assert result.adopted and status["adoption_status"] == "complete"
        assert (await ThreadFilterStore(pg_store).get(target_key)).category == "legacy"
        pending = await CartStateStore(pg_store).get_pending(target_key)
        assert pending is not None and pending.product_id == 1
        assert await CartStateStore(pg_store).get_last_reco(target_key) == [(1, "")]
        assert await RevertStore(pg_store).get(target_key) == {"legacy", "shared", "v2"}
        for root, name in (
            ("buyer_thread_filters", "filters"),
            ("buyer_cart", "pending"),
            ("buyer_cart", "last_reco"),
            ("buyer_revert", "categories"),
        ):
            assert await pg_store.aget((root, legacy_key), name) is None
    finally:
        resume_adoption.set()
        for task_name in ("adoption", "gc"):
            task = locals().get(task_name)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await conn.execute("DELETE FROM chat_session_contexts WHERE context_id=%s", (context_id,))
        await pool.close()
        session_context.reset()


async def test_pg_root_gc_fence_blocks_adoption_until_all_roots_are_deleted(
    pg_store, monkeypatch
) -> None:
    context_id = str(uuid.uuid4())
    session_id = f"it-fence-gc-{uuid.uuid4().hex}"
    thread_id = "thread"
    legacy_owner = f"guest-{uuid.uuid4().hex}"
    legacy_key = f"{legacy_owner}:{thread_id}"
    target_key = context_thread_key(context_id, thread_id)
    conn = pg_store.conn.connection
    await conn.execute(
        "INSERT INTO chat_session_contexts "
        "(context_id, session_id, owner_type, owner_id, state) "
        "VALUES (%s, %s, 'guest', %s, 'active')",
        (context_id, session_id, legacy_owner),
    )
    await conn.execute(
        "INSERT INTO chat_session_threads (context_id, thread_id) VALUES (%s, %s)",
        (context_id, thread_id),
    )
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
    await pg_store.aput(("buyer_thread_filters", legacy_key), "filters", {"category": "legacy"})
    await pg_store.aput(
        ("buyer_cart", legacy_key),
        "pending",
        {"product_id": 1, "quantity": 1, "options": [], "attempts": 0},
    )
    await pg_store.aput(("buyer_revert", legacy_key), "categories", {"categories": ["legacy"]})
    await conn.execute(
        "UPDATE chat_session_migrations "
        "SET legacy_quiet_until=now()-interval '1 second' "
        "WHERE migration_name='issue-187-session-context'"
    )
    gc_holds_fence = asyncio.Event()
    resume_gc = asyncio.Event()
    original_delete_page = session_state_module._delete_legacy_root_page

    async def paused_delete_page(*args, **kwargs):
        gc_holds_fence.set()
        await resume_gc.wait()
        return await original_delete_page(*args, **kwargs)

    monkeypatch.setattr(session_state_module, "_delete_legacy_root_page", paused_delete_page)
    pool = AsyncConnectionPool(get_settings().profile_db_url, open=False, min_size=1, max_size=4)
    await pool.open(wait=True)
    pg_store_module.set_store(pg_store)
    session_context.set_pool(pool)
    context = SessionContext(context_id, session_id, "guest", legacy_owner, 0, "active")
    try:
        gc = asyncio.create_task(run_legacy_gc_batch())
        await asyncio.wait_for(gc_holds_fence.wait(), timeout=2)
        adoption = asyncio.create_task(adopt_legacy_thread(context, thread_id, legacy_owner))
        await asyncio.sleep(0.1)
        assert not adoption.done()

        resume_gc.set()
        await asyncio.wait_for(gc, timeout=2)
        result = await asyncio.wait_for(adoption, timeout=2)
        status = await (
            await conn.execute(
                "SELECT adoption_status FROM chat_session_threads "
                "WHERE context_id=%s AND thread_id=%s",
                (context_id, thread_id),
            )
        ).fetchone()
        assert result.adopted and result.copied == 0
        assert status["adoption_status"] == "complete"
        assert await ThreadFilterStore(pg_store).get(target_key) is None
        assert await CartStateStore(pg_store).get_pending(target_key) is None
        assert await RevertStore(pg_store).get(target_key) == set()
        for root, name in (
            ("buyer_thread_filters", "filters"),
            ("buyer_cart", "pending"),
            ("buyer_revert", "categories"),
        ):
            assert await pg_store.aget((root, legacy_key), name) is None
    finally:
        resume_gc.set()
        for task_name in ("adoption", "gc"):
            task = locals().get(task_name)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await conn.execute("DELETE FROM chat_session_contexts WHERE context_id=%s", (context_id,))
        await pool.close()
        session_context.reset()


async def test_pg_session_fence_unlock_is_cancellation_safe(pg_store, monkeypatch) -> None:
    pool = AsyncConnectionPool(get_settings().profile_db_url, open=False, min_size=1, max_size=2)
    await pool.open(wait=True)
    repo = SessionContextRepository(pool=pool)
    unlock_started = asyncio.Event()
    resume_unlock = asyncio.Event()
    original_unlock = getattr(session_state_module, "_unlock_legacy_root_fence", None)

    async def paused_unlock(conn):  # noqa: ANN001
        unlock_started.set()
        await resume_unlock.wait()
        assert original_unlock is not None
        return await original_unlock(conn)

    monkeypatch.setattr(
        session_state_module, "_unlock_legacy_root_fence", paused_unlock, raising=False
    )

    async def acquire_and_release() -> None:
        async with session_state_module._legacy_root_fence(repo, transaction_scoped=False):
            pass

    task = asyncio.create_task(acquire_and_release())
    try:
        await asyncio.wait_for(unlock_started.wait(), timeout=2)
        task.cancel()
        resume_unlock.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        async with pool.connection() as conn:
            acquired = await (
                await conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                    (session_state_module._LEGACY_ROOT_FENCE_KEY,),
                )
            ).fetchone()
            assert acquired == (True,)
            released = await (
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (session_state_module._LEGACY_ROOT_FENCE_KEY,),
                )
            ).fetchone()
            assert released == (True,)
    finally:
        resume_unlock.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await pool.close()


async def test_pg_session_fence_discards_connection_when_unlock_returns_false(
    pg_store, monkeypatch
) -> None:
    pool = AsyncConnectionPool(get_settings().profile_db_url, open=False, min_size=1, max_size=2)
    await pool.open(wait=True)
    repo = SessionContextRepository(pool=pool)

    async def failed_unlock(conn):  # noqa: ANN001
        return False

    monkeypatch.setattr(
        session_state_module, "_unlock_legacy_root_fence", failed_unlock, raising=False
    )
    try:
        fence_backend_pid = None
        with pytest.raises(RuntimeError, match="legacy root advisory unlock"):
            async with session_state_module._legacy_root_fence(
                repo, transaction_scoped=False
            ) as fence_conn:
                fence_backend_pid = fence_conn.info.backend_pid

        async with pool.connection() as conn:
            assert conn.info.backend_pid != fence_backend_pid
            acquired = await (
                await conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                    (session_state_module._LEGACY_ROOT_FENCE_KEY,),
                )
            ).fetchone()
            assert acquired == (True,)
            released = await (
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (session_state_module._LEGACY_ROOT_FENCE_KEY,),
                )
            ).fetchone()
            assert released == (True,)
    finally:
        await pool.close()


async def test_pg_session_fence_discards_connection_after_unlock_query_failure(
    pg_store, monkeypatch
) -> None:
    pool = AsyncConnectionPool(get_settings().profile_db_url, open=False, min_size=1, max_size=2)
    await pool.open(wait=True)
    repo = SessionContextRepository(pool=pool)

    async def failed_unlock(conn):  # noqa: ANN001
        await conn.execute("SELECT missing_legacy_root_unlock_function()")
        return True

    monkeypatch.setattr(session_state_module, "_unlock_legacy_root_fence", failed_unlock)
    try:
        fence_backend_pid = None
        with pytest.raises(Exception, match="missing_legacy_root_unlock_function"):
            async with session_state_module._legacy_root_fence(
                repo, transaction_scoped=False
            ) as fence_conn:
                fence_backend_pid = fence_conn.info.backend_pid

        async with pool.connection() as conn:
            assert conn.info.backend_pid != fence_backend_pid
            acquired = await (
                await conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                    (session_state_module._LEGACY_ROOT_FENCE_KEY,),
                )
            ).fetchone()
            assert acquired == (True,)
            released = await (
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (session_state_module._LEGACY_ROOT_FENCE_KEY,),
                )
            ).fetchone()
            assert released == (True,)
    finally:
        await pool.close()


async def test_revert_store_add_concurrent_calls_no_lost_update(pg_store) -> None:
    """동시 add() 호출이 서로의 갱신을 잃지 않는다 — get→put 락 없으면 lost update(PR #46 리뷰)."""
    wrapper = RevertStore(pg_store)
    key = _key()
    await asyncio.gather(
        wrapper.add(key, ["A"]),
        wrapper.add(key, ["B"]),
        wrapper.add(key, ["C"]),
    )
    assert await wrapper.get(key) == {"A", "B", "C"}


async def test_state_persists_across_store_instances() -> None:
    """재시작·다중 인스턴스 스모크(이슈 #33 범위) — 새 연결로도 이전에 쓴 productId 가 보인다.

    상품명(product.name 사본)은 규칙상 pg-profile 에 저장하지 않고 프로세스 로컬 캐시에만
    두므로(PR #46 후속 리뷰), 재시작(=캐시 소실) 후에는 pid 만 복원되고 이름은 "" 로 degrade
    한다("그거 담아줘" pid 해소는 계속 작동)."""
    key = _key()
    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store_a:
        await store_a.setup()
        await CartStateStore(store_a).set_last_reco(key, [(999, "영속성 테스트")])

    cart_state._last_reco_names.clear()  # 프로세스 재시작 시 휘발성 이름 캐시 소실 재현

    async with AsyncPostgresStore.from_conn_string(get_settings().profile_db_url) as store_b:
        await store_b.setup()
        reco = await CartStateStore(store_b).get_last_reco(key)
    assert reco == [(999, "")]  # pid 는 영속, 이름은 재시작 후 소실(graceful degrade)


async def test_revert_store_concurrent_writes_across_postgres_pools() -> None:
    """서로 다른 앱 인스턴스(pool)의 RMW도 advisory lock으로 lost update가 없다."""
    key = _key()
    conninfo = hardened_pg_conninfo(get_settings().profile_db_url)
    pool_config = state_store_pool_config()
    async with (
        AsyncPostgresStore.from_conn_string(conninfo, pool_config=pool_config) as store_a,
        AsyncPostgresStore.from_conn_string(conninfo, pool_config=pool_config) as store_b,
    ):
        await store_a.setup()
        await store_b.setup()
        wrappers = [RevertStore(store_a), RevertStore(store_b)]
        expected = {f"category-{i}" for i in range(20)}
        await asyncio.gather(
            *(wrappers[i % 2].add(key, [category]) for i, category in enumerate(expected))
        )
        assert await wrappers[0].get(key) == expected


async def test_hardened_store_connection_parameters_are_active() -> None:
    """실 pg-profile 세션에 statement/TCP timeout 설정이 적용된다."""
    settings = get_settings()
    async with AsyncPostgresStore.from_conn_string(
        hardened_pg_conninfo(settings.profile_db_url),
        pool_config=state_store_pool_config(),
    ) as store:
        async with store.conn.connection() as conn:
            row = await (await conn.execute("SHOW statement_timeout")).fetchone()
            statement_timeout = row["statement_timeout"]
            params = conn.info.get_parameters()

    assert statement_timeout in {
        f"{int(settings.state_store_query_timeout_s * 1000)}ms",
        f"{settings.state_store_query_timeout_s:g}s",
    }
    assert params["tcp_user_timeout"] == str(settings.state_store_tcp_user_timeout_ms)


async def test_pg_store_module_connects_to_real_postgres() -> None:
    """app.core.pg_store.get_store() 가 실제로 AsyncPostgresStore(pg-profile)에 연결된다.

    conftest 의 reset_*_store() 는 매 테스트 InMemoryStore 로 되돌리므로, 여기서는
    set_store(None) 으로 재초기화를 강제해 지연 연결 경로 자체를 검증한다.
    """
    pg_store_module.set_store(None)
    try:
        store = await pg_store_module.get_store()
        assert isinstance(store, AsyncPostgresStore)
        key = _key()
        await CartStateStore(store).set_last_reco(key, [(1, "a")])
        assert await CartStateStore(store).get_last_reco(key) == [(1, "a")]
    finally:
        pg_store_module.set_store(None)


async def test_pg_store_get_store_concurrent_calls_single_connection() -> None:
    """동시 get_store() 호출이 커넥션을 중복 생성하지 않는다(락 없으면 콜드 스타트 레이스, PR #46 리뷰)."""
    pg_store_module.set_store(None)
    try:
        stores = await asyncio.gather(*(pg_store_module.get_store() for _ in range(10)))
        assert len({id(s) for s in stores}) == 1
    finally:
        pg_store_module.set_store(None)


async def test_set_store_none_defers_cleanup_to_next_get_store_call() -> None:
    """set_store(None) 이 sync 컨텍스트에서 놓친 정리를 다음 get_store() 가 확실히 처리한다.

    fire-and-forget(asyncio.get_running_loop().create_task) 방식은 실행 중인
    이벤트 루프가 없으면(예: conftest 의 sync autouse fixture) 조용히 스킵돼
    실제로 한 번도 정리가 안 됐었다(PR #46 후속 리뷰) — 지연 정리 큐로 교체 후,
    다음 get_store() 호출 시 확실히 __aexit__ 가 실행되는지 conn.closed 로 검증한다.
    """
    pg_store_module.set_store(None)
    store = await pg_store_module.get_store()
    assert isinstance(store, AsyncPostgresStore)
    conn = store.conn
    assert not conn.closed

    pg_store_module.set_store(None)  # sync 호출 — 정리는 아직 큐에만 쌓인 상태
    assert not conn.closed  # 아직 정리 안 됨(다음 get_store() 전까지)

    await pg_store_module.get_store()  # 이 호출 진입 시 _drain_pending_cleanup() 이 실행됨
    assert conn.closed

    pg_store_module.set_store(None)
