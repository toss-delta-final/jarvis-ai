"""개인화 그래프 변경의 감사·멱등 원장·개인화 중지 플래그 — pg-profile 전용 테이블 3개 (이슈 #358).

`store.set_graph()` 는 CAS 없는 blind overwrite 라, 사용자가 직접 고치는 경로를 열려면 그 위에
낙관적 동시성(`revision` CAS)·재전송 판정(멱등 원장)·크래시 복구(저널 선행 기록)·감사가 필요하다.
`processed_events` 의 `PROCESSING`/`COMPLETED` + claim/lease 패턴을 그대로 따른다
(SPEC-PROFILE-GRAPH-149 §7.2 — "저널을 선행 기록하는 것이 크래시 복구의 근거다").

**테이블을 셋으로 나눈 이유** — SPEC §7.1 은 "감사 겸 저널 + 중지 플래그" 2개라고 적었지만,
REQ-PGRAPH-043 은 원장이 "최초 응답 본문을 그대로" 들고 있게 하고 api-spec §3.9.1 PATCH 응답의
`edge.to` 는 `"brand:소니"` — **라벨 원문**이다. 한 행에 합치면 REQ-PGRAPH-081 [HARD](라벨 원문
금지)을 어긴다. 게다가 삭제 시 원장 항목을 지우면 감사 행까지 사라져 REQ-PGRAPH-062(초기화는
감사 보존)와 충돌한다. 보존 기간도 서로 다르다(원장 `graph_idempotency_ttl_h` <= 감사
`graph_audit_retention_days`, REQ-PGRAPH-044 — `config.py` 가 기동 시 강제).

`store.py` 의 BaseStore 와 별개 연결을 쓴다 — BaseStore 는 이 용도의 원자적 INSERT 를 제공하지
않는다(`processed_events` 와 같은 이유). dev 폴백은 InMemory dict + 경고 1회, **운영(jwks)은
폴백 금지** — 원장이 InMemory 로 내려가면 재시작마다 재전송 판정이 증발해 "부작용 1회" 보장이
조용히 깨진다.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal, get_args

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.agents.profile.graph_errors import (
    GraphEdgeNotFound,
    GraphMutationError,
    GraphStoreUnavailable,
    GraphVersionConflict,
)
from app.agents.profile.graph_models import GraphDocument, GraphNode
from app.agents.profile.graph_mutations import apply_correction, apply_suppression
from app.core.config import get_settings
from app.core.observability import identifier_fingerprint
from app.core.pg_resilience import (
    hardened_pg_conninfo,
    is_state_store_unavailable,
    run_with_query_timeout,
)

logger = logging.getLogger(__name__)

# DDL 직렬화 키 — 세 테이블을 한 트랜잭션에서 만들므로 잠금도 하나다.
SCHEMA_LOCK_KEY = "schema:profile_graph:lifecycle"

# 감사 액션 어휘 (SPEC §5.4). **`edgeRestore` 는 없다** — #499 가 I-35(복구)를 폐기하고 개별
# 삭제를 즉시 물리 삭제로 바꿨다. DB CHECK 와 이 튜플이 같은 어휘를 강제해야 dev 폴백에서
# 통과한 값이 운영에서 거부되는 일이 안 생긴다.
GraphAuditAction = Literal["edgeUpdate", "edgeSuppress", "graphReset", "personalizationToggle"]
AUDIT_ACTIONS: tuple[str, ...] = get_args(GraphAuditAction)


class LedgerRequestMismatch(Exception):
    """같은 파생 키인데 요청 본문이 다르다 — 재생하면 안 되는 상황.

    파생 키는 `{action}:{userId}:{scopeId}:{ifMatch}` 라 **본문이 들어가지 않는다**. 스테일한
    `If-Match` 를 든 다른 요청이 같은 키를 만들 수 있고, 그때 최초 응답을 재생하면 호출자가
    보내지도 않은 변경의 결과를 성공으로 받는다. 호출부는 이걸 `409` 로 옮긴다.
    """


@dataclass(frozen=True)
class LedgerEntry:
    """원장 조회 결과 — 재전송 판정에 필요한 것만."""

    status: str
    response_payload: dict | None
    request_fp: str | None = None


@dataclass
class _FallbackEntry:
    """dev 폴백의 원장 행 미러. 시각은 단조 시계로 잰다(벽시계 되감김에 안 흔들리게)."""

    user_id: int
    scope_id: str | None
    status: str
    claim_token: str | None = None
    lease_deadline: float | None = None
    response_payload: dict | None = None
    request_fp: str | None = None
    created_at: float = field(default_factory=time.monotonic)


_monotonic = time.monotonic  # 테스트가 만료 경계를 sleep 없이 고정할 수 있게 훅으로 둔다


def derived_key(action: str, user_id: int | str, scope_id: str, if_match: str) -> str:
    """재전송 판정용 파생 키 (REQ-PGRAPH-043, api-spec §3.9).

    `{userId}` 는 **원문**이다 — 지문화하면 pepper 회전 시 TTL 내 모든 원장 히트가 증발해
    재전송이 중복 부작용을 낸다. raw userId 금지는 감사 레코드(`actor_fp`)에만 걸린 조항이다.

    `{scopeId}`: edge 2종 = `edgeId`, `graphReset` = `"ALL"`, `personalizationToggle` = 빈 값.
    `If-Match` 는 `"g42"` 와 `g42` 가 동등하므로(api-spec §3.9) 따옴표를 벗겨 정규화한다 —
    안 그러면 같은 선행조건이 두 개의 키가 되어 멱등이 성립하지 않는다.
    """
    return f"profile-graph-{action}:{user_id}:{scope_id}:{normalize_if_match(if_match)}"


def normalize_if_match(value: str) -> str:
    """`If-Match` 의 따옴표를 벗긴다 — `"g42"` 와 `g42` 는 동등하다(api-spec §3.9).

    `*`·약한 태그·누락·빈 값은 Spring 이 선-400 으로 막으므로(#499) 여기서 다루지 않는다.
    """
    return value.strip().strip('"')


_pool: AsyncConnectionPool | None = None
_fallback: dict[str, dict] | None = None  # dev 폴백 — 테이블별 InMemory 상태
_fallback_warned = False
_init_lock = asyncio.Lock()
_pending_cleanup: list[AsyncConnectionPool] = []  # set_pool()/reset() 이 못 닫은 이전 풀


def _blank_fallback() -> dict[str, dict]:
    """폴백 상태의 빈 형태 — 실 테이블 3개와 1:1 로 둔다."""
    return {"audit": {}, "idempotency": {}, "personalization": {}}


def set_pool(pool: AsyncConnectionPool | None) -> None:
    """풀 교체(테스트용) — None 이면 다음 사용 시 재초기화한다.

    sync 라 여기서 직접 await 할 수 없다. fire-and-forget 태스크는 실행 중인 루프가 없으면
    조용히 스킵되므로(`processed_events.set_pool` 주석의 전례), 정리 대기열에 넣고 다음
    `_get_pool()`(반드시 async 컨텍스트)에서 확실히 닫는다.
    """
    global _pool, _fallback
    old_pool = _pool
    _pool = pool
    _fallback = None
    if old_pool is not None:
        _pending_cleanup.append(old_pool)


async def _drain_pending_cleanup(*, propagate_errors: bool = False) -> None:
    """대기열의 이전 풀들을 닫는다 — 이미 소멸한 루프에서 만들어진 풀일 수 있다.

    `CancelledError` 를 무조건 삼키면 이 `await` 지점에서 **현재 태스크 자체**가 취소되는
    경우까지 함께 삼켜진다. `task.cancelling()` 으로 "다른 루프 잔재"와 "실제 취소 요청"을
    구분해 후자만 재전파한다(`processed_events._drain_pending_cleanup` 과 동일).
    """
    first_error: Exception | None = None
    while _pending_cleanup:
        pool = _pending_cleanup.pop()
        try:
            await pool.close()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
        except Exception as exc:
            logger.warning("graph_journal pool cleanup failed", exc_info=True)
            if first_error is None:
                first_error = exc
    if propagate_errors and first_error is not None:
        raise first_error


async def close_pool() -> None:
    """지금 열려 있는 풀을 **이 이벤트 루프에서** 닫는다 (이슈 #208).

    살아 있는 `AsyncConnectionPool` 을 만든 루프가 그대로 닫히면, 루프 teardown 의
    `_cancel_all_tasks()` 가 취소를 삼키는 psycopg 워커와 만나 영원히 반환하지 않는다.
    **이 함수는 `tests/conftest.py::close_pg_pools_on_loop` 에 배선돼 있어야 한다** —
    누락은 CI 무한 대기로 나타나고 `tests/integration/test_pg_pool_loop_teardown.py` 가 잡는다.
    """
    set_pool(None)
    await _drain_pending_cleanup(propagate_errors=True)


def reset() -> None:
    """테스트 격리용 — InMemory 폴백으로 초기화(실제 연결 시도 없이 즉시 blank).

    `_init_lock` 도 새로 만든다 — pytest-asyncio 는 테스트 함수마다 새 이벤트 루프를 쓰는데,
    모듈 전역 `asyncio.Lock` 을 여러 루프에 걸쳐 재사용하면 이전 루프에 묶인 내부 상태 때문에
    다음 테스트에서 락 획득이 영영 안 풀리는 hang 이 난다(docs/lessons.md 2026-07-20).
    """
    global _pool, _fallback, _init_lock
    old_pool = _pool
    _pool = None
    _fallback = _blank_fallback()
    _init_lock = asyncio.Lock()
    if old_pool is not None:
        _pending_cleanup.append(old_pool)


async def _get_pool() -> AsyncConnectionPool | None:
    """AsyncConnectionPool(pg-profile) 지연 초기화 — 실패 시 dev 한정 InMemory 폴백(None 반환).

    락 없는 지연 초기화는 콜드 스타트 시 동시 요청이 풀을 중복 생성한다 — `_init_lock` 으로
    초기화 블록 전체를 직렬화한다.
    """
    global _pool, _fallback, _fallback_warned
    await _drain_pending_cleanup()
    async with _init_lock:
        if _pool is None and _fallback is None:
            settings = get_settings()
            pool = None
            try:
                pool = AsyncConnectionPool(
                    hardened_pg_conninfo(settings.profile_db_url),
                    open=False,
                    min_size=settings.state_store_pool_min_size,
                    max_size=settings.state_store_pool_max_size,
                    timeout=settings.state_store_query_timeout_s,
                )
                await asyncio.wait_for(
                    pool.open(wait=True), timeout=settings.state_store_connect_timeout_s
                )
                await _ensure_schema(pool)
                _pool = pool
            except Exception as exc:
                if pool is not None:
                    # open() 부분 실패(타임아웃 등) — 이미 생성된 풀을 닫아 커넥션 누수를 막는다.
                    # 정리 중 나는 CancelledError 는 실제 취소만 재전파한다(위와 동일 이유).
                    try:
                        await pool.close()
                    except asyncio.CancelledError:
                        task = asyncio.current_task()
                        if task is not None and task.cancelling() > 0:
                            raise
                    except Exception:
                        pass
                if settings.auth_mode == "jwks":
                    raise  # 운영 — 폴백 금지(멱등·감사가 조용히 깨지면 안 된다)
                if not _fallback_warned:
                    logger.warning(
                        "pg-profile graph_journal 연결 실패(%s) — InMemory 폴백 "
                        "(dev 전용: 프로세스 재시작 시 멱등 원장·감사 증발)",
                        exc,
                    )
                    _fallback_warned = True
                _fallback = _blank_fallback()
    return _pool


async def _ensure_schema(pool: AsyncConnectionPool) -> None:
    """테이블 3개를 idempotent 하게 만든다 (`db/profile/init/04_profile_graph.sql` 과 같은 정의).

    docker-entrypoint init script 는 **빈 볼륨에서만** 실행되므로 앱 연결 시점의 이 마이그레이션이
    정본이다(`processed_events._ensure_schema` 와 같은 규약). 세 테이블을 한 advisory 잠금·한
    트랜잭션에서 만들어, 동시 기동한 인스턴스가 서로 절반씩 만든 상태를 못 만들게 한다.
    """
    settings = get_settings()

    async def _run() -> None:
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(max(1, int(settings.state_store_migration_timeout_s * 1000))),),
                )
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (SCHEMA_LOCK_KEY,),
                )
                # 감사 — 지문만 담고 영구 보존한다. 전체 초기화도 이 테이블은 지우지 않는다
                # (REQ-PGRAPH-062: 파괴 동작이 추적 불가가 되면 안 된다). `actor_fp`·`object_fp`
                # 는 peppered HMAC 이고 **raw userId·라벨 원문은 어떤 컬럼에도 넣지 않는다**
                # (REQ-PGRAPH-081 [HARD]). `predicate` 만 고정 enum 이라 원문으로 남긴다.
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS profile_graph_audit (
                        id bigserial PRIMARY KEY,
                        request_id text NOT NULL,
                        actor_fp text NOT NULL,
                        action text NOT NULL,
                        edge_id_before text,
                        edge_id_after text,
                        predicate text,
                        object_fp text,
                        graph_version_before text NOT NULL,
                        graph_version_after text NOT NULL,
                        created_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                await conn.execute(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'profile_graph_audit_action_check'
                              AND conrelid = 'profile_graph_audit'::regclass
                        ) THEN
                            ALTER TABLE profile_graph_audit
                                ADD CONSTRAINT profile_graph_audit_action_check
                                CHECK (action IN (
                                    'edgeUpdate', 'edgeSuppress',
                                    'graphReset', 'personalizationToggle'
                                ));
                        END IF;
                    END $$
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_profile_graph_audit_actor "
                    "ON profile_graph_audit (actor_fp, created_at DESC)"
                )
                # `revision` 하한 조회용 — 문서가 손상돼 get_graph 가 None 을 돌려줘도 revision 이
                # 0 으로 되돌아가면 안 된다(REQ-PGRAPH-042). 감사는 초기화로도 보존되므로 여기가
                # 항상 존재하는 하한이다.
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_profile_graph_audit_version "
                    "ON profile_graph_audit (actor_fp, graph_version_after)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_profile_graph_audit_created "
                    "ON profile_graph_audit (created_at)"
                )
                # 멱등 원장 — 파생 키가 자연 키다(REQ-PGRAPH-043).
                # `{userId}` 는 **원문**을 쓴다: 지문화하면 pepper 회전 시 TTL 내 모든 원장 히트가
                # 증발해 재전송이 중복 부작용을 낸다. raw userId 금지는 감사 쪽 조항이다.
                # `response_payload` 에는 **라벨을 담지 않는다** — 재생 시 라벨은 현재 그래프
                # 문서에서 다시 읽어 조립한다.
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS profile_graph_idempotency (
                        derived_key text PRIMARY KEY,
                        user_id bigint NOT NULL,
                        scope_id text,
                        request_fp text,
                        status text NOT NULL DEFAULT 'processing',
                        claim_token text,
                        lease_expires_at timestamptz,
                        response_payload jsonb,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                await conn.execute(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'profile_graph_idempotency_status_check'
                              AND conrelid = 'profile_graph_idempotency'::regclass
                        ) THEN
                            ALTER TABLE profile_graph_idempotency
                                ADD CONSTRAINT profile_graph_idempotency_status_check
                                CHECK (status IN ('processing', 'completed'));
                        END IF;
                    END $$
                    """
                )
                # 크래시 잔재 재선점용 — `processing` 인데 lease 가 만료된 행만 훑는다.
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_profile_graph_idem_lease "
                    "ON profile_graph_idempotency (lease_expires_at) WHERE status = 'processing'"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_profile_graph_idem_scope "
                    "ON profile_graph_idempotency (user_id, scope_id)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_profile_graph_idem_created "
                    "ON profile_graph_idempotency (created_at)"
                )
                # 개인화 중지 — **전용 저장 위치여야 한다**(REQ-PGRAPH-050). 요약 항목에 두면
                # 전체 초기화가 그것을 지워 중지가 조용히 풀리고, 그래프 문서에 두면 지연
                # 크리티컬 reader 가 조회를 두 번 해야 해 REQ-PROF-001(단일 get)이 깨진다.
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS profile_personalization_state (
                        user_id bigint PRIMARY KEY,
                        enabled boolean NOT NULL DEFAULT true,
                        disabled_at timestamptz,
                        graph_version text,
                        updated_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )

    await asyncio.wait_for(_run(), timeout=settings.state_store_migration_timeout_s)


