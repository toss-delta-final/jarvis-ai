from __future__ import annotations

import asyncio

import pytest
from langgraph.store.memory import InMemoryStore

from app.agents.buyer.cart import state as cart_state
from app.agents.buyer.session_state import context_thread_key
from app.core import pg_store
from app.core.observability import message_fingerprint
from app.core.session_context import (
    BuyerSessionInput,
    FinalizationClaim,
    SessionClaimConflict,
    SessionContext,
    SessionContextRepository,
    SessionContextUnitOfWork,
)
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


class FailAfterFirstDeleteStore:
    def __init__(self, store: InMemoryStore) -> None:
        self.store = store
        self.delete_calls = 0

    def __getattr__(self, name: str):
        return getattr(self.store, name)

    async def adelete(self, namespace, key) -> None:
        self.delete_calls += 1
        if self.delete_calls == 2:
            raise RuntimeError("fault after first namespace delete")
        await self.store.adelete(namespace, key)


async def _seed_v2_state(store: InMemoryStore, key: str, label: str) -> None:
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
    await store.aput(
        ("buyer_relaxation_offers_v1", key),
        "turn",
        {"offers": {label: {"field": "max_price", "value": 1}}, "applied": None},
    )
    cart_state._last_reco_names[key] = {1: label}


async def _assert_v2_deleted(store: InMemoryStore, key: str) -> None:
    assert await store.aget(("buyer_thread_filters_v2", key), "filters") is None
    assert await store.aget(("buyer_cart_v2", key), "pending") is None
    assert await store.aget(("buyer_cart_v2", key), "last_reco") is None
    assert await store.aget(("buyer_revert_v2", key), "categories") is None
    assert await store.aget(("buyer_relaxation_offers_v1", key), "turn") is None
    assert cart_state._last_reco_names.get(key) is None


async def _assert_v2_preserved(store: InMemoryStore, key: str) -> None:
    assert await store.aget(("buyer_thread_filters_v2", key), "filters") is not None
    assert await store.aget(("buyer_cart_v2", key), "pending") is not None
    assert await store.aget(("buyer_cart_v2", key), "last_reco") is not None
    assert await store.aget(("buyer_revert_v2", key), "categories") is not None
    assert await store.aget(("buyer_relaxation_offers_v1", key), "turn") is not None
    assert cart_state._last_reco_names.get(key) is not None


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
    assert await registry.acquire("G1:T1", owner_id="G1", session_id="S1")
    coordinator = SessionLifecycleCoordinator(repo, registry)

    outcome = await coordinator.process_idle_transient(claim)

    assert outcome.status == "skipped"
    assert outcome.skip_reason == "active"
    assert (await repo.get_context("S1")).state == "active"
    with pytest.raises(SessionClaimConflict):
        await repo.get_finalization(claim.finalization_id)

    await registry.release("G1:T1")
    [replacement] = await repo.claim_expired_contexts(600, 30, 1)
    assert replacement.finalization_id != claim.finalization_id


async def test_snapshot_failure_abandons_idle_prephase_without_memory_mutation(
    monkeypatch, clock: Clock
) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)

    class BrokenProfile:
        async def get_session_ctx_snapshot(self, key: str):
            raise RuntimeError("snapshot unavailable")

    coordinator = SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=BrokenProfile,
    )

    failed = await coordinator.process_idle_transient(claim)

    assert failed.status == "retryable"
    assert (await repo.get_context("S1")).state == "active"
    with pytest.raises(SessionClaimConflict):
        await repo.get_finalization(claim.finalization_id)

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    coordinator = SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: ProfileStoreStub(0),
    )
    sweep = await coordinator.run_session_context_sweep(
        idle_timeout_s=600,
        lease_s=30,
        batch_size=1,
    )
    assert sweep.completed == 1
    assert (await repo.get_context("S1")).state == "idle_expired"


