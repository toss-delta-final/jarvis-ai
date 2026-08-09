from __future__ import annotations

import asyncio
import uuid

import httpx
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from app.core.session_context import (
    BuyerSessionInput,
    SessionActive,
    SessionClaimConflict,
    SessionContextRepository,
)
from app.core.session_lifecycle import SessionLifecycleCoordinator
from app.core import session_context as session_context_module
from app.main import app
from app.core.stream import ActiveStreamRegistry
from app.schemas.events import SessionClaimEvent

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_claim():
    from app.core.config import get_settings

    pool = AsyncConnectionPool(
        get_settings().profile_db_url,
        open=False,
        min_size=1,
        max_size=4,
    )
    await pool.open(wait=True)
    repository = SessionContextRepository(pool=pool)
    await repository.initialize()
    prefix = f"it-claim-{uuid.uuid4().hex}"
    registry = ActiveStreamRegistry()
    try:
        yield repository, pool, registry, prefix
    finally:
        registry._active.clear()
        registry._fences.clear()
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM chat_session_contexts WHERE session_id LIKE %s",
                (prefix + "%",),
            )
        await pool.close()


async def _rows(pool, session_id: str):  # noqa: ANN001
    async with pool.connection() as conn:
        context = await (
            await conn.execute(
                "SELECT owner_type, owner_id, generation FROM chat_session_contexts "
                "WHERE session_id=%s",
                (session_id,),
            )
        ).fetchone()
        history = await (
            await conn.execute(
                "SELECT from_owner_id, to_owner_id FROM chat_session_owner_claims "
                "WHERE session_id=%s",
                (session_id,),
            )
        ).fetchall()
    return context, history


async def test_pg_coordinator_different_targets_have_one_winner_and_history(pg_claim) -> None:
    repository, pool, registry, prefix = pg_claim
    session_id = prefix + "-different"
    await repository.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    coordinator = SessionLifecycleCoordinator(repository, registry)
    start = asyncio.Event()

    async def compete(user_id: int):
        await start.wait()
        try:
            return await coordinator.claim_owner(
                SessionClaimEvent(
                    sessionId=session_id,
                    guestId="G1",
                    userId=user_id,
                )
            )
        except SessionClaimConflict as exc:
            return exc

    tasks = [asyncio.create_task(compete(user_id)) for user_id in (7, 8)]
    start.set()
    outcomes = await asyncio.gather(*tasks)

    winners = [item for item in outcomes if not isinstance(item, Exception)]
    conflicts = [item for item in outcomes if isinstance(item, SessionClaimConflict)]
    assert len(winners) == len(conflicts) == 1
    assert await _rows(pool, session_id) == (
        ("member", winners[0].context.owner_id, 1),
        [("G1", winners[0].context.owner_id)],
    )
    assert not registry.is_fenced("G1", session_id)


async def test_pg_coordinator_same_target_is_accepted_then_exact_duplicate(pg_claim) -> None:
    repository, pool, registry, prefix = pg_claim
    session_id = prefix + "-same"
    await repository.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    coordinator = SessionLifecycleCoordinator(repository, registry)
    event = SessionClaimEvent(sessionId=session_id, guestId="G1", userId=7)

    first, second = await asyncio.gather(
        coordinator.claim_owner(event),
        coordinator.claim_owner(event),
    )

    assert sorted(item.claimed for item in (first, second)) == [False, True]
    assert first.context.generation == second.context.generation == 1
    assert await _rows(pool, session_id) == (
        ("member", "7", 1),
        [("G1", "7")],
    )
    assert not registry.is_fenced("G1", session_id)


async def test_pg_coordinator_no_row_race_creates_one_context_and_history(pg_claim) -> None:
    repository, pool, registry, prefix = pg_claim
    session_id = prefix + "-no-row"
    coordinator = SessionLifecycleCoordinator(repository, registry)
    event = SessionClaimEvent(sessionId=session_id, guestId="G1", userId=7)

    first, second = await asyncio.gather(
        coordinator.claim_owner(event),
        coordinator.claim_owner(event),
    )

    assert sorted(item.claimed for item in (first, second)) == [False, True]
    assert first.context.context_id == second.context.context_id
    assert await _rows(pool, session_id) == (
        ("member", "7", 0),
        [("G1", "7")],
    )
    assert not registry.is_fenced("G1", session_id)


