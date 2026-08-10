from __future__ import annotations

import asyncio
import logging

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from psycopg_pool import PoolTimeout

from app.core import session_context
from app.core import session_lifecycle as lifecycle
from app.core.observability import message_fingerprint
from app.core.session_context import (
    BuyerSessionInput,
    SessionActive,
    SessionContextRepository,
)
from app.core.stream import ActiveStreamRegistry, get_registry, open_stream
from app.main import app
from app.schemas.events import SessionClaimEvent


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class _BlockingObserver:
    def __init__(self, buyer_session: BuyerSessionInput) -> None:
        self.request_id = "claim-race"
        self.buyer_session = buyer_session
        self.trace = None
        self.commit_entered = asyncio.Event()
        self.commit_proceed = asyncio.Event()

    async def commit_user_message(self) -> None:
        self.commit_entered.set()
        await self.commit_proceed.wait()

    def on_first_token(self, now: float) -> None:
        pass

    def record_frame(self, frame: str, now: float) -> None:
        pass

    async def finish(
        self, now: float, status, error_type=None, terminal_reason: str = "eof"
    ) -> None:  # noqa: ANN001
        pass


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> SessionContextRepository:
    repository = SessionContextRepository()
    monkeypatch.setattr(session_context, "_default_repository", repository)
    return repository


@pytest.fixture
async def client() -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client


async def _post_claim(
    client: httpx.AsyncClient,
    *,
    session_id: str = "session-1",
    guest_id: str = "guest-1",
    user_id: int = 7,
    token: str | None = None,
) -> httpx.Response:
    headers = {"X-Internal-Token": token} if token is not None else {}
    return await client.post(
        "/events/session-claim",
        json={"sessionId": session_id, "guestId": guest_id, "userId": user_id},
        headers=headers,
    )


async def test_claim_accepts_guest_context_and_preserves_threads(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
) -> None:
    original = await repo.touch(BuyerSessionInput("session-1", "thread-1", "guest", "guest-1"))
    await repo.touch(BuyerSessionInput("session-1", "thread-2", "guest", "guest-1"))

    response = await _post_claim(client)

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    claimed = await repo.get_context("session-1")
    assert claimed is not None
    assert claimed.context_id == original.context_id
    assert claimed.owner_type == "member"
    assert claimed.owner_id == "7"
    assert claimed.generation == original.generation + 1
    assert await repo.get_threads(claimed.context_id) == ["thread-1", "thread-2"]
    assert repo._owner_claims == {"session-1": ("guest-1", "7")}


