"""요청 단위 구조화 로그 + 대화 저장 브릿지 (api-spec §6.3 b).

스트림 수명주기(#1 open_stream)에 훅으로 붙어 first-token/전체 지연·모델/토큰·streamStatus·
errorType 를 요청당 1건의 구조화 로그로 남기고, 어시스턴트 응답(부분 포함)을 대화 저장소에
마감한다.

[PII] 사용자 message **원문은 로그에 남기지 않는다** — 길이·해시만 기록한다(§6.3 b).
원문은 대화 저장소(§6.3 a)에만 존재한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field

from app.core.auth import Identity
from app.core.config import get_settings
from app.core.conversation import ConversationStoreProtocol, TurnStatus, conversation_key
from app.core.logging import get_logger, safe_fingerprint
from app.core.session_context import BuyerSessionInput
from app.core.tracing import RequestTrace

logger = get_logger("observability")


async def finish_trace_safely(
    trace: RequestTrace,
    *,
    status: TurnStatus,
    error_type: str | None,
    terminal_reason: str,
) -> None:
    """추적 실패가 실제 요청/스트림 결과를 대체하지 않게 마감한다."""
    try:
        await trace.finish(
            status=status.value,
            error_type=error_type,
            terminal_reason=terminal_reason,
        )
    except Exception:
        logger.warning(
            "trace.finish 실패 terminal_reason=%s code=TELEMETRY_FINISH_FAILED",
            terminal_reason,
        )


def message_fingerprint(text: str) -> tuple[int, str]:
    """PII 안전 지문 — (길이, HMAC-SHA256 앞 16자). 원문은 반환하지 않는다.

    salt 없는 sha256 은 짧은 질의를 사전/레인보우로 역산 가능하므로, 서버 전용 pepper(config)를
    키로 한 HMAC 을 쓴다. **운영은 `PII_HASH_PEPPER`에 실제 secret 을 주입해야** 로그 접근자에게도
    원문 역산이 막힌다(기본 빈 값은 개발용).
    """
    pepper = get_settings().pii_hash_pepper.encode("utf-8")
    digest = hmac.new(pepper, text.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return len(text), digest


def identifier_fingerprint(value: str | None) -> str | None:
    """로그 상관관계용 비가역 식별자 지문."""
    return safe_fingerprint(value)


# 프로세스(=워커) 인스턴스 식별자. 기동 시 1회 생성한다. `activeStreams`/`activeStreamsPeak` 는
# 워커별 값이므로(docs/specs/DESIGN-SHARED-STREAM-REGISTRY-476.md §2.1) 이 지문이 없으면 다중
# 워커 로그를 워커별로 갈라 합산할 수 없다. 랜덤 uuid 라 PII 가 아니다.
_WORKER_INSTANCE_ID = str(uuid.uuid4())


def worker_instance_id() -> str:
    """이 프로세스의 인스턴스 id (공유 레지스트리 행 소유자 표기용)."""
    return _WORKER_INSTANCE_ID


def worker_fingerprint() -> str | None:
    """`chat_request` 로그의 워커 지문 — 워커별 관측값을 갈라 합산하기 위한 축."""
    return identifier_fingerprint(_WORKER_INSTANCE_ID)


class _LogFingerprint(str):
    """`fingerprint_log_value`만 생성하는 rejection 로그 전용 branded string."""


def fingerprint_log_value(value: str) -> str:
    """raw 값을 peppered HMAC branded string으로 바꿔 rejection 필드 provenance를 보장한다."""
    fingerprint = identifier_fingerprint(value)
    assert fingerprint is not None
    return _LogFingerprint(fingerprint)


def role_of(identity: Identity) -> str:
    """로그/저장용 역할 문자열."""
    if identity.seller_id:
        return "seller"
    if identity.is_guest:
        return "guest"
    return "member"


@dataclass
class ModelCall:
    """노드별 LLM 호출 기록 (그래프 연결 후 채워짐). 스텁 단계에선 비어 있다."""

    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    usage_reserved: bool = False
    purpose: str | None = None


@dataclass
class RequestObservation:
    """요청 1건의 관측 상태. open_stream 이 훅(on_first_token/record_frame/finish)을 호출한다."""

    request_id: str
    conversation_id: str
    thread_id: str | None
    user_id: str | None
    # Identity.brand_id 와 같은 계약 — JWKS 판매자 경로는 int 를 강제하고(auth.py `type is int`)
    # 관용 경로는 문자열도 통과시킨다. 지문 계산 전 str() 캐스팅이 반드시 필요하다.
    brand_id: str | int | None
    role: str
    store: ConversationStoreProtocol
    message_length: int
    message_hash: str
    started: float
    pending_message: str
    pending_key: str
    buyer_session: BuyerSessionInput | None = None
    trace: RequestTrace | None = None
    turn_id: str | None = None
    context_id: str | None = None
    first_event_at: float | None = None
    first_text_token_at: float | None = None
    # 이슈 #396 — 구매자 progress stage 별 최초 발생 시각(started 기준 ms). 판매자
    # progress(`{"text": …}`)는 stage 가 없어 여기 섞이지 않는다(record_frame 참조).
    progress_stages: dict[str, int] = field(default_factory=dict)
    assistant_parts: list[str] = field(default_factory=list)
    model_calls: list[ModelCall] = field(default_factory=list)
    lane: str | None = None
    degraded: bool = False
    degrade_reason: str | None = None
    tool_calls: int = 0
    search_calls: int = 0
    search_candidates_max: int | None = None
    search_total_count_max: int | None = None
    search_elapsed_ms_max: int | None = None
    recent_history_tokens: int = 0
    situation_memory_tokens: int = 0
    evicted_history_tokens: int = 0
    memory_compaction_triggered: bool = False
    active_streams: int | None = None
    active_streams_peak: int | None = None
    finished: bool = False
    _finish_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def commit_user_message(self) -> None:
        """스트림 슬롯 확보(§2.9 a 409 통과) **후** 사용자 메시지를 저장한다(§6.3 a).

        409로 거절된 중복/더블클릭 요청은 이 호출에 도달하지 않으므로 유령 턴(응답 없는
        FAILED 턴)이 다음 컨텍스트를 오염시키지 않는다."""
        if self.turn_id is None:
            committed = await self.store.save_user_message(
                self.pending_key,
                self.user_id,
                self.role,
                self.pending_message,
                thread_id=self.thread_id,
                buyer_session=self.buyer_session,
            )
            self.turn_id = committed.turn_id
            self.context_id = committed.context_id

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
    ) -> int:
        """노드별 LLM 호출 기록(model·tokens). 그래프가 호출한다."""
        self.model_calls.append(
            ModelCall(
                model,
                prompt_tokens,
                completion_tokens,
                cached_input_tokens,
                cache_write_tokens,
                usage_reserved,
                purpose,
            )
        )
        return len(self.model_calls) - 1

    def record_model_usage(
        self,
        model: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        call_id: int | None = None,
        cached_input_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> int:
        """provider usage를 호출 ID 항목에 합치고, 레거시 호출은 모델 placeholder를 찾는다."""
        if call_id is None:
            call_id = next(
                (
                    index
                    for index in range(len(self.model_calls) - 1, -1, -1)
                    if self.model_calls[index].model == model
                    and not self.model_calls[index].usage_reserved
                    and self.model_calls[index].prompt_tokens == 0
                    and self.model_calls[index].completion_tokens == 0
                ),
                None,
            )
        if call_id is None:
            call_id = self.record_model_call(model)
        target = self.model_calls[call_id]
        if prompt_tokens is not None:
            target.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            target.completion_tokens = completion_tokens
        if cached_input_tokens is not None:
            target.cached_input_tokens = cached_input_tokens
        if cache_write_tokens is not None:
            target.cache_write_tokens = cache_write_tokens
        return call_id

    def set_lane(self, lane: str) -> None:
        """집계용 bounded 레인을 확정한다."""
        self.lane = lane

    def mark_degraded(self, reason: str) -> None:
        """trace에서 이미 우선순위가 확정된 단일 degrade 사유를 복제한다."""
        self.degraded = True
        self.degrade_reason = reason

    def record_tool_call(self) -> None:
        """실제 판매자 도구 실행 횟수를 누적한다."""
        self.tool_calls += 1

    def record_search_result(self, candidates: int, total_count: int, elapsed_ms: int) -> None:
        """검색 호출 수와 후보·전체 건수·종단 지연의 턴 내 최댓값을 누적한다."""
        self.search_calls += 1
        self.search_candidates_max = max(self.search_candidates_max or candidates, candidates)
        self.search_total_count_max = max(self.search_total_count_max or total_count, total_count)
        self.search_elapsed_ms_max = max(self.search_elapsed_ms_max or elapsed_ms, elapsed_ms)

    def record_memory_context(
        self,
        *,
        recent_tokens: int,
        situation_tokens: int,
        evicted_tokens: int,
        compaction_triggered: bool,
    ) -> None:
        """원문 없이 메모리 계층별 추정 토큰과 압축 판정만 기록한다."""
        self.recent_history_tokens = max(recent_tokens, 0)
        self.situation_memory_tokens = max(situation_tokens, 0)
        self.evicted_history_tokens = max(evicted_tokens, 0)
        self.memory_compaction_triggered = compaction_triggered

    def note_active_streams(self, count: int) -> None:
        """실제 열린 스트림 수를 기록한다. 최초 표본은 도착 부하, peak는 턴 중 최악 부하다."""
        if self.active_streams is None:
            self.active_streams = count
        self.active_streams_peak = max(self.active_streams_peak or count, count)

    @staticmethod
    def _call_cost_usd(call: ModelCall, settings) -> float | None:  # noqa: ANN001
        input_price = settings.model_price_in_per_1k.get(call.model)
        output_price = settings.model_price_out_per_1k.get(call.model)
        if input_price is None or output_price is None:
            return None
        cached_price = getattr(settings, "model_price_cached_in_per_1k", {}).get(
            call.model, input_price
        )
        cache_write_price = getattr(settings, "model_price_cache_write_per_1k", {}).get(
            call.model, input_price
        )
        cached = min(max(call.cached_input_tokens, 0), call.prompt_tokens)
        cache_write = min(max(call.cache_write_tokens, 0), max(call.prompt_tokens - cached, 0))
        uncached = max(call.prompt_tokens - cached - cache_write, 0)
        return (
            uncached / 1_000 * (input_price or 0.0)
            + cached / 1_000 * (cached_price or 0.0)
            + cache_write / 1_000 * (cache_write_price or 0.0)
            + call.completion_tokens / 1_000 * (output_price or 0.0)
        )

    def _cost_usd(self) -> float:
        """Settings 단가표로 모델 호출 비용을 계산한다(미등록 모델은 0 + 경고)."""
        settings = get_settings()
        total = 0.0
        warned: set[str] = set()
        for call in self.model_calls:
            call_cost = self._call_cost_usd(call, settings)
            if call_cost is None:
                if call.model not in warned:
                    logger.warning(
                        "모델 단가 미등록 model=%s code=MODEL_PRICE_MISSING",
                        call.model,
                    )
                    warned.add(call.model)
                continue
            total += call_cost
        return round(total, 8)

    @property
    def server_first_event_ms(self) -> int | None:
        if self.first_event_at is None:
            return None
        return round((self.first_event_at - self.started) * 1000)

    @property
    def server_first_text_token_ms(self) -> int | None:
        if self.first_text_token_at is None:
            return None
        return round((self.first_text_token_at - self.started) * 1000)

    def record_frame(self, frame: str, now: float) -> None:
        """첫 SSE 이벤트와 첫 non-empty token 텍스트를 분리해 기록한다.

        `record_frame`은 모든 SSE 프레임마다 불리는 핫 패스라, 프레임을 한 번만 파싱하고
        (`_parse_frame_payload`) token/progress 추출은 그 결과에서 각각 뽑는다 — 예전엔 두
        추출 함수가 독립적으로 `json.loads`를 해 프레임당 파싱 비용이 배가됐었다(PR #407
        리뷰). `first_event_at`은 파싱 성공 여부와 무관하게 프레임 도착 자체로 기록한다.
        """
        if frame.strip() and self.first_event_at is None:
            self.first_event_at = now
        payload = _parse_frame_payload(frame)
        text = _extract_token_text(payload)
        if text:
            if self.first_text_token_at is None:
                self.first_text_token_at = now
            self.assistant_parts.append(text)
        stage = _extract_progress_stage(payload)
        if stage and stage not in self.progress_stages:
            self.progress_stages[stage] = round((now - self.started) * 1000)

    async def finish(
        self,
        now: float,
        status: TurnStatus,
        error_type: str | None = None,
        terminal_reason: str = "eof",
    ) -> None:
        """취소와 동시 호출에도 하나의 cleanup task로 턴·로그·trace를 마감한다."""
        task = self._finish_task
        if task is None:
            task = asyncio.create_task(
                self._complete_finish(now, status, error_type, terminal_reason)
            )
            self._finish_task = task
        await asyncio.shield(task)

    async def _complete_finish(
        self,
        now: float,
        status: TurnStatus,
        error_type: str | None,
        terminal_reason: str,
    ) -> None:
        if self.turn_id is not None:
            try:
                await self.store.finalize_assistant(
                    self.turn_id, "".join(self.assistant_parts), status
                )
            except Exception:
                # 대화 저장(§6.3 a) 실패가 아래 구조화 로그(§6.3 b) emit 까지 막으면 안 된다 —
                # 별개 계약이라 한쪽 실패로 다른 쪽을 통째로 유실시키면 안 된다. finalize_assistant
                # 는 이제 실 pg-profile I/O 라 실패할 수 있는데, 여기서 전파하면 아래 chat_request
                # 로그(latency·model·tokens·streamStatus 등)가 통째로 유실되고 finished=True 라
                # 재시도 여지도 없다(PR #48 후속 리뷰). 실패는 관측 가능하게 남기고 계속 진행한다.
                logger.error(
                    "finalize_assistant 실패 turnFp=%s code=CONVERSATION_FINALIZE_FAILED",
                    identifier_fingerprint(self.turn_id),
                )
            stream_status = status.value
        else:
            stream_status = None  # 스트림 시작 전 거부(409 등) — 저장된 턴 없음

        latency_total_ms = round((now - self.started) * 1000)
        identity_fields = (
            {
                "sellerFp": identifier_fingerprint(self.user_id),
                "brandFp": (
                    identifier_fingerprint(str(self.brand_id))
                    if self.brand_id is not None
                    else None
                ),
            }
            if self.role == "seller"
            else {"ownerFp": identifier_fingerprint(self.user_id)}
        )
        try:
            cost_usd = self._cost_usd()
        except Exception:
            logger.warning("model cost calculation failed code=MODEL_COST_CALCULATION_FAILED")
            cost_usd = 0.0
        compaction_calls = [
            call for call in self.model_calls if call.purpose == "memory_compaction"
        ]
        try:
            settings = get_settings()
            compaction_cost_usd = round(
                sum(self._call_cost_usd(call, settings) or 0.0 for call in compaction_calls),
                8,
            )
        except Exception:
            logger.warning(
                "memory compaction cost calculation failed "
                "code=MEMORY_COMPACTION_COST_CALCULATION_FAILED"
            )
            compaction_cost_usd = 0.0
        record = {
            "event": "chat_request",
            "requestId": self.request_id,
            **identity_fields,
            "role": self.role,
            "sessionFp": identifier_fingerprint(self.conversation_id),
            "contextFp": identifier_fingerprint(self.context_id),
            "threadFp": identifier_fingerprint(self.thread_id),
            "latencyFirstToken": self.server_first_text_token_ms,
            "latencyTotal": latency_total_ms,
            # 워커별 값이다 — 합산은 workerFp 로 갈라서 한다(DESIGN-SHARED-STREAM-REGISTRY-476 §2.1).
            "activeStreams": self.active_streams,
            "activeStreamsPeak": self.active_streams_peak,
            "workerFp": worker_fingerprint(),
            "model": [m.model for m in self.model_calls] or None,
            "promptTokens": sum(m.prompt_tokens for m in self.model_calls),
            "completionTokens": sum(m.completion_tokens for m in self.model_calls),
            "cachedInputTokens": sum(m.cached_input_tokens for m in self.model_calls),
            "cacheWriteTokens": sum(m.cache_write_tokens for m in self.model_calls),
            "recentHistoryTokens": self.recent_history_tokens,
            "situationMemoryTokens": self.situation_memory_tokens,
            "evictedHistoryTokens": self.evicted_history_tokens,
            "memoryCompactionTriggered": self.memory_compaction_triggered,
            "memoryCompactionPromptTokens": sum(m.prompt_tokens for m in compaction_calls),
            "memoryCompactionCompletionTokens": sum(m.completion_tokens for m in compaction_calls),
            "memoryCompactionCostUsd": compaction_cost_usd,
            "lane": self.lane,
            "degraded": self.degraded,
            "degradeReason": self.degrade_reason,
            "costUsd": cost_usd,
            "toolCalls": self.tool_calls,
            "searchCalls": self.search_calls,
            "searchCandidatesMax": self.search_candidates_max,
            "searchTotalCountMax": self.search_total_count_max,
            "searchElapsedMsMax": self.search_elapsed_ms_max,
            "errorType": error_type,
            "streamStatus": stream_status,
            "messageLength": self.message_length,
            "messageHash": self.message_hash,
            # [PII] 사용자 message 원문은 여기에 절대 포함하지 않는다(§6.3 b).
            # 닫힌 어휘(stage 이름)만 담는다 — 이슈 #396, record_frame/_extract_progress_stage.
            "progressStages": self.progress_stages or None,
        }
        logger.info(json.dumps(record, ensure_ascii=False))
        if self.trace is not None:
            self.trace.record_server_timings(
                first_event_ms=self.server_first_event_ms,
                first_text_token_ms=self.server_first_text_token_ms,
            )
            await finish_trace_safely(
                self.trace,
                status=status,
                error_type=error_type,
                terminal_reason=terminal_reason,
            )
        self.finished = True


def _parse_frame_payload(frame: str) -> dict | None:
    """SSE `data:` 프레임을 **한 번만** 파싱해 payload dict 를 돌려준다.

    비-JSON·JSON 이지만 dict 가 아닌 프레임은 `None` — 호출부(`record_frame`)는 이 `None`
    을 그대로 "추출 실패"로 받아들이면 되므로 예외가 전파되지 않는다. `_extract_token_text`·
    `_extract_progress_stage` 가 이전엔 각자 이 파싱(strip → `data:` 제거 → `json.loads`)을
    독립적으로 했는데, `record_frame` 이 모든 SSE 프레임마다 불리는 핫 패스라 프레임당 파싱
    비용이 배가되고 있었다(PR #407 리뷰) — 파싱을 여기 한 곳으로 모으고 타입별 필드 추출은
    이 결과에서 한다.
    """
    try:
        line = frame.strip()
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
        payload = json.loads(line)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _extract_token_text(payload: dict | None) -> str | None:
    """이미 파싱된 프레임 payload 에서 token 이벤트의 text 만 추출한다."""
    if payload is None or payload.get("type") != "token":
        return None
    data = payload.get("data") or {}
    text = data.get("text") if isinstance(data, dict) else None
    return text if isinstance(text, str) else None


def _extract_progress_stage(payload: dict | None) -> str | None:
    """이미 파싱된 프레임 payload 에서 구매자 `progress` 이벤트의 `stage` 만 추출한다(이슈 #396).

    판매자 `progress`(`{"text": …}`)는 `stage` 키가 없어 `None` 을 돌려주므로 두 레인이
    섞이지 않는다 — 닫힌 어휘(stage 이름)만 다루며 사용자 문구·검색어는 절대 싣지 않는다.
    """
    if payload is None or payload.get("type") != "progress":
        return None
    data = payload.get("data") or {}
    stage = data.get("stage") if isinstance(data, dict) else None
    return stage if isinstance(stage, str) else None


def start_observation(
    *,
    request_id: str,
    identity: Identity,
    conversation_id: str,
    thread_id: str | None = None,
    message: str,
    store: ConversationStoreProtocol,
    now: float,
    buyer_session: BuyerSessionInput | None = None,
    trace: RequestTrace | None = None,
) -> RequestObservation:
    """사용자 메시지를 저장(§6.3 a)하고 관측 컨텍스트를 만든다. 원문은 저장소에만, 로그엔 지문만."""
    length, digest = message_fingerprint(message)
    role = role_of(identity)
    subject = identity.user_id or identity.subject
    # 메시지 저장은 open_stream 의 슬롯 확보 후 commit_user_message()에서(유령 턴 방지).
    # 저장 키는 대화 저장소 내부 신원 스코프이며 buyer transient 상태는 commit 결과의
    # lifecycle context_id:thread_id만 사용한다. 외부 로그에는 식별자 지문만 남긴다.
    observation = RequestObservation(
        request_id=request_id,
        conversation_id=conversation_id,
        thread_id=thread_id,
        user_id=subject,
        brand_id=identity.brand_id,
        role=role,
        store=store,
        message_length=length,
        message_hash=digest,
        started=now,
        pending_message=message,
        pending_key=conversation_key(subject, conversation_id),
        buyer_session=buyer_session,
        trace=trace,
    )
    if trace is not None:
        trace.attach_observation(observation)
    return observation


def emit_rejection(request_id: str, error_type: str, **fields: object) -> None:
    """스트림 전 거부(429/409/504 등)의 구조화 로그 — 대화 턴 없이 errorType 만 집계(§6.3 b).

    레이트 리밋(§2.8)·409(§2.9 a) 발동을 상한 튜닝 근거로 관측 가능하게 남긴다.
    """

    def _take(*keys: str) -> object | None:
        found = None
        for key in keys:
            value = fields.pop(key, None)
            if found is None and value is not None:
                found = value
        return found

    raw_owner = _take("userId", "ownerId", "guestId", "subject", "sub")
    raw_seller = _take("sellerId")
    raw_brand = _take("brandId")
    raw_session = _take("conversationId", "sessionId")
    raw_thread = _take("threadId")
    raw_stream = _take("streamKey", "stream_key")
    raw_context = _take("contextId")
    raw_ip = _take("ip", "ipAddress", "clientIp")
    raw_scope = _take("scope")
    scope_type = None
    scope_owner = None
    if raw_scope is not None:
        scope_text = str(raw_scope)
        prefix, separator, value = scope_text.partition(":")
        scope_type = prefix if separator and prefix in {"sub", "ip"} else "other"
        if scope_type == "sub" and value:
            scope_owner = value
        elif scope_type == "ip" and value and raw_ip is None:
            raw_ip = value

    # 임의 **fields를 그대로 병합하지 않는다. 새 필드는 비민감 allowlist에 명시적으로
    # 추가해야 하며, 식별자처럼 보이는 미지 키는 기본적으로 폐기한다.
    safe_text_values = {
        "path": {"/chat", "/seller/chat"},
        "role": {"member", "guest", "seller"},
        "status": {"COMPLETED", "FAILED", "CANCELLED"},
        "action": {"confirm"},
    }
    safe_fields = {
        key: value
        for key, allowed in safe_text_values.items()
        if isinstance((value := fields.get(key)), str) and value in allowed
    }
    retryable = fields.get("retryable")
    if isinstance(retryable, bool):
        safe_fields["retryable"] = retryable

    def _branded_fingerprint(key: str) -> str | None:
        value = fields.get(key)
        return str(value) if isinstance(value, _LogFingerprint) else None

    provided_owner_fp = _branded_fingerprint("ownerFp")
    provided_scope_fp = _branded_fingerprint("scopeFp")
    provided_ip_fp = _branded_fingerprint("ipFp")
    provided_scope_type = fields.get("scopeType")
    if not isinstance(provided_scope_type, str) or provided_scope_type not in {
        "sub",
        "ip",
        "other",
    }:
        provided_scope_type = None

    record = {
        "event": "chat_request",
        "requestId": request_id,
        "errorType": error_type,
        "streamStatus": None,
        # 스트림 전 거부는 라우팅보다 먼저 일어나 실제 lane을 확정할 수 없다.
        "lane": None,
        "degraded": False,
        "degradeReason": None,
        "costUsd": 0.0,
        "toolCalls": 0,
        "workerFp": worker_fingerprint(),
        "ownerFp": (
            identifier_fingerprint(str(raw_owner if raw_owner is not None else scope_owner))
            if raw_owner is not None or scope_owner is not None
            else provided_owner_fp
        ),
        "sellerFp": identifier_fingerprint(str(raw_seller)) if raw_seller is not None else None,
        "brandFp": identifier_fingerprint(str(raw_brand)) if raw_brand is not None else None,
        "sessionFp": identifier_fingerprint(str(raw_session)) if raw_session is not None else None,
        "threadFp": identifier_fingerprint(str(raw_thread)) if raw_thread is not None else None,
        "contextFp": identifier_fingerprint(str(raw_context)) if raw_context is not None else None,
        "streamFp": identifier_fingerprint(str(raw_stream)) if raw_stream is not None else None,
        "scopeFp": (
            identifier_fingerprint(str(raw_scope)) if raw_scope is not None else provided_scope_fp
        ),
        "scopeType": scope_type if scope_type is not None else provided_scope_type,
        "ipFp": (identifier_fingerprint(str(raw_ip)) if raw_ip is not None else provided_ip_fp),
        **safe_fields,
    }
    logger.info(json.dumps(record, ensure_ascii=False))