# ── 멱등 원장 (REQ-PGRAPH-043) ────────────────────────────────────────────────
#
# `processed_events` 의 claim/lease 와 같은 상태 기계다: 신규 선점과 **만료 lease 재선점**을
# 한 문장으로 처리하고, `completed` 행은 절대 재선점되지 않는다(재선점되면 부작용이 2회).
# 다른 점은 완료 시 **응답 payload 를 함께 적는다**는 것 — 재전송이 최초 응답을 그대로 받아야
# 하기 때문이다. payload 에는 라벨을 담지 않는다(모듈 docstring 참조).


async def claim(
    key: str,
    *,
    user_id: int,
    scope_id: str | None,
    lease_s: float,
    request_fp: str | None = None,
) -> str | None:
    """이 요청의 실행권을 선점한다 — 이미 진행 중이거나 완료됐으면 `None`.

    `None` 은 "실패"가 아니라 **분기 신호**다. 호출부는 `lookup()` 으로 `completed` 면 최초
    응답을 재생하고, `processing` 이면 다른 워커가 진행 중이라고 판정한다.
    """
    if lease_s < 0:
        raise ValueError("lease_s must be non-negative")
    token = uuid.uuid4().hex
    pool = await _get_pool()
    if pool is None:
        return _fallback_claim(
            key,
            user_id=user_id,
            scope_id=scope_id,
            lease_s=lease_s,
            token=token,
            request_fp=request_fp,
        )

    async def _run() -> str | None:
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO profile_graph_idempotency
                    (derived_key, user_id, scope_id, request_fp,
                     status, claim_token, lease_expires_at, updated_at)
                VALUES (%s, %s, %s, %s, 'processing', %s,
                        now() + make_interval(secs => %s), now())
                ON CONFLICT (derived_key) DO UPDATE
                SET status = 'processing',
                    claim_token = EXCLUDED.claim_token,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    request_fp = EXCLUDED.request_fp,
                    updated_at = now()
                WHERE profile_graph_idempotency.status = 'processing'
                  AND (profile_graph_idempotency.lease_expires_at IS NULL
                       OR profile_graph_idempotency.lease_expires_at <= now())
                RETURNING claim_token
                """,
                (key, int(user_id), scope_id, request_fp, token, float(lease_s)),
            )
            row = await cur.fetchone()
            return row[0] if row else None

    return await run_with_query_timeout(_run())


def _fallback_claim(
    key: str,
    *,
    user_id: int,
    scope_id: str | None,
    lease_s: float,
    token: str,
    request_fp: str | None,
) -> str | None:
    entries = _fallback_table("idempotency")
    entry = entries.get(key)
    if entry is not None:
        if entry.status == "completed":
            return None
        if entry.lease_deadline is not None and entry.lease_deadline > _monotonic():
            return None  # 진행 중이고 lease 가 살아 있다
    entries[key] = _FallbackEntry(
        user_id=int(user_id),
        scope_id=scope_id,
        status="processing",
        claim_token=token,
        lease_deadline=_monotonic() + lease_s,
        request_fp=request_fp,
        created_at=entry.created_at if entry is not None else _monotonic(),
    )
    return token


async def complete(key: str, token: str, response_payload: dict) -> bool:
    """실행권을 완료로 바꾸고 최초 응답을 적는다 — 토큰이 낡았으면 `False`.

    재선점된 뒤 늦게 깨어난 원래 주인이 남의 작업을 완료 처리하면, 그 워커가 실제로 만든
    상태와 원장에 적힌 응답이 어긋난다. 그래서 토큰 일치를 조건에 건다.
    """
    pool = await _get_pool()
    if pool is None:
        entries = _fallback_table("idempotency")
        entry = entries.get(key)
        if entry is None or entry.status != "processing" or entry.claim_token != token:
            return False
        entry.status = "completed"
        entry.claim_token = None
        entry.lease_deadline = None
        entry.response_payload = dict(response_payload)
        return True

    async def _run() -> bool:
        async with pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE profile_graph_idempotency
                SET status = 'completed',
                    claim_token = NULL,
                    lease_expires_at = NULL,
                    response_payload = %s,
                    updated_at = now()
                WHERE derived_key = %s AND status = 'processing' AND claim_token = %s
                RETURNING derived_key
                """,
                (Jsonb(response_payload), key, token),
            )
            return await cur.fetchone() is not None

    return await run_with_query_timeout(_run())


