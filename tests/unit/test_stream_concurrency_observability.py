"""이슈 #476 — 활성 스트림 동시성 관측의 실제 수명주기 검증."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi import HTTPException

from app.core.auth import Identity
from app.core.conversation import TurnStatus, get_conversation_store
from app.core.observability import start_observation
from app.core.stream import ActiveStreamRegistry, get_registry, open_stream
from app.core.tracing import FakeTraceExporter, TraceFactory, bind_request_trace
from app.schemas.spring import ProductSearchFilters, ProductSearchResult, SpringProduct
from app.services import search_service, spring_client


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


class _FailingObservation:
    """관측 실패가 스트림 수명주기·슬롯 정리에 영향을 주지 않아야 한다."""

    request_id = "failing-observation-476"
    buyer_session = None
    trace = None

    def note_active_streams(self, _count: int) -> None:
        raise RuntimeError("observation unavailable")

    async def commit_user_message(self) -> None:
        return None

    def record_frame(self, _frame: str, _now: float) -> None:
        return None

    async def finish(self, *_args, **_kwargs) -> None:
        return None


async def _observation(conversation_id: str, *, trace=None):
    return start_observation(
        request_id=f"req-{conversation_id}",
        identity=Identity(
            user_id="member-476", is_guest=False, seller_id=None, subject="member-476"
        ),
        conversation_id=conversation_id,
        message="동시성 관측",
        store=await get_conversation_store(),
        now=asyncio.get_running_loop().time(),
        trace=trace,
    )


def _chat_records(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "observability" and record.getMessage().startswith("{")
    ]


async def test_active_count_tracks_slots_without_counting_fences() -> None:
    registry = ActiveStreamRegistry()

    assert registry.active_count() == 0
    assert await registry.acquire("stream-a")
    assert registry.active_count() == 1
    assert await registry.acquire("stream-b")
    assert registry.active_count() == 2
    await registry.release("stream-a")
    await registry.release("stream-a")
    assert registry.active_count() == 1
    assert await registry.acquire_fence("owner", "session") is not None
    assert registry.active_count() == 1


async def test_observation_failure_does_not_leak_stream_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stream_key = "member-476:observation-failure"

    async def done(_turn_started_at: float):
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    with caplog.at_level(logging.WARNING, logger="app.core.stream"):
        response = await open_stream(
            _FakeRequest(), stream_key, done, observer=_FailingObservation()
        )
        chunks = [chunk async for chunk in response.body_iterator]

    assert chunks
    assert not get_registry().is_active(stream_key)
    assert "ACTIVE_STREAM_OBSERVATION_FAILED" in caplog.text


async def test_normal_turn_logs_active_stream_count(caplog: pytest.LogCaptureFixture) -> None:
    observer = await _observation("normal-476")

    async def done(_turn_started_at: float):
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    with caplog.at_level(logging.INFO, logger="observability"):
        response = await open_stream(_FakeRequest(), "member-476:normal", done, observer=observer)
        _ = [chunk async for chunk in response.body_iterator]

    [record] = [record for record in _chat_records(caplog) if record["event"] == "chat_request"]
    assert record["activeStreams"] >= 1
    assert record["activeStreamsPeak"] >= record["activeStreams"]


async def test_rejected_turn_logs_preexisting_active_stream_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = get_registry()
    assert await registry.acquire("member-476:busy")
    observer = await _observation("rejected-476")

    async def unused(_turn_started_at: float):
        yield "data: unused\n\n"

    try:
        with caplog.at_level(logging.INFO, logger="observability"):
            with pytest.raises(HTTPException) as exc_info:
                await open_stream(_FakeRequest(), "member-476:busy", unused, observer=observer)
    finally:
        await registry.release("member-476:busy")

    assert exc_info.value.status_code == 409
    [record] = [record for record in _chat_records(caplog) if record["event"] == "chat_request"]
    assert record["errorType"] == "STREAM_IN_PROGRESS"
    assert record["activeStreams"] == 1
    assert record["activeStreamsPeak"] == 1


async def test_turn_peak_samples_stream_opened_after_acquire(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A의 두 번째 프레임 대기 중 B를 열어, 루프 표본이 실제 피크를 잡아야 한다."""
    release_a = asyncio.Event()
    release_b = asyncio.Event()
    observer_a = await _observation("peak-a-476")
    observer_b = await _observation("peak-b-476")

    async def stream_a(_turn_started_at: float):
        yield 'data: {"type":"token","data":{"text":"A-1"}}\n\n'
        await release_a.wait()
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    async def stream_b(_turn_started_at: float):
        yield 'data: {"type":"token","data":{"text":"B-1"}}\n\n'
        await release_b.wait()
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    with caplog.at_level(logging.INFO, logger="observability"):
        response_a = await open_stream(
            _FakeRequest(), "member-476:peak-a", stream_a, observer=observer_a
        )
        response_b = await open_stream(
            _FakeRequest(), "member-476:peak-b", stream_b, observer=observer_b
        )
        release_a.set()
        _ = [chunk async for chunk in response_a.body_iterator]
        release_b.set()
        _ = [chunk async for chunk in response_b.body_iterator]

    records = _chat_records(caplog)
    record_a = next(record for record in records if record["requestId"] == "req-peak-a-476")
    assert record_a["activeStreams"] == 1
    assert record_a["activeStreamsPeak"] > record_a["activeStreams"]