async def test_expired_idle_prephase_is_reissued_by_fresh_claim_stage(
    clock: Clock,
) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    clock.advance(601)
    [original] = await repo.claim_expired_contexts(600, 30, 1)

    clock.advance(31)
    [reissued] = await repo.claim_expired_contexts(600, 30, 1)

    assert reissued.finalization_id == original.finalization_id
    assert reissued.claim_token != original.claim_token
    assert reissued.lease_expires_at != original.lease_expires_at
    assert (await repo.get_context("S1")).state == "active"
    with pytest.raises(SessionClaimConflict):
        await repo.validate_for_delete(original)


async def test_failed_fast_abandon_is_durably_recovered_by_public_fresh_stage(
    monkeypatch,
    clock: Clock,
) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)

    class BrokenProfile:
        async def get_session_ctx_snapshot(self, key: str):
            raise RuntimeError("snapshot unavailable")

    first = SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=BrokenProfile,
    )

    async def failed_abandon(candidate: FinalizationClaim) -> bool:
        return False

    monkeypatch.setattr(first, "_abandon_idle_prephase", failed_abandon)
    failed = await first.process_idle_transient(claim)
    assert failed.status == "retryable"
    assert (await repo.get_finalization(claim.finalization_id)).transient_status == "pending"

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    clock.advance(31)
    result = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: ProfileStoreStub(0),
    ).run_session_context_sweep(idle_timeout_s=600, lease_s=30, batch_size=1)

    assert result.completed == 1
    finalization = await repo.get_finalization(claim.finalization_id)
    assert finalization.transient_status == "completed"
    assert (await repo.get_context("S1")).state == "idle_expired"


async def test_phase_a_cancellation_abandons_idle_claim_and_preserves_cancellation(
    clock: Clock,
) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("cancel-session", "T1", "member", "7"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)

    class CancelProfile:
        async def get_session_ctx_snapshot(self, key: str):
            raise asyncio.CancelledError

    coordinator = SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=CancelProfile,
    )
    with pytest.raises(asyncio.CancelledError):
        await coordinator.process_idle_transient(claim)

    assert (await repo.get_context("cancel-session")).state == "active"
    with pytest.raises(SessionClaimConflict):
        await repo.get_finalization(claim.finalization_id)
    [replacement] = await repo.claim_expired_contexts(600, 30, 1)
    assert replacement.finalization_id != claim.finalization_id


async def test_abandon_rejects_same_token_when_lease_was_replaced(clock: Clock) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    clock.advance(601)
    [stale] = await repo.claim_expired_contexts(600, 30, 1)
    finalization = repo._finalizations[stale.finalization_id]
    finalization.lease_expires_at = stale.lease_expires_at + 1

    async with repo.lock_session("S1") as uow:
        abandoned = await uow.abandon_idle_prephase(stale)

    assert abandoned is False
    assert await repo.get_finalization(stale.finalization_id)


async def test_abandon_rejects_stale_token_and_committed_phase_a(clock: Clock) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "guest", "G1"))
    clock.advance(601)
    [stale] = await repo.claim_expired_contexts(600, 30, 1)
    clock.advance(31)
    [replacement] = await repo.claim_expired_contexts(600, 30, 1)

    async with repo.lock_session("S1") as uow:
        assert await uow.abandon_idle_prephase(stale) is False
        await uow.prepare_idle_finalizing_with_watermark(replacement, None)
        assert await uow.abandon_idle_prephase(replacement) is False

    assert (await repo.get_context("S1")).state == "idle_finalizing"