async def release(key: str, token: str) -> bool:
    """선점을 **흔적 없이** 되돌린다 — 상태를 바꾸지 않은 요청의 롤백.

    저널을 선행 기록하는 설계라 `404`·`409`·no-op 판정이 claim 뒤에 온다. 그때 행을 남겨두면
    (a) REQ-PGRAPH-080("상태를 바꾸지 않는 요청은 감사 행을 남기지 않는다")을 어기고
    (b) 조건이 바뀐 뒤의 정당한 재시도가 "진행 중"으로 막힌다. 토큰 일치를 조건에 걸어
    재선점된 남의 작업을 지우지 않는다.
    """
    pool = await _get_pool()
    if pool is None:
        entries = _fallback_table("idempotency")
        entry = entries.get(key)
        if entry is None or entry.status != "processing" or entry.claim_token != token:
            return False
        del entries[key]
        return True

    async def _run() -> bool:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM profile_graph_idempotency "
                "WHERE derived_key = %s AND status = 'processing' AND claim_token = %s "
                "RETURNING derived_key",
                (key, token),
            )
            return await cur.fetchone() is not None

    return await run_with_query_timeout(_run())


async def lookup(
    key: str, *, request_fp: str | None = None, ttl_h: float | None = None
) -> LedgerEntry | None:
    """원장을 조회한다 — TTL 이 지났으면 미스다.

    TTL 경과분을 미스로 돌리면 그 뒤 재전송은 `revision` CAS 로 판정되어 최악 `409` 다.
    정확성은 CAS 가 담보하므로 안전한 방향의 degrade 다.

    `request_fp` 를 주면 같은 키의 **다른 본문**을 걸러 `LedgerRequestMismatch` 를 던진다.
    """
    ttl = get_settings().graph_idempotency_ttl_h if ttl_h is None else ttl_h
    pool = await _get_pool()
    if pool is None:
        entry = _fallback_table("idempotency").get(key)
        if entry is None or _monotonic() - entry.created_at >= ttl * 3600:
            return None
        found = LedgerEntry(entry.status, entry.response_payload, entry.request_fp)
    else:

        async def _run() -> LedgerEntry | None:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT status, response_payload, request_fp "
                    "FROM profile_graph_idempotency "
                    "WHERE derived_key = %s "
                    "  AND created_at > now() - make_interval(secs => %s)",
                    (key, float(ttl) * 3600.0),
                )
                row = await cur.fetchone()
                return LedgerEntry(row[0], row[1], row[2]) if row else None

        found = await run_with_query_timeout(_run())
        if found is None:
            return None

    if request_fp is not None and found.request_fp is not None and found.request_fp != request_fp:
        raise LedgerRequestMismatch(key)
    return found


