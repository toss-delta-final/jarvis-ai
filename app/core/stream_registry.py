"""동시 스트림 레지스트리 (api-spec §2.9 a) — 프로세스 로컬 / 워커 간 공유 두 백엔드.

설계 정본은 `docs/specs/DESIGN-SHARED-STREAM-REGISTRY-476.md` 다. 요약:

- `ActiveStreamRegistry` — 인메모리(기본). 변경 연산이 `async def` 지만 본문에 `await` 가
  없으므로 코루틴이 이벤트 루프에 양보하지 않는다 — check-then-set 원자성은 이전과 동일하게
  공짜로 성립한다.
- `SharedStreamRegistry` — pg-profile 테이블 2개(`db/profile/init/04_stream_registry.sql`)로
  활성 슬롯·scope fence·scope idle 을 **셋 다** 워커 간에 공유한다. fence 원자성의 근거는
  이벤트 루프가 아니라 스코프 키에 건 `pg_advisory_xact_lock` 이다(SPEC §3).
- 두 백엔드 모두 `active_count()`/`is_active()`/`is_fenced()` 는 **sync·이 워커 관점**이다.
  프레임마다 호출되는 관측 경로라 DB 를 치면 안 되고(SPEC §2.1), 의미도 "이 이벤트 루프가
  얼마나 붐비는가" 라서 지표의 원래 목적에 더 맞다.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.observability import worker_instance_id
from app.core.session_context import SessionStateUnavailable

logger = get_logger(__name__)


@dataclass(frozen=True)
class StreamScopeFence:
    """Owner/session registry fence identity token."""

    owner_id: str
    session_id: str


@dataclass
class _ScopeIdleWaiters:
    event: asyncio.Event
    count: int = 0


class ActiveStreamRegistry:
    """인메모리 활성 스트림 레지스트리 (§2.9 a).

    변경 연산은 `async def` 지만 본문에 `await` 가 없다 — 호출자가 `await` 해도 코루틴이
    이벤트 루프에 양보하지 않으므로 check-then-add 사이에 선점이 없다.
    """

    def __init__(self) -> None:
        self._active: dict[str, tuple[str, str] | None] = {}
        self._fences: dict[tuple[str, str], StreamScopeFence] = {}
        self._scope_idle: dict[tuple[str, str], _ScopeIdleWaiters] = {}

    async def acquire(
        self,
        stream_key: str,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """활성 등록. buyer scope가 fenced 상태이거나 key가 활성이면 False."""
        scope = _scope_of(owner_id, session_id)
        if stream_key in self._active:
            return False
        if scope is not None and scope in self._fences:
            return False
        self._active[stream_key] = scope
        if scope is not None:
            waiters = self._scope_idle.get(scope)
            if waiters is not None:
                waiters.event.clear()
        return True

    async def release(self, stream_key: str) -> None:
        """활성 해제 (중복 해제 무해)."""
        scope = self._active.pop(stream_key, None)
        if scope is not None and scope not in self._active.values():
            waiters = self._scope_idle.get(scope)
            if waiters is not None:
                waiters.event.set()

    def is_active(self, stream_key: str) -> bool:
        """이 워커가 들고 있는 슬롯인지. 교차 워커 판정은 `acquire` 가 저장소에서 한다."""
        return stream_key in self._active

    def active_count(self) -> int:
        """이 워커의 활성 스트림 수를 O(1)로 반환한다. scope 예약인 fence는 세지 않는다."""
        return len(self._active)

    async def acquire_fence(self, owner_id: str, session_id: str) -> StreamScopeFence | None:
        """해당 buyer session scope에 active stream이 없을 때 non-blocking fence를 획득한다."""
        scope = (owner_id, session_id)
        if scope in self._fences or scope in self._active.values():
            return None
        token = StreamScopeFence(owner_id=owner_id, session_id=session_id)
        self._fences[scope] = token
        return token

    async def release_fence(self, token: StreamScopeFence) -> None:
        """발급한 동일 token 객체만 fence를 해제할 수 있다."""
        scope = (token.owner_id, token.session_id)
        if self._fences.get(scope) is not token:
            raise ValueError("stream scope fence token is not active")
        del self._fences[scope]

    def is_fenced(self, owner_id: str, session_id: str) -> bool:
        """이 워커가 들고 있는 fence인지. 교차 워커 판정은 `acquire_fence` 가 저장소에서 한다."""
        return (owner_id, session_id) in self._fences

    async def wait_for_scope_idle(self, owner_id: str, session_id: str) -> None:
        """해당 buyer session scope의 기존 stream이 모두 종료될 때까지 event 기반 대기한다."""
        scope = (owner_id, session_id)
        while scope in self._active.values():
            waiters = self._scope_idle.get(scope)
            if waiters is None or waiters.event.is_set():
                waiters = _ScopeIdleWaiters(asyncio.Event())
                self._scope_idle[scope] = waiters
            waiters.count += 1
            try:
                await waiters.event.wait()
            finally:
                waiters.count -= 1
                if waiters.count == 0 and self._scope_idle.get(scope) is waiters:
                    del self._scope_idle[scope]

    def lease_renewal_due(self, stream_key: str) -> bool:
        """폴링 tick 에서 DB 왕복이 필요한지 **sync** 로 먼저 묻는다 (인메모리는 항상 False)."""
        return False

    async def renew_lease(self, stream_key: str) -> None:
        """인메모리 백엔드에는 lease가 없다 (프로세스가 죽으면 dict도 사라진다)."""
        return None

    async def close(self) -> None:
        """소유 자원 정리 (인메모리는 없음)."""
        return None


def _scope_of(owner_id: str | None, session_id: str | None) -> tuple[str, str] | None:
    if (owner_id is None) != (session_id is None):
        raise ValueError("owner_id and session_id must be provided together")
    if owner_id is None or session_id is None:
        return None
    return (owner_id, session_id)


class SharedStreamStore(Protocol):
    """공유 레지스트리 저장소. 원자성은 구현이 책임진다(SPEC §3)."""

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def try_acquire_stream(
        self,
        *,
        stream_key: str,
        stream_token: str,
        owner_id: str | None,
        session_id: str | None,
        instance_id: str,
        ttl_s: float,
    ) -> bool: ...

    async def release_stream(self, *, stream_key: str, stream_token: str) -> None: ...

    async def renew_stream(self, *, stream_key: str, stream_token: str, ttl_s: float) -> bool: ...

    async def try_acquire_fence(
        self,
        *,
        owner_id: str,
        session_id: str,
        fence_token: str,
        instance_id: str,
        ttl_s: float,
    ) -> bool: ...

    async def release_fence(self, *, owner_id: str, session_id: str, fence_token: str) -> None: ...

    async def scope_has_active_stream(self, *, owner_id: str, session_id: str) -> bool: ...


class SharedStreamRegistry(ActiveStreamRegistry):
    """워커 간 공유 레지스트리. 프로세스 로컬 미러는 관측·토큰 보관용으로 유지한다."""

    def __init__(
        self,
        store: SharedStreamStore,
        *,
        instance_id: str | None = None,
        clock=time.monotonic,  # noqa: ANN001
    ) -> None:
        super().__init__()
        self._store = store
        self._instance_id = instance_id or worker_instance_id()
        self._clock = clock
        self._stream_tokens: dict[str, str] = {}
        self._fence_tokens: dict[tuple[str, str], str] = {}
        self._next_renewal: dict[str, float] = {}

    async def acquire(
        self,
        stream_key: str,
        *,
        owner_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        scope = _scope_of(owner_id, session_id)
        if stream_key in self._active:
            return False
        settings = get_settings()
        token = str(uuid.uuid4())
        acquired = await self._guarded(
            "acquire",
            self._store.try_acquire_stream(
                stream_key=stream_key,
                stream_token=token,
                owner_id=owner_id,
                session_id=session_id,
                instance_id=self._instance_id,
                ttl_s=settings.stream_registry_lease_ttl_s,
            ),
        )
        if not acquired:
            return False
        self._active[stream_key] = scope
        self._stream_tokens[stream_key] = token
        self._next_renewal[stream_key] = (
            self._clock() + settings.stream_registry_lease_renew_interval_s
        )
        return True

    async def release(self, stream_key: str) -> None:
        self._active.pop(stream_key, None)
        self._next_renewal.pop(stream_key, None)
        token = self._stream_tokens.pop(stream_key, None)
        if token is None:
            return
        # 해제 실패는 던지지 않는다 — finally 경로라 원래 예외를 가리고, lease 만료로 자가
        # 치유된다(SPEC §6).
        try:
            await self._store.release_stream(stream_key=stream_key, stream_token=token)
        except Exception:
            logger.warning("shared stream release failed code=STREAM_REGISTRY_RELEASE_FAILED")

    async def acquire_fence(self, owner_id: str, session_id: str) -> StreamScopeFence | None:
        scope = (owner_id, session_id)
        if scope in self._fences:
            return None
        settings = get_settings()
        token = str(uuid.uuid4())
        acquired = await self._guarded(
            "acquire_fence",
            self._store.try_acquire_fence(
                owner_id=owner_id,
                session_id=session_id,
                fence_token=token,
                instance_id=self._instance_id,
                ttl_s=settings.stream_registry_lease_ttl_s,
            ),
        )
        if not acquired:
            return None
        fence = StreamScopeFence(owner_id=owner_id, session_id=session_id)
        self._fences[scope] = fence
        self._fence_tokens[scope] = token
        return fence

    async def release_fence(self, token: StreamScopeFence) -> None:
        scope = (token.owner_id, token.session_id)
        if self._fences.get(scope) is not token:
            raise ValueError("stream scope fence token is not active")
        del self._fences[scope]
        fence_token = self._fence_tokens.pop(scope, None)
        if fence_token is None:
            return
        try:
            await self._store.release_fence(
                owner_id=token.owner_id,
                session_id=token.session_id,
                fence_token=fence_token,
            )
        except Exception:
            logger.warning("shared stream fence release failed code=STREAM_FENCE_RELEASE_FAILED")

    async def wait_for_scope_idle(self, owner_id: str, session_id: str) -> None:
        """`asyncio.Event` 는 프로세스를 못 넘는다 — 폴링 + 상한으로 대기한다(SPEC §5)."""
        settings = get_settings()
        poll = settings.stream_registry_scope_poll_s
        deadline = self._clock() + settings.stream_registry_scope_idle_wait_max_s
        while True:
            if not await self._scope_busy(owner_id, session_id):
                return
            remaining = deadline - self._clock()
            if remaining <= 0:
                # 무한 대기 금지. 원격 워커가 죽으면 lease 만료로 풀리므로 상한 도달은 이상 상황.
                logger.warning(
                    "shared stream scope idle wait cap reached "
                    "code=STREAM_SCOPE_IDLE_WAIT_CAP max_s=%s",
                    settings.stream_registry_scope_idle_wait_max_s,
                )
                return
            await asyncio.sleep(min(poll, remaining))

    async def _scope_busy(self, owner_id: str, session_id: str) -> bool:
        if (owner_id, session_id) in self._active.values():
            return True
        return await self._guarded(
            "scope_idle",
            self._store.scope_has_active_stream(owner_id=owner_id, session_id=session_id),
        )

    def lease_renewal_due(self, stream_key: str) -> bool:
        due = self._next_renewal.get(stream_key)
        return due is not None and self._clock() >= due

    async def renew_lease(self, stream_key: str) -> None:
        token = self._stream_tokens.get(stream_key)
        if token is None:
            return
        settings = get_settings()
        # 실패해도 다음 tick 에 바로 다시 치지 않도록 먼저 미룬다.
        self._next_renewal[stream_key] = (
            self._clock() + settings.stream_registry_lease_renew_interval_s
        )
        try:
            alive = await self._store.renew_stream(
                stream_key=stream_key,
                stream_token=token,
                ttl_s=settings.stream_registry_lease_ttl_s,
            )
        except Exception:
            logger.warning("shared stream lease renewal failed code=STREAM_LEASE_RENEW_FAILED")
            return
        if not alive:
            logger.warning("shared stream lease lost code=STREAM_LEASE_LOST")

    async def close(self) -> None:
        await self._store.close()

    async def _guarded(self, operation: str, awaitable):  # noqa: ANN001, ANN202
        """저장소 장애는 fail-closed — 503 STATE_UNAVAILABLE 로 나간다(SPEC §6)."""
        try:
            return await awaitable
        except asyncio.CancelledError:
            raise
        except SessionStateUnavailable:
            raise
        except Exception as exc:
            logger.warning(
                "shared stream registry store unavailable "
                "code=STREAM_REGISTRY_UNAVAILABLE operation=%s",
                operation,
                exc_info=True,
            )
            raise SessionStateUnavailable("shared stream registry is unavailable") from exc


@dataclass
class _MemoryStreamRow:
    stream_token: str
    owner_id: str | None
    session_id: str | None
    instance_id: str
    expires_at: float


@dataclass
class _MemoryFenceRow:
    fence_token: str
    instance_id: str
    expires_at: float


@dataclass
class InMemorySharedStreamStore:
    """테스트용 공유 저장소 — 레지스트리 인스턴스 여러 개가 한 저장소를 본다.

    실제 정본은 `PostgresSharedStreamStore` 이며, SQL 의 원자성(advisory lock·조건부 UPSERT)은
    `tests/integration/test_pg_shared_stream_registry.py` 가 실 pg-profile 로 검증한다. 여기서는
    같은 직렬화를 `asyncio.Lock` 으로 흉내내 레지스트리 프로토콜 로직만 고정한다.
    """

    clock: object = time.monotonic
    streams: dict[str, _MemoryStreamRow] = field(default_factory=dict)
    fences: dict[tuple[str, str], _MemoryFenceRow] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _now(self) -> float:
        return self.clock()  # type: ignore[operator]

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def _live_scope_streams(self, owner_id: str, session_id: str) -> bool:
        now = self._now()
        return any(
            row.owner_id == owner_id and row.session_id == session_id and row.expires_at > now
            for row in self.streams.values()
        )

    async def try_acquire_stream(
        self,
        *,
        stream_key: str,
        stream_token: str,
        owner_id: str | None,
        session_id: str | None,
        instance_id: str,
        ttl_s: float,
    ) -> bool:
        async with self._lock:
            now = self._now()
            if owner_id is not None and session_id is not None:
                fence = self.fences.get((owner_id, session_id))
                if fence is not None and fence.expires_at > now:
                    return False
            current = self.streams.get(stream_key)
            if current is not None and current.expires_at > now:
                return False
            self.streams[stream_key] = _MemoryStreamRow(
                stream_token, owner_id, session_id, instance_id, now + ttl_s
            )
            return True

    async def release_stream(self, *, stream_key: str, stream_token: str) -> None:
        async with self._lock:
            current = self.streams.get(stream_key)
            if current is not None and current.stream_token == stream_token:
                del self.streams[stream_key]

    async def renew_stream(self, *, stream_key: str, stream_token: str, ttl_s: float) -> bool:
        async with self._lock:
            current = self.streams.get(stream_key)
            if current is None or current.stream_token != stream_token:
                return False
            current.expires_at = self._now() + ttl_s
            return True

    async def try_acquire_fence(
        self,
        *,
        owner_id: str,
        session_id: str,
        fence_token: str,
        instance_id: str,
        ttl_s: float,
    ) -> bool:
        async with self._lock:
            now = self._now()
            if self._live_scope_streams(owner_id, session_id):
                return False
            current = self.fences.get((owner_id, session_id))
            if current is not None and current.expires_at > now:
                return False
            self.fences[(owner_id, session_id)] = _MemoryFenceRow(
                fence_token, instance_id, now + ttl_s
            )
            return True

    async def release_fence(self, *, owner_id: str, session_id: str, fence_token: str) -> None:
        async with self._lock:
            current = self.fences.get((owner_id, session_id))
            if current is not None and current.fence_token == fence_token:
                del self.fences[(owner_id, session_id)]

    async def scope_has_active_stream(self, *, owner_id: str, session_id: str) -> bool:
        async with self._lock:
            return self._live_scope_streams(owner_id, session_id)


class PostgresSharedStreamStore:
    """pg-profile 정본 저장소.

    스코프 조작(`try_acquire_stream` with scope · `try_acquire_fence`)은 트랜잭션 시작 직후
    `pg_advisory_xact_lock('stream-scope:<owner>:<session>')` 을 잡는다. 두 연산이 서로의
    테이블을 교차 검사하므로 같은 락으로 직렬화해야 "A 는 fence 없음을 보고 stream 을 넣고
    B 는 active 없음을 보고 fence 를 넣는" 레이스가 사라진다(SPEC §3).

    해시 충돌은 서로 다른 스코프가 락을 공유하게 만들 뿐 **거짓 409 를 만들지 않는다** —
    배제는 락이 아니라 커밋된 행이 유지한다.

    락 순서: 이 락은 자기 짧은 트랜잭션 안에서만 잡혔다 커밋 시 풀린다. 반대로
    `session_context::_advisory_lock`('chat-session:…')을 잡은 채 이 락을 기다리는 경로는
    있어도(claim_owner→acquire_fence) 그 역방향은 없으므로 순환 대기가 없다.
    """

    _SCOPE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"

    def __init__(self, pool=None) -> None:  # noqa: ANN001
        self._pool = pool
        self._owned_pool = None

    async def initialize(self) -> None:
        from pathlib import Path  # noqa: PLC0415

        if self._pool is None:
            self._pool = await self._open_pool()
            self._owned_pool = self._pool
        settings = get_settings()
        sql = (
            Path(__file__).parents[2] / "db" / "profile" / "init" / "04_stream_registry.sql"
        ).read_text(encoding="utf-8")
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(settings.state_store_migration_timeout_s * 1000))),),
                )
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("schema:stream_registry",),
                )
                await conn.execute(sql)

    async def _open_pool(self):  # noqa: ANN202
        from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415

        from app.core.pg_resilience import hardened_pg_conninfo  # noqa: PLC0415

        settings = get_settings()
        pool = AsyncConnectionPool(
            hardened_pg_conninfo(settings.profile_db_url),
            open=False,
            min_size=settings.state_store_pool_min_size,
            max_size=settings.state_store_pool_max_size,
            timeout=settings.state_store_query_timeout_s,
        )
        await asyncio.wait_for(pool.open(wait=True), timeout=settings.state_store_connect_timeout_s)
        return pool

    async def close(self) -> None:
        pool = self._owned_pool
        self._owned_pool = None
        self._pool = None
        if pool is not None:
            await pool.close()

    def _require_pool(self):  # noqa: ANN202
        if self._pool is None:
            raise SessionStateUnavailable("shared stream registry pool is not initialized")
        return self._pool

    @staticmethod
    def _scope_lock_key(owner_id: str, session_id: str) -> str:
        return f"stream-scope:{owner_id}:{session_id}"

    async def try_acquire_stream(
        self,
        *,
        stream_key: str,
        stream_token: str,
        owner_id: str | None,
        session_id: str | None,
        instance_id: str,
        ttl_s: float,
    ) -> bool:
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                if owner_id is not None and session_id is not None:
                    await conn.execute(
                        self._SCOPE_LOCK_SQL, (self._scope_lock_key(owner_id, session_id),)
                    )
                    fenced = await (
                        await conn.execute(
                            "SELECT 1 FROM stream_scope_fences "
                            "WHERE owner_id=%s AND session_id=%s AND lease_expires_at > now()",
                            (owner_id, session_id),
                        )
                    ).fetchone()
                    if fenced is not None:
                        return False
                row = await (
                    await conn.execute(
                        """
                        INSERT INTO active_streams
                            (stream_key, stream_token, owner_id, session_id,
                             instance_id, lease_expires_at)
                        VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))
                        ON CONFLICT (stream_key) DO UPDATE
                        SET stream_token=EXCLUDED.stream_token,
                            owner_id=EXCLUDED.owner_id,
                            session_id=EXCLUDED.session_id,
                            instance_id=EXCLUDED.instance_id,
                            lease_expires_at=EXCLUDED.lease_expires_at,
                            created_at=now()
                        WHERE active_streams.lease_expires_at <= now()
                        RETURNING stream_key
                        """,
                        (stream_key, stream_token, owner_id, session_id, instance_id, ttl_s),
                    )
                ).fetchone()
                return row is not None

    async def release_stream(self, *, stream_key: str, stream_token: str) -> None:
        pool = self._require_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM active_streams WHERE stream_key=%s AND stream_token=%s",
                (stream_key, stream_token),
            )

    async def renew_stream(self, *, stream_key: str, stream_token: str, ttl_s: float) -> bool:
        pool = self._require_pool()
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    """
                    UPDATE active_streams
                    SET lease_expires_at = now() + make_interval(secs => %s)
                    WHERE stream_key=%s AND stream_token=%s
                    RETURNING stream_key
                    """,
                    (ttl_s, stream_key, stream_token),
                )
            ).fetchone()
            return row is not None

    async def try_acquire_fence(
        self,
        *,
        owner_id: str,
        session_id: str,
        fence_token: str,
        instance_id: str,
        ttl_s: float,
    ) -> bool:
        pool = self._require_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    self._SCOPE_LOCK_SQL, (self._scope_lock_key(owner_id, session_id),)
                )
                active = await (
                    await conn.execute(
                        "SELECT 1 FROM active_streams "
                        "WHERE owner_id=%s AND session_id=%s AND lease_expires_at > now() LIMIT 1",
                        (owner_id, session_id),
                    )
                ).fetchone()
                if active is not None:
                    return False
                row = await (
                    await conn.execute(
                        """
                        INSERT INTO stream_scope_fences
                            (owner_id, session_id, fence_token, instance_id, lease_expires_at)
                        VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))
                        ON CONFLICT (owner_id, session_id) DO UPDATE
                        SET fence_token=EXCLUDED.fence_token,
                            instance_id=EXCLUDED.instance_id,
                            lease_expires_at=EXCLUDED.lease_expires_at,
                            created_at=now()
                        WHERE stream_scope_fences.lease_expires_at <= now()
                        RETURNING fence_token
                        """,
                        (owner_id, session_id, fence_token, instance_id, ttl_s),
                    )
                ).fetchone()
                return row is not None

    async def release_fence(self, *, owner_id: str, session_id: str, fence_token: str) -> None:
        pool = self._require_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "DELETE FROM stream_scope_fences "
                "WHERE owner_id=%s AND session_id=%s AND fence_token=%s",
                (owner_id, session_id, fence_token),
            )

    async def scope_has_active_stream(self, *, owner_id: str, session_id: str) -> bool:
        pool = self._require_pool()
        async with pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT 1 FROM active_streams "
                    "WHERE owner_id=%s AND session_id=%s AND lease_expires_at > now() LIMIT 1",
                    (owner_id, session_id),
                )
            ).fetchone()
            return row is not None


def registry_is_process_local() -> bool:
    """레지스트리가 프로세스 로컬인가 — 하드코딩이 아니라 설정에서 파생된다(SPEC §1).

    True 인 동안 워커 다중화는 §2.9(a) 409 가드를 무력화한다. False 여도 나머지 프로세스 로컬
    상태(docs/specs/OPS-SCALEOUT-476.md §2 인벤토리)를 검토했다는 뜻은 아니다.
    """
    return get_settings().stream_registry_backend != "shared"


_registry: ActiveStreamRegistry | None = None


def build_registry() -> ActiveStreamRegistry:
    """설정에 따라 백엔드를 고른다. 기본값 `memory` 는 DB 를 전혀 타지 않는다."""
    if get_settings().stream_registry_backend == "shared":
        return SharedStreamRegistry(PostgresSharedStreamStore())
    return ActiveStreamRegistry()


def get_registry() -> ActiveStreamRegistry:
    """활성 스트림 레지스트리 싱글턴."""
    global _registry
    if _registry is None:
        _registry = build_registry()
    return _registry


def set_registry(registry: ActiveStreamRegistry | None) -> None:
    """테스트/기동에서 싱글턴을 교체한다 (None 이면 다음 접근에 다시 만든다)."""
    global _registry
    _registry = registry


async def initialize_stream_registry() -> None:
    """공유 백엔드일 때만 스키마·풀을 준비한다(기동 1회)."""
    registry = get_registry()
    store = getattr(registry, "_store", None)
    if store is None:
        return
    await store.initialize()


async def close_stream_registry() -> None:
    """소유한 풀을 닫고 싱글턴을 리셋한다 (기본 백엔드는 no-op)."""
    global _registry
    registry = _registry
    _registry = None
    if registry is not None:
        await registry.close()