async def test_failed_abandon_is_self_healed_by_next_sweep(monkeypatch, clock: Clock) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    clock.advance(601)
    [orphan] = await repo.claim_expired_contexts(600, 30, 1)

    class BrokenProfile:
        async def get_session_ctx_snapshot(self, key: str):
            raise RuntimeError("snapshot unavailable")

    failed_coordinator = SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=BrokenProfile,
    )

    async def failed_abandon(claim: FinalizationClaim) -> bool:
        return False

    monkeypatch.setattr(failed_coordinator, "_abandon_idle_prephase", failed_abandon)
    outcome = await failed_coordinator.process_idle_transient(orphan)
    assert outcome.status == "retryable"
    assert (await repo.get_context("S1")).state == "active"
    assert (await repo.get_finalization(orphan.finalization_id)).claim_token == (orphan.claim_token)

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    clock.advance(31)
    recovered_coordinator = SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: ProfileStoreStub(0),
    )
    result = await recovered_coordinator.run_session_context_sweep(
        idle_timeout_s=600,
        lease_s=30,
        batch_size=1,
    )

    assert result.completed == 1
    with pytest.raises(SessionClaimConflict):
        await repo.validate_for_delete(orphan)
    finalizations = list(repo._finalizations.values())
    assert len(finalizations) == 1
    assert finalizations[0].claim_token is None
    assert finalizations[0].transient_status == "completed"


async def test_cancellation_survives_failed_abandon_and_next_sweep_self_heals(
    monkeypatch, clock: Clock
) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    clock.advance(601)
    [orphan] = await repo.claim_expired_contexts(600, 30, 1)

    class CancelProfile:
        async def get_session_ctx_snapshot(self, key: str):
            raise asyncio.CancelledError

    failed_coordinator = SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=CancelProfile,
    )

    async def failed_abandon(claim: FinalizationClaim) -> bool:
        return False

    monkeypatch.setattr(failed_coordinator, "_abandon_idle_prephase", failed_abandon)
    with pytest.raises(asyncio.CancelledError):
        await failed_coordinator.process_idle_transient(orphan)
    assert (await repo.get_finalization(orphan.finalization_id)).claim_token == (orphan.claim_token)

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    clock.advance(31)
    result = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: ProfileStoreStub(0),
    ).run_session_context_sweep(idle_timeout_s=600, lease_s=30, batch_size=1)

    assert result.completed == 1
    assert (await repo.get_context("S1")).state == "idle_expired"


async def test_terminal_snapshot_failure_keeps_journal_for_lease_recovery(
    monkeypatch, clock: Clock
) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    terminal = await repo.begin_terminal(7, "S1")
    assert terminal.claim is not None

    class BrokenProfile:
        async def get_session_ctx_snapshot(self, key: str):
            raise RuntimeError("snapshot unavailable")

    failed = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=BrokenProfile,
    ).process_terminal_transient(terminal.claim)
    assert failed.status == "retryable"
    pending = await repo.get_finalization(terminal.claim.finalization_id)
    assert pending.transient_status == "pending"
    assert pending.watermark_status == "pending"

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    clock.advance(901)
    recovered = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: ProfileStoreStub(0),
    ).run_session_context_sweep(idle_timeout_s=600, lease_s=30, batch_size=1)
    assert recovered.recovered == 1
    assert recovered.completed == 1
    assert (await repo.get_context("S1")).state == "terminal"


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


async def test_i20_supersedes_idle_between_phase_a_and_phase_b(monkeypatch, clock: Clock) -> None:
    repo = SessionContextRepository(clock=clock)
    context = await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    clock.advance(601)
    [idle] = await repo.claim_expired_contexts(600, 30, 1)
    async with repo.lock_session("S1") as uow:
        await uow.prepare_idle_finalizing_with_watermark(idle, 0)

    terminal = await repo.begin_terminal(7, "S1")

    assert terminal.context.state == "terminal"
    assert terminal.context.generation == idle.generation + 1
    old = await repo.get_finalization(idle.finalization_id)
    assert old.status == "superseded"
    assert old.claim_token is None and old.lease_expires_at is None
    stale = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
    ).process_idle_transient(idle)
    assert stale.status == "skipped"
    assert stale.skip_reason == "superseded"
    assert await repo.get_threads(context.context_id) == ["T1"]

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts(filters=1)

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    assert terminal.claim is not None
    completed = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: ProfileStoreStub(0),
    ).process_terminal_transient(terminal.claim)
    assert completed.status == "completed"
    assert (await repo.get_context("S1")).state == "terminal"


