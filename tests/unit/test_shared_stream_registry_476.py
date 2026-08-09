"""이슈 #476 완료 조건 3 — 워커 간 공유 스트림 레지스트리.

설계: `docs/specs/DESIGN-SHARED-STREAM-REGISTRY-476.md`.

교차 워커는 **레지스트리 인스턴스 2개가 같은 저장소를 보게** 해서 시뮬레이션한다. 저장소는
`InMemorySharedStreamStore`(주입 clock 으로 lease 만료를 시간 없이 재현) 다. 실제 SQL 배선은
`tests/integration/test_pg_shared_stream_registry.py` 가 pg-profile 로 따로 검증한다.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.core import stream as stream_mod
from app.core import stream_registry as registry_mod
from app.core.config import Settings
from app.core.session_context import SessionStateUnavailable
from app.core.stream import (
    ActiveStreamRegistry,
    InMemorySharedStreamStore,
    SharedStreamRegistry,
    StreamScopeFence,
)


class _Clock:
    """수동 단조 시계 — lease 만료를 sleep 없이 재현한다."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _BrokenStore(InMemorySharedStreamStore):
    """모든 연산이 실패하는 저장소 — fail-closed 경로용."""

    async def try_acquire_stream(self, **kwargs) -> bool:  # noqa: ANN003
        raise RuntimeError("store down")

    async def try_acquire_fence(self, **kwargs) -> bool:  # noqa: ANN003
        raise RuntimeError("store down")

    async def scope_has_active_stream(self, **kwargs) -> bool:  # noqa: ANN003
        raise RuntimeError("store down")

    async def release_stream(self, **kwargs) -> None:  # noqa: ANN003
        raise RuntimeError("store down")

    async def release_fence(self, **kwargs) -> None:  # noqa: ANN003
        raise RuntimeError("store down")


@pytest.fixture
def shared_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = Settings(
        _env_file=None,
        stream_registry_backend="shared",
        stream_registry_lease_ttl_s=60.0,
        stream_registry_lease_renew_interval_s=5.0,
        stream_registry_scope_poll_s=0.01,
        stream_registry_scope_idle_wait_max_s=120.0,
    )
    monkeypatch.setattr(registry_mod, "get_settings", lambda: settings)
    return settings


def _two_workers(
    store: InMemorySharedStreamStore, clock: _Clock
) -> tuple[SharedStreamRegistry, SharedStreamRegistry]:
    return (
        SharedStreamRegistry(store, instance_id="worker-a", clock=clock),
        SharedStreamRegistry(store, instance_id="worker-b", clock=clock),
    )


# ── 불변식 1: 409 가드가 워커 간에도 유효하다 ──────────────────────────────────


async def test_only_one_worker_acquires_the_same_stream_key(shared_settings: Settings) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, worker_b = _two_workers(store, clock)

    results = await asyncio.gather(
        worker_a.acquire("member-7:thread-1"),
        worker_b.acquire("member-7:thread-1"),
    )

    assert sorted(results) == [False, True], "같은 stream_key 는 정확히 하나만 성공해야 한다"
    winner = worker_a if results[0] else worker_b
    loser = worker_b if results[0] else worker_a
    assert winner.is_active("member-7:thread-1")
    assert not loser.is_active("member-7:thread-1")

    # 승자가 놓으면 패자가 잡을 수 있다.
    await winner.release("member-7:thread-1")
    assert await loser.acquire("member-7:thread-1")


async def test_release_only_removes_the_slot_this_worker_owns(shared_settings: Settings) -> None:
    """토큰이 다르면 남의 행을 지우지 않는다 (해제 레이스가 남의 슬롯을 열지 않게)."""
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, worker_b = _two_workers(store, clock)

    assert await worker_a.acquire("shared-key")
    assert not await worker_b.acquire("shared-key")
    # B 는 슬롯을 못 잡았으므로 토큰이 없다 — B 의 release 는 A 의 행을 건드리지 않는다.
    await worker_b.release("shared-key")
    assert "shared-key" in store.streams
    assert not await worker_b.acquire("shared-key")


# ── 불변식 2: fence 가 워커 간에도 유효하다 ────────────────────────────────────


async def test_fence_on_one_worker_blocks_acquire_on_another(shared_settings: Settings) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, worker_b = _two_workers(store, clock)

    fence = await worker_a.acquire_fence("7", "session-1")
    assert fence is not None

    assert not await worker_b.acquire("member-7:thread-1", owner_id="7", session_id="session-1"), (
        "다른 워커가 fence 를 들고 있으면 그 스코프의 stream 은 시작할 수 없다"
    )
    # 스코프가 다른 키는 막지 않는다.
    assert await worker_b.acquire("member-7:other", owner_id="7", session_id="session-2")

    await worker_a.release_fence(fence)
    assert await worker_b.acquire("member-7:thread-1", owner_id="7", session_id="session-1")


