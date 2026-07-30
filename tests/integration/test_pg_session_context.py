from __future__ import annotations

import asyncio
import uuid

import pytest
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings
from app.core.session_context import (
    BuyerSessionInput,
    SessionClaimConflict,
    SessionContextRepository,
)
from app.core import session_context as session_context_module

pytestmark = pytest.mark.integration


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
    [idle] = await repo.claim_expired_contexts(10, 30, 10)
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
