"""app/api/seller.py SSE 1차 배선 검증 (3-7) — 실 LLM·HTTP 서버 없음.

_general_stream 제너레이터를 직접 소비한다(스텁 에이전트 주입). SSE 와이어 포맷
(data: {"type": ..., "data": {...}}\n\n)과 이벤트 순서·마스킹·오류 매핑을 검증한다.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import types

import pytest
from langchain_core.messages import AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.seller import hitl, period
from app.api import seller as seller_api
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.llm import LLMNotConfigured
from app.core.logging import safe_fingerprint
from app.schemas.seller import SellerChatRequest

_IDENTITY = Identity(user_id=None, is_guest=False, seller_id="7", brand_id="3")


@pytest.fixture(autouse=True)
def _hitl_memory_checkpointer():
    """4-2: product/confirm 레인이 hitl 그래프를 쓰므로 PG 연결 없이 InMemory 주입."""
    hitl.set_checkpointer(InMemorySaver())
    yield
    hitl.set_checkpointer(None)


def _request(message: str) -> SellerChatRequest:
    return SellerChatRequest(session_id="s-1", thread_id="t-1", message=message)


def _confirm_request(draft_id: str) -> SellerChatRequest:
    """A-2: 승인 요청 — 최상위 action/draftId 구조화 필드(message 는 비운다)."""
    return SellerChatRequest(
        session_id="s-1", thread_id="t-1", message="", action="confirm", draft_id=draft_id
    )


async def test_seller_endpoint_scopes_stream_lock_by_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """판매자 엔드포인트도 sessionId가 아니라 threadId를 스트림 락 키로 사용한다."""
    captured: dict[str, object] = {}
    marker = object()

    async def _capture_open_stream(_request, stream_key, _factory, *, observer=None, role=None):
        captured["stream_key"] = stream_key
        captured["observer"] = observer
        captured["role"] = role
        return marker

    monkeypatch.setattr(seller_api, "open_stream", _capture_open_stream)
    identity = Identity(
        user_id="7",
        is_guest=False,
        seller_id="7",
        brand_id="3",
        subject="7",
    )
    http_request = types.SimpleNamespace(state=types.SimpleNamespace(request_id="req-seller"))

    response = await seller_api.seller_chat(
        SellerChatRequest(
            session_id="shared-session", thread_id="seller-room", message="매출 알려줘"
        ),
        http_request,
        identity,
    )

    assert response is marker
    assert identity.session_id is None, "판매자 티켓에는 구매자 sessionId claim을 요구하지 않는다"
    assert captured["stream_key"] == "7:seller-room"
    assert captured["observer"].buyer_session is None
    assert captured["role"] == "seller"


class _StubStreamAgent:
    """astream 만 흉내 — (AIMessageChunk, metadata) 튜플을 순서대로 방출한다."""

    def __init__(self, chunks: list[object], exc: Exception | None = None) -> None:
        self._chunks = chunks
        self._exc = exc
        # [#346] 기간 주입 검증용 — 입력 메시지를 그대로 보관한다.
        self.seen_input: dict | None = None

    async def astream(
        self,
        _input: dict,
        config: dict | None = None,
        context: object = None,
        stream_mode: str = "",
    ):
        self.seen_input = _input
        for chunk in self._chunks:
            yield (chunk, {"langgraph_node": "model"})
        if self._exc is not None:
            raise self._exc


def _collect(request: SellerChatRequest) -> list[dict]:
    """스트림을 전부 소비해 SSE 페이로드(dict) 목록으로 파싱한다."""

    async def run() -> list[str]:
        return [line async for line in seller_api._general_stream(request, _IDENTITY)]

    lines = asyncio.run(run())
    payloads = []
    for line in lines:
        assert line.startswith("data: ") and line.endswith("\n\n")  # SSE 와이어 규약
        payloads.append(json.loads(line[len("data: ") :]))
    return payloads


def test_stream_tokens_then_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """모델 청크 → token(text 증분) 순서 보존 → 마지막은 done(stop)."""
    agent = _StubStreamAgent(
        [
            AIMessageChunk(content="지난달 매출은 "),
            AIMessageChunk(content="1,200,000원입니다."),
        ]
    )
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("지난달 매출 알려줘"))

    assert [e["type"] for e in events] == ["meta", "token", "token", "done"]
    assert events[0]["data"]["lane"] == "general"  # 첫 프레임 = 레인(B)
    assert events[1]["data"]["text"] == "지난달 매출은 "
    assert events[-1]["data"]["finishReason"] == "stop"  # CamelModel by_alias
    assert events[-1]["data"]["panel"] == "keep"  # 대화 = 패널 유지


def test_stream_sanitizes_chunks_without_losing_boundary_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM 청크는 위험 문자를 제거하되 청크 경계의 정상 공백을 보존한다."""
    agent = _StubStreamAgent(
        [
            AIMessageChunk(content="지난달\x1b[31m 매출은 \u200b"),
            AIMessageChunk(content="\u202e1,200,000원\n입니다."),
        ]
    )
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("지난달 매출 알려줘"))

    text = "".join(e["data"]["text"] for e in events if e["type"] == "token")
    assert text == "지난달[31m 매출은 1,200,000원\n입니다."
    assert all(ch not in text for ch in ("\x1b", "\u200b", "\u202e"))


def test_stream_skips_tool_use_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """tool_use 블록(도구 호출 인자)은 사용자 스트림에 흘리지 않는다."""
    agent = _StubStreamAgent(
        [
            AIMessageChunk(content=[{"type": "tool_use", "name": "get_sales_timeseries"}]),
            AIMessageChunk(content=[{"type": "text", "text": "조회 결과입니다."}]),
        ]
    )
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("매출 조회"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[1]["data"]["text"] == "조회 결과입니다."


def test_stream_masks_output_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """출력 검사(§10-⑥) — 청크에 섞인 시크릿 패턴이 마스킹되어 나간다."""
    agent = _StubStreamAgent([AIMessageChunk(content="키는 sk-abcdefghijklmnop1234 입니다")])
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("설정 알려줘"))

    assert "sk-abcdefghijklmnop1234" not in events[1]["data"]["text"]
    assert "[민감한 정보라 가려드렸어요]" in events[1]["data"]["text"]


def test_stream_masks_secret_obfuscated_with_unsafe_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """위험 문자로 시크릿 정규식을 우회해도 정제 후 마스킹되어야 한다."""
    agent = _StubStreamAgent([AIMessageChunk(content="키는 sk-abcdefgh\u200bijklmnop1234 입니다")])
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("설정 알려줘"))

    text = "".join(e["data"]["text"] for e in events if e["type"] == "token")
    assert "sk-abcdefghijklmnop1234" not in text
    assert "[민감한 정보라 가려드렸어요]" in text


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        (
            ["키는 Bearer abcdefgh", "\ufe0fijklmnop", "1234 입니다"],
            "키는 [민감한 정보라 가려드렸어요] 입니다",
        ),
        (["값은 sk-abcdef", "ghijklmnop1234 끝"], "값은 [민감한 정보라 가려드렸어요] 끝"),
        (["번호는 990101-", "1234567 입니다"], "번호는 [민감한 정보라 가려드렸어요] 입니다"),
    ],
)
def test_stream_masks_secrets_split_across_chunks(
    monkeypatch: pytest.MonkeyPatch, chunks: list[str], expected: str
) -> None:
    """최소 길이 전에서 분할된 시크릿도 조각을 노출하지 않고 한 번 마스킹한다."""
    agent = _StubStreamAgent([AIMessageChunk(content=chunk) for chunk in chunks])
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("설정 알려줘"))

    text = "".join(event["data"]["text"] for event in events if event["type"] == "token")
    assert text == expected


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        (["좋아 ❤", "\ufe0f 입니다"], "좋아 ❤️ 입니다"),
        (["한자 㐂", "\U000e0100 입니다"], "한자 㐂\U000e0100 입니다"),
        (
            [
                "국기 🏴\U000e0067\U000e0062",
                "\U000e0065\U000e006e\U000e0067\U000e007f 입니다",
            ],
            "국기 🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f 입니다",
        ),
        (["비지원 🏴\U000e0075", "\U000e0073 입니다"], "비지원 🏴 입니다"),
    ],
)
def test_stream_sanitizes_unicode_sequences_split_across_chunks(
    monkeypatch: pytest.MonkeyPatch, chunks: list[str], expected: str
) -> None:
    """청크 경계의 등록 시퀀스는 보존하고 비지원 Tag payload는 제거한다."""
    agent = _StubStreamAgent([AIMessageChunk(content=chunk) for chunk in chunks])
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("표시해줘"))

    text = "".join(event["data"]["text"] for event in events if event["type"] == "token")
    assert text == expected


