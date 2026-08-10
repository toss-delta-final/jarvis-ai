"""#406 — 구매자 본검색 Spring 재시도 `retrying` progress 회귀."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.session_context import BuyerSessionInput
from app.schemas.spring import ProductSearchFilters, ProductSearchResult
from app.services import spring_client
from tests._fakes import DEFAULT_DECOMPOSE, DEFAULT_PRODUCTS, FakeLLM

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> None:
    """T6의 환경 주입이 다음 테스트의 기본 재시도 설정으로 남지 않게 한다."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _member() -> Identity:
    return Identity(user_id="406", is_guest=False, seller_id=None, subject="406")


def _req(thread_id: str) -> SimpleNamespace:
    return SimpleNamespace(session_id="s-406", thread_id=thread_id, message="무선 이어폰 추천해줘")


async def _committed_observer(request, identity):  # noqa: ANN001
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "guest" if identity.is_guest else "member",
            buyer_owner_id(identity, get_settings()),
        )
    )
    return SimpleNamespace(
        context_id=context.context_id,
        request_id="req-406",
        record_model_call=lambda *_: None,
    )


class _RecordingPush:
    async def __call__(self, push) -> bool:  # noqa: ANN001
        return True


async def _run(search, thread_id: str):  # noqa: ANN001
    observer = await _committed_observer(_req(thread_id), _member())
    return _production_run_buyer_turn(
        _req(thread_id),
        _member(),
        observer=observer,
        llm=FakeLLM(decompose=DEFAULT_DECOMPOSE),
        search=search,
        push_fn=_RecordingPush(),
    )


def _event(frame: str) -> dict:
    return json.loads(frame.strip()[len("data:") :].strip())


async def _next_progress(gen, stage: str) -> dict:  # noqa: ANN001
    while True:
        event = _event(await anext(gen))
        if event["type"] == "progress" and event["data"]["stage"] == stage:
            return event


def _retrying_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    monkeypatch.setattr(get_settings(), "spring_max_retries", 1)


