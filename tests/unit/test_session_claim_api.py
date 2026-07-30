from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from fastapi import HTTPException

from app.core import session_context
from app.core import session_lifecycle as lifecycle
from app.core.observability import message_fingerprint
from app.core.session_context import (
    BuyerSessionInput,
    SessionActive,
    SessionContextRepository,
)
from app.core.stream import get_registry, open_stream
from app.main import app
from app.schemas.events import SessionClaimEvent


class _Request:
    async def is_disconnected(self) -> bool:
        return False


class _BlockingObserver:
    def __init__(self, buyer_session: BuyerSessionInput) -> None:
        self.request_id = "claim-race"
        self.buyer_session = buyer_session
        self.commit_entered = asyncio.Event()
        self.commit_proceed = asyncio.Event()

    async def commit_user_message(self) -> None:
        self.commit_entered.set()
        await self.commit_proceed.wait()

    def on_first_token(self, now: float) -> None:
        pass

    def record_frame(self, frame: str) -> None:
        pass

    async def finish(self, now: float, status, error_type=None) -> None:  # noqa: ANN001
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


async def test_exact_duplicate_never_mutates_context_generation_or_history(
    client: httpx.AsyncClient,
    repo: SessionContextRepository,
) -> None:
    await repo.touch(BuyerSessionInput("session-1", "thread-1", "guest", "guest-1"))
    first = await _post_claim(client)
    before = await repo.get_context("session-1")
    history_before = dict(repo._owner_claims)
    assert get_registry().acquire(
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
    assert get_registry().acquire(
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

    async def inner():
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
            SessionClaimEvent(session_id="session-1", guest_id="guest-1", user_id=7)
        )

    assert await repo.get_context("session-1") is None
    assert registry.is_active("guest-1:new-thread")
    assert not registry.is_fenced("guest-1", "session-1")
    observer.commit_proceed.set()
    response = await stream_task
    _ = [chunk async for chunk in response.body_iterator]
    assert not registry.is_active("guest-1:new-thread")


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
            SessionClaimEvent(session_id="session-1", guest_id="guest-1", user_id=7)
        )
    )
    await entered.wait()

    assert registry.is_fenced("guest-1", "session-1")
    observer = _BlockingObserver(BuyerSessionInput("session-1", "new-thread", "guest", "guest-1"))

    async def inner():
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
    assert registry.acquire(
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
    event = SessionClaimEvent(session_id="session-1", guest_id="guest-1", user_id=7)

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
    assert get_registry().acquire(
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
    guest_fp = message_fingerprint(guest_id)[1]

    with caplog.at_level(logging.INFO, logger="app.core.session_lifecycle"):
        response = await _post_claim(client, session_id=session_id, guest_id=guest_id)

    assert response.status_code == 202
    log_text = caplog.text
    assert session_fp in log_text and guest_fp in log_text
    assert session_id not in log_text and guest_id not in log_text
    assert "generation=0" in log_text and "outcome=accepted" in log_text