def test_stream_scope_refusal_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """scope 위반 → 에이전트 미빌드(LLM 0회), 거절 token + done."""

    def _fail_build(today: str, checkpointer: object = None):
        raise AssertionError("scope 차단 시 에이전트를 빌드하면 안 된다")

    monkeypatch.setattr(seller_api, "build_general_agent", _fail_build)

    events = _collect(_request("경쟁사 매출 알려줘"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert "도와드리기 어려운 영역" in events[1]["data"]["text"]


def test_stream_error_event_on_build_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """에이전트 빌드 실패도 error 이벤트 봉투로 종료 — 무봉투 파손 금지(마감 리뷰 M2)."""

    def _boom(today: str, checkpointer: object = None):
        raise RuntimeError("settings broken")

    monkeypatch.setattr(seller_api, "build_general_agent", _boom)

    events = _collect(_request("매출 알려줘"))

    assert [e["type"] for e in events] == ["meta", "error"]
    assert events[1]["data"]["code"] == "INTERNAL"


def test_stream_model_not_configured_maps_to_llm_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """활성 provider 키 누락은 일반 INTERNAL이 아니라 LLM_UNAVAILABLE이다."""

    def _not_configured(today: str, checkpointer: object = None):
        raise LLMNotConfigured("openai key missing")

    monkeypatch.setattr(seller_api, "build_general_agent", _not_configured)

    with caplog.at_level(logging.ERROR, logger="app.api.seller"):
        events = _collect(_request("매출 알려줘"))

    assert [event["type"] for event in events] == ["meta", "error"]
    assert events[-1]["data"]["code"] == "LLM_UNAVAILABLE"
    assert events[-1]["data"]["requestId"]
    assert events[-1]["data"]["retryable"] is False
    assert '"action": "general"' in caplog.text
    assert '"errorCode": "LLM_UNAVAILABLE"' in caplog.text
    assert safe_fingerprint("t-1") in caplog.text
    assert "thread=t-1" not in caplog.text
    assert "openai key missing" not in caplog.text


def test_stream_error_event_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """스트림 내부 예외 → error 이벤트(INTERNAL)로 종료(§2.7 — 봉투 아님)."""
    agent = _StubStreamAgent([AIMessageChunk(content="일부 ")], exc=RuntimeError("boom"))
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("매출 알려줘"))

    assert events[0]["type"] == "meta"
    assert events[1]["type"] == "token"
    assert events[-1]["type"] == "error"
    assert events[-1]["data"]["code"] == "INTERNAL"
    assert events[-1]["data"]["requestId"]
    assert events[-1]["data"]["retryable"] is True


# ── #266 P1: general 레인 벽시계 상한 + 타임아웃 오류 매핑 ────────────────────


class _SlowStreamAgent:
    """청크 **사이**를 길게 끄는 에이전트.

    SDK 의 timeout= 이 못 잡는 형태를 재현한다 — 첫 토큰은 즉시 오므로 read 간격
    기반 상한은 발동하지 않고, 늘어지는 것은 스트림 전체 벽시계뿐이다.
    """

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s

    async def astream(
        self,
        _input: dict,
        config: dict | None = None,
        context: object = None,
        stream_mode: str = "",
    ):
        yield (AIMessageChunk(content="집계 중 "), {"langgraph_node": "model"})
        await asyncio.sleep(self._delay_s)
        yield (AIMessageChunk(content="완료했습니다."), {"langgraph_node": "model"})


def _settings_with(**overrides):
    """현재 Settings 를 복제해 일부 값만 덮는다(env·lru_cache 오염 없음)."""
    from app.core.config import get_settings

    return get_settings().model_copy(update=overrides)


def test_general_stream_wall_clock_timeout_maps_to_llm_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T-1 — general 레인이 상한을 넘기면 INTERNAL 이 아니라 LLM_TIMEOUT 이다.

    상한 도입 전에는 이 레인에 앱 시계가 없어 스트림 전체 90s 에만 의존했고,
    지연은 어떤 error 코드도 만들지 못했다(#266 P1).
    """
    monkeypatch.setattr(
        seller_api, "get_settings", lambda: _settings_with(seller_general_timeout_s=0.05)
    )
    monkeypatch.setattr(
        seller_api,
        "build_general_agent",
        lambda today, checkpointer=None: _SlowStreamAgent(delay_s=1.0),
    )

    with caplog.at_level(logging.WARNING, logger="app.api.seller"):
        events = _collect(_request("지난달 매출 알려줘"))

    assert [event["type"] for event in events] == ["meta", "token", "error"]
    assert events[-1]["data"]["code"] == "LLM_TIMEOUT"
    assert events[-1]["data"]["requestId"]
    assert events[-1]["data"]["retryable"] is True
    assert '"errorCode": "LLM_TIMEOUT"' in caplog.text
    assert '"action": "general"' in caplog.text


def test_general_stream_sdk_timeout_maps_to_llm_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """T-2 — SDK 타임아웃은 TimeoutError 서브클래스가 아니지만 LLM_TIMEOUT 이어야 한다.

    빈 메시지를 쓰는 이유: 문자열 매칭으로 판정했다면 여기서 INTERNAL 로 새는데,
    그 오분류가 이 이슈의 P1 파생 증상이었다.
    """
    import httpx

    agent = _StubStreamAgent([AIMessageChunk(content="일부 ")], exc=httpx.ReadTimeout(""))
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("매출 알려줘"))

    assert [event["type"] for event in events] == ["meta", "token", "error"]
    assert events[-1]["data"]["code"] == "LLM_TIMEOUT"
    assert events[-1]["data"]["retryable"] is True


def test_checkpointer_timeout_is_not_reported_as_llm_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#266 PR 리뷰 — pg-profile 체크포인터 장애를 LLM 지연으로 감추지 않는다.

    get_checkpointer 는 자체 wait_for 상한을 갖고 운영에서는 폴백 없이 raise 한다. 그때
    나오는 것은 asyncio.TimeoutError(= TimeoutError)라 is_timeout_error 가 **타입만으로**
    참으로 판정한다 — 발생 지점으로 구분하지 않으면 인프라 장애가 LLM_TIMEOUT 으로 나간다.
    """

    async def _connect_timeout():
        raise TimeoutError  # get_checkpointer 의 asyncio.wait_for 가 내는 것과 같은 타입

    monkeypatch.setattr(seller_api, "get_checkpointer", _connect_timeout)
    monkeypatch.setattr(
        seller_api,
        "build_general_agent",
        lambda today, checkpointer=None: pytest.fail(
            "체크포인터 실패 시 에이전트를 빌드하면 안 된다"
        ),
    )

    with caplog.at_level(logging.ERROR, logger="app.api.seller"):
        events = _collect(_request("매출 알려줘"))

    assert [event["type"] for event in events] == ["meta", "error"]
    assert events[-1]["data"]["code"] == "INTERNAL", "인프라 장애가 LLM_TIMEOUT 으로 나가면 안 된다"
    assert '"event": "seller_checkpointer_unavailable"' in caplog.text
    assert "seller_stream_timeout" not in caplog.text


def test_checkpoint_io_failure_during_stream_is_internal_not_llm_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#266 3차 리뷰 — 스트림 **도중**의 체크포인트 I/O 장애도 LLM_TIMEOUT 이 아니다.

    최초 연결 이후 pg-profile 이 죽으면 그 예외는 general 상한 **안**에서 올라온다.
    psycopg 의 `QueryCanceled`(statement_timeout)·`PoolTimeout` 은 이름과 달리
    `OperationalError` 계열이라 `TimeoutError` 가 아니고, 그래서 `is_timeout_error` 가
    False 를 내 INTERNAL 로 분류된다 — 이 성질에 의존하므로 회귀로 고정한다.
    """
    from psycopg.errors import QueryCanceled

    agent = _StubStreamAgent([AIMessageChunk(content="집계 ")], exc=QueryCanceled("canceled"))
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("매출 알려줘"))

    assert [event["type"] for event in events] == ["meta", "token", "error"]
    assert events[-1]["data"]["code"] == "INTERNAL", "pg 오류가 LLM_TIMEOUT 으로 나가면 안 된다"


def test_general_stream_timeout_budget_is_config_driven(monkeypatch: pytest.MonkeyPatch) -> None:
    """설정값을 바꾸면 **동작이 실제로 달라지는지** 확인한다.

    lessons.md 2026-08-03 「튜너블을 추가하고 배선하지 않으면 초록불인데 동작은 안 바뀐다」 —
    값 유효성만 검사하면 배선 누락을 구조적으로 놓친다.
    """
    monkeypatch.setattr(
        seller_api,
        "build_general_agent",
        lambda today, checkpointer=None: _SlowStreamAgent(delay_s=0.2),
    )

    monkeypatch.setattr(
        seller_api, "get_settings", lambda: _settings_with(seller_general_timeout_s=0.02)
    )
    tight = _collect(_request("매출 알려줘"))

    monkeypatch.setattr(
        seller_api, "get_settings", lambda: _settings_with(seller_general_timeout_s=10.0)
    )
    loose = _collect(_request("매출 알려줘"))

    assert tight[-1]["type"] == "error" and tight[-1]["data"]["code"] == "LLM_TIMEOUT"
    assert loose[-1]["type"] == "done" and loose[-1]["data"]["finishReason"] == "stop"


# ── 4-1b: _seller_stream 3분기 디스패치 ──────────────────────────────────────


def _collect_seller(
    request: SellerChatRequest,
    identity: Identity = _IDENTITY,
) -> list[dict]:
    """_seller_stream(통합 입구)을 전부 소비해 SSE 페이로드 목록으로 파싱한다."""

    async def run() -> list[str]:
        return [line async for line in seller_api._seller_stream(request, identity)]

    lines = asyncio.run(run())
    payloads = []
    for line in lines:
        assert line.startswith("data: ") and line.endswith("\n\n")
        payloads.append(json.loads(line[len("data: ") :]))
    return payloads


def test_non_numeric_seller_identity_error_is_not_retryable() -> None:
    """토큰 발급 결함인 비숫자 판매자 클레임은 같은 요청 재시도로 복구되지 않는다."""
    malformed_identity = Identity(
        user_id=None,
        is_guest=False,
        seller_id="not-a-number",
        brand_id="3",
    )

    events = _collect_seller(_request("매출 알려줘"), malformed_identity)

    assert [event["type"] for event in events] == ["error"]
    assert events[0]["data"]["code"] == "INTERNAL"
    assert events[0]["data"]["retryable"] is False


def _route_stub(category: str, confidence: float = 0.9):
    from app.agents.seller.schemas import RouteDecision

    async def stub(question, context, recent_turns=(), screen=None):
        return RouteDecision(category=category, reason="stub", confidence=confidence)

    return stub


def _no_route(question, context, recent_turns=(), screen=None):
    raise AssertionError("이 경로에서는 라우팅(LLM)을 호출하면 안 된다")


def test_confirm_message_short_circuits_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """① confirm 선판정 — 라우팅·LLM 없이 confirm 레인(4-2)으로 위임된다.

    미존재 draftId 는 not_found 안내 token + done (hitl.confirm_draft 코드 판정).
    """
    monkeypatch.setattr(seller_api, "route_question", _no_route)

    events = _collect_seller(_confirm_request("d-1"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[0]["data"]["lane"] == "confirm"
    assert "찾지 못했어요" in events[1]["data"]["text"]
    assert events[-1]["data"]["panel"] == "keep"  # 미존재 = 변경 없음


def test_confirm_executed_result_streams_token_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """confirm 레인 — 실행 결과 text 가 그대로 token 으로 나간다(LLM 0회)."""
    monkeypatch.setattr(seller_api, "route_question", _no_route)

    async def fake_confirm(draft_id, *, seller_id, brand_id):
        assert (draft_id, seller_id, brand_id) == ("d-9", 7, 3)  # Identity → int 캐스팅
        return hitl.ConfirmOutcome("executed", "변경을 반영했습니다 (productId=101).")

    monkeypatch.setattr(seller_api, "confirm_draft", fake_confirm)

    events = _collect_seller(_confirm_request("d-9"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert "반영했습니다" in events[1]["data"]["text"]
    assert events[-1]["data"]["panel"] == "refresh"  # 실제 쓰기 → 우측 재조회


def test_confirm_output_is_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    """confirm 결과 text 도 다른 레인처럼 mask_output 을 거친다(리뷰 반영 — 마스킹 우회 차단)."""
    monkeypatch.setattr(seller_api, "route_question", _no_route)

    async def fake_confirm(draft_id, *, seller_id, brand_id):
        return hitl.ConfirmOutcome("executed", "반영 완료. 키는 sk-abcdefghijklmnop1234 입니다")

    monkeypatch.setattr(seller_api, "confirm_draft", fake_confirm)

    events = _collect_seller(_confirm_request("d-9"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    text = events[1]["data"]["text"]
    assert "sk-abcdefghijklmnop1234" not in text
    assert "[민감한 정보라 가려드렸어요]" in text


def test_confirm_spring_down_maps_to_apology_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """confirm 중 Spring 장애 — 사과 token(초안 유지 안내) + error(INTERNAL, retryable=True)."""
    from app.services.spring_client import SpringUnavailableError

    monkeypatch.setattr(seller_api, "route_question", _no_route)

    async def fake_confirm(draft_id, *, seller_id, brand_id):
        raise SpringUnavailableError("conn refused")

    monkeypatch.setattr(seller_api, "confirm_draft", fake_confirm)

    events = _collect_seller(_confirm_request("d-9"))

    assert [e["type"] for e in events] == ["meta", "token", "error"]
    assert "초안은 유지" in events[1]["data"]["text"]
    assert events[2]["data"]["code"] == "INTERNAL"
    assert events[2]["data"]["retryable"] is True


def test_confirm_spring_rejected_maps_to_non_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#620] confirm 중 매핑 안 된 4xx(SpringRejected) — 5xx 와 달리 retryable=False.

    SpringRejected 는 SpringUnavailableError 의 하위라 이 except 를 먼저 두지 않으면
    위 5xx 테스트와 같은 "일시적 오류(재시도 가능)" 로 뭉개진다 — 그게 이 이슈의 핵심
    증상이었다.
    """
    from app.services.spring_client import SpringRejected

    monkeypatch.setattr(seller_api, "route_question", _no_route)

    async def fake_confirm(draft_id, *, seller_id, brand_id):
        raise SpringRejected("SOME_NEW_CODE: PATCH /internal/seller/1/products/101")

    monkeypatch.setattr(seller_api, "confirm_draft", fake_confirm)

    events = _collect_seller(_confirm_request("d-9"))

    assert [e["type"] for e in events] == ["meta", "token", "error"]
    assert events[2]["data"]["code"] == "INTERNAL"
    assert events[2]["data"]["retryable"] is False


def test_scope_refusal_short_circuits_before_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """② scope 선차단 — 라우팅 이전에 거절 token + done (LLM 0회)."""
    monkeypatch.setattr(seller_api, "route_question", _no_route)

    events = _collect_seller(_request("경쟁사 매출 알려줘"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[0]["data"]["lane"] == "refused"
    assert "도와드리기 어려운 영역" in events[1]["data"]["text"]
    assert events[-1]["data"]["panel"] == "keep"


# ── ②.5 [#531] 차트 요청 레인 선판정 — 순서 계약이 이 판정의 전부다 ──────────────


def _chart_pipeline_stub():
    """analysis 레인 진입만 확인하면 되는 최소 파이프라인 스텁."""
    from app.agents.seller.orchestrator import PipelineResult, VerifiedReport

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return PipelineResult(
            kind="report",
            text="그래프를 준비했습니다.",
            verified=VerifiedReport("그래프를 준비했습니다.", passed=True, attempts=1),
        )

    return fake_pipeline


def test_chart_keyword_short_circuits_routing_to_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#531] 차트 요청은 라우팅(LLM) 없이 analysis 레인으로 직행한다.

    supervisor 는 "이번달 매출 그래프 보여줘"를 조회로 보아 general 을 고르는데
    (해석 신호가 없다 — 프롬프트대로다), general 은 report 이벤트를 발행하지 않아
    좌표(charts[].series[].points[])가 나갈 자리 자체가 없다. 그 결과 general_agent 가
    ASCII 아트를 token 으로 그리던 것이 이 이슈다.
    """
    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", _chart_pipeline_stub())

    events = _collect_seller(_request("이번달 매출 그래프 보여줘"))

    assert events[0]["type"] == "meta"
    assert events[0]["data"]["lane"] == "analysis"


@pytest.mark.parametrize(
    "message",
    [
        "이번달 매출 차트 보여줘",
        "매출 그래프 띄워줘",
        "상품별 재고 시각화해줘",
        "전환율 도표로 보여줘",
        "최근 7일 매출 그려줘",
        "전환율 분석하고 그래프로 보여줘",  # 다른 발화와 섞여 와도 잡는다(존재 검사)
    ],
)
def test_chart_vocabulary_all_reach_analysis_lane(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    """차트 어휘 5종은 문장 어디에 있든 analysis 레인으로 간다."""
    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", _chart_pipeline_stub())

    events = _collect_seller(_request(message))

    assert events[0]["data"]["lane"] == "analysis"


def test_plain_lookup_still_goes_through_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    """[회귀] 차트 어휘가 없는 조회는 선판정이 삼키지 않는다 — 라우터가 판정한다.

    선판정이 "매출"만으로 반응하면 general 조회 전체가 analysis 로 끌려가
    #180(조회/해석 분리)이 통째로 무너진다.
    """
    called: list[str] = []

    async def _counting_route(question, context, recent_turns=(), screen=None):
        from app.agents.seller.schemas import RouteDecision

        called.append(question)
        return RouteDecision(category="general", reason="stub", confidence=0.9)

    agent = _StubStreamAgent([AIMessageChunk(content="1,200,000원입니다.")])
    monkeypatch.setattr(seller_api, "route_question", _counting_route)
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect_seller(_request("최근 7일 매출 보여줘"))

    assert called == ["최근 7일 매출 보여줘"], "라우팅을 건너뛰면 안 된다"
    assert events[0]["data"]["lane"] == "general"


def test_chart_keyword_does_not_bypass_scope_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """[순서 ②] scope 선차단이 차트 선판정보다 앞이다 — 도메인 밖 차트 요청 차단.

    뒤집히면 "경쟁사 매출 그래프 보여줘"가 analysis 레인에 들어가 타 판매자 데이터를
    조회하려 든다.
    """
    monkeypatch.setattr(seller_api, "route_question", _no_route)

    def _must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("도메인 밖 요청은 분석 파이프라인에 닿으면 안 된다")

    monkeypatch.setattr(seller_api, "run_analysis_pipeline", _must_not_run)

    events = _collect_seller(_request("경쟁사 매출 그래프 보여줘"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[0]["data"]["lane"] == "refused"


def test_chart_keyword_does_not_bypass_image_product_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[순서 image] 사진 실은 발화의 목적지는 등록 초안뿐이다(#506).

    "이 사진으로 등록하고 그래프도 보여줘" 가 analysis 로 새면 이미지가 버려진다.
    """
    monkeypatch.setattr(seller_api, "route_question", _no_route)

    def _must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("이미지 첨부 턴은 분석 파이프라인에 닿으면 안 된다")

    monkeypatch.setattr(seller_api, "run_analysis_pipeline", _must_not_run)

    async def _fake_product_stream(
        request, context, *, request_id=None, pending=None, pending_unknown=False
    ):
        yield seller_api._meta("product")
        yield seller_api._done("keep")

    monkeypatch.setattr(seller_api, "_product_stream", _fake_product_stream)

    request = SellerChatRequest(
        session_id="s-1",
        thread_id="t-1",
        message="이 사진으로 등록하고 그래프도 보여줘",
        image_urls=["https://cdn.example.com/a.jpg"],
    )
    events = _collect_seller(request)

    assert events[0]["data"]["lane"] == "product"


# ── [#591] analysis 실행 경로 교체 — supervisor analysis → search 레인 ──────────


def test_analysis_decision_runs_the_search_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#591] supervisor `analysis`(= 저장된 보고서를 찾는 의도)는 search 레인이 답한다.

    프롬프트만 고치고 이 배선을 그대로 두면 analysis 발화가 여전히 5단 파이프라인으로
    들어간다 — 이슈가 "실행 경로 교체"를 따로 못 박은 이유다. meta.lane 은 "analysis"
    그대로라 S-4(Lane 6종)는 무개정이고, progress·report 는 이 레인에 없다.
    """

    def _must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("analysis 판정이 5단 분석 파이프라인에 닿으면 안 된다")

    agent = _StubStreamAgent([AIMessageChunk(content="최신 보고서 요약입니다.")])
    monkeypatch.setattr(seller_api, "route_question", _route_stub("analysis"))
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", _must_not_run)
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect_seller(_request("최근 분석 보고서 보여줘"))

    types = [e["type"] for e in events]
    assert events[0]["type"] == "meta"
    assert events[0]["data"]["lane"] == "analysis"  # S-4 무개정
    assert "progress" not in types and "report" not in types  # search 레인엔 없는 이벤트
    assert types[-1] == "done" and events[-1]["data"]["panel"] == "keep"


def test_chart_turn_is_unaffected_by_the_search_lane_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#591 회귀] 차트 턴은 그대로 planner 경로(5단 파이프라인)로 간다.

    게이트 ②.5 는 supervisor 보다 앞이라 이 재편과 무관해야 한다. 여기서 search 레인으로
    새면 좌표를 싣는 report 이벤트가 나갈 자리가 사라지고 #531 이 그대로 되살아난다.
    """
    from app.agents.seller.orchestrator import PipelineResult

    called: list[str] = []

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        called.append(question)
        return PipelineResult(kind="report", text="그래프를 준비했습니다.")

    def _must_not_build(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("차트 턴이 search 레인으로 새면 안 된다")

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)
    monkeypatch.setattr(seller_api, "build_general_agent", _must_not_build)

    events = _collect_seller(_request("이번달 매출 그래프 보여줘"))

    assert called == ["이번달 매출 그래프 보여줘"]
    assert events[0]["data"]["lane"] == "analysis"
    assert "report" in [e["type"] for e in events]


def test_analysis_route_relays_progress_and_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """분석 파이프라인 레인 — 진행 token(emit 중계) → 최종 text token → report → done.

    [#591] supervisor `analysis` 는 이제 search 레인으로 간다 — 이 5단 파이프라인에 닿는
    채팅 경로는 게이트 ②.5(차트 어휘) 하나뿐이라 그 입구로 진입한다. `_no_route` 로
    라우팅이 호출되지 않는 것까지 함께 고정한다.
    """
    from app.agents.seller.orchestrator import PipelineResult, VerifiedReport

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        await emit("매출 이상 분석 중…")
        return PipelineResult(
            kind="report",
            text="6월 매출 보고서 본문",
            verified=VerifiedReport(
                "6월 매출 보고서 본문", passed=True, attempts=1, last_score=None
            ),
        )

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("지난달 매출 그래프 보여줘"))

    assert [e["type"] for e in events] == ["meta", "progress", "token", "report", "done"]
    assert events[0]["data"]["lane"] == "analysis"
    assert events[1]["data"]["text"] == "매출 이상 분석 중…"  # 진행 = progress
    assert events[2]["data"]["text"] == "6월 매출 보고서 본문"  # 좌측 채팅 = token 산문
    assert events[3]["data"]["body"] == "6월 매출 보고서 본문"  # 우측 패널 = report 구조화
    assert events[-1]["data"]["panel"] == "replace"  # 보고서 → 우측 패널 교체


def _report_pipeline_result(**overrides):
    """report kind 의 완전한 PipelineResult 픽스처 — report 이벤트 직렬화 재료 포함."""
    import datetime as dt

    from app.agents.seller.orchestrator import PipelineResult, VerifiedReport
    from app.agents.seller.schemas import (
        ActionRecommendation,
        AnalysisFinding,
        ChartPoint,
        ChartSeries,
        ChartSet,
        ChartSpec,
        RecommendationSet,
    )

    base = dict(
        kind="report",
        text="6월 매출 보고서 본문",
        verified=VerifiedReport(
            "핵심 요약입니다.\n\n상세 해설입니다.", passed=True, attempts=1, last_score=None
        ),
        recommendations=RecommendationSet(
            recommendations=[
                ActionRecommendation(
                    action_type="price_adjust",
                    product_id=10293,
                    title="감귤청 가격 10% 인하",
                    rationale="근거",
                    expected_effect="재유입",
                )
            ]
        ),
        charts=ChartSet(
            charts=[
                ChartSpec(
                    title="일별 매출",
                    chart_type="line",
                    unit="KRW",
                    series=[ChartSeries(label="매출", points=[ChartPoint(x="07-01", y=1240000)])],
                    summary="6월 대비 12% 감소",
                )
            ]
        ),
        findings=[
            AnalysisFinding(
                analysis_type="sales_anomaly",
                summary="07-12 매출 급락",
                evidence=["07-12 매출 1,250,000원"],
                severity="warning",
                recommendation="프로모션 부재 확인",
            ),
            AnalysisFinding(
                analysis_type="churn",
                summary="데이터 확보 실패 — 분석 실행 오류(응답 시간 초과)",
                evidence=[],
                severity="info",
            ),
        ],
        period=(dt.date(2026, 7, 1), dt.date(2026, 7, 31)),
        chart_requested=True,
    )
    base.update(overrides)
    return PipelineResult(**base)


def test_analysis_route_emits_report_between_token_and_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """report 는 kind=report 일 때 정확히 1회, token 뒤·done 앞 — 전 필드 계약
    (이슈 #296, api-spec §3.2 v0.24.0). charts 직렬화는 구 chart 이벤트 형식 그대로."""

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return _report_pipeline_result()

    monkeypatch.setattr(seller_api, "route_question", _route_stub("analysis"))
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("지난달 매출 그래프로 보여줘"))

    assert [e["type"] for e in events] == ["meta", "token", "report", "done"]
    data = events[2]["data"]
    assert data["title"] == "판매 분석 보고서"
    assert data["period"] == {"from": "2026-07-01", "to": "2026-07-31"}
    assert data["generatedAt"].endswith("+09:00")  # KST 고정
    assert data["summary"] == "핵심 요약입니다."  # 첫 문단 분리
    assert data["body"] == "핵심 요약입니다.\n\n상세 해설입니다."  # 산문 전문
    # findings — camelCase·내부 보류 필드(chart_data_hint) 비노출
    assert data["findings"][0] == {
        "analysisType": "sales_anomaly",
        "severity": "warning",
        "summary": "07-12 매출 급락",
        "evidence": ["07-12 매출 1,250,000원"],
        "recommendation": "프로모션 부재 확인",
    }
    assert "chartDataHint" not in data["findings"][0]
    # limitations — degrade finding(evidence==[])의 summary 모음
    assert data["limitations"] == ["데이터 확보 실패 — 분석 실행 오류(응답 시간 초과)"]
    # [#504] 차트 기간 별도 지정 없음 → chartPeriod null, 실패 사유 없음 → 빈 배열
    assert data["chartPeriod"] is None
    assert data["chartUnavailable"] == []
    # charts — 구 chart 이벤트 직렬화 형식 그대로 이관
    assert data["chartRequested"] is True
    chart_data = data["charts"][0]
    assert chart_data["title"] == "일별 매출"
    assert chart_data["chartType"] == "line"  # 와이어 camelCase
    assert chart_data["unit"] == "KRW"
    assert chart_data["aggregate"] == "sum"  # [#504] 소스 레지스트리 집계 방식(기본 sum)
    assert chart_data["series"][0]["label"] == "매출"
    assert chart_data["series"][0]["points"][0] == {"x": "07-01", "y": 1240000}
    assert chart_data["summary"] == "6월 대비 12% 감소"
    # recommendations — index = 목록 순서(§6.3 "N번" 계약) + 안내 문구
    assert data["recommendations"] == [
        {
            "index": 1,
            "title": "감귤청 가격 10% 인하",
            "expectedEffect": "재유입",
            "actionType": "price_adjust",
            "productId": 10293,
        }
    ]
    assert data["applyGuide"].startswith("적용을 원하시면")


def test_analysis_report_event_allows_empty_charts(monkeypatch: pytest.MonkeyPatch) -> None:
    """charts 미요청(None)·전건 드랍([]) 공통 — report 는 나가고 charts 는 빈 배열
    (구 chart 미발행 규약 폐기 — 분기는 charts 배열 유무로만)."""
    from app.agents.seller.schemas import ChartSet

    cases = [
        dict(charts=None, chart_requested=False),  # 미요청
        dict(charts=ChartSet(charts=[]), chart_requested=True),  # 요청했으나 전건 드랍
    ]
    for overrides in cases:

        async def fake_pipeline(
            question, context, *, today, emit, recent_turns=(), screen=None, _o=overrides
        ):
            return _report_pipeline_result(**_o)

        monkeypatch.setattr(seller_api, "route_question", _route_stub("analysis"))
        monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

        events = _collect_seller(_request("지난달 매출 그래프로 보여줘"))

        assert [e["type"] for e in events] == ["meta", "token", "report", "done"]
        data = events[2]["data"]
        assert data["charts"] == []
        assert data["chartRequested"] is overrides["chart_requested"]


def test_analysis_report_event_chart_fields_504(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#504] chartPeriod(다를 때만)·chartUnavailable(부분 성공 공존)·unit RATING·
    aggregate avg — 재설계 신필드 직렬화 계약."""
    import datetime as dt

    from app.agents.seller.charts import ChartUnavailable
    from app.agents.seller.schemas import ChartPoint, ChartSeries, ChartSet, ChartSpec

    rating_chart = ChartSpec(
        title="상품별 평균 평점",
        chart_type="bar",
        unit="RATING",
        aggregate="avg",
        series=[ChartSeries(label="평점", points=[ChartPoint(x="감귤청 500ml", y=4.6)])],
        summary="상품 42개 중 상위 15개만 표시했습니다.",
    )
    result = _report_pipeline_result(
        charts=ChartSet(charts=[rating_chart]),
        chart_period=(dt.date(2026, 8, 1), dt.date(2026, 8, 7)),
        chart_unavailable=(
            ChartUnavailable(
                reason="unsupported_axes", message="'퍼널'은(는) 그래프로 만들 수 없습니다."
            ),
        ),
    )

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return result

    monkeypatch.setattr(seller_api, "route_question", _route_stub("analysis"))
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("차트 보여줘"))
    data = next(e for e in events if e["type"] == "report")["data"]

    # 차트 기간이 분석 기간과 다르면 chartPeriod 를 싣는다
    assert data["chartPeriod"] == {"from": "2026-08-01", "to": "2026-08-07"}
    # 부분 성공 — charts 와 chartUnavailable 이 동시에 나간다
    assert data["charts"][0]["unit"] == "RATING"
    assert data["charts"][0]["aggregate"] == "avg"
    assert data["chartUnavailable"] == [
        {"reason": "unsupported_axes", "message": "'퍼널'은(는) 그래프로 만들 수 없습니다."}
    ]


def test_analysis_report_event_chart_period_equal_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[#504] 차트 기간이 분석 기간과 같으면 chartPeriod 는 null — "없으면 period 와
    같다"는 FE 계약이라 같은 값을 중복으로 싣지 않는다."""
    import datetime as dt

    result = _report_pipeline_result(
        chart_period=(dt.date(2026, 7, 1), dt.date(2026, 7, 31))  # == period
    )

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return result

    monkeypatch.setattr(seller_api, "route_question", _route_stub("analysis"))
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("차트 보여줘"))
    data = next(e for e in events if e["type"] == "report")["data"]
    assert data["chartPeriod"] is None


def test_analysis_report_event_chart_only_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#504] chart_only 턴 — 제목이 "판매 분석 그래프"로 나간다(FE 는 title 을 그대로
    쓰므로 이 값이 화면 구분의 전부다). 보고서 재료 없이도 이벤트가 성립한다."""
    result = _report_pipeline_result(
        verified=None,
        recommendations=None,
        findings=None,
        chart_only=True,
    )

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return result

    monkeypatch.setattr(seller_api, "route_question", _route_stub("analysis"))
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("최근 7일 매출 그래프만"))
    data = next(e for e in events if e["type"] == "report")["data"]
    assert data["title"] == "판매 분석 그래프"
    assert data["body"] == "" and data["findings"] == []
    assert data["charts"][0]["title"] == "일별 매출"


def test_analysis_route_no_report_event_for_non_report_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kind!=report(되묻기·사과·거절)면 report 이벤트를 보내지 않는다."""
    from app.agents.seller.orchestrator import PipelineResult

    for kind, text, panel in [
        ("clarification", "기간을 명시해 주세요.", "keep"),
        ("apology", "죄송합니다. 다시 시도해 주세요.", "keep"),
    ]:

        async def fake_pipeline(
            question, context, *, today, emit, recent_turns=(), screen=None, _k=kind, _t=text
        ):
            return PipelineResult(kind=_k, text=_t)

        monkeypatch.setattr(seller_api, "route_question", _route_stub("analysis"))
        monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

        events = _collect_seller(_request("차트로 보여줘"))

        assert [e["type"] for e in events] == ["meta", "token", "done"]
        assert events[-1]["data"]["panel"] == panel


def test_no_chart_event_ever_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """구 chart 이벤트는 legacy 폐기 — 어떤 경로에서도 방출되지 않는다(v0.24.0)."""

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return _report_pipeline_result()

    monkeypatch.setattr(seller_api, "route_question", _route_stub("analysis"))
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("지난달 매출 그래프로 보여줘"))

    assert "chart" not in {e["type"] for e in events}
    assert not hasattr(seller_api, "_chart_event")  # 직렬화 함수 자체가 제거됐다


