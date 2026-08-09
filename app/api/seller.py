"""판매자 챗봇 엔드포인트 — POST /seller/chat (S-4, api-spec §3.2).

FE 가 Spring 발급 판매자 JWT(role=seller)로 직접 호출한다. 인증 = require_seller
(판매자 스코프·brandId 클레임 검증, 스코프 없으면 403). 신원(sellerId/brandId)은
검증된 JWT 클레임에서만 도출한다 — 요청 본문 신원은 신뢰하지 않는다(IDOR 방지).

MVP 범위(api-spec v0.14.0 §3.2, 결정 20 개정): 통계 Q&A + 상세 수정 draft 흐름.
이벤트: meta / token / progress / draft / report / done / error — done.finishReason 은 "stop" 단일.
  · meta(lane)  : 매 스트림 첫 프레임(FE 화면 전환 레인, 2026-07-22 B).
  · progress    : 분석 진행 상태(로딩 표시, 최종 답변 아님).
  · report      : 분석 레인 전용, kind=="report" 일 때 정확히 1회 — 우측 패널용
                  구조화 보고서(기간·요약·findings·한계·차트·추천 내장). token(산문)
                  뒤·done 앞. 구 chart 이벤트(v0.20.0, #242)는 legacy 폐기
                  (이슈 #296, api-spec §3.2 v0.24.0 — FE 미구현 실증으로 안전 대체).
  · done(panel) : 우측 패널 조치(replace/keep/refresh) — FE 요구 1~3.

[4-1b 3분기 배선 + 4-2 HITL 실행] 입구 판정 순서(REALIGN §4 확정):
  ① confirm 필드 선판정(request.action=="confirm", LLM 0회) → _confirm_stream:
     hitl.confirm_draft 가 존재→소유→멱등→TTL 검사 후 resume 실행(I-10/11/12).
     [2026-07-22 A-2] 승인은 최상위 action/draftId 구조화 필드로 받는다(발화≠동의).
  ①.5 추천 적용 선판정(parse_apply_message "N번 적용해줘", LLM 0회) →
     _apply_stream: 이력 recommendations[N-1] → draft 변환(4-3 §6.3).
  ② scope 선차단(check_scope, LLM 0회).
  ③ supervisor 라우팅(route_question — 장애 시 general 폴백은 함수 내부).
분기: analysis → run_analysis_pipeline(emit 큐 중계, 예외 2경우만 사과+error) /
product → draft 검증(validate_draft)·checkpoint 저장(start_draft)·draft emit /
general → 기존 astream 스트림.
스트림 수명주기(409·취소·타임아웃 §2.9 공통)는 팀 공통 래퍼 open_stream 소관 —
chat.py 와 동일하게 registry_key(identity, threadId) 로 방당 1스트림을 강제한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage
from pydantic.alias_generators import to_camel

from app.agents.seller import category_catalog, draft_session
from app.agents.seller import period_confirm as seller_period_confirm
from app.agents.seller import thread as seller_thread
from app.agents.seller.checkpoint import get_checkpointer
from app.agents.seller.context import SellerContext
from app.agents.seller.history import apply_recommendation
from app.agents.seller.hitl import (
    DraftRecord,
    confirm_draft,
    invalidate_draft,
    start_draft,
    validate_draft,
)
from app.agents.seller.middleware import StreamingOutputGuard, check_scope, mask_output
from app.agents.seller.models import SellerRole, init_seller_model, seller_trace_model_metadata
from app.agents.seller.preview import build_create_preview, diff_notes, parse_int_or_none
from app.agents.seller.vision import ProductImageAnalysis, analyze_product_images
from app.agents.seller.orchestrator import (
    PipelineResult,
    route_question,
    run_analysis_pipeline,
    run_resolved_pipeline,
)
from app.agents.seller.period import disclosure_text, parse_period_approval, resolve_from_message
from app.agents.seller.pipeline import (
    APPLY_GUIDE,
    format_general_input,
    parse_apply_message,
    split_report_summary,
)
from app.agents.seller.prompts import PENDING_DRAFT_GATE_PROMPT
from app.agents.seller.schemas import DraftChange, DraftProposal, PendingDraftAction
from app.agents.seller.workers import build_general_agent, build_product_agent
from app.api.deps import require_seller
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.conversation import TurnStatus, get_conversation_store
from app.core.errors import get_request_id, new_request_id
from app.core.llm import LLMNotConfigured, is_timeout_error
from app.core.observability import (
    emit_rejection,
    finish_trace_safely,
    identifier_fingerprint,
    start_observation,
)
from app.core.pg_resilience import is_state_store_unavailable
from app.core.session_context import SessionStateUnavailable
from app.core.stream import open_stream, registry_key
from app.core.tracing import current_request_trace, start_request_trace_safely, trace_span
from app.core.text import _strip_unsafe, _strip_unsafe_multiline
from app.schemas.chat import ErrorData, TokenData
from app.schemas.seller import SellerChatRequest
from app.services.spring_client import SpringUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["seller"])

# confirm 실행 중 Spring 장애 안내 — draft 는 interrupt 에 남아 재confirm 가능(4-2).
_CONFIRM_SPRING_DOWN_TOKEN = (
    "죄송합니다. 상품 서버와 통신이 원활하지 않아 반영하지 못했습니다. "
    "초안은 유지되니 잠시 후 같은 승인 요청을 다시 보내주세요."
)

# 분석 파이프라인 예외 2경우(planner 장애·1차 report 실패)의 사과 문구(§7).
_ANALYSIS_APOLOGY_TOKEN = (
    "죄송합니다. 분석 처리 중 문제가 발생해 답변을 완성하지 못했습니다. 잠시 후 다시 시도해 주세요."
)

# 진행 token 큐 종료 신호 — 파이프라인 완료(정상/예외 공통)를 스트림 루프에 알린다.
_PIPELINE_DONE = object()

# report.generatedAt 타임존 — KST 고정(이슈 #296, api-spec §3.2 v0.24.0 계약).
_KST = timezone(timedelta(hours=9))


def _seller_log(
    level: int,
    event: str,
    *,
    context: SellerContext | None = None,
    identity: Identity | None = None,
    thread_id: str | None = None,
    action: str | None = None,
    error_code: str | None = None,
    status: str | None = None,
) -> None:
    """판매자 로그는 고정 상태와 peppered 식별자 지문만 허용한다."""
    seller_id = context.seller_id if context is not None else getattr(identity, "seller_id", None)
    brand_id = context.brand_id if context is not None else getattr(identity, "brand_id", None)
    record = {
        "event": event,
        "sellerFp": identifier_fingerprint(str(seller_id)) if seller_id is not None else None,
        "brandFp": identifier_fingerprint(str(brand_id)) if brand_id is not None else None,
        "threadFp": identifier_fingerprint(thread_id),
        "action": action,
        "errorCode": error_code,
        "status": status,
    }
    logger.log(level, json.dumps(record, ensure_ascii=False))


def _sse(event_type: str, data: dict) -> str:
    payload = {"type": event_type, "data": data}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _visible_token(text: str) -> str:
    """이미 신뢰경계 정제를 마친 텍스트를 token SSE로 감싼다."""
    return _sse("token", TokenData(text=text).model_dump(by_alias=True))


def _token(text: str) -> str:
    visible = mask_output(_strip_unsafe_multiline(text))
    return _visible_token(visible)


def _resolve_request_id(request_id: str | None) -> str:
    """직접 호출되는 하위 스트림에도 비어 있지 않은 상관관계 ID를 보장한다."""
    return request_id or new_request_id()


def _set_trace_lane(lane: Lane) -> None:
    if trace := current_request_trace():
        trace.set_lane(lane)


def _mark_seller_degraded(reason: str) -> None:
    if trace := current_request_trace():
        trace.mark_degraded(reason)


def _llm_metadata(role: SellerRole) -> dict[str, str] | None:
    return seller_trace_model_metadata(role)


def _error(
    code: str,
    message: str,
    *,
    request_id: str,
    retryable: bool,
) -> str:
    """판매자 스트림 오류를 공통 SSE 계약으로 직렬화한다."""
    return _sse(
        "error",
        ErrorData(
            code=code,
            message=message,
            request_id=request_id,
            retryable=retryable,
        ).model_dump(by_alias=True),
    )


def _llm_unavailable(
    *,
    lane: str,
    thread_id: str,
    request_id: str,
    context: SellerContext,
) -> str:
    """활성 provider 미구성을 비밀값 없는 오류 로그와 계약 이벤트로 변환한다."""
    _seller_log(
        logging.ERROR,
        "seller_llm_unavailable",
        context=context,
        thread_id=thread_id,
        action=lane,
        error_code="LLM_UNAVAILABLE",
        status="FAILED",
    )
    return _error(
        "LLM_UNAVAILABLE",
        "현재 AI 모델을 사용할 수 없습니다.",
        request_id=request_id,
        retryable=False,
    )


# ── 화면 전환 신호 (FE 계약 B, 2026-07-22 — 판매자 스트림 전용, 구매자 무관) ──────────
#
# 판매자 대시보드는 좌(채팅)/우(패널) 분할이다. 서버가 질문을 분기(analysis/product/
# general/confirm/apply/refused)해도 그 결과를 FE 가 알 수 없어 "우측 패널을 바꿀지"를
# 판단하지 못했다(FE 요구 1~3). 아래 두 신호로 해소한다:
#   · meta(lane)   : 매 스트림 첫 프레임. FE 가 레인을 즉시 알아 로딩 상태를 준비한다.
#   · done(panel)  : 종료 시 패널 조치를 확정한다 — replace(패널 교체)/keep(유지)/refresh(재조회).
# analysis 진행 상태는 최종 답변이 아니므로 token 이 아니라 progress 로 분리한다.

# 레인(meta.lane) — supervisor 3분기 + 코드 선판정 3종.
Lane = Literal["analysis", "product", "general", "confirm", "apply", "refused"]
# 패널 조치(done.panel) — 우측 패널을 어떻게 할지 FE 에 지시.
Panel = Literal["replace", "keep", "refresh"]


def _meta(lane: Lane) -> str:
    """스트림 첫 프레임 — FE 가 우측 패널 처리 레인을 즉시 알도록 한다(요구 1~3)."""
    return _sse("meta", {"lane": lane})


def _progress(text: str) -> str:
    """분석 진행 상태 — 최종 답변이 아니라 로딩 표시용(FE: 임시 텍스트, 답변에서 제외)."""
    return _sse("progress", {"text": text})


def _done(panel: Panel = "keep") -> str:
    """종료 프레임 — finishReason 은 판매자 스트림에서 stop 단일. panel 은 우측 패널 조치.

    구매자 DoneData(app/schemas/chat.py)는 건드리지 않는다 — 판매자 전용 필드라
    여기서 직접 페이로드를 구성한다(camelCase 규약 유지).
    """
    return _sse("done", {"finishReason": "stop", "panel": panel})


def _chunk_text(content: object) -> str:
    """AIMessageChunk.content → 텍스트 증분.

    Anthropic 은 str 또는 블록 리스트를 준다 — text 블록만 취하고 tool_use
    블록(도구 호출 인자)은 사용자 스트림에 흘리지 않는다.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