async def test_active_stream_on_one_worker_blocks_fence_on_another(
    shared_settings: Settings,
) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, worker_b = _two_workers(store, clock)

    assert await worker_a.acquire("member-7:thread-1", owner_id="7", session_id="session-1")

    assert await worker_b.acquire_fence("7", "session-1") is None, (
        "다른 워커가 그 스코프의 활성 스트림을 들고 있으면 fence 를 잡을 수 없다(→ SessionActive)"
    )

    await worker_a.release("member-7:thread-1")
    assert await worker_b.acquire_fence("7", "session-1") is not None


async def test_two_workers_never_hold_the_same_scope_fence(shared_settings: Settings) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, worker_b = _two_workers(store, clock)

    fences = await asyncio.gather(
        worker_a.acquire_fence("7", "session-1"),
        worker_b.acquire_fence("7", "session-1"),
    )

    assert sum(fence is not None for fence in fences) == 1


async def test_shared_fence_still_requires_the_issued_token_identity(
    shared_settings: Settings,
) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, _ = _two_workers(store, clock)

    fence = await worker_a.acquire_fence("7", "session-1")
    assert fence is not None
    forged = StreamScopeFence(owner_id="7", session_id="session-1")
    with pytest.raises(ValueError, match="not active"):
        await worker_a.release_fence(forged)
    assert worker_a.is_fenced("7", "session-1")
    assert ("7", "session-1") in store.fences

    await worker_a.release_fence(fence)
    assert not worker_a.is_fenced("7", "session-1")
    assert ("7", "session-1") not in store.fences


# ── 불변식 3: scope-idle 이 워커 간에도 유효하다 ───────────────────────────────


