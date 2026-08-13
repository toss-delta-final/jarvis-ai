"""판매자 공용 checkpointer 싱글턴 (pg-profile AsyncPostgresSaver — 대화 스레드 배선).

hitl.py(4-2) 전용이던 checkpointer 싱글턴을 분리했다. 이제 두 소비자가 나눠 쓴다:
- HITL draft 스레드: ``seller-draft:{draftId}`` (hitl.py — 승인 대기 interrupt/resume)
- 채팅 대화 스레드: ``seller-chat:{sellerId}:{threadId}`` (thread.py — 멀티턴 누적)
같은 checkpoints 테이블(pg-profile)에 공존하되 thread_id 접두로 구분되어 충돌이 없다.

dev 폴백 규약(4-2 확정, 2026-07-20)은 그대로다: 연결 실패 시 InMemorySaver + 경고
1회(service_token dev 스킵 선례), 운영(auth_mode=jwks)은 폴백 금지 — 프로세스 재시작
시 draft·대화 맥락 증발은 운영에서 허용 불가.

[known-risk] from_conn_string 은 pool 이 아니라 단일 AsyncConnection 을 연다(현재
langgraph-checkpoint-postgres 계약). HITL 전용일 땐 충분했지만 채팅 hot-path 합류로
병목이 확인되면 AsyncConnectionPool 기반 생성(state_store_pool_config 재사용)으로
전환한다 — 후속 이슈.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.core.config import get_settings
from app.core.pg_resilience import hardened_pg_conninfo

logger = logging.getLogger(__name__)

# 싱글턴 — set_checkpointer(테스트 주입) / 미주입 시 지연 초기화.
_checkpointer: BaseCheckpointSaver | None = None
_checkpointer_ctx: object | None = None  # AsyncPostgresSaver cm — 앱 수명 동안 GC 방지
_fallback_warned = False
_pending_cleanup: list[object] = []

# 콜드스타트 초기화 직렬화 락(PR #182 리뷰) — 모듈 로드 시점 생성 금지: 테스트가
# asyncio.run 반복으로 루프를 바꾸면 Lock 의 루프 바인딩(3.12)이 깨진다. 지연 생성하고
# set_checkpointer 에서 함께 리셋한다.
_init_lock: asyncio.Lock | None = None


def _get_init_lock() -> asyncio.Lock:
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


# 소비자(hitl 그래프, thread recorder 그래프)가 checkpointer 로 컴파일한 그래프 캐시를
# 갖는다 — checkpointer 교체 시 함께 무효화해야 stale 그래프가 옛 저장소를 잡고 있지
# 않는다. 소비자가 모듈 import 시점에 자기 초기화 함수를 등록한다.
_reset_hooks: list[Callable[[], None]] = []


def register_reset_hook(hook: Callable[[], None]) -> None:
    """set_checkpointer 시 함께 초기화할 소비자 캐시 무효화 함수를 등록한다."""
    _reset_hooks.append(hook)


def set_checkpointer(checkpointer: BaseCheckpointSaver | None) -> None:
    """checkpointer 교체(테스트용) — None 이면 다음 사용 시 재초기화한다.

    등록된 reset hook 을 모두 호출해 소비자의 컴파일된 그래프 캐시도 무효화한다
    (기존 hitl.set_checkpointer 가 _graph·_confirm_locks 를 함께 초기화하던 계약 유지).
    """
    global _checkpointer, _checkpointer_ctx, _init_lock
    old_ctx = _checkpointer_ctx
    _checkpointer = checkpointer
    _checkpointer_ctx = None
    _init_lock = None  # 루프 교체(테스트 asyncio.run 반복) 대비 락도 재생성
    if old_ctx is not None:
        _pending_cleanup.append(old_ctx)
    for hook in _reset_hooks:
        hook()


async def _drain_pending_cleanup(*, propagate_errors: bool = False) -> None:
    """이전 checkpointer ctx를 닫되 현재 태스크의 실제 취소만 다시 전파한다.

    sync `set_checkpointer()`는 ctx를 직접 await-close 할 수 없어 대기열로 넘긴다. 이전
    이벤트 루프에 묶인 stale ctx의 `__aexit__()`가 `CancelledError`를 낼 수 있지만,
    이를 무조건 삼키면 현재 종료 태스크 자체의 실제 취소까지 무시된다.
    `task.cancelling()`으로 둘을 구분해 실제 취소 요청만 다시 던진다
    (`app/core/pg_store.py`와 같은 근거).
    """
    first_error: Exception | None = None
    while _pending_cleanup:
        ctx = _pending_cleanup.pop()
        try:
            await ctx.__aexit__(None, None, None)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
        except Exception as exc:
            logger.warning("seller checkpointer context cleanup failed", exc_info=True)
            if first_error is None:
                first_error = exc
    if propagate_errors and first_error is not None:
        raise first_error


async def close_checkpointer() -> None:
    """지금 열린 checkpointer 단일 연결 ctx를 이 이벤트 루프에서 닫는다 (이슈 #221)."""
    set_checkpointer(None)
    await _drain_pending_cleanup(propagate_errors=True)


async def get_checkpointer() -> BaseCheckpointSaver:
    """공용 checkpointer — 미주입 시 지연 초기화(AsyncPostgresSaver, dev 폴백).

    초기화는 락으로 직렬화한다(PR #182 리뷰) — 콜드스타트에 동시 요청이 몰리면
    _init_checkpointer 가 병렬 실행되어 커넥션 누수(_checkpointer_ctx 덮어쓰기)와
    인스턴스 분열이 생긴다. 바깥 검사(비-None 정상 경로)는 락을 타지 않는다.
    """
    global _checkpointer
    await _drain_pending_cleanup()
    if _checkpointer is None:
        async with _get_init_lock():
            if _checkpointer is None:  # 락 대기 중 다른 코루틴이 끝냈을 수 있다
                _checkpointer = await _init_checkpointer()
    return _checkpointer


async def _init_checkpointer() -> BaseCheckpointSaver:
    """AsyncPostgresSaver(pg-profile) 초기화 — 실패 시 dev 한정 InMemorySaver 폴백.

    운영(auth_mode=jwks)은 폴백 금지 — 재시작 시 상태 증발은 허용 불가(모듈
    docstring). setup() 은 checkpoint 테이블 생성 멱등 호출이다.
    """
    global _checkpointer_ctx, _fallback_warned
    settings = get_settings()
    ctx = None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # 현재 langgraph-checkpoint-postgres의 from_conn_string은 pool_config를 받지 않고
        # 단일 AsyncConnection을 직접 연다. 따라서 BaseStore pool 상한 설정 대상이 아니며,
        # 연결/서버/TCP 제한은 hardened conninfo로 적용한다.
        ctx = AsyncPostgresSaver.from_conn_string(hardened_pg_conninfo(settings.profile_db_url))
        saver = await asyncio.wait_for(
            ctx.__aenter__(), timeout=settings.seller_checkpoint_connect_timeout_s
        )
        # setup()(DDL)도 **동일 상한으로 감싼다** (#266 PR 리뷰) — 이웃인 pg_store.py 가
        # 이미 같은 이유로 그렇게 한다(PR #46 후속 리뷰). 여기만 빠져 있었다.
        # 콜드 DB 에서 setup() 은 MIGRATIONS 8종 + 조회/기록을 **순차 실행**하므로, 앱 상한이
        # 없으면 문장당 statement_timeout(3s)씩 누적돼 이 상수가 뜻하는 5s 를 크게 넘는다.
        # 호출부(_general_stream)는 이 함수 전체가 상한 안에 끝난다고 보고 레인 예산을
        # 계산하므로(config `_require_general_lane_within_stream_cap`), 그 전제를 여기서 지킨다.
        await asyncio.wait_for(saver.setup(), timeout=settings.seller_checkpoint_connect_timeout_s)
        _checkpointer_ctx = ctx
        return saver
    except Exception as exc:
        if ctx is not None:
            # __aenter__ 타임아웃으로 취소된 경우도 포함해 항상 정리를 시도한다 — 좁히면
            # 부분적으로 열린 커넥션이 그대로 샌다(pg_store.py 와 동일 논리). setup() 실패
            # 경로에서도 커넥션이 남지 않게 한다.
            with contextlib.suppress(Exception):
                await ctx.__aexit__(type(exc), exc, exc.__traceback__)
        if settings.auth_mode == "jwks":
            raise  # 운영 — 폴백 금지, 기동/요청 실패로 드러낸다
        if not _fallback_warned:
            logger.warning(
                "pg-profile checkpointer 연결 실패(%s) — InMemorySaver 폴백 "
                "(dev 전용: 프로세스 재시작 시 draft·대화 맥락 증발)",
                exc,
            )
            _fallback_warned = True
        return InMemorySaver()


# ── checkpoint 정리 (이슈 #601, `08-PERSISTENCE.md` §1.1·§5 결정 63) ────────────────
#
# draft 1건이 checkpoint thread 1개를 만들고(`seller-draft:{draftId}`), 상태 갱신마다
# 행이 **늘기만** 한다(`invalidate_draft`도 삭제가 아니라 새 checkpoint를 쓰는 것 —
# 08-PERSISTENCE.md §1.1). 대화 스레드(`seller-chat:{sellerId}:{threadId}`)도 턴마다
# 증가만 한다. 지금까지 이 둘을 지우는 코드가 없었다(§1 실측) — 여기서 신설한다.
#
# LangGraph 표준 스키마(`checkpoints`/`checkpoint_blobs`/`checkpoint_writes`, 전부
# `thread_id` 컬럼 보유)를 **직접** 지운다. `checkpoint_blobs`는 `checkpoint_id`가 없어
# (channel+version이 키) 개별 checkpoint 단위로 나이를 잴 수 없으므로, **스레드 단위로
# 통째로** 지운다 — 대상 판정은 `checkpoints.checkpoint->>'ts'`(ISO8601, BaseCheckpointSaver
# 계약)의 스레드별 최댓값("마지막 활동")이 보존 기간을 넘었는가로 한다. `thread_id`
# 접두어로만 범위를 좁혀 다른 도메인(구매자·프로필) thread는 손대지 않는다
# (08-PERSISTENCE.md §1.1 "다른 도메인 thread를 건드리지 않게 하는 것이 이 규칙의 목적").

_DRAFT_THREAD_PREFIX = "seller-draft:%"
_CHAT_THREAD_PREFIX = "seller-chat:%"

_STALE_THREADS_SQL = """
    SELECT thread_id
    FROM checkpoints
    WHERE thread_id LIKE %s
    GROUP BY thread_id
    HAVING max((checkpoint->>'ts')::timestamptz) < now() - make_interval({unit} => %s)
    LIMIT %s
"""


async def _delete_stale_threads(
    conn, *, prefix: str, unit: str, retention: int, batch_size: int
) -> int:
    """`prefix`에 걸리는 스레드 중 마지막 활동이 `retention`(`unit` 단위) 지난 것을 지운다.

    `checkpoints`에서 대상 `thread_id` 집합을 먼저 좁힌 뒤(최대 `batch_size`개), 그 집합으로
    세 테이블을 각각 지운다 — 세 테이블이 FK로 묶여 있지 않아(LangGraph 스키마 자체 규약)
    코드가 원자성을 대신 챙긴다. `checkpoints` 삭제 행 수를 반환값으로 쓴다(이 함수의
    "지운 checkpoint 수"는 대화·draft가 실제로 쌓아온 상태 스냅샷 개수를 뜻한다).
    """
    # AsyncPostgresSaver.from_conn_string 이 row_factory=dict_row·autocommit=True 로 연다
    # (langgraph 계약) — 행은 튜플이 아니라 dict 이고, execute() 마다 즉시 커밋되므로 여기서
    # 별도 트랜잭션을 열지 않는다.
    sql = _STALE_THREADS_SQL.format(unit=unit)
    cur = await conn.execute(sql, (prefix, retention, batch_size))
    thread_ids = [row["thread_id"] for row in await cur.fetchall()]
    if not thread_ids:
        return 0
    deleted = 0
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        result = await conn.execute(
            f"DELETE FROM {table} WHERE thread_id = ANY(%s)",  # noqa: S608 - table은 고정 리터럴 3종
            (thread_ids,),
        )
        if table == "checkpoints":
            deleted = result.rowcount
    return deleted


async def cleanup_expired_checkpoints(
    *, draft_retention_hours: int, thread_retention_days: int, batch_size: int
) -> tuple[int, int]:
    """draft(`seller-draft:`) 48시간 · 대화(`seller-chat:`) 30일 checkpoint 정리 1회.

    `(draft_checkpoints_deleted, thread_checkpoints_deleted)`를 반환한다. checkpointer가
    `AsyncPostgresSaver`가 아니면(dev InMemorySaver 폴백) 지울 대상이 없으므로 `(0, 0)`을
    돌려준다 — 무인 배치 정리 단계(`cleanup.run_cleanup_batch`)의 ⑤가 이 함수를 부른다.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    saver = await get_checkpointer()
    if not isinstance(saver, AsyncPostgresSaver):
        logger.info("checkpoint 정리 스킵 — checkpointer 가 pg-profile 이 아니다(dev 폴백)")
        return (0, 0)

    conn = saver.conn
    draft_deleted = await _delete_stale_threads(
        conn,
        prefix=_DRAFT_THREAD_PREFIX,
        unit="hours",
        retention=draft_retention_hours,
        batch_size=batch_size,
    )
    thread_deleted = await _delete_stale_threads(
        conn,
        prefix=_CHAT_THREAD_PREFIX,
        unit="days",
        retention=thread_retention_days,
        batch_size=batch_size,
    )
    return (draft_deleted, thread_deleted)