def _fallback_table(name: str) -> dict:
    """폴백 상태 접근 — `_get_pool()` 이 이미 폴백을 꽂아 둔 뒤에만 부른다."""
    global _fallback
    if _fallback is None:
        _fallback = _blank_fallback()
    return _fallback[name]


# ── 변경 감사 (REQ-PGRAPH-080~082) ────────────────────────────────────────────
#
# 남기는 것은 *무엇을* 지웠는지가 아니라 *언제·누가(지문)* 지웠는지다. 전체 초기화가 모든
# 개인화 데이터를 지워도 이 테이블만은 남으므로(REQ-PGRAPH-062), 여기에 원문이 있으면
# "삭제했다"는 약속 자체가 거짓이 된다.
#
# **호출 시점이 계약이다** — 감사 행은 문서 쓰기가 성공한 **뒤에만** 만든다. 상태를 바꾸지
# 않은 요청(404·409·no-op·재전송)은 남기지 않는다(REQ-PGRAPH-080).


@dataclass(frozen=True)
class AuditRecord:
    """`GraphAuditRecord`(SPEC §5.4)의 저장 형태. **[HARD] 와이어에 노출하지 않는다.**"""

    request_id: str
    actor_fp: str
    action: str
    graph_version_before: str
    graph_version_after: str
    edge_id_before: str | None = None
    edge_id_after: str | None = None
    predicate: str | None = None
    object_fp: str | None = None