@pytest.mark.parametrize("failure", [TimeoutError("pool"), psycopg.OperationalError("down")])
async def test_claim_pg_operational_failure_maps_to_state_unavailable(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """claim 저장소의 연결/timeout 장애는 공개 wire에서 503으로 고정한다."""

    async def fail(_event):
        raise failure

    monkeypatch.setattr(lifecycle, "claim_owner", fail)
    response = await _post_claim(client, session_id="claim-fail")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STATE_UNAVAILABLE"


async def test_claim_programming_error_is_not_masked(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(_event):
        raise psycopg.ProgrammingError("bad sql")

    monkeypatch.setattr(lifecycle, "claim_owner", fail)
    with pytest.raises(psycopg.ProgrammingError):
        await _post_claim(client, session_id="claim-code")


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("pool"), PoolTimeout("pool"), psycopg.OperationalError("down")],
)
async def test_session_end_pg_operational_failure_maps_to_state_unavailable(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """session-end의 실제 저장소 연결/timeout 장애만 공개 wire 503으로 변환한다."""

    async def fail(*_args):
        raise failure

    monkeypatch.setattr(lifecycle.SessionLifecycleCoordinator, "begin_terminal", fail)
    response = await client.post(
        "/events/session-end",
        json={"userId": 7, "sessionId": "terminal-infra-fail"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STATE_UNAVAILABLE"


@pytest.mark.parametrize(
    "failure",
    [
        psycopg.ProgrammingError("bad sql"),
        psycopg.IntegrityError("broken invariant"),
        ValueError("domain bug"),
    ],
)
async def test_session_end_non_infra_failure_is_not_masked(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """programming/integrity/domain 오류는 STATE_UNAVAILABLE로 오분류하지 않는다."""

    async def fail(*_args):
        raise failure

    monkeypatch.setattr(lifecycle.SessionLifecycleCoordinator, "begin_terminal", fail)
    with pytest.raises(type(failure)):
        await client.post(
            "/events/session-end",
            json={"userId": 7, "sessionId": "terminal-code-fail"},
        )


@pytest.mark.parametrize(
    ("path", "payload", "unexpected"),
    [
        (
            "/events/session-claim",
            {"sessionId": "s", "guestId": "g", "userId": 7},
            {"unexpected": True},
        ),
        (
            "/events/session-end",
            {"sessionId": "s", "userId": 7},
            {"unexpected": True},
        ),
    ],
)
async def test_event_payloads_reject_unknown_fields(
    client: httpx.AsyncClient,
    path: str,
    payload: dict,
    unexpected: dict,
) -> None:
    """event 전용 inbound 모델은 알려지지 않은 필드를 400 BAD_REQUEST로 거부한다."""
    response = await client.post(path, json={**payload, **unexpected})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/events/session-claim",
            {"session_id": "s", "guest_id": "g", "user_id": 7},
        ),
        (
            "/events/session-end",
            {"session_id": "s", "user_id": 7},
        ),
    ],
)
async def test_event_payloads_reject_snake_case_fields(
    client: httpx.AsyncClient,
    path: str,
    payload: dict,
) -> None:
    """event wire는 camelCase alias만 허용하고 Python 필드명 입력은 거부한다."""
    response = await client.post(path, json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/events/session-claim",
            {
                "sessionId": "canonical",
                "session_id": "shadow",
                "guestId": "g",
                "userId": 7,
            },
        ),
        (
            "/events/session-end",
            {
                "sessionId": "canonical",
                "session_id": "shadow",
                "userId": 7,
            },
        ),
    ],
)
async def test_event_payloads_reject_camel_snake_collisions(
    client: httpx.AsyncClient,
    path: str,
    payload: dict,
) -> None:
    """camel+snake collision은 shadow 필드를 무시하지 않고 400으로 닫는다."""
    response = await client.post(path, json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/events/session-claim",
            {"sessionId": 1, "guestId": "g", "userId": 7},
        ),
        (
            "/events/session-claim",
            {"sessionId": "s", "guestId": 1, "userId": 7},
        ),
        (
            "/events/session-end",
            {"sessionId": 1, "userId": 7},
        ),
        (
            "/events/session-end",
            {"sessionId": "s", "userId": 7, "reason": 1},
        ),
    ],
)
async def test_event_string_fields_do_not_coerce_numbers(
    client: httpx.AsyncClient,
    path: str,
    payload: dict,
) -> None:
    """event key/reason 문자열 필드는 숫자를 문자열로 coercion하지 않는다."""
    response = await client.post(path, json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


async def test_session_end_keeps_terminal_durable_when_profile_is_retryable(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agents.profile.finalizer as profile_finalizer
    from app.agents.profile.store import get_profile_store
    from app.core.conversation import conversation_key

    await repo.touch(BuyerSessionInput("terminal-profile", "thread-1", "member", "7"))
    store = await get_profile_store()
    await store.append_session_ctx(
        conversation_key("7", "terminal-profile"),
        "재시도할 프로필 신호",
    )
    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: None)

    response = await client.post(
        "/events/session-end",
        json={"userId": 7, "sessionId": "terminal-profile"},
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    context = await repo.get_context("terminal-profile")
    assert context is not None and context.state == "terminal"
    [journal] = repo._finalizations.values()
    assert journal.transient_status == "completed"
    assert journal.profile_status == "retryable"


async def test_stream_scope_idle_wait_is_event_driven() -> None:
    registry = ActiveStreamRegistry()
    assert await registry.acquire("7:thread", owner_id="7", session_id="session-1")
    waiter = asyncio.create_task(registry.wait_for_scope_idle("7", "session-1"))
    await asyncio.sleep(0)
    assert not waiter.done()

    await registry.release("7:thread")
    await asyncio.wait_for(waiter, timeout=0.1)
    assert registry._scope_idle == {}


async def test_stream_scope_idle_events_are_lazy_and_follow_reacquire_race() -> None:
    registry = ActiveStreamRegistry()
    for index in range(100):
        scope = f"session-{index}"
        assert await registry.acquire(f"stream-{index}", owner_id="7", session_id=scope)
        await registry.release(f"stream-{index}")
    assert registry._scope_idle == {}

    assert await registry.acquire("first", owner_id="7", session_id="same")
    waiter = asyncio.create_task(registry.wait_for_scope_idle("7", "same"))
    await asyncio.sleep(0)
    assert len(registry._scope_idle) == 1

    await registry.release("first")
    assert await registry.acquire("second", owner_id="7", session_id="same")
    assert not registry._scope_idle[("7", "same")].event.is_set()
    await asyncio.sleep(0)
    assert not waiter.done()
    await registry.release("second")
    await asyncio.wait_for(waiter, timeout=0.1)
    assert registry._scope_idle == {}


async def test_stream_scope_idle_wakes_all_same_scope_waiters_and_cleans_state() -> None:
    registry = ActiveStreamRegistry()
    assert await registry.acquire("stream", owner_id="7", session_id="shared")
    waiters = [asyncio.create_task(registry.wait_for_scope_idle("7", "shared")) for _ in range(5)]
    await asyncio.sleep(0)
    assert registry._scope_idle[("7", "shared")].count == 5

    await registry.release("stream")
    await asyncio.wait_for(asyncio.gather(*waiters), timeout=0.1)

    assert registry._scope_idle == {}


async def test_stream_scope_idle_cancelled_last_waiter_does_not_leak_state() -> None:
    registry = ActiveStreamRegistry()
    assert await registry.acquire("first", owner_id="7", session_id="cancelled")
    cancelled = asyncio.create_task(registry.wait_for_scope_idle("7", "cancelled"))
    await asyncio.sleep(0)

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert registry._scope_idle == {}

    replacement = asyncio.create_task(registry.wait_for_scope_idle("7", "cancelled"))
    await asyncio.sleep(0)
    assert len(registry._scope_idle) == 1
    await registry.release("first")
    await asyncio.wait_for(replacement, timeout=0.1)
    assert registry._scope_idle == {}


def test_stream_scope_idle_registry_can_be_reused_across_event_loops() -> None:
    registry = ActiveStreamRegistry()

    async def run_once(index: int) -> None:
        stream_key = f"stream-{index}"
        assert await registry.acquire(stream_key, owner_id="7", session_id="cross-loop")
        waiter = asyncio.create_task(registry.wait_for_scope_idle("7", "cross-loop"))
        await asyncio.sleep(0)
        await registry.release(stream_key)
        await waiter
        assert registry._scope_idle == {}

    asyncio.run(run_once(1))
    asyncio.run(run_once(2))


async def test_terminal_gate_precedes_stream_wait_and_watermark_capture(
    repo: SessionContextRepository,
) -> None:
    registry = ActiveStreamRegistry()
    await repo.touch(BuyerSessionInput("stream-terminal", "thread-1", "member", "7"))
    assert await registry.acquire(
        "7:thread-1",
        owner_id="7",
        session_id="stream-terminal",
    )
    snapshot_called = asyncio.Event()

    class _ProfileStore:
        async def get_session_ctx_snapshot(self, key: str) -> tuple[list[str], int]:
            snapshot_called.set()
            return [], 0

    coordinator = lifecycle.SessionLifecycleCoordinator(
        repo,
        registry,
        profile_store_factory=_ProfileStore,
    )
    terminal_task = asyncio.create_task(coordinator.begin_terminal(7, "stream-terminal"))
    await asyncio.sleep(0)

    context = await repo.get_context("stream-terminal")
    assert context is not None and context.state == "terminal"
    assert not snapshot_called.is_set()
    assert not terminal_task.done()

    await registry.release("7:thread-1")
    outcome = await asyncio.wait_for(terminal_task, timeout=0.2)
    assert snapshot_called.is_set()
    assert outcome.duplicate is False
    duplicate = await coordinator.begin_terminal(7, "stream-terminal")
    assert duplicate.duplicate is True


async def test_session_end_supersedes_idle_finalizing_barrier(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agents.profile.finalizer as profile_finalizer

    context = await repo.touch(BuyerSessionInput("i20-barrier", "thread-1", "member", "7"))
    repo._contexts["i20-barrier"].last_activity_at = 0
    [idle] = await repo.claim_expired_contexts(1, 30, 1)
    async with repo.lock_session("i20-barrier") as uow:
        await uow.prepare_idle_finalizing_with_watermark(idle, 0)

    async def clear_context(context_id: str, thread_ids: list[str]):
        from app.agents.buyer.session_state import CleanupCounts

        return CleanupCounts(filters=1)

    monkeypatch.setattr("app.core.session_lifecycle.session_state.clear_context", clear_context)
    monkeypatch.setattr(profile_finalizer, "get_llm", lambda: None)
    response = await client.post(
        "/events/session-end",
        json={"userId": 7, "sessionId": "i20-barrier"},
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    terminal_context = await repo.get_context("i20-barrier")
    assert terminal_context is not None and terminal_context.state == "terminal"
    old = await repo.get_finalization(idle.finalization_id)
    assert old.status == "superseded"
    assert old.claim_token is None
    terminal_rows = [
        row
        for row in repo._finalizations.values()
        if row.context_id == context.context_id and row.reason == "terminal"
    ]
    assert len(terminal_rows) == 1
    assert terminal_rows[0].transient_status == "completed"


async def test_exact_duplicate_never_mutates_context_generation_or_history(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
) -> None:
    await repo.touch(BuyerSessionInput("session-1", "thread-1", "guest", "guest-1"))
    first = await _post_claim(client)
    before = await repo.get_context("session-1")
    history_before = dict(repo._owner_claims)
    assert await get_registry().acquire(
        "guest-1:thread-1",
        owner_id="guest-1",
        session_id="session-1",
    )

    duplicate = await _post_claim(client)

    assert first.status_code == duplicate.status_code == 202
    assert duplicate.json() == {"status": "duplicate"}
    assert await repo.get_context("session-1") == before
    assert repo._owner_claims == history_before
    assert get_registry().is_active("guest-1:thread-1")
    assert not get_registry().is_fenced("guest-1", "session-1")


async def test_claim_rejects_active_stream_in_any_registered_thread(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
) -> None:
    original = await repo.touch(BuyerSessionInput("session-1", "thread-1", "guest", "guest-1"))
    await repo.touch(BuyerSessionInput("session-1", "thread-2", "guest", "guest-1"))
    assert await get_registry().acquire(
        "guest-1:thread-2",
        owner_id="guest-1",
        session_id="session-1",
    )

    response = await _post_claim(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_ACTIVE"
    assert await repo.get_context("session-1") == original
    assert repo._owner_claims == {}
    assert get_registry().is_active("guest-1:thread-2")
    assert not get_registry().is_fenced("guest-1", "session-1")


async def test_new_guest_thread_slot_before_db_touch_blocks_claim(
    repo: SessionContextRepository,
) -> None:
    registry = get_registry()
    observer = _BlockingObserver(BuyerSessionInput("session-1", "new-thread", "guest", "guest-1"))
    coordinator = lifecycle.SessionLifecycleCoordinator(repo, registry)

    async def inner(_turn_started_at=None):
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    stream_task = asyncio.create_task(
        open_stream(
            _Request(),
            "guest-1:new-thread",
            inner,
            observer=observer,
        )
    )
    await observer.commit_entered.wait()

    with pytest.raises(SessionActive):
        await coordinator.claim_owner(
            SessionClaimEvent(sessionId="session-1", guestId="guest-1", userId=7)
        )

    assert await repo.get_context("session-1") is None
    assert registry.is_active("guest-1:new-thread")
    assert not registry.is_fenced("guest-1", "session-1")
    observer.commit_proceed.set()
    response = await stream_task
    _ = [chunk async for chunk in response.body_iterator]
    assert not registry.is_active("guest-1:new-thread")


async def test_claim_coordinator_replaces_legacy_guess_and_quarantines_it(
    repo: SessionContextRepository,
) -> None:
    repo._contexts["session-1"] = session_context._MemoryContext(
        "legacy-context",
        "session-1",
        "member",
        "6",
        0,
        "active",
        repo._clock(),
        "legacy_backfill",
    )
    repo._migration_conflicts[("session-1", "6")] = ("resolved", "legacy-context")

    outcome = await lifecycle.SessionLifecycleCoordinator(repo, get_registry()).claim_owner(
        SessionClaimEvent(sessionId="session-1", guestId="signed-guest", userId=7)
    )

    assert outcome.context.owner_id == "7"
    assert outcome.context.context_id != "legacy-context"
    assert repo._migration_conflicts[("session-1", "6")][0] == "quarantined"


async def test_claim_fence_blocks_new_guest_slot_until_transition_finishes(
    repo: SessionContextRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = get_registry()
    coordinator = lifecycle.SessionLifecycleCoordinator(repo, registry)
    entered = asyncio.Event()
    proceed = asyncio.Event()
    original = lifecycle._transition_claim

    async def blocked_transition(repository, conn, event):  # noqa: ANN001
        entered.set()
        await proceed.wait()
        return await original(repository, conn, event)

    monkeypatch.setattr(lifecycle, "_transition_claim", blocked_transition)
    task = asyncio.create_task(
        coordinator.claim_owner(
            SessionClaimEvent(sessionId="session-1", guestId="guest-1", userId=7)
        )
    )
    await entered.wait()

    assert registry.is_fenced("guest-1", "session-1")
    observer = _BlockingObserver(BuyerSessionInput("session-1", "new-thread", "guest", "guest-1"))

    async def inner(_turn_started_at=None):
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    with pytest.raises(HTTPException) as blocked:
        await open_stream(
            _Request(),
            "guest-1:new-thread",
            inner,
            observer=observer,
        )
    assert blocked.value.status_code == 409
    assert not observer.commit_entered.is_set()

    proceed.set()
    outcome = await task
    assert outcome.claimed is True
    assert not registry.is_fenced("guest-1", "session-1")
    assert await registry.acquire(
        "guest-1:new-thread",
        owner_id="guest-1",
        session_id="session-1",
    )


async def test_claim_releases_fence_after_transition_error_and_cancellation(
    repo: SessionContextRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = get_registry()
    coordinator = lifecycle.SessionLifecycleCoordinator(repo, registry)
    event = SessionClaimEvent(sessionId="session-1", guestId="guest-1", userId=7)

    async def fail_transition(repository, conn, claim_event):  # noqa: ANN001
        raise RuntimeError("db transition failed")

    monkeypatch.setattr(lifecycle, "_transition_claim", fail_transition)
    with pytest.raises(RuntimeError, match="db transition failed"):
        await coordinator.claim_owner(event)
    assert not registry.is_fenced("guest-1", "session-1")

    entered = asyncio.Event()

    async def cancelled_transition(repository, conn, claim_event):  # noqa: ANN001
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(lifecycle, "_transition_claim", cancelled_transition)
    task = asyncio.create_task(coordinator.claim_owner(event))
    await entered.wait()
    assert registry.is_fenced("guest-1", "session-1")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not registry.is_fenced("guest-1", "session-1")


@pytest.mark.parametrize(
    ("guest_id", "user_id"),
    [("wrong-source", 7), ("guest-1", 8)],
)
async def test_claim_rejects_wrong_source_or_different_target(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
    guest_id: str,
    user_id: int,
) -> None:
    await repo.touch(BuyerSessionInput("session-1", "thread-1", "guest", "guest-1"))
    if user_id == 8:
        assert (await _post_claim(client)).status_code == 202

    response = await _post_claim(client, guest_id=guest_id, user_id=user_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_CLAIM_CONFLICT"


async def test_no_row_claim_creates_member_context_and_guest_source_history(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
) -> None:
    response = await _post_claim(client)

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    context = await repo.get_context("session-1")
    assert context is not None
    assert (context.owner_type, context.owner_id, context.generation, context.state) == (
        "member",
        "7",
        0,
        "active",
    )
    assert repo._owner_claims == {"session-1": ("guest-1", "7")}


async def test_claim_rejects_idle_finalizing_before_active_stream_check(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
) -> None:
    context = await repo.touch(BuyerSessionInput("session-1", "thread-1", "guest", "guest-1"))
    repo._contexts["session-1"].state = "idle_finalizing"
    assert await get_registry().acquire(
        "guest-1:thread-1",
        owner_id="guest-1",
        session_id="session-1",
    )

    response = await _post_claim(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_FINALIZING"
    assert not get_registry().is_fenced("guest-1", "session-1")
    assert await repo.get_context("session-1") == context.__class__(
        context.context_id,
        context.session_id,
        context.owner_type,
        context.owner_id,
        context.generation,
        "idle_finalizing",
    )


async def test_claim_rejects_terminal_context_as_conflict(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
) -> None:
    await repo.touch(BuyerSessionInput("session-1", "thread-1", "guest", "guest-1"))
    repo._contexts["session-1"].state = "terminal"

    response = await _post_claim(client)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_CLAIM_CONFLICT"


async def test_claim_requires_service_token_in_jwks_mode(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "jwks")
    monkeypatch.setattr(settings, "internal_api_token", "service-secret")

    missing = await _post_claim(client)
    invalid = await _post_claim(client, token="wrong")
    accepted = await _post_claim(client, token="service-secret")

    assert missing.status_code == invalid.status_code == 401
    assert missing.json()["error"]["code"] == "INTERNAL_TOKEN_INVALID"
    assert accepted.status_code == 202


@pytest.mark.parametrize(
    "body",
    [
        {"guestId": "guest-1", "userId": 7},
        {"sessionId": "session-1", "userId": 7},
        {"sessionId": "session-1", "guestId": "guest-1", "userId": 0},
    ],
)
async def test_claim_rejects_invalid_body(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
    body: dict[str, object],
) -> None:
    response = await client.post("/events/session-claim", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


@pytest.mark.parametrize(
    "case",
    [
        "bool-user",
        "string-user",
        "float-user",
        "zero-user",
        "overflow-user",
        "numeric-session",
        "numeric-guest",
        "empty-session",
        "empty-guest",
        "long-session",
        "long-guest",
    ],
)
async def test_claim_rejects_values_outside_strict_public_contract(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
    case: str,
) -> None:
    from app.core.config import get_settings

    body: dict[str, object] = {
        "sessionId": "session-1",
        "guestId": "guest-1",
        "userId": 7,
    }
    invalid = {
        "bool-user": ("userId", True),
        "string-user": ("userId", "7"),
        "float-user": ("userId", 7.0),
        "zero-user": ("userId", 0),
        "overflow-user": ("userId", 2**63),
        "numeric-session": ("sessionId", 1),
        "numeric-guest": ("guestId", 1),
        "empty-session": ("sessionId", ""),
        "empty-guest": ("guestId", ""),
        "long-session": ("sessionId", "s" * (get_settings().chat_key_max_chars + 1)),
        "long-guest": ("guestId", "g" * (get_settings().chat_key_max_chars + 1)),
    }
    field, value = invalid[case]
    body[field] = value

    response = await client.post("/events/session-claim", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"


async def test_claim_accepts_bigint_max_with_configured_contract_boundaries(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "jwks")
    monkeypatch.setattr(settings, "internal_api_token", "service-secret")
    session_id = "s" * settings.chat_key_max_chars
    guest_id = "g" * settings.chat_key_max_chars

    response = await client.post(
        "/events/session-claim",
        json={
            "sessionId": session_id,
            "guestId": guest_id,
            "userId": 2**63 - 1,
        },
        headers={"X-Internal-Token": settings.internal_api_token},
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    claimed = await repo.get_context(session_id)
    assert claimed is not None
    assert claimed.owner_id == str(2**63 - 1)


async def test_claim_log_fingerprints_external_identifiers(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "pii_hash_pepper", "claim-log-pepper")
    session_id = "raw-session-secret"
    guest_id = "raw-guest-secret"
    session_fp = message_fingerprint(session_id)[1]
    owner_fp = message_fingerprint(guest_id)[1]

    with caplog.at_level(logging.INFO, logger="app.core.session_lifecycle"):
        response = await _post_claim(client, session_id=session_id, guest_id=guest_id)

    assert response.status_code == 202
    log_text = caplog.text
    assert session_fp in log_text and owner_fp in log_text
    assert session_id not in log_text and guest_id not in log_text
    assert "sessionFp=" in log_text and "ownerFp=" in log_text
    assert "generation=0" in log_text and "outcome=accepted" in log_text