async def test_turn_peak_samples_first_token_wait_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A가 첫 프레임 전에 기다리는 사이 B가 열리면 A의 피크가 올라야 한다."""
    release_a = asyncio.Event()
    observer_a = await _observation("first-wait-a-476")
    observer_b = await _observation("first-wait-b-476")

    async def stream_a(_turn_started_at: float):
        await release_a.wait()
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    async def stream_b(_turn_started_at: float):
        yield 'data: {"type":"done","data":{"finishReason":"stop"}}\n\n'

    open_a = asyncio.create_task(
        open_stream(_FakeRequest(), "member-476:first-wait-a", stream_a, observer=observer_a)
    )
    while not get_registry().is_active("member-476:first-wait-a"):
        await asyncio.sleep(0)

    with caplog.at_level(logging.INFO, logger="observability"):
        response_b = await open_stream(
            _FakeRequest(), "member-476:first-wait-b", stream_b, observer=observer_b
        )
        release_a.set()
        response_a = await open_a
        _ = [chunk async for chunk in response_a.body_iterator]
        _ = [chunk async for chunk in response_b.body_iterator]

    records = _chat_records(caplog)
    record_a = next(record for record in records if record["requestId"] == "req-first-wait-a-476")
    assert record_a["activeStreams"] == 1
    assert record_a["activeStreamsPeak"] > record_a["activeStreams"]


def _trace(request_id: str):
    return TraceFactory(
        exporter=FakeTraceExporter(), enabled=True, sampling_rate=1.0
    ).start_request(
        name="buyer_chat_turn",
        request_id=request_id,
        conversation_id=f"session-{request_id}",
        thread_id=f"thread-{request_id}",
        lane="recommend",
        environment="test",
    )


async def test_chat_request_leaves_search_fields_empty_without_search(
    caplog: pytest.LogCaptureFixture,
) -> None:
    observation = await _observation("no-search-476", trace=_trace("no-search-476"))

    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(asyncio.get_running_loop().time(), status=TurnStatus.COMPLETED)

    [record] = [record for record in _chat_records(caplog) if record["event"] == "chat_request"]
    assert record["searchCalls"] == 0
    assert record["searchCandidatesMax"] is None
    assert record["searchTotalCountMax"] is None
    assert record["searchElapsedMsMax"] is None


async def test_chat_request_aggregates_search_result_maxima(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """두 실제 backend 호출의 후보·total·지연 최댓값을 한 요청 로그에 합친다."""
    results = iter(
        [
            ProductSearchResult(
                products=[SpringProduct(product_id=1, name="one", price=1)], total_count=3
            ),
            ProductSearchResult(
                products=[
                    SpringProduct(product_id=index, name=str(index), price=1) for index in range(4)
                ],
                total_count=9,
            ),
        ]
    )
    ticks = iter([10.0, 10.025, 20.0, 20.075])

    async def fake_search(_filters: ProductSearchFilters) -> ProductSearchResult:
        return next(results)

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    monkeypatch.setattr(search_service, "monotonic", lambda: next(ticks))
    trace = _trace("search-max-476")
    observation = await _observation("search-max-476", trace=trace)

    with bind_request_trace(trace):
        backend = search_service.SpringSearchBackend()
        await backend.search(ProductSearchFilters(keyword="first"))
        await backend.search(ProductSearchFilters(keyword="second"))

    with caplog.at_level(logging.INFO, logger="observability"):
        await observation.finish(asyncio.get_running_loop().time(), status=TurnStatus.COMPLETED)

    [record] = [record for record in _chat_records(caplog) if record["event"] == "chat_request"]
    assert record["searchCalls"] == 2
    assert record["searchCandidatesMax"] == 4
    assert record["searchTotalCountMax"] == 9
    assert record["searchElapsedMsMax"] == 75


async def test_search_observation_failure_does_not_fail_search(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenTrace:
        def record_search_result(self, *_args) -> None:
            raise RuntimeError("observation unavailable")

    expected = ProductSearchResult(
        products=[SpringProduct(product_id=1, name="one", price=1)], total_count=1
    )

    async def fake_search(_filters: ProductSearchFilters) -> ProductSearchResult:
        return expected

    monkeypatch.setattr(spring_client, "search_products", fake_search)
    monkeypatch.setattr(search_service, "current_request_trace", lambda: BrokenTrace())

    with caplog.at_level(logging.WARNING, logger="app.services.search_service"):
        result = await search_service.SpringSearchBackend().search(
            ProductSearchFilters(keyword="safe")
        )

    assert result is expected
    assert "SEARCH_RESULT_OBSERVATION_FAILED" in caplog.text
