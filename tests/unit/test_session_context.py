from __future__ import annotations

import asyncio

import pytest

from app.core.session_context import (
    BuyerSessionInput,
    SessionClaimConflict,
    SessionContextRepository,
    SessionFinalizing,
    SessionForbidden,
    _MemoryContext,
    reset,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def repo(clock: Clock) -> SessionContextRepository:
    return SessionContextRepository(clock=clock)


async def test_concurrent_first_messages_converge_on_one_context(repo) -> None:
    request = BuyerSessionInput("S1", "T1", "guest", "G1")
    first, second = await asyncio.gather(repo.touch(request), repo.touch(request))
    assert first == second
    assert await repo.get_threads(first.context_id) == ["T1"]


async def test_touch_rejects_other_owner(repo) -> None:
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    with pytest.raises(SessionForbidden):
        await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))


async def test_signed_touch_replaces_legacy_guess_but_runtime_owner_remains_authoritative(
    repo, clock
) -> None:
    repo._contexts["S1"] = _MemoryContext(
        "legacy-context", "S1", "member", "7", 0, "active", clock(), "legacy_backfill"
    )

    signed = await repo.touch(BuyerSessionInput("S1", "T1", "member", "8"))

    assert signed.owner_id == "8"
    assert signed.context_id != "legacy-context"
    assert repo._migration_conflicts[("S1", "7")][0] == "quarantined"
    with pytest.raises(SessionForbidden):
        await repo.touch(BuyerSessionInput("S1", "T2", "member", "7"))


async def test_signed_claim_replaces_legacy_guess_and_quarantines_old_owner(repo, clock) -> None:
    repo._contexts["S1"] = _MemoryContext(
        "legacy-context", "S1", "member", "7", 0, "active", clock(), "legacy_backfill"
    )

    outcome = await repo.claim_owner("S1", "signed-guest", 8)

    assert outcome.claimed is True
    assert outcome.context.owner_id == "8"
    assert outcome.context.context_id != "legacy-context"
    assert repo._migration_conflicts[("S1", "7")][0] == "quarantined"
    with pytest.raises(SessionForbidden):
        await repo.touch(BuyerSessionInput("S1", "T2", "member", "7"))


async def test_touch_registers_threads_and_reactivates_idle(repo, clock) -> None:
    original = await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)

    touched = await repo.touch(BuyerSessionInput("S1", "T2", "guest", "G1"))

    assert touched.context_id == original.context_id
    assert touched.state == "active"
    assert touched.generation == original.generation + 1
    assert await repo.get_threads(original.context_id) == ["T1", "T2"]
    with pytest.raises(SessionClaimConflict):
        await repo.validate_for_delete(claim)


async def test_touch_denies_finalizing_and_terminal(repo, clock) -> None:
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)
    await repo.mark_idle_finalizing(claim)
    with pytest.raises(SessionFinalizing):
        await repo.touch(BuyerSessionInput("S1", "T2", "guest", "G1"))

    terminal_repo = SessionContextRepository(clock=clock)
    await terminal_repo.touch(BuyerSessionInput("S2", "T1", "member", "7"))
    await terminal_repo.begin_terminal(7, "S2")
    with pytest.raises(SessionForbidden):
        await terminal_repo.touch(BuyerSessionInput("S2", "T2", "member", "7"))


async def test_idle_claims_are_bounded_and_lease_recovery_replaces_token(repo, clock) -> None:
    for session_id in ("S1", "S2"):
        await repo.touch(BuyerSessionInput(session_id, "T1", "guest", f"G-{session_id}"))
    clock.advance(601)

    [first] = await repo.claim_expired_contexts(600, 30, 1)
    await repo.mark_idle_finalizing(first)
    assert await repo.claim_recoverable_finalizations(30, 10) == []
    clock.advance(31)
    [recovered] = await repo.claim_recoverable_finalizations(30, 1)

    assert recovered.finalization_id == first.finalization_id
    assert recovered.claim_token != first.claim_token
    assert recovered.reason == "idle"


