from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

from app.agents.buyer.cart import state as cart_state
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
    SessionClaimConflict,
    SessionContextRepository,
    SessionContextUnitOfWork,
    SessionFinalizing,
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
    prefix = f"it-{uuid.uuid4().hex}"
    try:
        yield repo, pool, prefix
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM chat_session_contexts WHERE session_id LIKE %s", (prefix + "%",)
            )
        await pool.close()


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
    idle = next(
        claim
        for claim in await repo.claim_expired_contexts(10, 30, 100)
        if claim.session_id == session_id
    )
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
    [claim] = await repo.claim_expired_contexts(10, 30, 10)
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
    [idle] = await repo.claim_expired_contexts(10, 30, 10)
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
    idle = next(
        claim
        for claim in await repo.claim_expired_contexts(10, 30, 100)
        if claim.session_id == session_id
    )
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
    assert registry.acquire("stream-1", owner_id="7", session_id=session_id)
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
    registry.release("stream-1")
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
    idle = next(
        claim
        for claim in await repo.claim_expired_contexts(10, 30, 100)
        if claim.session_id == session_id
    )
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
    idle = next(
        claim
        for claim in await repo.claim_expired_contexts(10, 30, 100)
        if claim.session_id == session_id
    )

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
    idle = next(
        claim
        for claim in await repo.claim_expired_contexts(10, 30, 100)
        if claim.session_id == session_id
    )

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
    claims = await repo.claim_expired_contexts(10, 30, 10)
    claim_a = next(item for item in claims if item.session_id == prefix + "a")
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
    [claim] = await repo.claim_expired_contexts(10, 30, 10)
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
    [first] = await repo.claim_expired_contexts(10, 30, 10)
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
    [idle] = await repo.claim_expired_contexts(10, 30, 10)
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
    [claim] = await repo.claim_expired_contexts(10, 30, 10)
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
    [idle] = await repo.claim_expired_contexts(10, 30, 10)
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
    [claim] = await repo.claim_expired_contexts(10, 30, 1)
    registry = ActiveStreamRegistry()

    class BrokenProfile:
        async def get_session_ctx_snapshot(self, key: str):
            if failure == "cancel":
                raise asyncio.CancelledError
            raise RuntimeError("snapshot unavailable")

    if failure == "active":
        assert registry.acquire("active", owner_id="7", session_id=session_id)
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
        registry.release("active")

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
    [claim] = await repo.claim_expired_contexts(10, 30, 1)

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
    [orphan] = await repo.claim_expired_contexts(10, 30, 1)

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
    [orphan] = await repo.claim_expired_contexts(10, 30, 1)
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
    [stale] = await repo.claim_expired_contexts(10, 30, 1)
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
    [orphan] = await repo.claim_expired_contexts(10, 30, 1)
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
    winners = [claim for batch in (first, second) for claim in batch]

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
    claims = await repo.claim_expired_contexts(10, 30, 100)
    by_session = {claim.session_id: claim for claim in claims}
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

    replacements = await repo.claim_expired_contexts(10, 30, 100)

    assert not set(sessions.values()) & {claim.session_id for claim in replacements}
    assert (await repo.get_finalization(captured.finalization_id)).watermark_status == "captured"
    assert (await repo.get_finalization(superseded.finalization_id)).status == "superseded"
    assert (await repo.get_finalization(completed.finalization_id)).transient_status == "completed"