class _CheckpointerUnavailable(Exception):
    """체크포인터(pg-profile) 연결 실패 표식 — LLM 지연과 구분하기 위한 내부 태그 (#266 PR 리뷰).

    `get_checkpointer()` 는 자체적으로 `asyncio.wait_for(..., seller_checkpoint_connect_timeout_s)`
    를 걸고 운영(`auth_mode=jwks`)에서는 폴백 없이 raise 한다(`seller/checkpoint.py`). 그때 나오는
    것은 `asyncio.TimeoutError`(= 내장 `TimeoutError`)라 `is_timeout_error` 가 **타입만으로 참**
    으로 판정한다 — 그대로 두면 pg-profile 장애가 "응답 생성이 지연되어 중단됐습니다"(LLM_TIMEOUT,
    WARNING)로 나가 인프라 장애가 느린 LLM 응답으로 감춰진다. 타입이 같으므로 **발생 지점**으로만
    구분할 수 있어 호출부에서 이 예외로 감싸 올린다.
    """


def _seller_context(identity: Identity) -> SellerContext:
    """검증된 Identity → SellerContext 숫자 캐스팅 (판매자·브랜드 id는 숫자 계약, §2.6).

    JWT `sub`/`brandId` 클레임은 발급자에 따라 문자열("1")·숫자(1)로 올 수 있어
    int 로 정규화한다 — SellerContext·DraftRecord·spring_client 의 int 타입과
    일치시켜 Pydantic 직렬화 경고/검증 실패를 막는다. 빈 값은 require_seller 가
    이미 403 으로 걸렀다. 숫자가 아닌 클레임은 ValueError — 호출부(_seller_stream)
    가 error 이벤트로 봉투 종료한다.
    """
    return SellerContext(
        seller_id=int(identity.seller_id or 0), brand_id=int(identity.brand_id or 0)
    )


