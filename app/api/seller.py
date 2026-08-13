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
  ②.5 차트 요청 선판정(wants_chart_keyword, LLM 0회, #531) → _analysis_stream 직행:
     차트 좌표는 analysis 레인의 report 이벤트에만 실린다(경로 B).
  ③ supervisor 라우팅(route_question — 장애 시 general 폴백은 함수 내부).
분기: [#591] analysis·general → **search 레인**(_general_stream, 조회 도구 12종 +
get_latest_report) — meta.lane 만 갈린다 /
product → draft 검증(validate_draft)·checkpoint 저장(start_draft)·draft emit.
run_analysis_pipeline(5단 분석)은 게이트 ②.5 차트 경로(_analysis_stream)에만 남는다.
스트림 수명주기(409·취소·타임아웃 §2.9 공통)는 팀 공통 래퍼 open_stream 소관 —
chat.py 와 동일하게 registry_key(identity, threadId) 로 방당 1스트림을 강제한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import perf_counter
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage
from pydantic.alias_generators import to_camel

from app.agents.seller import (
    category_catalog,
    category_resolver,
    draft_lifecycle,
    draft_session,
    hitl,
)
from app.agents.seller import thread as seller_thread
from app.agents.seller import analysis_store, report_view
from app.agents.seller.analysis_store import note_seller_seen
from app.agents.seller.checkpoint import get_checkpointer
from app.agents.seller.context import SellerContext
from app.agents.seller import history
from app.agents.seller.history import apply_recommendation

# [#622] invalidate_draft/start_draft 는 더 이상 여기서 직접 부르지 않는다 — draft
# 발급(무효화·checkpoint 저장·pending 갱신)은 draft_lifecycle.publish_draft 단일
# 입구를 통한다(아키텍처 테스트: tests/unit/test_seller_draft_lifecycle.py).
# `hitl` 모듈 자체는 [#620] 가격 변경 시 사전 재조회(hitl._find_product)에 쓴다.
from app.agents.seller.hitl import DraftRecord, confirm_draft, validate_draft
from app.agents.seller.middleware import StreamingOutputGuard, check_scope, mask_output
from app.agents.seller.models import SellerRole, init_seller_model, seller_trace_model_metadata
from app.agents.seller.preview import build_create_preview, parse_int_or_none
from app.agents.seller.vision import ProductImageAnalysis, analyze_product_images
from app.agents.seller.orchestrator import (
    PipelineResult,
    route_question,
    run_analysis_pipeline,
)
from app.agents.seller.period import disclosure_text, resolve_from_message
from app.agents.seller.pipeline import (
    APPLY_GUIDE,
    format_general_input,
    parse_apply_message,
    split_report_summary,
    wants_chart_keyword,
)
from app.agents.seller.prompts import PENDING_DRAFT_GATE_PROMPT
from app.agents.seller.schemas import DraftChange, DraftProposal, PendingDraftAction
from app.agents.seller.workers import build_general_agent, build_product_agent
from app.api.deps import require_seller
from app.core.auth import Identity
from app.core.clock import now_kst, today_kst
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
from app.schemas.seller_report import (
    SellerReportListItem,
    SellerReportListResponse,
)
from app.schemas.spring import SellerProductRow
from app.services.spring_client import SpringRejected, SpringUnavailableError

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

# 레인(meta.lane) — supervisor 3분기 + 코드 선판정 4종(confirm·apply·scope·chart).
# [#531] chart 선판정(②.5)은 새 lane 을 만들지 않고 analysis 를 재사용한다 —
# 좌표를 싣는 report 이벤트가 그 레인의 것이라 목적지가 같다.
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
    lane: Lane = "general",
) -> AsyncIterator[str]:
    """search 레인 — general_agent astream → token/done (3-7 — SPEC §7 수명주기·degrade).

    [#591] supervisor 의 `general`(조회)과 `analysis`(저장된 보고서를 찾는 의도)가 **같은
    이 함수**를 쓴다. `analysis` 가 5단 분석 파이프라인을 부르던 자리를 여기로 옮긴 것이라
    (`_analysis_stream` 은 게이트 ②.5 차트 경로 전용으로 남는다), 실행 경로만 바뀌고
    `decision.category` 3분기 구조와 `Lane` 6종 값은 그대로다(S-4 무개정).

    `lane` 은 meta 첫 프레임·트레이스·로그에만 쓴다 — 도구 목록도 프롬프트도 기간 처리도
    두 레인이 동일하다. 값을 나눠두는 이유는 FE 가 `meta.lane` 으로 우측 패널을 준비하고
    (계약 §3.3), 로그의 레인 분포가 supervisor 판정과 1:1로 맞아야 하기 때문이다.

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
    _set_trace_lane(lane)
    # search 레인은 항상 대화(우측 패널 유지) — 첫 프레임에 레인을 알린다.
    yield _meta(lane)
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
            today=today_kst(),
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
                    today=today_kst().isoformat(), checkpointer=checkpointer
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
            lane=lane,
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
            action=lane,
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
                action=lane,
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
            action=lane,
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
) -> AsyncIterator[str]:
    """분석 레인 (4-1b) — 파이프라인 emit(진행)을 progress 로, 최종 답변을 token 으로 중계.

    - 진행 상태는 최종 답변이 아니므로 `progress` 이벤트로 분리한다(FE: 로딩 표시).
      최종 산출은 단일 `token`(보고서/되묻기/사과 공통) + kind=="report" 일 때만
      구조화 `report` 이벤트 1회(우측 패널 재료, 이슈 #296) → 패널 교체 여부는
      kind 로 갈린다(아래 panel).
    - 패널: kind=="report" 만 우측 교체(replace) — 되묻기(clarification)·사과(apology)·
      거절(refused)은 대화이므로 유지(keep).
      (FE 요구 2·3 — "화면 바뀔 질문만" 교체.)
    - **예외 2경우**(planner 장애·1차 report 실패)만 여기로 전파 — 사과 token 후
      error 로 종료(REVIEW-STAGE3 §5-2). error 종료는 패널 유지(done 없음).
    - 진행 문구는 파이프라인 내부 상수라 마스킹 불필요, 최종 text 는 mask_output 적용.

    [#584] 기간 확인 승인 재개(구 pending 인자)는 게이트 ①.7 과 함께 사라졌다 —
    코드가 값을 보충한 기간은 확인 없이 실행되고 해석은 응답 첫 줄에 고지된다.
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
            return await run_analysis_pipeline(
                request.message,
                context,
                today=today_kst(),
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
        # 대화 스레드 기록(best-effort) — 되묻기 포함 최종 문안이 후속 발화의 맥락이 된다.
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


async def _ensure_draft_category(
    proposal: DraftProposal,
    *,
    message: str,
    analysis: ProductImageAnalysis | None,
    pending: draft_session.PendingCreate | None,
) -> tuple[DraftProposal, bool]:
    """[#506 후속] create 초안의 카테고리를 코드가 책임지고 채운다.

    BE `categoryId` 는 필수라 카테고리 없는 create 초안은 승인해도 등록되지 않는다.
    에이전트가 못 골랐거나(후보가 애매) 목록 밖 값을 적었으면 여기서 세 단계로 복구한다:

      ① 수정 턴이면 **기존 초안의 카테고리**를 되살린다("가격만 바꿔줘" 턴에서
         확정해 둔 카테고리가 조용히 사라지는 사고를 막는다).
      ② 넓힌 후보로 LLM 택1(category_resolver) — 판매자가 대충 말한 상품군을 실제
         카테고리 id 로 확정하는 지점이다.
      ③ 그래도 못 고르면 그대로 둔다 — validate_draft 가 카테고리를 되묻는다.

    잘못 배정하느니 되묻는다: 카테고리는 등록 후 변경할 수 없다(preview 경고와 동일,
    BE I-11 에는 category 필드 자체가 없다).

    [#622] 반환의 두 번째 값(`revived`)은 ① 경로를 탔는지다 — 이번 발화가 사실은
    카테고리를 바꾸려는 의도였는데 에이전트가 후보를 애매하게 봐서 카테고리를 빼고
    반환한 경우에도, ①은 발화 의도를 판별하지 않고 무조건 이전 값을 되살린다. 그
    발화 의도 판별 로직을 새로 넣는 대신(#622 결정 — 오탐 위험과 구현 범위 고려), 호출부가
    `revived`를 preview note에 "카테고리는 이전 초안 값을 유지했습니다"로 노출해 판매자가
    승인 전에 최소한 알아챌 수 있게 한다. `diff_notes`가 이 경우를 note 로 못 잡는 이유는
    before==after(둘 다 이전 값)라 필드 자체가 diff 되지 않기 때문이다.
    """
    if proposal.op != "create":
        return proposal, False
    current = next((c.after for c in proposal.changes if c.field == "category"), None)
    if current is not None and category_catalog.get(current) is not None:
        return proposal, False

    revived = False
    resolved: str | None = None
    if pending is not None and (kept := pending.changes.get("category")):
        if category_catalog.get(kept) is not None:
            resolved = kept
            revived = True
    if resolved is None:
        hint = analysis.category_hint if analysis is not None else None
        entry = await category_resolver.resolve_category(message, hint=hint)
        resolved = entry.id if entry is not None else None
    if resolved is None:
        # 목록 밖 값이 남아 있으면 걷어낸다 — validate_draft 의 "누락" 안내가
        # "잘못된 값" 안내보다 판매자에게 할 일을 정확히 알려준다.
        if current is not None:
            return (
                proposal.model_copy(
                    update={"changes": [c for c in proposal.changes if c.field != "category"]}
                ),
                False,
            )
        return proposal, False

    changes = [c for c in proposal.changes if c.field != "category"]
    changes.append(DraftChange(field="category", before="", after=resolved))
    return proposal.model_copy(update={"changes": changes}), revived


def _product_agent_input(
    request: SellerChatRequest,
    *,
    analysis: ProductImageAnalysis | None,
    candidates: list[category_catalog.CategoryEntry],
    pending: draft_session.PendingCreate | None,
    image_urls: list[str],
    recent_turns: list[seller_thread.Turn] = (),
) -> str:
    """[#506] product 에이전트 입력 조립 — 발화 + 이미지 분석·카테고리 후보·기존 초안 주입.

    이미지 원본이 아니라 **분석 결과(텍스트)** 를 주입한다 — 분석은 첨부 턴 1회이고
    (vision.py), 에이전트는 텍스트 루프를 유지해 매 턴 이미지 토큰이 들지 않는다.

    [상품명 인식 개선] recent_turns 가 있으면 [최근 대화] 블록으로 먼저 주입한다 —
    product 레인은 매 턴 새 agent.ainvoke() 호출이라 checkpointer 메모리가 없다
    (general 레인과 다름). 대상 상품이 불명확해 되물은 다음 턴에서 판매자의 답만
    보면 직전에 무엇을 물었는지 알 수 없어 다시 헤매는 문제(대상 특정 실패의 주된
    원인)를 이 블록으로 완화한다 — supervisor 라우팅을 거쳐 product 레인에 처음
    진입하는 호출부에서만 채워진다(등록 초안 대기 중 수정/사진 계속 경로는 pending
    이 이미 맥락을 나른다).
    """
    blocks: list[str] = []
    if recent_turns:
        history_lines = "\n".join(
            f"- {'판매자' if role == 'user' else 'assistant'}: {text}" for role, text in recent_turns
        )
        blocks.append("[최근 대화]\n" + history_lines)
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
    pending_unknown: bool = False,
    recent_turns: list[seller_thread.Turn] | None = None,
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

    [#622] `pending_unknown=True`(등록 초안 대기 조회 실패)면 새 draft 를 발급하지 않고
    막는다 — 이 상태에서 발급하면 실제로 대기 중이던 create draft(조회만 실패했을 뿐
    존재할 수 있다)가 무효화되지 않은 채 두 번째 draft 와 동시 생존할 수 있다. 이 스트림에
    닿는 진입점 전부(사진 직행·게이트 낙하·supervisor 라우팅)가 이 가드를 공유한다.

    [상품명 인식 개선] `recent_turns`는 supervisor 라우팅 경로(③)에서만 이미 로드된
    맥락을 넘겨받는다(호출부 참조) — 등록 초안 대기 중 사진 계속/수정 경로(①.3)는
    pending 이 이미 맥락을 나르므로 넘기지 않는다(기본값 None → 빈 컨텍스트, 기존
    동작 그대로).
    """
    request_id = _resolve_request_id(request_id)
    _set_trace_lane("product")
    yield _meta("product")
    if pending_unknown:
        yield _token(draft_lifecycle.UNKNOWN_STATE_BLOCK_TEXT)
        yield _done("keep")
        return
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
        recent_turns=recent_turns or [],
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
                    # [이슈 #621] product 단독 호출 전용 상한으로 분리 — 분석 워커 6종과
                    # 공유하던 seller_worker_timeout_s(60s, 팬아웃 기준)는 management
                    # 레인엔 느슨했다(§config._require_management_lane_within_stream_cap).
                    timeout=settings.seller_product_agent_timeout_s,
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

    # [#506 후속] 카테고리 복구 — 선검증 **전**에 돈다. validate_draft 는 카테고리
    # 누락을 되묻기로 바꾸므로, 복구 기회를 그 앞에 두지 않으면 판매자가 이미 충분히
    # 말한 상품군인데도 한 번 더 묻게 된다(LLM 호출은 실패한 턴에만 1회 추가).
    proposal, category_revived = await _ensure_draft_category(
        proposal, message=request.message, analysis=analysis, pending=pending
    )

    # [#620] update 초안이 price/original_price 를 건드리면 미리 I-9 재조회해 row 를
    # 넘긴다 — validate_draft 가 BE validatePriceRange 와 같은 규칙(price ≤
    # originalPrice, 생략 필드는 저장된 값)으로 카드 표시 전에 되물을 수 있게 한다.
    # 그 외 op·필드는 이 추가 Spring 왕복이 필요 없다(row=None 이면 이 검사만 건너뛴다).
    # Spring 장애는 이 선택적 2차 검증 때문에 초안 생성 전체를 막지 않는다 — 실패하면
    # row=None 으로 건너뛰고 confirm 시점 BE 422(InvalidPrice)에 맡긴다(안전망 유지).
    row = None
    if proposal.op == "update" and proposal.product_id is not None:
        touches_price = any(
            c.field in ("price", "original_price") for c in proposal.changes
        )
        if touches_price:
            try:
                # [#622] _find_product 는 (row, exhausted) 튜플을 반환한다 — 이 사전
                # 검증은 어차피 best-effort(못 찾으면 검사만 건너뛰고 confirm 시점 BE
                # 422 에 맡긴다)라 exhausted(상한 소진 vs 진짜 없음)는 구분하지 않는다.
                row, _exhausted = await hitl._find_product(context.brand_id, proposal.product_id)
            except SpringUnavailableError:
                row = None

    # 코드 선검증(4-2) — 실행 불가능한 draft 는 FE 에 보여주기 전에 되묻는다.
    record, problem = validate_draft(
        proposal, seller_id=context.seller_id, brand_id=context.brand_id, row=row
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

    # [#623] update 초안의 changes[].before 를 코드 소유로 이관 — list_my_products 가
    # 5개 필드(id·name·price·stock·status)만 요약해 LLM 이 originalPrice·category·
    # description·imageUrl 의 before 를 채울 수 없던 구조적 결함(빈 문자열 → confirm
    # 시점 find_stale_changes 가 항상 불일치로 오판, 영구 반영 차단)을 막는다.
    record = await _snapshot_before(record, row=row)

    try:
        # [#622] invalidate·checkpoint 저장·pending 갱신 단일 입구 — 이전엔 invalidate_draft/
        # save_pending 이 `if record.op == "create":` 안에서만 불려, 수정 턴이 op="update"로
        # 응답하면 대기 중이던 이전 create draft 가 무효화도 pending 갱신도 안 된 채 남았다.
        lifecycle_notes = await draft_lifecycle.publish_draft(
            context,
            request.thread_id,
            record,
            prev=pending,
            image_urls=image_urls,
            analysis=analysis,
        )
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

    # ── [#506] create 초안 preview 구성 ──────────────────────────────────────────
    preview: dict | None = None
    if record.op == "create":
        notes = list(lifecycle_notes)
        if category_revived:
            # [#622] ①(카테고리 되살림, _ensure_draft_category)이 발화 의도와 무관하게
            # 이전 값을 되살렸을 수 있다 — before==after 라 diff_notes 는 이 경우를
            # 못 잡으므로 별도로 note 를 붙여 승인 전에 알아챌 수 있게 한다.
            notes.append("카테고리는 이전 초안 값을 유지했습니다.")
        seller_inputs = _seller_input_summary(record)
        preview = build_create_preview(
            record,
            analysis=analysis,
            seller_inputs=seller_inputs,
            modified_notes=notes or None,
        )

    await seller_thread.record_turn(
        context, request.thread_id, request.message, _draft_recorded_text(record)
    )
    yield _draft_event(record, preview=preview)
    yield _done("replace")  # diff 카드 = 우측 패널 교체


async def _snapshot_before(record: DraftRecord, *, row: SellerProductRow | None) -> DraftRecord:
    """update 초안의 changes[].before 를 조회 시점 실제값으로 코드가 덮어쓴다(#623).

    [배경] list_my_products(도구)는 컨텍스트 폭주 방지를 위해 8개 필드 중
    productId·name·price·stock·status 5개만 요약한다 — originalPrice·category·
    description·imageUrl 은 없다. PRODUCT_PROMPT 가 "before 는 조회값 그대로"를
    강제하던 구 규약에서는, 이 4개 필드를 건드리는 update 초안의 before 가 항상
    빈 문자열이었다. confirm 시점 find_stale_changes(hitl.py)는 이 빈 문자열을
    실제 현재값과 대조해 **항상** 불일치로 판정 — 재시도해도 같은 이유로 영구
    차단됐다(#623 증상). 이 함수는 P4(#590 apply_recommendation)가 이미 쓰는
    hitl._find_product + history._current_value_str 패턴을 product 레인에도 적용해
    before 를 LLM 산물에서 코드 소유로 옮긴다 — PRODUCT_PROMPT 는 더는 before 를
    책임지지 않는다(prompts.PRODUCT_PROMPT 참조).

    stock_quantity 는 예외로 LLM 값을 그대로 둔다: list_my_products 는 옵션별
    재고를 이미 정확히 노출하고(#524), 여기서 _current_value_str(row,
    "stock_quantity")로 덮어쓰면 옵션별 값이 아니라 **행 전체 합계**로 뭉개진다.
    find_stale_changes 도 stock_quantity 는 애초에 비교 대상에서 제외한다(주문
    차감으로 인한 자연 변동, hitl._STALE_EXEMPT_FIELDS) — 그대로 둬도 안전하다.

    조회 실패(Spring 장애·상품 미발견·페이지 상한 소진)는 soft-fail 로 처리한다 —
    이번 턴 초안 발급 자체를 막지 않는다(가격 사전검증과 같은 기존 방침, 위
    `touches_price` 블록 참조). 실제 불일치·미발견은 confirm 시점
    hitl.find_stale_changes/_execute_draft 가 최종 방어선으로 남는다.

    op != "update"(create·delete·ship)는 그대로 반환한다 — create 는 원래
    before="" 계약이고, delete 의 status before(list_my_products 조회값)는
    find_stale_changes 가 애초에 비교하지 않으며, ship 은 changes 가 비어 있다.
    """
    if record.op != "update":
        return record
    assert record.product_id is not None  # validate_draft 가 보장(update 필수)
    if row is None:
        try:
            row, _exhausted = await hitl._find_product(record.brand_id, record.product_id)
        except SpringUnavailableError:
            row = None
    if row is None:
        return record  # soft-fail — 기존 before 유지, confirm 시점 안전망에 위임

    changes = [
        c.model_copy(update={"before": history._current_value_str(row, c.field)})
        if c.field != "stock_quantity"
        else c
        for c in record.changes
    ]
    return record.model_copy(update={"changes": changes})


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
        # [#541] 카테고리 2칸 표기도 같은 마스킹을 탄다 — 표시 키를 늘릴 때 이 목록을
        # 같이 늘리지 않으면 그 키만 마스킹을 비껴간다(#524 lesson "입구를 전부 센다").
        "categoryMajor",
        "categorySubPath",
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

    [#506] op="create" 는 preview{} 를 함께 싣는다(§3.2 추가 전용, v0.31.0) — FE 등록
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
                    # [#524] 옵션별 재고 change 에만 실리는 추가 전용 키 — FE 는 모르는
                    # 키를 무시한다(§3.2 확장 규칙). 값은 표시용 옵션명이다.
                    **(
                        {"optionName": mask_output(_strip_unsafe(c.option_name))}
                        if c.option_name
                        else {}
                    ),
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
    """PipelineResult → SSE `report` 이벤트. 페이로드 조립은 `_report_payload` 가 한다.

    [#599] 조립부를 분리한 이유: R-2(보고서 상세 API)가 **같은 조립기**를 써야 채팅 패널과
    보고서 페이지가 어긋나지 않는다. R-2 는 SSE 프레임이 아니라 dict 가 필요하고, 저장된
    보고서는 자기 제목·생성 시각을 가지므로 그 두 값만 덮어쓴다.
    """
    return _sse("report", _report_payload(result))


def _report_payload(
    result: PipelineResult,
    *,
    title: str | None = None,
    generated_at: datetime | None = None,
) -> dict:
    """PipelineResult → `report` 페이로드 dict (이슈 #296, api-spec §3.2 v0.24.0 — 구 chart 대체).

    [#599] `title`·`generated_at` 은 저장 보고서를 되살릴 때만 넘긴다(R-2). 생략하면 채팅
    레인의 기존 동작 그대로다 — 제목은 고정 문구, 시각은 호출 시각.

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
    # [#504] chartPeriod 는 차트 기간이 분석 기간과 **다를 때만** 싣는다 — 없으면
    # period 와 같다는 뜻이라 FE 가 아무것도 그리지 않는다(계약). 가격·재고(스냅샷)
    # 차트는 애초에 chart_period 가 설정되지 않는다(기간 개념 없음 — summary 가 안내).
    chart_period = (
        {"from": result.chart_period[0].isoformat(), "to": result.chart_period[1].isoformat()}
        if result.chart_period and result.chart_period != result.period
        else None
    )
    return {
        # [#504] chart_only 턴은 보고서가 아니라 그래프가 주인공 — 제목으로 구분한다
        # (FE 는 report.title 을 그대로 쓰므로 FE 작업 0).
        # [#599] 저장 보고서는 자기 제목("8월 9일 일간 분석")을 가진다 — 오면 그것을 쓴다.
        "title": (
            title
            if title is not None
            else ("판매 분석 그래프" if result.chart_only else "판매 분석 보고서")
        ),
        "period": (
            {"from": result.period[0].isoformat(), "to": result.period[1].isoformat()}
            if result.period
            else None
        ),
        "chartPeriod": chart_period,
        # KST 고정(이슈 #296, api-spec §3.2 v0.24.0 계약) — 기준 시각은 app/core/clock.py.
        # [#599] 저장 보고서는 created_at 이 생성 시각 — 조회 시각을 쓰면 매번 바뀐다.
        "generatedAt": (generated_at or now_kst()).isoformat(timespec="seconds"),
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
                # [#504] 집계 방식 — 소스 레지스트리가 채운 값 그대로(FE 헤더 분기
                # 근거: sum=합계 / avg=평균 / none=스냅샷이라 헤더 숫자 숨김).
                "aggregate": c.aggregate,
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
        # [#504] 차트를 못 만든 사유 — 부분 성공이면 charts 와 **동시에** 나간다.
        # message 는 서버 완성 문장(charts.py 소유) — FE 는 그대로 렌더, reason 은
        # 로깅·QA 용 개방형 어휘라 닫힌 유니온으로 계약하지 않는다.
        "chartUnavailable": [
            {
                "reason": item.reason,
                "message": mask_output(_strip_unsafe(item.message)),
            }
            for item in result.chart_unavailable
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
    }


async def _apply_stream(
    n: int,
    request: SellerChatRequest,
    context: SellerContext,
    *,
    request_id: str | None = None,
    pending: draft_session.PendingCreate | None = None,
    pending_unknown: bool = False,
) -> AsyncIterator[str]:
    """추천 적용 레인 (4-3 §6.3 — 입구 ①.5 코드 선판정 후 진입, LLM 0회).

    최신 이력의 recommendations[N-1] 을 코드가 draft 로 변환(대화 재해석 금지) →
    4-2 와 동일하게 checkpoint 저장 후 draft emit — 이후 confirm 흐름 합류.
    불성립(이력 없음·인덱스 불일치·적용 불가 유형·상품 미발견)은 되묻기 token.
    패널: draft 성립 시 diff 카드(replace), 불성립은 대화(keep) — product 레인과 동일.

    [#622] `pending`(등록 초안 대기 게이트 판정 실패 낙하로 여기 온 경우) 이 있으면
    새 draft 발급 시 이전 create draft 를 무효화한다 — 이전엔 이 함수가 `pending`을
    받지 않아, 대기 중 "N번 적용해줘"가 게이트를 우회해 두 번째 draft 를 발급해도
    이전 draft 가 무효화되지 않았다(같은 문제를 supervisor 라우팅 product 분기는
    리뷰 M-1b 로 이미 고쳐 뒀었다). `pending_unknown`은 `_product_stream`과 동일한
    가드(모듈독스트링 참조).
    """
    request_id = _resolve_request_id(request_id)
    _set_trace_lane("apply")
    yield _meta("apply")
    if pending_unknown:
        yield _token(draft_lifecycle.UNKNOWN_STATE_BLOCK_TEXT)
        yield _done("keep")
        return
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
        # [#622] publish_draft — pending 이 있으면(게이트 낙하) 그 이전 create draft 를
        # 함께 무효화한다. 4-2(product 레인)와 같은 단일 입구.
        await draft_lifecycle.publish_draft(context, request.thread_id, record, prev=pending)
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
    await draft_lifecycle.cancel_pending(context, request.thread_id, pending)
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
    except SpringRejected:
        # [#620] 매핑 안 된 4xx — 서버가 요청 자체를 거부한 것이라 재시도해도 결과가
        # 같다. `SpringUnavailableError` 하위라 이 except 를 두지 않아도 아래 catch-all
        # 이 잡지만, 그러면 "일시적 오류·재시도 가능"으로 잘못 안내된다(이 이슈의
        # 핵심 증상) — 먼저 잡아 retryable=False 로 구분한다. draft 는 checkpoint 에
        # 남지만 재confirm 을 권하지 않는다(같은 4xx 가 반복될 뿐이다).
        yield _token(
            "죄송합니다. 서버가 이 요청을 거부해 반영하지 못했습니다. "
            "내용을 다시 확인해 새로 요청해 주세요."
        )
        yield _error(
            "INTERNAL",
            "요청이 거부되었습니다.",
            request_id=request_id,
            retryable=False,
        )
        return
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

    # 무인 분석 대상 자동 등록(결정 110~112, 이슈 #585) — fire-and-forget, 실패해도 이 스트림에
    # 영향 없다. 신원 캐스팅 성공 직후 1회만(캐스팅 실패 요청을 대상에 넣지 않기 위해 위쪽이 아닌
    # 여기다). require_seller 에 넣지 않는 이유는 OPS §1.7 참조(buyer 공용 sync 의존성).
    note_seller_seen(context)

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
    # [#622] 3상태 조회 — "none"(대기 없음)·"found"(대기 있음)·"unknown"(조회 실패,
    # draft_lifecycle 모듈독스트링 ②). ①.3 게이트는 "found"일 때만 돈다 — "unknown"은
    # 판정 근거(실제 pending 내용)가 없다. 아래 apply_n·image_urls 직행·③ product 분기는
    # lookup.state == "unknown"이면 pending_unknown=True 를 넘겨 새 draft 발급을 막는다
    # (draft_lifecycle.UNKNOWN_STATE_BLOCK_TEXT) — 조회 실패 중 신규 발급하면 실제로
    # 대기 중이던 draft 가 무효화 안 된 채 두 번째 draft 와 동시 생존할 수 있어서다.
    lookup = await draft_lifecycle.lookup_pending(context, request.thread_id)
    if lookup.state == "found":
        pending_create = lookup.pending
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

    pending_unknown = lookup.state == "unknown"

    # ①.5 추천 적용 코드 선판정 ("N번 적용해줘" 정형 발화, LLM 0회) — 4-3 §6.3.
    # [#622] 게이트 ①.3 판정 실패(None) 낙하로 여기 온 경우 pending 을 넘긴다 — ③(product)
    # 분기와 동일하게, 새 draft 발급 시 이전 create draft 무효화가 누락되지 않게(_apply_stream
    # 독스트링 — 리뷰 M-1b 와 같은 문제를 apply 경로에도 적용).
    apply_n = parse_apply_message(request.message)
    if apply_n is not None:
        async for line in _apply_stream(
            apply_n,
            request,
            context,
            request_id=request_id,
            pending=lookup.pending if lookup.state == "found" else None,
            pending_unknown=pending_unknown,
        ):
            yield line
        return

    # [#506] 이미지 첨부 턴은 supervisor 를 거치지 않고 product 레인 직행 — 사진을 실은
    # 발화의 목적지는 등록 초안뿐이고, 라우팅 LLM 은 이미지를 볼 수 없어 판정 근거도 없다.
    if request.image_urls:
        async for line in _product_stream(
            request, context, request_id=request_id, pending_unknown=pending_unknown
        ):
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

    # 대화 스레드 최근 턴 1회 조회(실패는 [] degrade) — 아래 ②.5·③ 이 공유한다.
    # 맥락은 supervisor 입력과 analysis planner 입력에 주입되고, general 은 스레드
    # 자체를 물고 있어 불필요.
    recent_turns = await seller_thread.load_recent_turns(context, request.thread_id)

    # ②.5 [#531] 차트 요청 코드 선판정 (LLM 0회) — supervisor 라우팅 **앞**에 둔다.
    #
    # 차트 좌표는 report 이벤트 charts[] 로만 나가고 그 이벤트는 analysis 레인에만 있다
    # (general 은 meta/token/done 뿐 — _general_stream 참조). 그런데 SUPERVISOR_PROMPT 는
    # "이번달 매출 그래프 보여줘"에 해석 신호가 없으니 general 을 고른다("의도 신호가
    # 없으면 general 이 기본값") — 프롬프트대로 정확히 동작한 결과 좌표가 나갈 자리 자체가
    # 사라지고, general_agent 가 ASCII 아트를 token 으로 그린다. #504 의 chart_only 경로도
    # planner 에 도달하지 못해 죽은 코드였다. 프롬프트에 예외를 끼우면 "조회=general" 축이
    # 흔들리므로(경계 예시 다수가 그 축에 의존) 코드로 결정론적으로 막는다.
    #
    # [위치 근거 — 순서가 이 판정의 전부다]
    # · ①(초안 대기 게이트)보다 뒤: 초안 대기 중 "그래프 보여줘"는 offtopic 분기가 초안을
    #   지켜야 한다(동시 생존 draft 차단).
    # · image_urls 직행(#506)보다 뒤: 사진을 실은 발화의 목적지는 등록 초안뿐이다.
    # · ②(scope)보다 뒤: 앞에 두면 "경쟁사 매출 그래프 보여줘" 같은 도메인 밖 요청이
    #   analysis 레인으로 새어 타 판매자 데이터를 조회하려 든다.
    # · load_recent_turns 뒤: 이미 실은 맥락을 그대로 넘기고 라우팅 LLM 1회만 아낀다.
    #
    # 로그를 선판정 안에서도 남기는 이유: 빼면 차트 턴이 seller_routed 집계에서 통째로
    # 사라져 레인 분포에 구멍이 생긴다. 라우터가 고른 것과 같은 필드로 찍는다.
    if wants_chart_keyword(request.message):
        _seller_log(
            logging.INFO,
            "seller_routed",
            context=context,
            thread_id=request.thread_id,
            action="analysis",
            status="ROUTED",
        )
        async for line in _analysis_stream(
            request,
            context,
            recent_turns,
            request_id=request_id,
        ):
            yield line
        return

    # ③ supervisor 라우팅 — 장애 시 general 폴백은 route_question 내부(4-1a).
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
        # [#591] `analysis` = "저장된 보고서를 찾는 의도"로 재정의됐다 — 5단 분석
        # 파이프라인이 아니라 search 레인(조회 도구 + get_latest_report)이 답한다.
        # 그 파이프라인은 채팅 밖 상주형으로 옮겨가고, 채팅에서 _analysis_stream 을
        # 부르는 곳은 이제 게이트 ②.5(차트)뿐이다. meta.lane 은 "analysis" 그대로다.
        async for line in _general_stream(request, context, request_id=request_id, lane="analysis"):
            yield line
    elif decision.category == "product":
        # [리뷰 M-1b] 게이트 판정 실패 낙하로 온 경우에도 pending 을 전달한다 —
        # 새 create draft 발급 시 이전 draftId 무효화가 누락되지 않게(동시 생존 차단).
        # [#622] lookup.state == "found"일 때만 pending 을 넘긴다 — "none"·"unknown"은
        # 애초에 넘길 pending 자체가 없다(unknown 은 pending_unknown 으로 별도 차단).
        # [상품명 인식 개선] 위에서 이미 로드해 둔 recent_turns 를 그대로 넘긴다 —
        # 대상 상품이 불명확해 되물은 다음 턴이 이 경로로 다시 들어올 때 직전
        # 되물음/list_my_products 결과를 기억하게 한다.
        async for line in _product_stream(
            request,
            context,
            request_id=request_id,
            pending=lookup.pending if lookup.state == "found" else None,
            pending_unknown=pending_unknown,
            recent_turns=recent_turns,
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


# ── R-1 / R-2 보고서 조회 (이슈 #599, 결정 6·10·113) ─────────────────────────────
#
# 분석 보고서는 `report` SSE 이벤트로 방출하지 않는다(결정 6) — 전용 페이지가 유일한 소비
# 경로이고 이 둘이 그 경로다. 인증은 S-4 와 같은 CH-6 SELLER 티켓이라 BE 신규 발급 경로가
# 없다(FE 는 기존 openSellerSession() 을 그대로 쓴다).


def _iso_z(value: datetime | None) -> str | None:
    """RFC3339 UTC(`Z`) 표기 — R-1·R-2 공통(확정 3).

    S-4 `report` 이벤트의 `generatedAt` 은 KST(+09:00) 고정 계약이라 조립기 기본값을 바꿀 수
    없다. 조회 API 는 표기를 하나로 통일하고 페이지가 KST 로 변환한다 — 서버가 타임존을
    결정하지 않는 쪽이 정석이다.

    ⚠️ FE `formatGeneratedAt()` 은 문자열 앞부분을 잘라 쓰므로 `Z` 를 그대로 넘기면 9시간
    어긋난 시각이 표시된다(밤 시간대는 날짜도 하루 달라진다). 페이지가 변환해서 넘긴다.
    """
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _report_context(identity: Identity) -> SellerContext:
    """조회 API 용 신원 캐스팅 — 실패는 401 `INVALID_SELLER_IDENTITY`.

    스트림 경로(`_seller_stream`)는 같은 실패를 error 이벤트로 봉투 종료한다. 조회 API 는
    스트림이 아니므로 같은 어휘를 HTTP 상태로 낸다 — 500 으로 새어 나가면 "서버 장애"로
    보여 토큰 발급 문제가 진단되지 않는다.
    """
    try:
        return _seller_context(identity)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INVALID_SELLER_IDENTITY",
                "message": "판매자 신원을 확인할 수 없습니다.",
            },
        ) from exc


def _mask_deep(value: object, _depth: int = 0) -> object:
    """중첩 구조의 모든 문자열에 조립기와 **같은 정제**를 건다.

    `_report_payload` 는 자기가 만드는 필드(summary·body·findings·추천)에 이미
    `mask_output` + `_strip_unsafe_multiline` 을 건다. R-2 가 그 위에 얹는 `segments`·
    `holds` 는 조립기를 거치지 않는데, `llmLabel`·`llmDesc` 는 **LLM 이 쓴 문장**이라
    같은 경계를 적용해야 한다 — 한 응답 안에서 어떤 필드는 정제되고 어떤 필드는 날것이면
    그 틈이 곧 유출 경로다(customerLabel 재식별 금지 규약과 같은 맥락).
    """
    if _depth > 4:
        return value
    if isinstance(value, str):
        return mask_output(_strip_unsafe_multiline(value))
    if isinstance(value, dict):
        return {k: _mask_deep(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_deep(v, _depth + 1) for v in value]
    return value


async def _no_report_reason(brand_id: int) -> str | None:
    """목록이 비었을 때의 사유(결정 113). 판정 불가면 **추정하지 않고** None.

    `not_registered`(대상 미등록 = 사고)와 `no_trigger`(이상 없음 = 정상)가 와이어에서 똑같이
    `items: []` 로 보이면 운영 사고가 몇 주간 드러나지 않는다. 그래서 사유를 갈라 싣되,
    근거가 없으면 "이상 없음"으로 단정하지 않는다 — 단정하면 "판정 보류 != 이상 없음"
    불변 규약을 와이어에서 깨는 것이다.

    `06_seller_analysis_ext.sql` 미적용 환경에서는 `last_run_at` 컬럼이 없어 조회가 예외로
    끝난다. 그때 R-1 이 죽으면 안 되므로(이슈 명시 방어 조항) 삼키고 None 을 돌려준다.
    """
    try:
        target = await analysis_store.get_target_status(brand_id)
    except Exception:
        logger.warning("seller_no_report_reason_unavailable brand=%s", brand_id, exc_info=True)
        return None
    if target is None:
        return "not_registered"

    ttl_days = get_settings().seller_analysis_target_ttl_days
    age_days = (datetime.now(UTC) - target.last_seen_at).days
    if age_days > ttl_days:
        # R-1 경로에서는 사실상 도달하지 않는다 — 호출 자체가 touch_target 으로
        # last_seen_at 을 갱신하기 때문이다(그래서 이 판정을 훅보다 **먼저** 한다).
        # 어휘를 남겨 두는 이유는 무인 배치 쪽 진단이 같은 값을 쓰기 때문이다.
        return "inactive"
    if target.last_run_at is None:
        # 등록됐으나 배치 미실행 — 사고가 아니라 정상 대기다(확정 2).
        return "pending_first_run"
    if target.last_skip_reason in ("no_trigger", "no_baseline"):
        return target.last_skip_reason
    return None


@router.get("/seller/reports", response_model=SellerReportListResponse)
async def seller_reports(
    identity: Identity = Depends(require_seller),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    unread_only: bool = Query(default=False, alias="unreadOnly"),
) -> SellerReportListResponse:
    """R-1 — 판매자 분석 보고서 목록.

    범위를 벗어난 `limit`/`offset` 은 FastAPI 검증이 잡고 `core.errors` 핸들러가
    400 `BAD_REQUEST` 봉투로 바꾼다(422 가 나가지 않는다).
    """
    context = _report_context(identity)
    brand_id = context.brand_id

    reports = await analysis_store.list_reports(
        brand_id, limit=limit, offset=offset, unread_only=unread_only
    )
    total = await analysis_store.count_reports(brand_id, unread_only=unread_only)
    unread = await analysis_store.count_unread_reports(brand_id)
    counts = await analysis_store.count_recommendations_by_reports(
        [r.id for r in reports], brand_id=brand_id
    )
    # 사유 판정을 훅보다 먼저 한다 — note_seller_seen 이 last_seen_at 을 갱신하므로
    # 순서가 뒤바뀌면 inactive 가 영원히 안 나온다.
    reason = await _no_report_reason(brand_id) if not reports else None

    # 이슈 3 잔여분 — #585 는 _seller_stream() 진입부에만 훅을 걸었다. 채팅은 안 쓰고
    # 보고서만 보는 판매자가 TTL 로 무인 순회 대상에서 탈락하는 것을 막는다.
    # fire-and-forget + 하루 1회 캐시라 목록 응답을 지연시키지 않는다.
    note_seller_seen(context)

    return SellerReportListResponse(
        total=total,
        unread_count=unread,
        no_report_reason=reason,
        items=[
            SellerReportListItem(
                report_id=str(r.id),
                trigger_type=r.trigger_type,
                period_from=r.period_from,
                period_to=r.period_to,
                title=r.title,
                summary=r.summary,
                recommendation_count=counts.get(r.id, 0),
                has_holds=bool(r.holds),
                created_at=_iso_z(r.created_at) or "",
                read_at=_iso_z(r.read_at),
            )
            for r in reports
        ],
    )


@router.get("/seller/reports/{report_id}")
async def seller_report_detail(
    report_id: str,
    identity: Identity = Depends(require_seller),
) -> dict:
    """R-2 — 판매자 분석 보고서 상세.

    본문은 `_report_payload()` 가 조립한다 — S-4 `report` 이벤트와 **같은 조립기**라
    FE `AnalysisReport.tsx` 를 무수정으로 재사용하고, 채팅 패널과 보고서 페이지가 구조적으로
    어긋날 수 없다. 여기서는 그 위에 보고서 전용 필드만 얹는다.

    응답 모델을 두지 않은 이유는 `schemas/seller_report.py` docstring 참조 — 같은 계약을
    두 곳에 선언하면 한쪽만 고쳐지는 순간 두 화면이 갈린다.
    """
    brand_id = _report_context(identity).brand_id
    try:
        report_uuid = UUID(report_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": "reportId 형식이 올바르지 않습니다."},
        ) from exc

    report = await analysis_store.get_report(report_uuid, brand_id=brand_id)
    if report is None:
        # 남의 브랜드 보고서도 같은 404 다 — 존재 여부를 구분해 알려주면 id 열거가 된다.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "REPORT_NOT_FOUND", "message": "보고서를 찾을 수 없습니다."},
        )
    # 저장 계층이 ORDER BY rank 로 주지만 명시해 둔다 — 이 순서가 화면의 "N번"이고,
    # 어긋나면 "N번 적용해줘"가 다른 추천을 조용히 적용한다.
    recs = sorted(
        await analysis_store.list_recommendations_by_report(report_uuid, brand_id=brand_id),
        key=lambda r: r.rank,
    )

    payload = _report_payload(
        report_view.record_to_pipeline_result(report, recs),
        title=report.title,
    )
    # 확정 3 — 조회 API 는 Z 표기로 통일한다(S-4 는 KST 고정이라 조립기를 못 바꾼다).
    payload["generatedAt"] = _iso_z(report.created_at)

    # limitations 는 조립기가 findings(evidence 빈 것)에서 뽑는다 — 무인 파이프라인의 판정
    # 보류는 finding 이 아니라 holds 에 쌓이므로 여기서 덧붙인다. 보류를 화면에서 지우면
    # "판정 보류 != 이상 없음" 불변 규약이 와이어에서 깨진다.
    payload["limitations"] = [
        *payload.get("limitations", []),
        *(mask_output(_strip_unsafe(h)) for h in report_view.holds_to_limitations(report.holds)),
    ]

    # 추천 — 조립기가 만든 항목(index·title·expectedEffect·actionType·productId)에 저장 측
    # 필드를 덧댄다. index 는 조립기의 목록 순서(1-base)이고 rank 는 저장값인데, 두 값이
    # 어긋나면 "N번 적용해줘"가 다른 추천을 적용한다 — record_to_pipeline_result 가 rank 로
    # 정렬해 넘기므로 zip 이 성립한다.
    for item, rec in zip(payload.get("recommendations", []), recs, strict=False):
        item.update(
            {
                "rank": rec.rank,
                "targetKind": rec.target_kind,
                "segmentLabel": rec.segment_label,
                "productIds": list(rec.product_ids),
                "rationale": rec.rationale,
                "effectivenessScore": rec.effectiveness_score,
                "status": rec.status,
                "appliedAt": _iso_z(rec.applied_at),
            }
        )

    payload.update(
        {
            "reportId": str(report.id),
            "triggerType": report.trigger_type,
            "comparedPeriod": (
                {
                    "from": report.compared_from.isoformat(),
                    "to": report.compared_to.isoformat(),
                }
                if report.compared_from and report.compared_to
                else None
            ),
            # ── 명세 별칭(확정 4) ────────────────────────────────────────
            # 이슈 #599·`01-ARCHITECTURE.md` §3 은 `reportMd`·`periodFrom/To`·
            # `comparedFrom/To`(평면)로 적혀 있고, FE `SellerReport` 는 `body`·
            # `period{from,to}`·`comparedPeriod{from,to}`(중첩)로 읽는다. 한쪽만 실으면
            # 명세를 어기거나 FE 를 고쳐야 하므로 **둘 다 싣는다** — 추천의
            # `index`/`rank` 와 같은 방침이다.
            #
            # `reportMd` 는 원본 컬럼이 아니라 `payload["body"]` 를 쓴다. 조립기가
            # mask_output + _strip_unsafe_multiline 을 적용한 값이라, 원본을 그대로
            # 실으면 같은 본문의 두 필드가 **마스킹 여부만 다르게** 나간다.
            "reportMd": payload.get("body", ""),
            "periodFrom": report.period_from.isoformat(),
            "periodTo": report.period_to.isoformat(),
            "comparedFrom": (report.compared_from.isoformat() if report.compared_from else None),
            "comparedTo": report.compared_to.isoformat() if report.compared_to else None,
            # LLM 이 쓴 llmLabel·llmDesc 가 들어 있어 조립기와 같은 정제를 건다(_mask_deep).
            "segments": _mask_deep(report_view.segments_to_wire(report.segments)),
            "holds": _mask_deep(report.holds) if isinstance(report.holds, list) else [],
            # 이번 조회로 각인되기 **직전** 값이다 — 방금 바뀐 값을 실으면 화면이
            # "안 읽음 -> 읽음" 전환을 감지할 수 없다.
            "readAt": _iso_z(report.read_at),
        }
    )

    await analysis_store.mark_report_read(report_uuid, brand_id=brand_id)
    return payload
