"""구매자 SSE `progress` 이벤트 회귀 (이슈 #289) — 계약 미등재·플래그 gated.

`app/core/config.py::progress_events_enabled` 기본값은 False. 플래그 off = 기존 7종
이벤트와 순서가 바이트 동일해야 하고, on = `progress`가 스트림 첫 프레임으로 추가되며
나머지 이벤트 순서는 불변이어야 한다(§3.1 순서 계약 불변, `docs/api-spec.md` §3.1).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import app.agents.buyer.graph as buyer_graph
from app.agents.buyer._frames import progress as progress_frame
from app.agents.buyer.cart.state import get_cart_store
from app.agents.buyer.graph import run_buyer_turn as _production_run_buyer_turn
from app.agents.buyer.session_state import context_thread_key
from app.agents.seller import hitl
from app.api import seller as seller_api
from app.api.deps import buyer_owner_id
from app.core import session_context
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.observability import RequestObservation
from app.core.session_context import BuyerSessionInput, SessionStateUnavailable
from app.schemas.seller import SellerChatRequest
from app.schemas.spring import AddToCartResult, CartView, ProductSearchResult
from tests._fakes import DEFAULT_DECOMPOSE, DEFAULT_PRODUCTS, FakeLLM


def _member() -> Identity:
    return Identity(user_id="42", is_guest=False, seller_id=None, subject="42")


def _req(message: str, thread_id: str) -> SimpleNamespace:
    return SimpleNamespace(session_id="s-289", thread_id=thread_id, message=message)


async def _committed_observer(request, identity):  # noqa: ANN001
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "guest" if identity.is_guest else "member",
            buyer_owner_id(identity, get_settings()),
        )
    )
    return SimpleNamespace(
        context_id=context.context_id,
        request_id="req-289",
        record_model_call=lambda *_: None,
    )


async def run_buyer_turn(request, identity, **kwargs):  # noqa: ANN001
    observer = await _committed_observer(request, identity)
    async for frame in _production_run_buyer_turn(
        request,
        identity,
        observer=observer,
        **kwargs,
    ):
        yield frame


async def _thread_key_for(thread_id: str) -> str:
    observer = await _committed_observer(_req("", thread_id), _member())
    return context_thread_key(observer.context_id, thread_id)


async def _collect(gen) -> list[dict]:
    events: list[dict] = []
    async for frame in gen:
        line = frame.strip()
        assert line.startswith("data:") and frame.endswith("\n\n")  # SSE 와이어 규약
        events.append(json.loads(line[len("data:") :].strip()))
    return events


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def _make_search(products):
    async def _search(filters, exclude_product_ids=None):
        return ProductSearchResult(products=list(products), total_count=len(products))

    return _search


class _RecordingPush:
    def __init__(self) -> None:
        self.pushes: list = []

    async def __call__(self, push) -> bool:
        self.pushes.append(push)
        return True


async def _run_recommend(thread_id: str) -> list[dict]:
    return await _collect(
        run_buyer_turn(
            _req("무선 이어폰 추천해줘", thread_id),
            _member(),
            llm=FakeLLM(decompose=DEFAULT_DECOMPOSE),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
        )
    )


async def _run_cart_add(thread_id: str, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    import app.services.spring_client as sc

    async def fake_add(req):
        return AddToCartResult(success=True, cart_item_id=901)

    async def fake_get(*, user_id=None, guest_id=None):
        return CartView(items=[])

    monkeypatch.setattr(sc, "add_to_cart", fake_add)
    monkeypatch.setattr(sc, "get_cart", fake_get)
    store = await get_cart_store()
    key = await _thread_key_for(thread_id)
    await store.set_last_reco(key, [(101, "이어폰A")])
    llm = FakeLLM(decompose={"intent": "cart_add", "cart": {"productId": 101, "quantity": 1}})
    return await _collect(run_buyer_turn(_req("담아줘", thread_id), _member(), llm=llm))


async def _run_order_status(thread_id: str) -> list[dict]:
    async def fetch(user_id: int):
        return SimpleNamespace(orders=[])

    llm = FakeLLM(decompose={"intent": "order_status", "filters": {}})
    return await _collect(
        run_buyer_turn(
            _req("배송 상태 알려줘", thread_id), _member(), llm=llm, order_status_fn=fetch
        )
    )


async def _run_general(thread_id: str) -> list[dict]:
    llm = FakeLLM(decompose={"intent": "general", "reply": "안녕하세요! 무엇을 도와드릴까요?"})
    return await _collect(run_buyer_turn(_req("오늘 날씨 어때?", thread_id), _member(), llm=llm))


# ─────────── AC-4.3 — progress() 프레임 헬퍼: message 생략/포함 ───────────


def test_progress_frame_omits_message_when_empty() -> None:
    frame = progress_frame("analyzing", "")
    payload = json.loads(frame[len("data: ") :].strip())
    assert payload == {"type": "progress", "data": {"stage": "analyzing"}}


def test_progress_frame_omits_message_when_none() -> None:
    frame = progress_frame("analyzing")
    payload = json.loads(frame[len("data: ") :].strip())
    assert "message" not in payload["data"]


def test_progress_frame_includes_message_when_present() -> None:
    frame = progress_frame("analyzing", "요청을 확인하고 있어요")
    payload = json.loads(frame[len("data: ") :].strip())
    assert payload == {
        "type": "progress",
        "data": {"stage": "analyzing", "message": "요청을 확인하고 있어요"},
    }


# ─────────── AC-4.1/2 — 대표 턴 유형: 플래그 off 바이트 동일 / on 첫 프레임 progress ───────────


async def test_progress_flag_off_wire_identical_recommend() -> None:
    """플래그 off(기본값) — 추천 턴에 progress 타입이 없다."""
    events = await _run_recommend("reco-289-off")
    assert "progress" not in _types(events)
    assert _types(events)[-1] == "done"


async def test_progress_flag_on_prepends_progress_recommend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """플래그 on — progress 가 정확히 1회·맨 앞, 이후 시퀀스는 off 와 동일(추천 턴)."""
    off_types = _types(await _run_recommend("reco-289-cmp-off"))
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    on_events = await _run_recommend("reco-289-cmp-on")
    on_types = _types(on_events)
    assert on_types == ["progress"] + off_types
    assert on_types.count("progress") == 1
    assert on_events[0]["data"] == {
        "stage": "analyzing",
        "message": get_settings().progress_analyzing_message,
    }


async def test_progress_flag_off_wire_identical_cart_add(monkeypatch: pytest.MonkeyPatch) -> None:
    events = await _run_cart_add("cart-289-off", monkeypatch)
    assert "progress" not in _types(events)
    assert _types(events)[-1] == "done"


async def test_progress_flag_on_prepends_progress_cart_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """플래그 on — 비추천(담기) 턴에서도 progress 가 맨 앞에 오고 이후 순서는 불변."""
    off_types = _types(await _run_cart_add("cart-289-cmp-off", monkeypatch))
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    on_types = _types(await _run_cart_add("cart-289-cmp-on", monkeypatch))
    assert on_types == ["progress"] + off_types


async def test_progress_flag_off_wire_identical_order_status() -> None:
    events = await _run_order_status("order-289-off")
    assert "progress" not in _types(events)
    assert _types(events) == ["token", "done"]


async def test_progress_flag_on_prepends_progress_order_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    off_types = _types(await _run_order_status("order-289-cmp-off"))
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    on_types = _types(await _run_order_status("order-289-cmp-on"))
    assert on_types == ["progress"] + off_types


async def test_progress_flag_off_wire_identical_general() -> None:
    events = await _run_general("general-289-off")
    assert "progress" not in _types(events)
    assert _types(events) == ["token", "done"]


async def test_progress_flag_on_prepends_progress_general(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """플래그 on — 비추천(일반 대화) 턴에서도 progress 가 맨 앞에 오고 이후 순서는 불변."""
    off_types = _types(await _run_general("general-289-cmp-off"))
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    on_types = _types(await _run_general("general-289-cmp-on"))
    assert on_types == ["progress"] + off_types


async def test_progress_message_key_absent_when_config_message_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """config `progress_analyzing_message` 가 빈 문자열이면 실제 턴에서도 message 키가 없다."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    monkeypatch.setattr(get_settings(), "progress_analyzing_message", "")
    events = await _run_general("general-289-empty-message")
    assert events[0]["type"] == "progress"
    assert events[0]["data"] == {"stage": "analyzing"}


