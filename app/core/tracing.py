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
from app.core.pii import redact
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
OBSERVABILITY_LANES = frozenset(
    {
        "recommend",
        "cart",
        "fallback",
        "analysis",
        "product",
        "general",
        "confirm",
        "apply",
        "refused",
    }
)
_BUYER_DEGRADE_REASON_RANK = {
    reason: rank for rank, reason in enumerate(BUYER_DEGRADE_REASON_PRECEDENCE)
}
_SELLER_DEGRADE_REASON_RANK = {
    reason: rank for rank, reason in enumerate(SELLER_DEGRADE_REASON_PRECEDENCE)
}


def _preferred_degrade_reason(current: object, reason: str) -> str:
    """동시 degrade 표시 순서와 무관하게 역할별 단일 사유 우선순위를 적용한다."""
    if (
        isinstance(current, str)
        and current in _BUYER_DEGRADE_REASON_RANK
        and reason in _BUYER_DEGRADE_REASON_RANK
    ):
        return min((current, reason), key=_BUYER_DEGRADE_REASON_RANK.__getitem__)
    if (
        isinstance(current, str)
        and current in _SELLER_DEGRADE_REASON_RANK
        and reason in _SELLER_DEGRADE_REASON_RANK
    ):
        return min((current, reason), key=_SELLER_DEGRADE_REASON_RANK.__getitem__)
    return reason


SAFE_METADATA_KEYS = frozenset(
    {
        "requestId",
        "sessionFp",
        "threadFp",
        "lane",
        "environment",
        "model",
        "rankingArm",
        "promptTokens",
        "completionTokens",
        "cachedInputTokens",
        "cacheWriteTokens",
        "httpMethod",
        "upstream",
        "statusClass",
        "degraded",
        "degradeReason",
        "toolCalls",
        "errorType",
        "terminalReason",
        "server_first_event_ms",
        "server_first_text_token_ms",
        "provider_ttft_ms",
    }
)