async def _general_stream(
    request: SellerChatRequest,
    context: SellerContext,
    *,
    request_id: str | None = None,
) -> AsyncIterator[str]:
    """general_agent astream → token/done (3-7 — SPEC §7 수명주기·degrade).

    - C1(REVIEW-SELLER-STAGE2): build_general_agent 는 **요청마다 재빌드** —
      빌드 시점 today 박제가 장기 실행 서버에서 stale 해지는 것을 방지한다.
    - scope 선차단: 미들웨어(end 점프)가 주입하는 거절 메시지는 astream
      messages 모드에서 모델 청크로 흐르지 않으므로, 코드에서 같은 판정점
      (check_scope)으로 거절 문안을 직접 token emit 한다.
    - [#346] 기간 선해결: 환산은 period.py 소관이고 프롬프트는 주어진 from/to 를 쓰기만
      한다. 해석 불가 기간은 LLM·도구 호출 **앞에서** 되묻기로 끝난다 — 분석 레인의
      resolve_plan 실패 → clarification 과 같은 자리다.
    - 출력 검사(§10-⑥): 요청 단위 StreamingOutputGuard가 Unicode 문맥과
      청크 경계 시크릿 prefix를 보류해 확정된 안전 조각만 내보낸다.
    - 오류: 스트림 내부 실패는 error 이벤트(LLM_TIMEOUT/INTERNAL) 후 종료(§2.7).
    - 대화 스레드: 공용 checkpointer + thread_id(chat_config)로 멀티턴 누적 —
      체크포인트 로드→새 메시지 append→저장은 LangGraph 소관이라 invoke 가 곧
      기록이다(record_turn 불필요 — 이중 기록 금지). 재빌드(C1)여도 상태는
      checkpointer 에 있어 스레드는 이어진다.
    """
    request_id = _resolve_request_id(request_id)
    _set_trace_lane("general")
    # general 은 항상 대화(우측 패널 유지) — 첫 프레임에 레인을 알린다.
    yield _meta("general")
    refusal = check_scope(request.message)
    if refusal:
        yield _token(refusal)
        yield _done("keep")
        return

    # [#346] 기간 환산 — 프롬프트가 하던 일을 코드로 옮겼다. 여기서 끝내야 상한·0/음수
    # 가드가 general 레인에도 걸리고("최근 999999일"), 분석 레인과 같은 (from, to) 가
    # 나온다. 순수 계산이라 아래 레인 상한(seller_general_timeout_s) 밖이어도 무해하다.
    settings = get_settings()
    try:
        resolution = resolve_from_message(
            request.message,
            today=date.today(),
            recent_default_days=settings.seller_recent_days_default,
            max_days=settings.seller_period_max_days,
        )
    except ValueError as exc:
        # 되묻기 문구는 period.py 가 만든다(§4.2 문구 소유권) — 여기서 가공하지 않는다.
        yield _token(str(exc))
        yield _done("keep")
        return
    if resolution.any_confirmation_needed:
        # 분석 레인처럼 확인 대기를 걸지 않고 **고지**만 한다 — 근거는
        # period.disclosure_text docstring(오해석 비용 비대칭, DESIGN §7).
        yield _token(disclosure_text(resolution) + "\n\n")

    queue: asyncio.Queue[object] = asyncio.Queue()

    async def produce() -> None:
        """Run all token-backed trace contexts in one persistent task."""
        # 체크포인터 초기화는 general 상한 **밖**에서 먼저 끝낸다 (#266 PR 리뷰).
        # 안에 두면 pg-profile 장애의 TimeoutError 와 LLM 지연의 TimeoutError 가 같은
        # 타입으로 섞여 is_timeout_error 가 둘을 구분할 수 없다. 반대 방향 오분류
        # (LLM 예산 소진을 인프라 장애로 기록)도 함께 막힌다.
        #
        # 밖에 두는 대신 **초기화 전체가 유한**해야 한다 — 그러지 않으면 이 앞에서 늘어져
        # SSE 캡(stream_total_timeout_s)이 in-stream error 없이 done(stop) 으로 끊는,
        # 이 이슈가 없애려는 실패 모드가 콜드스타트에 재현된다. `_init_checkpointer` 가
        # 연결과 setup() 을 **각각** seller_checkpoint_connect_timeout_s 로 감싸 그 유한성을
        # 보장하고(3차 리뷰로 setup() 누락 수정), 예산식은 2배로 계산한다
        # (config `_require_general_lane_within_stream_cap`).
        #
        # [남는 한계] 스트림 **도중**의 체크포인트 read/write 는 여전히 아래 상한 안에서 돈다.
        # pg 가 오류를 던지면(statement_timeout → QueryCanceled, PoolTimeout — 둘 다
        # psycopg OperationalError 라 TimeoutError 가 아니다) is_timeout_error 가 False 를
        # 내 INTERNAL 로 정확히 분류된다. 다만 pg 가 "오류 없이 느리기만" 하면 그 시간은
        # LLM 예산을 잠식하고 레인 타이머가 LLM_TIMEOUT 으로 보고한다 — 그 시점의 정보로는
        # 원인을 가릴 수 없다(각 문장은 statement_timeout 3s 로 묶여 있다).
        try:
            checkpointer = await get_checkpointer()
        except Exception as exc:
            raise _CheckpointerUnavailable from exc
        # (#266 P1) 레인 전체 벽시계 상한. 다른 레인이 쓰는 asyncio.wait_for 는 여기서
        # 쓸 수 없다 — astream 은 중간에 yield 하는 async generator 다. SDK 의 timeout=
        # 도 스트리밍에서는 청크 간 read 간격만 재므로 상한이 되지 못한다.
        async with asyncio.timeout(get_settings().seller_general_timeout_s):
            with trace_span("seller.graph.general", "chain"):
                # 빌드도 producer 안 — 실패 시 기존 error 이벤트 봉투로 종료한다.
                agent = build_general_agent(
                    today=date.today().isoformat(), checkpointer=checkpointer
                )
                output_guard = StreamingOutputGuard()
                started = perf_counter()
                first_text = True
                with trace_span("llm.seller.general", "llm", _llm_metadata("worker")):
                    async for item in agent.astream(
                        {
                            "messages": [
                                HumanMessage(
                                    content=format_general_input(request.message, resolution)
                                )
                            ]
                        },
                        config=seller_thread.chat_config(context, request.thread_id),
                        context=context,
                        stream_mode="messages",
                    ):
                        message_chunk = item[0] if isinstance(item, tuple) else item
                        if not isinstance(message_chunk, AIMessageChunk):
                            continue
                        text = _chunk_text(message_chunk.content)
                        if text and first_text:
                            first_text = False
                            if trace := current_request_trace():
                                trace.record_provider_ttft(
                                    int(round((perf_counter() - started) * 1000))
                                )
                        for visible in output_guard.feed(text):
                            await queue.put(_visible_token(visible))
                for visible in output_guard.flush():
                    await queue.put(_visible_token(visible))

    producer_task = asyncio.create_task(produce())
    producer_task.add_done_callback(lambda _task: queue.put_nowait(_PIPELINE_DONE))
    try:
        while True:
            item = await queue.get()
            if item is _PIPELINE_DONE:
                break
            yield str(item)
        await producer_task
        yield _done("keep")
    except LLMNotConfigured:
        yield _llm_unavailable(
            lane="general",
            thread_id=request.thread_id,
            request_id=request_id,
            context=context,
        )
    except _CheckpointerUnavailable:
        # 인프라 장애를 LLM 지연으로 감추지 않는다 (#266 PR 리뷰). 이 분기는 아래
        # is_timeout_error 보다 **먼저** 와야 한다 — 체크포인터 타임아웃도 TimeoutError 라
        # 순서가 바뀌면 그대로 LLM_TIMEOUT 으로 흡수된다.
        # 코드가 INTERNAL 인 이유: 이미 meta 를 발신해 503 봉투로 돌아갈 수 없고, 판매자
        # in-stream 오류 코드에 STATE_UNAVAILABLE 이 없다(§3.2). 스트림 전 store 장애를
        # 503 STATE_UNAVAILABLE 로 올리는 경로(seller_chat)와는 단계가 다르다.
        _seller_log(
            logging.ERROR,
            "seller_checkpointer_unavailable",
            context=context,
            thread_id=request.thread_id,
            action="general",
            error_code="INTERNAL",
            status="FAILED",
        )
        yield _error(
            "INTERNAL",
            "일시적인 오류가 발생했습니다.",
            request_id=request_id,
            retryable=True,
        )
    except Exception as exc:
        # (#266 P1) 타임아웃 판정은 **타입**으로 한다. asyncio.timeout 은 TimeoutError 를
        # 던지지만, SDK 타임아웃(httpx.TimeoutException·provider APITimeoutError)은
        # TimeoutError 의 서브클래스가 아니라 예전에는 아래 INTERNAL 로 떨어졌다.
        # is_timeout_error 가 원인 체인까지 따라가므로 감싸인 예외도 잡힌다.
        if is_timeout_error(exc):
            _seller_log(
                logging.WARNING,
                "seller_stream_timeout",
                context=context,
                thread_id=request.thread_id,
                action="general",
                error_code="LLM_TIMEOUT",
                status="FAILED",
            )
            yield _error(
                "LLM_TIMEOUT",
                "응답 생성이 지연되어 중단됐습니다.",
                request_id=request_id,
                retryable=True,
            )
            return
        _seller_log(
            logging.ERROR,
            "seller_stream_failed",
            context=context,
            thread_id=request.thread_id,
            action="general",
            error_code="INTERNAL",
            status="FAILED",
        )
        yield _error(
            "INTERNAL",
            "일시적인 오류가 발생했습니다.",
            request_id=request_id,
            retryable=True,
        )
    finally:
        if not producer_task.done():
            producer_task.cancel()
        await asyncio.gather(producer_task, return_exceptions=True)