async def test_terminal_gate_is_durable_before_existing_stream_finishes(
    monkeypatch, clock: Clock
) -> None:
    repo = SessionContextRepository(clock=clock)
    registry = ActiveStreamRegistry()
    await repo.touch(BuyerSessionInput("S1", "T1", "member", "7"))
    assert await registry.acquire("7:T1", owner_id="7", session_id="S1")

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts()

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    task = asyncio.create_task(
        SessionLifecycleCoordinator(
            repo,
            registry,
            profile_store_factory=lambda: ProfileStoreStub(0),
        ).begin_terminal(7, "S1")
    )
    await asyncio.sleep(0)

    context = await repo.get_context("S1")
    assert context is not None and context.state == "terminal"
    assert not task.done()
    await registry.release("7:T1")
    outcome = await task
    assert outcome.context.state == "terminal"


async def test_terminal_inherits_captured_idle_watermark_and_supersession_relation(
    clock: Clock,
) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("inherit-pending", "T1", "member", "7"))
    clock.advance(601)
    [idle] = await repo.claim_expired_contexts(600, 30, 1)
    async with repo.lock_session("inherit-pending") as uow:
        await uow.prepare_idle_finalizing_with_watermark(idle, 17)

    terminal = await repo.begin_terminal(7, "inherit-pending")

    assert terminal.claim is not None
    assert terminal.finalization.supersedes_finalization_id == idle.finalization_id
    assert terminal.finalization.watermark_status == "captured"
    assert terminal.finalization.profile_watermark == 17
    assert terminal.finalization.transient_status == "pending"
    superseded = await repo.get_finalization(idle.finalization_id)
    assert superseded.status == "superseded"
    assert superseded.superseded_at is not None


async def test_terminal_inherits_completed_idle_transient_without_new_cleanup_claim(
    clock: Clock,
) -> None:
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput("inherit-complete", "T1", "member", "7"))
    clock.advance(601)
    [idle] = await repo.claim_expired_contexts(600, 30, 1)
    async with repo.lock_session("inherit-complete") as uow:
        await uow.prepare_idle_finalizing_with_watermark(idle, 23)
        await uow.complete_idle_delete(idle)

    terminal = await repo.begin_terminal(7, "inherit-complete")

    assert terminal.duplicate is False
    assert terminal.claim is None
    assert terminal.finalization.supersedes_finalization_id == idle.finalization_id
    assert terminal.finalization.watermark_status == "captured"
    assert terminal.finalization.profile_watermark == 23
    assert terminal.finalization.transient_status == "completed"
    assert terminal.finalization.profile_status == "pending"


async def test_actual_partial_idle_delete_recovers_all_namespaces_and_preserves_other_context(
    clock: Clock,
) -> None:
    repo = SessionContextRepository(clock=clock)
    target = await repo.touch(BuyerSessionInput("target", "T1", "guest", "G1"))
    other = await repo.touch(BuyerSessionInput("other", "T1", "guest", "G2"))
    target_key = context_thread_key(target.context_id, "T1")
    other_key = context_thread_key(other.context_id, "T1")
    store = InMemoryStore()
    await _seed_v2_state(store, target_key, "target")
    await _seed_v2_state(store, other_key, "other")
    fault_store = FailAfterFirstDeleteStore(store)
    pg_store.set_store(fault_store)
    clock.advance(601)
    claims = await repo.claim_expired_contexts(600, 30, 10)
    claim = next(item for item in claims if item.session_id == "target")

    failed = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
    ).process_idle_transient(claim)

    assert failed.status == "retryable"
    assert fault_store.delete_calls == 2
    assert await store.aget(("buyer_thread_filters_v2", target_key), "filters") is None
    assert await store.aget(("buyer_cart_v2", target_key), "pending") is not None
    assert (await repo.get_context("target")).state == "idle_finalizing"
    assert (await repo.get_finalization(claim.finalization_id)).transient_status == "pending"
    await _assert_v2_preserved(store, other_key)

    pg_store.set_store(store)
    clock.advance(31)
    result = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
    ).run_session_context_sweep(idle_timeout_s=600, lease_s=30, batch_size=1)

    assert result.completed == 1
    await _assert_v2_deleted(store, target_key)
    await _assert_v2_preserved(store, other_key)