def test_analysis_report_event_masks_and_strips_unsafe_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """report 이벤트도 draft 이벤트와 동일하게 필드별 마스킹·정제를 거친다
    (본문·findings·차트 — 제어문자 제거 + 시크릿 마스킹)."""
    from app.agents.seller.orchestrator import VerifiedReport
    from app.agents.seller.schemas import (
        AnalysisFinding,
        ChartPoint,
        ChartSeries,
        ChartSet,
        ChartSpec,
    )

    zwsp = "\u200b"
    charts = ChartSet(
        charts=[
            ChartSpec(
                title="6월\x1b[31m 매출",
                chart_type="line",
                unit="KRW",
                series=[ChartSeries(label="매출", points=[ChartPoint(x="07-01", y=1240000)])],
                summary=f"키는 Bearer abcdefgh{zwsp}ijklmnop1234 입니다",
            )
        ]
    )

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return _report_pipeline_result(
            verified=VerifiedReport(
                f"본문 키는 Bearer abcdefgh{zwsp}ijklmnop1234 입니다",
                passed=True,
                attempts=1,
                last_score=None,
            ),
            findings=[
                AnalysisFinding(
                    analysis_type="sales_anomaly",
                    summary="요약\x1b[31m 텍스트",
                    evidence=["근거\x1b[31m 수치"],
                    severity="warning",
                )
            ],
            charts=charts,
        )

    monkeypatch.setattr(seller_api, "route_question", _route_stub("analysis"))
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("차트로 보여줘"))

    data = next(e for e in events if e["type"] == "report")["data"]
    assert "Bearer abcdefghijklmnop1234" not in data["body"]
    assert "[민감한 정보라 가려드렸어요]" in data["body"]
    assert data["summary"] == data["body"]  # 짧은 본문 — 요약도 동일 정제를 거친 값
    assert "\x1b" not in data["findings"][0]["summary"]
    assert "\x1b" not in data["findings"][0]["evidence"][0]
    chart_data = data["charts"][0]
    assert "\x1b" not in chart_data["title"]
    assert "Bearer abcdefghijklmnop1234" not in chart_data["summary"]
    assert "[민감한 정보라 가려드렸어요]" in chart_data["summary"]