async def _analysis_stream(
    request: SellerChatRequest,
    context: SellerContext,
    recent_turns: list[seller_thread.Turn],
    *,
    request_id: str | None = None,
    pending: seller_period_confirm.PendingPeriod | None = None,
) -> AsyncIterator[str]:
    """분석 레인 (4-1b) — 파이프라인 emit(진행)을 progress 로, 최종 답변을 token 으로 중계.

    - 진행 상태는 최종 답변이 아니므로 `progress` 이벤트로 분리한다(FE: 로딩 표시).
      최종 산출은 단일 `token`(보고서/되묻기/사과 공통) + kind=="report" 일 때만
      구조화 `report` 이벤트 1회(우측 패널 재료, 이슈 #296) → 패널 교체 여부는
      kind 로 갈린다(아래 panel).
    - 패널: kind=="report" 만 우측 교체(replace) — 되묻기(clarification)·사과(apology)·
      거절(refused)·기간 확인(period_confirmation)은 대화이므로 유지(keep).
      (FE 요구 2·3 — "화면 바뀔 질문만" 교체.)
    - **예외 2경우**(planner 장애·1차 report 실패)만 여기로 전파 — 사과 token 후
      error 로 종료(REVIEW-STAGE3 §5-2). error 종료는 패널 유지(done 없음).
    - 진행 문구는 파이프라인 내부 상수라 마스킹 불필요, 최종 text 는 mask_output 적용.

    [#345] pending 이 주어지면 **기간 확인 승인 재개**다 — planner 를 건너뛰고 저장된
    ResolvedPlan 으로 곧바로 실행한다(DESIGN-SELLER-PERIOD §6). 와이어 이벤트 순서와
    패널 규칙은 신규 질문과 완전히 같다 — FE 는 이 턴이 재개인지 알 필요가 없다.
    """
    request_id = _resolve_request_id(request_id)
    _set_trace_lane("analysis")
    yield _meta("analysis")
    queue: asyncio.Queue[object] = asyncio.Queue()

    async def emit(text: str) -> None:
        await queue.put(text)

    async def run_pipeline():
        """Keep the analysis trace token and all descendants in one task context."""
        with trace_span("seller.graph.analysis", "chain"):
            if pending is not None:
                # 원 질문(pending.question)으로 실행한다 — 승인 발화("응")는 질문이 아니라
                # 워커 입력·이력에 그대로 쓰면 맥락이 사라진다.
                return await run_resolved_pipeline(
                    pending.question, pending.plan, context, emit=emit
                )
            return await run_analysis_pipeline(
                request.message,
                context,
                today=date.today(),
                emit=emit,
                recent_turns=recent_turns,
                screen=request.screen,
            )

    pipeline_task = asyncio.create_task(run_pipeline())
    # 정상/예외 공통으로 sentinel 을 넣어 진행 루프를 반드시 끝낸다.
    pipeline_task.add_done_callback(lambda _task: queue.put_nowait(_PIPELINE_DONE))
    try:
        while True:
            item = await queue.get()
            if item is _PIPELINE_DONE:
                break
            yield _progress(str(item))

        try:
            result = await pipeline_task
        except LLMNotConfigured:
            yield _llm_unavailable(
                lane="analysis",
                thread_id=request.thread_id,
                request_id=request_id,
                context=context,
            )
            return
        except (TimeoutError, asyncio.TimeoutError):
            yield _token(_ANALYSIS_APOLOGY_TOKEN)
            yield _error(
                "LLM_TIMEOUT",
                "분석 응답이 지연되어 중단됐습니다.",
                request_id=request_id,
                retryable=True,
            )
            return
        except Exception:
            _seller_log(
                logging.ERROR,
                "seller_stream_failed",
                context=context,
                thread_id=request.thread_id,
                action="analysis",
                error_code="INTERNAL",
                status="FAILED",
            )
            yield _token(_ANALYSIS_APOLOGY_TOKEN)
            yield _error(
                "INTERNAL",
                "일시적인 오류가 발생했습니다.",
                request_id=request_id,
                retryable=True,
            )
            return
        # [#345] 기간 확인 대기 저장 — token 을 내보내기 **전에** 한다. 저장에 실패하면
        # 후속 "응" 이 신규 질문으로 처리되는데(degrade, DESIGN §5.4), 그 사실을 모른 채
        # 확인 문구만 나가는 것보다 순서를 맞춰 두는 편이 추적하기 쉽다.
        if result.kind == "period_confirmation" and result.resolved is not None:
            await seller_period_confirm.save_pending(
                context,
                request.thread_id,
                question=request.message,
                plan=result.resolved,
            )
        # 대화 스레드 기록(best-effort) — 되묻기 포함 최종 문안이 후속 발화의 맥락이 된다.
        # 승인 재개 턴은 사용자 발화가 "응" 이라 그대로 기록한다 — 무엇에 답했는지는
        # 직전 턴(확인 문구)이 스레드에 남아 있어 맥락이 이어진다.
        await seller_thread.record_turn(context, request.thread_id, request.message, result.text)
        yield _token(result.text)
        # report 는 kind=="report" 일 때 정확히 1회 — token(산문) 뒤·done 앞
        # (이슈 #296, api-spec §3.2 v0.24.0). 보고서±차트 분기는 charts 배열
        # 유무로만 표현한다(구 chart 이벤트의 조건 3중 검사·미발행 규약 폐기).
        if result.kind == "report":
            yield _report_event(result)
        # 보고서만 우측 패널 교체, 되묻기·사과·거절은 대화로 유지.
        yield _done("replace" if result.kind == "report" else "keep")
    finally:
        if not pipeline_task.done():
            pipeline_task.cancel()
        await asyncio.gather(pipeline_task, return_exceptions=True)


def _deserialize_analysis(data: dict | None) -> ProductImageAnalysis | None:
    """draft_session 에 보관된 vision 분석 직렬화형 복원 — 손상은 None degrade."""
    if not data:
        return None
    try:
        return ProductImageAnalysis.model_validate(data)
    except Exception:
        return None


def _category_candidates(
    analysis: ProductImageAnalysis | None,
    message: str,
    pending: draft_session.PendingCreate | None = None,
) -> list[category_catalog.CategoryEntry]:
    """[#506] 카테고리 후보 — 기존 초안 확정값 > vision 힌트 > 발화 매칭, k 상한(중복 제거).

    발화 검색을 함께 도는 이유: 수정 턴("남방 말고 셔츠야")은 새 카테고리 어휘가
    발화에만 있다. LLM 이 임의 카테고리를 만들 경로는 없다 — 후보 밖 값은
    validate_draft 가 되묻기로 전환한다(이중 방어).

    [리뷰 M-3] 수정 턴에는 기존 초안의 카테고리 id 를 **항상 후보에 포함**한다 —
    안 그러면 "가격만 바꿔줘" 턴에서 확정해 둔 카테고리가 후보 목록에 없어
    프롬프트의 "기존 값 유지"와 "목록 밖 값 금지"가 충돌하고, 카테고리가 vision
    힌트로 되돌아가거나 조용히 탈락한다(등록 후 변경 불가 필드라 비용이 크다).
    """
    k = get_settings().seller_category_candidates_k
    merged: dict[str, category_catalog.CategoryEntry] = {}
    if pending is not None and (current_id := pending.changes.get("category")):
        if (current := category_catalog.get(current_id)) is not None:
            merged.setdefault(current.id, current)
    if analysis is not None and analysis.category_hint.strip():
        for entry in category_catalog.search(analysis.category_hint, k):
            merged.setdefault(entry.id, entry)
    for entry in category_catalog.search(message, k):
        merged.setdefault(entry.id, entry)
    return list(merged.values())[:k]


def _product_agent_input(
    request: SellerChatRequest,
    *,
    analysis: ProductImageAnalysis | None,
    candidates: list[category_catalog.CategoryEntry],
    pending: draft_session.PendingCreate | None,
    image_urls: list[str],
) -> str:
    """[#506] product 에이전트 입력 조립 — 발화 + 이미지 분석·카테고리 후보·기존 초안 주입.

    이미지 원본이 아니라 **분석 결과(텍스트)** 를 주입한다 — 분석은 첨부 턴 1회이고
    (vision.py), 에이전트는 텍스트 루프를 유지해 매 턴 이미지 토큰이 들지 않는다.
    """
    blocks: list[str] = []
    if image_urls:
        blocks.append("[이미지 URL]\n" + "\n".join(image_urls))
    if analysis is not None:
        blocks.append(
            "[이미지 분석]\n"
            f"- 상품명 제안: {analysis.name}\n"
            f"- 한 줄 요약: {analysis.summary}\n"
            f"- 상세 설명: {analysis.description}\n"
            f"- 상품군: {analysis.category_hint}"
        )
    if candidates:
        blocks.append(
            "[카테고리 후보] (id | 경로)\n" + category_catalog.candidates_block(candidates)
        )
    if pending is not None and pending.changes:
        existing = "\n".join(f"- {field}: {after}" for field, after in pending.changes.items())
        blocks.append("[기존 초안] (수정 턴 — 요청된 항목만 바꾼다)\n" + existing)
    blocks.append("[판매자 요청]\n" + request.message)
    return "\n\n".join(blocks)


