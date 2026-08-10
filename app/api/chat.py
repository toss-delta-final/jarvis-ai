"""구매자 챗봇 엔드포인트 — POST /chat (SSE 스트리밍, FE 직접).

buyer 그래프(SPEC-RECOMMEND-001)를 open_stream 으로 감싸 스트리밍한다. SSE 이벤트명·필드는
api-spec §3.1(camelCase)과 일치하며, 상품 카드는 싣지 않는다(경로 B) —
products.ready 는 {sessionId, listIds} 상관키만 나른다.

스트림 수명주기(§2.9 동시 스트림 409·취소·전체/first-token 타임아웃)는 open_stream 이,
레이트 리밋(§2.8)·오류 봉투(§2.5)는 app.main 미들웨어·핸들러가 담당한다. 대화 저장·구조화
로그(§6.3)는 observation(start_observation)이 open_stream 훅으로 붙는다.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.agents.buyer.graph import run_buyer_turn
from app.api import deps as auth_deps
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.conversation import TurnStatus, get_conversation_store
from app.core.errors import get_request_id
from app.core.observability import emit_rejection, finish_trace_safely, start_observation
from app.core.pg_resilience import is_state_store_unavailable
from app.core.session_context import BuyerSessionInput, SessionStateUnavailable
from app.core.stream import open_stream, registry_key
from app.core.tracing import start_request_trace_safely
from app.schemas.chat import BuyerChatRequest

router = APIRouter(tags=["chat"])


@router.post("/chat")
async def chat(
    request: BuyerChatRequest,
    http_request: Request,
    identity: Identity = Depends(auth_deps.get_identity),
) -> StreamingResponse:
    """구매자 챗봇 SSE 스트리밍 (api-spec §3.1)."""
    settings = auth_deps.get_settings()
    auth_deps.require_buyer_session(identity, request.session_id, settings)
    buyer_session = BuyerSessionInput(
        session_id=request.session_id,
        thread_id=request.thread_id,
        owner_type="guest" if identity.is_guest else "member",
        owner_id=auth_deps.buyer_owner_id(identity, settings),
    )
    request_id = get_request_id(http_request)
    trace = start_request_trace_safely(
        name="buyer_chat_turn",
        request_id=request_id,
        conversation_id=request.session_id,
        thread_id=request.thread_id,
        lane="buyer",
        environment=get_settings().app_environment,
    )
    # [#326] 콘텐츠 추적 모드에서만 발화 원문이 루트 span 에 실린다(off 면 no-op).
    # [#469] 칩 제거 턴은 발화 없이 conditionActions 만 올 수 있어 함께 싣는다 — 키가
    # _LLM_CONTENT_KEYS 밖이라 strict(전체 카나리아) 검증을 받는다. getattr 인 이유:
    # 필드는 BuyerChatRequest 소유라, 기반 ChatRequest 로 직접 호출하는 테스트 더블을 깨지 않는다.
    condition_actions = getattr(request, "condition_actions", None) or []
    trace.record_request_content(
        input_text=request.message,
        extra_inputs=(
            {
                "conditionActions": json.dumps(
                    # [이슈 #434] exclude_none=True — value 없는 기존 요청의 트레이스 직렬화가
                    # 오늘과 바이트 동일하게 유지된다("value": null 이 새로 붙지 않는다).
                    [
                        action.model_dump(by_alias=True, exclude_none=True)
                        for action in condition_actions
                    ],
                    ensure_ascii=False,
                )
            }
            if condition_actions
            else None
        ),
    )
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
        # get_conversation_store()(pg-profile 지연 연결)는 운영(jwks)에서 폴백 없이 raise 한다.
        # 이 호출은 start_observation 인자라 open_stream 안전망 밖 — 예외가 나면 observation 이
        # 없어 §6.3 b chat_request 로그(errorType 집계)가 통째로 빠진다. pg-profile 장애야말로
        # 관측이 필요하므로 rejection 로그를 남기고 그대로 전파한다(§2.5 봉투, PR #48 후속 리뷰).
        emit_rejection(
            request_id,
            "INTERNAL",
            conversationId=request.session_id,
            threadId=request.thread_id,
        )
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
        buyer_session=buyer_session,
    )
    return await open_stream(
        http_request,
        registry_key(identity, request.thread_id),
        lambda turn_started_at: run_buyer_turn(
            request,
            identity,
            observer=observation,
            request_id=request_id,
            turn_started_at=turn_started_at,
        ),
        observer=observation,
        role="buyer",
    )