async def test_retrying_is_emitted_before_blocked_search_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1 — 재시도 신호는 검색 완료를 기다리지 않고 즉시 스트림으로 나간다."""
    _retrying_enabled(monkeypatch)
    release = asyncio.Event()
    search_returned = asyncio.Event()

    async def search(filters, exclude_product_ids=None):  # noqa: ANN001
        spring_client.notify_search_retry()
        await release.wait()
        search_returned.set()
        return ProductSearchResult(products=DEFAULT_PRODUCTS, total_count=len(DEFAULT_PRODUCTS))

    gen = await _run(search, "retrying-realtime-406")
    retrying = await asyncio.wait_for(_next_progress(gen, "retrying"), timeout=2)
    assert retrying["data"]["message"] == get_settings().progress_retrying_message
    assert not search_returned.is_set(), "검색이 이미 끝난 뒤 나온 frame이면 실시간 신호가 아니다"
    release.set()
    await gen.aclose()


async def test_retrying_is_absent_when_retry_progress_is_not_possible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2 — 기본 재시도 0 경로는 가짜 신호에도 `searching`만 유지하고 `retrying`은 내지 않는다."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    monkeypatch.setattr(get_settings(), "spring_max_retries", 0)

    async def signalled_search(filters, exclude_product_ids=None):  # noqa: ANN001
        spring_client.notify_search_retry()
        return ProductSearchResult(products=DEFAULT_PRODUCTS, total_count=len(DEFAULT_PRODUCTS))

    async def plain_search(filters, exclude_product_ids=None):  # noqa: ANN001
        return ProductSearchResult(products=DEFAULT_PRODUCTS, total_count=len(DEFAULT_PRODUCTS))

    for search, thread_id in (
        (signalled_search, "retrying-off-signal-406"),
        (plain_search, "retrying-off-plain-406"),
    ):
        gen = await _run(search, thread_id)
        events = [_event(frame) async for frame in gen]
        stages = [event["data"]["stage"] for event in events if event["type"] == "progress"]
        assert "searching" in stages
        assert "retrying" not in stages


async def test_retrying_is_emitted_at_most_once_per_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """T3 — fan-out 검색이 콜백을 여러 번 보내도 한 턴의 stage는 하나다."""
    _retrying_enabled(monkeypatch)

    async def search(filters, exclude_product_ids=None):  # noqa: ANN001
        spring_client.notify_search_retry()
        spring_client.notify_search_retry()
        return ProductSearchResult(products=DEFAULT_PRODUCTS, total_count=len(DEFAULT_PRODUCTS))

    gen = await _run(search, "retrying-once-406")
    events = [_event(frame) async for frame in gen]
    assert [event["data"]["stage"] for event in events if event["type"] == "progress"].count(
        "retrying"
    ) == 1


async def test_closing_stream_cancels_blocked_search_without_orphan_task(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T4 — 조기 종료는 기다리지 않고 본검색 task를 취소해 고아를 남기지 않는다."""
    _retrying_enabled(monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def search(filters, exclude_product_ids=None):  # noqa: ANN001
        spring_client.notify_search_retry()
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    preexisting_tasks = asyncio.all_tasks()
    gen = await _run(search, "retrying-cancel-406")
    await asyncio.wait_for(_next_progress(gen, "retrying"), timeout=2)
    assert started.is_set()
    await asyncio.wait_for(gen.aclose(), timeout=2)
    await asyncio.wait_for(cancelled.wait(), timeout=2)
    # 동기 cancel 뒤 하위 gather까지 전파되는 데는 스케줄러 틱이 더 필요할 수 있다. task 자체를
    # await하지 않고 루프만 양보해 #84의 외부 취소 전달 규율을 그대로 검증한다.
    orphaned = []
    for _ in range(10):
        await asyncio.sleep(0)
        orphaned = [
            task
            for task in asyncio.all_tasks() - preexisting_tasks
            if task is not asyncio.current_task() and not task.done()
        ]
        if not orphaned:
            break
    assert not orphaned, f"취소 뒤 남은 검색 task: {orphaned!r}"
    assert "Task exception was never retrieved" not in caplog.text


async def test_retry_observer_context_does_not_leak_after_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5 — create_task 뒤 닫은 ContextVar scope가 yield 뒤 호출자 턴에 새지 않는다."""
    _retrying_enabled(monkeypatch)

    async def search(filters, exclude_product_ids=None):  # noqa: ANN001
        spring_client.notify_search_retry()
        return ProductSearchResult(products=DEFAULT_PRODUCTS, total_count=len(DEFAULT_PRODUCTS))

    gen = await _run(search, "retrying-context-406")
    async for _ in gen:
        pass
    # [#306] `_search_retry_suppressed` 비누수 단언은 그 ContextVar 가 제거되며 함께 빠졌다.
    assert spring_client._search_budget_override.get() is None
    assert spring_client._search_retry_observer.get() is None


@pytest.mark.parametrize(
    ("retries", "responses", "expected"),
    [
        (1, [503, 200], 1),
        (0, [503], 0),
        (1, [400], 0),
        (1, [503, 503], 1),
    ],
)
async def test_search_retry_observer_reports_only_actual_retry_entries(
    monkeypatch: pytest.MonkeyPatch, retries: int, responses: list[int], expected: int
) -> None:
    """T6 — 관측 콜백은 재시도 가능한 실제 다음 시도 진입 직전에만 정확히 한 번씩 돈다."""
    monkeypatch.setattr(get_settings(), "spring_max_retries", retries)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        status = responses[min(calls, len(responses) - 1)]
        calls += 1
        return httpx.Response(status, json={"success": status == 200, "data": []})

    monkeypatch.setattr(
        spring_client,
        "_client",
        lambda *, timeout=None: httpx.AsyncClient(
            base_url="https://spring.test", transport=httpx.MockTransport(handler)
        ),
    )
    observed = 0

    def observe() -> None:
        nonlocal observed
        observed += 1

    with spring_client.observe_search_retry(observe):
        try:
            await spring_client.search_products(ProductSearchFilters(keyword="q"))
        except spring_client.SpringUnavailableError:
            pass

    assert observed == expected
