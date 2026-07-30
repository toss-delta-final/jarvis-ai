"""Request-scoped in-memory tracing primitives.

The active request and span ancestry live in context variables so asyncio tasks inherit
parentage without sharing a mutable global span stack.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
from typing import Literal, Protocol
from uuid import UUID, uuid4

RunType = Literal["chain", "llm", "tool", "retriever"]
TraceStatus = Literal["COMPLETED", "FAILED", "CANCELLED"]
SafeScalar = str | int | float | bool | None

logger = logging.getLogger(__name__)


@dataclass
class TraceNode:
    id: UUID
    trace_id: UUID
    parent_id: UUID | None
    name: str
    run_type: RunType
    started_at: datetime
    ended_at: datetime | None = None
    metadata: dict[str, SafeScalar] = field(default_factory=dict)
    error_type: str | None = None


class TraceExporter(Protocol):
    async def export(self, nodes: tuple[TraceNode, ...]) -> None:
        raise NotImplementedError


class NoopTraceExporter:
    """Exporter used when no telemetry destination is configured."""

    async def export(self, nodes: tuple[TraceNode, ...]) -> None:
        del nodes


class FakeTraceExporter:
    """In-memory exporter for tests and local verification."""

    def __init__(self) -> None:
        self.exported: list[tuple[TraceNode, ...]] = []

    async def export(self, nodes: tuple[TraceNode, ...]) -> None:
        self.exported.append(nodes)


class RequestTrace:
    """One request's root node and explicitly recorded child spans."""

    def __init__(
        self,
        *,
        exporter: TraceExporter,
        name: str,
        request_id: str,
        conversation_id: str,
        thread_id: str,
        lane: str,
        environment: str,
    ) -> None:
        trace_id = uuid4()
        self._exporter = exporter
        self._root_id = trace_id
        self._nodes = [
            TraceNode(
                id=self._root_id,
                trace_id=trace_id,
                parent_id=None,
                name=name,
                run_type="chain",
                started_at=_utc_now(),
                metadata={
                    "requestId": request_id,
                    "conversationId": conversation_id,
                    "threadId": thread_id,
                    "lane": lane,
                    "environment": environment,
                },
            )
        ]
        self._finished = False

    @property
    def root_id(self) -> UUID:
        return self._root_id

    def _start_span(
        self,
        *,
        name: str,
        run_type: RunType,
        metadata: dict[str, SafeScalar] | None,
        parent_id: UUID,
    ) -> TraceNode | None:
        if self._finished:
            return None
        node = TraceNode(
            id=uuid4(),
            trace_id=self._nodes[0].trace_id,
            parent_id=parent_id,
            name=name,
            run_type=run_type,
            started_at=_utc_now(),
            metadata=dict(metadata or {}),
        )
        self._nodes.append(node)
        return node

    def record_provider_ttft(self, milliseconds: int) -> None:
        if not self._finished:
            self._nodes[0].metadata["provider_ttft_ms"] = milliseconds

    def mark_degraded(self, reason: str) -> None:
        if not self._finished:
            self._nodes[0].metadata.update(degraded=True, degradeReason=reason)

    async def finish(
        self,
        *,
        status: TraceStatus,
        error_type: str | None,
        terminal_reason: str,
    ) -> None:
        if self._finished:
            return
        self._finished = True

        ended_at = _utc_now()
        root = self._nodes[0]
        root.ended_at = ended_at
        root.error_type = error_type
        root.metadata["errorType"] = error_type
        root.metadata["terminalReason"] = terminal_reason
        for node in self._nodes[1:]:
            if node.ended_at is None:
                node.ended_at = ended_at
                if status != "COMPLETED" and node.error_type is None:
                    node.error_type = error_type

        try:
            await self._exporter.export(tuple(self._nodes))
        except Exception:
            logger.warning("trace export failed code=TELEMETRY_EXPORT_FAILED")


class NoopRequestTrace(RequestTrace):
    """Allocation-free request trace used by disabled factory paths."""

    def __init__(self) -> None:
        # Deliberately avoid RequestTrace initialization: no UUIDs, nodes, or metadata.
        pass

    def record_provider_ttft(self, milliseconds: int) -> None:
        del milliseconds

    def mark_degraded(self, reason: str) -> None:
        del reason

    async def finish(
        self,
        *,
        status: TraceStatus,
        error_type: str | None,
        terminal_reason: str,
    ) -> None:
        del status, error_type, terminal_reason


_NOOP_REQUEST_TRACE = NoopRequestTrace()
_current_request_trace: ContextVar[RequestTrace | None] = ContextVar(
    "current_request_trace", default=None
)
_active_span_stack: ContextVar[tuple[UUID, ...]] = ContextVar("active_span_stack", default=())


class TraceFactory:
    def __init__(
        self,
        *,
        exporter: TraceExporter,
        enabled: bool,
        sampling_rate: float,
    ) -> None:
        self._exporter = exporter
        self._enabled = enabled
        self._sampling_rate = sampling_rate

    def start_request(
        self,
        *,
        name: str,
        request_id: str,
        conversation_id: str,
        thread_id: str,
        lane: str,
        environment: str,
    ) -> RequestTrace:
        if not self._enabled or self._sampling_rate <= 0.0:
            return _NOOP_REQUEST_TRACE
        return RequestTrace(
            exporter=self._exporter,
            name=name,
            request_id=request_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            lane=lane,
            environment=environment,
        )


def current_request_trace() -> RequestTrace | None:
    return _current_request_trace.get()


@contextmanager
def bind_request_trace(trace: RequestTrace) -> Iterator[None]:
    trace_token = _current_request_trace.set(trace)
    stack_token = _active_span_stack.set(())
    try:
        yield
    finally:
        _active_span_stack.reset(stack_token)
        _current_request_trace.reset(trace_token)


@contextmanager
def trace_span(
    name: str,
    run_type: RunType,
    metadata: dict[str, SafeScalar] | None = None,
) -> Iterator[TraceNode | None]:
    trace = current_request_trace()
    if trace is None or isinstance(trace, NoopRequestTrace):
        yield None
        return

    stack = _active_span_stack.get()
    node = trace._start_span(
        name=name,
        run_type=run_type,
        metadata=metadata,
        parent_id=stack[-1] if stack else trace.root_id,
    )
    if node is None:
        yield None
        return

    token = _active_span_stack.set((*stack, node.id))
    try:
        yield node
    except BaseException as exc:
        node.error_type = type(exc).__name__
        raise
    finally:
        node.ended_at = _utc_now()
        _active_span_stack.reset(token)


def _utc_now() -> datetime:
    return datetime.now(UTC)
