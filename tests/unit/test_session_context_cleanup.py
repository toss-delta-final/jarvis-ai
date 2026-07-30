from __future__ import annotations

import asyncio

import pytest

from app.core.session_context import BuyerSessionInput, SessionContextRepository
from app.core.session_lifecycle import SessionLifecycleCoordinator
from app.core.stream import ActiveStreamRegistry


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ProfileStoreStub:
    def __init__(self, watermark: int = 0) -> None:
        self.watermark = watermark
        self.keys: list[str] = []

    async def get_session_ctx_snapshot(self, key: str) -> tuple[list[str], int]:
        self.keys.append(key)
        return (["saved"] if self.watermark else [], self.watermark)


@pytest.fixture
def clock() -> Clock:
    return Clock()


async def test_idle_cleanup_commits_gate_then_deletes_all_registered_threads(
    monkeypatch, clock: Clock
) -> None:
    repo = SessionContextRepository(clock=clock)
    context = await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    await repo.touch(BuyerSessionInput("S1", "T2", "guest", "G1"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)
    deleted: list[tuple[str, tuple[str, ...]]] = []

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        assert (await repo.get_context("S1")).state == "idle_finalizing"
        deleted.append((context_id, tuple(thread_ids)))
        return CleanupCounts(filters=2)

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())

    outcome = await coordinator.process_idle_transient(claim)

    assert outcome.status == "completed"
    assert outcome.cleanup.filters == 2
    assert deleted == [(context.context_id, ("T1", "T2"))]
    assert (await repo.get_context("S1")).state == "idle_expired"
    assert await repo.get_threads(context.context_id) == []


async def test_member_phase_a_captures_real_profile_watermark(monkeypatch, clock: Clock) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)
    profile = ProfileStoreStub(watermark=17)

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    coordinator = SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: profile,
    )

    outcome = await coordinator.process_idle_transient(claim)

    finalization = await repo.get_finalization(claim.finalization_id)
    assert outcome.status == "completed"
    assert finalization.watermark_status == "captured"
    assert finalization.profile_watermark == 17
    assert profile.keys == ["7:S1"]


async def test_active_stream_skips_phase_a_without_committing_finalizing(clock: Clock) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)
    registry = ActiveStreamRegistry()
    assert registry.acquire("G1:T1", owner_id="G1", session_id="S1")
    coordinator = SessionLifecycleCoordinator(repo, registry)

    outcome = await coordinator.process_idle_transient(claim)

    assert outcome.status == "skipped"
    assert outcome.skip_reason == "active"
    assert (await repo.get_context("S1")).state == "active"


async def test_partial_delete_is_retryable_and_lease_recovery_finishes(
    monkeypatch, clock: Clock
) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)
    calls = 0

    async def clear_context(context_id: str, thread_ids: list[str]):
        nonlocal calls
        from app.agents.buyer.session_state import CleanupCounts

        calls += 1
        if calls == 1:
            raise RuntimeError("crash after first namespace")
        return CleanupCounts(filters=1)

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())

    failed = await coordinator.process_idle_transient(claim)
    assert failed.status == "retryable"
    assert (await repo.get_context("S1")).state == "idle_finalizing"

    clock.advance(31)
    sweep = await coordinator.run_session_context_sweep(
        idle_timeout_s=600,
        lease_s=30,
        batch_size=1,
    )

    assert sweep.recovered == 1
    assert sweep.completed == 1
    assert calls == 2
    assert (await repo.get_context("S1")).state == "idle_expired"


async def test_cancellation_during_delete_leaves_committed_recovery_journal(
    monkeypatch, clock: Clock
) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)

    async def cancel_delete(context_id: str, thread_ids: list[str]):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", cancel_delete)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())

    with pytest.raises(asyncio.CancelledError):
        await coordinator.process_idle_transient(claim)

    assert (await repo.get_context("S1")).state == "idle_finalizing"
    finalization = await repo.get_finalization(claim.finalization_id)
    assert finalization.transient_status == "pending"


async def test_terminal_transient_cleanup_keeps_terminal_state(monkeypatch, clock: Clock) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    terminal = await repo.begin_terminal(7, "S1")
    assert terminal.claim is not None

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts(revert=1)

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    coordinator = SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: ProfileStoreStub(0),
    )

    outcome = await coordinator.process_terminal_transient(terminal.claim)

    assert outcome.status == "completed"
    assert outcome.cleanup.revert == 1
    assert (await repo.get_context("S1")).state == "terminal"
    assert (await repo.get_finalization(terminal.claim.finalization_id)).transient_status == (
        "completed"
    )


async def test_sweep_spends_recovery_capacity_before_new_idle(monkeypatch, clock: Clock) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("recover", "T1", "guest", "G1"))
    await repo.touch(BuyerSessionInput("new", "T1", "guest", "G2"))
    clock.advance(601)
    claims = await repo.claim_expired_contexts(600, 30, 1)
    await repo.mark_idle_finalizing(claims[0])
    untouched_session = "new" if claims[0].session_id == "recover" else "recover"
    clock.advance(31)

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())

    result = await coordinator.run_session_context_sweep(
        idle_timeout_s=600,
        lease_s=30,
        batch_size=1,
    )

    assert result.recovered == 1
    assert result.claimed == 1
    assert (await repo.get_context(untouched_session)).state == "active"


async def test_invalid_recovery_row_does_not_consume_batch_capacity(monkeypatch) -> None:
    from app.core.session_context import FinalizationClaim
    from app.core.session_lifecycle import FinalizationOutcome

    def claim(finalization_id: str) -> FinalizationClaim:
        return FinalizationClaim(
            finalization_id,
            "C1",
            "S1",
            "guest",
            "G1",
            0,
            "idle",
            finalization_id + "-token",
            1_000.0,
        )

    invalid, valid = claim("invalid"), claim("valid")

    class SweepRepo:
        def __init__(self) -> None:
            self.recovery_calls = 0

        async def claim_recoverable_finalizations(self, lease_s: float, batch_size: int):
            self.recovery_calls += 1
            return [invalid] if self.recovery_calls == 1 else ([valid] if batch_size else [])

        async def claim_expired_contexts(
            self, idle_timeout_s: float, lease_s: float, batch_size: int
        ):
            return []

    repo = SweepRepo()
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())

    async def process(candidate: FinalizationClaim) -> FinalizationOutcome:
        if candidate.finalization_id == "invalid":
            return FinalizationOutcome("skipped", skip_reason="invalid")
        return FinalizationOutcome("completed")

    monkeypatch.setattr(coordinator, "process_transient_claim", process)

    result = await coordinator.run_session_context_sweep(
        idle_timeout_s=600,
        lease_s=30,
        batch_size=1,
    )

    assert repo.recovery_calls == 2
    assert result.claimed == 1
    assert result.recovered == 1
    assert result.invalid_recovery == 1
    assert result.completed == 1