def _fingerprint(value: str | None) -> str | None:
    """peppered HMAC — 값이 없으면 지문도 없다.

    빈 값을 지문화하면 "대상 없음"(전체 초기화·중지 토글)과 "라벨이 빈 문자열인 대상"이 같은
    값이 되어 감사에서 둘을 구분할 수 없다.
    """
    if not value:
        return None
    return identifier_fingerprint(value)


async def record_audit(
    *,
    user_id: int,
    request_id: str,
    action: str,
    graph_version_before: str,
    graph_version_after: str,
    edge_id_before: str | None = None,
    edge_id_after: str | None = None,
    predicate: str | None = None,
    object_label: str | None = None,
) -> None:
    """변경 감사 1행을 남긴다 — 주체·대상은 지문으로만.

    `object_label` 은 **여기서 즉시 지문화되고 원문은 어디에도 저장되지 않는다.** 호출부가
    라벨을 넘기는 이유는 지문을 만들기 위해서지 기록하기 위해서가 아니다.
    """
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unknown graph audit action: {action}")

    record = AuditRecord(
        request_id=request_id,
        # raw userId 금지(REQ-PGRAPH-081) — 로그·관측 경로와 같은 지문 함수를 쓴다.
        actor_fp=identifier_fingerprint(str(user_id)) or "",
        action=action,
        graph_version_before=graph_version_before,
        graph_version_after=graph_version_after,
        edge_id_before=edge_id_before,
        edge_id_after=edge_id_after,
        predicate=predicate,
        object_fp=_fingerprint(object_label),
    )

    pool = await _get_pool()
    if pool is None:
        _fallback_table("audit").setdefault(record.actor_fp, []).append(record)
        return

    async def _run() -> None:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO profile_graph_audit
                    (request_id, actor_fp, action, edge_id_before, edge_id_after,
                     predicate, object_fp, graph_version_before, graph_version_after)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.request_id,
                    record.actor_fp,
                    record.action,
                    record.edge_id_before,
                    record.edge_id_after,
                    record.predicate,
                    record.object_fp,
                    record.graph_version_before,
                    record.graph_version_after,
                ),
            )

    await run_with_query_timeout(_run())


