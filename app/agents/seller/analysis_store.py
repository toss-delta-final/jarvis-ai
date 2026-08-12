"""판매자 분석 저장 계층 — 5테이블 리포지토리 + targets 자동 등록 훅 (이슈 #585).

`db/profile/init/05_seller_analysis.sql`의 애플리케이션 진입점이다. 전체 설계 근거는
`docs/specs/DESIGN-SELLER-ANALYSIS-STORE-585.md`(D-1~D-4) 참조 — 이 모듈은 그 설계의 §2·§3·§4를
그대로 구현한다.

**전용 `AsyncConnectionPool`을 새로 연다** (D-3) — `app/agents/seller/checkpoint.py`의
`AsyncPostgresSaver` 단일 커넥션과 무접촉이다(OPS-RUNTIME.md §1.6 목적). BaseStore·advisory·
graph_journal·history 와 마찬가지로 pg-profile 에 붙는 풀 하나가 더 늘어난다.

**dev/test 연결 실패는 InMemory 미러가 아니라 no-op 이다** (D-4) — 5테이블·FK·트랜잭션을
메모리로 흉내내면 이 이슈 범위가 폭증하고 "저장된 줄 알았는데 아님"이라는 거짓 성공이
생긴다. 운영(auth_mode=jwks)은 폴백 없이 그대로 raise 한다(history.py·graph_journal.py 관행).

읽기(list_reports 등)는 재시도가 없다(이슈 명시 — 대화형 조회 경로를 늦추지 않는다). 쓰기만
`seller_db_write_retries`(기본 1회) 재시도하며, 재시도는 트랜잭션을 통째로 재실행한다 —
멱등은 `id` PK 충돌 무해화(`ON CONFLICT (id) DO NOTHING`, 호출부가 uuid4 를 미리 생성해
넘긴다) 또는 자연 키 UNIQUE UPSERT 로 보장한다(analysis_records.py 모듈 docstring 참조).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, TypeVar
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.agents.seller.analysis_records import (
    OutcomeRecord,
    RecommendationRecord,
    ReportRecord,
    SnapshotRecord,
)
from app.agents.seller.context import SellerContext
from app.core.config import get_settings
from app.core.pg_resilience import (
    BoundedLRUCache,
    hardened_pg_conninfo,
    is_state_store_unavailable,
    run_with_query_timeout,
    state_store_pool_config,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# DDL 직렬화 키 — 5테이블을 한 트랜잭션에서 만드므로 잠금도 하나다(graph_journal 선례).
SCHEMA_LOCK_KEY = "schema:seller_analysis:lifecycle"

_TABLE_NAMES: tuple[str, ...] = (
    "seller_analysis_targets",
    "seller_analysis_snapshots",
    "seller_analysis_reports",
    "seller_analysis_recommendations",
    "seller_analysis_outcomes",
)

_pool: AsyncConnectionPool | None = None
# 연결 실패 후 dev/test 확정 no-op(D-4) — InMemory 미러 대신 매 호출 즉시 반환한다. 재접속을
# 매 호출마다 재시도하면 DB 가 죽어 있는 동안 대화형 경로가 계속 그 지연을 문다.
_dev_noop = False
_fallback_warned = False
_init_lock = asyncio.Lock()
_pending_cleanup: list[AsyncConnectionPool] = []

# targets 훅 — (brand_id, 날짜) 중복 억제 캐시. 없어도 동작하지만 턴마다 UPDATE 가 나간다.
_seen_cache: BoundedLRUCache[int, str] | None = None
# asyncio.create_task 강참조 — 지역 변수만 두면 GC 가 fire-and-forget 태스크를 조용히 지운다.
_background: set[asyncio.Task] = set()


def _get_seen_cache() -> BoundedLRUCache[int, str]:
    global _seen_cache
    if _seen_cache is None:
        _seen_cache = BoundedLRUCache(
            max_entries=get_settings().state_store_local_cache_max_entries
        )
    return _seen_cache


# ── 풀 수명주기 (pg_store.py·graph_journal.py 와 동일 규약) ──────────────────────


def set_pool(pool: AsyncConnectionPool | None) -> None:
    """풀 교체(테스트용) — None 이면 다음 사용 시 재초기화한다.

    sync 라 여기서 직접 await 할 수 없다. 이전 풀은 정리 대기열에 넣고 다음 `_get_pool()`
    (반드시 async 컨텍스트)에서 확실히 닫는다(`graph_journal.set_pool` 과 동일 근거).
    """
    global _pool, _dev_noop
    old_pool = _pool
    _pool = pool
    _dev_noop = False
    if old_pool is not None:
        _pending_cleanup.append(old_pool)


async def _drain_pending_cleanup(*, propagate_errors: bool = False) -> None:
    """대기열의 이전 풀들을 닫는다 — 이미 소멸한 루프에서 만들어진 풀일 수 있다.

    `CancelledError` 를 무조건 삼키면 이 `await` 지점에서 현재 태스크 자체가 취소되는 경우까지
    함께 삼켜진다. `task.cancelling()` 으로 "다른 루프 잔재"와 "실제 취소 요청"을 구분한다
    (`graph_journal._drain_pending_cleanup` 과 동일).
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
            logger.warning("seller_analysis pool cleanup failed", exc_info=True)
            if first_error is None:
                first_error = exc
    if propagate_errors and first_error is not None:
        raise first_error


async def close_pool() -> None:
    """지금 열려 있는 풀을 이 이벤트 루프에서 닫는다 (`app.main._close_owned_resources` 등록)."""
    set_pool(None)
    await _drain_pending_cleanup(propagate_errors=True)


async def warm_pool() -> None:
    """기동 시 풀을 미리 연다 — 실패해도 기동을 막지 않는다(호출부 `app.main` 이 삼킨다).

    `graph_journal.warm_pool` 과 대칭 — 첫 판매자 턴이 지연 초기화 비용(연결 5s +
    마이그레이션 30s)을 물지 않게 한다.
    """
    await _get_pool()


