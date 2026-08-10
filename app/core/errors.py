"""스트림 전(前) 오류 봉투 (api-spec §2.5) + requestId 부여.

비스트리밍 응답·SSE 스트림 시작 전 거부(인증·검증·409·429·504 등)의 오류는
{"error": {code, message, requestId}} 봉투로 낸다. 스트림 **내부** 오류는 §3.1
in-stream `error` 이벤트로 별개다(이 모듈 소관 아님).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agents.profile.graph_errors import (
    GraphEdgeNotEditable,
    GraphEdgeNotFound,
    GraphMutationError,
    GraphObjectUnknown,
    GraphStoreUnavailable,
    GraphVersionConflict,
)
from app.core.logging import get_logger
from app.core.session_context import (
    SessionActive,
    SessionClaimConflict,
    SessionContextError,
    SessionFinalizing,
    SessionForbidden,
    SessionStateUnavailable,
)

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"

# api-spec §2.5 [확정] 스트림 전 상태 코드 매핑. detail dict 의 code 가 우선한다.
_STATUS_CODE_MAP: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "TOKEN_INVALID",
    403: "FORBIDDEN",
    # [#360] §2.5 가 등재한 기본 코드. edge 케이스는 `_GRAPH_ERROR_MAP` 이 덮는다 — 이 값은
    # 그 핸들러를 안 타는 다른 404 의 안전망이다. `412` 는 채택하지 않았으므로 채우지 않는다.
    404: "NOT_FOUND",
    # ⚠️ 이 기본값은 채팅 표면(§2.9 a)의 것이다 — 그래프 표면은 전용 핸들러가 덮는다.
    409: "STREAM_IN_PROGRESS",
    429: "RATE_LIMITED",
    500: "INTERNAL",
    # [#148] I-22 실패 응답표(§3.7) — 내부 의존성 일시 장애. 5xx 라 detail 은 무시되고
    # 이 맵의 코드·고정 메시지가 나간다(업스트림 문자열 유출 방지).
    503: "UPSTREAM_UNAVAILABLE",
    504: "UPSTREAM_TIMEOUT",
}
_DEFAULT_MESSAGE: dict[int, str] = {
    400: "요청 본문/파라미터 오류",
    401: "인증 실패",
    403: "권한 없음",
    409: "동일 방에 진행 중인 스트림이 있습니다",
    429: "요청이 너무 많습니다",
    500: "서버 내부 오류",
    503: "상류(LLM/저장소) 일시 이용 불가",
    504: "상류(LLM/Spring) 응답 지연",
}

# api-spec §3.9 실패표와 1:1 (#360). `graph_errors` 상단 매핑표의 실제 구현이다.
#
# **라우터는 도메인 예외만 던진다.** 상태 코드·code 를 라우터가 매번 기억하게 하면 한 곳만
# 빠뜨려도 계약이 깨지는데, 그 결함은 **정상 경로 테스트로 잡히지 않는다** — 특히 `409` 의
# 기본 코드가 `STREAM_IN_PROGRESS` 라 코드 지정을 빠뜨리면 FE 에 "스트림 진행 중"이 나간다
# (§3.9 구현 노트 1). 누락은 `tests/unit/test_graph_error_mapping.py` 가 잡는다.
_GRAPH_ERROR_MAP: dict[type[GraphMutationError], tuple[int, str, str]] = {
    GraphObjectUnknown: (400, "BAD_REQUEST", "요청한 대상을 확인할 수 없습니다"),
    # **남의 edge 든 미존재든 동일 응답**이어야 열거가 막힌다 — 메시지에 식별자를 싣지 않는다.
    GraphEdgeNotFound: (404, "PROFILE_EDGE_NOT_FOUND", "대상 취향 항목을 찾을 수 없습니다"),
    GraphVersionConflict: (
        409,
        "PROFILE_VERSION_CONFLICT",
        "프로필이 그 사이에 변경되었습니다. 다시 조회한 뒤 시도해 주세요.",
    ),
    GraphEdgeNotEditable: (
        409,
        "PROFILE_EDGE_NOT_EDITABLE",
        "구매 기록에서 만들어진 항목은 수정할 수 없습니다.",
    ),
    GraphStoreUnavailable: (503, "UPSTREAM_UNAVAILABLE", "상류(저장소) 일시 이용 불가"),
}

_SESSION_ERROR_MAP: dict[type[SessionContextError], tuple[int, str, str]] = {
    SessionForbidden: (403, "SESSION_FORBIDDEN", "session access denied"),
    SessionFinalizing: (409, "SESSION_FINALIZING", "session cleanup is in progress"),
    SessionActive: (409, "SESSION_ACTIVE", "session is active"),
    SessionClaimConflict: (409, "SESSION_CLAIM_CONFLICT", "session claim conflicts"),
    SessionStateUnavailable: (503, "STATE_UNAVAILABLE", "session state is unavailable"),
}


def new_request_id() -> str:
    """추적용 요청 식별자 생성 (로그 상관관계)."""
    return uuid.uuid4().hex


def get_request_id(request: Request) -> str:
    """요청 컨텍스트의 requestId 를 반환한다 (미들웨어 미적용 시 즉석 생성)."""
    rid = getattr(request.state, "request_id", None)
    if not rid:
        rid = new_request_id()
        request.state.request_id = rid
    return rid


def error_envelope(
    code: str, message: str, request_id: str, *, detail: dict[str, Any] | None = None
) -> dict[str, Any]:
    """§2.5 오류 봉투 dict.

    **[HARD] 확장은 `error.detail`(중첩 object)로만 한다** — `error` 와 나란한 최상위 형제 필드를
    추가하지 않는다(§2.5). 현행 용례는 `PROFILE_VERSION_CONFLICT` 의 `graphVersion`(§3.9)이고,
    Spring 은 이 위치를 그대로 유지해 FE 에 전달한다.

    `detail` 은 **실을 것이 있을 때만** 붙는다 — 빈 객체를 습관적으로 내보내면 소비자가 존재
    여부로 분기할 수 없다. 키워드 전용이라 기존 호출부(위치 인자 3개)는 그대로 동작한다.
    """
    body: dict[str, Any] = {"code": code, "message": message, "requestId": request_id}
    if detail is not None:
        body["detail"] = detail
    return {"error": body}


def _resolve(status_code: int, detail: Any) -> tuple[str, str]:
    """(status, detail) → (code, 안전 메시지). detail 의 code/message 가 매핑을 덮어쓴다."""
    code = _STATUS_CODE_MAP.get(status_code, "ERROR")
    message = _DEFAULT_MESSAGE.get(status_code, "오류가 발생했습니다")
    if status_code >= 500:
        # 5xx 는 detail(내부 오류 메시지/PII 가능)을 무시하고 항상 고정 안전 메시지를 쓴다.
        return code, message
    if isinstance(detail, dict):
        code = str(detail.get("code", code))
        message = str(detail.get("message", message))
    elif isinstance(detail, str) and detail:
        message = detail
    return code, message


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """HTTPException → §2.5 봉투. 내부 스택/PII 미포함, requestId 부여."""
    code, message = _resolve(exc.status_code, exc.detail)
    rid = get_request_id(request)
    if exc.status_code >= 500:
        logger.error("unhandled http error status=%s code=%s rid=%s", exc.status_code, code, rid)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_envelope(code, message, rid),
        headers={REQUEST_ID_HEADER: rid},
    )


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """요청 본문/파라미터 검증 실패 → 400 BAD_REQUEST 봉투 (상세 필드는 미노출)."""
    rid = get_request_id(request)
    return JSONResponse(
        status_code=400,
        content=error_envelope("BAD_REQUEST", _DEFAULT_MESSAGE[400], rid),
        headers={REQUEST_ID_HEADER: rid},
    )


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """라우터/그래프에서 처리되지 않은 예외(500) → §2.5 봉투. 내부 스택/PII 미노출."""
    rid = get_request_id(request)
    logger.exception("unhandled exception rid=%s", rid)
    return JSONResponse(
        status_code=500,
        content=error_envelope("INTERNAL", "서버 내부 오류", rid),
        headers={REQUEST_ID_HEADER: rid},
    )


async def _session_context_exception_handler(
    request: Request, exc: SessionContextError
) -> JSONResponse:
    """Expected lifecycle rejections use stable public codes without leaking state."""
    status_code, code, message = _SESSION_ERROR_MAP[type(exc)]
    rid = get_request_id(request)
    return JSONResponse(
        status_code=status_code,
        content=error_envelope(code, message, rid),
        headers={REQUEST_ID_HEADER: rid},
    )


async def _graph_mutation_exception_handler(
    request: Request, exc: GraphMutationError
) -> JSONResponse:
    """그래프 변경 도메인 예외 → §3.9 실패표 (#360).

    라우터가 상태 코드를 몰라도 되게 하는 자리다. `PROFILE_VERSION_CONFLICT` 만 최신 버전을
    `error.detail.graphVersion` 에 싣는다 — 그게 없으면 FE 가 재조회 없이 재시도할 수 없다.

    **메시지는 맵의 고정 문구다.** 예외 문자열을 그대로 쓰면 `edgeId` 가 응답으로 새어 나가
    "남의 edge 든 미존재든 동일 응답"(열거 방지)이 깨진다.
    """
    status_code, code, message = _GRAPH_ERROR_MAP[type(exc)]
    rid = get_request_id(request)
    detail = (
        {"graphVersion": exc.latest_graph_version}
        if isinstance(exc, GraphVersionConflict)
        else None
    )
    return JSONResponse(
        status_code=status_code,
        content=error_envelope(code, message, rid, detail=detail),
        headers={REQUEST_ID_HEADER: rid},
    )


async def request_context_middleware(request: Request, call_next: Any) -> Any:
    """요청마다 requestId 를 부여하고 응답 헤더로 노출한다 (로그 상관관계)."""
    rid = new_request_id()
    request.state.request_id = rid
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = rid
    return response


def install_error_handling(app: FastAPI) -> None:
    """예외 핸들러 + requestId 미들웨어를 앱에 등록한다."""
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(SessionContextError, _session_context_exception_handler)
    app.add_exception_handler(GraphMutationError, _graph_mutation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
    app.middleware("http")(request_context_middleware)