async def test_scope_idle_waits_for_a_stream_owned_by_another_worker(
    shared_settings: Settings,
) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, worker_b = _two_workers(store, clock)

    assert await worker_a.acquire("member-7:thread-1", owner_id="7", session_id="session-1")

    waiter = asyncio.create_task(worker_b.wait_for_scope_idle("7", "session-1"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not waiter.done(), "다른 워커의 스트림이 살아 있는 동안에는 깨어나면 안 된다"

    await worker_a.release("member-7:thread-1")
    await asyncio.wait_for(waiter, timeout=2.0)


async def test_scope_idle_returns_immediately_when_the_scope_is_free(
    shared_settings: Settings,
) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    _, worker_b = _two_workers(store, clock)

    await asyncio.wait_for(worker_b.wait_for_scope_idle("7", "session-1"), timeout=1.0)


async def test_scope_idle_gives_up_at_the_cap_instead_of_waiting_forever(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        _env_file=None,
        stream_registry_backend="shared",
        stream_registry_lease_ttl_s=60.0,
        stream_registry_lease_renew_interval_s=5.0,
        stream_registry_scope_poll_s=0.001,
        stream_registry_scope_idle_wait_max_s=120.0,
    )
    monkeypatch.setattr(registry_mod, "get_settings", lambda: settings)
    clock = _Clock()

    class _AlwaysBusyStore(InMemorySharedStreamStore):
        """lease 를 계속 연장하는 원격 워커 — 저장소는 영원히 busy 라고 답한다."""

        async def scope_has_active_stream(self, **kwargs) -> bool:  # noqa: ANN003
            clock.advance(settings.stream_registry_scope_poll_s * 100)
            return True

    worker_b = SharedStreamRegistry(_AlwaysBusyStore(clock=clock), instance_id="b", clock=clock)

    # 원격 스트림이 끝나지 않아도 상한에서 깨어난다 (무한 대기 금지).
    with caplog.at_level(logging.WARNING, logger="app.core.stream_registry"):
        await asyncio.wait_for(worker_b.wait_for_scope_idle("7", "session-1"), timeout=5.0)
    assert "STREAM_SCOPE_IDLE_WAIT_CAP" in caplog.text


# ── 불변식 4: lease 만료 (#48 슬롯 영구 누수 재발 방지) ────────────────────────


async def test_expired_lease_from_a_dead_worker_frees_the_slot(shared_settings: Settings) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, worker_b = _two_workers(store, clock)

    assert await worker_a.acquire("member-7:thread-1", owner_id="7", session_id="session-1")
    # worker_a 가 죽었다 — release 도 lease 연장도 없다.
    assert not await worker_b.acquire("member-7:thread-1", owner_id="7", session_id="session-1")

    clock.advance(shared_settings.stream_registry_lease_ttl_s + 1)
    assert await worker_b.acquire("member-7:thread-1", owner_id="7", session_id="session-1"), (
        "죽은 워커가 남긴 행은 TTL 후 무시되어야 한다(#48 슬롯 영구 누수 재발 방지)"
    )


async def test_expired_fence_from_a_dead_worker_frees_the_scope(
    shared_settings: Settings,
) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, worker_b = _two_workers(store, clock)

    assert await worker_a.acquire_fence("7", "session-1") is not None
    assert not await worker_b.acquire("s", owner_id="7", session_id="session-1")

    clock.advance(shared_settings.stream_registry_lease_ttl_s + 1)
    assert await worker_b.acquire("s", owner_id="7", session_id="session-1")


async def test_expired_scope_row_does_not_keep_scope_idle_waiting(
    shared_settings: Settings,
) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, worker_b = _two_workers(store, clock)
    assert await worker_a.acquire("member-7:thread-1", owner_id="7", session_id="session-1")
    # 로컬 미러가 아니라 저장소만 보게 한다 — worker_a 는 "죽은" 원격 워커다.
    worker_a._active.clear()

    clock.advance(shared_settings.stream_registry_lease_ttl_s + 1)
    await asyncio.wait_for(worker_b.wait_for_scope_idle("7", "session-1"), timeout=2.0)


async def test_renewed_lease_keeps_the_slot_across_the_ttl(shared_settings: Settings) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, worker_b = _two_workers(store, clock)
    assert await worker_a.acquire("member-7:thread-1")

    assert not worker_a.lease_renewal_due("member-7:thread-1")
    clock.advance(shared_settings.stream_registry_lease_renew_interval_s)
    assert worker_a.lease_renewal_due("member-7:thread-1")
    await worker_a.renew_lease("member-7:thread-1")
    assert not worker_a.lease_renewal_due("member-7:thread-1")

    # 연장했으므로 원래 TTL 을 넘겨도 슬롯은 살아 있다.
    clock.advance(shared_settings.stream_registry_lease_ttl_s - 1)
    assert not await worker_b.acquire("member-7:thread-1")


# ── 불변식 5: 기본값(memory) 동등성 ────────────────────────────────────────────


async def test_memory_backend_keeps_the_process_local_semantics() -> None:
    """기본 백엔드는 이 기능 도입 전과 같다 — 서로 다른 인스턴스는 서로를 모른다."""
    worker_a = ActiveStreamRegistry()
    worker_b = ActiveStreamRegistry()

    assert await worker_a.acquire("member-7:thread-1")
    assert await worker_b.acquire("member-7:thread-1")
    assert worker_a.lease_renewal_due("member-7:thread-1") is False


def test_default_backend_builds_the_process_local_registry() -> None:
    assert type(registry_mod.build_registry()) is ActiveStreamRegistry


def test_shared_backend_builds_the_shared_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, stream_registry_backend="shared")
    monkeypatch.setattr(registry_mod, "get_settings", lambda: settings)
    registry = registry_mod.build_registry()
    assert isinstance(registry, SharedStreamRegistry)
    assert isinstance(registry._store, registry_mod.PostgresSharedStreamStore)


# ── 불변식 7: active_count() 는 저장소를 치지 않는다 ───────────────────────────


async def test_active_count_never_touches_the_store(shared_settings: Settings) -> None:
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    worker_a, worker_b = _two_workers(store, clock)
    assert await worker_a.acquire("a-1")
    assert await worker_b.acquire("b-1")

    class _ExplodingStore(InMemorySharedStreamStore):
        async def scope_has_active_stream(self, **kwargs) -> bool:  # noqa: ANN003
            raise AssertionError("active_count must not read the store")

    # 저장소를 폭발하는 것으로 바꿔도 active_count/is_active/is_fenced 는 멀쩡하다.
    worker_a._store = _ExplodingStore(clock=clock)
    assert worker_a.active_count() == 1, "워커별 값이다 — 다른 워커의 슬롯은 세지 않는다"
    assert worker_a.is_active("a-1")
    assert not worker_a.is_active("b-1")
    assert not worker_a.is_fenced("7", "session-1")


# ── D5: 저장소 장애는 fail-closed (503 STATE_UNAVAILABLE) ─────────────────────


async def test_store_failure_fails_closed_on_acquire(shared_settings: Settings) -> None:
    registry = SharedStreamRegistry(_BrokenStore(), instance_id="worker-a")
    with pytest.raises(SessionStateUnavailable):
        await registry.acquire("member-7:thread-1", owner_id="7", session_id="session-1")
    assert not registry.is_active("member-7:thread-1")


async def test_store_failure_fails_closed_on_fence_and_scope_idle(
    shared_settings: Settings,
) -> None:
    registry = SharedStreamRegistry(_BrokenStore(), instance_id="worker-a")
    with pytest.raises(SessionStateUnavailable):
        await registry.acquire_fence("7", "session-1")
    with pytest.raises(SessionStateUnavailable):
        await registry.wait_for_scope_idle("7", "session-1")


async def test_release_paths_never_raise_on_store_failure(
    shared_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """해제는 finally 경로다 — 던지면 원래 예외를 가린다. lease 만료로 자가 치유된다."""
    store = InMemorySharedStreamStore()
    registry = SharedStreamRegistry(store, instance_id="worker-a")
    assert await registry.acquire("member-7:thread-1")
    fence = await registry.acquire_fence("7", "session-2")
    assert fence is not None

    registry._store = _BrokenStore()
    with caplog.at_level(logging.WARNING, logger="app.core.stream_registry"):
        await registry.release("member-7:thread-1")
        await registry.release_fence(fence)
        await registry.renew_lease("member-7:thread-1")

    assert "STREAM_REGISTRY_RELEASE_FAILED" in caplog.text
    assert "STREAM_FENCE_RELEASE_FAILED" in caplog.text
    assert not registry.is_active("member-7:thread-1")
    assert not registry.is_fenced("7", "session-2")


# ── open_stream 배선 ─────────────────────────────────────────────────────────


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


async def test_open_stream_fails_closed_when_the_shared_store_is_down(
    shared_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """저장소 장애는 조용히 통과하지 않는다 — 503 STATE_UNAVAILABLE 로 나간다(D5)."""
    monkeypatch.setattr(
        stream_mod, "get_registry", lambda: SharedStreamRegistry(_BrokenStore(), instance_id="a")
    )

    async def never_used(_turn_started_at: float):
        yield "data: unused\n\n"  # pragma: no cover - acquire 가 먼저 던진다

    with pytest.raises(SessionStateUnavailable):
        await stream_mod.open_stream(_FakeRequest(), "member-7:thread-1", never_used)


async def test_open_stream_renews_the_lease_on_polling_ticks(
    shared_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """연장 훅이 폴링 tick 에 실제로 걸려 있는지 — 훅을 지우면 이 테스트가 깨진다."""
    clock = _Clock()
    store = InMemorySharedStreamStore(clock=clock)
    registry = SharedStreamRegistry(store, instance_id="worker-a", clock=clock)
    monkeypatch.setattr(stream_mod, "get_registry", lambda: registry)
    renewals: list[str] = []
    original = registry.renew_lease

    async def counting(stream_key: str) -> None:
        renewals.append(stream_key)
        await original(stream_key)

    monkeypatch.setattr(registry, "renew_lease", counting)

    async def slow_then_done(_turn_started_at: float):
        # 첫 프레임 전에 폴링 tick 을 한 번 돌게 하고, 그 사이 lease 연장 시각을 넘긴다.
        clock.advance(shared_settings.stream_registry_lease_renew_interval_s + 1)
        await asyncio.sleep(shared_settings.stream_disconnect_poll_s * 2)
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    response = await stream_mod.open_stream(_FakeRequest(), "member-7:renew-thread", slow_then_done)
    _ = [chunk async for chunk in response.body_iterator]

    assert renewals == ["member-7:renew-thread"]
    assert not registry.is_active("member-7:renew-thread")


# ── config 교차 검증 ──────────────────────────────────────────────────────────


def test_lease_renewal_must_stay_well_under_the_ttl() -> None:
    with pytest.raises(ValueError, match="STREAM_REGISTRY_LEASE_RENEW_INTERVAL_S"):
        Settings(
            _env_file=None,
            stream_registry_lease_ttl_s=10.0,
            stream_registry_lease_renew_interval_s=5.0,
        )


def test_scope_idle_cap_must_exceed_the_lease_ttl() -> None:
    with pytest.raises(ValueError, match="STREAM_REGISTRY_SCOPE_IDLE_WAIT_MAX_S"):
        Settings(
            _env_file=None,
            stream_registry_lease_ttl_s=60.0,
            stream_registry_scope_idle_wait_max_s=60.0,
        )
