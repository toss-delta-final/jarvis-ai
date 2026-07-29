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
    global _checkpointer, _checkpointer_ctx
    _checkpointer = checkpointer
    _checkpointer_ctx = None
    for hook in _reset_hooks:
        hook()


async def get_checkpointer() -> BaseCheckpointSaver:
    """공용 checkpointer — 미주입 시 지연 초기화(AsyncPostgresSaver, dev 폴백)."""
    global _checkpointer
    if _checkpointer is None:
        _checkpointer = await _init_checkpointer()
    return _checkpointer


async def _init_checkpointer() -> BaseCheckpointSaver:
    """AsyncPostgresSaver(pg-profile) 초기화 — 실패 시 dev 한정 InMemorySaver 폴백.

    운영(auth_mode=jwks)은 폴백 금지 — 재시작 시 상태 증발은 허용 불가(모듈
    docstring). setup() 은 checkpoint 테이블 생성 멱등 호출이다.
    """
    global _checkpointer_ctx, _fallback_warned
    settings = get_settings()
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        # 현재 langgraph-checkpoint-postgres의 from_conn_string은 pool_config를 받지 않고
        # 단일 AsyncConnection을 직접 연다. 따라서 BaseStore pool 상한 설정 대상이 아니며,
        # 연결/서버/TCP 제한은 hardened conninfo로 적용한다.
        ctx = AsyncPostgresSaver.from_conn_string(hardened_pg_conninfo(settings.profile_db_url))
        saver = await asyncio.wait_for(
            ctx.__aenter__(), timeout=settings.seller_checkpoint_connect_timeout_s
        )
        await saver.setup()
        _checkpointer_ctx = ctx
        return saver
    except Exception as exc:
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