def test_analysis_token_strips_unsafe_report_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """보고서·compose_response 계열 LLM text 는 token 직전 공용 정제를 거친다."""
    from app.agents.seller.orchestrator import PipelineResult

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return PipelineResult(
            kind="report",
            text="6월\x1b[31m 매출\n보고서\u200b\u202e\n   기대 효과: 유지",
        )

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("지난달 매출 그래프 보여줘"))

    assert (
        "".join(e["data"]["text"] for e in events if e["type"] == "token")
        == "6월[31m 매출\n보고서\n   기대 효과: 유지"
    )


def test_analysis_token_masks_secret_after_stripping_unsafe_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """분석 결과도 정제 후 마스킹해 zero-width 기반 시크릿 우회를 차단한다."""
    from app.agents.seller.orchestrator import PipelineResult

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return PipelineResult(
            kind="report",
            text="키는 Bearer abcdefgh\u200bijklmnop1234 입니다",
        )

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("지난달 매출 그래프 보여줘"))

    text = "".join(e["data"]["text"] for e in events if e["type"] == "token")
    assert "Bearer abcdefghijklmnop1234" not in text
    assert "[민감한 정보라 가려드렸어요]" in text


def test_analysis_route_clarification_is_token_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """되묻기(kind=clarification)도 동일 계약 — text→token→done (error 아님)."""
    from app.agents.seller.orchestrator import PipelineResult

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return PipelineResult(kind="clarification", text="기간을 명시해 주세요.")

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("매출 그래프"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert "기간" in events[1]["data"]["text"]
    assert events[-1]["data"]["panel"] == "keep"  # 되묻기 = 대화(패널 유지)


def test_analysis_route_exception_maps_to_apology_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """예외 전파(planner 장애 등) → 사과 token + error(INTERNAL) 종료(§5-2 매핑)."""

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        await emit("분석 계획 수립 중…")
        raise RuntimeError("planner down")

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("매출 그래프 보여줘"))

    assert [e["type"] for e in events] == ["meta", "progress", "token", "error"]
    assert "죄송합니다" in events[2]["data"]["text"]
    assert events[3]["data"]["code"] == "INTERNAL"