async def test_i20_after_partial_idle_delete_finishes_idempotent_terminal_cleanup(
    clock: Clock,
) -> None:
    repo = SessionContextRepository(clock=clock)
    target = await repo.touch(BuyerSessionInput("target", "T1", "member", "7"))
    target_key = context_thread_key(target.context_id, "T1")
    store = InMemoryStore()
    await _seed_v2_state(store, target_key, "target")
    fault_store = FailAfterFirstDeleteStore(store)
    pg_store.set_store(fault_store)
    clock.advance(601)
    [idle] = await repo.claim_expired_contexts(600, 30, 1)

    failed = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
    ).process_idle_transient(idle)
    assert failed.status == "retryable"
    assert (await repo.get_context("target")).state == "idle_finalizing"
    assert await store.aget(("buyer_thread_filters_v2", target_key), "filters") is None
    assert await store.aget(("buyer_cart_v2", target_key), "pending") is not None

    terminal = await repo.begin_terminal(7, "target")
    assert terminal.claim is not None
    assert (await repo.get_finalization(idle.finalization_id)).status == "superseded"
    pg_store.set_store(store)
    completed = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: ProfileStoreStub(0),
    ).process_terminal_transient(terminal.claim)

    assert completed.status == "completed"
    assert (await repo.get_context("target")).state == "terminal"
    await _assert_v2_deleted(store, target_key)


async def test_actual_partial_terminal_delete_recovers_and_keeps_terminal(
    clock: Clock,
) -> None:
    repo = SessionContextRepository(clock=clock)
    target = await repo.touch(BuyerSessionInput("target", "T1", "member", "7"))
    other = await repo.touch(BuyerSessionInput("other", "T1", "guest", "G2"))
    terminal = await repo.begin_terminal(7, "target")
    assert terminal.claim is not None
    target_key = context_thread_key(target.context_id, "T1")
    other_key = context_thread_key(other.context_id, "T1")
    store = InMemoryStore()
    await _seed_v2_state(store, target_key, "target")
    await _seed_v2_state(store, other_key, "other")
    fault_store = FailAfterFirstDeleteStore(store)
    pg_store.set_store(fault_store)
    profile = ProfileStoreStub(0)

    failed = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: profile,
    ).process_terminal_transient(terminal.claim)

    assert failed.status == "retryable"
    assert await store.aget(("buyer_thread_filters_v2", target_key), "filters") is None
    assert await store.aget(("buyer_cart_v2", target_key), "pending") is not None
    assert (await repo.get_context("target")).state == "terminal"
    await _assert_v2_preserved(store, other_key)

    pg_store.set_store(store)
    clock.advance(901)
    result = await SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: profile,
    ).run_session_context_sweep(idle_timeout_s=600, lease_s=30, batch_size=1)

    assert result.completed == 1
    assert (await repo.get_context("target")).state == "terminal"
    await _assert_v2_deleted(store, target_key)
    await _assert_v2_preserved(store, other_key)


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

        return CleanupCounts(revert=1, repurchase=1, relaxation_offers=1)

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    coordinator = SessionLifecycleCoordinator(
        repo,
        ActiveStreamRegistry(),
        profile_store_factory=lambda: ProfileStoreStub(0),
    )

    outcome = await coordinator.process_terminal_transient(terminal.claim)

    assert outcome.status == "completed"
    assert outcome.cleanup.revert == 1
    assert outcome.cleanup.repurchase == 1
    assert outcome.cleanup.relaxation_offers == 1
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


