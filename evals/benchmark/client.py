"""HTTP/SSE 블랙박스 클라이언트와 monotonic 지연 계측."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

_MISSING_REASONS = {
    "no_token_event",
    "error_event",
    "http_error",
    "connection_error",
    "client_timeout",
    "empty_stream",
}


def _decode_frame(lines: list[str]) -> dict[str, Any] | None:
    """SSE 프레임의 data 줄을 JSON 객체로 디코딩한다."""
    data = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
    if not data.strip():
        return None
    try:
        value = json.loads(data)
    except json.JSONDecodeError:
        return {"type": "unknown", "data": {}}
    return value if isinstance(value, dict) else {"type": "unknown", "data": {}}


async def measure_request(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """요청 직전부터 스트림 종료까지 재고 token TTFT를 별도로 고정한다."""
    started = clock()
    first_event_at: float | None = None
    first_token_at: float | None = None
    event_types: list[str] = []
    request_id: str | None = None
    missing_reason: str | None = None
    status_code: int | None = None
    try:
        async with client.stream("POST", endpoint, json=payload, headers=headers) as response:
            status_code = response.status_code
            request_id = response.headers.get("X-Request-Id")
            if response.status_code >= 400:
                await response.aread()
                missing_reason = "http_error"
            else:
                frame_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line:
                        frame_lines.append(line)
                        continue
                    frame = _decode_frame(frame_lines)
                    frame_lines = []
                    if frame is None:
                        continue
                    now = clock()
                    if first_event_at is None:
                        first_event_at = now
                    event_type = str(frame.get("type", "unknown"))
                    event_types.append(event_type)
                    data = frame.get("data")
                    text = data.get("text") if isinstance(data, dict) else None
                    if event_type == "token" and isinstance(text, str) and text.strip():
                        first_token_at = first_token_at or now
                frame = _decode_frame(frame_lines)
                if frame is not None:
                    now = clock()
                    first_event_at = first_event_at or now
                    event_type = str(frame.get("type", "unknown"))
                    event_types.append(event_type)
                    data = frame.get("data")
                    text = data.get("text") if isinstance(data, dict) else None
                    if event_type == "token" and isinstance(text, str) and text.strip():
                        first_token_at = first_token_at or now
                if first_token_at is None:
                    if "error" in event_types:
                        missing_reason = "error_event"
                    elif not event_types:
                        missing_reason = "empty_stream"
                    else:
                        missing_reason = "no_token_event"
    except httpx.TimeoutException:
        missing_reason = "client_timeout"
    except httpx.RequestError:
        missing_reason = "connection_error"
    ended = clock()
    if missing_reason not in _MISSING_REASONS | {None}:
        raise AssertionError("unbounded TTFT missing reason")
    saw_error_event = "error" in event_types
    success = (
        first_token_at is not None
        and bool(event_types)
        and event_types[-1] == "done"
        and not saw_error_event
        and missing_reason is None
    )
    return {
        "request_id": request_id,
        "http_status": status_code,
        "client_ttft_ms": (
            (first_token_at - started) * 1000 if first_token_at is not None else None
        ),
        "ttft_missing_reason": missing_reason,
        "client_first_event_ms": (
            (first_event_at - started) * 1000 if first_event_at is not None else None
        ),
        "client_total_ms": (ended - started) * 1000,
        "terminal_event": event_types[-1] if event_types else None,
        "event_types": event_types,
        "success": success,
        "timed_out": missing_reason == "client_timeout",
        "error": saw_error_event
        or missing_reason in {"error_event", "http_error", "connection_error"},
    }
