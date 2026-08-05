"""psycopg_pool 워커의 취소 삼킴 — 이슈 #208 무한 대기의 상류 전제.

`AsyncConnectionPool.worker()` 는 `await task.run()` 을 `except CLIENT_EXCEPTIONS` 로 감싸는데,
async 빌드에서 그 튜플은 `(Exception, asyncio.CancelledError)` 다(psycopg_pool/pool_async.py).
그래서 유지보수 태스크를 **실행 중인** 워커를 취소하면 `CancelledError` 가 삼켜지고 워커는
`await q.get()` 으로 되돌아간다 — 그 태스크는 불사가 된다.

이벤트 루프가 닫힐 때 `asyncio.Runner.close()` 는 `_cancel_all_tasks()` 를 부르는데, 이 함수는
태스크 목록을 **한 번만** 취소하고 무기한 `gather()` 로 기다린다. 삼킨 워커가 하나라도 남아
있으면 그 gather 는 영원히 반환하지 않는다 — `-m integration` 이 간헐적으로 매달린 실제 원인이다.

이 테스트가 깨지면 psycopg_pool 이 이 동작을 고친 것이다. 그때는 `close_pool()`/`close_store()`
호출을 걷어낼지 재검토할 수 있다(그래도 커넥션 회수 자체는 여전히 필요하다).
"""

from __future__ import annotations

import asyncio

from psycopg_pool.pool_async import AsyncConnectionPool

# 워커가 취소를 삼킨 뒤 `q.get()` 으로 되돌아가기까지 루프에 주는 여유. 삼킴은 즉시 일어나므로
# 넉넉히 잡아도 실제 대기는 없다 — 태스크가 정상 종료하면 wait 가 곧바로 반환한다.
_SETTLE_S = 0.5


class _NeverEndingTask:
    """유지보수 태스크 대역 — 워커가 `task.run()` 안에 머무는 창을 결정론적으로 만든다."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        await asyncio.Event().wait()  # 취소로만 벗어난다


async def test_pool_worker_swallows_cancellation_while_running_a_task() -> None:
    queue: asyncio.Queue = asyncio.Queue()
    worker = asyncio.create_task(AsyncConnectionPool.worker(queue))
    task = _NeverEndingTask()
    queue.put_nowait(task)
    await asyncio.wait_for(task.started.wait(), timeout=_SETTLE_S)

    worker.cancel()
    await asyncio.wait([worker], timeout=_SETTLE_S)

    assert not worker.done(), "psycopg_pool 워커가 취소에 응답했다 — #208 전제가 바뀌었다"
    assert worker.cancelling() == 1  # 취소 요청은 남아 있는데도 태스크는 살아 있다

    # 정리 — 이제 `q.get()` 에 park 했으니 두 번째 취소는 먹는다. 루프 teardown 이
    # `_cancel_all_tasks()` 로 **한 번만** 취소한다는 점이 바로 #208 의 교착 조건이다.
    worker.cancel()
    await asyncio.wait([worker], timeout=_SETTLE_S)
    assert worker.cancelled()


async def test_pool_worker_parked_on_queue_does_respond_to_cancellation() -> None:
    """대비군 — 큐에서 대기 중인 워커는 정상 취소된다. 그래서 #208 이 간헐적이었다."""
    queue: asyncio.Queue = asyncio.Queue()
    worker = asyncio.create_task(AsyncConnectionPool.worker(queue))
    await asyncio.sleep(0)  # 워커가 q.get() 에 park 하게

    worker.cancel()
    await asyncio.wait([worker], timeout=_SETTLE_S)

    assert worker.cancelled()