def reset_pool() -> None:
    """테스트 격리용 — 다음 사용 시 재초기화(실제 연결 시도 없이 즉시 확정하지 않는다).

    `_init_lock` 도 새로 만든다 — pytest-asyncio 테스트마다 새 이벤트 루프를 쓰는데, 모듈 전역
    `asyncio.Lock` 을 여러 루프에서 재사용하면 락 획득이 영영 안 풀리는 hang 이 난다
    (docs/lessons.md 2026-07-20, graph_journal.reset 과 동일 근거).
    """
    global _pool, _dev_noop, _init_lock, _seen_cache
    old_pool = _pool
    _pool = None
    _dev_noop = False
    _init_lock = asyncio.Lock()
    _seen_cache = None
    if old_pool is not None:
        _pending_cleanup.append(old_pool)


async def _get_pool() -> AsyncConnectionPool | None:
    """AsyncConnectionPool(pg-profile 전용, D-3) 지연 초기화 — 실패 시 (D-4) 참조.

    락 없는 지연 초기화는 콜드 스타트 시 동시 요청이 풀을 중복 생성한다 — `_init_lock` 으로
    초기화 블록 전체를 직렬화한다(graph_journal·history·pg_store 공통 관행).
    """
    global _pool, _dev_noop, _fallback_warned
    await _drain_pending_cleanup()
    async with _init_lock:
        if _pool is None and not _dev_noop:
            settings = get_settings()
            pool = None
            try:
                pool = AsyncConnectionPool(
                    hardened_pg_conninfo(settings.profile_db_url),
                    open=False,
                    **state_store_pool_config(),
                )
                await asyncio.wait_for(
                    pool.open(wait=True), timeout=settings.state_store_connect_timeout_s
                )
                await _run_ddl(pool)
                _pool = pool
            except asyncio.CancelledError:
                # targets 자동 등록은 fire-and-forget이라 요청/테스트 portal 종료와 함께
                # pool.open() 도중 취소될 수 있다. 방금 만든 풀을 닫지 않으면 psycopg worker가
                # 취소를 삼키고 이벤트 루프 teardown을 영원히 막는다(#208과 같은 실패 형태).
                if pool is not None:
                    try:
                        await pool.close()
                    except Exception:
                        pass
                raise
            except Exception as exc:
                if pool is not None:
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
                        "pg-profile seller_analysis 연결 실패(%s) — no-op 확정 "
                        "(dev 전용: 분석 저장 계층 쓰기/조회가 조용히 스킵된다, InMemory 미러 없음)",
                        exc,
                    )
                    _fallback_warned = True
                _dev_noop = True
    return _pool


# ── 부팅 스키마 준비 (D-2, 설계서 §2) ────────────────────────────────────────────


async def ensure_schema() -> None:
    """5테이블을 idempotent 생성(`_get_pool`이 이미 수행) 후 `to_regclass`로 재확인한다.

    `app.main._lifespan()`이 부른다. 컬럼 등 스키마 **내용**은 검증하지 않는다(OPS §1.3) —
    `CREATE TABLE IF NOT EXISTS`는 기존 테이블을 갱신하지 않으므로 컬럼 추가는 `06_*.sql`이
    필요하고 누락은 런타임에 드러난다.

    운영(auth_mode=jwks)은 누락 시 `RuntimeError`로 기동을 거부한다 — "SQL 파일 적용을 사람이
    빠뜨림"(OPS §6 최상단 위험)이 여기서 잡힌다. dev/test 는 경고 후 계속한다. `_get_pool()`
    자체의 실패(DB 도달 불가 등)는 이미 그쪽에서 jwks 여부에 따라 raise/no-op 확정으로
    처리되므로 여기서 다시 다루지 않는다.
    """
    settings = get_settings()
    pool = await _get_pool()
    if pool is None:
        return  # dev/test — _get_pool() 이 이미 경고를 남겼다
    missing = await _missing_tables(pool)
    if missing:
        message = f"seller_analysis schema missing tables after ensure: {', '.join(missing)}"
        if settings.auth_mode == "jwks":
            raise RuntimeError(message)
        logger.warning(message)


async def _missing_tables(pool: AsyncConnectionPool) -> list[str]:
    async def _run() -> list[str]:
        async with pool.connection() as conn:
            missing: list[str] = []
            for name in _TABLE_NAMES:
                cur = await conn.execute("SELECT to_regclass(%s)", (name,))
                row = await cur.fetchone()
                if row is None or row[0] is None:
                    missing.append(name)
            return missing

    return await run_with_query_timeout(_run())


