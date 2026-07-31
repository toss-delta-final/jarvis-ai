"""Request-scoped in-memory tracing primitives.

The active request and span ancestry live in context variables so asyncio tasks inherit
parentage without sharing a mutable global span stack.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import logging

from app.core.logging import safe_fingerprint
import re
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

RunType = Literal["chain", "llm", "tool", "retriever"]
TraceStatus = Literal["COMPLETED", "FAILED", "CANCELLED"]
SafeScalar = str | int | float | bool | None

logger = logging.getLogger(__name__)

# Singular buyer degradeReason precedence, highest severity first. This keeps the exported
# field bounded and order-independent when concurrent recommendation work marks more than one
# existing degrade path; no list or raw failure detail is retained.
BUYER_DEGRADE_REASON_PRECEDENCE = (
    "search_failed",
    "push_skipped",
    "rerank_fallback",
    "fanout_partial",
    "dedup_skipped",
    "cart_merge_skipped",
)
SELLER_DEGRADE_REASON_PRECEDENCE = (
    "spring_write_failed",
    "all_workers_failed",
    "partial_report",
    "worker_degrade",
)
SELLER_DEGRADE_REASONS = frozenset(SELLER_DEGRADE_REASON_PRECEDENCE)
_BUYER_DEGRADE_REASON_RANK = {
    reason: rank for rank, reason in enumerate(BUYER_DEGRADE_REASON_PRECEDENCE)
}
_SELLER_DEGRADE_REASON_RANK = {
    reason: rank for rank, reason in enumerate(SELLER_DEGRADE_REASON_PRECEDENCE)
}

SAFE_METADATA_KEYS = frozenset(
    {
        "requestId",
        "sessionFp",
        "threadFp",
        "lane",
        "environment",
        "model",
        "promptTokens",
        "completionTokens",
        "httpMethod",
        "upstream",
        "statusClass",
        "degraded",
        "degradeReason",
        "errorType",
        "terminalReason",
        "server_first_event_ms",
        "server_first_text_token_ms",
        "provider_ttft_ms",
    }
)

_UNSAFE_KEY_PARTS = (
    "authorization",
    "cookie",
    "apikey",
    "prompt",
    "body",
    "input",
    "output",
    "tool",
    "customer",
)
# 문자열 어디에 있어도 잡아야 하는 카나리아 — 토큰·API 키·이메일. 리터럴 접두사나 `@` 를
# 요구해 무작위 hex 에 우연히 걸리지 않으므로 모든 값에 항상 적용한다.
_TEXT_CANARY_PATTERNS = (
    re.compile(r"\bbearer\s+\S+", re.IGNORECASE),
    re.compile(r"\b(?:sk-|lsv2_)[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
)
# 숫자열 카나리아 — 휴대폰·주민번호. 숫자만으로 이뤄져 무작위 16진수와 우연히 겹칠 수 있다.
_NUMERIC_CANARY_PATTERNS = (
    re.compile(r"(?<!\d)01[016789][ -]?\d{3,4}[ -]?\d{4}(?!\d)"),
    re.compile(r"(?<!\d)\d{6}[ -]?[1-4]\d{6}(?!\d)"),
)
_CANARY_PATTERNS = _TEXT_CANARY_PATTERNS + _NUMERIC_CANARY_PATTERNS

# 서버가 만든 불투명 식별자 — **사용자 데이터가 도달할 수 없는** 필드다. 이 hex 안의 숫자열이
# 휴대폰/주민번호 패턴과 우연히 겹쳐 **트레이스가 통째로 버려지던** 오탐의 유일한 원인이라
# (실측 오탐률·경위는 docs/lessons.md 2026-07-31 항목) 여기서만 숫자열 카나리아를 끈다.
#
# 면제는 **위치 + 값의 모양**으로 결정한다. 키 이름만 보면 트리 어디서든(예: 예외의 `vars()`)
# 같은 이름을 쓰는 dict 가 생기는 순간 면제가 따라붙고, 그 값이 해시가 아니라 원본이어도 그냥
# 통과한다(PR #218 리뷰). 실제 그 생성기의 산출물일 때만 끄면 그 창이 닫힌다.
#
# 패턴 자체의 경계를 hex 로 넓히는 방식은 쓰지 않는다 — 그러면 `userid01012345678` 처럼 hex 로
# 끝나는 흔한 단어 뒤에 붙은 **진짜 PII 까지 모든 문자열에서** 탐지를 피한다(같은 리뷰).
# 키를 추가할 때는 "그 값이 서버 생성임을 코드로 보일 수 있는가"에 답할 수 있어야 한다.
_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# `extra.metadata` 문맥에서만 면제되는 키 → 그 생성기가 내는 값의 모양.
_OPAQUE_METADATA_SHAPES = {
    "requestId": re.compile(r"[0-9a-f]{32}"),  # errors.new_request_id: uuid4().hex
    "sessionFp": re.compile(r"[0-9a-f]{16}"),  # logging.safe_fingerprint: HMAC hexdigest[:16]
    "threadFp": re.compile(r"[0-9a-f]{16}"),
}
# run payload 최상위에서만 면제되는 키 → LangSmith 정렬 키(`<타임스탬프><uuid>` 를 `.` 로 연결).
_OPAQUE_PAYLOAD_SHAPES = {
    "dotted_order": re.compile(rf"\d{{8}}T\d{{12}}Z{_UUID_RE}(?:\.\d{{8}}T\d{{12}}Z{_UUID_RE})*"),
}


def _is_opaque_identifier(key: str, value: object, *, metadata: bool) -> bool:
    """이 (위치, 키, 값) 이 서버 생성 불투명 식별자로 확인되면 숫자열 카나리아를 면제한다."""
    shapes = _OPAQUE_METADATA_SHAPES if metadata else _OPAQUE_PAYLOAD_SHAPES
    shape = shapes.get(key)
    return bool(shape and isinstance(value, str) and shape.fullmatch(value))


class UnsafeTelemetryError(ValueError):
    """Raised when a trace payload may contain raw or sensitive data."""


def validate_export_payload(payload: object) -> None:
    """Fail closed when an export payload contains unsafe keys or canary values."""

    _validate_value(payload)


def _validate_value(value: object, *, metadata: bool = False, opaque: bool = False) -> None:
    """`opaque` 는 이 값이 서버 생성 불투명 식별자로 **확인됐다**는 뜻이다(`_is_opaque_identifier`).

    면제는 `_NUMERIC_CANARY_PATTERNS` 에만 적용된다 — 토큰·키·이메일 카나리아는 어떤 필드에서도
    끄지 않는다(`_OPAQUE_METADATA_SHAPES` 주석 참조).
    """
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key)
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
            if metadata and key not in SAFE_METADATA_KEYS:
                raise UnsafeTelemetryError("metadata key is not allowlisted")
            is_safe_metadata_key = metadata and key in SAFE_METADATA_KEYS
            if not is_safe_metadata_key and any(
                part in normalized_key for part in _UNSAFE_KEY_PARTS
            ):
                if not isinstance(nested, Mapping) or nested:
                    raise UnsafeTelemetryError("raw-data field is not empty")
            _validate_value(
                nested,
                metadata=key == "metadata",
                opaque=_is_opaque_identifier(key, nested, metadata=metadata),
            )
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _validate_value(nested, metadata=metadata, opaque=opaque)
        return
    if isinstance(value, BaseException):
        _validate_value(value.args)
        _validate_value(vars(value))
        return
    if isinstance(value, str):
        patterns = _TEXT_CANARY_PATTERNS if opaque else _CANARY_PATTERNS
        if any(pattern.search(value) for pattern in patterns):
            raise UnsafeTelemetryError("sensitive value canary detected")


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
        payload_validator: Callable[[object], None],
    ) -> None:
        trace_id = uuid4()
        self._exporter = exporter
        self._payload_validator = payload_validator
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
                    "sessionFp": safe_fingerprint(conversation_id),
                    "threadFp": safe_fingerprint(thread_id),
                    "lane": lane,
                    "environment": environment,
                },
            )
        ]
        self._finished = False
        self._finish_task: asyncio.Task[None] | None = None

    def _is_closing(self) -> bool:
        return self._finished or self._finish_task is not None

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
        if self._is_closing():
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
        if not self._is_closing():
            self._nodes[0].metadata.setdefault("provider_ttft_ms", milliseconds)

    def set_lane(self, lane: str) -> None:
        """Replace the request's provisional lane after bounded routing completes."""
        if not self._is_closing():
            self._nodes[0].metadata["lane"] = lane

    def record_llm_usage(
        self,
        *,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        """Attach bounded provider facts to the active explicit LLM span."""
        if self._is_closing():
            return
        stack = _active_span_stack.get()
        if not stack:
            return
        node = next(
            (candidate for candidate in reversed(self._nodes) if candidate.id == stack[-1]), None
        )
        if node is None or node.run_type != "llm":
            return
        node.metadata["model"] = model
        if prompt_tokens is not None:
            node.metadata["promptTokens"] = prompt_tokens
        if completion_tokens is not None:
            node.metadata["completionTokens"] = completion_tokens

    def record_server_timings(
        self,
        *,
        first_event_ms: int | None,
        first_text_token_ms: int | None,
    ) -> None:
        if not self._is_closing():
            self._nodes[0].metadata.update(
                server_first_event_ms=first_event_ms,
                server_first_text_token_ms=first_text_token_ms,
            )

    def mark_degraded(self, reason: str) -> None:
        if not self._is_closing():
            root = self._nodes[0]
            current = root.metadata.get("degradeReason")
            if (
                isinstance(current, str)
                and current in _BUYER_DEGRADE_REASON_RANK
                and reason in _BUYER_DEGRADE_REASON_RANK
            ):
                reason = min(
                    (current, reason),
                    key=_BUYER_DEGRADE_REASON_RANK.__getitem__,
                )
            elif (
                isinstance(current, str)
                and current in _SELLER_DEGRADE_REASON_RANK
                and reason in _SELLER_DEGRADE_REASON_RANK
            ):
                reason = min(
                    (current, reason),
                    key=_SELLER_DEGRADE_REASON_RANK.__getitem__,
                )
            root.metadata.update(degraded=True, degradeReason=reason)

    async def finish(
        self,
        *,
        status: TraceStatus,
        error_type: str | None,
        terminal_reason: str,
    ) -> None:
        task = self._finish_task
        if task is None:
            task = asyncio.create_task(
                self._complete_finish(
                    status=status,
                    error_type=error_type,
                    terminal_reason=terminal_reason,
                )
            )
            self._finish_task = task
        await asyncio.shield(task)

    async def _complete_finish(
        self,
        *,
        status: TraceStatus,
        error_type: str | None,
        terminal_reason: str,
    ) -> None:
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

        nodes = tuple(self._nodes)
        try:
            self._payload_validator(_build_export_payloads(nodes, project_name=None))
        except UnsafeTelemetryError:
            logger.warning(
                "trace dropped requestId=%s root=%s code=TELEMETRY_REDACTION_FAILED",
                root.metadata["requestId"],
                root.name,
            )
        except Exception:
            logger.warning("trace validation failed code=TELEMETRY_VALIDATION_FAILED")
        else:
            try:
                await self._exporter.export(nodes)
            except Exception:
                logger.warning("trace export failed code=TELEMETRY_EXPORT_FAILED")
        self._finished = True


class NoopRequestTrace(RequestTrace):
    """Allocation-free request trace used by disabled factory paths."""

    def __init__(self) -> None:
        # Deliberately avoid RequestTrace initialization: no UUIDs, nodes, or metadata.
        pass

    def record_provider_ttft(self, milliseconds: int) -> None:
        del milliseconds

    def set_lane(self, lane: str) -> None:
        del lane

    def record_llm_usage(
        self,
        *,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        del model, prompt_tokens, completion_tokens

    def record_server_timings(
        self,
        *,
        first_event_ms: int | None,
        first_text_token_ms: int | None,
    ) -> None:
        del first_event_ms, first_text_token_ms

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
        payload_validator: Callable[[object], None] = validate_export_payload,
    ) -> None:
        self._exporter = exporter
        self._enabled = enabled
        self._sampling_rate = sampling_rate
        self._payload_validator = payload_validator

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
        if not self._enabled or not _is_sampled(request_id, self._sampling_rate):
            return _NOOP_REQUEST_TRACE
        return RequestTrace(
            exporter=self._exporter,
            name=name,
            request_id=request_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            lane=lane,
            environment=environment,
            payload_validator=self._payload_validator,
        )


class LangSmithTraceExporter:
    """Explicit, bounded LangSmith batch exporter with no global auto-tracing."""

    def __init__(self, client: Any, project_name: str, timeout_s: float) -> None:
        self._client = client
        self._project_name = project_name
        self._timeout_s = timeout_s

    async def export(self, nodes: tuple[TraceNode, ...]) -> None:
        payloads = _build_export_payloads(nodes, project_name=self._project_name)
        validate_export_payload(payloads)
        try:
            async with asyncio.timeout(self._timeout_s):
                await asyncio.to_thread(self._client.batch_ingest_runs, create=payloads)
        except TimeoutError:
            logger.warning("trace export timed out code=TELEMETRY_EXPORT_TIMEOUT")
        except Exception:
            logger.warning("trace export failed code=TELEMETRY_EXPORT_FAILED")


def _build_export_payloads(
    nodes: tuple[TraceNode, ...], *, project_name: str | None
) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    dotted_orders: dict[UUID, str] = {}
    for node in nodes:
        segment = f"{node.started_at.astimezone(UTC):%Y%m%dT%H%M%S%fZ}{node.id}"
        if node.parent_id is None:
            dotted_order = segment
        else:
            dotted_order = f"{dotted_orders[node.parent_id]}.{segment}"
        dotted_orders[node.id] = dotted_order
        metadata = dict(node.metadata)
        if node.error_type is not None:
            metadata["errorType"] = node.error_type
        payload: dict[str, object] = {
            "id": node.id,
            "trace_id": node.trace_id,
            "parent_run_id": node.parent_id,
            "dotted_order": dotted_order,
            "name": node.name,
            "run_type": node.run_type,
            "start_time": node.started_at,
            "end_time": node.ended_at,
            "inputs": {},
            "outputs": {},
            "extra": {"metadata": metadata},
        }
        if project_name is not None:
            payload["session_name"] = project_name
        payloads.append(payload)
    return payloads


def _is_sampled(request_id: str, sampling_rate: float) -> bool:
    if sampling_rate <= 0.0:
        return False
    if sampling_rate >= 1.0:
        return True
    digest = hashlib.sha256(request_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest, "big") / (1 << 256)
    return bucket < sampling_rate


_trace_factory: TraceFactory | None = None


def set_trace_factory(factory: TraceFactory | None) -> None:
    """Override or reset the process-wide trace factory."""

    global _trace_factory
    _trace_factory = factory


def get_trace_factory() -> TraceFactory:
    """Build the configured explicit exporter lazily and cache it."""

    global _trace_factory
    if _trace_factory is not None:
        return _trace_factory

    from app.core.config import get_settings

    settings = get_settings()
    if not settings.langsmith_tracing:
        _trace_factory = TraceFactory(
            exporter=NoopTraceExporter(),
            enabled=False,
            sampling_rate=settings.langsmith_tracing_sampling_rate,
        )
        return _trace_factory

    from langsmith import Client

    api_key = (
        settings.langsmith_api_key.get_secret_value()
        if settings.langsmith_api_key is not None
        else None
    )
    client = Client(
        api_key=api_key,
        auto_batch_tracing=False,
        omit_traced_runtime_info=True,
        tracing_sampling_rate=1.0,
    )
    _trace_factory = TraceFactory(
        exporter=LangSmithTraceExporter(
            client,
            settings.langsmith_project,
            settings.langsmith_export_timeout_s,
        ),
        enabled=True,
        sampling_rate=settings.langsmith_tracing_sampling_rate,
    )
    return _trace_factory


def start_request_trace_safely(
    *,
    name: str,
    request_id: str,
    conversation_id: str,
    thread_id: str,
    lane: str,
    environment: str,
) -> RequestTrace:
    """Start optional telemetry without allowing initialization to fail a request."""
    try:
        return get_trace_factory().start_request(
            name=name,
            request_id=request_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            lane=lane,
            environment=environment,
        )
    except Exception:
        logger.warning("trace start failed code=TELEMETRY_START_FAILED")
        return _NOOP_REQUEST_TRACE


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
