"""pg 풀은 자기를 만든 이벤트 루프 안에서 닫힌다 (이슈 #208) — 실 pg-profile 필요.

`docker compose up -d pg-profile` 로 컨테이너가 떠 있어야 통과한다. 기본 pytest 실행에서는
@pytest.mark.integration 으로 제외된다(pyproject.toml addopts).

배경: 이 저장소의 pg 모듈들은 sync 리셋터(`set_pool(None)`·`reset()`)에서 await 할 수 없어
풀 close 를 "다음 async 진입"으로 미룬다. 그 결과 테스트가 끝나는 시점에 **살아 있는 풀**이
곧 파괴될 이벤트 루프에 남고, 루프 teardown 의 `_cancel_all_tasks()` 가 psycopg_pool 의
취소 삼킴 워커(tests/unit/test_pool_worker_cancellation.py)와 만나면 영원히 반환하지 않는다.

따라서 각 모듈은 **자기 루프에서 풀을 닫는 async API** 를 제공해야 하고, 테스트 하니스는
매 테스트 teardown 에서 그것을 부른다(tests/conftest.py `_close_pg_pools_on_this_loop`).
여기서는 그 API 들이 실제로 백그라운드 태스크를 남기지 않는지 실 DB 로 검증한다.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest

from app.agents.profile import graph_journal, processed_events, session_activity
from app.agents.profile import store as profile_store_module
from app.agents.seller import analysis_store
from app.core import conversation, pg_store, session_context
from tests.conftest import close_pg_pools_on_loop, pg_pool_tasks

pytestmark = pytest.mark.integration


async def _open_processed_events() -> None:
    processed_events.set_pool(None)
    event_id = f"it-evt-{uuid.uuid4().hex}"
    await processed_events.mark_if_new(event_id)
    await processed_events.unmark_event(event_id)


async def _open_seller_analysis() -> None:
    analysis_store.set_pool(None)
    assert await analysis_store._get_pool() is not None


async def _open_session_activity() -> None:
    # `reset()` 이 아니라 `set_pool(None)` — reset 은 InMemory 폴백(`_fallback_rows = {}`)을
    # 꽂아 실 pg 경로를 아예 타지 않는다.
    session_activity.set_pool(None)
    await session_activity.touch_session(208, f"it-sess-{uuid.uuid4().hex}")


async def _open_graph_journal() -> None:
    # `reset()` 이 아니라 `set_pool(None)` — reset 은 InMemory 폴백을 꽂아 실 pg 경로를 안 탄다.
    graph_journal.set_pool(None)
    assert await graph_journal._get_pool() is not None


async def _open_conversation() -> None:
    conversation.set_store(None)
    await conversation.get_conversation_store()


async def _open_pg_store() -> None:
    pg_store.set_store(None)
    await pg_store.get_store()


async def _open_profile_store() -> None:
    profile_store_module.set_store(None)
    await profile_store_module.get_profile_store()


async def _open_session_context() -> None:
    session_context.reset()
    await session_context.initialize()


_PG_MODULES = [
    pytest.param(_open_seller_analysis, analysis_store, "close_pool", id="seller_analysis"),
    pytest.param(_open_processed_events, processed_events, "close_pool", id="processed_events"),
    pytest.param(_open_session_activity, session_activity, "close_pool", id="session_activity"),
    pytest.param(_open_graph_journal, graph_journal, "close_pool", id="graph_journal"),
    pytest.param(_open_conversation, conversation, "close_store", id="conversation"),
    pytest.param(_open_pg_store, pg_store, "close_store", id="pg_store"),
    pytest.param(_open_profile_store, profile_store_module, "close_store", id="profile_store"),
    pytest.param(
        _open_session_context, session_context, "close_session_lifecycle", id="session_context"
    ),
]


@pytest.mark.parametrize(("open_pool", "module", "close_name"), _PG_MODULES)
async def test_module_close_leaves_no_pool_tasks_on_this_loop(
    open_pool: Callable[[], Awaitable[None]], module: object, close_name: str
) -> None:
    """모듈의 async close 는 이 루프의 풀 백그라운드 태스크를 남기지 않는다."""
    await open_pool()
    assert pg_pool_tasks(), "실 pg 풀이 열리지 않았다 — 전제가 깨졌다(폴백을 탔는지 확인)"

    close: Callable[[], Awaitable[None]] = getattr(module, close_name)
    await close()

    assert pg_pool_tasks() == []


async def test_harness_close_covers_every_pg_module() -> None:
    """하니스 teardown 훅 하나가 모든 pg 모듈의 풀을 이 루프에서 닫는다.

    모듈을 새로 추가하고 훅에 배선하지 않으면 여기서 잡힌다 — 그 누락이 곧 CI 무한 대기다.
    """
    for open_pool, _module, _close_name in (p.values for p in _PG_MODULES):
        await open_pool()
    assert pg_pool_tasks(), "실 pg 풀이 열리지 않았다 — 전제가 깨졌다(폴백을 탔는지 확인)"

    await close_pg_pools_on_loop()

    assert pg_pool_tasks() == []
