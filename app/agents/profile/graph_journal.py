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
    GraphEdgeNotEditable,
    GraphEdgeNotFound,
    GraphMutationError,
    GraphObjectUnknown,
    GraphStoreUnavailable,
    GraphVersionConflict,
)
from app.agents.profile.graph_models import (
    GraphDocument,
    GraphEdge,
    is_projected,
    normalize_label,
)
from app.agents.profile.graph_mutations import (
    MutationOutcome,
    apply_correction,
    apply_suppression,
)
from app.agents.profile.resolver import ObjectSpec, resolve_user_object
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
GraphAuditAction = Literal["edgeUpdate", "edgeDelete", "graphReset", "personalizationToggle"]
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

    `{scopeId}`: edge 2종 = `edgeId`, `graphReset` = `"ALL"`,
    `personalizationToggle` = **대상 상태**(`"true"`/`"false"`, #360 — 구 계약은 빈 값이었다).
    토글은 그래프 문서를 안 바꿔 `graphVersion` 이 고정이라, scope 를 비우면 끄기와 켜기가 같은
    키가 되어 **재개가 중지 응답을 재생**한다(원장 TTL 동안 프라이버시 스위치가 잠긴다).
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


async def warm_pool() -> None:
    """기동 시 풀·스키마를 미리 준비한다 — 첫 사용자 턴이 그 비용을 물지 않도록 (#359).

    `close_pool` 과 대칭이며 `app.main._lifespan` 이 부른다. 지금까지 이 풀은 열리는 자리가 없어
    **첫 호출자가 지연 초기화 전체를 지불**했는데, 그 첫 호출자가 항상 백그라운드 sweep(사용자
    예산 밖)이라 무해했다. #359 의 중지 게이트가 **구매자 턴**을 첫 호출자로 바꾼다 —
    `pool.open(wait=True)`(연결 5s) + `_ensure_schema`(마이그레이션 30s, advisory 잠금 + 테이블
    3개 + 인덱스)가 `_init_lock` 안에서 직렬화되므로, 배포 직후 첫 회원 턴이 first-token 10s
    관문 안에서 그것을 물고 동시 턴까지 함께 막는다.

    **실패해도 기동을 막지 않는다** — 호출부가 잡는다. 워밍은 최적화이지 선결 조건이 아니고,
    실패하면 지연 초기화가 원래 하던 일을 그대로 한다(운영에서 pg-profile 이 죽어 있으면
    `_get_pool` 이 raise 하는데, 그걸 기동 실패로 승격시키면 개인화와 무관한 구매자 턴까지 서버가
    안 뜬다).
    """
    await _get_pool()


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
                        mutation_fp text,
                        created_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                # 기존 볼륨에도 컬럼을 더한다(빈 볼륨만 위 CREATE 가 담당한다).
                await conn.execute(
                    "ALTER TABLE profile_graph_audit ADD COLUMN IF NOT EXISTS mutation_fp text"
                )
                # **감사 쓰기를 멱등으로 만든다.** 크래시 재개가 "감사는 썼는데 완료 표시 전에
                # 끊긴" 지점에서 다시 시작하면 같은 변경이 두 행이 되고, 그건 REQ-PGRAPH-080
                # ("모든 상태 변경은 감사 행을 남긴다" = 한 변경 한 행)을 깬다. 파생 키가 그
                # 변경의 자연 키이므로 그걸로 막는다. 부분 인덱스인 이유는 구 행의
                # `derived_key` 가 NULL 이라 전체 UNIQUE 면 하나만 남기 때문이다.
                await conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_graph_audit_mutation "
                    "ON profile_graph_audit (mutation_fp) WHERE mutation_fp IS NOT NULL"
                )
                # **어휘가 바뀌면 제약과 기존 행이 함께 이행돼야 한다.**
                #
                # `IF NOT EXISTS` 로만 두면 이름이 같은 낡은 CHECK 가 기존 볼륨에 남아, 새
                # 어휘(`edgeSuppress`→`edgeDelete`, #499)의 INSERT 가 **운영에서만** 거부된다.
                # 그렇다고 제약만 갈면 이번엔 옛 값이 든 행 때문에 `ADD CONSTRAINT` 검증이
                # 실패해 스키마 초기화 전체가 죽는다 — 둘 다 통합 테스트가 실제로 잡았다.
                #
                # 그래서 **제약 해제 → 행 이행 → 제약 재생성** 순서다. 순서가 중요하다: 낡은
                # CHECK 를 그대로 둔 채 UPDATE 하면 새 값이 그 CHECK 에 걸려 거부된다
                # (`new row ... violates check constraint` — 실제로 밟았다).
                #
                # 이 UPDATE 는 감사 내용을 바꾸는 것이 아니라 같은 사건의 이름을 새 어휘로 옮기는
                # 것이라 REQ-PGRAPH-062(감사 보존)와 어긋나지 않는다. 값이 이미 새 어휘면 0행이라
                # 재실행이 안전하다.
                await conn.execute(
                    "ALTER TABLE profile_graph_audit "
                    "DROP CONSTRAINT IF EXISTS profile_graph_audit_action_check"
                )
                await conn.execute(
                    "UPDATE profile_graph_audit SET action = 'edgeDelete' "
                    "WHERE action = 'edgeSuppress'"
                )
                await conn.execute(
                    "ALTER TABLE profile_graph_audit "
                    "ADD CONSTRAINT profile_graph_audit_action_check "
                    "CHECK (action IN "
                    "('edgeUpdate', 'edgeDelete', 'graphReset', 'personalizationToggle'))"
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
                        disabled_spans jsonb NOT NULL DEFAULT '[]'::jsonb,
                        graph_version text,
                        updated_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                # 구 볼륨에는 컬럼이 없다 — `CREATE TABLE IF NOT EXISTS` 는 기존 테이블을
                # 갱신하지 않으므로 추가를 따로 건다(이 파일이 마이그레이션 정본이다).
                await conn.execute(
                    """
                    ALTER TABLE profile_personalization_state
                        ADD COLUMN IF NOT EXISTS disabled_spans jsonb NOT NULL DEFAULT '[]'::jsonb
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
    takeover: bool = False,
) -> str | None:
    """이 요청의 실행권을 선점한다 — 이미 진행 중이거나 완료됐으면 `None`.

    `None` 은 "실패"가 아니라 **분기 신호**다. 호출부는 `lookup()` 으로 `completed` 면 최초
    응답을 재생하고, `processing` 이면 다른 워커가 진행 중이라고 판정한다.

    `takeover=True` 는 **lease 가 아직 살아 있어도** `processing` 을 재선점한다. 호출부가
    그래프 락을 쥐고 있을 때만 쓴다 — 그 락(`mutation_lock`, 실 pg 에서는 advisory lock)이
    사용자당 그래프 변경의 **진짜** 상호배제라, 그것을 쥔 채 `processing` 잔재를 봤다면 원래
    주인은 지금 돌고 있지 않다(크래시했거나 예외로 빠져나갔다). 그 상황에서 lease 만료를
    기다리면 재시도가 최대 TTL 동안 틀린 답을 받는다(PR #540 리뷰). `completed` 는 여전히
    재선점하지 않는다 — 그건 부작용 2회다.
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
            takeover=takeover,
        )

    # `takeover` 면 lease 만료 조건만 뺀다 — `status='processing'` 조건은 그대로다(완료분은
    # 절대 재선점하지 않는다).
    lease_clause = (
        ""
        if takeover
        else "AND (profile_graph_idempotency.lease_expires_at IS NULL "
        "OR profile_graph_idempotency.lease_expires_at <= now())"
    )

    async def _run() -> str | None:
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"""
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
                  {lease_clause}
                RETURNING claim_token
                """,  # noqa: S608 - lease_clause 는 위에서 만든 리터럴 두 개 중 하나다
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
    takeover: bool = False,
) -> str | None:
    entries = _fallback_table("idempotency")
    entry = entries.get(key)
    if entry is not None:
        if entry.status == "completed":
            return None
        if (
            not takeover
            and entry.lease_deadline is not None
            and entry.lease_deadline > _monotonic()
        ):
            return None  # 진행 중이고 lease 가 살아 있다
    entries[key] = _FallbackEntry(
        user_id=int(user_id),
        scope_id=scope_id,
        status="processing",
        claim_token=token,
        lease_deadline=_monotonic() + lease_s,
        # 의도(payload)는 재선점해도 **보존한다** — 크래시 재개가 그걸로 응답을 재구성한다.
        response_payload=entry.response_payload if entry is not None else None,
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


async def record_intent(key: str, token: str, payload: dict) -> bool:
    """문서를 쓰기 **직전에** "무엇을 하려는지"를 원장에 적는다 (§7.2 저널 선행 기록).

    이게 크래시 재개의 근거다. 문서 쓰기와 완료 표시는 서로 다른 저장소라 한 트랜잭션에 못
    묶이므로(§7.1 다중 항목 원자성 없음), 그 사이에 끊기면 "이 요청이 무엇을 만들려 했는가"를
    알 방법이 없어진다 — 재개가 응답을 재구성하지 못하고, 재시도는 `404`/`409` 를 받는다.

    상태는 `processing` 그대로 두고 payload 만 채운다. 완료는 `complete()` 가 표시한다.
    """
    pool = await _get_pool()
    if pool is None:
        entry = _fallback_table("idempotency").get(key)
        if entry is None or entry.status != "processing" or entry.claim_token != token:
            return False
        entry.response_payload = dict(payload)
        return True

    async def _run() -> bool:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE profile_graph_idempotency SET response_payload = %s, updated_at = now() "
                "WHERE derived_key = %s AND status = 'processing' AND claim_token = %s "
                "RETURNING derived_key",
                (Jsonb(payload), key, token),
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
    # 이 변경의 자연 키 **지문** — 감사 쓰기를 멱등으로 만든다(크래시 재개가 두 행을 남기지
    # 않게). 파생 키 원문을 그대로 두지 않는 이유는 그 안에 `userId` 원문이 들어 있기
    # 때문이다(REQ-PGRAPH-043 규약) — 감사에는 raw userId 를 남길 수 없다(REQ-PGRAPH-081).
    # 유일성 판정에만 쓰이므로 되돌릴 수 없는 지문으로 충분하다.
    mutation_fp: str | None = None


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
    mutation_key: str | None = None,
) -> None:
    """변경 감사 1행을 남긴다 — 주체·대상은 지문으로만.

    `object_label` 은 **여기서 즉시 지문화되고 원문은 어디에도 저장되지 않는다.** 호출부가
    라벨을 넘기는 이유는 지문을 만들기 위해서지 기록하기 위해서가 아니다.

    `mutation_key`(파생 키)를 주면 **멱등**이다 — 같은 변경의 두 번째 쓰기는 무시된다. 크래시
    재개가 "감사는 썼는데 완료 표시 전에 끊긴" 지점에서 다시 시작할 수 있으므로 필요하다.
    없으면 한 변경이 두 행이 되어 REQ-PGRAPH-080("한 변경 = 한 감사 행")을 깬다.
    파생 키는 **지문으로 바꿔** 저장한다 — 그 안에 `userId` 원문이 들어 있고 감사에는 남길 수
    없다(REQ-PGRAPH-081). 유일성 판정에만 쓰이므로 되돌릴 수 없는 값으로 충분하다.
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
        mutation_fp=_fingerprint(mutation_key),
    )

    pool = await _get_pool()
    if pool is None:
        rows = _fallback_table("audit").setdefault(record.actor_fp, [])
        if record.mutation_fp is not None and any(
            existing.mutation_fp == record.mutation_fp for existing in rows
        ):
            return  # 실 pg 의 UNIQUE 와 같은 판정 — 두 경로가 갈리면 안 된다
        rows.append(record)
        return

    async def _run() -> None:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO profile_graph_audit
                    (request_id, actor_fp, action, edge_id_before, edge_id_after,
                     predicate, object_fp, graph_version_before, graph_version_after,
                     mutation_fp)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (mutation_fp) WHERE mutation_fp IS NOT NULL DO NOTHING
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
                    record.mutation_fp,
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
    # I-36 전용. 라벨이 아니라 **개수만**이라 여기 실어도 노출 경계를 넘지 않는다
    # (api-spec §3.9.4 `purged` — 정확히 `{edges, transcriptTurns}` 2키).
    purged: dict[str, int] | None = None
    # I-33 전용. **투영된 edge 를 원장이 든다** — 재전송은 "최초 응답 본문을 그대로"여야 하는데
    # (REQ-PGRAPH-043), 그 사이 그 edge 가 삭제됐으면 현재 문서로는 재구성할 수 없다. 라벨을
    # 원장에 두는 것은 #358 이 테이블 3개를 나눌 때 이미 전제한 것이고(감사와 한 행에 둘 수
    # 없는 이유가 바로 그 라벨이었다), REQ-PGRAPH-081 의 라벨 금지는 **감사 테이블** 조항이다.
    edge: dict | None = None


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
    object_spec: ObjectSpec | None = None,
    lease_s: float | None = None,
) -> GraphMutationResult:
    """edge 수정·삭제를 저널·CAS·멱등 아래에서 적용한다.

    `GraphVersionConflict`·`GraphEdgeNotFound`·`GraphEdgeNotEditable`·`GraphObjectUnknown`·
    `GraphStoreUnavailable` 을 던진다 — 상태 코드 매핑은 `app/core/errors.py` 가 소유한다.

    **[#360] `edgeUpdate` 는 `object_spec` 을 받는다** — 확정된 노드가 아니라 **요청 원문**이다.
    `predicate`·`object` 중 생략한 쪽은 대상 edge 의 현재 값으로 채우는데(api-spec §3.9.1),
    그 출처가 **잠금 아래에서 읽은 문서**여야 하기 때문이다. 미리 읽어 채우면 그 사이 값이
    바뀌었을 때 사용자가 보내지 않은 변경이 적용되고, 재전송이 이미 사라진 edge 를 찾다 실패해
    §7.2 의 *"재전송 판정이 `404` 판정보다 앞"* 이 깨진다.
    """
    if action not in ("edgeUpdate", "edgeDelete"):
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
    request_fp = _request_fingerprint(action, predicate, object_spec)
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

            # **404 판정은 원장을 보고 한다** (api-spec §3.9.2, PR #540 리뷰).
            #
            # 원장에 이 파생 키의 행이 있으면 그 요청은 **이미 접수돼 적어도 일부가 적용된**
            # 것이다. 문서 쓰기는 됐는데 감사·완료 전에 끊긴 창(평범한 pg 일시 장애로도 열린다)
            # 에서는 대상 edge 가 이미 사라져 있는데, 그걸 "없으니 404"로 판정하면 api-spec 이
            # ⚠️ 로 금지한 **"edge 존재 여부로 뭉뚱그리기"** 를 그대로 하는 것이다. 게다가 이
            # 판정이 원장을 안 보면 TTL 이 지나도 같은 404 라 **자가 복구도 안 된다.**
            #
            # **대상 판정은 조회의 노출 경계와 같다**(`is_projected`, PR #562 리뷰). `edge_id` 는
            # `sha256("{predicate}|{node_id}")` 라 사용자별 salt 가 없는 콘텐츠 해시여서 라벨만
            # 알면 계산된다 — `edge_id` 일치만 보면 GET 에 안 나오는 edge 가 변경 대상이 되고
            # 두 가지가 뚫린다. (1) **존재 오라클**: `200`/`404` 차이로 민감 파생 취향이 추론된
            # 적 있는지 알아낼 수 있다(REQ-PGRAPH-076 [HARD] "존재 자체를 노출하지 않는다").
            # (2) **충돌 해소 우회**: `superseded`(병합 엔진이 상충에서 내린 패자)를 수정하면
            # `_pin` 이 무조건 `active` 로 되돌려, 같은 노드에 상충하는 active edge 가 둘 생기고
            # `_resolve_conflicts` 를 전혀 안 거친다.
            #
            # **반대 방향은 막지 않는다** — 보이는 edge 를 고친 결과가 숨겨진 패자와 같은 키가
            # 되는 경우는 `apply_correction` 의 병합 경로이고 의도된 동작이다(그쪽 `_find` 는
            # 필터하지 않는다 — 하면 관측 근거를 잃고 `merged` 가 거짓이 된다).
            pending = await lookup(key, request_fp=request_fp)
            if pending is None and _find_edge(document, edge_id) is None:
                # 흔적이 아예 없다 → 진짜 없는 대상이다. 원장·감사를 건드리기 전에 끝낸다.
                raise GraphEdgeNotFound(edge_id)

            token = await claim(
                key,
                user_id=user_id,
                scope_id=edge_id,
                lease_s=ttl,
                request_fp=request_fp,
                # 그래프 락을 쥐고 있으므로 `processing` 잔재의 주인은 지금 돌고 있지 않다 —
                # lease 만료를 기다리면 재시도가 최대 TTL 동안 틀린 답을 받는다.
                takeover=pending is not None,
            )
            if token is None:
                # 완료분은 위에서 걸렀으니 여기 오는 것은 **진행 중 + lease 살아 있음**뿐이다 —
                # 다른 워커가 같은 요청을 들고 있다. "진행 중"이라는 상태를 와이어에 만들지
                # 않기 위해(§2.5 에 그런 코드가 없다) 최신 버전으로 충돌을 알린다.
                raise GraphVersionConflict(graph_version(document.revision) if document else "g0")

            return await _apply_claimed(
                resume_payload=pending.response_payload if pending else None,
                store=store,
                document=document,
                user_id=user_id,
                action=action,
                edge_id=edge_id,
                if_match=if_match,
                request_id=request_id,
                now=now,
                predicate=predicate,
                object_spec=object_spec,
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


async def next_revision(*, user_id: int, existing: GraphDocument | None) -> int:
    """다음 `revision` — 문서와 **감사 하한** 중 큰 쪽에서 하나 더 (REQ-PGRAPH-042).

    `store.get_graph` 는 스키마가 깨진 문서를 조용히 `None` 으로 돌려준다(배치를 죽이지 않으려고).
    그때 `empty_document()` 로 새로 시작하면 revision 이 0 이 되고, 이미 발급된 `If-Match: "g43"`
    이 **다른 상태를 가리키게 된다** — 낙관적 동시성 전체가 무너지는 지점이다.

    감사는 전체 초기화로도 보존되므로(REQ-PGRAPH-062) 거기 남은 `graph_version_after` 최댓값이
    **항상 존재하는 하한**이다. 그래서 손상·초기화 어느 쪽으로도 되돌아가지 않는다.
    """
    floor = 0
    for row in await list_audit(user_id=user_id):
        for version in (row.graph_version_before, row.graph_version_after):
            if version.startswith("g") and version[1:].isdigit():
                floor = max(floor, int(version[1:]))
    current = existing.revision if existing is not None else 0
    return max(current, floor) + 1


@dataclass(frozen=True)
class PersonalizationState:
    """중지 플래그 + 감쇠 차감 입력 (REQ-PGRAPH-050/055).

    `disabled_spans` 는 **닫힌** 구간만 든다 — 중지 중인 현재 구간은 끝이 없어 실을 수 없다.
    """

    enabled: bool
    disabled_at: str | None
    disabled_spans: list[dict]


async def get_personalization_state(*, user_id: int) -> PersonalizationState:
    """플래그와 중지 구간을 **한 번에** 읽는다 (#359).

    `get_personalization_flag` 와 나누면 배치가 왕복을 두 번 하게 된다 — 같은 단일 행이다.
    hot-path 는 구간이 필요 없어 여전히 `get_personalization_flag` 를 쓴다.
    """
    pool = await _get_pool()
    if pool is None:
        row = _fallback_table("personalization").get(int(user_id))
        if row is None:
            return PersonalizationState(True, None, [])
        return PersonalizationState(
            bool(row["enabled"]),
            row.get("disabled_at"),
            list(row.get("disabled_spans") or []),
        )

    async def _run() -> PersonalizationState:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT enabled, disabled_at, disabled_spans "
                "FROM profile_personalization_state WHERE user_id = %s",
                (int(user_id),),
            )
            found = await cur.fetchone()
            if found is None:
                return PersonalizationState(True, None, [])
            enabled, disabled_at, spans = found
            return PersonalizationState(
                bool(enabled),
                # 저장은 timestamptz 지만 감쇠 계산은 ISO 문자열로 한다(순수 함수 입력).
                disabled_at.isoformat() if disabled_at is not None else None,
                list(spans or []),
            )

    return await run_with_query_timeout(_run())


async def get_personalization_flag(*, user_id: int) -> bool:
    """개인화 중지 플래그 — 없으면 켜짐(기본값). REQ-PGRAPH-050 의 전용 저장 위치."""
    pool = await _get_pool()
    if pool is None:
        row = _fallback_table("personalization").get(int(user_id))
        return True if row is None else bool(row["enabled"])

    async def _run() -> bool:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT enabled FROM profile_personalization_state WHERE user_id = %s",
                (int(user_id),),
            )
            found = await cur.fetchone()
            return True if found is None else bool(found[0])

    return await run_with_query_timeout(_run())


async def set_personalization_flag(
    *, user_id: int, enabled: bool, now: str, graph_version_token: str | None = None
) -> bool:
    """중지 플래그를 upsert 한다 — **락이 필요 없다**(사용자당 단일 행이라 그 자체로 원자적).

    바뀌었으면 `True`. 같은 값이면 `False` 를 돌려주어 호출부가 no-op 으로 처리하게 한다
    (감사 행도 남기지 않는다 — REQ-PGRAPH-080, api-spec §3.9.5).

    **그래프 락과 요약 락을 동시에 쥐지 않기 위해** 여기서 요약 표식은 건드리지 않는다. 그쪽은
    호출부가 요약 락 아래에서 따로 처리한다(`store.py` 의 advisory 풀 이중 점유 주석).

    **[#359] 재개는 같은 upsert 안에서 중지 구간을 닫는다**(REQ-PGRAPH-055) — `disabled_at → now`
    를 `disabled_spans` 에 덧붙인다. 그래프 문서는 여전히 건드리지 않는다.
    """
    state = await get_personalization_state(user_id=user_id)
    if state.enabled == enabled:
        return False

    disabled_at = None if enabled else now
    spans = state.disabled_spans
    if enabled and state.disabled_at:
        # 재개 — 이제야 구간이 닫힌다. 중지 중에는 끝이 없어 실을 수 없다(열린 구간을 빼면
        # 미래를 차감하는 셈이고, 어차피 중지 중에는 배치가 안 돌아 감쇠도 안 걸린다).
        spans = _capped_spans([*spans, {"from": state.disabled_at, "to": now}])

    pool = await _get_pool()
    if pool is None:
        _fallback_table("personalization")[int(user_id)] = {
            "enabled": enabled,
            "disabled_at": disabled_at,
            "disabled_spans": spans,
            "graph_version": graph_version_token,
        }
        return True

    async def _run() -> None:
        async with pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO profile_personalization_state
                    (user_id, enabled, disabled_at, disabled_spans, graph_version, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (user_id) DO UPDATE
                SET enabled = EXCLUDED.enabled,
                    disabled_at = EXCLUDED.disabled_at,
                    disabled_spans = EXCLUDED.disabled_spans,
                    graph_version = EXCLUDED.graph_version,
                    updated_at = now()
                """,
                (int(user_id), enabled, disabled_at, Jsonb(spans), graph_version_token),
            )

    await run_with_query_timeout(_run())
    return True


def _capped_spans(spans: list[dict]) -> list[dict]:
    """중지 구간 목록을 상한 안으로 — 넘으면 **가장 오래된 두 개를 하나로 합친다**.

    합치면 그 사이 간격까지 중지로 세므로 감쇠를 **덜 빼는**(=취향을 더 오래 살리는) 쪽으로
    틀린다. 오래된 것을 버리는 대안은 반대로 이미 지난 중지를 없던 일로 만들어 감쇠가 몰린다 —
    "데이터는 보존된다"는 약속에 가까운 쪽을 고른다. 병합은 결정론적이라 재생 동일성도 유지된다.
    """
    limit = get_settings().graph_decay_pause_spans_max
    ordered = sorted(spans, key=lambda span: (span.get("from") or "", span.get("to") or ""))
    while len(ordered) > limit:
        first, second, *rest = ordered
        logger.warning(
            "profile_personalization_spans_merged",
            extra={"limit": limit, "kept": len(rest) + 1},
        )
        ordered = [{"from": first["from"], "to": second["to"]}, *rest]
    return ordered


async def set_personalization(
    *, user_id: int, enabled: bool, request_id: str, now: str, if_match: str | None = None
) -> GraphMutationResult:
    """개인화 중지·재개 (§7.2 3행, REQ-PGRAPH-050~052).

    **락을 두 개 쥐지 않는다.** 플래그는 단일 행 upsert 라 그 자체로 원자적이고, 요약 표식만
    기존 요약 락 아래에서 내린다(`store.mark_summary_usable`). 그래프 락과 요약 락을 동시에
    잡으면 advisory 풀에서 커넥션을 둘 점유해 구매자 hot-path 가 3초 타임아웃으로 죽는다
    (`store.py` 실측 주석) — SPEC §7.2 의 "같은 잠금 아래" 문구는 이 제약에 맞춰 읽는다.

    `If-Match` 는 선택이다(api-spec §3.9.5). 주면 존중하고, 없으면 원장을 쓰지 않는다 —
    본문이 상태값 하나뿐이라 no-op 판정이 재전송 보호를 대신한다.

    같은 값이면 no-op 이다: 감사 행도 원장도 남기지 않고 버전도 그대로다(REQ-PGRAPH-080).
    """
    from app.agents.profile.store import get_profile_store

    store = await get_profile_store()
    settings = get_settings()

    document = await store.get_graph(str(user_id))
    current = graph_version(document.revision) if document else "g0"
    if if_match is not None and normalize_if_match(if_match) != current:
        raise GraphVersionConflict(current)

    # **파생 키에 대상 상태를 싣는다** (#360). 이 경로는 그래프 문서를 건드리지 않아
    # `graphVersion` 이 변하지 않으므로, 끄기와 켜기가 **정상적으로 같은 `If-Match`** 를
    # 지참한다. scope 가 비면 두 요청이 같은 키가 되어 **켜기가 끄기 응답을 재생**하고,
    # 원장 TTL(`graph_idempotency_ttl_h`, 기본 24h) 동안 프라이버시 스위치가 잠긴다.
    # 본문 지문(`request_fp`)으로 막는 것은 틀린 해법이다 — `LedgerRequestMismatch` → `409` 가
    # 되어 정본 I-37 이 금지한 "충돌로 끄기가 실패하는 상황"을 그대로 만든다.
    scope = "true" if enabled else "false"
    key = derived_key("personalizationToggle", user_id, scope, if_match) if if_match else None
    token = None
    try:
        if key is not None:
            replayed = await _replay_if_completed(key, document)
            if replayed is not None:
                return replayed
            token = await claim(
                key, user_id=user_id, scope_id=scope, lease_s=settings.session_end_claim_ttl_s
            )
            if token is None:
                raise GraphVersionConflict(current)

        changed = await set_personalization_flag(
            user_id=user_id, enabled=enabled, now=now, graph_version_token=current
        )
        if not changed:
            if key is not None and token is not None:
                await release(key, token)
            return GraphMutationResult(graph_version=current, replayed=True)

        # 표식 하향은 **플래그 뒤**다. 플래그가 정본이고 표식은 소비 경로의 이중 방어라
        # (REQ-PGRAPH-100), 순서를 뒤집으면 표식만 내려간 채 중지가 안 걸린 창이 생긴다.
        await store.mark_summary_usable(str(user_id), enabled)

        await record_audit(
            user_id=user_id,
            request_id=request_id,
            action="personalizationToggle",
            graph_version_before=current,
            graph_version_after=current,  # 그래프 문서는 안 바뀐다 — 중지는 데이터 변경이 아니다
        )
        if key is not None and token is not None:
            await complete(key, token, {"graphVersion": current, "personalization": enabled})
        return GraphMutationResult(graph_version=current, replayed=False)
    except GraphMutationError:
        raise
    except Exception as exc:
        if is_state_store_unavailable(exc):
            raise GraphStoreUnavailable(str(exc)) from exc
        raise


async def reset_graph(
    *, user_id: int, if_match: str, request_id: str, now: str, lease_s: float | None = None
) -> GraphMutationResult:
    """전체 초기화 — 개인화 데이터를 물리 삭제한다 (§7.2, REQ-PGRAPH-061).

    지우는 것: 그래프(노드·edge·tombstone) · fact · 요약 · 세션버퍼 · 전사록.
    **남기는 것: 감사 로그**(REQ-PGRAPH-062)와 **개인화 중지 상태**(REQ-PGRAPH-063). 감사가
    사라지면 파괴 동작이 추적 불가가 되고, 중지가 풀리면 사용자가 끈 적 없는 개인화가 조용히
    다시 켜진다.

    문서는 **삭제가 아니라 교체**다 — 지우면 다음 읽기가 `None` 을 받아 revision 이 0 으로
    되돌아간다(REQ-PGRAPH-042). `next_revision` 이 감사 하한까지 보고 값을 정한다.

    각 삭제는 멱등이라 크래시 뒤 재개가 안전하다 — 이미 없는 것을 지워도 문제되지 않는다.
    """
    # 지연 임포트로 순환을 끊는다 — `graph_merge` 는 `store` 를 거쳐 이 모듈로 돌아온다
    # (`apply_edge_mutation` 의 같은 주석 참조).
    from app.agents.profile.graph_merge import empty_document
    from app.agents.profile.store import get_profile_store

    store = await get_profile_store()
    settings = get_settings()
    key = derived_key("graphReset", user_id, "ALL", if_match)
    ttl = settings.session_end_claim_ttl_s if lease_s is None else lease_s

    async with store.graph_lock(str(user_id)):
        try:
            document = await store.get_graph(str(user_id))
            replayed = await _replay_if_completed(key, document)
            if replayed is not None:
                return replayed

            before = graph_version(document.revision) if document else "g0"
            # **CAS 판정도 원장을 보고 한다** (PR #540 리뷰) — 문서 교체 뒤 감사·완료 전에
            # 끊기면 revision 이 이미 올라가 있어, 원래 `If-Match` 로 온 재시도가 여기 걸려
            # `409` 를 받는다. §7.2 는 "재전송이면 최초 응답 재생"을 요구한다.
            pending = await lookup(key)
            if pending is None and normalize_if_match(if_match) != before:
                raise GraphVersionConflict(before)

            token = await claim(
                key,
                user_id=user_id,
                scope_id="ALL",
                lease_s=ttl,
                takeover=pending is not None,  # 그래프 락 보유 — 위 `claim` 주석과 같은 근거
            )
            if token is None:
                raise GraphVersionConflict(before)

            if pending is not None and pending.response_payload:
                # 크래시 재개 — 교체가 이미 끝났는지 의도한 revision 으로 판정한다.
                intended = pending.response_payload.get("graphVersion", "")
                if document is not None and graph_version(document.revision) == intended:
                    await record_audit(
                        user_id=user_id,
                        request_id=request_id,
                        action="graphReset",
                        graph_version_before=normalize_if_match(if_match),
                        graph_version_after=intended,
                        mutation_key=key,
                    )
                    await complete(key, token, pending.response_payload)
                    return GraphMutationResult(
                        graph_version=intended,
                        replayed=True,
                        purged=pending.response_payload.get("purged"),
                    )

            revision = await next_revision(user_id=user_id, existing=document)
            after = graph_version(revision)
            # **개수는 지우기 전에 센다.** 지운 뒤에 세면 전부 0 이다.
            # 세는 대상은 **사용자가 I-32 에서 보던 것**이라야 화면 문구("취향 12건")와 맞는다 —
            # 그래서 투영과 같은 술어(`is_projected`)를 쓴다. 저장 문서의 edge 를 통째로 세면
            # `superseded`·민감 파생까지 들어가 사용자가 본 적 없는 수가 나간다(REQ-PGRAPH-076 [HARD]).
            visible_edges = (
                sum(1 for edge in document.edges if is_projected(edge)) if document else 0
            )
            internal = await store.purge_personal_data(str(user_id))
            turns = await _delete_transcripts(user_id)
            # 진행 중인 이 초기화의 키는 남긴다 — 지우면 아래 `complete` 가 쓴 표식이 사라져
            # 초기화 재전송이 다시 실행된다.
            await invalidate_ledger(user_id=user_id, keep=key)
            # `purge_personal_data` 가 돌려주는 `{facts, summary, buffers}` 는 **와이어에 나가지
            # 않는다** — api-spec §3.9.4 의 `purged` 는 정확히 2키다. 사용자가 만든 적 없는 내부
            # 구조를 세어 보여줄 이유가 없다(v0.32.0 이 5키 → 2키로 줄인 근거).
            logger.info(
                "profile_graph_reset_internal_counts",
                extra={"actorFp": identifier_fingerprint(str(user_id)), **internal},
            )
            payload = {
                "graphVersion": after,
                "purged": {"edges": visible_edges, "transcriptTurns": turns},
            }
            # 문서 교체 **전에** 의도를 남긴다 — 교체 뒤 끊기면 재개가 이 값으로 응답을 재구성한다.
            await record_intent(key, token, payload)
            await store.set_graph(
                str(user_id),
                empty_document(now).model_copy(update={"revision": revision, "purged_at": now}),
            )

            await record_audit(
                user_id=user_id,
                request_id=request_id,
                action="graphReset",
                graph_version_before=before,
                graph_version_after=after,
                mutation_key=key,
            )
            await complete(key, token, payload)
            return GraphMutationResult(
                graph_version=after, replayed=False, purged=payload["purged"]
            )
        except GraphMutationError:
            raise
        except Exception as exc:
            if is_state_store_unavailable(exc):
                raise GraphStoreUnavailable(str(exc)) from exc
            raise


async def invalidate_ledger(*, user_id: int, keep: str | None = None) -> int:
    """이 사용자의 원장 항목을 지운다 — purge 후 "성공" 재생을 막는다 (REQ-PGRAPH-028).

    원장 TTL(`graph_idempotency_ttl_h`, 시간 단위)이 초기화 시점보다 길 수 있다. 무효화하지
    않으면 purge 로 사라진 edge 를 향한 재전송이 원장 히트로 **되돌릴 대상이 없는 "성공"을
    재생**한다. 두 보존 기간의 대소를 조정해 맞추려 들면 안 된다 — SPEC §11 이 "운영자가 값을
    바꾸면 조용히 깨지는 불변식을 만들지 않는다"로 금지한 패턴이다.

    `keep` 은 진행 중인 초기화 자신의 키다. 그것까지 지우면 방금 쓴 완료 표식이 사라져 초기화
    재전송이 다시 실행된다 — 부작용 1회 보장이 깨진다.

    **개별 삭제 재전송과는 트리거가 다르다.** 그쪽은 원문이 없어도 `200 replayed` 여야 한다
    (AC-PGRAPH-16). 여기서 지우는 것은 *초기화*라는 별개 사건이 원장을 낡게 만든 경우다.
    """
    pool = await _get_pool()
    if pool is None:
        entries = _fallback_table("idempotency")
        doomed = [
            key for key, entry in entries.items() if entry.user_id == int(user_id) and key != keep
        ]
        for key in doomed:
            del entries[key]
        return len(doomed)

    async def _run() -> int:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM profile_graph_idempotency "
                "WHERE user_id = %s AND derived_key IS DISTINCT FROM %s",
                (int(user_id), keep),
            )
            return cur.rowcount

    return await run_with_query_timeout(_run())


async def _delete_transcripts(user_id: int) -> int:
    """전사록 삭제 — 다른 저장소라 실패해도 초기화 전체를 되돌리지 않는다.

    #322 로 전체 초기화 범위에 들어왔지만(api-spec §3.9.4), `conversation_turns` 는 pg-profile
    의 별도 테이블이고 그래프 문서와 한 트랜잭션에 묶이지 않는다(§7.1 — 다중 항목 원자성 없음).
    여기서 예외를 올리면 이미 지운 fact·요약이 되살아나지 않은 채 초기화만 실패로 보고된다.
    남은 전사록은 다음 초기화가 다시 지운다(삭제는 멱등이다).
    """
    from app.core.conversation import get_conversation_store

    try:
        store = await get_conversation_store()
        return await store.delete_turns_for_user(str(user_id))
    except Exception:
        logger.warning("profile_graph_reset_transcript_delete_failed", exc_info=True)
        return 0


def _projected_edge(document: GraphDocument, edge_id: str | None, *, settings) -> dict | None:
    """원장에 보관할 **와이어 항목** — I-33 재전송이 최초 응답을 그대로 돌려주게 한다 (#360).

    삭제(`edge_id_after is None`)는 실을 것이 없다. 지연 임포트인 이유는 `graph_projection` 이
    `graph_merge` 를 거쳐 이 모듈로 돌아오기 때문이다(같은 파일의 다른 지연 임포트와 같은 근거).
    """
    if edge_id is None:
        return None
    from app.agents.profile.graph_projection import project_edge

    view = project_edge(document, edge_id, settings=settings)
    return view.model_dump(by_alias=True) if view is not None else None


def _find_edge(document: GraphDocument | None, edge_id: str) -> GraphEdge | None:
    """변경의 **대상**이 될 수 있는 edge — 잠금 아래에서 읽은 문서에서만 찾는다 (#360).

    **조회에 안 나오는 edge 는 "없는 것"이다**(`is_projected`, PR #562 리뷰) — 대상 경계와 노출
    경계가 다르면 사용자가 볼 수 없는 것을 고치거나 지울 수 있게 되고, `200`/`404` 차이가 그
    존재를 알려주는 오라클이 된다.

    이 필터는 **변경 대상 조회에만** 걸린다. `graph_mutations` 의 병합 대상 조회
    (`apply_correction` 의 `target = _find(document, new_id)`)는 숨겨진 edge 도 봐야 한다 —
    거기서 못 찾으면 관측 근거를 잃고 `merged` 가 거짓이 된다.
    """
    if document is None:
        return None
    return next(
        (edge for edge in document.edges if edge.edge_id == edge_id and is_projected(edge)), None
    )


def _request_fingerprint(
    action: str, predicate: str | None, spec: "ObjectSpec | None"
) -> str | None:
    """요청 본문의 지문 — 같은 파생 키·다른 본문을 가르는 재료.

    `edgeDelete` 는 본문이 없어(경로에 신원과 대상이 전부 있다) `None` 이다 — 같은 키면 같은
    요청이 맞다.

    **[#360] `edgeUpdate` 는 요청 *원문*에서 만든다.** 확정된 노드로 만들면 두 가지가 깨진다:
    (a) 대상 해석이 락 안으로 들어가 지문 계산이 replay 판정보다 늦어지고, (b) **부분 변경**은
    생략한 쪽을 문서에서 채우므로 같은 요청이 문서 상태에 따라 다른 지문을 얻는다. 라벨은
    `normalize_label` 로 정규화해 표기 변형이 다른 지문을 만들지 않게 한다(`node_id` 를 쓰던
    구 구현이 노리던 성질을 원문 단계에서 확보한다).
    """
    if action != "edgeUpdate":
        return None
    spec = spec or ObjectSpec()
    label = normalize_label(spec.label) if spec.label else ""
    return identifier_fingerprint(
        f"{predicate or ''}|{spec.node_id or ''}|{spec.node_type or ''}|{label}"
    )


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
        # 재전송은 **최초 응답 본문 그대로**다(REQ-PGRAPH-043). 이 시점에는 이미 다 지워져
        # 있어 다시 세면 전부 0 이므로, 원장이 든 값을 그대로 돌려줘야 한다. `edge` 도 같은
        # 이유다 — 그 사이 삭제됐으면 현재 문서로는 재구성할 수 없다.
        purged=payload.get("purged"),
        edge=payload.get("edge"),
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
    object_spec: ObjectSpec | None,
    key: str,
    token: str,
    settings,
    resume_payload: dict | None = None,
) -> GraphMutationResult:
    """claim 을 쥔 상태의 본체 — 실패 경로마다 반드시 claim 을 푼다.

    **[#360] 대상 해석과 `editable` 판정이 여기 있는 이유**는 둘 다 **잠금 아래에서 읽은 문서**를
    봐야 하기 때문이다. 순서는 `404`(위에서 원장과 함께) → `editable`(409) → `object` 해석(400)
    → `revision` CAS 다. CAS 앞에 두는 것은 **재조회로 결과가 바뀌지 않는 사실을 먼저 알리기**
    위해서다(api-spec §3.9.1 v0.32.7).
    """
    if action == "edgeDelete":
        mutate = lambda doc: apply_suppression(doc, edge_id=edge_id, now=now)  # noqa: E731
    else:
        current = _find_edge(document, edge_id)
        if current is None:
            # 문서에 대상이 없는데 여기까지 왔다 = **원장에 흔적이 있다**(404 판정이 원장과 함께
            # 이뤄졌다). 즉 이미 적용된 재개다 — 해석할 대상이 없으니 no-op 변형을 쓰고 아래
            # resume 분기가 남은 단계만 마저 한다.
            mutate = lambda doc: (doc, MutationOutcome(changed=False))  # noqa: E731
        else:
            if current.predicate == "purchased":
                # 구매 파생은 수정 불가 — 와이어 `editable: false` 와 같은 술어다(api-spec §3.9.1).
                await release(key, token)
                raise GraphEdgeNotEditable(edge_id)
            node = await resolve_user_object(
                object_spec, document=document, current=current, settings=settings, now=now
            )
            if node is None:
                # 어휘 밖 라벨·형식 불일치·그래프 밖 `nodeId` — 추측해서 붙이지 않는다.
                await release(key, token)
                raise GraphObjectUnknown(edge_id)
            # 생략한 쪽은 대상 edge 의 현재 값으로 채운다(api-spec §3.9.1).
            effective_predicate = predicate or current.predicate
            mutate = lambda doc: apply_correction(  # noqa: E731
                doc,
                edge_id=edge_id,
                predicate=effective_predicate,
                node=node,
                now=now,
                settings=settings,
            )

    if resume_payload is not None:
        # **크래시 재개** (§7.2, PR #540 리뷰). 이전 시도가 의도를 적어 뒀다는 뜻이다.
        # 문서에 이미 반영됐는지는 순수 변형으로 판정한다 — 반영됐다면 같은 변경이 더 할 일이
        # 없어 `changed=False` 가 나온다. revision 비교로 하지 않는 이유는 그 사이 배치
        # consolidation 이 revision 을 더 올렸을 수 있어서다.
        _, probe = mutate(document)
        if not probe.changed:
            # 문서 쓰기까지는 끝났다 — 남은 단계(감사·완료)만 마저 하고 **최초 의도한 응답**을
            # 돌려준다. 감사는 파생 키로 멱등이라 이미 썼다면 두 번 남지 않는다.
            await record_audit(
                user_id=user_id,
                request_id=request_id,
                action=action,
                graph_version_before=normalize_if_match(if_match),
                graph_version_after=resume_payload.get("graphVersion", ""),
                edge_id_before=edge_id,
                edge_id_after=resume_payload.get("edgeId"),
                mutation_key=key,
            )
            await complete(key, token, resume_payload)
            return _result_from(resume_payload, action=action, fallback_edge_id=edge_id)
        # 아직 반영 안 됐다 — 아래 정상 경로가 처음부터 다시 한다.

    before = graph_version(document.revision)
    if normalize_if_match(if_match) != before:
        await release(key, token)
        raise GraphVersionConflict(before)

    updated, outcome = mutate(document)

    if not outcome.changed:
        # 상태를 바꾸지 않았다 — 감사 행도 revision 도 남기지 않고 claim 을 되돌린다.
        # **되돌리지 않으면 뒤따르는 진짜 변경이 막힌다** — 파생 키에 본문이 없어서 같은
        # `If-Match` 로 온 다른 수정이 같은 키를 쓰기 때문이다(테스트로 고정).
        await release(key, token)
        return GraphMutationResult(graph_version=before, replayed=True, edge_id=edge_id)

    after = graph_version(document.revision + 1)
    payload = {
        "graphVersion": after,
        "edgeId": outcome.edge_id_after,
        "merged": outcome.merged,
        "suppressed": action == "edgeDelete",
        # **투영된 edge 를 원장에 보관한다** (#360). 재전송은 "최초 응답 본문을 그대로"인데
        # (REQ-PGRAPH-043), 그 사이 이 edge 가 삭제됐으면 **현재 문서로는 재구성할 수 없다** —
        # 수정 성공 → 삭제 → 원래 `If-Match` 로 온 네트워크 재시도(원장 TTL 24h 내)가 그 창이다.
        # 지연 임포트로 순환을 끊는다(`graph_projection` → `graph_merge` → … → 이 모듈).
        "edge": _projected_edge(updated, outcome.edge_id_after, settings=settings),
    }
    # **문서를 쓰기 전에 의도를 남긴다** (§7.2 저널 선행 기록). 문서 쓰기와 완료 표시는 서로
    # 다른 저장소라 한 트랜잭션에 못 묶이므로, 그 사이에 끊기면 재개가 "무엇을 만들려 했는지"를
    # 알 방법이 없다 — 그러면 재시도가 최초 응답 대신 404/409 를 받는다.
    await record_intent(key, token, payload)
    await store.set_graph(
        str(user_id), updated.model_copy(update={"revision": document.revision + 1})
    )
    if action == "edgeDelete":
        # [HARD] 원문은 **그 요청을 처리하는 시점에** 사라져야 한다(REQ-PGRAPH-025) — 라벨만
        # 지우고 근거 fact 를 남기면 사용자가 "지웠다"고 믿는 문장이 저장소에 그대로 있다.
        # 문서 쓰기 뒤에 두는 이유: 문서 쓰기가 실패하면 fact 는 살아 있어야 한다(무손상).
        await store.delete_facts_backing(str(user_id), {edge_id})

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
        mutation_key=key,
    )
    await complete(key, token, payload)
    return GraphMutationResult(
        graph_version=after,
        replayed=False,
        edge_id=outcome.edge_id_after,
        merged=outcome.merged,
        suppressed=action == "edgeDelete",
        edge=payload["edge"],
    )


def _result_from(
    payload: dict, *, action: str, fallback_edge_id: str | None = None
) -> GraphMutationResult:
    """저장된 의도 payload → 결과. 재개·재생이 **최초 응답과 같은 값**을 돌려주게 한다."""
    return GraphMutationResult(
        graph_version=payload.get("graphVersion", ""),
        replayed=True,
        # `.get(key, default)` 이 아니라 `or` 인 이유: 삭제 성공의 `edgeId` 는 **키가 있고 값이
        # None** 이라 기본값이 발동하지 않는다(`apply_suppression` 이 `edge_id_after=None`).
        edge_id=payload.get("edgeId") or fallback_edge_id,
        merged=bool(payload.get("merged")),
        suppressed=bool(payload.get("suppressed", action == "edgeDelete")),
        purged=payload.get("purged"),
        edge=payload.get("edge"),
    )
