"""이슈 #476 완료 조건 3 — 공유 스트림 레지스트리의 **실제 SQL 배선**.

단위 테스트(`tests/unit/test_shared_stream_registry_476.py`)는 인메모리 저장소로 레지스트리
프로토콜 로직을 고정한다. 여기서는 같은 불변식 1~4 를 실 pg-profile 로 한 번씩 확인해
`pg_advisory_xact_lock` 직렬화·조건부 UPSERT·lease 필터가 정말로 걸리는지 검증한다.

    docker compose up -d pg-catalog pg-profile   # catalog 5433 / profile 5434
    uv run pytest -m integration tests/integration/test_pg_shared_stream_registry.py
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from psycopg_pool import AsyncConnectionPool

from app.core import stream_registry as registry_mod
from app.core.config import Settings
from app.core.stream_registry import PostgresSharedStreamStore, SharedStreamRegistry

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_registry(monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings

    settings = Settings(
        _env_file=None,
        stream_registry_backend="shared",
        # 통합 테스트에서 만료를 기다릴 수 있게 짧게 잡는다(교차 검증은 renew < ttl/2).
        stream_registry_lease_ttl_s=2.0,
        stream_registry_lease_renew_interval_s=0.5,
        stream_registry_scope_poll_s=0.05,
        stream_registry_scope_idle_wait_max_s=10.0,
    )
    monkeypatch.setattr(registry_mod, "get_settings", lambda: settings)

    pool = AsyncConnectionPool(
        get_settings().profile_db_url,
        open=False,
        min_size=1,
        max_size=6,
    )
    await pool.open(wait=True)
    store = PostgresSharedStreamStore(pool=pool)
    await store.initialize()
    prefix = f"it-registry-{uuid.uuid4().hex}"
    worker_a = SharedStreamRegistry(store, instance_id="worker-a")
    worker_b = SharedStreamRegistry(store, instance_id="worker-b")
    try:
        yield worker_a, worker_b, pool, prefix, settings
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM active_streams WHERE stream_key LIKE %s", (prefix + "%",)
            )
            await conn.execute(
                "DELETE FROM stream_scope_fences WHERE session_id LIKE %s", (prefix + "%",)
            )
        await pool.close()


async def test_pg_only_one_worker_acquires_the_same_stream_key(pg_registry) -> None:
    worker_a, worker_b, pool, prefix, _ = pg_registry
    key = f"{prefix}:thread-1"

    results = await asyncio.gather(worker_a.acquire(key), worker_b.acquire(key))

    assert sorted(results) == [False, True]
    async with pool.connection() as conn:
        rows = await (
            await conn.execute("SELECT instance_id FROM active_streams WHERE stream_key=%s", (key,))
        ).fetchall()
    assert len(rows) == 1


async def test_pg_fence_and_active_stream_exclude_each_other_across_workers(pg_registry) -> None:
    worker_a, worker_b, _pool, prefix, _ = pg_registry
    session_id = f"{prefix}-session"

    fence = await worker_a.acquire_fence("7", session_id)
    assert fence is not None
    assert not await worker_b.acquire(f"{prefix}:thread-1", owner_id="7", session_id=session_id)

    await worker_a.release_fence(fence)
    assert await worker_b.acquire(f"{prefix}:thread-1", owner_id="7", session_id=session_id)
    # 반대 방향 — 활성 스트림이 있으면 다른 워커는 fence 를 못 잡는다.
    assert await worker_a.acquire_fence("7", session_id) is None


async def test_pg_concurrent_scope_operations_never_both_succeed(pg_registry) -> None:
    """advisory lock 직렬화 확인 — fence 와 stream 이 동시에 성립하면 안 된다."""
    worker_a, worker_b, _pool, prefix, _ = pg_registry
    session_id = f"{prefix}-race"

    fence, acquired = await asyncio.gather(
        worker_a.acquire_fence("7", session_id),
        worker_b.acquire(f"{prefix}:race-thread", owner_id="7", session_id=session_id),
    )

    assert not (fence is not None and acquired), (
        "같은 스코프의 fence 와 활성 스트림이 동시에 성립하면 §2.9(a) 가드가 뚫린다"
    )


async def test_pg_scope_idle_waits_for_the_other_workers_stream(pg_registry) -> None:
    worker_a, worker_b, _pool, prefix, _ = pg_registry
    session_id = f"{prefix}-idle"
    key = f"{prefix}:idle-thread"
    assert await worker_a.acquire(key, owner_id="7", session_id=session_id)

    waiter = asyncio.create_task(worker_b.wait_for_scope_idle("7", session_id))
    await asyncio.sleep(0.2)
    assert not waiter.done()

    await worker_a.release(key)
    await asyncio.wait_for(waiter, timeout=5.0)


async def test_pg_expired_lease_from_a_dead_worker_frees_the_slot(pg_registry) -> None:
    worker_a, worker_b, _pool, prefix, settings = pg_registry
    key = f"{prefix}:dead-thread"
    session_id = f"{prefix}-dead"
    assert await worker_a.acquire(key, owner_id="7", session_id=session_id)
    assert not await worker_b.acquire(key, owner_id="7", session_id=session_id)

    # worker_a 가 죽었다 — release 도 lease 연장도 없다. TTL 뒤에는 슬롯이 풀린다(#48).
    await asyncio.sleep(settings.stream_registry_lease_ttl_s + 0.5)
    assert await worker_b.acquire(key, owner_id="7", session_id=session_id)


async def test_pg_renewed_lease_keeps_the_slot_alive(pg_registry) -> None:
    worker_a, worker_b, _pool, prefix, settings = pg_registry
    key = f"{prefix}:renew-thread"
    assert await worker_a.acquire(key)

    deadline = asyncio.get_running_loop().time() + settings.stream_registry_lease_ttl_s + 0.5
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(settings.stream_registry_lease_renew_interval_s / 2)
        if worker_a.lease_renewal_due(key):
            await worker_a.renew_lease(key)

    assert not await worker_b.acquire(key), "연장된 lease 는 TTL 을 넘겨도 살아 있어야 한다"


async def test_pg_release_only_deletes_the_row_this_worker_owns(pg_registry) -> None:
    worker_a, worker_b, pool, prefix, _ = pg_registry
    key = f"{prefix}:owned-thread"
    assert await worker_a.acquire(key)
    assert not await worker_b.acquire(key)

    await worker_b.release(key)
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT 1 FROM active_streams WHERE stream_key=%s", (key,))
        ).fetchone()
    assert row is not None, "슬롯을 못 잡은 워커의 release 가 남의 행을 지우면 안 된다"

    # 레지스트리의 로컬 토큰 가드가 여기서 이미 막지만, DELETE 의 stream_token 조건 자체도
    # 방어선이다 — 저장소를 직접 쳐서 그 조건을 검증한다(레지스트리 가드만 보면 SQL 을
    # `WHERE stream_key=%s` 로 완화해도 테스트가 통과한다).
    store = worker_a._store
    await store.release_stream(stream_key=key, stream_token=str(uuid.uuid4()))
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT 1 FROM active_streams WHERE stream_key=%s", (key,))
        ).fetchone()
    assert row is not None, "다른 토큰으로 부른 DELETE 가 살아있는 행을 지우면 안 된다"

    await store.release_stream(stream_key=key, stream_token=worker_a._stream_tokens[key])
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT 1 FROM active_streams WHERE stream_key=%s", (key,))
        ).fetchone()
    assert row is None