async def test_expired_prephase_idle_is_reissued_but_started_rows_are_preserved(
    repo, clock
) -> None:
    await repo.touch(BuyerSessionInput("orphan", "T1", "member", "7"))
    await repo.touch(BuyerSessionInput("captured", "T1", "member", "8"))
    clock.advance(601)
    claims = await repo.claim_expired_contexts(600, 30, 10)
    orphan = next(claim for claim in claims if claim.session_id == "orphan")
    captured = next(claim for claim in claims if claim.session_id == "captured")
    async with repo.lock_session("captured") as uow:
        await uow.capture_profile_watermark(captured, 0)
    clock.advance(31)

    replacements = await repo.claim_expired_contexts(600, 30, 10)

    [replacement] = [claim for claim in replacements if claim.session_id == "orphan"]
    assert replacement.finalization_id == orphan.finalization_id
    assert replacement.claim_token != orphan.claim_token
    with pytest.raises(SessionClaimConflict):
        await repo.validate_for_delete(orphan)
    preserved = await repo.get_finalization(captured.finalization_id)
    assert preserved.watermark_status == "captured"
    assert all(claim.session_id != "captured" for claim in replacements)


async def test_owner_claim_is_atomic_idempotent_and_rejects_wrong_source(repo) -> None:
    original = await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))

    claimed = await repo.claim_owner("S1", "G1", 7)
    duplicate = await repo.claim_owner("S1", "G1", 7)

    assert claimed.claimed is True
    assert claimed.context.owner_type == "member"
    assert claimed.context.owner_id == "7"
    assert claimed.context.generation == original.generation + 1
    assert duplicate.claimed is False
    assert duplicate.context == claimed.context
    with pytest.raises(SessionClaimConflict):
        await repo.claim_owner("S1", "someone-else", 8)


async def test_owner_claim_rejects_finalizing_context(repo, clock) -> None:
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)
    await repo.mark_idle_finalizing(claim)
    with pytest.raises(SessionFinalizing):
        await repo.claim_owner("S1", "G1", 7)


async def test_terminal_supersedes_idle_and_transient_profile_phases(repo, clock) -> None:
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    clock.advance(601)
    [idle] = await repo.claim_expired_contexts(600, 30, 1)

    terminal = await repo.begin_terminal(7, "S1")

    assert terminal.context.state == "terminal"
    assert terminal.context.generation == idle.generation + 1
    assert terminal.finalization.reason == "terminal"
    assert (await repo.get_finalization(idle.finalization_id)).status == "superseded"
    assert terminal.claim is not None
    async with repo.lock_session("S1") as uow:
        await uow.capture_profile_watermark(terminal.claim, 7)
    await repo.complete_transient_phase(terminal.claim)
    await repo.record_profile_phase(terminal.claim.finalization_id, "retryable")
    [candidate] = await repo.list_recoverable_profile_phases(10)
    assert candidate.finalization_id == terminal.claim.finalization_id
    claimed = await repo.claim_profile_phase(candidate.finalization_id, 30)
    assert claimed is not None


async def test_tokenless_profile_record_cannot_change_claimed_processing_row(repo) -> None:
    await repo.touch(BuyerSessionInput("claimed-profile", "T1", "member", "7"))
    terminal = await repo.begin_terminal(7, "claimed-profile")
    assert terminal.claim is not None
    async with repo.lock_session("claimed-profile") as uow:
        await uow.capture_profile_watermark(terminal.claim, 7)
    await repo.complete_transient_phase(terminal.claim)
    claimed = await repo.claim_profile_phase(terminal.claim.finalization_id, 30)
    assert claimed is not None

    with pytest.raises(SessionClaimConflict):
        await repo.record_profile_phase(terminal.claim.finalization_id, "completed")

    journal = await repo.get_finalization(terminal.claim.finalization_id)
    assert journal.profile_status == "processing"
    assert journal.claim_token == claimed.claim_token