async def list_audit(*, user_id: int) -> list[AuditRecord]:
    """한 사용자의 감사 행을 오래된 순으로 — 조회 대상은 **지문**이다.

    `actor_fp` 로 찾으므로 pepper 가 회전하면 과거 행을 못 찾는다. 감사는 사후 대조용이고
    재전송 판정처럼 정확성이 걸린 경로가 아니라 그 트레이드오프를 받는다(원장이 raw userId 를
    쓰는 이유가 그 반대편이다 — `derived_key` 주석 참조).
    """
    actor_fp = identifier_fingerprint(str(user_id)) or ""
    pool = await _get_pool()
    if pool is None:
        return list(_fallback_table("audit").get(actor_fp, []))

    async def _run() -> list[AuditRecord]:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT request_id, actor_fp, action, graph_version_before, graph_version_after, "
                "       edge_id_before, edge_id_after, predicate, object_fp "
                "FROM profile_graph_audit WHERE actor_fp = %s ORDER BY id",
                (actor_fp,),
            )
            return [AuditRecord(*row) for row in await cur.fetchall()]

    return await run_with_query_timeout(_run())


# ── 조립 — 잠금 · 저널 · CAS · 감사 · 롤백 (§7.2) ──────────────────────────────
#
# 순서 자체가 계약이다.
#
#   1. per-user 그래프 잠금
#   2. 문서 읽기 → 대상 부재면 404 (**원장·감사를 건드리기 전에**)
#   3. 원장 claim → 실패하면 재전송 분기(completed 면 최초 응답 재생)
#   4. `revision` CAS → 불일치면 claim 해제 후 409 + 최신 버전
#   5. 순수 변형 → `changed=False` 면 claim 해제 후 no-op 응답
#   6. 문서 쓰기
#   7. 감사 기록 (**여기서 처음 생긴다**)
#   8. 원장 완료
#
# 404·409·no-op 판정이 감사보다 앞이고 롤백이 붙는 이유는 REQ-PGRAPH-080 이다 — 상태를 바꾸지
# 않은 요청은 감사 행을 남기지 않는다. 감사·원장 완료가 문서 쓰기보다 뒤인 이유는 그 반대다 —
# 문서가 안 바뀌었는데 기록만 남으면 감사가 거짓이 되고 원장이 일어나지 않은 일을 재생한다.