class ObservationSink(Protocol):
    """요청 로그가 추적 샘플링과 무관하게 받는 bounded 관측 이벤트."""

    def set_lane(self, lane: str) -> None: ...

    def mark_degraded(self, reason: str) -> None: ...

    def record_model_call(
        self,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_input_tokens: int = 0,
        cache_write_tokens: int = 0,
        *,
        usage_reserved: bool = False,
        purpose: str | None = None,
    ) -> int: ...

    def record_memory_context(
        self,
        *,
        recent_tokens: int,
        situation_tokens: int,
        evicted_tokens: int,
        compaction_triggered: bool,
    ) -> None: ...

    def record_model_usage(
        self,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        call_id: int | None = None,
        cached_input_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> int: ...

    def record_tool_call(self) -> None: ...

    def record_search_result(self, candidates: int, total_count: int, elapsed_ms: int) -> None: ...


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


# [#326] 콘텐츠 서브트리에서 숫자열 카나리아까지 면제되는 키 — **입력 방향(inputs) 전용**.
# `record_request_content(input_text=…)`/`record_llm_content(system=…, user=…)` 가 쓰는 키와
# 정확히 일치한다. 사용자가 직접 타이핑한 텍스트라 가격·수량 등 숫자 오탐이 잦은 자리다.
# **출력 방향(outputs)은 키와 무관하게 전부 strict** — LLM 응답은 사용자 입력이 아니라 모델
# 생성물이라 도구가 가져온 백엔드 데이터(회원 전화번호·주민번호)를 그대로 옮겨 적을 수 있고
# (seller 레인이 SSE 직전 `mask_output` 을 두는 것과 같은 위협 모델), 캡처 시점은 그 마스킹
# **이전**이기 때문이다(PR #327 리뷰). Spring `requestBody`/`responseBody` 도 inputs 안에
# 있지만 이 목록에 없어 strict 다. 기본이 strict 라서 새 콘텐츠 작성자가 키를 추가해도
# 조용히 면제를 얻지 못한다.
_LLM_CONTENT_KEYS = frozenset({"message", "system", "user"})


def validate_export_payload(payload: object, *, allow_content: bool = False) -> None:
    """Fail closed when an export payload contains unsafe keys or canary values.

    [#326] `allow_content=True` 는 콘텐츠 추적 모드 전용이다 — run payload 의 `inputs`/`outputs`
    서브트리에서 **구조 검증(키 allowlist·raw-data 키 금지)만** 면제한다(발화·prompt·응답·
    Spring 페이로드가 실리는 자리라 이 면제가 곧 기능이다). 텍스트 카나리아
    (`_TEXT_CANARY_PATTERNS`: bearer 토큰·`sk-`/`lsv2_` API 키·이메일)는 콘텐츠 안에서도
    **계속 적용**되고, 숫자열 카나리아(휴대폰·주민번호)는 **입력 방향의 발화·prompt 키**
    (`_LLM_CONTENT_KEYS`)에서만 면제된다 — Spring 페이로드와 **모든 출력(outputs)** 은 전체
    카나리아 대상이다(LLM 응답은 모델 생성물이라 업스트림 데이터와 같은 위협 모델).
    걸리면 기존 fail-closed 대로 trace 전체가 버려진다 — 콘텐츠 모드에서 이메일·
    토큰·업스트림 PII 가 섞인 요청의 트레이스가 사라지는 것은 의도된 트레이드오프다.
    metadata allowlist 와 나머지 필드의 검증은 그대로 유지된다.
    """

    _validate_value(payload, allow_content=allow_content)


def _validate_value(
    value: object,
    *,
    metadata: bool = False,
    opaque: bool = False,
    allow_content: bool = False,
    content: str | None = None,
) -> None:
    """`opaque` 는 이 값이 서버 생성 불투명 식별자로 **확인됐다**는 뜻이다(`_is_opaque_identifier`).

    `content` 는 [#326] 콘텐츠 서브트리 안에서의 검증 강도다 — `"strict"`(기본: 구조 검증만
    면제, 카나리아 전체 적용) 또는 `"lenient"`(`_LLM_CONTENT_KEYS` 의 발화·LLM 원문: 숫자열
    카나리아까지 면제). 토큰·키·이메일 카나리아(`_TEXT_CANARY_PATTERNS`)는 **어떤 필드에서도
    끄지 않는다**(`validate_export_payload` docstring 참조).
    """
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key)
            if content is None and allow_content and not metadata and key in ("inputs", "outputs"):
                # 콘텐츠 컨테이너 진입 — inputs 는 안의 키가 강도를 결정하고("auto"),
                # outputs 는 전부 strict 다(모델 생성물 = 업스트림 데이터와 같은 위협 모델).
                _validate_value(
                    nested,
                    allow_content=allow_content,
                    content="auto" if key == "inputs" else "strict",
                )
                continue
            if content is not None:
                # 콘텐츠 서브트리 — 키 검사 없이 카나리아만 본다. 강도는 발화·LLM 원문 키만
                # lenient, 나머지(Spring 페이로드 등)는 strict(숫자열 카나리아 유지). 한 번
                # 정해진 강도는 더 깊은 값에 그대로 상속된다.
                if content == "auto":
                    mode = "lenient" if key in _LLM_CONTENT_KEYS else "strict"
                else:
                    mode = content
                _validate_value(nested, allow_content=allow_content, content=mode)
                continue
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
                allow_content=allow_content,
            )
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _validate_value(
                nested,
                metadata=metadata,
                opaque=opaque,
                allow_content=allow_content,
                content=content,
            )
        return
    if isinstance(value, BaseException):
        _validate_value(value.args, allow_content=allow_content, content=content)
        _validate_value(vars(value), allow_content=allow_content, content=content)
        return
    if isinstance(value, str):
        # lenient 콘텐츠(발화·LLM 원문)만 숫자열 카나리아를 끈다 — 가격·수량 오탐 회피.
        # strict 콘텐츠(Spring 페이로드)와 일반 필드는 전체 카나리아 대상이다.
        patterns = _TEXT_CANARY_PATTERNS if (opaque or content == "lenient") else _CANARY_PATTERNS
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
    # [#326] 콘텐츠 추적 모드 전용 — capture_content=False 인 trace 에서는 항상 빈 dict 로
    # 남는다(기록 API 가 gate 를 통과시키지 않는다). export 는 이 dict 를 그대로 싣는다.
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)


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