def test_analysis_route_timeout_maps_to_llm_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """파이프라인 TimeoutError → 사과 token + error(LLM_TIMEOUT)."""

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        raise TimeoutError("planner timeout")

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("매출 그래프 보여줘"))

    assert events[-1]["type"] == "error"
    assert events[-1]["data"]["code"] == "LLM_TIMEOUT"


class _StubProductAgent:
    def __init__(self, proposal) -> None:
        self._proposal = proposal

    async def ainvoke(self, _input: dict, context: object = None) -> dict:
        return {"structured_response": self._proposal}


def test_product_route_emits_draft_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """product 분기 — DraftProposal → SSE draft(camelCase 페이로드) + done."""
    from app.agents.seller.schemas import DraftChange, DraftProposal

    proposal = DraftProposal(
        op="update",
        product_id=101,
        changes=[DraftChange(field="price", before="15000", after="12900")],
        summary="가격 12,900원으로 인하",
    )
    monkeypatch.setattr(seller_api, "route_question", _route_stub("product"))
    monkeypatch.setattr(seller_api, "build_product_agent", lambda: _StubProductAgent(proposal))

    events = _collect_seller(_request("감귤청 가격 12900원으로 바꿔줘"))

    assert [e["type"] for e in events] == ["meta", "draft", "done"]
    assert events[0]["data"]["lane"] == "product"
    assert events[-1]["data"]["panel"] == "replace"  # diff 카드 → 우측 패널
    draft = events[1]["data"]
    assert draft["op"] == "update"
    assert draft["productId"] == 101  # F2 — 숫자 id
    assert draft["draftId"]  # 발급됨(실행 바인딩은 4-2)
    assert draft["changes"] == [{"field": "price", "before": "15000", "after": "12900"}]


# ── [상품명 인식 개선] product 레인 recent_turns 배선 ─────────────────────────


def test_product_agent_input_includes_recent_turns_block() -> None:
    """recent_turns 가 있으면 [최근 대화] 블록으로 [판매자 요청] 보다 앞서 주입된다."""
    agent_input = seller_api._product_agent_input(
        _request("이거 가격 3000원으로 바꿔줘"),
        analysis=None,
        candidates=[],
        pending=None,
        image_urls=[],
        recent_turns=[
            ("user", "감귤청 재고 좀 보여줘"),
            ("assistant", "어느 상품을 말씀하시는 건가요? 후보가 여러 개입니다."),
        ],
    )

    assert agent_input.startswith("[최근 대화]")
    assert "감귤청 재고 좀 보여줘" in agent_input
    assert "어느 상품을 말씀하시는 건가요?" in agent_input
    assert agent_input.index("[최근 대화]") < agent_input.index("[판매자 요청]")


def test_product_agent_input_omits_recent_turns_block_when_empty() -> None:
    """recent_turns 미지정(기본값)이면 블록 자체가 없다 — 기존 입력 그대로 유지."""
    agent_input = seller_api._product_agent_input(
        _request("가격 3000원으로 바꿔줘"),
        analysis=None,
        candidates=[],
        pending=None,
        image_urls=[],
    )

    assert "[최근 대화]" not in agent_input


def test_product_route_receives_recent_turns_from_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """supervisor 경유 product 진입(③)도 스레드 최근 턴을 받는다.

    product 레인은 매 턴 새 agent.ainvoke() 호출이라(checkpointer 없음) 대상 상품이
    불명확해 되물은 다음 턴이 이전 되물음을 기억하지 못하던 문제의 회귀 방지 —
    이제 recent_turns 가 [최근 대화] 블록으로 agent 입력에 실제로 전달돼야 한다.
    """
    from app.agents.seller import thread as seller_thread
    from app.agents.seller.context import SellerContext
    from app.agents.seller.schemas import DraftChange, DraftProposal

    ctx = SellerContext(seller_id=7, brand_id=3)
    asyncio.run(
        seller_thread.record_turn(
            ctx, "t-1", "감귤청 가격 바꿔줘", "어느 상품을 말씀하시는 건가요?"
        )
    )
    captured: dict[str, str] = {}

    class _CapturingProductAgent:
        async def ainvoke(self, input: dict, context: object = None) -> dict:
            captured["content"] = input["messages"][0].content
            return {
                "structured_response": DraftProposal(
                    op="update",
                    product_id=101,
                    changes=[DraftChange(field="price", before="15000", after="12900")],
                    summary="가격 12,900원으로 인하",
                )
            }

    monkeypatch.setattr(seller_api, "route_question", _route_stub("product"))
    monkeypatch.setattr(seller_api, "build_product_agent", lambda: _CapturingProductAgent())

    _collect_seller(_request("12900원으로 바꿔줘"))

    assert "[최근 대화]" in captured["content"]
    assert "감귤청 가격 바꿔줘" in captured["content"]
    assert "어느 상품을 말씀하시는 건가요?" in captured["content"]


def test_product_draft_strips_llm_and_seller_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """draft 의 seller before·LLM after/summary 는 FE diff 카드 노출 직전에 정제된다."""
    from app.agents.seller.schemas import DraftChange, DraftProposal

    proposal = DraftProposal(
        op="update",
        product_id=101,
        changes=[
            DraftChange(
                field="description",
                before="기존\x1b[31m 설명 sk-abcdefgh\u200bijklmnop1234\u202e",
                after="새\n설명 Bearer abcdefgh\u200bijklmnop1234\x00",
            )
        ],
        summary="설명\t수정\u200b\u202e",
    )
    monkeypatch.setattr(seller_api, "route_question", _route_stub("product"))
    monkeypatch.setattr(seller_api, "build_product_agent", lambda: _StubProductAgent(proposal))

    events = _collect_seller(_request("설명 바꿔줘"))

    draft = next(e for e in events if e["type"] == "draft")["data"]
    assert draft["changes"] == [
        {
            "field": "description",
            "before": "기존[31m 설명 [민감한 정보라 가려드렸어요]",
            "after": "새\n설명 [민감한 정보라 가려드렸어요]",
        }
    ]
    assert draft["summary"] == "설명 수정"


