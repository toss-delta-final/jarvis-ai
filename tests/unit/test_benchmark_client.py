"""MockTransport로 SSE TTFT 경계를 오프라인 검증한다."""

import httpx
import pytest

from evals.benchmark.client import measure_request
from evals.benchmark.stats import summarize_group


@pytest.mark.asyncio
async def test_non_empty_token_is_ttft_and_other_event_is_not() -> None:
    body = (
        b'data: {"type":"conditions","data":{}}\n\n'
        b'data: {"type":"token","data":{"text":"  ok  "}}\n\n'
        b'data: {"type":"done","data":{}}\n\n'
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"X-Request-Id": "rid-1"}, content=body)
    )
    ticks = iter([0.0, 0.1, 0.25, 0.4, 0.5])
    async with httpx.AsyncClient(base_url="http://test", transport=transport) as client:
        result = await measure_request(
            client, endpoint="/chat", payload={}, clock=lambda: next(ticks)
        )
    assert result["request_id"] == "rid-1"
    assert result["client_first_event_ms"] == pytest.approx(100)
    assert result["client_ttft_ms"] == pytest.approx(250)
    assert result["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b'data: {"type":"done","data":{}}\n\n', "no_token_event"),
        (b'data: {"type":"error","data":{}}\n\n', "error_event"),
        (b"", "empty_stream"),
    ],
)
async def test_stream_without_token_is_null_not_zero(body: bytes, reason: str) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    counter = iter(range(10))
    async with httpx.AsyncClient(base_url="http://test", transport=transport) as client:
        result = await measure_request(
            client, endpoint="/chat", payload={}, clock=lambda: float(next(counter))
        )
    assert result["client_ttft_ms"] is None
    assert result["ttft_missing_reason"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b'data: {"type":"token","data":{"text":"ok"}}\n\ndata: {"type":"error","data":{}}\n\n',
        b'data: {"type":"token","data":{"text":"ok"}}\n\ndata: {"type":"done","data":{}}\n\ndata: {"type":"error","data":{}}\n\n',
    ],
)
async def test_error_event_is_failure_even_after_token_or_done(body: bytes) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    counter = iter(range(10))
    async with httpx.AsyncClient(base_url="http://test", transport=transport) as client:
        result = await measure_request(
            client, endpoint="/chat", payload={}, clock=lambda: float(next(counter))
        )
    assert result["error"] is True
    assert result["success"] is False
    result["phase"] = "measured"
    summary = summarize_group(
        [result], elapsed_s=1, p99_min_samples=100, resamples=2, confidence=0.95, seed=1
    )
    assert summary["error_count"] == 1


@pytest.mark.asyncio
async def test_zero_clock_value_is_a_measured_ttft_not_missing() -> None:
    body = b'data: {"type":"token","data":{"text":"ok"}}\n\ndata: {"type":"done","data":{}}\n\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ticks = iter([0.0, 0.0, 1.0, 2.0])
    async with httpx.AsyncClient(base_url="http://test", transport=transport) as client:
        result = await measure_request(
            client, endpoint="/chat", payload={}, clock=lambda: next(ticks)
        )
    assert result["client_ttft_ms"] == 0.0
    assert result["client_first_event_ms"] == 0.0
