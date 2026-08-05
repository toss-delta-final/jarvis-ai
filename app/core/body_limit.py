"""요청 바디 크기 상한 (이슈 #299, api-spec §2.5·§2.8) — 순수 ASGI 미들웨어.

레이트 리밋(§2.8)은 요청 **건수**만 세므로 그 앞단에서 요청 본문 크기 자체를 유계로 만든다.
`BaseHTTPMiddleware`(`app.middleware("http")`)가 아니라 순수 ASGI 클래스로 구현하는 이유는 둘이다:
`Content-Length` 로 바디를 한 바이트도 읽기 전에 거절해야 하고, 헤더가 없는(chunked) 경우엔
`receive` 를 감싸 실수신 바이트를 세야 하는데 `BaseHTTPMiddleware` 로는 이 둘 다 깔끔하지 않다.

초과 응답은 계약 변경 없이 기존 400 BAD_REQUEST 매핑을 그대로 쓴다(api-spec §2.5 — 413·
PAYLOAD_TOO_LARGE 는 등재돼 있지 않고, 새 코드 도입은 정본 개정이 선행돼야 하는 계약 변경이라
이 레인 범위 밖이다).

[리뷰 2차 R-1] 이 층이 **보장하는 것**과 **보장하지 않는 것**:
  (a) `Content-Length` 헤더가 있으면 **경로와 무관하게** 다운스트림 앱 호출 **전에** 사전
      거절된다 — 라우트가 바디를 읽는지 여부와 무관하다(`GET /health` 처럼 `Request` 인자조차
      받지 않는 핸들러도 포함). 실무 클라이언트(브라우저·httpx·curl)는 스트리밍이 아닌 바디에
      항상 `Content-Length` 를 붙이므로 사실상 전부가 이 경로를 탄다.
  (b) `Content-Length` 가 없는 chunked 전송은 **다운스트림이 실제로 `receive()` 를 불러 읽은
      만큼만** 셀 수 있다 — 바디를 한 번도 읽지 않는 라우트(예: `GET /health`)에서는 chunked
      바이트를 셀 수 없다. 이것은 상한 우회가 아니라 **경계**다: 앱이 읽지 않는 바이트는
      파싱·검증 비용 자체가 발생하지 않으므로(이 이슈가 막으려던 비용은 "파싱·검증 단계
      도달"이다) 이 층이 막을 대상이 애초에 없다. 순수 네트워크 유입량(수신 바이트 자체)은
      ASGI 서버·리버스 프록시(예: nginx `client_max_body_size`) 소관이다 — 앱 미들웨어가 그걸
      세려면 읽지 않아도 될 바이트를 **우리가 대신 읽어야** 하는데, 그건 공격자가 노리는 바로
      그 비용(파싱 없이도 서버 자원을 소모시키는 것)을 이 층이 자발적으로 떠안는 셈이 된다.
  (c) CORS preflight(`OPTIONS`)는 이 층에 닿지 않는다 — `CORSMiddleware` 가 최외곽에서 바디를
      읽지 않고 단락 응답하므로(`app/main.py` 의 미들웨어 등록 순서 주석 참조) preflight 는
      비용이 없고, 이 층 이전에 이미 끝난다. 설계상 확정이며 순서를 바꾸지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import get_settings
from app.core.errors import REQUEST_ID_HEADER, error_envelope, new_request_id
from app.core.logging import get_logger
from app.core.observability import emit_rejection

logger = get_logger(__name__)

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# 초과 응답 본문의 message — errorType(BODY_TOO_LARGE)와 다르다: errorType 은 §6.3 관측용 내부
# 집계 라벨이고, 와이어 error.code 는 2-a 결정대로 기존 매핑의 BAD_REQUEST 그대로다.
_REJECT_MESSAGE = "요청 본문이 너무 큽니다"


def _parse_content_length(scope: Scope) -> int | None:
    """`Content-Length` 헤더 값을 정수로 파싱한다. 헤더 없음/비정수는 None(무시하고 통과)."""
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                # 형식 위반은 이미 h11/uvicorn 소관 — 추적 편의(이 미들웨어)가 요청을 막지 않는다.
                return None
    return None


async def _reject(
    scope: Scope,
    send: Send,
    max_bytes: int,
    *,
    content_length: int | None = None,
    received_bytes: int | None = None,
) -> None:
    """400 BAD_REQUEST 봉투를 직접 `send` 하고 관측 로그를 남긴다."""
    request_id = new_request_id()
    path = scope.get("path", "")
    body = json.dumps(
        error_envelope("BAD_REQUEST", _REJECT_MESSAGE, request_id), ensure_ascii=False
    ).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1")),
        (b"content-length", str(len(body)).encode("latin-1")),
    ]
    await send({"type": "http.response.start", "status": 400, "headers": headers})
    await send({"type": "http.response.body", "body": body})
    # errorType 은 와이어 코드가 아니라 §6.3 내부 집계 라벨이다 — 와이어 error.code 는 위와 같이
    # 항상 BAD_REQUEST (2-a). emit_rejection 은 path 를 알려진 값(allowlist, observability.py
    # safe_text_values)만 기록하고 그 밖은 폐기한다 — 이 저장소의 로그 정책이다.
    # [리뷰 3차 C-2] content_length·received_bytes 는 여기 넘기지 않는다: emit_rejection 의
    # allowlist(safe_text_values/retryable/식별자 fingerprint)에 정수 바이트 수 자리가 없어
    # 넘겨도 조용히 버려진다(실측: 구조화 로그 chat_request JSON 에 두 필드가 전혀 안 찍힘) —
    # 바이트 수는 아래 평문 logger.info 가 담당한다(역할 분담).
    emit_rejection(
        request_id,
        "BODY_TOO_LARGE",
        path=path,
    )
    # [리뷰 2차 R-3] 요청 본문 원문은 절대 로깅하지 않는다(§6.3 b) — 아래는 rid/바이트 수만.
    # `path` 는 여기 넣지 않는다: 이 미들웨어는 전 경로에 적용돼 scope["path"] 가 공격자가 통제
    # 하는 임의 문자열일 수 있는데, emit_rejection 이 이미 그 위험을 알고 path 를 allowlist로
    # 좁혀 폐기하는 정책을 갖고 있다(위 주석). 같은 사실을 이 logger.info 로 또 남기면서 그
    # 정책을 우회하는 두 번째 로그 경로를 만들지 않는다 — 정본 정책은 emit_rejection 하나다.
    logger.info(
        "request body size limit exceeded rid=%s max_bytes=%s content_length=%s received_bytes=%s",
        request_id,
        max_bytes,
        content_length,
        received_bytes,
    )


class BodySizeLimitMiddleware:
    """요청 바디가 `request_body_max_bytes` 를 넘으면 다운스트림 앱 호출 전/도중에 400 을 낸다."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 요청 시점에 읽는다(__init__ 에 박지 않는다) — app/core/ratelimit.py 가 요청마다
        # get_settings() 를 부르는 것과 같은 규약이라 테스트가 env 로 상한을 바꿀 수 있다.
        max_bytes = get_settings().request_body_max_bytes

        content_length = _parse_content_length(scope)
        if content_length is not None and content_length > max_bytes:
            await _reject(scope, send, max_bytes, content_length=content_length)
            return

        # Content-Length 가 없거나(chunked) 실제 수신량이 헤더보다 클 때를 대비해 receive 를
        # 감싸 http.request 메시지의 body 길이를 누적한다. 경계는 초과일 때만(> , >= 아님).
        received = 0
        rejected = False
        # [리뷰 2차 R-2] downstream 이 **자기 응답을 먼저 시작한 뒤** 바디를 마저 읽는 경로를
        # 위한 플래그. 지금 스택(FastAPI/Starlette)은 body 를 다 읽은 뒤에야 라우트 로직이
        # 도니 실제로 도달하지 않지만, 그 순서는 이 미들웨어가 강제하는 게 아니라 downstream
        # 구현이 우연히 지키는 관례라 1차 F-1 과 같은 종류의 잠복 결함이다 — 방어적으로 막는다.
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received, rejected
            if rejected:
                # 이미 거절 응답을 보냈다 — downstream 이 disconnect 를 무시하고 receive() 를
                # 계속 부르더라도(§F-1 리뷰) 실수신을 다시 세거나 _reject 를 재호출하지 않는다.
                # _reject 는 raw send 로 http.response.start 를 내므로, 여기서 막지 않으면 매
                # 재호출마다 누적값이 여전히 상한을 넘어 응답을 중복 전송해 ASGI 프로토콜을
                # 위반한다(다운스트림이 disconnect 를 순순히 받아들인다는 가정에만 기대던 버그).
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body") or b"")
                if received > max_bytes:
                    rejected = True
                    if response_started:
                        # [리뷰 2차 R-2] downstream 이 이미 자기 http.response.start 를 보낸
                        # 뒤라, 그 위에 우리 400 봉투를 겹쳐 보낼 자리가 없다(두 번째
                        # http.response.start 는 ASGI 프로토콜 위반). 봉투를 포기하고 그냥
                        # disconnect 만 돌려 downstream 을 멈춘다. 정상 스택에서는 도달하지
                        # 않아야 하는 경로라 warning 으로 남긴다.
                        logger.warning(
                            "body limit exceeded after downstream already started its own "
                            "response — skipping 400 envelope, disconnecting only"
                        )
                        return {"type": "http.disconnect"}
                    await _reject(scope, send, max_bytes, received_bytes=received)
                    # downstream 이 멈추도록 disconnect 를 돌려준다.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if rejected:
                # 이미 응답을 시작했다 — downstream 이 그 뒤 보내는 http.response.* 는 ASGI
                # 프로토콜 위반·이중 응답을 막기 위해 삼킨다.
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception as exc:
            # rejected 상태에서 downstream 이 http.disconnect 수신 후 던지는 예외(ClientDisconnect
            # 등, 정상 흐름의 일부)는 이미 응답을 보냈으므로 삼킨다. 그 외 예외는 그대로 전파한다.
            if not rejected:
                raise
            # 완전 무음으로 삼키면 그 자리에 섞여 들어온 진짜 버그도 함께 증발한다(응답은 이미
            # 나갔으니 증상도 안 보인다) — 타입 이름만 남긴다. exc_info·str(exc) 는 쓰지 않는다:
            # 예외 메시지에 요청 본문 일부가 실릴 수 있어(§6.3 b 본문 로깅 금지) 위반 소지가
            # 있다. [리뷰 3차 C-1] warning 을 쓴다: 이 앱은 configure_logging() 을 인자 없이
            # 불러 루트 로거가 INFO 라(app/core/logging.py, app/main.py create_app()) debug 는
            # 운영에서 한 줄도 안 나온다. 실측(raw ASGI 흉내가 아니라 TestClient 로 실제
            # FastAPI 스택에 오버사이즈 /chat 요청을 쏨): 이 except 분기는 발동하지 않았다 —
            # FastAPI 가 `await request.body()` 를 자체 try/except 로 감싸 disconnect 를
            # HTTPException(400) 으로 바꾸고, 그 400 은 guarded_send 가 삼켜 예외가 이
            # 미들웨어까지 올라오지 않는다. 즉 여기 걸리는 예외는 정상 거절 경로가 아닌
            # 예상 밖 사건이라 warning 이 맞고, 운영 잡음도 나지 않는다.
            logger.warning(
                "downstream exception swallowed after body-limit rejection type=%s",
                type(exc).__name__,
            )