async def test_progress_flag_on_llm_unavailable_yields_error_not_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`progress`는 0~1회다 — 플래그 on 이어도 emit 지점(decompose 직전, L531) **이전에**
    턴이 끝나면 0회다. LLM 미구성(`llm is None`, L436)은 그 반례: emit 에 도달하지 못하고
    `error{LLM_UNAVAILABLE}`를 첫(이자 유일한) 프레임으로 낸다.
    """
    monkeypatch.setattr(buyer_graph, "get_llm", lambda: None)
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    events = await _collect(
        run_buyer_turn(_req("아무거나", "thread-llm-unavail-289"), _member(), llm=None)
    )
    assert _types(events) == ["error"]
    assert events[0]["data"]["code"] == "LLM_UNAVAILABLE"


# ─────────── AC-4.4 — 스트림 전 오류 봉투는 progress on 에서도 보존된다 ───────────


async def test_session_state_unavailable_stays_pre_stream_when_progress_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """context_id 미확보(SessionStateUnavailable)는 progress on 이어도 첫 yield 전에 raise된다.

    emit 이 프렐류드(context_id 검증)보다 **뒤**라는 설계(§289 emit 지점)를 고정한다 — 이걸
    깨고 emit 을 위로 올리면 이 예외가 200 헤더 전송 뒤의 in-stream error 로 바뀌어
    §2.5 스트림 전 오류 봉투 계약을 위반한다.
    """
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    observer = SimpleNamespace(
        context_id=None, request_id="req-289-503", record_model_call=lambda *_: None
    )
    request = _req("아무 말이나", "thread-503")
    frames: list[str] = []
    with pytest.raises(SessionStateUnavailable):
        async for frame in _production_run_buyer_turn(
            request, _member(), llm=FakeLLM(), observer=observer
        ):
            frames.append(frame)
    assert frames == []


# ─────────── AC-4.5 — first_event_at 은 progress 로, first_text_token_at 은 무영향 ───────────


async def test_first_event_at_moves_to_progress_first_text_token_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    request = _req("오늘 날씨 어때?", "thread-observe-289")
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "member",
            buyer_owner_id(_member(), get_settings()),
        )
    )
    observer = RequestObservation(
        request_id="req-289-observe",
        conversation_id=request.session_id,
        thread_id=request.thread_id,
        user_id="42",
        brand_id=None,
        role="buyer",
        store=None,  # type: ignore[arg-type]
        message_length=len(request.message),
        message_hash="h",
        started=0.0,
        pending_message=request.message,
        pending_key="k",
    )
    observer.context_id = context.context_id

    llm = FakeLLM(decompose={"intent": "general", "reply": "안녕하세요"})
    frame_types: list[str] = []
    t = 0.0
    async for frame in _production_run_buyer_turn(request, _member(), llm=llm, observer=observer):
        t += 1.0
        observer.record_frame(frame, t)
        frame_types.append(json.loads(frame.strip()[len("data:") :].strip())["type"])

    first_event_at = observer.first_event_at
    first_text_token_at = observer.first_text_token_at
    assert frame_types[0] == "progress"
    assert first_event_at == 1.0
    assert first_text_token_at is not None
    assert first_event_at is not None
    assert first_text_token_at > first_event_at


# ─────────── AC-4.6 — 판매자 스트림은 플래그 무관 무영향 ───────────


def test_seller_analysis_progress_unaffected_by_buyer_progress_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """판매자 자신의 `progress`(§3.2 analysis 레인, `{"text": ...}`)는 구매자
    `progress_events_enabled` 플래그와 무관하게 동일하게 나온다 — 두 `progress`가 서로
    오염되지 않음을 고정한다.

    F-3 리뷰 반영: 종전에는 general 레인(`progress` 자체가 없는 레인)만 돌려
    `"progress" not in off_types`를 단언했는데, 이는 이 PR 과 무관하게 항상 참이라 회귀가
    나도 깨지지 않는 단언이었다. analysis 레인을 직접 돌려 판매자의 실제 `progress` 프레임을
    검증한다.
    """
    from app.agents.seller.orchestrator import PipelineResult
    from app.agents.seller.schemas import RouteDecision

    hitl.set_checkpointer(InMemorySaver())
    try:
        identity = Identity(user_id=None, is_guest=False, seller_id="7", brand_id="3")
        request = SellerChatRequest(
            session_id="s-289-seller", thread_id="t-289-seller", message="지난달 매출 분석해줘"
        )

        async def _route_stub(question, context, recent_turns=(), screen=None):
            return RouteDecision(category="analysis", reason="stub", confidence=0.9)

        async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
            await emit("매출 이상 분석 중…")
            return PipelineResult(kind="report", text="6월 매출 보고서 본문")

        def _run() -> list[str]:
            monkeypatch.setattr(seller_api, "route_question", _route_stub)
            monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

            async def _drive() -> list[str]:
                return [line async for line in seller_api._seller_stream(request, identity)]

            return asyncio.run(_drive())

        off_lines = _run()
        monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
        on_lines = _run()
        assert on_lines == off_lines
        off_types = [json.loads(line[len("data: ") :].strip())["type"] for line in off_lines]
        assert off_types == ["meta", "progress", "token", "done"]
        progress_payload = json.loads(off_lines[1][len("data: ") :].strip())
        assert progress_payload["data"] == {"text": "매출 이상 분석 중…"}
    finally:
        hitl.set_checkpointer(None)


# ─────────── R4-1 — 운영 레인(jwks)에서 플래그가 켜져 있으면 기동 실패 ───────────


def test_progress_events_enabled_fails_startup_in_jwks_mode() -> None:
    """운영(jwks)에서 PROGRESS_EVENTS_ENABLED=true 이면 Settings 기동이 실패한다.

    계약 미등재 이벤트가 .env 실수 한 줄로 운영 와이어에 나가는 사고를 막는
    fail-closed 가드다(`_require_pepper_in_prod`, 이 파일의 pepper/토큰 가드와 같은 관용구).
    """
    from app.core.config import Settings

    with pytest.raises(Exception, match="PROGRESS_EVENTS_ENABLED"):
        Settings(
            auth_mode="jwks",
            jwks_url="http://x",
            pii_hash_pepper="p",
            internal_api_token="tok",
            google_api_key="k",
            progress_events_enabled=True,
        )


def test_progress_events_disabled_succeeds_startup_in_jwks_mode() -> None:
    """가드는 정상 운영 설정(플래그 off)까지 막지 않는다."""
    from app.core.config import Settings

    Settings(
        auth_mode="jwks",
        jwks_url="http://x",
        pii_hash_pepper="p",
        internal_api_token="tok",
        google_api_key="k",
        progress_events_enabled=False,
    )  # ok


def test_progress_events_enabled_fails_startup_in_staging_even_with_dev_auth() -> None:
    """R5-1 — `auth_mode`는 인증 방식일 뿐 실트래픽을 보장하지 않는다.

    dev 인증으로 도는 staging(실 FE 가 붙을 수 있음)도 `app_environment` 축으로 막아야
    한다 — jwks 단독 판정이면 이 조합이 가드를 그냥 통과해 계약 미등재 이벤트가 실
    클라이언트로 나간다(리뷰 4차 지적).
    """
    from app.core.config import Settings

    with pytest.raises(Exception, match="PROGRESS_EVENTS_ENABLED"):
        Settings(
            auth_mode="dev",
            app_environment="staging",
            progress_events_enabled=True,
        )


def test_progress_events_enabled_succeeds_startup_in_local_dev() -> None:
    """가드를 넓혀도 로컬(기본 `local`)·실측 하네스 경로는 계속 살아 있다.

    실측 하네스(`evals/first_event_budget/measure_first_event.py`)는 `AUTH_MODE=dev`만
    핀으로 박고 `app_environment`는 기본값(`local`)이라 이 조합과 정확히 같다.
    """
    from app.core.config import Settings

    Settings(auth_mode="dev", app_environment="local", progress_events_enabled=True)  # ok