def test_product_draft_executes_the_sanitized_after_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실행값은 정제하되 SSE 전용 시크릿 마스킹으로 영구 오염하지 않는다."""
    from app.agents.seller.schemas import DraftChange, DraftProposal
    from app.schemas.spring import ProductUpdateResult, SellerProductList, SellerProductRow
    from app.services.spring_client import set_spring_client

    class _Spring:
        def __init__(self) -> None:
            self.patch = None

        async def list_products(self, brand_id, status=None, q=None, limit=None, offset=None):
            row = SellerProductRow(
                productId=101,
                name="감귤청",
                price=15000,
                stockQuantity=100,
                description="기존\x1b[31m 설명\u200b\u202e",
            )
            return SellerProductList(rows=[row])

        async def update_product(self, brand_id, product_id, patch):
            self.patch = patch
            return ProductUpdateResult(productId=product_id)

    spring = _Spring()
    proposal = DraftProposal(
        op="update",
        product_id=101,
        changes=[
            DraftChange(
                field="description",
                before="기존\x1b[31m 설명\u200b\u202e",
                after="새\n설명 ❤️ Bearer abcdefghijklmnop1234 A\ufe0fB\U000e0061",
            )
        ],
        summary="설명 수정",
    )
    monkeypatch.setattr(seller_api, "route_question", _route_stub("product"))
    monkeypatch.setattr(seller_api, "build_product_agent", lambda: _StubProductAgent(proposal))
    set_spring_client(spring)
    try:
        draft_events = _collect_seller(_request("설명 바꿔줘"))
        draft = next(e for e in draft_events if e["type"] == "draft")["data"]
        confirm_events = _collect_seller(_confirm_request(draft["draftId"]))
    finally:
        set_spring_client(None)

    assert [e["type"] for e in confirm_events] == ["meta", "token", "done"]
    assert draft["changes"][0]["after"] == "새\n설명 ❤️ [민감한 정보라 가려드렸어요] AB"
    assert spring.patch.description == "새\n설명 ❤️ Bearer abcdefghijklmnop1234 AB"
    assert "[민감한 정보라 가려드렸어요]" not in spring.patch.description
    assert all(char not in spring.patch.description for char in ("\ufe0fB", "\U000e0061"))


def test_snapshot_before_fills_update_before_from_spring(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#623] update 초안의 before 는 LLM 산물이 아니라 코드가 조회한 실제값이다.

    list_my_products 가 originalPrice·description·imageUrl 을 요약하지 않아 LLM 이
    before 를 빈 문자열로 낼 수밖에 없던 필드들 — _snapshot_before 가 I-9 재조회로
    실제값을 채워야 confirm 시점 find_stale_changes 가 영구 불일치로 오판하지 않는다
    (#623 증상: "초안 작성 이후 상품 정보가 변경되어 반영을 중단했습니다"가 매번 뜸).
    stock_quantity 는 예외로 LLM(list_my_products 조회) 값을 그대로 유지해야 한다 —
    옵션별 재고는 행 전체 합계로 덮어쓰면 안 된다.
    """
    from app.agents.seller.schemas import DraftChange, DraftProposal
    from app.schemas.spring import SellerProductList, SellerProductRow
    from app.services.spring_client import set_spring_client

    class _Spring:
        async def list_products(self, brand_id, status=None, q=None, limit=None, offset=None):
            row = SellerProductRow(
                productId=101,
                name="감귤청",
                price=15000,
                originalPrice=20000,
                stockQuantity=100,
                description="실제 저장된 설명",
                imageUrl="https://cdn.example.com/real.jpg",
            )
            return SellerProductList(rows=[row])

    set_spring_client(_Spring())
    proposal = DraftProposal(
        op="update",
        product_id=101,
        changes=[
            # LLM 은 [#623] 이후 계약대로 "" 로 낸다 — list_my_products 가 요약하지
            # 않는 필드라 정확한 값을 알 방법이 없다.
            DraftChange(field="original_price", before="", after="18000"),
            DraftChange(field="description", before="", after="새 설명"),
            DraftChange(field="image_url", before="", after="https://cdn.example.com/new.jpg"),
            # 재고는 프롬프트 계약대로 LLM 이 조회값을 그대로 낸다 — 코드가 덮어쓰지 않는다.
            DraftChange(field="stock_quantity", before="100", after="80"),
        ],
        summary="정가·설명·이미지·재고 수정",
    )
    monkeypatch.setattr(seller_api, "route_question", _route_stub("product"))
    monkeypatch.setattr(seller_api, "build_product_agent", lambda: _StubProductAgent(proposal))
    try:
        events = _collect_seller(_request("101번 상품 정가·설명·이미지·재고 수정"))
    finally:
        set_spring_client(None)

    draft = next(e for e in events if e["type"] == "draft")["data"]
    before_by_field = {c["field"]: c["before"] for c in draft["changes"]}
    assert before_by_field["originalPrice"] == "20000"  # 코드가 채움(LLM "" 무시)
    assert before_by_field["description"] == "실제 저장된 설명"
    assert before_by_field["imageUrl"] == "https://cdn.example.com/real.jpg"
    assert before_by_field["stockQuantity"] == "100"  # 재고는 LLM/조회값 그대로 유지


def test_snapshot_before_soft_fails_when_spring_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """조회 실패(Spring 장애 등)는 이번 턴 초안 발급을 막지 않는다 — soft-fail(#623).

    before 갱신에 실패해도 기존 changes 를 그대로 두고 draft 를 발급한다. 실제
    불일치·미발견은 confirm 시점 find_stale_changes/_execute_draft 가 최종 방어한다.
    """
    from app.agents.seller.schemas import DraftChange, DraftProposal
    from app.services.spring_client import SpringUnavailableError, set_spring_client

    class _DownSpring:
        async def list_products(self, brand_id, status=None, q=None, limit=None, offset=None):
            raise SpringUnavailableError("boom")

    set_spring_client(_DownSpring())
    proposal = DraftProposal(
        op="update",
        product_id=101,
        changes=[DraftChange(field="description", before="", after="새 설명")],
        summary="설명 수정",
    )
    monkeypatch.setattr(seller_api, "route_question", _route_stub("product"))
    monkeypatch.setattr(seller_api, "build_product_agent", lambda: _StubProductAgent(proposal))
    try:
        events = _collect_seller(_request("설명 바꿔줘"))
    finally:
        set_spring_client(None)

    assert [e["type"] for e in events] == ["meta", "draft", "done"]  # 초안 발급 자체는 유지
    draft = events[1]["data"]
    assert draft["changes"] == [{"field": "description", "before": "", "after": "새 설명"}]


def test_draft_changes_field_is_camelcase(monkeypatch: pytest.MonkeyPatch) -> None:
    """C-1 — 와이어 `changes[].field` 는 camelCase(규약 §2.2). 내부는 snake_case 유지.

    stock_quantity→stockQuantity, original_price→originalPrice, image_url→imageUrl.
    """
    from app.agents.seller.schemas import DraftChange, DraftProposal

    proposal = DraftProposal(
        op="update",
        product_id=101,
        changes=[
            DraftChange(field="stock_quantity", before="100", after="50"),
            DraftChange(field="original_price", before="30000", after="28000"),
            DraftChange(field="image_url", before="a.jpg", after="b.jpg"),
            DraftChange(field="price", before="15000", after="12900"),
        ],
        summary="재고·정가·이미지·가격 수정",
    )
    monkeypatch.setattr(seller_api, "route_question", _route_stub("product"))
    monkeypatch.setattr(seller_api, "build_product_agent", lambda: _StubProductAgent(proposal))

    events = _collect_seller(_request("101번 상품 여러 필드 수정"))

    draft = next(e for e in events if e["type"] == "draft")["data"]
    wire_fields = [c["field"] for c in draft["changes"]]
    assert wire_fields == ["stockQuantity", "originalPrice", "imageUrl", "price"]
    # [#297] orderItemId 는 ship 전용 키 — 상품 op 와이어는 불변(추가 전용 계약).
    assert "orderItemId" not in draft


def test_ship_draft_event_carries_order_item_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#297] op="ship"(I-30 발송) draft — orderItemId 탑재·changes 빈 목록·panel=replace."""
    from app.agents.seller.schemas import DraftProposal

    proposal = DraftProposal(
        op="ship",
        product_id=None,
        order_item_id=5551,
        changes=[],
        summary="주문 342 벨티드 린넨 원피스(블루/M) 발송 처리",
    )
    monkeypatch.setattr(seller_api, "route_question", _route_stub("product"))
    monkeypatch.setattr(seller_api, "build_product_agent", lambda: _StubProductAgent(proposal))

    events = _collect_seller(_request("342번 주문 발송 처리해줘"))

    assert [e["type"] for e in events] == ["meta", "draft", "done"]
    assert events[0]["data"] == {"lane": "product"}  # 레인 신설 없음(확정 2026-08-04)
    draft = events[1]["data"]
    assert draft["op"] == "ship"
    assert draft["orderItemId"] == 5551
    assert draft["productId"] is None
    assert draft["changes"] == []
    assert "발송" in draft["summary"]
    assert events[2]["data"]["panel"] == "replace"  # diff 카드 = 우측 패널 교체