async def _product_stream(
    request: SellerChatRequest,
    context: SellerContext,
    *,
    request_id: str | None = None,
    pending: draft_session.PendingCreate | None = None,
) -> AsyncIterator[str]:
    """product 레인 (4-2 — draft 생성 + checkpoint 저장, 실행은 confirm 스트림).

    product_agent(2-7)로 DraftProposal 을 만들고 validate_draft(코드 선검증 —
    캐스팅·필수 필드·C4)를 통과하면 start_draft 로 checkpoint 에 저장(interrupt
    대기)한 뒤 SSE `draft` 이벤트로 내보낸다. clarification·검증 불성립은
    되묻기 token. 실행은 스트림 2(_confirm_stream — hitl.confirm_draft) 소관.
    check_scope 는 입구 ②에서 이미 수행됨(구조화 레인 코드 경로 — 배정표 준수).
    패널: draft 성립 시 우측에 diff 카드(replace), 되묻기·검증 불성립은 대화(keep).
    최종 문안(되묻기·초안 요약)은 대화 스레드에 기록(best-effort) — 후속 발화 맥락.

    [#506] 이미지 기반 등록: imageUrls 첨부 턴은 vision 1회 분석 → 분석·카테고리 후보
    주입. pending(수정 턴)은 기존 초안 값을 주입하고 **재분석하지 않는다** — 새 사진을
    첨부한 턴만 예외(재분석 + 대표사진 교체). create draft 성립 시 preview 를 함께
    싣고(draft 이벤트), draft_session 에 대기를 저장하며 이전 draftId 는 무효화한다.
    """
    request_id = _resolve_request_id(request_id)
    _set_trace_lane("product")
    yield _meta("product")
    settings = get_settings()

    # ── [#506] vision 분석 (이미지 첨부 턴 1회) / 수정 턴 캐시 복원 ──────────────
    new_images = list(request.image_urls or [])
    analysis: ProductImageAnalysis | None = None
    image_urls: list[str] = new_images
    try:
        if new_images:
            with trace_span("seller.graph.vision", "chain"):
                analysis = await analyze_product_images(new_images, seller_message=request.message)
        elif pending is not None:
            analysis = _deserialize_analysis(pending.analysis)
            image_urls = list(pending.image_urls)
    except LLMNotConfigured:
        yield _llm_unavailable(
            lane="product",
            thread_id=request.thread_id,
            request_id=request_id,
            context=context,
        )
        return

    candidates = _category_candidates(analysis, request.message, pending)
    agent_input = _product_agent_input(
        request,
        analysis=analysis,
        candidates=candidates,
        pending=pending,
        image_urls=image_urls,
    )

    try:
        with trace_span("seller.graph.product", "chain"):
            agent = build_product_agent()
            with trace_span("llm.seller.product", "llm", _llm_metadata("product")):
                result = await asyncio.wait_for(
                    agent.ainvoke(
                        {"messages": [HumanMessage(content=agent_input)]},
                        context=context,
                    ),
                    timeout=settings.seller_worker_timeout_s,
                )
        proposal = result.get("structured_response")
        if not isinstance(proposal, DraftProposal):
            raise TypeError("product_agent 가 DraftProposal 을 반환하지 않았다")
    except LLMNotConfigured:
        yield _llm_unavailable(
            lane="product",
            thread_id=request.thread_id,
            request_id=request_id,
            context=context,
        )
        return
    except (TimeoutError, asyncio.TimeoutError):
        yield _error(
            "LLM_TIMEOUT",
            "초안 생성이 지연되어 중단됐습니다.",
            request_id=request_id,
            retryable=True,
        )
        return
    except Exception:
        _seller_log(
            logging.ERROR,
            "seller_stream_failed",
            context=context,
            thread_id=request.thread_id,
            action="product",
            error_code="INTERNAL",
            status="FAILED",
        )
        yield _error(
            "INTERNAL",
            "일시적인 오류가 발생했습니다.",
            request_id=request_id,
            retryable=True,
        )
        return

    if proposal.clarification:
        await seller_thread.record_turn(
            context, request.thread_id, request.message, proposal.clarification
        )
        yield _token(proposal.clarification)
        yield _done("keep")
        return

    # 코드 선검증(4-2) — 실행 불가능한 draft 는 FE 에 보여주기 전에 되묻는다.
    record, problem = validate_draft(
        proposal, seller_id=context.seller_id, brand_id=context.brand_id
    )
    if record is None:
        text = problem or "초안을 만들지 못했습니다. 다시 요청해 주세요."
        await seller_thread.record_turn(context, request.thread_id, request.message, text)
        yield _token(text)
        yield _done("keep")
        return

    # [#506] 대표사진 URL 은 **코드가 강제**한다 — LLM 이 [이미지 URL] 블록을 옮겨적다
    # 변형하면 "보여준 것 ≠ 실행하는 것"이 된다(계약값은 코드). 새 첨부가 있으면 그 값,
    # 없으면 pending 의 기존 값이 정본이다. 반드시 start_draft(checkpoint 저장) **전**에
    # 정규화한다 — 저장 뒤에 바꾸면 실행 정본과 표시가 갈라진다.
    if record.op == "create":
        expected_image = image_urls[0] if image_urls else None
        if expected_image is not None:
            replaced = [
                c.model_copy(update={"after": expected_image}) if c.field == "image_url" else c
                for c in record.changes
            ]
            if not any(c.field == "image_url" for c in replaced):
                replaced.append(DraftChange(field="image_url", before="", after=expected_image))
            record = record.model_copy(update={"changes": replaced})

    try:
        await start_draft(record)  # checkpoint 저장 + interrupt 대기(안전장치 ①)
    except Exception:
        _seller_log(
            logging.ERROR,
            "seller_checkpoint_failed",
            context=context,
            thread_id=request.thread_id,
            action="product",
            error_code="INTERNAL",
            status="FAILED",
        )
        yield _error(
            "INTERNAL",
            "일시적인 오류가 발생했습니다.",
            request_id=request_id,
            retryable=True,
        )
        return

    # ── [#506] create 초안: 이전 draft 무효화 + 세션 저장 + preview 구성 ─────────
    preview: dict | None = None
    if record.op == "create":
        notes: list[str] = []
        if pending is not None:
            # 수정 턴 — 이전 draftId 무효화(FE 계약 §5.6: 옛 카드 confirm 사고 차단).
            await invalidate_draft(pending.draft_id)
            notes = diff_notes(pending.changes, record)
        seller_inputs = _seller_input_summary(record)
        preview = build_create_preview(
            record,
            analysis=analysis,
            seller_inputs=seller_inputs,
            modified_notes=notes or None,
        )
        await draft_session.save_pending(
            context,
            request.thread_id,
            draft_session.PendingCreate(
                draft_id=record.draft_id,
                image_urls=tuple(image_urls),
                analysis=analysis.model_dump() if analysis is not None else None,
                changes={c.field: c.after for c in record.changes},
            ),
        )

    await seller_thread.record_turn(
        context, request.thread_id, request.message, _draft_recorded_text(record)
    )
    yield _draft_event(record, preview=preview)
    yield _done("replace")  # diff 카드 = 우측 패널 교체


def _seller_input_summary(record: DraftRecord) -> str | None:
    """preview sections `source` 의 "판매자 입력" 항목 — 발화에서만 오는 값(가격·재고).

    파싱은 실행 계층과 동일 관용(preview.parse_int_or_none → hitl._parse_int) —
    "29,900원" 같은 접미사 값도 실행과 같은 숫자로 표기된다(H-1, 리뷰 반영).
    """
    values = {c.field: c.after for c in record.changes}
    parts: list[str] = []
    price = parse_int_or_none(values.get("price"))
    if price is not None:
        parts.append(f"{price:,}원")
    stock = parse_int_or_none(values.get("stock_quantity"))
    if stock is not None:
        parts.append(f"{stock:,}개")
    return " / ".join(parts) if parts else None


def _draft_recorded_text(record: DraftRecord) -> str:
    """draft 성립 턴의 스레드 기록 문안 — diff 전문이 아니라 후속 발화 이해용 요약."""
    if record.op == "ship":
        base = "주문 발송 초안"
    elif record.op == "create":
        base = "상품 등록 초안"  # [#506] FE 계약 문서와 같은 어휘 — 수정 초안과 구분.
    else:
        base = "상품 변경 초안"
    if record.summary:
        return f"{base}을 생성했습니다: {record.summary}"
    return f"{base}을 생성했습니다 (op={record.op})."


def _masked_preview(preview: dict) -> dict:
    """[리뷰 M-4] preview 표시 사본에도 changes[] 와 같은 시크릿 마스킹을 적용한다.

    changes 는 마스킹되는데 FE 가 실제로 그리는 preview 만 원문이면 표시 계층 마스킹
    정책이 create 카드에서 무력화된다. imageUrl 만 면제(정규식이 S3 경로 세그먼트를
    오탐할 수 있고 값은 hitl 이 URL 검증 완료 — _wire_value 의 image_url 면제와 동일).
    위험 문자 제거(_strip_unsafe)는 validate_draft 가 이미 수행했으므로 마스킹만 더한다.
    """
    masked = dict(preview)
    for key in (
        "title",
        "priceText",
        "originalPriceText",
        "stockText",
        "categoryPath",
        "summary",
        "description",
    ):
        if isinstance(masked.get(key), str):
            masked[key] = mask_output(masked[key])
    masked["sections"] = [
        {**section, "items": [mask_output(item) for item in section.get("items", [])]}
        for section in preview.get("sections", [])
    ]
    return masked