@dataclass(frozen=True)
class GraphMutationResult:
    """#360 이 와이어로 옮길 최소 결과. 라벨은 없다 — 필요하면 현재 문서에서 다시 읽는다."""

    graph_version: str
    replayed: bool = False
    edge_id: str | None = None
    merged: bool = False
    suppressed: bool = False


def graph_version(revision: int) -> str:
    """와이어 버전 토큰. **[HARD] 불투명** — 클라이언트는 파싱·순서 비교를 하지 않는다(§5.3)."""
    return f"g{revision}"


async def apply_edge_mutation(
    *,
    user_id: int,
    action: str,
    edge_id: str,
    if_match: str,
    request_id: str,
    now: str,
    predicate: str | None = None,
    node: GraphNode | None = None,
    lease_s: float | None = None,
) -> GraphMutationResult:
    """edge 수정·삭제를 저널·CAS·멱등 아래에서 적용한다.

    `GraphVersionConflict`·`GraphEdgeNotFound`·`GraphStoreUnavailable` 을 던진다 — 상태 코드
    매핑은 호출자가 생기는 #360 몫이다(`graph_errors` docstring).
    """
    if action not in ("edgeUpdate", "edgeSuppress"):
        raise ValueError(f"unsupported edge mutation action: {action}")

    # 지연 임포트로 순환을 끊는다 — `store` 는 테스트 격리를 위해 이 모듈의 `reset()` 을 부르고
    # (`reset_profile_store`), 여기는 그 저장소를 쓴다. 모듈 최상단에서 서로를 부르면 import 가
    # 반쪽 초기화 상태에서 터진다.
    from app.agents.profile.store import get_profile_store

    store = await get_profile_store()
    settings = get_settings()
    key = derived_key(action, user_id, edge_id, if_match)
    # 파생 키에는 **요청 본문이 들어가지 않는다**(REQ-PGRAPH-043). 그래서 스테일한 `If-Match` 를
    # 든 다른 본문이 같은 키를 만들 수 있고, 그대로 재생하면 호출자가 보내지도 않은 변경의
    # 결과를 성공으로 받는다. 본문 지문을 함께 실어 그 경우를 충돌로 떨어뜨린다.
    request_fp = _request_fingerprint(action, predicate, node)
    ttl = settings.session_end_claim_ttl_s if lease_s is None else lease_s

    async with store.graph_lock(str(user_id)):
        try:
            document = await store.get_graph(str(user_id))

            # **재전송 판정이 404 판정보다 앞이다.** 삭제가 성공하면 그 edge 는 문서에서 사라지므로,
            # 순서를 뒤집으면 정상적인 네트워크 재시도가 전부 `404` 를 받는다 — 실제로는 성공했는데
            # 호출자에게는 실패로 보인다(REQ-PGRAPH-043 의 "원장이 없으면 재시도가 틀린 답을
            # 받는다"가 가리키는 그 상황이다).
            try:
                replayed = await _replay_if_completed(key, document, request_fp=request_fp)
            except LedgerRequestMismatch as exc:
                # 같은 키·다른 본문 — 재생하면 호출자가 보내지도 않은 변경의 결과를 성공으로
                # 받는다. 스테일한 선행조건으로 온 것이므로 충돌이 맞는 답이다.
                raise GraphVersionConflict(
                    graph_version(document.revision) if document else ""
                ) from exc
            if replayed is not None:
                return replayed

            if document is None or not any(e.edge_id == edge_id for e in document.edges):
                # 원장·감사를 건드리기 전에 끝낸다 — 진행 중 표식조차 만들지 않아야 그 사용자의
                # 다음 정당한 요청이 막히지 않는다.
                raise GraphEdgeNotFound(edge_id)

            token = await claim(
                key, user_id=user_id, scope_id=edge_id, lease_s=ttl, request_fp=request_fp
            )
            if token is None:
                # 완료분은 위에서 걸렀으니 여기 오는 것은 **진행 중**뿐이다 — 다른 워커가 같은
                # 요청을 들고 있다. "진행 중"이라는 상태를 와이어에 만들지 않기 위해(§2.5 에
                # 그런 코드가 없다) 최신 버전으로 충돌을 알린다.
                raise GraphVersionConflict(graph_version(document.revision))

            return await _apply_claimed(
                store=store,
                document=document,
                user_id=user_id,
                action=action,
                edge_id=edge_id,
                if_match=if_match,
                request_id=request_id,
                now=now,
                predicate=predicate,
                node=node,
                key=key,
                token=token,
                settings=settings,
            )
        except GraphMutationError:
            raise
        except Exception as exc:
            if is_state_store_unavailable(exc):
                # 문서 무손상은 예외가 아니라 **쓰기 순서**로 보장된다 — 문서 쓰기 전에 죽으면
                # 아무것도 안 바뀌었고, 쓴 뒤에 죽으면 원장 `processing` 잔재가 남아 lease
                # 재선점으로 재개된다.
                raise GraphStoreUnavailable(str(exc)) from exc
            raise