async def test_pg_coordinator_signed_claim_replaces_legacy_guess(pg_claim) -> None:
    repository, pool, registry, prefix = pg_claim
    session_id = prefix + "-legacy-takeover"
    legacy_context_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO chat_session_contexts
                (context_id, session_id, owner_type, owner_id,
                 authority_source, state)
            VALUES (%s, %s, 'member', '6', 'legacy_backfill', 'active')
            """,
            (legacy_context_id, session_id),
        )
        await conn.execute(
            """
            INSERT INTO chat_session_migration_conflicts
                (session_id, owner_id, legacy_status, legacy_last_activity_at,
                 resolution_status, resolved_context_id)
            VALUES (%s, '6', 'completed', now()-interval '1 day',
                    'resolved', %s)
            """,
            (session_id, legacy_context_id),
        )
    try:
        outcome = await SessionLifecycleCoordinator(repository, registry).claim_owner(
            SessionClaimEvent(sessionId=session_id, guestId="signed-guest", userId=7)
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
                    "WHERE session_id=%s AND owner_id='6'",
                    (session_id,),
                )
            ).fetchone()
        assert outcome.claimed is True
        assert authority == ("7", "runtime")
        assert conflict == ("quarantined",)
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM chat_session_migration_conflicts WHERE session_id=%s",
                (session_id,),
            )


async def test_pg_coordinator_rejects_active_unregistered_thread_scope(pg_claim) -> None:
    repository, pool, registry, prefix = pg_claim
    session_id = prefix + "-active"
    assert await registry.acquire(
        "G1:new-thread",
        owner_id="G1",
        session_id=session_id,
    )

    with pytest.raises(SessionActive):
        await SessionLifecycleCoordinator(repository, registry).claim_owner(
            SessionClaimEvent(sessionId=session_id, guestId="G1", userId=7)
        )

    assert await _rows(pool, session_id) == (None, [])
    assert registry.is_active("G1:new-thread")
    assert not registry.is_fenced("G1", session_id)


async def test_pg_coordinator_releases_fence_on_db_failure_and_cancellation(
    pg_claim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, registry, prefix = pg_claim
    session_id = prefix + "-failure"
    await repository.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    coordinator = SessionLifecycleCoordinator(repository, registry)
    event = SessionClaimEvent(sessionId=session_id, guestId="G1", userId=7)

    async def fail(conn, claim_session_id, guest_id, user_id):  # noqa: ANN001
        raise RuntimeError("forced transition failure")

    monkeypatch.setattr(repository, "_claim_owner_on_connection", fail)
    with pytest.raises(RuntimeError, match="forced transition failure"):
        await coordinator.claim_owner(event)
    assert not registry.is_fenced("G1", session_id)

    entered = asyncio.Event()

    async def block(conn, claim_session_id, guest_id, user_id):  # noqa: ANN001
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(repository, "_claim_owner_on_connection", block)
    task = asyncio.create_task(coordinator.claim_owner(event))
    await entered.wait()
    assert registry.is_fenced("G1", session_id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not registry.is_fenced("G1", session_id)


async def test_pg_claim_operational_failure_returns_503_and_rolls_back(
    pg_claim,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실 PG claim transaction 장애는 공개 503이며 owner/history mutation을 rollback한다."""
    repository, pool, registry, prefix = pg_claim
    session_id = prefix + "-wire-failure"
    await repository.touch(BuyerSessionInput(session_id, "T1", "guest", "G1"))
    original = repository._claim_owner_on_connection

    async def mutate_then_fail(conn, claim_session_id, guest_id, user_id):  # noqa: ANN001
        await original(conn, claim_session_id, guest_id, user_id)
        raise psycopg.OperationalError("controlled connection loss")

    monkeypatch.setattr(repository, "_claim_owner_on_connection", mutate_then_fail)
    monkeypatch.setattr(session_context_module, "_default_repository", repository)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/events/session-claim",
            json={"sessionId": session_id, "guestId": "G1", "userId": 7},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STATE_UNAVAILABLE"
    assert await _rows(pool, session_id) == (("guest", "G1", 0), [])
    assert not registry.is_fenced("G1", session_id)
