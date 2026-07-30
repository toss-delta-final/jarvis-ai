from __future__ import annotations

import asyncio

import pytest

from app.core.session_context import (
    BuyerSessionInput,
    SessionClaimConflict,
    SessionContextRepository,
    SessionFinalizing,
    SessionForbidden,
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
    await repo.complete_transient_phase(terminal.claim)
    await repo.record_profile_phase(terminal.claim.finalization_id, "retryable")
    [candidate] = await repo.list_recoverable_profile_phases(10)
    assert candidate.finalization_id == terminal.claim.finalization_id
    claimed = await repo.claim_profile_phase(candidate.finalization_id, 30)
    assert claimed is not None


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