def _request_fingerprint(action: str, predicate: str | None, node: GraphNode | None) -> str | None:
    """요청 본문의 지문 — 같은 파생 키·다른 본문을 가르는 재료.

    `edgeSuppress` 는 본문이 없어(경로에 신원과 대상이 전부 있다) `None` 이다 — 같은 키면 같은
    요청이 맞다. `edgeUpdate` 만 `(predicate, nodeId)` 로 지문을 만든다. 라벨이 아니라
    `node_id` 를 쓰는 것은 그것이 이미 정규화된 값이라 표기 변형이 다른 지문을 만들지 않기
    때문이다.
    """
    if action != "edgeUpdate" or predicate is None or node is None:
        return None
    return identifier_fingerprint(f"{predicate}|{node.node_id}")


async def _replay_if_completed(
    key: str, document: GraphDocument | None, *, request_fp: str | None = None
) -> GraphMutationResult | None:
    """이미 완료된 같은 요청이면 최초 응답을 그대로 돌려준다 (REQ-PGRAPH-043).

    `None` 이면 재전송이 아니다 — 호출부가 정상 경로로 진행한다. TTL 이 지난 원장은 미스라
    여기서도 `None` 이고, 그 뒤 판정은 `revision` CAS 가 맡는다(안전한 방향의 degrade).
    """
    # 본문 지문이 다르면 `LedgerRequestMismatch` 가 올라온다 — 호출부가 409 로 옮긴다.
    hit = await lookup(key, request_fp=request_fp)
    if hit is None or hit.status != "completed" or not hit.response_payload:
        return None
    payload = hit.response_payload
    fallback = graph_version(document.revision) if document else payload.get("graphVersion", "")
    return GraphMutationResult(
        graph_version=payload.get("graphVersion", fallback),
        replayed=True,
        edge_id=payload.get("edgeId"),
        merged=bool(payload.get("merged")),
        suppressed=bool(payload.get("suppressed")),
    )


async def _apply_claimed(
    *,
    store,
    document: GraphDocument,
    user_id: int,
    action: str,
    edge_id: str,
    if_match: str,
    request_id: str,
    now: str,
    predicate: str | None,
    node: GraphNode | None,
    key: str,
    token: str,
    settings,
) -> GraphMutationResult:
    """claim 을 쥔 상태의 본체 — 실패 경로마다 반드시 claim 을 푼다."""
    before = graph_version(document.revision)
    if normalize_if_match(if_match) != before:
        await release(key, token)
        raise GraphVersionConflict(before)

    if action == "edgeSuppress":
        updated, outcome = apply_suppression(document, edge_id=edge_id, now=now)
    else:
        if predicate is None or node is None:
            await release(key, token)
            raise ValueError("edgeUpdate requires predicate and node")
        updated, outcome = apply_correction(
            document, edge_id=edge_id, predicate=predicate, node=node, now=now, settings=settings
        )

    if not outcome.changed:
        # 상태를 바꾸지 않았다 — 감사 행도 revision 도 남기지 않고 claim 을 되돌린다.
        # **되돌리지 않으면 뒤따르는 진짜 변경이 막힌다** — 파생 키에 본문이 없어서 같은
        # `If-Match` 로 온 다른 수정이 같은 키를 쓰기 때문이다(테스트로 고정).
        await release(key, token)
        return GraphMutationResult(graph_version=before, replayed=True, edge_id=edge_id)

    after = graph_version(document.revision + 1)
    await store.set_graph(
        str(user_id), updated.model_copy(update={"revision": document.revision + 1})
    )

    # 문서 쓰기가 성공한 **뒤에만** 기록한다.
    await record_audit(
        user_id=user_id,
        request_id=request_id,
        action=action,
        graph_version_before=before,
        graph_version_after=after,
        edge_id_before=outcome.edge_id_before,
        edge_id_after=outcome.edge_id_after,
        predicate=outcome.predicate,
        object_label=outcome.object_label,
    )
    payload = {
        "graphVersion": after,
        "edgeId": outcome.edge_id_after,
        "merged": outcome.merged,
        "suppressed": action == "edgeSuppress",
    }
    await complete(key, token, payload)
    return GraphMutationResult(
        graph_version=after,
        replayed=False,
        edge_id=outcome.edge_id_after,
        merged=outcome.merged,
        suppressed=action == "edgeSuppress",
    )