def _draft_event(record: DraftRecord, *, preview: dict | None = None) -> str:
    """DraftRecord → SSE draft 이벤트 (product 레인·추천 적용 레인 공용, api-spec §3.2).

    [C-1 수정 2026-07-22] 와이어의 `changes[].field` 는 **camelCase**(규약 §2.2, api-spec).
    내부 DraftChange.field 는 Spring 쓰기(I-10/11)용 snake_case 로 남기고, 여기서
    나갈 때만 to_camel 로 변환한다 — original_price→originalPrice, image_url→imageUrl,
    stock_quantity→stockQuantity(그 외는 동일). 이 필드는 FE 표시 전용이라 confirm 은
    draftId 만 되보낸다(역변환 불필요).

    [#297] op="ship"(주문 발송, I-30)은 orderItemId 를 함께 싣는다(§3.2 추가 전용) —
    상품 op 3종의 와이어는 불변이다(orderItemId 키는 ship 에만 존재).

    [#506] op="create" 는 preview{} 를 함께 싣는다(§3.2 추가 전용, v0.30.0) — FE 등록
    미리보기 카드는 preview 만 보고 그린다(changes 는 실행 정본). image_url 값은
    mask_output 을 태우지 않는다 — 시크릿 패턴 정규식(sk-…)이 S3 경로 세그먼트를
    오탐 마스킹할 수 있고, 값 자체는 validate_draft 가 http(s)·길이를 이미 보장한다.
    """

    def _wire_value(field: str, raw: str) -> str:
        if field == "description":
            return mask_output(_strip_unsafe_multiline(raw))
        if field == "image_url":
            return _strip_unsafe(raw)  # [#506] URL 마스킹 오탐 방지 — 검증은 hitl 소관
        return mask_output(_strip_unsafe(raw))

    return _sse(
        "draft",
        {
            "draftId": record.draft_id,
            "op": record.op,
            "productId": record.product_id,  # int | None(create) — F2 숫자 확정
            # ship 전용 키 — 추가 전용(기존 op 와이어 불변).
            **({"orderItemId": record.order_item_id} if record.op == "ship" else {}),
            "changes": [
                {
                    "field": to_camel(c.field),
                    "before": _wire_value(c.field, c.before),
                    "after": _wire_value(c.field, c.after),
                }
                for c in record.changes
            ],
            "summary": mask_output(_strip_unsafe(record.summary)),
            # create 전용 키 — 추가 전용(기존 op 와이어 불변). 표시 사본은 코드 산물.
            **({"preview": _masked_preview(preview)} if preview is not None else {}),
        },
    )


def _report_event(result: PipelineResult) -> str:
    """PipelineResult → SSE report 이벤트 (이슈 #296, api-spec §3.2 v0.24.0 — 구 chart 대체).

    `_draft_event` 와 같은 패턴(camelCase 변환 + 필드별 마스킹). 호출부(_analysis_stream)
    가 kind=="report" 일 때만 정확히 1회 호출한다 — 되묻기·사과·거절·error 종료에는
    나가지 않는다. compose_response 가 token 산문으로 눌러 펴며 버리던 구조
    (findings·기간·추천·차트)를 우측 패널 재료로 그대로 싣는다.

    - charts 직렬화는 구 `_chart_event`(v0.20.0) 본문을 그대로 이관 — 형식 불변이라
      FE `AnalysisChart.tsx` 를 재사용한다. 빈 charts 는 [] 로 나간다(구 "빈 배열도
      보내지 않는다" 미발행 규약은 chart 이벤트와 함께 폐기).
    - limitations 는 degrade finding(evidence 빈 목록)의 summary 모음 — D3 탐지
      문자열("확보 실패") 매칭이 아니라 구조 판정이다(verifier 의 degrade 규약과 동일).
    - recommendations[].index 는 목록 순서(1-base) 명시 — "N번 적용해줘"(§6.3)가
      조회하는 그 순서다(FE 정렬 사고 방지).
    """
    report_text = result.verified.report if result.verified else ""
    findings = result.findings or []
    recommendations = result.recommendations.recommendations if result.recommendations else []
    charts = result.charts.charts if result.charts else []
    return _sse(
        "report",
        {
            "title": "판매 분석 보고서",
            "period": (
                {"from": result.period[0].isoformat(), "to": result.period[1].isoformat()}
                if result.period
                else None
            ),
            "generatedAt": datetime.now(_KST).isoformat(timespec="seconds"),
            "summary": mask_output(_strip_unsafe_multiline(split_report_summary(report_text))),
            "body": mask_output(_strip_unsafe_multiline(report_text)),
            "findings": [
                {
                    "analysisType": f.analysis_type,
                    "severity": f.severity,
                    "summary": mask_output(_strip_unsafe_multiline(f.summary)),
                    "evidence": [mask_output(_strip_unsafe_multiline(e)) for e in f.evidence],
                    "recommendation": mask_output(_strip_unsafe_multiline(f.recommendation)),
                }
                for f in findings
            ],
            "limitations": [
                mask_output(_strip_unsafe_multiline(f.summary)) for f in findings if not f.evidence
            ],
            "chartRequested": result.chart_requested,
            "charts": [
                {
                    "title": mask_output(_strip_unsafe(c.title)),
                    "chartType": c.chart_type,
                    "unit": c.unit,
                    "series": [
                        {
                            "label": mask_output(_strip_unsafe(s.label)),
                            "points": [
                                {"x": mask_output(_strip_unsafe(p.x)), "y": p.y} for p in s.points
                            ],
                        }
                        for s in c.series
                    ],
                    "summary": mask_output(_strip_unsafe_multiline(c.summary)),
                }
                for c in charts
            ],
            "recommendations": [
                {
                    "index": i,
                    "title": mask_output(_strip_unsafe(rec.title)),
                    "expectedEffect": mask_output(_strip_unsafe(rec.expected_effect)),
                    "actionType": rec.action_type,
                    "productId": rec.product_id,
                }
                for i, rec in enumerate(recommendations, start=1)
            ],
            "applyGuide": APPLY_GUIDE if recommendations else "",
        },
    )


async def _apply_stream(
    n: int,
    request: SellerChatRequest,
    context: SellerContext,
    *,
    request_id: str | None = None,
) -> AsyncIterator[str]:
    """추천 적용 레인 (4-3 §6.3 — 입구 ①.5 코드 선판정 후 진입, LLM 0회).

    최신 이력의 recommendations[N-1] 을 코드가 draft 로 변환(대화 재해석 금지) →
    4-2 와 동일하게 checkpoint 저장 후 draft emit — 이후 confirm 흐름 합류.
    불성립(이력 없음·인덱스 불일치·적용 불가 유형·상품 미발견)은 되묻기 token.
    패널: draft 성립 시 diff 카드(replace), 불성립은 대화(keep) — product 레인과 동일.
    """
    request_id = _resolve_request_id(request_id)
    _set_trace_lane("apply")
    yield _meta("apply")
    try:
        with trace_span("seller.graph.apply", "chain"):
            record, problem = await apply_recommendation(n, context)
    except SpringUnavailableError:
        yield _token(
            "죄송합니다. 상품 정보를 확인하지 못해 추천을 적용할 수 없었습니다. "
            "잠시 후 다시 시도해 주세요."
        )
        yield _error(
            "INTERNAL",
            "상품 서버 통신에 실패했습니다.",
            request_id=request_id,
            retryable=True,
        )
        return
    except Exception:
        _seller_log(
            logging.ERROR,
            "seller_apply_failed",
            context=context,
            thread_id=request.thread_id,
            action="apply",
            error_code="INTERNAL",
            status="FAILED",
        )
        yield _error(
            "INTERNAL",
            "일시적인 오류가 발생했습니다.",
            request_id=request_id,
            retryable=True,
        )
        return

    if record is None:
        text = problem or "추천을 적용하지 못했습니다. 다시 요청해 주세요."
        await seller_thread.record_turn(context, request.thread_id, request.message, text)
        yield _token(text)
        yield _done("keep")
        return

    try:
        await start_draft(record)  # 4-2 재사용 — draftId↔checkpoint 바인딩
    except Exception:
        _seller_log(
            logging.ERROR,
            "seller_checkpoint_failed",
            context=context,
            thread_id=request.thread_id,
            action="apply",
            error_code="INTERNAL",
            status="FAILED",
        )
        yield _error(
            "INTERNAL",
            "일시적인 오류가 발생했습니다.",
            request_id=request_id,
            retryable=True,
        )
        return

    await seller_thread.record_turn(
        context, request.thread_id, request.message, _draft_recorded_text(record)
    )
    yield _draft_event(record)
    yield _done("replace")  # diff 카드 = 우측 패널 교체


# ── [#506] 등록 초안 대기 게이트 헬퍼 (입구 ①.8) ─────────────────────────────────

# 취소 코드 단축경로 — 정형 발화만(오독 위험 최소 집합). 그 외 취소 의도는 게이트 LLM 이
# 판정한다. 승인에는 단축경로가 없다 — 위험 비대칭(FE 계약 §5.7, 발화 ≠ 동의 [HARD]).
_CANCEL_MESSAGES = frozenset(
    {"취소", "취소해줘", "취소할래", "취소요", "등록취소", "초안취소", "등록 취소", "초안 취소"}
)


def _is_cancel_message(message: str) -> bool:
    return " ".join(message.strip().rstrip(".!~").split()) in _CANCEL_MESSAGES