async def test_locked_unit_of_work_keeps_idle_phase_transitions_together(repo, clock) -> None:
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)

    async with repo.lock_session("S1") as uow:
        prepared = await uow.prepare_idle_finalizing(claim)
        assert prepared.state == "idle_finalizing"
        validated = await uow.validate_idle_delete(claim)
        assert validated == prepared
        await uow.complete_idle_delete(claim)

    final = await repo.get_context("S1")
    assert final is not None
    assert final.state == "idle_expired"
    assert await repo.get_threads(final.context_id) == []


async def test_touch_keeps_completed_idle_profile_recoverable(repo, clock) -> None:
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)
    async with repo.lock_session("S1") as uow:
        await uow.prepare_idle_finalizing(claim)
        await uow.capture_profile_watermark(claim, 9)
        await uow.complete_idle_delete(claim)
    await repo.record_profile_phase(claim.finalization_id, "retryable")

    touched = await repo.touch(BuyerSessionInput("S1", "T2", "member", "7"))

    assert touched.generation == claim.generation + 1
    [candidate] = await repo.list_recoverable_profile_phases(10)
    assert candidate.finalization_id == claim.finalization_id
    assert candidate.profile_watermark == 9


async def test_member_watermark_accepts_explicit_empty_snapshot(repo, clock) -> None:
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)
    async with repo.lock_session("S1") as uow:
        await uow.prepare_idle_finalizing(claim)
        pending = await repo.get_finalization(claim.finalization_id)
        assert pending.watermark_status == "pending"
        assert pending.profile_watermark is None
        with pytest.raises(ValueError):
            await uow.capture_profile_watermark(claim, -1)
        await uow.capture_profile_watermark(claim, 0)
    captured = await repo.get_finalization(claim.finalization_id)
    assert captured.watermark_status == "captured"
    assert captured.profile_watermark == 0


async def test_terminal_supersedes_completed_idle_from_previous_generation(repo, clock) -> None:
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    clock.advance(601)
    [idle] = await repo.claim_expired_contexts(600, 30, 1)
    async with repo.lock_session("S1") as uow:
        await uow.prepare_idle_finalizing(idle)
        await uow.capture_profile_watermark(idle, 0)
        await uow.complete_idle_delete(idle)
    await repo.record_profile_phase(idle.finalization_id, "retryable")
    await repo.touch(BuyerSessionInput("S1", "T2", "member", "7"))
    assert [item.finalization_id for item in await repo.list_recoverable_profile_phases(10)] == [
        idle.finalization_id
    ]

    terminal = await repo.begin_terminal(7, "S1")

    assert terminal.claim is not None
    superseded = await repo.get_finalization(idle.finalization_id)
    assert superseded.status == "superseded"
    assert superseded.claim_token is None
    assert superseded.lease_expires_at is None
    assert await repo.list_recoverable_profile_phases(10) == []


async def test_terminal_duplicate_does_not_reissue_live_or_completed_work(repo, clock) -> None:
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    first = await repo.begin_terminal(7, "S1")

    live_duplicate = await repo.begin_terminal(7, "S1")
    assert live_duplicate.duplicate is True
    assert live_duplicate.claim is None

    assert first.claim is not None
    await repo.complete_transient_phase(first.claim)
    completed_duplicate = await repo.begin_terminal(7, "S1")
    assert completed_duplicate.duplicate is True
    assert completed_duplicate.claim is None


async def test_terminal_duplicate_reissues_expired_pending_claim(repo, clock) -> None:
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    first = await repo.begin_terminal(7, "S1")
    assert first.claim is not None
    clock.advance(901)

    recovered = await repo.begin_terminal(7, "S1")

    assert recovered.duplicate is False
    assert recovered.claim is not None
    assert recovered.claim.finalization_id == first.claim.finalization_id
    assert recovered.claim.claim_token != first.claim.claim_token


async def test_unit_of_work_rejects_claim_from_another_session(repo, clock) -> None:
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    await repo.touch(BuyerSessionInput("S2", "T1", "guest", "G2"))
    clock.advance(601)
    claims = await repo.claim_expired_contexts(600, 30, 2)
    claim = next(item for item in claims if item.session_id == "S1")

    async with repo.lock_session("S2") as uow:
        with pytest.raises(SessionClaimConflict):
            await uow.prepare_idle_finalizing(claim)


