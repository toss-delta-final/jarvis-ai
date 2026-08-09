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

from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings
from app.core.pg_resilience import hardened_pg_conninfo

logger = logging.getLogger(__name__)

# DDL 직렬화 키 — 세 테이블을 한 트랜잭션에서 만들므로 잠금도 하나다.
SCHEMA_LOCK_KEY = "schema:profile_graph:lifecycle"

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