async def _classify_pending_utterance(message: str) -> str | None:
    """초안 대기 중 발화 4분류(modify/approve/cancel/offtopic) — 실패는 None(폴백).

    구조화 출력 단발 호출 — 오분류해도 비파괴적이다: approve/offtopic 은 안내만 하고
    초안을 유지하며, modify 오분류는 새 초안(이전 무효화)으로 이어질 뿐이다.
    """
    settings = get_settings()
    try:
        model = init_seller_model("draft_gate").with_structured_output(PendingDraftAction)
        with trace_span("llm.seller.draft_gate", "llm", _llm_metadata("draft_gate")):
            result = await asyncio.wait_for(
                model.ainvoke(
                    [
                        SystemMessage(content=PENDING_DRAFT_GATE_PROMPT),
                        HumanMessage(content=message),
                    ]
                ),
                timeout=settings.seller_pending_gate_timeout_s,
            )
    except LLMNotConfigured:
        # [리뷰 H-2] raise 하면 generator 밖으로 전파돼 open_stream 이 일반 INTERNAL 로
        # 오분류한다. None 폴백이면 일반 흐름의 route_question 이 같은 예외를 잡아
        # 계약 이벤트(LLM_UNAVAILABLE)로 정확히 응답한다 — 초안은 유지된다.
        logger.warning("pending draft gate LLM 미구성 — 일반 흐름 폴백(LLM_UNAVAILABLE 경로)")
        return None
    except Exception:
        logger.warning("pending draft gate 판정 실패 — 일반 흐름 폴백", exc_info=True)
        return None
    return result.action if isinstance(result, PendingDraftAction) else None


async def _cancel_pending_stream(
    request: SellerChatRequest,
    context: SellerContext,
    pending: draft_session.PendingCreate,
    *,
    request_id: str | None = None,
) -> AsyncIterator[str]:
    """채팅 '취소' 경로 — 초안 무효화 + 세션 폐기 + 카드 닫기(FE 계약 §5.7 취소 행).

    패널은 replace 다 — FE 가 카드를 닫고 초안 모드를 해제한다(keep 이면 죽은 카드가
    남는다). LLM 0회.
    """
    del request_id  # 오류 경로 없음 — 시그니처 일관성 유지용
    _set_trace_lane("product")
    yield _meta("product")
    await invalidate_draft(pending.draft_id)
    await draft_session.clear_pending(context, request.thread_id)
    text = "등록 초안을 취소했습니다. 새로 등록하시려면 사진을 다시 첨부해 주세요."
    await seller_thread.record_turn(context, request.thread_id, request.message, text)
    yield _token(text)
    yield _done("replace")


async def _confirm_stream(
    request: SellerChatRequest,
    context: SellerContext,
    *,
    request_id: str | None = None,
) -> AsyncIterator[str]:
    """confirm 레인 (4-2 스트림 2) — 코드 검사 후 resume 실행, LLM 0회.

    hitl.confirm_draft 가 존재→소유→멱등→TTL 검사를 통과한 경우에만 그래프를
    resume 해 I-10/11/12 를 실행한다. 모든 결과(executed/stale/만료/멱등/미존재)는
    token+done — Spring 장애만 사과 token + error(INTERNAL, draft 유지·재시도 가능).
    패널: 실제 쓰기가 일어난 executed 만 우측 재조회(refresh) — 그 외(변경 없음)는 유지(keep).
    결과 문안은 대화 스레드에 기록 — confirm 은 message 가 빈 계약(발화≠동의)이라
    사용자 턴은 플레이스홀더("(초안 승인)")로 남긴다.
    """
    request_id = _resolve_request_id(request_id)
    draft_id = request.draft_id or ""
    _set_trace_lane("confirm")
    yield _meta("confirm")
    try:
        with trace_span("seller.graph.confirm", "chain"):
            outcome = await confirm_draft(
                draft_id, seller_id=context.seller_id, brand_id=context.brand_id
            )
    except SpringUnavailableError:
        _mark_seller_degraded("spring_write_failed")
        yield _token(_CONFIRM_SPRING_DOWN_TOKEN)
        yield _error(
            "INTERNAL",
            "상품 서버 통신에 실패했습니다.",
            request_id=request_id,
            retryable=True,
        )
        return
    except Exception:
        _seller_log(
            logging.ERROR,
            "seller_confirm_failed",
            context=context,
            thread_id=request.thread_id,
            action="confirm",
            error_code="INTERNAL",
            status="FAILED",
        )
        yield _error(
            "INTERNAL",
            "일시적인 오류가 발생했습니다.",
            request_id=request_id,
            retryable=True,
        )
        return
    await seller_thread.record_turn(context, request.thread_id, "(초안 승인)", outcome.text)
    # [#506] 등록(create) 승인 완료 — 대기 세션이 이 draftId 를 가리키면 해제한다
    # (수정·발송 confirm 이 남의 등록 대기를 지우지 않게 draftId 대조).
    if outcome.status == "executed":
        pending_create = await draft_session.load_pending(context, request.thread_id)
        if pending_create is not None and pending_create.draft_id == request.draft_id:
            await draft_session.clear_pending(context, request.thread_id)
    yield _token(outcome.text)
    # 실제 쓰기(executed)만 대시보드·목록 재조회 유발 — 나머지는 변경 없음.
    yield _done("refresh" if outcome.status == "executed" else "keep")