async def test_sweep_applies_configured_bounded_concurrency_recovery_first(
    monkeypatch,
) -> None:
    from app.core.config import get_settings
    from app.core.session_lifecycle import FinalizationOutcome

    def claim(name: str) -> FinalizationClaim:
        return FinalizationClaim(
            name,
            "C-" + name,
            "S-" + name,
            "guest",
            "G-" + name,
            0,
            "idle",
            "token-" + name,
            1_000.0,
        )

    recovery = [claim(f"r{i}") for i in range(3)]
    fresh = [claim(f"f{i}") for i in range(3)]

    class Repo:
        def __init__(self) -> None:
            self.recovery_sent = False
            self.fresh_sent = False

        async def claim_recoverable_finalizations(self, lease_s: float, batch_size: int):
            if self.recovery_sent:
                return []
            self.recovery_sent = True
            return recovery[:batch_size]

        async def claim_expired_contexts(
            self, idle_timeout_s: float, lease_s: float, batch_size: int
        ):
            if self.fresh_sent:
                return []
            self.fresh_sent = True
            return fresh[:batch_size]

        async def list_recoverable_profile_phases(self, batch_size: int):
            return []

    settings = get_settings().model_copy(update={"profile_idle_max_concurrency": 2})
    monkeypatch.setattr("app.core.session_lifecycle.get_settings", lambda: settings)
    coordinator = SessionLifecycleCoordinator(Repo(), ActiveStreamRegistry())
    active = 0
    peak = 0
    started: list[str] = []
    release = asyncio.Event()

    async def process(candidate: FinalizationClaim):
        nonlocal active, peak
        started.append(candidate.finalization_id)
        active += 1
        peak = max(peak, active)
        if active == 2:
            release.set()
        await release.wait()
        await asyncio.sleep(0)
        active -= 1
        return FinalizationOutcome("completed")

    monkeypatch.setattr(coordinator, "process_transient_claim", process)
    result = await coordinator.run_session_context_sweep(
        idle_timeout_s=600,
        lease_s=30,
        batch_size=4,
    )

    assert peak == 2
    assert started[:3] == ["r0", "r1", "r2"]
    assert started[3:] == ["f0"]
    assert result.recovered == 3
    assert result.claimed == 4
    assert result.completed == 4


async def test_sweep_isolates_one_claim_exception_without_cancelling_wave(monkeypatch) -> None:
    from app.core.session_lifecycle import FinalizationOutcome

    claims = [
        FinalizationClaim(
            name,
            "C-" + name,
            "S-" + name,
            "guest",
            "G-" + name,
            0,
            "idle",
            "token-" + name,
            1_000.0,
        )
        for name in ("bad", "good")
    ]

    class Repo:
        sent = False

        async def claim_recoverable_finalizations(self, lease_s: float, batch_size: int):
            if self.sent:
                return []
            self.sent = True
            return claims

        async def claim_expired_contexts(self, *args):
            return []

        async def list_recoverable_profile_phases(self, batch_size: int):
            return []

    coordinator = SessionLifecycleCoordinator(Repo(), ActiveStreamRegistry())

    async def process(candidate: FinalizationClaim):
        if candidate.finalization_id == "bad":
            raise RuntimeError("one claim failed")
        return FinalizationOutcome("completed")

    monkeypatch.setattr(coordinator, "process_transient_claim", process)
    result = await coordinator.run_session_context_sweep(batch_size=2)
    assert result.completed == 1
    assert result.retryable == 1
    assert result.claimed == 2


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

        async def list_recoverable_profile_phases(self, batch_size: int):
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


