"""SSE 스트림 수명주기 (api-spec §2.9) — 동시 스트림 레지스트리 + 취소/타임아웃.

`POST /chat`·`POST /seller/chat` 공통. 엔드포인트의 내부 이벤트 제너레이터를
`open_stream()` 으로 감싸면:
  (a) 동시 스트림 제한 — threadId 당 1개, 초과 시 409 STREAM_IN_PROGRESS(§2.9 a)
  (b) 취소 — 연결 종료 시 Starlette 가 응답 task 를 취소 → finally 로 정리(레지스트리
      해제 + 내부 제너레이터 aclose 로 LLM 스트림/그래프 task 전파). 제너레이터가 유휴일
      때는 is_disconnected() 폴링으로도 조기 감지(§2.9 b)
  (c) 타임아웃 — 역할 공통 first-token 상한 초과 시 스트림 전 504, 역할별 전체 상한
      (구매자 stream_total_timeout_buyer_s, 판매자·미지정 stream_total_timeout_s) 초과 시 절단(§2.9 c)

레지스트리가 프로세스 로컬인 동안 워커 다중화는 금지다. 레지스트리 자체는 이제
`STREAM_REGISTRY_BACKEND=shared` 로 워커 간 공유로 올릴 수 있지만(#476,
`docs/specs/DESIGN-SHARED-STREAM-REGISTRY-476.md`), 다른 프로세스 로컬 상태의 선행조건이
남으므로 증설 판단과 전환 순서는 `docs/specs/OPS-SCALEOUT-476.md`를 따른다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import nullcontext
from typing import Any, Literal

from fastapi import HTTPException, Request, status
from fastapi.responses import StreamingResponse
from langsmith.run_helpers import tracing_context

from app.core.auth import Identity
from app.core.config import get_settings
from app.core.conversation import TurnStatus
from app.core.errors import new_request_id
from app.core.logging import get_logger
from app.core.observability import RequestObservation, identifier_fingerprint
from app.core.session_context import SessionStateUnavailable
from app.core.stream_registry import (
    ActiveStreamRegistry,
    InMemorySharedStreamStore,
    PostgresSharedStreamStore,
    SharedStreamRegistry,
    SharedStreamStore,
    StreamScopeFence,
    build_registry,
    close_stream_registry,
    get_registry,
    initialize_stream_registry,
    registry_is_process_local,
    set_registry,
)
from app.core.tracing import RequestTrace, bind_request_trace
from app.schemas.chat import DoneData, ErrorData

logger = get_logger(__name__)

# 레지스트리 구현은 app/core/stream_registry.py 로 분리했다. 기존 import 경로를 유지하기 위해
# 여기서 재수출한다.
__all__ = [
    "ActiveStreamRegistry",
    "InMemorySharedStreamStore",
    "PostgresSharedStreamStore",
    "SharedStreamRegistry",
    "SharedStreamStore",
    "StreamScopeFence",
    "build_registry",
    "close_stream_registry",
    "get_registry",
    "initialize_stream_registry",
    "open_stream",
    "registry_is_process_local",
    "registry_key",
    "set_registry",
]

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # 리버스 프록시 버퍼링 비활성 (api-spec §2.4)
}


def _note_active_streams(
    observer: RequestObservation | None,
    registry: ActiveStreamRegistry,
) -> None:
    """관측 실패가 스트림 슬롯·응답 수명주기를 바꾸지 않게 활성 수 표본을 격리한다."""
    if observer is None:
        return
    try:
        observer.note_active_streams(registry.active_count())
    except Exception:
        logger.warning("active stream observation failed code=ACTIVE_STREAM_OBSERVATION_FAILED")


async def _renew_stream_lease(registry: ActiveStreamRegistry, stream_key: str) -> None:
    """공유 백엔드 lease 연장을 이미 도는 폴링 tick 에 얹는다 (#476, SPEC §4).

    `lease_renewal_due()` 는 **sync** 라 인메모리 백엔드에서는 코루틴조차 만들지 않고 즉시
    False 다 — 프레임마다 DB 를 치지 않고, 기본 배포의 핫 패스는 그대로다. 연장 실패가 스트림을
    죽이지 않게 `_note_active_streams` 와 같은 예외 격리를 유지한다(부기 실패가 응답 수명주기를
    바꾸면 안 된다 — #48 슬롯 누수의 성질).
    """
    if not registry.lease_renewal_due(stream_key):
        return
    try:
        await registry.renew_lease(stream_key)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("stream lease renewal failed code=STREAM_LEASE_RENEW_FAILED")


def registry_key(identity: Identity, thread_id: str) -> str:
    """동시성 레지스트리 키를 **인증된 신원**에 묶는다 (§2.9 a IDOR 방지).

    키를 본문 thread_id 만으로 쓰면 다른 사용자가 남의 thread_id 를 body 에 실어
    동시성 슬롯을 선점(피해자에 409 유발)할 수 있다. owner(판매자/회원 식별자, 게스트는
    "guest")를 접두어로 붙여 사용자 간 슬롯 침범을 막는다.
    """
    # subject(검증된 sub)는 게스트 UUID 포함 모든 역할에 보존된다(auth.Identity). 이걸로
    # 키를 묶어야 게스트끼리도 서로의 슬롯을 침범하지 못한다. 신원 없음(dev 무토큰)은 "anon".
    owner = identity.subject or "anon"
    return f"{owner}:{thread_id}"


def _done_stop_frame(role: Literal["buyer", "seller"] | None = None) -> str:
    """전체 상한 초과 시의 정상 절단 done 프레임 (finishReason stop).

    [이슈 #621 ③] role=="seller" 는 판매자 전용 페이로드({"finishReason":"stop",
    "panel":"keep"})를 직접 구성한다 — 구매자 DoneData 스키마(app/schemas/chat.py)는
    panel 필드가 없어 건드리지 않는다(app/api/seller.py::_done() 과 같은 방식). 절단
    전에는 이미 `_confirm_stream` 등이 `panel` 을 실은 done 을 내지만, 총 상한 절단은
    이 함수가 단독으로 만들어 role 을 몰랐다 — confirm 이 절단되면 FE 의
    invalidateQueries(["seller"]) 가 안 걸려 쓰기가 반영된 뒤에도 우측 목록이 옛 값으로
    남는 문제(#621 문제③)를 여기서 고친다. role 미지정·"buyer" 는 기존 동작 그대로다.
    """
    import json

    if role == "seller":
        payload = {"type": "done", "data": {"finishReason": "stop", "panel": "keep"}}
    else:
        payload = {"type": "done", "data": DoneData(finish_reason="stop").model_dump(by_alias=True)}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _error_frame(
    code: str,
    message: str,
    *,
    request_id: str,
    retryable: bool,
) -> str:
    """스트림 시작 후 오류의 in-stream `error` 프레임 (api-spec §3.1, §2.9 c)."""
    import json

    payload = {
        "type": "error",
        "data": ErrorData(
            code=code,
            message=message,
            request_id=request_id,
            retryable=retryable,
        ).model_dump(by_alias=True),
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _safe_finish(
    observer: RequestObservation, stream_key: str, loop_time: float, *args: object
) -> None:
    """observer.finish() 를 방어적으로 호출한다.

    finish() 는 이제 pg-profile 실 DB 쓰기(finalize_assistant)를 거쳐 실패할 수 있다.
    호출부 대부분이 이 결과를 받아 곧장 HTTPException/CancelledError 를 (재)전파하는데,
    방어 없이 await 하면 finish() 자신의 새 예외가 그 원래 예외를 가려버린다 —
    _wrapped() finally 에서 고친 것과 동일한 문제가 open_stream() 앞부분 호출부들에도
    있었다(PR #48 후속 리뷰). 로그만 남기고 삼켜 원래 흐름을 지킨다.
    """
    try:
        await observer.finish(loop_time, *args)
    except Exception:
        logger.exception("observer.finish 실패 streamFp=%s", identifier_fingerprint(stream_key))


def _terminal_event_of(frame: str) -> tuple[str | None, str | None]:
    """SSE 종결 이벤트의 terminal reason 과 error code 를 반환한다."""
    import json

    try:
        line = frame.strip()
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
        payload = json.loads(line)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    event_type = payload.get("type")
    if event_type == "done":
        return "done", None
    if event_type != "error":
        return None, None
    data = payload.get("data") or {}
    code = data.get("code") if isinstance(data, dict) else None
    return "error_frame", code if isinstance(code, str) else "INTERNAL"


_PUMP_EOF = object()


class _IteratorPump:
    """One-task, demand-driven owner for an async iterator and its trace context."""

    def __init__(self, iterator: AsyncIterator[str], trace: RequestTrace | None) -> None:
        self._iterator = iterator
        self._trace = trace
        self._demands: asyncio.Queue[asyncio.Future[object]] = asyncio.Queue(maxsize=1)
        self._terminal_ack = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        self._stopped = False

    def request(self) -> asyncio.Future[object]:
        """Submit exactly one pull without allowing eager prefetch."""
        if self._stopped:
            raise RuntimeError("stream iterator pump is stopped")
        reply: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._demands.put_nowait(reply)
        return reply

    async def cancel_and_wait(self) -> None:
        if not self._task.done():
            self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def wait_closed(self) -> None:
        await self._task

    def acknowledge_terminal(self) -> None:
        self._terminal_ack.set()

    def done(self) -> bool:
        return self._task.done()

    async def _run(self) -> None:
        current_reply: asyncio.Future[object] | None = None
        context: Any = bind_request_trace(self._trace) if self._trace is not None else nullcontext()
        try:
            # LANGSMITH_TRACING is the application feature flag as well as an SDK
            # auto-tracing flag. Disable the latter at the persistent producer task
            # boundary so every seller LangGraph/LLM/tool child task inherits the
            # suppression while our independent RequestTrace context stays active.
            with tracing_context(enabled=False):
                with context:
                    while True:
                        current_reply = await self._demands.get()
                        try:
                            item = await self._iterator.__anext__()
                        except StopAsyncIteration:
                            self._stopped = True
                            current_reply.set_result(_PUMP_EOF)
                            current_reply = None
                            break
                        except asyncio.CancelledError as exc:
                            # A generator-raised cancellation is a stream outcome. A task
                            # cancellation has a positive cancelling() count and must tear
                            # down the producer immediately.
                            if asyncio.current_task() and asyncio.current_task().cancelling():
                                raise
                            self._stopped = True
                            current_reply.set_exception(exc)
                            current_reply = None
                            break
                        except BaseException as exc:
                            self._stopped = True
                            current_reply.set_exception(exc)
                            current_reply = None
                            break

                        current_reply.set_result(item)
                        current_reply = None
                        if _terminal_event_of(item)[0] is not None:
                            self._stopped = True
                            await self._terminal_ack.wait()
                            break
        finally:
            self._stopped = True
            if current_reply is not None and not current_reply.done():
                current_reply.cancel()
            try:
                await self._iterator.aclose()  # type: ignore[attr-defined]
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("stream iterator close failed code=STREAM_CLOSE_FAILED")


class _ResponseLifecycle:
    """Idempotent owner armed before a StreamingResponse leaves open_stream()."""

    def __init__(
        self,
        *,
        pump: _IteratorPump,
        registry: ActiveStreamRegistry,
        stream_key: str,
        observer: RequestObservation | None,
        loop: asyncio.AbstractEventLoop,
        prefetched_terminal: bool,
    ) -> None:
        self._pump = pump
        self._registry = registry
        self._stream_key = stream_key
        self._observer = observer
        self._loop = loop
        self._prefetched_terminal = prefetched_terminal
        self._lock = asyncio.Lock()
        self._finished = False

    async def finish(
        self,
        *,
        stream_status: TurnStatus,
        error_type: str | None,
        terminal_reason: str,
        natural_stop: bool,
        pending_reply: asyncio.Future[object] | None = None,
    ) -> None:
        async with self._lock:
            if self._finished:
                return
            try:
                if pending_reply is not None and not pending_reply.done():
                    pending_reply.cancel()
                if natural_stop or self._prefetched_terminal:
                    self._pump.acknowledge_terminal()
                if natural_stop or self._prefetched_terminal or self._pump.done():
                    await self._pump.wait_closed()
                else:
                    await self._pump.cancel_and_wait()
            finally:
                await self._registry.release(self._stream_key)
                if self._observer is not None:
                    await _safe_finish(
                        self._observer,
                        self._stream_key,
                        self._loop.time(),
                        stream_status,
                        error_type,
                        terminal_reason,
                    )
                self._finished = True

    async def cancel(self) -> None:
        await self.finish(
            stream_status=TurnStatus.CANCELLED,
            error_type=None,
            terminal_reason="client_disconnect",
            natural_stop=False,
        )


class _ArmedBodyIterator:
    """Async iterator whose close hook works even before its first __anext__."""

    def __init__(self, iterator: AsyncIterator[str], lifecycle: _ResponseLifecycle) -> None:
        self._iterator = iterator
        self._lifecycle = lifecycle
        self.started = False

    def __aiter__(self) -> _ArmedBodyIterator:
        return self

    async def __anext__(self) -> str:
        self.started = True
        return await self._iterator.__anext__()

    async def aclose(self) -> None:
        if self.started:
            await self._iterator.aclose()  # type: ignore[attr-defined]
        else:
            await self._lifecycle.cancel()

    async def abort(self) -> None:
        await self._lifecycle.cancel()
        if self.started:
            await self._iterator.aclose()  # type: ignore[attr-defined]


class _LifecycleStreamingResponse(StreamingResponse):
    """StreamingResponse that owns cancellation in the header/body handoff."""

    body_iterator: _ArmedBodyIterator

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            await self.body_iterator.abort()
            raise


async def open_stream(
    request: Request,
    stream_key: str,
    inner_factory: Callable[[float], AsyncIterator[str]],
    *,
    observer: RequestObservation | None = None,
    role: Literal["buyer", "seller"] | None = None,
) -> StreamingResponse:
    """내부 이벤트 제너레이터를 §2.9 수명주기로 감싼 StreamingResponse 를 만든다.

    스트림 시작 전 실패는 예외로 던져 §2.5 봉투로 나간다:
      - 409 STREAM_IN_PROGRESS: 동일 threadId 활성 스트림 존재
      - 504 UPSTREAM_TIMEOUT: first-token 상한 초과 (첫 이벤트 도착 전)

    role 미지정은 기존 전체 상한을 유지한다. 명시하지 않은 호출자가 조용히 더 좁은 구매자
    상한으로 잘리지 않도록, 역할을 명시한 호출자만 좁은 예산을 받는다.

    활성 스트림 표본은 실제 열린 수다. acquire 성공 표본은 자신을 포함하고, 409 거절 표본은
    자신을 제외한다.

    [#427, DESIGN-SHARED-BUDGET-384 §3 D2] `inner_factory` 는 이 함수가 실제로 `deadline`
    계산에 쓰는 `start`(`loop.time()`)를 받는다 — 구제 체인 공유 예산(`rescue_deadline`)이
    이 스트림의 실제 데드라인과 같은 원점에서 파생돼야 `rescue_deadline ≤ deadline` 부등식이
    항상 성립한다는 것을 보장할 수 있다(D2). 아래 `start = loop.time()` 대입 **바로 아래**에서
    호출해야 한다 — 순서가 바뀌면 다른 원점을 넘기게 된다.
    """
    settings = get_settings()
    registry = get_registry()
    loop = asyncio.get_event_loop()
    stream_request_id = observer.request_id if observer is not None else new_request_id()
    trace = observer.trace if observer is not None else None

    buyer_session = getattr(observer, "buyer_session", None)
    try:
        acquired = await registry.acquire(
            stream_key,
            owner_id=buyer_session.owner_id if buyer_session is not None else None,
            session_id=buyer_session.session_id if buyer_session is not None else None,
        )
    except SessionStateUnavailable:
        # 공유 백엔드 저장소 장애 — fail-closed. fail-open 하면 다중 워커에서 §2.9(a) 가드가
        # 사라져 동시 스트림이 조용히 겹친다(#476 SPEC §6). 계약에 이미 있는 503
        # STATE_UNAVAILABLE 로 나간다(app.core.errors 매핑, 새 오류 코드 없음).
        if observer is not None:
            await _safe_finish(
                observer,
                stream_key,
                loop.time(),
                TurnStatus.FAILED,
                "STATE_UNAVAILABLE",
                "store_unavailable",
            )
        raise
    if not acquired:
        if observer is not None:
            _note_active_streams(observer, registry)
            await _safe_finish(
                observer,
                stream_key,
                loop.time(),
                TurnStatus.FAILED,
                "STREAM_IN_PROGRESS",
                "stream_in_progress",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "STREAM_IN_PROGRESS",
                "message": "동일 방에 진행 중인 스트림이 있습니다",
            },
        )

    if observer is not None:
        _note_active_streams(observer, registry)
        try:
            # 슬롯 확보 후에만 사용자 메시지 저장(§6.3 a, 유령 턴 방지). 이제 실제 pg-profile
            # I/O 라 DB 오류·클라이언트 disconnect 로 예외를 던질 수 있다 — release 없이
            # 전파되면 해당 threadId가 프로세스 재시작 전까지 영구히 409를 반환한다
            # (슬롯 영구 누수, PR #48 리뷰).
            await observer.commit_user_message()
        except asyncio.CancelledError:
            # disconnect 로 이 DB 쓰기 중 취소되면 CancelledError(BaseException)라 아래
            # except Exception 으로는 안 잡혀 release 가 스킵됐었다(슬롯 영구 누수, PR #48
            # 후속 리뷰) — 명시적으로 잡아 슬롯을 풀고 CANCELLED 로 마감 후 취소를 재전파한다.
            await registry.release(stream_key)
            await _safe_finish(
                observer,
                stream_key,
                loop.time(),
                TurnStatus.CANCELLED,
                None,
                "client_disconnect",
            )
            raise
        except Exception:
            await registry.release(stream_key)
            await _safe_finish(
                observer,
                stream_key,
                loop.time(),
                TurnStatus.FAILED,
                "INTERNAL",
                "store_unavailable",
            )
            raise

    start = loop.time()
    try:
        agen = inner_factory(start)
    except Exception:
        # 그래프 진입 검증 등 inner_factory 동기 예외 — 슬롯·턴 누수 방지.
        await registry.release(stream_key)
        if observer is not None:
            await _safe_finish(
                observer, stream_key, loop.time(), TurnStatus.FAILED, "INTERNAL", "tool_error"
            )
        raise
    pump = _IteratorPump(agen, trace)

    poll = settings.stream_disconnect_poll_s
    total_timeout = (
        settings.stream_total_timeout_buyer_s
        if role == "buyer"
        else settings.stream_total_timeout_s
    )
    deadline = start + total_timeout
    ft_deadline = start + settings.stream_first_token_timeout_s

    async def _empty_stream() -> AsyncIterator[str]:
        return
        yield  # pragma: no cover - 빈 스트림(제너레이터화용 unreachable)

    # (c) first-token 상한 — 첫 이벤트 도착 전이므로 아직 200 헤더 전. 초과 시 504(§2.5).
    # (b) 이 대기 구간에서도 disconnect 를 폴링한다 — 첫 이벤트 전에 클라이언트가 떠나면
    #     상류 LLM 비용·레지스트리 슬롯을 first-token 상한(기본 10s)까지 붙들지 않는다.
    # wait_for 로 reply를 취소하지 않고 persistent producer의 단일 demand를 폴링한다.
    # 스트림 반환 전 실패는 _wrapped finally 가 안 도므로 여기서 해제하고, 정상/빈 스트림만
    # _wrapped finally 한 곳에서 해제한다(이중 해제 레이스 방지).
    first_reply = pump.request()

    async def _abort_prestream() -> None:
        try:
            if not first_reply.done():
                first_reply.cancel()
            await pump.cancel_and_wait()
        finally:
            await registry.release(stream_key)

    first: str | None = None
    try:
        while True:
            remaining = ft_deadline - loop.time()
            if remaining <= 0:
                await _abort_prestream()
                if observer is not None:
                    await _safe_finish(
                        observer,
                        stream_key,
                        loop.time(),
                        TurnStatus.FAILED,
                        "UPSTREAM_TIMEOUT",
                        "first_event_timeout",
                    )
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail={"code": "UPSTREAM_TIMEOUT", "message": "상류(LLM) 응답 지연"},
                )
            completed, _ = await asyncio.wait({first_reply}, timeout=min(remaining, poll))
            await _renew_stream_lease(registry, stream_key)
            if observer is not None:
                _note_active_streams(observer, registry)
            if first_reply in completed:
                try:
                    result = first_reply.result()
                    if result is _PUMP_EOF:
                        first = None
                    else:
                        first = result  # type: ignore[assignment]
                except StopAsyncIteration:  # pragma: no cover - pump converts EOF to sentinel
                    first = None  # 빈 스트림 — 해제는 _wrapped finally 에서.
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # 첫 프레임 전 상류 오류(LLM/Spring 등) — 누수 방지 후 전파.
                    await _abort_prestream()
                    if observer is not None:
                        await _safe_finish(
                            observer,
                            stream_key,
                            loop.time(),
                            TurnStatus.FAILED,
                            "INTERNAL",
                            "tool_error",
                        )
                    raise
                break
            if await request.is_disconnected():
                # (b) 첫 이벤트 전 연결 종료 — 정리 후 즉시 종료(소비될 응답 없음).
                logger.info(
                    "stream cancelled before first token streamFp=%s",
                    identifier_fingerprint(stream_key),
                )
                await _abort_prestream()
                if observer is not None:
                    await _safe_finish(
                        observer,
                        stream_key,
                        loop.time(),
                        TurnStatus.CANCELLED,
                        None,
                        "client_disconnect",
                    )
                return StreamingResponse(
                    _empty_stream(),
                    media_type="text/event-stream; charset=utf-8",
                    headers=_SSE_HEADERS,
                )
    except asyncio.CancelledError:
        await _abort_prestream()
        if observer is not None:
            await _safe_finish(
                observer,
                stream_key,
                loop.time(),
                TurnStatus.CANCELLED,
                None,
                "client_disconnect",
            )
        raise

    # 첫 이벤트 확보 — SSE readiness와 user-visible text 지연을 분리 기록(§6.3).
    if observer is not None and first is not None:
        observer.record_frame(first, loop.time())

    first_terminal = first is None or (
        first is not None and _terminal_event_of(first)[0] is not None
    )
    lifecycle = _ResponseLifecycle(
        pump=pump,
        registry=registry,
        stream_key=stream_key,
        observer=observer,
        loop=loop,
        prefetched_terminal=first_terminal,
    )

    async def _wrapped() -> AsyncIterator[str]:
        # 다음 이벤트는 persistent producer에 하나씩 demand한다. outward yield 동안에는
        # outstanding demand가 없으므로 terminal 뒤 over-pull이나 eager prefetch가 없다.
        next_reply: asyncio.Future[object] | None = None
        stream_status = TurnStatus.COMPLETED
        error_type: str | None = None
        terminal_reason = "eof"
        natural_stop = False
        try:
            if first is None:
                natural_stop = True
                return
            if first is not None:
                first_terminal, first_err = _terminal_event_of(first)
                if first_terminal is not None:
                    terminal_reason = first_terminal
                    natural_stop = True
                if first_err is not None:
                    # terminal error 상태는 프레임을 넘기기 전에 확정한다. 소비자가 이 프레임
                    # 직후 iterator 를 닫아도 finally 가 FAILED/error_type 으로 마감해야 한다.
                    stream_status = TurnStatus.FAILED
                    error_type = first_err
                yield first  # first 는 위에서 record_frame 됨(중복 누적 방지)
                if first_terminal is not None:
                    # done/error 는 모두 종결 이벤트 — 이후 프레임을 당기지 않는다.
                    return
            next_reply = pump.request()
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    # (c) 전체 상한 초과 — done(stop)으로 정상 절단(COMPLETED, FAILED 아님).
                    logger.info(
                        "stream total cap reached streamFp=%s", identifier_fingerprint(stream_key)
                    )
                    terminal_reason = "total_timeout_stop"
                    done_frame = _done_stop_frame(role=role)
                    if observer is not None:
                        observer.record_frame(done_frame, loop.time())
                    yield done_frame
                    break
                completed, _ = await asyncio.wait({next_reply}, timeout=min(remaining, poll))
                await _renew_stream_lease(registry, stream_key)
                if observer is not None:
                    _note_active_streams(observer, registry)
                if next_reply not in completed:
                    # 아직 이벤트 없음(제너레이터 유휴) — (b) 연결 종료 조기 감지.
                    if await request.is_disconnected():
                        logger.info(
                            "stream cancelled by client disconnect streamFp=%s",
                            identifier_fingerprint(stream_key),
                        )
                        stream_status = TurnStatus.CANCELLED
                        terminal_reason = "client_disconnect"
                        break
                    continue  # 같은 task 를 계속 기다린다(제너레이터 보존)
                try:
                    result = next_reply.result()
                    if result is _PUMP_EOF:
                        natural_stop = True
                        break
                    item = result  # type: ignore[assignment]
                except StopAsyncIteration:  # pragma: no cover - pump converts EOF to sentinel
                    break
                except Exception:
                    # (c) 첫 이벤트 후(=200 전송 후) 상류 오류 — 계약상 in-stream error 로
                    #     마무리해야 한다(연결만 끊기지 않게, §2.9 c/§3.1). 기본 INTERNAL.
                    #     실제 그래프는 필요 시 자체 error(LLM_UNAVAILABLE 등)를 먼저 emit 가능.
                    logger.exception(
                        "in-stream error streamFp=%s", identifier_fingerprint(stream_key)
                    )
                    stream_status = TurnStatus.FAILED
                    error_type = "INTERNAL"
                    terminal_reason = "tool_error"
                    natural_stop = True
                    error_frame = _error_frame(
                        "INTERNAL",
                        "처리 중 오류가 발생했습니다",
                        request_id=stream_request_id,
                        retryable=True,
                    )
                    if observer is not None:
                        observer.record_frame(error_frame, loop.time())
                    yield error_frame
                    break
                if observer is not None:
                    observer.record_frame(item, loop.time())  # 부분 텍스트 누적(§6.3 a)
                item_terminal, item_err = _terminal_event_of(item)
                if item_terminal is not None:
                    terminal_reason = item_terminal
                    natural_stop = True
                if item_err is not None:
                    stream_status = TurnStatus.FAILED
                    error_type = item_err
                yield item
                if item_terminal is not None:
                    # 그래프 terminal 프레임 뒤 token/done 을 당겨 응답·저장소를 오염시키지 않는다.
                    break
                next_reply = pump.request()
        except asyncio.CancelledError:
            # Starlette 가 클라이언트 disconnect 로 응답 task 를 취소한 경우 — CANCELLED 로 마감.
            stream_status = TurnStatus.CANCELLED
            terminal_reason = "client_disconnect"
            raise
        finally:
            # (b) 취소·상한·정상 종료 공통 정리: persistent producer가 내부 제너레이터를
            #     같은 task/context에서 close → 레지스트리 해제 → 대화 저장·로그 마감.
            await lifecycle.finish(
                stream_status=stream_status,
                error_type=error_type,
                terminal_reason=terminal_reason,
                natural_stop=natural_stop,
                pending_reply=next_reply,
            )

    body = _ArmedBodyIterator(_wrapped(), lifecycle)
    return _LifecycleStreamingResponse(
        body,
        media_type="text/event-stream; charset=utf-8",
        headers=_SSE_HEADERS,
    )