def test_product_route_clarification_is_token_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """clarification 이 차 있으면 draft 불성립 — 되묻기 token + done."""
    from app.agents.seller.schemas import DraftProposal

    proposal = DraftProposal(
        op="update", summary="", clarification="'감귤' 상품이 3건입니다. 어느 상품인가요?"
    )
    monkeypatch.setattr(seller_api, "route_question", _route_stub("product"))
    monkeypatch.setattr(seller_api, "build_product_agent", lambda: _StubProductAgent(proposal))

    events = _collect_seller(_request("감귤 가격 바꿔줘"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert "어느 상품" in events[1]["data"]["text"]
    assert events[-1]["data"]["panel"] == "keep"


def test_product_route_invalid_draft_becomes_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_draft 불성립(4-2 코드 선검증) — draft 미발행, 되묻기 token + done."""
    from app.agents.seller.schemas import DraftProposal

    proposal = DraftProposal(op="update", product_id=None, summary="")  # 대상 미특정
    monkeypatch.setattr(seller_api, "route_question", _route_stub("product"))
    monkeypatch.setattr(seller_api, "build_product_agent", lambda: _StubProductAgent(proposal))

    events = _collect_seller(_request("가격 바꿔줘"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert "상품" in events[1]["data"]["text"]


def test_product_route_draft_is_confirmable(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2E: 스트림 1 draft 의 draftId 로 confirm(스트림 2) — checkpoint 바인딩 검증."""
    from app.agents.seller.schemas import DraftChange, DraftProposal
    from app.schemas.spring import (
        BehaviorEventsResult,
        BehaviorProductRow,
        ProductUpdateResult,
        SellerProductList,
        SellerProductRow,
    )
    from app.services.spring_client import set_spring_client

    class _Spring:
        def __init__(self):
            self.patches = []

        async def list_products(self, brand_id, status=None, q=None, limit=None, offset=None):
            row = SellerProductRow(productId=101, name="감귤청", price=15000, stockQuantity=100)
            return SellerProductList(rows=[row])

        async def update_product(self, brand_id, product_id, patch):
            self.patches.append((brand_id, product_id, patch))
            # [#620] changes 가 비면 "이미 그 값" 으로 갈음돼 already_done 이 된다 —
            # 이 테스트는 실제 반영(executed)을 검증하므로 비어있지 않은 값을 준다.
            return ProductUpdateResult(productId=product_id, changes=["PRICE"])

        # [#659] 저성과 참고 문구 조회 — 임계 이상으로 두어 이 테스트의 반영 안내
        # 텍스트·이벤트 시퀀스를 건드리지 않는다.
        async def get_events(
            self, brand_id, from_=None, to=None, event_type=None, product_id=None, group_by=None
        ):
            return BehaviorEventsResult(
                rows=[BehaviorProductRow(productId=product_id, salesQuantity=999, counts={})]
            )

    spring = _Spring()
    set_spring_client(spring)
    proposal = DraftProposal(
        op="update",
        product_id=101,
        changes=[DraftChange(field="price", before="15000", after="12900")],
        summary="가격 인하",
    )
    monkeypatch.setattr(seller_api, "route_question", _route_stub("product"))
    monkeypatch.setattr(seller_api, "build_product_agent", lambda: _StubProductAgent(proposal))
    try:
        draft_events = _collect_seller(_request("감귤청 가격 12900원으로"))
        draft_id = draft_events[1]["data"]["draftId"]

        confirm_events = _collect_seller(_confirm_request(draft_id))
    finally:
        set_spring_client(None)

    assert [e["type"] for e in confirm_events] == ["meta", "token", "done"]
    assert "반영했어요" in confirm_events[1]["data"]["text"]
    assert confirm_events[-1]["data"]["panel"] == "refresh"
    assert spring.patches[0][1] == 101 and spring.patches[0][2].price == 12900


def test_apply_message_short_circuits_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """①.5 적용 선판정 — 라우팅·LLM 없이 적용 레인(4-3). 이력 없음 → 되묻기 token."""

    async def _no_reports(brand_id, *, limit, before=None):
        return []

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api.analysis_store, "list_reports", _no_reports)

    events = _collect_seller(_request("1번 적용해줘"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[0]["data"]["lane"] == "apply"
    assert "적용할 만한 분석 추천이 없어요" in events[1]["data"]["text"]


def test_apply_message_with_history_emits_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """①.5 → 최신 보고서 recommendations[N-1] 이 draft 이벤트로 — before 는 I-9 현재값.

    [이슈 #590] apply_recommendation 참조처가 Store -> analysis_store(DB, 이슈 #585)로
    바뀌어, history.save_history(Store) 와 별개로 analysis_store 조회 함수를 가짜로
    대체해 같은 추천을 돌려준다(실 PG 연결 없음).
    """
    from datetime import date
    from uuid import uuid4

    from app.agents.seller import analysis_store, history
    from app.agents.seller.analysis_records import RecommendationRecord, ReportRecord
    from app.agents.seller.schemas import ActionRecommendation, ProposedChange, RecommendationSet
    from app.schemas.spring import SellerProductList, SellerProductRow
    from app.services.spring_client import set_spring_client

    class _Spring:
        async def list_products(self, brand_id, status=None, q=None, limit=None, offset=None):
            row = SellerProductRow(productId=101, name="감귤청", price=15000, stockQuantity=100)
            return SellerProductList(rows=[row])

    set_spring_client(_Spring())
    monkeypatch.setattr(seller_api, "route_question", _no_route)
    recs = RecommendationSet(
        recommendations=[
            ActionRecommendation(
                action_type="price_adjust",
                product_id=101,
                title="감귤청 가격 10% 인하",
                rationale="r",
                changes=[ProposedChange(field="price", after="13500")],
            )
        ]
    )
    report = ReportRecord(
        id=uuid4(),
        brand_id=3,
        trigger_type="manual",
        period_from=date(2026, 6, 1),
        period_to=date(2026, 6, 30),
        title="지난달 매출 분석 보고서",
        summary="보고서",
        report_md="보고서",
        verified=True,
        attempts=1,
    )
    rec_records = [
        RecommendationRecord(
            id=uuid4(),
            report_id=report.id,
            brand_id=3,
            rank=1,
            action_type="price_adjust",
            product_ids=[101],
            title="감귤청 가격 10% 인하",
            rationale="r",
            changes=[{"field": "price", "after": "13500"}],
        )
    ]

    async def _fake_list_reports(brand_id, *, limit, before=None):
        return [report] if brand_id == 3 else []

    async def _fake_list_recommendations_by_report(report_id, *, brand_id):
        return rec_records if report_id == report.id and brand_id == 3 else []

    monkeypatch.setattr(analysis_store, "list_reports", _fake_list_reports)
    monkeypatch.setattr(
        analysis_store, "list_recommendations_by_report", _fake_list_recommendations_by_report
    )

    try:
        asyncio.run(
            history.save_history(
                7,
                question="지난달 매출 분석",
                analyses=["sales_anomaly"],
                date_from="2026-06-01",
                date_to="2026-06-30",
                report="보고서",
                recommendations=recs,
            )
        )

        events = _collect_seller(_request("1번 적용해줘"))
    finally:
        set_spring_client(None)

    assert [e["type"] for e in events] == ["meta", "draft", "done"]
    assert events[-1]["data"]["panel"] == "replace"
    draft = events[1]["data"]
    assert draft["op"] == "update" and draft["productId"] == 101
    assert draft["changes"] == [{"field": "price", "before": "15000", "after": "13500"}]
    # [결정 61] summary 에 출처 보고서를 명시한다.
    assert draft["summary"] == "지난달 매출 분석 보고서 · 1번 — 감귤청 가격 10% 인하"


def test_general_route_uses_general_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """general 분기 — 기존 astream 스트림 경로로 위임된다."""
    agent = _StubStreamAgent([AIMessageChunk(content="안녕하세요, 무엇을 도와드릴까요?")])
    monkeypatch.setattr(seller_api, "route_question", _route_stub("general"))
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect_seller(_request("안녕"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert events[0]["data"]["lane"] == "general"
    assert "도와드릴까요" in events[1]["data"]["text"]


def test_route_model_not_configured_emits_llm_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """supervisor 생성 전 provider 미구성도 general meta 뒤 error로 종료한다."""

    async def not_configured(question, context, recent_turns=(), screen=None):
        raise LLMNotConfigured("openai key missing")

    monkeypatch.setattr(seller_api, "route_question", not_configured)

    with caplog.at_level(logging.ERROR, logger="app.api.seller"):
        events = _collect_seller(_request("매출 알려줘"))

    assert [event["type"] for event in events] == ["meta", "error"]
    assert events[0]["data"]["lane"] == "general"
    assert events[1]["data"]["code"] == "LLM_UNAVAILABLE"
    assert events[1]["data"]["requestId"]
    assert events[1]["data"]["retryable"] is False
    assert '"action": "routing"' in caplog.text
    assert '"errorCode": "LLM_UNAVAILABLE"' in caplog.text
    assert safe_fingerprint("t-1") in caplog.text
    assert "thread=t-1" not in caplog.text
    assert "openai key missing" not in caplog.text


# ── 화면 전환 신호(meta/panel) 계약 — FE 요구 1~3 (2026-07-22 B) ──────────────────


def test_every_stream_starts_with_meta_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """모든 판매자 스트림의 첫 프레임은 meta{lane} — FE 가 레인을 즉시 안다."""
    agent = _StubStreamAgent([AIMessageChunk(content="네")])
    monkeypatch.setattr(seller_api, "route_question", _route_stub("general"))
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect_seller(_request("안녕"))

    assert events[0]["type"] == "meta"
    assert events[0]["data"]["lane"] in {
        "analysis",
        "product",
        "general",
        "confirm",
        "apply",
        "refused",
    }


def test_analysis_progress_is_separate_from_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """진행 상태는 progress, 최종 보고서는 token — FE 가 로딩과 답변을 구분한다."""
    from app.agents.seller.orchestrator import PipelineResult

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        await emit("워커 실행 중…")
        await emit("보고서 작성 중…")
        return PipelineResult(kind="report", text="최종 보고서")

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    events = _collect_seller(_request("매출 그래프 보여줘"))

    assert [e["type"] for e in events] == [
        "meta",
        "progress",
        "progress",
        "token",
        "report",
        "done",
    ]
    assert [e["data"]["text"] for e in events if e["type"] == "progress"] == [
        "워커 실행 중…",
        "보고서 작성 중…",
    ]
    assert events[-3]["data"]["text"] == "최종 보고서"  # token
    assert events[-1]["data"]["panel"] == "replace"


def test_panel_action_per_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """레인별 종료 panel: general=keep · analysis(report)=replace · confirm(executed)=refresh."""
    # general → keep
    monkeypatch.setattr(seller_api, "route_question", _route_stub("general"))
    monkeypatch.setattr(
        seller_api,
        "build_general_agent",
        lambda today, checkpointer=None: _StubStreamAgent([AIMessageChunk(content="네")]),
    )
    ev = _collect_seller(_request("배송 정책 뭐야?"))
    assert ev[-1]["data"]["panel"] == "keep"

    # confirm(executed) → refresh
    async def fake_confirm(draft_id, *, seller_id, brand_id):
        return hitl.ConfirmOutcome("executed", "반영했습니다.")

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "confirm_draft", fake_confirm)
    ev2 = _collect_seller(_confirm_request("d-9"))
    assert ev2[0]["data"]["lane"] == "confirm"
    assert ev2[-1]["data"]["panel"] == "refresh"


# ── 대화 스레드 배선 — general thread config · 비-general 레인 record_turn ────────


def test_general_lane_passes_thread_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """general astream 에 seller-chat:{sellerId}:{threadId} config 가 전달된다."""
    captured: dict = {}

    class _ConfigCapturingAgent(_StubStreamAgent):
        async def astream(self, _input, config=None, context=None, stream_mode=""):
            captured["config"] = config
            async for item in super().astream(
                _input, config=config, context=context, stream_mode=stream_mode
            ):
                yield item

    monkeypatch.setattr(seller_api, "route_question", _route_stub("general"))
    monkeypatch.setattr(
        seller_api,
        "build_general_agent",
        lambda today, checkpointer=None: _ConfigCapturingAgent([AIMessageChunk(content="네")]),
    )

    _collect_seller(_request("어제 매출 알려줘"))

    assert captured["config"] == {"configurable": {"thread_id": "seller-chat:7:t-1"}}


def test_analysis_clarification_is_recorded_to_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """되묻기 문안도 스레드에 기록된다 — '되묻기 턴 미저장' 해소, 후속 발화 맥락."""
    from app.agents.seller import thread as seller_thread
    from app.agents.seller.context import SellerContext
    from app.agents.seller.orchestrator import PipelineResult

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        return PipelineResult(kind="clarification", text="어느 기간을 분석할까요?")

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    _collect_seller(_request("매출 그래프 보여줘"))

    turns = asyncio.run(
        seller_thread.load_recent_turns(SellerContext(seller_id=7, brand_id=3), "t-1")
    )
    assert turns == [
        ("user", "매출 그래프 보여줘"),
        ("assistant", "어느 기간을 분석할까요?"),
    ]


def test_confirm_outcome_is_recorded_with_placeholder_user_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """confirm 은 message 가 빈 계약 — '(초안 승인)' 플레이스홀더로 스레드에 남는다."""
    from app.agents.seller import thread as seller_thread
    from app.agents.seller.context import SellerContext

    async def fake_confirm(draft_id, *, seller_id, brand_id):
        return hitl.ConfirmOutcome("executed", "변경을 반영했습니다.")

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "confirm_draft", fake_confirm)

    _collect_seller(_confirm_request("d-1"))

    turns = asyncio.run(
        seller_thread.load_recent_turns(SellerContext(seller_id=7, brand_id=3), "t-1")
    )
    assert turns == [("user", "(초안 승인)"), ("assistant", "변경을 반영했습니다.")]


def test_routing_receives_recent_turns_from_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """입구 ③에서 스레드 최근 턴을 조회해 route_question 에 넘긴다."""
    from app.agents.seller import thread as seller_thread
    from app.agents.seller.context import SellerContext
    from app.agents.seller.schemas import RouteDecision

    ctx = SellerContext(seller_id=7, brand_id=3)
    asyncio.run(seller_thread.record_turn(ctx, "t-1", "어제 매출?", "120만원입니다."))
    seen: dict = {}

    async def capturing_route(question, context, recent_turns=(), screen=None):
        seen["turns"] = list(recent_turns)
        return RouteDecision(category="general", reason="stub", confidence=0.9)

    monkeypatch.setattr(seller_api, "route_question", capturing_route)
    monkeypatch.setattr(
        seller_api,
        "build_general_agent",
        lambda today, checkpointer=None: _StubStreamAgent([AIMessageChunk(content="네")]),
    )

    _collect_seller(_request("그럼 지난주는?"))

    assert seen["turns"] == [("user", "어제 매출?"), ("assistant", "120만원입니다.")]


# ─────────── S-4 화면 맥락 배선 (이슈 #118) ───────────


def test_routing_receives_screen_from_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[배선 가드] 입구가 `request.screen` 을 supervisor 로 흘린다.

    orchestrator·thread 단위 테스트만 있으면 여기 배선을 통째로 빠뜨려도 전부 초록이다 —
    `screen` 이 실제 요청에서 출발해 주입 지점에 도달하는지는 이 층에서만 확인된다.

    [#591] 구 단일 테스트는 supervisor 와 planner 를 한 요청으로 함께 봤다. 이제 두 주입
    지점이 서로 다른 입구(③ 라우팅 / ②.5 차트 게이트)에 달려 있어 요청을 나눠 확인한다.
    """
    from app.agents.seller.schemas import RouteDecision

    seen: dict = {}

    async def capturing_route(question, context, recent_turns=(), screen=None):
        seen["route_screen"] = screen
        return RouteDecision(category="general", reason="stub", confidence=0.9)

    agent = _StubStreamAgent([AIMessageChunk(content="신규 주문은 0건입니다.")])
    monkeypatch.setattr(seller_api, "route_question", capturing_route)
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    request = SellerChatRequest.model_validate(
        {
            "sessionId": "s-1",
            "threadId": "t-1",
            "message": "이 목록 왜 비어?",
            "screen": {"pageType": "seller_orders", "filters": {"status": "신규주문"}},
        }
    )
    events = _collect_seller(request)

    assert seen["route_screen"] is not None
    assert seen["route_screen"].page_type == "seller_orders"
    assert seen["route_screen"].filters == {"status": "신규주문"}
    assert "error" not in [event.get("type") for event in events]


def test_pipeline_receives_screen_from_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[배선 가드] 차트 게이트로 들어온 요청도 `request.screen` 을 planner 까지 흘린다."""
    from app.agents.seller.orchestrator import PipelineResult

    seen: dict = {}

    async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
        seen["pipeline_screen"] = screen
        return PipelineResult(kind="report", text="보고서")

    monkeypatch.setattr(seller_api, "route_question", _no_route)
    monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

    request = SellerChatRequest.model_validate(
        {
            "sessionId": "s-1",
            "threadId": "t-1",
            "message": "이 목록 그래프로 보여줘",
            "screen": {"pageType": "seller_orders", "filters": {"status": "신규주문"}},
        }
    )
    events = _collect_seller(request)

    assert seen["pipeline_screen"] is not None
    assert seen["pipeline_screen"].page_type == "seller_orders"
    assert seen["pipeline_screen"].filters == {"status": "신규주문"}
    # 스텁 파이프라인이 정상 종료했는지까지 본다 — 스텁이 예외로 죽으면 분석 레인이 사과 token
    # 으로 흘러 위 단언만으로는 "주입은 됐지만 흐름은 깨진" 상태를 구분하지 못한다.
    assert "error" not in [event.get("type") for event in events]


def test_routing_receives_none_screen_for_a_legacy_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`screen` 없는 기존 요청은 `None` 이 흘러 입력 문자열이 오늘과 같아진다."""
    from app.agents.seller.schemas import RouteDecision

    seen: dict = {}

    async def capturing_route(question, context, recent_turns=(), screen=None):
        seen["screen"] = screen
        return RouteDecision(category="general", reason="stub", confidence=0.9)

    monkeypatch.setattr(seller_api, "route_question", capturing_route)
    monkeypatch.setattr(
        seller_api,
        "build_general_agent",
        lambda today, checkpointer=None: _StubStreamAgent([AIMessageChunk(content="네")]),
    )

    _collect_seller(_request("안녕하세요"))

    assert seen["screen"] is None


# ─────────── 기간 환산 이관 (이슈 #346 — general 레인) ───────────


def test_general_stream_injects_code_resolved_period(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#346] 기간은 코드가 환산해 입력 메시지로 주입된다 — 프롬프트가 산수하지 않는다.

    이 배선이 빠지면 period.py 단위 테스트는 전부 초록인데 실 응답은 종전대로
    LLM 이 날짜를 지어내는 상태가 된다 — 레인 통일이 코드에만 있고 와이어에는 없다.
    """
    agent = _StubStreamAgent([AIMessageChunk(content="1,200,000원입니다.")])
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    _collect(_request("지난달 매출 알려줘"))

    expected = period.resolve_period("지난달", today=dt.date.today(), recent_default_days=7)
    assert agent.seen_input is not None
    content = agent.seen_input["messages"][0].content
    assert (
        f"[조회 기간] from={expected.date_from.isoformat()} "
        f"to={expected.date_to.isoformat()}" in content
    )
    assert "[판매자 질문] 지난달 매출 알려줘" in content


def test_general_stream_asks_back_before_calling_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """해석 불가 기간은 LLM·도구 호출 **앞에서** 되묻기로 끝난다(비용 0).

    빌더가 불리면 실패하도록 두어 "되묻기인데 모델은 이미 돌았다"를 구조적으로 막는다.
    """

    def _must_not_build(**_kwargs: object) -> object:
        raise AssertionError("되묻기 경로에서 general 에이전트를 빌드하면 안 된다")

    monkeypatch.setattr(seller_api, "build_general_agent", _must_not_build)

    events = _collect(_request("오늘 매출 얼마야?"))

    assert [e["type"] for e in events] == ["meta", "token", "done"]
    assert "오늘" in events[1]["data"]["text"]
    assert events[-1]["data"]["panel"] == "keep"  # 되묻기는 대화 — 패널 유지


def test_general_stream_enforces_period_upper_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """[#346 완료 조건 ②] general 레인에도 seller_period_max_days 상한이 걸린다.

    종전에는 이 환산이 프롬프트에만 있어 상한·0/음수 가드가 통째로 비켜갔다 —
    "최근 999999일" 이 그대로 도구 인자가 될 수 있었다.
    """

    def _must_not_build(**_kwargs: object) -> object:
        raise AssertionError("상한 위반은 모델 호출 전에 끊어야 한다")

    monkeypatch.setattr(seller_api, "build_general_agent", _must_not_build)

    events = _collect(_request("최근 999999일 매출 얼마야?"))

    limit = get_settings().seller_period_max_days
    assert f"{limit}일 이내" in events[1]["data"]["text"]


def test_general_stream_discloses_supplemented_period(monkeypatch: pytest.MonkeyPatch) -> None:
    """코드가 값을 보충한 해석("이번 달")은 확인 대신 **고지**하고 바로 실행한다.

    분석 레인은 실행 전에 확인을 받지만(#345) general 은 조회 한두 번이라 비용이
    비대칭이다 — 확인 왕복 대신 무엇으로 봤는지를 먼저 밝혀 조용한 대체를 막는다.
    """
    agent = _StubStreamAgent([AIMessageChunk(content="1,200,000원입니다.")])
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("이번 달 매출 얼마야?"))

    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert "이번 달" in texts[0] and "기간으로 봤습니다" in texts[0]
    assert texts[-1] == "1,200,000원입니다."  # 고지 뒤에 모델 응답이 이어진다
    assert agent.seen_input is not None, "고지만 하고 실행을 멈추면 안 된다"


def test_general_stream_does_not_disclose_plain_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """확인이 필요 없는 어휘("지난달")에는 고지를 붙이지 않는다.

    전부 고지하면 "재고 얼마 남았어?" 같은 기간 무관 질문에도 무관한 한 줄이 매번
    따라붙어, 정작 필요한 고지가 묻힌다.
    """
    agent = _StubStreamAgent([AIMessageChunk(content="1,200,000원입니다.")])
    monkeypatch.setattr(seller_api, "build_general_agent", lambda today, checkpointer=None: agent)

    events = _collect(_request("지난달 매출 알려줘"))

    texts = [e["data"]["text"] for e in events if e["type"] == "token"]
    assert texts == ["1,200,000원입니다."]


# ── [#622 결정 — 이슈 ⑤] _ensure_draft_category 카테고리 복구 + preview note ──────


def test_ensure_draft_category_revives_pending_category_on_modify_turn() -> None:
    """수정 턴에서 에이전트가 카테고리를 비웠으면 이전 초안 값을 되살린다(① 경로)."""
    from app.agents.seller import category_catalog, draft_session
    from app.agents.seller.schemas import DraftChange, DraftProposal

    category_id = category_catalog.all_entries()[0].id
    pending = draft_session.PendingCreate(
        draft_id="d-1",
        image_urls=(),
        analysis=None,
        changes={"category": category_id, "name": "감귤청"},
    )
    proposal = DraftProposal(
        op="create",
        product_id=None,
        changes=[DraftChange(field="price", before="", after="12900")],
        summary="가격만 수정",
    )

    result, revived = asyncio.run(
        seller_api._ensure_draft_category(
            proposal, message="가격만 12900원으로 바꿔줘", analysis=None, pending=pending
        )
    )

    assert revived is True
    category_change = next(c for c in result.changes if c.field == "category")
    assert category_change.after == category_id


def test_ensure_draft_category_not_revived_when_agent_already_chose_valid_category() -> None:
    """에이전트가 이미 유효한 카테고리를 골랐으면 되살릴 필요가 없다 — revived=False."""
    from app.agents.seller import category_catalog, draft_session
    from app.agents.seller.schemas import DraftChange, DraftProposal

    entries = category_catalog.all_entries()
    chosen_id, pending_id = entries[0].id, entries[1].id
    pending = draft_session.PendingCreate(
        draft_id="d-1", image_urls=(), analysis=None, changes={"category": pending_id}
    )
    proposal = DraftProposal(
        op="create",
        product_id=None,
        changes=[DraftChange(field="category", before="", after=chosen_id)],
        summary="새 상품 등록",
    )

    result, revived = asyncio.run(
        seller_api._ensure_draft_category(
            proposal, message="상품 등록해줘", analysis=None, pending=pending
        )
    )

    assert revived is False
    category_change = next(c for c in result.changes if c.field == "category")
    assert category_change.after == chosen_id  # 되살리지 않고 에이전트 선택 유지


def test_ensure_draft_category_not_revived_when_no_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대기 중인 초안이 없으면(신규 등록 첫 턴 등) 되살릴 이전 값 자체가 없다."""
    from app.agents.seller.schemas import DraftChange, DraftProposal

    async def _no_match(message, *, hint=None):
        return None

    monkeypatch.setattr(seller_api.category_resolver, "resolve_category", _no_match)

    proposal = DraftProposal(
        op="create",
        product_id=None,
        changes=[DraftChange(field="price", before="", after="12900")],
        summary="새 상품 등록",
    )

    result, revived = asyncio.run(
        seller_api._ensure_draft_category(
            proposal, message="아무 카테고리나", analysis=None, pending=None
        )
    )

    assert revived is False
    assert all(c.field != "category" for c in result.changes)