async def test_sweep_bounds_examined_invalid_rows_even_when_repository_never_ends(
    monkeypatch,
) -> None:
    from app.core.session_context import FinalizationClaim
    from app.core.session_lifecycle import FinalizationOutcome

    class EndlessInvalidRepo:
        def __init__(self) -> None:
            self.calls = 0

        async def claim_recoverable_finalizations(self, lease_s: float, batch_size: int):
            self.calls += 1
            suffix = str(self.calls)
            return [
                FinalizationClaim(
                    suffix,
                    "C" + suffix,
                    "S" + suffix,
                    "guest",
                    "G" + suffix,
                    0,
                    "idle",
                    "token-" + suffix,
                    1_000.0,
                )
            ]

        async def claim_expired_contexts(
            self, idle_timeout_s: float, lease_s: float, batch_size: int
        ):
            raise AssertionError("examined bound must stop before fresh claims")

        async def list_recoverable_profile_phases(self, batch_size: int):
            return []

    repo = EndlessInvalidRepo()
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())

    async def invalid(claim):
        return FinalizationOutcome("skipped", skip_reason="invalid")

    monkeypatch.setattr(coordinator, "process_transient_claim", invalid)

    result = await coordinator.run_session_context_sweep(
        idle_timeout_s=600,
        lease_s=30,
        batch_size=1,
        max_examined=3,
    )

    assert repo.calls == 3
    assert result.examined == 3
    assert result.examined_limit_reached is True
    assert result.claimed == 0
    assert result.invalid_recovery == 3


async def test_phase_failure_log_uses_session_fingerprint(caplog, clock: Clock) -> None:
    session_id = "raw-secret-session"
    repo = SessionContextRepository(clock=clock)
    await repo.touch(BuyerSessionInput(session_id, "T1", "member", "7"))
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 30, 1)

    class BrokenProfile:
        async def get_session_ctx_snapshot(self, key: str):
            raise RuntimeError("snapshot unavailable")

    with caplog.at_level("WARNING"):
        await SessionLifecycleCoordinator(
            repo,
            ActiveStreamRegistry(),
            profile_store_factory=BrokenProfile,
        ).process_idle_transient(claim)

    assert session_id not in caplog.text
    assert message_fingerprint(session_id)[1] in caplog.text


async def test_idle_completion_records_evidence_and_state_before_thread_delete(
    monkeypatch,
) -> None:
    operations: list[str] = []

    class Cursor:
        def __init__(self, row) -> None:
            self.row = row

        async def fetchone(self):
            return self.row

    class Connection:
        async def execute(self, sql: str, params):
            normalized = " ".join(sql.split())
            operations.append(normalized)
            return Cursor(None if "DELETE FROM chat_session_threads" in normalized else ("ok",))

    uow = SessionContextUnitOfWork(SessionContextRepository(), "S1")
    uow.conn = Connection()
    claim = FinalizationClaim("F1", "C1", "S1", "guest", "G1", 0, "idle", "token", 1_000.0)

    async def validated(candidate: FinalizationClaim) -> SessionContext:
        return SessionContext("C1", "S1", "guest", "G1", 0, "idle_finalizing")

    monkeypatch.setattr(uow, "validate_idle_delete", validated)

    await uow.complete_idle_delete(claim)

    assert operations[0].startswith("UPDATE chat_session_finalizations")
    assert operations[1].startswith("UPDATE chat_session_contexts")
    assert operations[2].startswith("DELETE FROM chat_session_threads")


async def test_idle_completion_does_not_delete_threads_when_state_transition_loses_cas(
    monkeypatch,
) -> None:
    operations: list[str] = []

    class Cursor:
        def __init__(self, row) -> None:
            self.row = row

        async def fetchone(self):
            return self.row

    class Connection:
        async def execute(self, sql: str, params):
            normalized = " ".join(sql.split())
            operations.append(normalized)
            if normalized.startswith("UPDATE chat_session_contexts"):
                return Cursor(None)
            return Cursor(("ok",))

    uow = SessionContextUnitOfWork(SessionContextRepository(), "S1")
    uow.conn = Connection()
    claim = FinalizationClaim("F1", "C1", "S1", "guest", "G1", 0, "idle", "token", 1_000.0)

    async def validated(candidate: FinalizationClaim) -> SessionContext:
        return SessionContext("C1", "S1", "guest", "G1", 0, "idle_finalizing")

    monkeypatch.setattr(uow, "validate_idle_delete", validated)

    with pytest.raises(SessionClaimConflict):
        await uow.complete_idle_delete(claim)

    assert all(
        not operation.startswith("DELETE FROM chat_session_threads") for operation in operations
    )