class _EntryFailurePool:
    def __init__(self, *, fail_at: str) -> None:
        self.fail_at = fail_at
        self.connection_exits = 0
        self.transaction_exits = 0

    def connection(self):
        pool = self

        class ConnectionContext:
            async def __aenter__(self):
                return Connection()

            async def __aexit__(self, exc_type, exc, tb):
                pool.connection_exits += 1

        class Connection:
            def transaction(self):
                class TransactionContext:
                    async def __aenter__(self):
                        if pool.fail_at == "transaction":
                            raise RuntimeError("transaction enter failed")

                    async def __aexit__(self, exc_type, exc, tb):
                        pool.transaction_exits += 1

                return TransactionContext()

            async def execute(self, *_args, **_kwargs):
                if pool.fail_at == "advisory":
                    raise RuntimeError("advisory lock failed")

        return ConnectionContext()


@pytest.mark.parametrize(
    ("fail_at", "transaction_exits"),
    [("transaction", 0), ("advisory", 1)],
)
async def test_unit_of_work_entry_failure_releases_acquired_resources(
    fail_at: str, transaction_exits: int
) -> None:
    pool = _EntryFailurePool(fail_at=fail_at)
    repo = SessionContextRepository(pool=pool)

    with pytest.raises(RuntimeError):
        async with repo.lock_session("S1"):
            pass

    assert pool.transaction_exits == transaction_exits
    assert pool.connection_exits == 1


async def test_initialize_propagates_programming_errors(monkeypatch) -> None:
    import psycopg_pool
    from app.core import session_context

    class BrokenPool:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def open(self, **_kwargs) -> None:
            raise RuntimeError("bad pool configuration")

        async def close(self) -> None:
            pass

    reset()
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", BrokenPool)
    with pytest.raises(RuntimeError, match="bad pool configuration"):
        await session_context.initialize()
    reset()


async def test_initialize_timeout_uses_dev_fallback_with_warning(monkeypatch, caplog) -> None:
    import psycopg_pool
    from app.core import session_context

    class TimedOutPool:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def open(self, **_kwargs) -> None:
            raise TimeoutError("profile db timeout")

        async def close(self) -> None:
            pass

    reset()
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", TimedOutPool)
    with caplog.at_level("WARNING"):
        await session_context.initialize()
    assert "session lifecycle" in caplog.text
    reset()


async def test_initialize_session_lifecycle_does_not_mask_reachable_db_in_dev(
    monkeypatch,
) -> None:
    from app.core import session_context

    calls: list[str] = []

    async def initialize() -> None:
        calls.append("initialize")

    reset()
    monkeypatch.setattr(session_context, "initialize", initialize)
    await session_context.initialize_session_lifecycle()
    assert calls == ["initialize"]
    reset()


async def test_owned_pool_is_closed_and_reset_when_schema_initialization_fails(
    monkeypatch,
) -> None:
    import psycopg_pool
    from app.core import session_context

    pools = []

    class Pool:
        def __init__(self, *_args, **_kwargs) -> None:
            self.closed = False
            pools.append(self)

        async def open(self, **_kwargs) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    async def fail_initialize(self) -> None:
        raise RuntimeError("schema failed")

    reset()
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", Pool)
    monkeypatch.setattr(SessionContextRepository, "initialize", fail_initialize)
    with pytest.raises(RuntimeError, match="schema failed"):
        await session_context.initialize()

    assert pools[0].closed is True
    assert session_context._owned_pool is None
    assert session_context._default_repository._pool is None
    reset()


async def test_close_session_lifecycle_closes_only_owned_pool(monkeypatch) -> None:
    from app.core import session_context

    class Pool:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    owned = Pool()
    injected = Pool()
    session_context._owned_pool = owned
    session_context._default_repository = SessionContextRepository(pool=owned)
    await session_context.close_session_lifecycle()
    await session_context.close_session_lifecycle()
    assert owned.close_calls == 1
    assert session_context._default_repository._pool is None

    session_context.set_pool(injected)
    await session_context.close_session_lifecycle()
    assert injected.close_calls == 0
    reset()