async def _run_ddl(pool: AsyncConnectionPool) -> None:
    """5테이블 + 인덱스 + 확장 컬럼을 idempotent 하게 만든다.

    정의 원천은 `db/profile/init/05_seller_analysis.sql`(테이블) +
    `06_seller_analysis_ext.sql`(이슈 #599 확장 컬럼)이다.

    이 SQL 파일이 정본이다 — 이후 컬럼·인덱스 변경은 `06_*.sql` 추가와 이 함수 갱신을 같은
    커밋에 넣는다(설계서 §9 "DDL이 두 곳에 존재" 위험 대응). 한 advisory 잠금·한 트랜잭션에서
    만들어 동시 기동 인스턴스가 서로 절반씩 만든 상태를 못 만들게 한다(graph_journal 선례).
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
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seller_analysis_targets (
                        brand_id bigint PRIMARY KEY,
                        seller_id bigint NOT NULL,
                        first_seen_at timestamptz NOT NULL DEFAULT now(),
                        last_seen_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS ix_sat_active "
                    "ON seller_analysis_targets (last_seen_at DESC)"
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seller_analysis_snapshots (
                        id uuid PRIMARY KEY,
                        brand_id bigint NOT NULL,
                        period_from date NOT NULL,
                        period_to date NOT NULL,
                        computed_at timestamptz NOT NULL DEFAULT now(),
                        source text NOT NULL,
                        feature_spec_version text NOT NULL,
                        total_customers integer NOT NULL,
                        row_limit integer NOT NULL,
                        truncated boolean NOT NULL,
                        insufficient_cohort boolean NOT NULL,
                        scaler_params jsonb NOT NULL,
                        pca_used boolean NOT NULL,
                        pca_params jsonb,
                        silhouette double precision,
                        random_state integer NOT NULL,
                        clusters jsonb NOT NULL,
                        feature_rows jsonb NOT NULL,
                        holds jsonb NOT NULL DEFAULT '[]',
                        UNIQUE (brand_id, period_to, feature_spec_version)
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS ix_sas_brand_computed "
                    "ON seller_analysis_snapshots (brand_id, computed_at DESC)"
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seller_analysis_reports (
                        id uuid PRIMARY KEY,
                        brand_id bigint NOT NULL,
                        trigger_type text NOT NULL,
                        period_from date NOT NULL,
                        period_to date NOT NULL,
                        compared_from date,
                        compared_to date,
                        title text NOT NULL,
                        summary text NOT NULL,
                        report_md text NOT NULL,
                        segments jsonb NOT NULL DEFAULT '[]',
                        holds jsonb NOT NULL DEFAULT '[]',
                        verified boolean NOT NULL,
                        score_total integer,
                        attempts integer NOT NULL,
                        snapshot_id uuid REFERENCES seller_analysis_snapshots(id) ON DELETE SET NULL,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        read_at timestamptz
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS ix_sar_brand_created "
                    "ON seller_analysis_reports (brand_id, created_at DESC)"
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seller_analysis_recommendations (
                        id uuid PRIMARY KEY,
                        report_id uuid NOT NULL
                            REFERENCES seller_analysis_reports(id) ON DELETE CASCADE,
                        brand_id bigint NOT NULL,
                        rank integer NOT NULL,
                        action_type text NOT NULL,
                        target_kind text NOT NULL DEFAULT 'product',
                        segment_label text NOT NULL DEFAULT '',
                        product_ids bigint[] NOT NULL DEFAULT '{}',
                        title text NOT NULL,
                        rationale text NOT NULL,
                        expected_effect text NOT NULL DEFAULT '',
                        changes jsonb NOT NULL DEFAULT '[]',
                        effectiveness_score double precision NOT NULL DEFAULT 0.5,
                        status text NOT NULL DEFAULT 'proposed',
                        applied_at timestamptz,
                        draft_id text,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        UNIQUE (report_id, rank)
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS ix_sarec_brand_status "
                    "ON seller_analysis_recommendations (brand_id, status, created_at DESC)"
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS ix_sarec_draft "
                    "ON seller_analysis_recommendations (draft_id)"
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS seller_analysis_outcomes (
                        id uuid PRIMARY KEY,
                        rec_id uuid NOT NULL
                            REFERENCES seller_analysis_recommendations(id) ON DELETE CASCADE,
                        brand_id bigint NOT NULL,
                        product_id bigint,
                        action_type text NOT NULL,
                        applied_at timestamptz NOT NULL,
                        measured_at timestamptz NOT NULL DEFAULT now(),
                        metric_key text NOT NULL,
                        window_days integer NOT NULL,
                        outcome_spec_version text NOT NULL DEFAULT 'oc_v1',
                        treated_pre_succ integer, treated_pre_trials integer,
                        treated_post_succ integer, treated_post_trials integer,
                        control_pre_succ integer, control_pre_trials integer,
                        control_post_succ integer, control_post_trials integer,
                        control_products integer,
                        delta_pp double precision,
                        p_value double precision,
                        verdict text NOT NULL,
                        confounders jsonb NOT NULL DEFAULT '[]',
                        UNIQUE (rec_id, metric_key)
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS ix_saout_rank "
                    "ON seller_analysis_outcomes (brand_id, action_type, verdict, "
                    "outcome_spec_version)"
                )
                # [이슈 #601] 무인 배치 실행 로그 — db/profile/init/06_seller_analysis_run_log.sql
                # 과 같은 문장이다(그 파일이 정본, 여긴 idempotent 복제 — 05 파일의 §9 규약).
                await conn.execute(
                    "ALTER TABLE seller_analysis_targets "
                    "ADD COLUMN IF NOT EXISTS last_run_at timestamptz"
                )
                await conn.execute(
                    "ALTER TABLE seller_analysis_targets "
                    "ADD COLUMN IF NOT EXISTS last_skip_reason text"
                )

                # ── 06_seller_analysis_ext.sql (이슈 #599) — 컬럼 추가는 CREATE TABLE
                # IF NOT EXISTS 가 하지 않으므로 ALTER 로 따로 얹는다. ADD COLUMN IF NOT
                # EXISTS 라 재적용 무해하고, 같은 트랜잭션·같은 잠금 안이라 05 와 06 이
                # 절반씩 적용된 상태가 생기지 않는다.
                await conn.execute(
                    "ALTER TABLE seller_analysis_reports "
                    "ADD COLUMN IF NOT EXISTS findings jsonb NOT NULL DEFAULT '[]'"
                )
                await conn.execute(
                    "ALTER TABLE seller_analysis_targets "
                    "ADD COLUMN IF NOT EXISTS last_run_at timestamptz"
                )
                await conn.execute(
                    "ALTER TABLE seller_analysis_targets "
                    "ADD COLUMN IF NOT EXISTS last_skip_reason text"
                )

    await asyncio.wait_for(_run(), timeout=settings.state_store_migration_timeout_s)


# ── 쓰기 경계 (설계서 §3.2) ──────────────────────────────────────────────────────


async def _write(run: Callable[[AsyncConnection], Awaitable[_T]]) -> _T | None:
    """트랜잭션 + `SET LOCAL statement_timeout` + 실패 시 1회 재시도(트랜잭션 통째 재실행).

    풀이 없으면(D-4 dev/test no-op) 아무 것도 하지 않고 `None`을 돌려준다 — 호출부가 이미
    알고 있는 값(예: 캐릭터가 생성한 `record.id`)으로 대체한다. 읽기에는 이 헬퍼를 쓰지 않는다
    (이슈 명시).
    """
    settings = get_settings()
    pool = await _get_pool()
    if pool is None:
        return None
    write_timeout_ms = max(1, int(settings.seller_analysis_write_timeout_s * 1000))
    attempts = settings.seller_db_write_retries + 1
    for attempt in range(attempts):
        try:
            async with pool.connection(timeout=settings.state_store_query_timeout_s) as conn:
                async with conn.transaction():
                    # SET/SET LOCAL 은 서버측 파라미터 바인딩($1)을 지원하지 않아 실 PG
                    # 에서 SyntaxError 가 난다 — _run_ddl 과 같은 set_config 형태로 통일.
                    # 세 번째 인자 is_local=true 라 SET LOCAL 과 동일하게 트랜잭션 종료 시
                    # 원복된다.
                    await conn.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(write_timeout_ms),),
                    )
                    return await run(conn)
        except Exception as exc:
            if attempt + 1 >= attempts or not is_state_store_unavailable(exc):
                raise
            logger.warning("seller_analysis write retry attempt=%d", attempt + 1, exc_info=True)
    return None  # pragma: no cover - 루프가 항상 return/raise 한다


def _row_to(model: type[_T], cur_description: Any, row: tuple) -> _T:
    columns = [col.name for col in cur_description]
    return model(**dict(zip(columns, row, strict=True)))


# ── targets (결정 110~112, 설계서 §4) ───────────────────────────────────────────


async def register_target(brand_id: int, seller_id: int) -> None:
    """`seller_analysis_targets` UPSERT — 멱등, 다중 인스턴스 경합 무해."""

    async def _run(conn: AsyncConnection) -> None:
        await conn.execute(
            """
            INSERT INTO seller_analysis_targets (brand_id, seller_id) VALUES (%s, %s)
            ON CONFLICT (brand_id) DO UPDATE
               SET last_seen_at = now(), seller_id = EXCLUDED.seller_id
            """,
            (brand_id, seller_id),
        )

    await _write(_run)


async def _register_quietly(context: SellerContext) -> None:
    """fire-and-forget 실제 등록 — 모든 예외를 삼킨다(history.save_history 관행 승계)."""
    try:
        await register_target(context.brand_id, context.seller_id)
    except Exception:  # noqa: BLE001 - 등록 실패가 판매자 응답을 죽이면 안 된다
        logger.warning(
            "seller analysis target register failed code=SELLER_ANALYSIS_TARGET_REGISTER_FAILED",
            exc_info=True,
        )


def note_seller_seen(context: SellerContext) -> None:
    """`/seller/chat` 스트림 진입부(`_seller_context` 성공 직후)에서만 부른다.

    `require_seller`(`api/deps.py`)에는 넣지 않는다 — buyer 와 공용이고 sync 의존성이라
    `asyncio.create_task`를 걸 자리가 아니다(OPS §1.7 명시, 실측 확인). R-1 조회 경로의 훅은
    이 이슈 범위 밖이다(이슈 본문 명시 — 별도 이슈).
    """
    today = date.today().isoformat()
    cache = _get_seen_cache()
    if cache.get(context.brand_id) == today:
        return  # 하루 1회만 실제 쓰기
    # 쓰기 시도 **전에** 채운다 — 실패한 브랜드는 다음 날까지 재시도하지 않는다. 매 턴 실패
    # 쓰기를 반복하는 것보다 낫고, 등록은 "언젠가 되면 되는" 성질이다.
    cache[context.brand_id] = today
    task = asyncio.create_task(_register_quietly(context))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def update_target_run(
    brand_id: int, *, last_run_at: datetime, last_skip_reason: str | None
) -> None:
    """무인 배치 1브랜드 실행 결과를 `seller_analysis_targets`에 기록한다(이슈 #601).

    `last_skip_reason=None`은 "보고서를 만들었다"이지 "아무 일도 안 했다"가 아니다 —
    실행 자체는 `last_run_at`이 증언한다. 사유가 있으면(`no_baseline_data`·
    `snapshot_failed`·`no_findings` 등) 그 값을 남겨 R-1 `noReportReason`과 운영 로그
    양쪽의 원천으로 쓴다(10-TRIGGER.md 결정 98). 대상이 아직 `register_target`으로
    등록되지 않았으면(브랜드가 그 사이 삭제된 이상 상황) 0행 UPDATE로 조용히 넘어간다 —
    이 함수가 대상을 새로 만들지는 않는다(대상 등록은 접속 시 훅 소관).
    """

    async def _run(conn: AsyncConnection) -> None:
        await conn.execute(
            "UPDATE seller_analysis_targets "
            "SET last_run_at = %s, last_skip_reason = %s WHERE brand_id = %s",
            (last_run_at, last_skip_reason, brand_id),
        )

    await _write(_run)


async def list_active_targets(ttl_days: int | None = None) -> list[int]:
    """무인 순회 대상 — `last_seen_at`이 `ttl_days`(기본 `seller_analysis_target_ttl_days`) 이내."""
    settings = get_settings()
    days = settings.seller_analysis_target_ttl_days if ttl_days is None else ttl_days
    pool = await _get_pool()
    if pool is None:
        return []

    async def _run() -> list[int]:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT brand_id FROM seller_analysis_targets "
                "WHERE last_seen_at > now() - make_interval(days => %s) "
                "ORDER BY brand_id",
                (days,),
            )
            return [row[0] for row in await cur.fetchall()]

    return await run_with_query_timeout(_run())


# ── 스냅샷 ───────────────────────────────────────────────────────────────────────


async def save_snapshot(record: SnapshotRecord) -> UUID:
    """UPSERT (brand_id, period_to, feature_spec_version) — 같은 날 재계산은 최신값으로 덮는다.

    저장 직전 직렬화 크기를 로그로 남긴다(이슈 명시) — `seller_analysis_write_timeout_s`(15s)가
    실측 없이 정해진 값이라, 첫 주 로그로 조정한다(OPS §1.5).
    """
    try:
        payload_bytes = len(json.dumps(record.feature_rows, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        payload_bytes = -1
    rows = len(record.feature_rows) if isinstance(record.feature_rows, list) else None
    logger.info(
        "seller_snapshot_write brand=%s rows=%s bytes=%d",
        record.brand_id,
        rows,
        payload_bytes,
    )

    async def _run(conn: AsyncConnection) -> UUID:
        cur = await conn.execute(
            """
            INSERT INTO seller_analysis_snapshots (
                id, brand_id, period_from, period_to, computed_at, source,
                feature_spec_version, total_customers, row_limit, truncated,
                insufficient_cohort, scaler_params, pca_used, pca_params, silhouette,
                random_state, clusters, feature_rows, holds
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (brand_id, period_to, feature_spec_version) DO UPDATE SET
                period_from = EXCLUDED.period_from,
                computed_at = EXCLUDED.computed_at,
                source = EXCLUDED.source,
                total_customers = EXCLUDED.total_customers,
                row_limit = EXCLUDED.row_limit,
                truncated = EXCLUDED.truncated,
                insufficient_cohort = EXCLUDED.insufficient_cohort,
                scaler_params = EXCLUDED.scaler_params,
                pca_used = EXCLUDED.pca_used,
                pca_params = EXCLUDED.pca_params,
                silhouette = EXCLUDED.silhouette,
                random_state = EXCLUDED.random_state,
                clusters = EXCLUDED.clusters,
                feature_rows = EXCLUDED.feature_rows,
                holds = EXCLUDED.holds
            RETURNING id
            """,
            (
                record.id,
                record.brand_id,
                record.period_from,
                record.period_to,
                record.computed_at,
                record.source,
                record.feature_spec_version,
                record.total_customers,
                record.row_limit,
                record.truncated,
                record.insufficient_cohort,
                Jsonb(record.scaler_params),
                record.pca_used,
                Jsonb(record.pca_params) if record.pca_params is not None else None,
                record.silhouette,
                record.random_state,
                Jsonb(record.clusters),
                Jsonb(record.feature_rows),
                Jsonb(record.holds),
            ),
        )
        row = await cur.fetchone()
        return row[0] if row else record.id

    result = await _write(_run)
    return result if result is not None else record.id


async def load_latest_snapshot(
    brand_id: int, *, fresh_within_hours: int | None = None
) -> SnapshotRecord | None:
    """가장 최근 스냅샷 — `fresh_within_hours`를 주면 그 안에 계산된 것만."""
    pool = await _get_pool()
    if pool is None:
        return None

    async def _run() -> SnapshotRecord | None:
        async with pool.connection() as conn:
            if fresh_within_hours is None:
                cur = await conn.execute(
                    "SELECT * FROM seller_analysis_snapshots WHERE brand_id = %s "
                    "ORDER BY computed_at DESC LIMIT 1",
                    (brand_id,),
                )
            else:
                cur = await conn.execute(
                    "SELECT * FROM seller_analysis_snapshots WHERE brand_id = %s "
                    "AND computed_at > now() - make_interval(hours => %s) "
                    "ORDER BY computed_at DESC LIMIT 1",
                    (brand_id, fresh_within_hours),
                )
            row = await cur.fetchone()
            if row is None:
                return None
            return _row_to(SnapshotRecord, cur.description, row)

    return await run_with_query_timeout(_run())


async def load_snapshot_at(brand_id: int, period_to: date) -> SnapshotRecord | None:
    """지정 `period_to` 의 스냅샷 1행 — churn 워커의 비교 기준(7일 전) 조회 축 (이슈 #594).

    `load_latest_snapshot` 은 "가장 최근"만 주므로 시점 지정 비교에 쓸 수 없다.

    ⚠️ **`feature_spec_version` 으로 거르지 않는다.** 걸러 버리면 "스펙이 달라 비교
    보류"(`spec_mismatch`)와 "그날 배치가 아예 없었다"(`no_baseline`)가 둘 다 None 으로
    같아져 판매자에게 사유를 밝힐 수 없다 — 버전 대조는 compute 스텝이 한다.
    같은 (brand_id, period_to) 에 스펙 버전이 여럿이면 최신 계산본을 준다.

    근사 탐색을 하지 않는 것도 의도다 — "7일 전"이 실제로 6일 전이면 순증감의 분모가
    조용히 달라진다. 그날 행이 없으면 None 이고, 호출부가 `Hold("no_baseline")` 을 단다.
    """
    pool = await _get_pool()
    if pool is None:
        return None

    async def _run() -> SnapshotRecord | None:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM seller_analysis_snapshots WHERE brand_id = %s "
                "AND period_to = %s ORDER BY computed_at DESC LIMIT 1",
                (brand_id, period_to),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return _row_to(SnapshotRecord, cur.description, row)

    return await run_with_query_timeout(_run())


async def delete_expired_snapshots(retention_days: int) -> int:
    """14일 보존(OPS §1.8) 실행부 — 호출(스케줄링)은 무인 배치 이슈 소관(설계서 §9)."""

    async def _run(conn: AsyncConnection) -> int:
        cur = await conn.execute(
            "DELETE FROM seller_analysis_snapshots "
            "WHERE computed_at < now() - make_interval(days => %s)",
            (retention_days,),
        )
        return cur.rowcount

    result = await _write(_run)
    return result if result is not None else 0


# ── 보고서 + 추천 (단일 트랜잭션, 이슈 명시) ─────────────────────────────────────


async def save_report(report: ReportRecord, recommendations: list[RecommendationRecord]) -> UUID:
    """보고서 1행 + 추천 N행을 한 트랜잭션에 쓴다 — 부분 저장되면 §6.3 "N번 적용해줘"가 깨진다.

    재시도가 두 번째 행을 만들지 않도록 `id` PK 충돌을 `DO NOTHING`으로 무해화한다(호출부가
    `report.id`·각 `rec.id`를 미리 uuid4 로 생성해 넘긴다).
    """

    async def _run(conn: AsyncConnection) -> UUID:
        await conn.execute(
            """
            INSERT INTO seller_analysis_reports (
                id, brand_id, trigger_type, period_from, period_to, compared_from, compared_to,
                title, summary, report_md, segments, findings, holds, verified, score_total,
                attempts, snapshot_id, created_at, read_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                report.id,
                report.brand_id,
                report.trigger_type,
                report.period_from,
                report.period_to,
                report.compared_from,
                report.compared_to,
                report.title,
                report.summary,
                report.report_md,
                Jsonb(report.segments),
                Jsonb(report.findings),
                Jsonb(report.holds),
                report.verified,
                report.score_total,
                report.attempts,
                report.snapshot_id,
                report.created_at,
                report.read_at,
            ),
        )
        for rec in recommendations:
            # [이슈 #598] 추천 생애주기 — 새 추천이 이전 'proposed' 추천과 같은
            # (brand_id, product, action_type)을 겨냥하면 이전 것을 superseded 로
            # 전이한다. `DESIGN-SELLER-ANALYSIS-STORE-585.md` 가 컬럼·enum 값만 두고
            # "전이 로직은 추천 생애주기 이슈"라고 명시적으로 미룬 지점이다. 같은
            # 트랜잭션 안에서 실행해 새 추천 삽입과 원자적으로 묶는다 — 따로 커밋하면
            # 그 사이 조회가 "이전 것도 살아있고 새 것도 있는" 순간을 볼 수 있다.
            # `product_ids && %s`(배열 겹침)로 매칭한다 — 상품 하나라도 겹치면 같은
            # 대상으로 본다(target_kind="product" 전제, 새 상품 유형이 생기면 재검토).
            if rec.product_ids:
                await conn.execute(
                    """
                    UPDATE seller_analysis_recommendations
                    SET status = 'superseded'
                    WHERE brand_id = %s AND action_type = %s AND status = 'proposed'
                      AND product_ids && %s::bigint[] AND id != %s
                    """,
                    (rec.brand_id, rec.action_type, rec.product_ids, rec.id),
                )
            await conn.execute(
                """
                INSERT INTO seller_analysis_recommendations (
                    id, report_id, brand_id, rank, action_type, target_kind, segment_label,
                    product_ids, title, rationale, expected_effect, changes,
                    effectiveness_score, status, applied_at, draft_id, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    rec.id,
                    rec.report_id,
                    rec.brand_id,
                    rec.rank,
                    rec.action_type,
                    rec.target_kind,
                    rec.segment_label,
                    rec.product_ids,
                    rec.title,
                    rec.rationale,
                    rec.expected_effect,
                    Jsonb(rec.changes),
                    rec.effectiveness_score,
                    rec.status,
                    rec.applied_at,
                    rec.draft_id,
                    rec.created_at,
                ),
            )
        return report.id

    result = await _write(_run)
    return result if result is not None else report.id


async def list_reports(
    brand_id: int,
    *,
    limit: int,
    before: datetime | None = None,
    offset: int = 0,
    unread_only: bool = False,
) -> list[ReportRecord]:
    """브랜드의 보고서 목록 — 최신순.

    페이지네이션이 두 벌이다: 채팅 도구(`get_latest_report`)는 `before` 커서를 쓰고,
    R-1 은 화면이 페이지 번호를 그리므로 `offset` 을 쓴다. 둘을 **동시에 주지 않는다** —
    커서와 오프셋을 섞으면 "이전 페이지로" 가 조용히 어긋난다.

    `unread_only` 는 R-1 의 같은 이름 쿼리 파라미터다(`read_at IS NULL`).
    """
    if before is not None and offset:
        raise ValueError("list_reports: before(커서)와 offset 을 함께 쓸 수 없다")
    pool = await _get_pool()
    if pool is None:
        return []

    where = ["brand_id = %s"]
    params: list[Any] = [brand_id]
    if before is not None:
        where.append("created_at < %s")
        params.append(before)
    if unread_only:
        where.append("read_at IS NULL")
    params.extend((limit, offset))

    async def _run() -> list[ReportRecord]:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM seller_analysis_reports "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY created_at DESC LIMIT %s OFFSET %s",
                tuple(params),
            )
            return [_row_to(ReportRecord, cur.description, row) for row in await cur.fetchall()]

    return await run_with_query_timeout(_run())


async def count_reports(brand_id: int, *, unread_only: bool = False) -> int:
    """R-1 `total` — `unread_only` 필터를 **적용한 뒤**의 건수."""
    pool = await _get_pool()
    if pool is None:
        return 0
    clause = " AND read_at IS NULL" if unread_only else ""

    async def _run() -> int:
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT count(*) FROM seller_analysis_reports WHERE brand_id = %s{clause}",
                (brand_id,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    return await run_with_query_timeout(_run())


async def count_unread_reports(brand_id: int) -> int:
    """R-1 `unreadCount` — 목록 배지용이라 **필터와 무관하게 항상 전량 기준**이다.

    `count_reports(unread_only=True)` 와 값은 같지만 의미가 다르다(이쪽은 필터 불문 고정).
    호출부가 둘을 헷갈리지 않도록 이름을 따로 둔다.
    """
    return await count_reports(brand_id, unread_only=True)


@dataclass(frozen=True)
class TargetStatus:
    """`seller_analysis_targets` 1행의 R-1 `noReportReason` 판정 재료.

    `last_run_at`·`last_skip_reason` 은 `06_seller_analysis_ext.sql` 이 추가한 컬럼이라
    마이그레이션 전에는 조회 자체가 실패한다 — `get_target_status()` 가 그 경우 `None` 을
    돌려주고, 호출부는 이유를 **추정하지 않고** `noReportReason: null` 로 응답한다.
    """

    brand_id: int
    last_seen_at: datetime
    last_run_at: datetime | None
    last_skip_reason: str | None


async def get_target_status(brand_id: int) -> TargetStatus | None:
    """등록 여부·마지막 접속·마지막 실행 기록. 미등록이면 `None`.

    ⚠️ 반환 `None` 은 **두 가지**를 뜻한다 — 미등록(정상 판정: `not_registered`)과
    조회 불가(컬럼 부재·DB 장애). 호출부가 이 둘을 구분할 수 없으므로 R-1 은 보수적으로
    간다: 조회가 예외로 끝난 경우를 별도로 잡아 `null` 을 낸다(§보수적 폴백 규약).
    """
    pool = await _get_pool()
    if pool is None:
        return None

    async def _run() -> TargetStatus | None:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT brand_id, last_seen_at, last_run_at, last_skip_reason "
                "FROM seller_analysis_targets WHERE brand_id = %s",
                (brand_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return TargetStatus(
                brand_id=row[0], last_seen_at=row[1], last_run_at=row[2], last_skip_reason=row[3]
            )

    return await run_with_query_timeout(_run())


async def count_recommendations_by_reports(
    report_ids: Sequence[UUID], *, brand_id: int
) -> dict[UUID, int]:
    """보고서별 추천 개수 — R-1 `recommendationCount`.

    목록 한 페이지(최대 100건)마다 `list_recommendations_by_report` 를 부르면 N+1 이 된다.
    한 번의 GROUP BY 로 끝내고, 추천이 0건인 보고서는 키가 없으므로 호출부가 0 으로 읽는다.
    """
    if not report_ids:
        return {}
    pool = await _get_pool()
    if pool is None:
        return {}

    async def _run() -> dict[UUID, int]:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT report_id, count(*) FROM seller_analysis_recommendations "
                "WHERE brand_id = %s AND report_id = ANY(%s) GROUP BY report_id",
                (brand_id, list(report_ids)),
            )
            return {row[0]: int(row[1]) for row in await cur.fetchall()}

    return await run_with_query_timeout(_run())


async def get_report(report_id: UUID, *, brand_id: int) -> ReportRecord | None:
    """`brand_id` 필수 — id 만으로 조회하면 남의 브랜드 데이터가 열린다(IDOR)."""
    pool = await _get_pool()
    if pool is None:
        return None

    async def _run() -> ReportRecord | None:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM seller_analysis_reports WHERE id = %s AND brand_id = %s",
                (report_id, brand_id),
            )
            row = await cur.fetchone()
            return _row_to(ReportRecord, cur.description, row) if row else None

    return await run_with_query_timeout(_run())


async def mark_report_read(report_id: UUID, *, brand_id: int) -> None:
    async def _run(conn: AsyncConnection) -> None:
        await conn.execute(
            "UPDATE seller_analysis_reports SET read_at = now() WHERE id = %s AND brand_id = %s",
            (report_id, brand_id),
        )

    await _write(_run)


async def count_reports_today(brand_id: int) -> int:
    """F-9 일 상한 판정 재료 — 오늘(서버 기준 자정 이후) 생성된 보고서 수."""
    pool = await _get_pool()
    if pool is None:
        return 0

    async def _run() -> int:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM seller_analysis_reports "
                "WHERE brand_id = %s AND created_at >= date_trunc('day', now())",
                (brand_id,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    return await run_with_query_timeout(_run())


# ── 추천 ─────────────────────────────────────────────────────────────────────────


async def get_recommendation(rec_id: UUID, *, brand_id: int) -> RecommendationRecord | None:
    pool = await _get_pool()
    if pool is None:
        return None

    async def _run() -> RecommendationRecord | None:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM seller_analysis_recommendations WHERE id = %s AND brand_id = %s",
                (rec_id, brand_id),
            )
            row = await cur.fetchone()
            return _row_to(RecommendationRecord, cur.description, row) if row else None

    return await run_with_query_timeout(_run())


async def list_recommendations_by_report(
    report_id: UUID, *, brand_id: int
) -> list[RecommendationRecord]:
    """보고서 1건에 딸린 추천 전체 — `rank` 순(= "N번"의 저장 측 근거).

    `brand_id` 필수 — report_id 만으로 조회하면 남의 브랜드 데이터가 열린다(IDOR,
    다른 조회 API와 동일 규약). 07 결정 49 — `apply_recommendation`이 "N번"을 풀 때 쓴다.
    [#591] `get_latest_report` 도구도 같은 함수를 쓴다 — 채팅에서 본 번호와 "N번 적용해줘"가
    푸는 번호가 갈리면 안 되므로 조회 경로를 하나로 둔다.
    """
    pool = await _get_pool()
    if pool is None:
        return []

    async def _run() -> list[RecommendationRecord]:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM seller_analysis_recommendations "
                "WHERE report_id = %s AND brand_id = %s ORDER BY rank",
                (report_id, brand_id),
            )
            return [
                _row_to(RecommendationRecord, cur.description, row) for row in await cur.fetchall()
            ]

    return await run_with_query_timeout(_run())


async def find_by_draft_id(draft_id: str) -> RecommendationRecord | None:
    """draft_id(AI 발급 단명 토큰)로 조회 — `brand_id` 필터가 없는 유일한 조회 API.

    호출부가 반드시 `SellerContext.brand_id`로 결과를 재확인해야 한다(IDOR 방지) —
    draft_id 는 AI 가 발급한 단명 토큰이라 이 조회 자체는 신원 판정이 아니다. 07 결정 49,
    `ix_sarec_draft` 인덱스를 탄다.
    """
    pool = await _get_pool()
    if pool is None:
        return None

    async def _run() -> RecommendationRecord | None:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM seller_analysis_recommendations WHERE draft_id = %s",
                (draft_id,),
            )
            row = await cur.fetchone()
            return _row_to(RecommendationRecord, cur.description, row) if row else None

    return await run_with_query_timeout(_run())


async def mark_recommendation_applied(rec_id: UUID, *, brand_id: int, draft_id: str | None) -> None:
    async def _run(conn: AsyncConnection) -> None:
        await conn.execute(
            "UPDATE seller_analysis_recommendations "
            "SET status = 'applied', applied_at = now(), draft_id = COALESCE(%s, draft_id) "
            "WHERE id = %s AND brand_id = %s",
            (draft_id, rec_id, brand_id),
        )

    await _write(_run)


async def expire_recommendations(older_than_days: int, *, batch_size: int) -> int:
    """`proposed` 추천 중 `created_at`이 `older_than_days` 지난 것을 `expired`로 전이한다.

    무인 정리 배치 ④단계(08-PERSISTENCE.md §5, 결정 68) — `status`가 이미 `applied`·
    `superseded`인 행은 건드리지 않는다(둘 다 "이미 처리됨"이라 만료 대상이 아니다).
    `batch_size`(`seller_cleanup_batch_size`)로 1회 UPDATE 행 수를 제한해 락 보유 시간을
    억제한다 — 서브쿼리로 대상 id를 먼저 좁혀 `LIMIT`을 적용한다(UPDATE 자체는 LIMIT을
    지원하지 않는다).
    """

    async def _run(conn: AsyncConnection) -> int:
        cur = await conn.execute(
            """
            UPDATE seller_analysis_recommendations
            SET status = 'expired'
            WHERE id IN (
                SELECT id FROM seller_analysis_recommendations
                WHERE status = 'proposed'
                  AND created_at < now() - make_interval(days => %s)
                LIMIT %s
            )
            """,
            (older_than_days, batch_size),
        )
        return cur.rowcount

    result = await _write(_run)
    return result if result is not None else 0


# ── 성과 측정 ───────────────────────────────────────────────────────────────────


async def save_outcome(record: OutcomeRecord) -> UUID:
    """UPSERT (rec_id, metric_key)."""

    async def _run(conn: AsyncConnection) -> UUID:
        cur = await conn.execute(
            """
            INSERT INTO seller_analysis_outcomes (
                id, rec_id, brand_id, product_id, action_type, applied_at, measured_at,
                metric_key, window_days, outcome_spec_version,
                treated_pre_succ, treated_pre_trials, treated_post_succ, treated_post_trials,
                control_pre_succ, control_pre_trials, control_post_succ, control_post_trials,
                control_products, delta_pp, p_value, verdict, confounders
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s
            )
            ON CONFLICT (rec_id, metric_key) DO UPDATE SET
                product_id = EXCLUDED.product_id,
                applied_at = EXCLUDED.applied_at,
                measured_at = EXCLUDED.measured_at,
                window_days = EXCLUDED.window_days,
                outcome_spec_version = EXCLUDED.outcome_spec_version,
                treated_pre_succ = EXCLUDED.treated_pre_succ,
                treated_pre_trials = EXCLUDED.treated_pre_trials,
                treated_post_succ = EXCLUDED.treated_post_succ,
                treated_post_trials = EXCLUDED.treated_post_trials,
                control_pre_succ = EXCLUDED.control_pre_succ,
                control_pre_trials = EXCLUDED.control_pre_trials,
                control_post_succ = EXCLUDED.control_post_succ,
                control_post_trials = EXCLUDED.control_post_trials,
                control_products = EXCLUDED.control_products,
                delta_pp = EXCLUDED.delta_pp,
                p_value = EXCLUDED.p_value,
                verdict = EXCLUDED.verdict,
                confounders = EXCLUDED.confounders
            RETURNING id
            """,
            (
                record.id,
                record.rec_id,
                record.brand_id,
                record.product_id,
                record.action_type,
                record.applied_at,
                record.measured_at,
                record.metric_key,
                record.window_days,
                record.outcome_spec_version,
                record.treated_pre_succ,
                record.treated_pre_trials,
                record.treated_post_succ,
                record.treated_post_trials,
                record.control_pre_succ,
                record.control_pre_trials,
                record.control_post_succ,
                record.control_post_trials,
                record.control_products,
                record.delta_pp,
                record.p_value,
                record.verdict,
                Jsonb(record.confounders),
            ),
        )
        row = await cur.fetchone()
        return row[0] if row else record.id

    result = await _write(_run)
    return result if result is not None else record.id


async def list_outcomes(
    brand_id: int, *, action_type: str | None = None, verdict: str | None = None
) -> list[OutcomeRecord]:
    pool = await _get_pool()
    if pool is None:
        return []

    async def _run() -> list[OutcomeRecord]:
        async with pool.connection() as conn:
            clauses = ["brand_id = %s"]
            params: list[Any] = [brand_id]
            if action_type is not None:
                clauses.append("action_type = %s")
                params.append(action_type)
            if verdict is not None:
                clauses.append("verdict = %s")
                params.append(verdict)
            where = " AND ".join(clauses)
            cur = await conn.execute(
                f"SELECT * FROM seller_analysis_outcomes WHERE {where} ORDER BY measured_at DESC",  # noqa: S608 - clauses 는 고정 컬럼명 리터럴만 조립
                params,
            )
            return [_row_to(OutcomeRecord, cur.description, row) for row in await cur.fetchall()]

    return await run_with_query_timeout(_run())
