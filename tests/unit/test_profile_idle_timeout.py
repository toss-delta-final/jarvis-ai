"""AI 내부 inactivity timeout과 공통 session finalizer (이슈 #79)."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.agents.profile import finalizer, idle_timeout, processed_events, session_activity
from app.agents.profile.store import get_profile_store
from app.core import session_context
from app.core.config import get_settings
from app.core.conversation import conversation_key
from app.core.session_context import (
    BuyerSessionInput,
    ProfileRecoveryCandidate,
    SessionContextRepository,
)
from app.core.session_lifecycle import SessionLifecycleCoordinator
from app.core.stream import ActiveStreamRegistry


class _LLM:
    async def complete(self, *, system, user, tier, max_tokens=1024, json_output=True):
        if "델타 추출기" in system:
            return json.dumps(
                {
                    "deltas": [
                        {
                            "fact": "무선이어폰 선호",
                            "salience": 0.9,
                            "explicit": True,
                            "repetitionEma": 0.0,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        return "# 취향\n- 무선이어폰 선호"

    async def stream(self, **kwargs):
        yield "x"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_profile_recovery_respects_configured_concurrency(monkeypatch) -> None:
    candidates = [
        ProfileRecoveryCandidate(f"f{i}", f"c{i}", f"s{i}", str(i), 0, 0) for i in range(4)
    ]

    class Repo:
        async def list_recoverable_profile_phases(self, batch_size: int):
            return candidates[:batch_size]

    settings = get_settings().model_copy(update={"profile_idle_max_concurrency": 2})
    monkeypatch.setattr(finalizer, "get_settings", lambda: settings)
    active = 0
    peak = 0
    two_started = asyncio.Event()
    release = asyncio.Event()

    async def process(candidate, repository, current_settings):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            two_started.set()
        await release.wait()
        active -= 1
        return None

    monkeypatch.setattr(finalizer, "_process_profile_candidate", process)
    task = asyncio.create_task(finalizer._process_recoverable_profile_phases(Repo(), 4))
    await asyncio.wait_for(two_started.wait(), timeout=0.1)
    release.set()
    await task
    assert peak == 2


async def _complete_idle_transient(
    repo: SessionContextRepository,
    coordinator: SessionLifecycleCoordinator,
    clock: _Clock,
) -> tuple[str, int]:
    clock.advance(601)
    [claim] = await repo.claim_expired_contexts(600, 0.01, 1)
    outcome = await coordinator.process_idle_transient(claim)
    assert outcome.status == "completed"
    return claim.context_id, claim.generation


async def test_profile_lease_expiry_never_preempts_live_local_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    monkeypatch.setattr(session_context, "_default_repository", repo)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    context = await repo.touch(BuyerSessionInput("long-llm", "t1", "member", "701"))
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("701", "long-llm"), "첫 취향")
    await _complete_idle_transient(repo, coordinator, clock)
    entered = asyncio.Event()
    proceed = asyncio.Event()
    active = 0
    peak = 0

    class _BlockingLLM(_LLM):
        async def complete(self, **kwargs):
            nonlocal active, peak
            if "델타 추출기" in kwargs["system"]:
                active += 1
                peak = max(peak, active)
                entered.set()
                await proceed.wait()
                active -= 1
            return await super().complete(**kwargs)

    monkeypatch.setattr(finalizer, "get_llm", lambda: _BlockingLLM())
    first = asyncio.create_task(finalizer.process_recoverable_profile_phases(1))
    await entered.wait()
    clock.advance(get_settings().profile_idle_claim_ttl_s + 1)
    second = asyncio.create_task(finalizer.process_recoverable_profile_phases(1))
    await asyncio.sleep(0)

    assert not first.done()
    assert not second.done()
    assert peak == 1
    proceed.set()
    first_result, second_result = await asyncio.gather(first, second)
    [result] = first_result
    assert second_result == [result]
    assert result.status is finalizer.ProfilePhaseStatus.COMPLETED
    journal = next(iter(repo._finalizations.values()))
    assert journal.transient_status == "completed"
    assert journal.profile_status == "completed"
    assert (
        await processed_events.get_status(
            f"chat-profile:{context.context_id}:{journal.generation}:idle"
        )
        == "completed"
    )


async def test_expired_processing_profile_is_reclaimed_with_new_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    monkeypatch.setattr(session_context, "_default_repository", repo)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    context = await repo.touch(BuyerSessionInput("expired-profile", "t1", "member", "705"))
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("705", "expired-profile"), "회수할 취향")
    await _complete_idle_transient(repo, coordinator, clock)
    [candidate] = await repo.list_recoverable_profile_phases(1)
    first_claim = await repo.claim_profile_phase(candidate.finalization_id, 30)
    assert first_claim is not None

    clock.advance(31)
    [expired] = await repo.list_recoverable_profile_phases(1)
    assert expired.finalization_id == candidate.finalization_id
    monkeypatch.setattr(finalizer, "get_llm", lambda: _LLM())
    [result] = await finalizer.process_recoverable_profile_phases(1)

    assert result.status is finalizer.ProfilePhaseStatus.COMPLETED
    journal = await repo.get_finalization(candidate.finalization_id)
    assert journal.profile_status == "completed"
    assert journal.claim_token is None
    assert (
        await processed_events.get_status(
            f"chat-profile:{context.context_id}:{journal.generation}:idle"
        )
        == "completed"
    )


async def test_cancelled_profile_task_records_retryable_then_public_recovery_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    monkeypatch.setattr(session_context, "_default_repository", repo)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    context = await repo.touch(BuyerSessionInput("cancel-recovery", "t1", "member", "706"))
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("706", "cancel-recovery"), "취소 복구 취향")
    await _complete_idle_transient(repo, coordinator, clock)
    entered = asyncio.Event()

    async def _cancel_target(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    original_checkpoint = finalizer.process_profile_checkpoint
    monkeypatch.setattr(finalizer, "process_profile_checkpoint", _cancel_target)
    recovery = asyncio.create_task(finalizer.process_recoverable_profile_phases(1))
    await entered.wait()
    active = finalizer._active_profile_tasks._active[context.context_id]
    active.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery
    await asyncio.sleep(0)

    journal = next(iter(repo._finalizations.values()))
    assert journal.profile_status == "retryable"
    assert journal.claim_token is None
    assert finalizer._active_profile_tasks._active == {}

    monkeypatch.setattr(finalizer, "process_profile_checkpoint", original_checkpoint)
    monkeypatch.setattr(finalizer, "get_llm", lambda: _LLM())
    [result] = await finalizer.process_recoverable_profile_phases(1)
    assert result.status is finalizer.ProfilePhaseStatus.COMPLETED


async def test_stale_profile_token_cannot_overwrite_reclaimed_result() -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    await repo.touch(BuyerSessionInput("stale-token", "t1", "member", "707"))
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("707", "stale-token"), "token 취향")
    await _complete_idle_transient(repo, coordinator, clock)
    [candidate] = await repo.list_recoverable_profile_phases(1)
    first = await repo.claim_profile_phase(candidate.finalization_id, 30)
    assert first is not None
    clock.advance(31)
    second = await repo.claim_profile_phase(candidate.finalization_id, 30)
    assert second is not None

    with pytest.raises(session_context.SessionClaimConflict):
        await repo.record_claimed_profile_phase(
            candidate.finalization_id,
            first.claim_token,
            "completed",
        )
    journal = await repo.get_finalization(candidate.finalization_id)
    assert journal.claim_token == second.claim_token
    assert await repo.record_claimed_profile_phase(
        candidate.finalization_id,
        second.claim_token,
        "completed",
    )


async def test_stale_profile_claim_token_cannot_overwrite_reclaimer() -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    await repo.touch(BuyerSessionInput("stale-token", "t1", "member", "706"))
    await _complete_idle_transient(repo, coordinator, clock)
    [candidate] = await repo.list_recoverable_profile_phases(1)
    first = await repo.claim_profile_phase(candidate.finalization_id, 30)
    assert first is not None
    clock.advance(31)
    second = await repo.claim_profile_phase(candidate.finalization_id, 30)
    assert second is not None and second.claim_token != first.claim_token

    with pytest.raises(session_context.SessionClaimConflict):
        await repo.record_claimed_profile_phase(
            candidate.finalization_id,
            first.claim_token,
            "completed",
        )
    processing = await repo.get_finalization(candidate.finalization_id)
    assert processing.profile_status == "processing"
    assert processing.claim_token == second.claim_token

    await repo.record_claimed_profile_phase(
        candidate.finalization_id,
        second.claim_token,
        "completed",
    )
    completed = await repo.get_finalization(candidate.finalization_id)
    assert completed.profile_status == "completed"
    assert completed.claim_token is None


async def test_profile_record_failure_is_recovered_after_processing_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    monkeypatch.setattr(session_context, "_default_repository", repo)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    await repo.touch(BuyerSessionInput("record-failure", "t1", "member", "707"))
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("707", "record-failure"), "복구할 취향")
    await _complete_idle_transient(repo, coordinator, clock)
    monkeypatch.setattr(finalizer, "get_llm", lambda: _LLM())
    original_record = repo.record_claimed_profile_phase
    attempts = 0

    async def _fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("record unavailable")
        return await original_record(*args, **kwargs)

    monkeypatch.setattr(repo, "record_claimed_profile_phase", _fail_once)
    assert await finalizer.process_recoverable_profile_phases(1) == []
    [processing] = repo._finalizations.values()
    assert processing.profile_status == "processing"

    clock.advance(get_settings().profile_idle_claim_ttl_s + 1)
    [result] = await finalizer.process_recoverable_profile_phases(1)
    assert result.status is finalizer.ProfilePhaseStatus.DUPLICATE
    assert (await repo.get_finalization(processing.finalization_id)).profile_status == "completed"


async def test_cancelled_profile_processor_marks_retryable_and_can_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    monkeypatch.setattr(session_context, "_default_repository", repo)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    context = await repo.touch(BuyerSessionInput("cancel-profile", "t1", "member", "708"))
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("708", "cancel-profile"), "취소 후 복구")
    await _complete_idle_transient(repo, coordinator, clock)
    entered = asyncio.Event()

    class _BlockingLLM(_LLM):
        async def complete(self, **kwargs):
            if "델타 추출기" in kwargs["system"]:
                entered.set()
                await asyncio.Event().wait()
            return await super().complete(**kwargs)

    monkeypatch.setattr(finalizer, "get_llm", lambda: _BlockingLLM())
    waiter = asyncio.create_task(finalizer.process_recoverable_profile_phases(1))
    await entered.wait()
    active = finalizer._active_profile_tasks._active[context.context_id]
    active.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)
    [retryable] = repo._finalizations.values()
    assert retryable.profile_status == "retryable"

    monkeypatch.setattr(finalizer, "get_llm", lambda: _LLM())
    [result] = await finalizer.process_recoverable_profile_phases(1)
    assert result.status is finalizer.ProfilePhaseStatus.COMPLETED


async def test_new_idle_generation_revalidates_after_joining_previous_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    monkeypatch.setattr(session_context, "_default_repository", repo)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    context = await repo.touch(BuyerSessionInput("generation-race", "t1", "member", "702"))
    store = await get_profile_store()
    key = conversation_key("702", "generation-race")
    await store.append_session_ctx(key, "첫 세대 취향")
    _, first_generation = await _complete_idle_transient(repo, coordinator, clock)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    delta_calls = 0

    class _TwoGenerationLLM(_LLM):
        async def complete(self, **kwargs):
            nonlocal delta_calls
            if "델타 추출기" in kwargs["system"]:
                delta_calls += 1
                if delta_calls == 1:
                    first_entered.set()
                    await release_first.wait()
            return await super().complete(**kwargs)

    llm = _TwoGenerationLLM()
    monkeypatch.setattr(finalizer, "get_llm", lambda: llm)
    first = asyncio.create_task(finalizer.process_recoverable_profile_phases(1))
    await first_entered.wait()

    clock.advance(1)
    resumed = await repo.touch(BuyerSessionInput("generation-race", "t2", "member", "702"))
    await store.append_session_ctx(key, "두 번째 세대 취향")
    _, second_generation = await _complete_idle_transient(repo, coordinator, clock)
    assert second_generation == resumed.generation
    second = asyncio.create_task(finalizer.process_recoverable_profile_phases(2))
    await asyncio.sleep(0)

    release_first.set()
    await asyncio.gather(first, second)

    assert delta_calls == 2
    assert (
        await processed_events.get_status(
            f"chat-profile:{context.context_id}:{first_generation}:idle"
        )
        == "completed"
    )
    assert (
        await processed_events.get_status(
            f"chat-profile:{context.context_id}:{second_generation}:idle"
        )
        == "completed"
    )
    journals = sorted(repo._finalizations.values(), key=lambda row: row.generation)
    assert [row.profile_status for row in journals] == ["completed", "completed"]


async def test_terminal_joins_live_idle_profile_task_without_parallel_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    monkeypatch.setattr(session_context, "_default_repository", repo)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    context = await repo.touch(BuyerSessionInput("terminal-join", "t1", "member", "703"))
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("703", "terminal-join"), "종료 전 취향")
    _, idle_generation = await _complete_idle_transient(repo, coordinator, clock)
    entered = asyncio.Event()
    proceed = asyncio.Event()
    active = 0
    peak = 0

    class _BlockingLLM(_LLM):
        async def complete(self, **kwargs):
            nonlocal active, peak
            if "델타 추출기" in kwargs["system"]:
                active += 1
                peak = max(peak, active)
                entered.set()
                await proceed.wait()
                active -= 1
            return await super().complete(**kwargs)

    monkeypatch.setattr(finalizer, "get_llm", lambda: _BlockingLLM())
    idle_task = asyncio.create_task(finalizer.process_recoverable_profile_phases(1))
    await entered.wait()
    terminal_task = asyncio.create_task(coordinator.begin_terminal(703, "terminal-join"))
    await asyncio.sleep(0)

    terminal_context = await repo.get_context("terminal-join")
    assert terminal_context is not None and terminal_context.state == "terminal"
    assert peak == 1
    proceed.set()
    terminal = await terminal_task
    await idle_task

    assert peak == 1
    assert terminal.context.state == "terminal"
    terminal_journal = await repo.get_finalization(terminal.finalization.finalization_id)
    assert terminal_journal.transient_status == "completed"
    assert terminal_journal.profile_status == "completed"
    assert (
        await processed_events.get_status(
            f"chat-profile:{context.context_id}:{idle_generation}:idle"
        )
        == "completed"
    )
    assert (
        await processed_events.get_status(
            f"chat-profile:{context.context_id}:{terminal.context.generation}:terminal"
        )
        == "completed"
    )


async def test_listed_idle_candidate_superseded_before_cas_runs_terminal_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    monkeypatch.setattr(session_context, "_default_repository", repo)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    context = await repo.touch(BuyerSessionInput("stale-cas", "t1", "member", "704"))
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("704", "stale-cas"), "종료 취향")
    _, idle_generation = await _complete_idle_transient(repo, coordinator, clock)
    listed = asyncio.Event()
    continue_list = asyncio.Event()
    original_list = repo.list_recoverable_profile_phases

    async def _paused_list(batch_size: int):
        candidates = await original_list(batch_size)
        listed.set()
        await continue_list.wait()
        return candidates

    monkeypatch.setattr(repo, "list_recoverable_profile_phases", _paused_list)
    stale = asyncio.create_task(finalizer.process_recoverable_profile_phases(1))
    await listed.wait()
    terminal = await repo.begin_terminal(704, "stale-cas")
    continue_list.set()

    assert await stale == []
    monkeypatch.setattr(repo, "list_recoverable_profile_phases", original_list)
    if terminal.claim is not None:
        transient = await coordinator.process_terminal_transient(terminal.claim)
        assert transient.status == "completed"
    else:
        assert terminal.finalization.transient_status == "completed"
    monkeypatch.setattr(finalizer, "get_llm", lambda: _LLM())
    await finalizer.process_recoverable_profile_phases(1)

    assert (
        await processed_events.get_status(
            f"chat-profile:{context.context_id}:{idle_generation}:idle"
        )
        is None
    )
    assert (
        await processed_events.get_status(
            f"chat-profile:{context.context_id}:{terminal.context.generation}:terminal"
        )
        == "completed"
    )


async def test_idle_candidate_superseded_at_profile_cas_is_skipped_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    monkeypatch.setattr(session_context, "_default_repository", repo)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    context = await repo.touch(BuyerSessionInput("cas-superseded", "t1", "member", "705"))
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("705", "cas-superseded"), "이전 취향")
    _, idle_generation = await _complete_idle_transient(repo, coordinator, clock)
    entered_cas = asyncio.Event()
    continue_cas = asyncio.Event()
    original_claim = repo.claim_profile_phase
    llm_calls = 0

    class _CountingLLM(_LLM):
        async def complete(self, **kwargs):
            nonlocal llm_calls
            llm_calls += 1
            return await super().complete(**kwargs)

    async def _paused_claim(finalization_id: str, lease_s: float):
        entered_cas.set()
        await continue_cas.wait()
        return await original_claim(finalization_id, lease_s)

    monkeypatch.setattr(finalizer, "get_llm", lambda: _CountingLLM())
    monkeypatch.setattr(repo, "claim_profile_phase", _paused_claim)
    recovery = asyncio.create_task(finalizer.process_recoverable_profile_phases(1))
    await entered_cas.wait()
    terminal = await repo.begin_terminal(705, "cas-superseded")
    continue_cas.set()

    assert await recovery == []
    assert llm_calls == 0
    assert (
        await processed_events.get_status(
            f"chat-profile:{context.context_id}:{idle_generation}:idle"
        )
        is None
    )
    assert terminal.finalization.supersedes_finalization_id is not None


async def test_profile_cas_lost_to_live_claim_skips_then_recovers_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    repo = SessionContextRepository(clock=clock)
    coordinator = SessionLifecycleCoordinator(repo, ActiveStreamRegistry())
    await repo.touch(BuyerSessionInput("cas-live", "t1", "member", "706"))
    store = await get_profile_store()
    await store.append_session_ctx(conversation_key("706", "cas-live"), "회복할 취향")
    await _complete_idle_transient(repo, coordinator, clock)
    [candidate] = await repo.list_recoverable_profile_phases(1)
    live_claim = await repo.claim_profile_phase(candidate.finalization_id, 30)
    assert live_claim is not None
    llm_calls = 0

    class _CountingLLM(_LLM):
        async def complete(self, **kwargs):
            nonlocal llm_calls
            llm_calls += 1
            return await super().complete(**kwargs)

    monkeypatch.setattr(finalizer, "get_llm", lambda: _CountingLLM())

    assert await finalizer._process_profile_candidate(candidate, repo, get_settings()) is None
    assert llm_calls == 0

    clock.advance(31)
    result = await finalizer._process_profile_candidate(candidate, repo, get_settings())
    assert result is not None and result.status is finalizer.ProfilePhaseStatus.COMPLETED
    assert llm_calls > 0
    assert (await repo.get_finalization(candidate.finalization_id)).profile_status == "completed"


async def test_finalizer_invalid_identity_degrades_to_retryable() -> None:
    result = await finalizer.finalize_profile_session("not-a-member-id", "session")

    assert result.status is finalizer.FinalizationStatus.RETRYABLE


async def _expired_claim(monkeypatch: pytest.MonkeyPatch, user_id: int, session_id: str):
    now = 0.0
    monkeypatch.setattr(session_activity, "_monotonic", lambda: now)
    await session_activity.touch_session(user_id, session_id)
    now = 600.0
    claims = await session_activity.claim_expired_sessions(
        idle_timeout_s=600,
        lease_s=900,
        batch_size=1,
    )
    assert len(claims) == 1
    return claims[0]


async def test_profile_only_recovery_does_not_reenter_legacy_activity_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.session_lifecycle import IdleSweepResult as LifecycleSweepResult

    async def _lifecycle_sweep(self, **kwargs):
        return LifecycleSweepResult(claimed=1, completed=1)

    async def _unexpected_legacy(**kwargs):
        raise AssertionError("profile recovery 직후 legacy activity sweep 재진입 금지")

    monkeypatch.setattr(
        SessionLifecycleCoordinator,
        "run_session_context_sweep",
        _lifecycle_sweep,
    )
    monkeypatch.setattr(session_activity, "claim_expired_sessions", _unexpected_legacy)

    result = await idle_timeout.run_idle_sweep()

    assert result.claimed == 1
    assert result.accepted == 1


async def test_internal_timeout_and_explicit_end_share_one_idempotent_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(finalizer, "get_llm", lambda: _LLM())
    claim = await _expired_claim(monkeypatch, 74, "race")
    store = await get_profile_store()
    key = conversation_key("74", "race")
    await store.append_session_ctx(key, "무선이어폰 좋아해")

    explicit, timeout = await asyncio.gather(
        finalizer.finalize_profile_session("74", "race"),
        finalizer.finalize_profile_session(
            "74",
            "race",
            activity_claim=claim,
            terminal=False,
        ),
    )

    assert {explicit.status, timeout.status} == {
        finalizer.FinalizationStatus.ACCEPTED,
        finalizer.FinalizationStatus.DUPLICATE,
    }
    assert await store.get_session_ctx(key) == []
    assert await processed_events.get_status("session-end:74:race") == "completed"
    row = await session_activity.get_session(74, "race")
    assert row is not None and row.status == "completed"


async def test_new_activity_during_terminal_finalizer_invalidates_terminal_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delta_started = asyncio.Event()
    allow_delta = asyncio.Event()

    class _BlockingLLM(_LLM):
        async def complete(self, **kwargs):
            if "델타 추출기" in kwargs["system"]:
                delta_started.set()
                await allow_delta.wait()
            return await super().complete(**kwargs)

    monkeypatch.setattr(finalizer, "get_llm", lambda: _BlockingLLM())
    now = 0.0
    monkeypatch.setattr(session_activity, "_monotonic", lambda: now)
    store = await get_profile_store()
    key = conversation_key("78", "terminal-race")
    await store.append_session_ctx(key, "종료가 처리할 이전 발화")
    await session_activity.touch_session(78, "terminal-race")
    task = asyncio.create_task(finalizer.finalize_profile_session(78, "terminal-race"))
    await delta_started.wait()

    now = 1.0
    await session_activity.touch_session(78, "terminal-race")
    await store.append_session_ctx(key, "종료 처리 중 들어온 새 발화")
    allow_delta.set()
    result = await task

    assert result.status is finalizer.FinalizationStatus.RETRYABLE
    assert await processed_events.get_status("session-end:78:terminal-race") is None
    row = await session_activity.get_session(78, "terminal-race")
    assert row is not None and row.status == "active"
    assert await store.get_session_ctx(key) == ["종료 처리 중 들어온 새 발화"]


async def test_idle_activity_completion_failure_is_reported_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = await _expired_claim(monkeypatch, 87, "completion-lost")

    async def _lose_completion(*args, **kwargs):
        return False

    monkeypatch.setattr(session_activity, "complete_session", _lose_completion)

    result = await finalizer.finalize_profile_session(
        claim.user_id,
        claim.session_id,
        activity_claim=claim,
        terminal=False,
    )

    assert result.status is finalizer.FinalizationStatus.RETRYABLE
    row = await session_activity.get_session(claim.user_id, claim.session_id)
    assert row is not None and row.status == "active"


async def test_cancelled_idle_finalizer_releases_both_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = await _expired_claim(monkeypatch, 90, "cancel")
    store = await get_profile_store()
    key = conversation_key("90", "cancel")
    await store.append_session_ctx(key, "취소되어도 보존할 발화")
    started = asyncio.Event()

    async def _block_delta(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(finalizer, "generate_session_delta", _block_delta)
    task = asyncio.create_task(
        finalizer.finalize_profile_session(90, "cancel", activity_claim=claim)
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert await store.get_session_ctx(key) == ["취소되어도 보존할 발화"]
    assert await processed_events.get_status("session-end:90:cancel") is None
    row = await session_activity.get_session(90, "cancel")
    assert row is not None and row.status == "active"