async def _seller_stream(
    request: SellerChatRequest,
    identity: Identity,
    *,
    request_id: str | None = None,
) -> AsyncIterator[str]:
    """판매자 챗 통합 스트림 (4-1b) — 입구 판정 ①②③ 후 3분기 위임."""
    request_id = _resolve_request_id(request_id)
    # ⓪ 신원 숫자 캐스팅 — 전 레인 공통(§2.6 숫자 계약). 숫자가 아닌 클레임은
    # 토큰 발급 결함이므로 fail-closed 로 봉투 종료한다(레인 진입 전 차단).
    try:
        context = _seller_context(identity)
    except (TypeError, ValueError):
        _seller_log(
            logging.WARNING,
            "seller_identity_rejected",
            identity=identity,
            thread_id=request.thread_id,
            error_code="INVALID_SELLER_IDENTITY",
            status="REJECTED",
        )
        yield _error(
            "INTERNAL",
            "일시적인 오류가 발생했습니다.",
            request_id=request_id,
            retryable=False,
        )
        return

    # ① confirm 필드 선판정 (A-2 최상위 구조화 필드, LLM 0회) → HITL 실행 레인(4-2).
    # action=="confirm" 이면 draftId 는 스키마 validator 가 보장한다(발화 ≠ 동의 [HARD]).
    if request.action == "confirm":
        async for line in _confirm_stream(request, context, request_id=request_id):
            yield line
        return

    # ①.3 등록 초안 대기 게이트 (#506, FE 계약 §5.7) — ①(confirm 버튼) 다음, 나머지
    # 전부보다 앞이다. [리뷰 M-1a] ①.5(apply)보다 앞인 이유: 초안 대기 중 "N번
    # 적용해줘"가 게이트를 우회해 **두 번째 draft** 를 발급하면 이전 create draft 와
    # 동시 생존한다 — 대기 중 딴 작업은 게이트가 차단(offtopic 안내)해야 한다.
    # ②(scope)보다 앞인 이유: "취소"·"상품명 짧게" 같은 초안 문맥 발화가 scope 필터에
    # 걸릴 수 있다. 대기가 있을 때만 돌린다 — 대기 없는 발화 오인을 구조적으로 없앤다.
    pending_create = await draft_session.load_pending(context, request.thread_id)
    if pending_create is not None:
        # 새 사진 첨부는 판정 없이 수정 턴(대표사진 교체 + 재분석)이다 — FE 계약 §3.4.
        if request.image_urls:
            async for line in _product_stream(
                request, context, request_id=request_id, pending=pending_create
            ):
                yield line
            return
        if _is_cancel_message(request.message):
            # 취소는 코드 단축경로 — 위험이 비대칭이라(§5.7) LLM 판정을 기다리지 않는다.
            async for line in _cancel_pending_stream(
                request, context, pending_create, request_id=request_id
            ):
                yield line
            return
        action = await _classify_pending_utterance(request.message)
        if action == "cancel":
            async for line in _cancel_pending_stream(
                request, context, pending_create, request_id=request_id
            ):
                yield line
            return
        if action == "modify":
            async for line in _product_stream(
                request, context, request_id=request_id, pending=pending_create
            ):
                yield line
            return
        if action == "approve":
            # 텍스트 승인은 받지 않는다(발화 ≠ 동의 [HARD]) — 버튼 안내만, 초안 유지.
            _set_trace_lane("product")
            yield _meta("product")
            yield _token(
                "등록은 안전을 위해 카드의 [등록] 버튼으로만 진행됩니다. "
                "오른쪽 초안 카드에서 [등록]을 눌러주세요. 수정할 내용이 있으면 말씀해 주세요."
            )
            yield _done("keep")
            return
        if action == "offtopic":
            # 딴 주제 차단 — 초안 유지 + 탈출구 문안(새로고침으로 카드를 잃은 경우 대비).
            _set_trace_lane("product")
            yield _meta("product")
            yield _token(
                "진행 중인 등록 초안이 있습니다. 먼저 오른쪽 카드에서 등록하거나 "
                "취소해주세요. 화면에 카드가 보이지 않으면 채팅에 '취소'라고 입력해주세요."
            )
            yield _done("keep")
            return
        # 판정 실패(None) — 초안은 유지한 채 일반 흐름으로 낙하한다(비파괴 폴백).
        # 낙하 경로가 product 레인에 닿으면 ③이 pending 을 넘겨 이전 draftId 무효화가
        # 이어진다(리뷰 M-1b — 동시 생존 draft 차단).

    # ①.5 추천 적용 코드 선판정 ("N번 적용해줘" 정형 발화, LLM 0회) — 4-3 §6.3.
    apply_n = parse_apply_message(request.message)
    if apply_n is not None:
        async for line in _apply_stream(
            apply_n,
            request,
            context,
            request_id=request_id,
        ):
            yield line
        return

    # ①.7 기간 확인 대기 선판정 (코드 판정, LLM 0회) — #345, DESIGN-SELLER-PERIOD §5.5.
    # ②(scope)보다 **앞**이어야 한다: "응" 은 판매 도메인 어휘가 아니라 scope 필터에
    # 걸릴 수 있다. ①·①.5 보다는 뒤다 — 그 둘이 더 명시적인 신호(구조화 필드·정형
    # 발화)라 기간 확인이 가로채면 안 된다.
    # 대기가 있을 때만 승인 판정을 돌린다 — 대기 없는 상태의 "응" 을 승인으로 오인할
    # 여지를 구조적으로 없앤다. 승인이 아니면(수정·새 질문 전부) 대기를 폐기하고
    # 아래 일반 흐름으로 흘린다(3경로 — 수정 전용 파서를 두지 않는 이유는 DESIGN §5.1).
    pending_period = await seller_period_confirm.load_pending(context, request.thread_id)
    if pending_period is not None:
        if parse_period_approval(request.message):
            # 승인은 1회성이다 — 실행 전에 폐기해 실패하더라도 재승인으로 두 번 돌지 않는다.
            await seller_period_confirm.clear_pending(context, request.thread_id)
            recent_turns = await seller_thread.load_recent_turns(context, request.thread_id)
            async for line in _analysis_stream(
                request,
                context,
                recent_turns,
                request_id=request_id,
                pending=pending_period,
            ):
                yield line
            return
        await seller_period_confirm.clear_pending(context, request.thread_id)

    # [#506] 이미지 첨부 턴은 supervisor 를 거치지 않고 product 레인 직행 — 사진을 실은
    # 발화의 목적지는 등록 초안뿐이고, 라우팅 LLM 은 이미지를 볼 수 없어 판정 근거도 없다.
    if request.image_urls:
        async for line in _product_stream(request, context, request_id=request_id):
            yield line
        return

    # ② scope 선차단 (LLM 0회) — 전 레인 공통 코드 경로. 도메인 밖 = 대화(패널 유지).
    # 거절 턴은 스레드에 기록하지 않는다 — 도메인 밖 장문이 맥락을 오염시키지 않게.
    refusal = check_scope(request.message)
    if refusal:
        _set_trace_lane("refused")
        yield _meta("refused")
        yield _token(refusal)
        yield _done("keep")
        return

    # ③ 대화 스레드 최근 턴 1회 조회(실패는 [] degrade) → supervisor 라우팅 —
    # 장애 시 general 폴백은 route_question 내부(4-1a). 맥락은 supervisor 입력과
    # analysis planner 입력에 주입되고, general 은 스레드 자체를 물고 있어 불필요.
    recent_turns = await seller_thread.load_recent_turns(context, request.thread_id)
    try:
        with trace_span("seller.routing", "chain"):
            decision = await route_question(
                request.message, context, recent_turns=recent_turns, screen=request.screen
            )
    except LLMNotConfigured:
        _set_trace_lane("general")
        yield _meta("general")
        yield _llm_unavailable(
            lane="routing",
            thread_id=request.thread_id,
            request_id=request_id,
            context=context,
        )
        return
    _seller_log(
        logging.INFO,
        "seller_routed",
        context=context,
        thread_id=request.thread_id,
        action=decision.category,
        status="ROUTED",
    )

    if decision.category == "analysis":
        async for line in _analysis_stream(
            request,
            context,
            recent_turns,
            request_id=request_id,
        ):
            yield line
    elif decision.category == "product":
        # [리뷰 M-1b] 게이트 판정 실패 낙하로 온 경우에도 pending 을 전달한다 —
        # 새 create draft 발급 시 이전 draftId 무효화가 누락되지 않게(동시 생존 차단).
        async for line in _product_stream(
            request, context, request_id=request_id, pending=pending_create
        ):
            yield line
    else:
        async for line in _general_stream(request, context, request_id=request_id):
            yield line


@router.post("/seller/chat")
async def seller_chat(
    request: SellerChatRequest,
    http_request: Request,
    identity: Identity = Depends(require_seller),
) -> StreamingResponse:
    """판매자 챗봇 SSE 스트리밍 (S-4, api-spec §3.2).

    신원(sellerId/brandId)은 require_seller 가 검증된 판매자 JWT 클레임에서
    확보한다(스코프 없으면 403). 4-1b 부터 supervisor 3분기 디스패치가 배선됐다.

    [합류 2026-07-20 rebase] 스트림 수명주기(§2.9)는 팀 공통 래퍼 open_stream 소관 —
    (a) threadId 당 동시 1스트림(409) (b) 연결 종료 취소 (c) first-token/전체 타임아웃.
    대화 저장·구조화 로그(obs #8)는 start_observation 이 담당한다(chat 과 동일 패턴).
    """
    request_id = get_request_id(http_request)
    trace = start_request_trace_safely(
        name="seller_chat_turn",
        request_id=request_id,
        conversation_id=request.session_id,
        thread_id=request.thread_id,
        lane="seller",
        environment=get_settings().app_environment,
    )
    # [#326] 콘텐츠 추적 모드에서만 발화 원문이 루트 span 에 실린다(off 면 no-op).
    trace.record_request_content(input_text=request.message)
    try:
        store = await get_conversation_store()
    except asyncio.CancelledError:
        await finish_trace_safely(
            trace,
            status=TurnStatus.CANCELLED,
            error_type=None,
            terminal_reason="client_disconnect",
        )
        raise
    except Exception as exc:
        await finish_trace_safely(
            trace,
            status=TurnStatus.FAILED,
            error_type="INTERNAL",
            terminal_reason="store_unavailable",
        )
        # chat.py 와 동일 — pg-profile 지연 연결 실패(운영 jwks raise)가 open_stream 안전망 밖이라
        # §6.3 b chat_request 로그(errorType 집계)를 통째로 놓친다. rejection 로그를 남기고 전파한다
        # (PR #48 후속 리뷰).
        emit_rejection(
            request_id,
            "INTERNAL",
            conversationId=request.session_id,
            threadId=request.thread_id,
            sellerId=identity.seller_id,
            brandId=identity.brand_id,
        )
        # 같은 pg-profile 장애가 구매자 /chat 은 503, 판매자 /seller/chat 은 500 으로 갈리던
        # 비대칭을 제거한다(§2.5 STATE_UNAVAILABLE). 판별은 chat.py 와 같은 경계 함수를 쓰며
        # 실제 I/O 장애(timeout·pool·connection)만 변환한다 — programming/domain 오류를
        # 503 으로 마스킹하면 코드 버그가 "일시 장애"로 묻힌다.
        if is_state_store_unavailable(exc):
            raise SessionStateUnavailable from exc
        raise
    observation = start_observation(
        request_id=request_id,
        identity=identity,
        conversation_id=request.session_id,
        thread_id=request.thread_id,
        message=request.message,
        store=store,
        now=asyncio.get_running_loop().time(),
        trace=trace,
        buyer_session=None,
    )
    return await open_stream(
        http_request,
        registry_key(identity, request.thread_id),
        # [#427] 판매자 레인은 구제 체인 공유 예산을 쓰지 않는다 — 받아서 무시한다(시그니처만
        # open_stream 의 새 계약에 맞춘다).
        lambda _turn_started_at: _seller_stream(request, identity, request_id=request_id),
        observer=observation,
        role="seller",
    )