def _maybe_redact(value: object) -> object:
    """[이슈 #321] 콘텐츠 트레이스에 실기 전 하드 PII 를 치환한다(`settings.pii_redact_trace_content`).

    문자열이 아닌 값(구조화 `extra_inputs` 등)은 그대로 둔다 — `redact()` 는 문자열 전용이고,
    비문자열에 적용하면 fail-closed 경로가 값 전체를 지워버려 conditionActions 같은 구조화
    입력이 손상된다. `_clip` 이 이후 단계에서 `repr()` 로 안전하게 문자열화한다.

    `tracing.py` 의 기존 카나리아 검증(`validate_export_payload`)·면제 목록은 그대로 둔다 —
    이 함수는 그 앞에서 치환만 하고, 검증기는 여전히 fail-closed 로 뒤를 지킨다.
    """
    if not isinstance(value, str):
        return value
    from app.core.config import get_settings  # noqa: PLC0415 - get_trace_factory 와 같은 지연 임포트

    if not get_settings().pii_redact_trace_content:
        return value
    redacted, _ = redact(value)
    return redacted


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
        capture_content: bool = False,
        content_max_chars: int = 0,
    ) -> None:
        trace_id = uuid4()
        self._exporter = exporter
        self._payload_validator = payload_validator
        self._capture_content = capture_content
        self._content_max_chars = content_max_chars
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
        self._observation: ObservationSink | None = None

    def _is_closing(self) -> bool:
        return self._finished or self._finish_task is not None

    @property
    def root_id(self) -> UUID:
        return self._root_id

    @property
    def degrade_reason(self) -> str | None:
        """[#140] 현재 우선순위가 적용된 단일 degrade 사유 — 읽기 전용.

        `mark_degraded` 가 이미 `_preferred_degrade_reason` 으로 우선순위를 정본화해 두므로
        여기서는 그 값을 그대로 읽기만 한다(로직 복제 금지).
        """
        reason = self._nodes[0].metadata.get("degradeReason")
        return reason if isinstance(reason, str) else None

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

    @property
    def captures_content(self) -> bool:
        """[#326] 콘텐츠 추적 모드가 켜진 살아있는 trace 인가 — 호출부의 수집 비용 가드용."""
        return self._capture_content and not self._is_closing()

    def _clip(self, value: object) -> str:
        text = value if isinstance(value, str) else repr(value)
        limit = self._content_max_chars
        if limit > 0 and len(text) > limit:
            return text[:limit] + f"…[truncated {len(text) - limit} chars]"
        return text

    def record_request_content(
        self,
        *,
        input_text: str | None = None,
        output_text: str | None = None,
        extra_inputs: Mapping[str, object] | None = None,
    ) -> None:
        """[#326] 루트 span 에 사용자 발화/최종 응답 원문을 싣는다(콘텐츠 모드에서만).

        `extra_inputs` 는 발화 외 구조화 입력(예: 칩 제거 `conditionActions`, I-22 시그널)용이다 —
        키가 `_LLM_CONTENT_KEYS` 에 없으므로 자동으로 strict(전체 카나리아) 검증을 받는다(#469).
        """
        if not self.captures_content:
            return
        root = self._nodes[0]
        if input_text is not None:
            root.inputs["message"] = self._clip(_maybe_redact(input_text))
        if extra_inputs:
            root.inputs.update(
                {key: self._clip(_maybe_redact(value)) for key, value in extra_inputs.items()}
            )
        if output_text is not None:
            root.outputs["message"] = self._clip(_maybe_redact(output_text))

    def record_llm_content(
        self,
        *,
        system: str | None = None,
        user: str | None = None,
        output: str | None = None,
        transcript: str | None = None,
    ) -> None:
        """[#326] 활성 LLM span 에 prompt·응답 원문을 싣는다(콘텐츠 모드에서만).

        `record_llm_usage` 와 같은 활성 span 탐색을 쓴다 — llm.py 초크포인트 한 곳에서 부르면
        모든 `llm.*` span 이 원문을 얻는다.

        `user` 는 **사용자가 직접 타이핑한 발화에서 파생된 prompt 전용**이다(lenient —
        숫자열 카나리아 면제). agent 대화 히스토리처럼 tool 결과(백엔드 데이터)가 섞이는
        텍스트는 `transcript` 로 넘겨라 — `_LLM_CONTENT_KEYS` 에 없는 키라 strict(전체
        카나리아)로 검증된다(PR #327 리뷰).
        """
        if not self.captures_content:
            return
        stack = _active_span_stack.get()
        if not stack:
            return
        node = next(
            (candidate for candidate in reversed(self._nodes) if candidate.id == stack[-1]), None
        )
        if node is None or node.run_type != "llm":
            return
        if system is not None:
            node.inputs["system"] = self._clip(_maybe_redact(system))
        if user is not None:
            node.inputs["user"] = self._clip(_maybe_redact(user))
        if transcript is not None:
            node.inputs["transcript"] = self._clip(_maybe_redact(transcript))
        if output is not None:
            node.outputs["content"] = self._clip(_maybe_redact(output))

    def record_span_content(
        self,
        node: TraceNode,
        *,
        inputs: Mapping[str, object] | None = None,
        outputs: Mapping[str, object] | None = None,
    ) -> None:
        """[#326] 임의 span 노드에 콘텐츠를 싣는다(콘텐츠 모드에서만) — Spring 페이로드용.

        [F2, #321 리뷰 2라운드] **의도적으로 `_maybe_redact` 를 타지 않는다** — 누락이 아니다.
        호출부는 `app/services/spring_client.py` 의 Spring `requestBody`/`responseBody` 하나뿐이고,
        이 값은 `_LLM_CONTENT_KEYS` 밖이라 항상 strict 검증(`_validate_value`)을 받는다 — 발화
        (lenient, `#321` 이 닫은 경로)와 위협 모델이 다르다. 백엔드 데이터에 고객 PII 가 섞이면
        지금은 `validate_export_payload` 가 트레이스를 통째로 버리고, 그 시끄러운 실패 자체가
        "업스트림이 PII 를 흘리고 있다"는 신호다. 여기서 치환해 버리면 그 신호가 조용해진다.
        """
        if not self.captures_content:
            return
        if inputs:
            node.inputs.update({key: self._clip(value) for key, value in inputs.items()})
        if outputs:
            node.outputs.update({key: self._clip(value) for key, value in outputs.items()})

    def attach_observation(self, observation: ObservationSink) -> None:
        """샘플링된 trace의 bounded 상태를 요청 로그 sink와 연결한다."""
        self._observation = observation
        root = self._nodes[0]
        lane = root.metadata.get("lane")
        if isinstance(lane, str) and lane in OBSERVABILITY_LANES:
            observation.set_lane(lane)
        reason = root.metadata.get("degradeReason")
        if isinstance(reason, str):
            observation.mark_degraded(reason)
        for _ in range(int(root.metadata.get("toolCalls") or 0)):
            observation.record_tool_call()

    def set_lane(self, lane: str) -> None:
        """Replace the request's provisional lane after bounded routing completes."""
        if not self._is_closing():
            self._nodes[0].metadata["lane"] = lane
            if lane not in OBSERVABILITY_LANES:
                logger.warning("unregistered observability lane code=OBSERVABILITY_LANE_UNKNOWN")
            elif self._observation is not None:
                self._observation.set_lane(lane)

    def record_llm_usage(
        self,
        *,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        call_id: int | None = None,
        cached_input_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> int | None:
        """Attach bounded provider facts to the active explicit LLM span."""
        if self._is_closing():
            return None
        observation_call_id = None
        if self._observation is not None:
            observation_call_id = self._observation.record_model_usage(
                model,
                prompt_tokens,
                completion_tokens,
                call_id,
                cached_input_tokens,
                cache_write_tokens,
            )
        stack = _active_span_stack.get()
        if not stack:
            return observation_call_id
        node = next(
            (candidate for candidate in reversed(self._nodes) if candidate.id == stack[-1]), None
        )
        if node is None or node.run_type != "llm":
            return observation_call_id
        node.metadata["model"] = model
        if prompt_tokens is not None:
            node.metadata["promptTokens"] = prompt_tokens
        if completion_tokens is not None:
            node.metadata["completionTokens"] = completion_tokens
        if cached_input_tokens is not None:
            node.metadata["cachedInputTokens"] = cached_input_tokens
        if cache_write_tokens is not None:
            node.metadata["cacheWriteTokens"] = cache_write_tokens
        return observation_call_id

    def record_llm_call(self, *, model: str) -> int | None:
        """요청 관측에 provider 호출 단위 placeholder를 만들고 ID를 반환한다."""
        if self._is_closing() or self._observation is None:
            return None
        return self._observation.record_model_call(model)

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
            reason = _preferred_degrade_reason(root.metadata.get("degradeReason"), reason)
            root.metadata.update(degraded=True, degradeReason=reason)
            if self._observation is not None:
                self._observation.mark_degraded(reason)

    def record_tool_call(self) -> None:
        """판매자 에이전트가 실제 실행한 도구 호출 수를 요청 단위로 누적한다."""
        if self._is_closing():
            return
        root = self._nodes[0]
        root.metadata["toolCalls"] = int(root.metadata.get("toolCalls") or 0) + 1
        if self._observation is not None:
            self._observation.record_tool_call()

    def record_search_result(self, candidates: int, total_count: int, elapsed_ms: int) -> None:
        """검색 결과 집계를 요청 관측으로 전달한다. trace가 관측 sink 없이도 검색을 막지 않는다."""
        if self._is_closing() or self._observation is None:
            return
        self._observation.record_search_result(candidates, total_count, elapsed_ms)

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
    """export 없이 요청 로그용 bounded 상태만 전달하는 trace."""

    def __init__(self, *, lane: str | None = None) -> None:
        self._lane = lane
        self._degrade_reason: str | None = None
        self._tool_calls = 0
        self._observation: ObservationSink | None = None

    def record_provider_ttft(self, milliseconds: int) -> None:
        del milliseconds

    @property
    def degrade_reason(self) -> str | None:
        """[#140] `RequestTrace.degrade_reason` 과 같은 읽기 전용 접근자 — Noop 경로용."""
        return self._degrade_reason

    @property
    def captures_content(self) -> bool:
        return False

    def record_request_content(
        self,
        *,
        input_text: str | None = None,
        output_text: str | None = None,
        extra_inputs: Mapping[str, object] | None = None,
    ) -> None:
        del input_text, output_text, extra_inputs

    def record_llm_content(
        self,
        *,
        system: str | None = None,
        user: str | None = None,
        output: str | None = None,
        transcript: str | None = None,
    ) -> None:
        del system, user, output, transcript

    def record_span_content(
        self,
        node: TraceNode,
        *,
        inputs: Mapping[str, object] | None = None,
        outputs: Mapping[str, object] | None = None,
    ) -> None:
        del node, inputs, outputs

    def attach_observation(self, observation: ObservationSink) -> None:
        self._observation = observation
        if self._lane in OBSERVABILITY_LANES:
            observation.set_lane(self._lane)
        if self._degrade_reason is not None:
            observation.mark_degraded(self._degrade_reason)
        for _ in range(self._tool_calls):
            observation.record_tool_call()

    def set_lane(self, lane: str) -> None:
        self._lane = lane
        if lane not in OBSERVABILITY_LANES:
            logger.warning("unregistered observability lane code=OBSERVABILITY_LANE_UNKNOWN")
        elif self._observation is not None:
            self._observation.set_lane(lane)

    def record_llm_usage(
        self,
        *,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        call_id: int | None = None,
        cached_input_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> int | None:
        if self._observation is not None:
            return self._observation.record_model_usage(
                model,
                prompt_tokens,
                completion_tokens,
                call_id,
                cached_input_tokens,
                cache_write_tokens,
            )
        return None

    def record_llm_call(self, *, model: str) -> int | None:
        if self._observation is None:
            return None
        return self._observation.record_model_call(model)

    def record_server_timings(
        self,
        *,
        first_event_ms: int | None,
        first_text_token_ms: int | None,
    ) -> None:
        del first_event_ms, first_text_token_ms

    def mark_degraded(self, reason: str) -> None:
        self._degrade_reason = _preferred_degrade_reason(self._degrade_reason, reason)
        if self._observation is not None:
            self._observation.mark_degraded(self._degrade_reason)

    def record_tool_call(self) -> None:
        self._tool_calls += 1
        if self._observation is not None:
            self._observation.record_tool_call()

    def record_search_result(self, candidates: int, total_count: int, elapsed_ms: int) -> None:
        if self._observation is not None:
            self._observation.record_search_result(candidates, total_count, elapsed_ms)

    async def finish(
        self,
        *,
        status: TraceStatus,
        error_type: str | None,
        terminal_reason: str,
    ) -> None:
        del status, error_type, terminal_reason


_current_request_trace: ContextVar[RequestTrace | None] = ContextVar(
    "current_request_trace", default=None
)
_active_span_stack: ContextVar[tuple[UUID, ...]] = ContextVar("active_span_stack", default=())
_bound_model_call_id: ContextVar[int | None] = ContextVar("bound_model_call_id", default=None)


class TraceFactory:
    def __init__(
        self,
        *,
        exporter: TraceExporter,
        enabled: bool,
        sampling_rate: float,
        payload_validator: Callable[[object], None] = validate_export_payload,
        capture_content: bool = False,
        content_max_chars: int = 0,
    ) -> None:
        self._exporter = exporter
        self._enabled = enabled
        self._sampling_rate = sampling_rate
        self._payload_validator = payload_validator
        self._capture_content = capture_content
        self._content_max_chars = content_max_chars

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
            return NoopRequestTrace(lane=lane)
        return RequestTrace(
            exporter=self._exporter,
            name=name,
            request_id=request_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            lane=lane,
            environment=environment,
            payload_validator=self._payload_validator,
            capture_content=self._capture_content,
            content_max_chars=self._content_max_chars,
        )


class LangSmithTraceExporter:
    """Explicit, bounded LangSmith batch exporter with no global auto-tracing."""

    def __init__(
        self, client: Any, project_name: str, timeout_s: float, *, allow_content: bool = False
    ) -> None:
        self._client = client
        self._project_name = project_name
        self._timeout_s = timeout_s
        self._allow_content = allow_content

    async def export(self, nodes: tuple[TraceNode, ...]) -> None:
        payloads = _build_export_payloads(nodes, project_name=self._project_name)
        validate_export_payload(payloads, allow_content=self._allow_content)
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
            # [#326] capture_content=False 인 trace 의 노드는 항상 빈 dict 다(기존 동작과 동일).
            "inputs": dict(node.inputs),
            "outputs": dict(node.outputs),
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
    # [#326] 콘텐츠 추적 모드 — 기본 off. 켜면 발화·LLM 원문·Spring 페이로드가 실리므로
    # 실사용자 오픈 전 디버깅 구간에서만 켠다(DEPLOY.md §8 규약).
    capture_content = settings.langsmith_trace_content
    if capture_content:
        logger.warning("LangSmith content tracing ON — raw payloads will be exported (#326)")
    _trace_factory = TraceFactory(
        exporter=LangSmithTraceExporter(
            client,
            settings.langsmith_project,
            settings.langsmith_export_timeout_s,
            allow_content=capture_content,
        ),
        enabled=True,
        sampling_rate=settings.langsmith_tracing_sampling_rate,
        payload_validator=(
            (lambda payload: validate_export_payload(payload, allow_content=True))
            if capture_content
            else validate_export_payload
        ),
        capture_content=capture_content,
        content_max_chars=settings.langsmith_trace_content_max_chars,
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
        return NoopRequestTrace(lane=lane)


def current_request_trace() -> RequestTrace | None:
    return _current_request_trace.get()


def current_model_call_usage() -> int | None:
    """현재 task의 provider usage를 받을 명시적 observation call ID."""
    return _bound_model_call_id.get()


@contextmanager
def bind_model_call_usage(call_id: int | None) -> Iterator[None]:
    """동일 모델의 동시 호출에서도 usage 귀속 대상을 task-local로 고정한다."""
    token = _bound_model_call_id.set(call_id)
    try:
        yield
    finally:
        _bound_model_call_id.reset(token)


@contextmanager
def bind_request_trace(trace: RequestTrace) -> Iterator[None]:
    trace_token = _current_request_trace.set(trace)
    stack_token = _active_span_stack.set(())
    call_token = _bound_model_call_id.set(None)
    try:
        yield
    finally:
        _bound_model_call_id.reset(call_token)
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
