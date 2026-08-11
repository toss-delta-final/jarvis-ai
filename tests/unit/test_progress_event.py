"""구매자 SSE `progress` 이벤트 회귀 (이슈 #289, 기본 on 전환은 #396) — 계약 등재 완료.

`app/core/config.py::progress_events_enabled` 기본값은 True(#396, 2026-08-06 FE 구현
완료로 해제). 플래그 off(명시적 escape hatch) = 기존 7종 이벤트와 순서가 바이트 동일해야
하고, on(기본) = `progress`가 스트림 첫 프레임으로 추가되며 나머지 이벤트 순서는
불변이어야 한다(§3.1 순서 계약 불변, `docs/api-spec.md` §3.1).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
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
from app.core.clock import KST
from app.core.config import get_settings
from app.core.observability import RequestObservation
from app.core.session_context import BuyerSessionInput, SessionStateUnavailable
from app.schemas.chat import CamelModel, ProgressData
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


async def _run_recommend_no_category_signal(thread_id: str) -> list[dict]:
    """categoryQueries 가 없고 prior 도 없는 — 카테고리 신호가 전혀 없는 추천 턴.

    `resolve_category_action` 의 `carry` 판정은 `prior is not None` 가드가 있어, 이 조건에서는
    `prior` 없는 첫 턴이 `else` 분기로 떨어진다 — 그 분기가 곧 "매핑을 태운다"는 아니다(#396
    라운드 1 mapping honesty 회귀).
    """
    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "semanticQuery": "아무거나 괜찮은 걸로",
            "filters": {"priceMax": 50000},
            "case": 2,
        }
    )
    return await _collect(
        run_buyer_turn(
            _req("아무거나 괜찮은 걸로 추천해줘", thread_id),
            _member(),
            llm=llm,
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


async def _map_stub(**kwargs):  # noqa: ANN003
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    return CategoryMapping(legs=[("무선이어폰", "무선 이어폰")])


async def _run_recommend_with_mapping(thread_id: str) -> list[dict]:
    """매핑을 실제로 태우고(mapping) 검색·rerank·push 도 성공하는(searching/reranking/publishing)
    "느린" 추천 턴 — stage 4종(mapping/searching/reranking/publishing)이 한 턴에서 재현된다.
    """
    return await _collect(
        run_buyer_turn(
            _req("무선 이어폰 추천해줘", thread_id),
            _member(),
            llm=FakeLLM(decompose=DEFAULT_DECOMPOSE),
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
            map_categories=_map_stub,
        )
    )


async def _run_recommend_needs_relaxation(thread_id: str) -> list[dict]:
    """본 검색이 0건이라 자동 완화 루프가 실제로 probe 하는 턴 — relaxing 이 정직하게 나온다."""
    from app.schemas.spring import SpringProduct

    product = SpringProduct(
        product_id=201,
        name="무선 이어폰 라이트",
        price=30000,
        rating=4.2,
        category="무선이어폰",
        brand="B",
    )

    async def _search(filters, exclude_product_ids=None):
        if filters.rating_min is not None and filters.rating_min >= 4.5:
            return ProductSearchResult(products=[], total_count=0)
        return ProductSearchResult(products=[product], total_count=1)

    llm = FakeLLM(decompose={"intent": "recommend", "filters": {"ratingMin": 4.5}, "case": 2})
    return await _collect(
        run_buyer_turn(
            _req("평점 낮아도 되니까 이어폰 추천해줘", thread_id),
            _member(),
            llm=llm,
            search=_search,
            push_fn=_RecordingPush(),
        )
    )


async def _run_recommend_needs_expansion(thread_id: str) -> list[dict]:
    """매핑이 canonical 을 못 내(D2 mapping_failed) 니즈 전개가 실제로 발동하는 턴."""
    from app.agents.buyer.recommendation.category_mapping import CategoryMapping

    async def _map_unresolved(**kwargs):  # noqa: ANN003
        return CategoryMapping(legs=[], unresolved=["집들이 선물"])

    async def _expand(message, *, llm, settings, observer=None):
        return ["캔들", "수납함"]

    llm = FakeLLM(
        decompose={
            "intent": "recommend",
            "case": 3,
            "categoryQueries": [{"category": None, "query": "집들이 선물"}],
            "filters": {},
        }
    )
    return await _collect(
        run_buyer_turn(
            _req("집들이 선물로 뭐 사갈까", thread_id),
            _member(),
            llm=llm,
            search=_make_search(DEFAULT_PRODUCTS),
            push_fn=_RecordingPush(),
            map_categories=_map_unresolved,
            expand_needs=_expand,
        )
    )


async def _run_recommend_with_retry_signal(thread_id: str) -> list[dict]:
    """#406 — 본검색이 실제 재시도 진입을 알린 턴의 retrying 문구 driver."""
    from app.services import spring_client

    async def _search(filters, exclude_product_ids=None):  # noqa: ANN001
        spring_client.notify_search_retry()
        return ProductSearchResult(
            products=list(DEFAULT_PRODUCTS), total_count=len(DEFAULT_PRODUCTS)
        )

    return await _collect(
        run_buyer_turn(
            _req("무선 이어폰 추천해줘", thread_id),
            _member(),
            llm=FakeLLM(decompose=DEFAULT_DECOMPOSE),
            search=_search,
            push_fn=_RecordingPush(),
        )
    )


_NEW_STAGE_DRIVERS = {
    "mapping": _run_recommend_with_mapping,
    "searching": _run_recommend_with_mapping,
    "reranking": _run_recommend_with_mapping,
    "publishing": _run_recommend_with_mapping,
    "relaxing": _run_recommend_needs_relaxation,
    "retrying": _run_recommend_with_retry_signal,
    "expanding": _run_recommend_needs_expansion,
}


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


# ─────────── R6-1 — progress() 도 다른 이벤트와 같은 CamelModel 직렬화 경로를 쓴다 ───────────


def test_progress_data_is_camel_model_and_serializes_like_the_wire() -> None:
    """`ProgressData` 는 다른 SSE 페이로드(`TokenData` 등)와 같은 `CamelModel` 규약을 따른다.

    이 테스트가 깨진다는 것은 `progress()` 가 다시 raw dict 로 우회했다는 뜻이다 — "다른
    이벤트와 같은 경로로 직렬화된다"는 이 라운드의 요지 자체를 고정한다.
    """
    assert issubclass(ProgressData, CamelModel)

    with_message = ProgressData(stage="analyzing", message="요청을 확인하고 있어요")
    assert with_message.model_dump(by_alias=True, exclude_none=True) == {
        "stage": "analyzing",
        "message": "요청을 확인하고 있어요",
    }

    without_message = ProgressData(stage="analyzing", message=None)
    assert without_message.model_dump(by_alias=True, exclude_none=True) == {
        "stage": "analyzing",
    }


_PROGRESS_STAGES = (
    "analyzing",
    "mapping",
    "expanding",
    "searching",
    "retrying",
    "relaxing",
    "reranking",
    "publishing",
)


def test_progress_data_accepts_all_eight_registered_stages() -> None:
    """#406 — 계약(§3.1 v0.32.4)이 확정한 어휘 8종 전부를 `ProgressData` 가 받아들인다."""
    for stage in _PROGRESS_STAGES:
        assert ProgressData(stage=stage).stage == stage


def test_progress_data_rejects_unregistered_stage() -> None:
    """R7-1 — `stage` 는 `Literal`(8종)이라 계약(§3.1)에 없는 값은 생성 자체가 거부된다.

    `retrying`은 #406으로 §3.1 v0.32.4에 등재돼 어휘가 8종이 됐다. 계약이 확정한 어휘를
    코드가 강제한다는 요지를 고정한다 — 어휘를 넓히려면 §3.1 개정과 이 `Literal`을 함께 고친다.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProgressData(stage="bogus")


def test_progress_events_enabled_defaults_to_true() -> None:
    """#396 — 인자 없이 만든 `Settings()`의 기본값이 True 인지 직접 고정한다.

    다른 모든 테스트가 값을 명시로 주입해서 돌기 때문에, 이 테스트가 없으면 기본값 자체가
    틀려도(예: 되돌림 실수로 다시 False 가 돼도) 아무도 못 잡는다.
    """
    from app.core.config import Settings

    assert Settings().progress_events_enabled is True


# ─────────── AC-4.1/2 — 대표 턴 유형: 플래그 off 바이트 동일 / on 첫 프레임 progress ───────────


async def test_progress_flag_off_wire_identical_recommend(monkeypatch: pytest.MonkeyPatch) -> None:
    """플래그 off(명시 강제, #396 이후 기본값은 on) — 추천 턴에 progress 타입이 없다."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
    events = await _run_recommend("reco-289-off")
    assert "progress" not in _types(events)
    assert _types(events)[-1] == "done"


async def test_progress_flag_on_prepends_progress_recommend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """플래그 on — 첫 프레임은 `progress{analyzing}`, progress 를 뺀 나머지는 off 와 동일(추천 턴).

    #396(다회 emit)으로 추천 턴은 `progress` 가 여러 번(analyzing/mapping/searching/
    reranking/publishing 등) 나갈 수 있으므로, "정확히 1회"·"progress 1개 + off 전체" 단언은
    더 이상 성립하지 않는다 — 첫 프레임 신원과 "progress 를 뺀 시퀀스는 불변"으로 정확히
    갱신한다(단언을 느슨하게 하는 게 아니라 다른 사실을 고정하는 것).
    """
    monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
    off_types = _types(await _run_recommend("reco-289-cmp-off"))
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    on_events = await _run_recommend("reco-289-cmp-on")
    on_types = _types(on_events)
    assert on_types[0] == "progress"
    assert on_events[0]["data"] == {
        "stage": "analyzing",
        "message": get_settings().progress_analyzing_message,
    }
    assert [t for t in on_types if t != "progress"] == off_types


async def test_progress_flag_off_wire_identical_cart_add(monkeypatch: pytest.MonkeyPatch) -> None:
    """플래그 off(명시 강제, #396 이후 기본값은 on)."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
    events = await _run_cart_add("cart-289-off", monkeypatch)
    assert "progress" not in _types(events)
    assert _types(events)[-1] == "done"


async def test_progress_flag_on_prepends_progress_cart_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """플래그 on — 비추천(담기) 턴에서도 progress 가 맨 앞에 오고 이후 순서는 불변."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
    off_types = _types(await _run_cart_add("cart-289-cmp-off", monkeypatch))
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    on_types = _types(await _run_cart_add("cart-289-cmp-on", monkeypatch))
    assert on_types == ["progress"] + off_types


async def test_progress_flag_off_wire_identical_order_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """플래그 off(명시 강제, #396 이후 기본값은 on)."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
    events = await _run_order_status("order-289-off")
    assert "progress" not in _types(events)
    assert _types(events) == ["token", "done"]


async def test_progress_flag_on_prepends_progress_order_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
    off_types = _types(await _run_order_status("order-289-cmp-off"))
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    on_types = _types(await _run_order_status("order-289-cmp-on"))
    assert on_types == ["progress"] + off_types


async def test_progress_flag_off_wire_identical_general(monkeypatch: pytest.MonkeyPatch) -> None:
    """플래그 off(명시 강제, #396 이후 기본값은 on)."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
    events = await _run_general("general-289-off")
    assert "progress" not in _types(events)
    assert _types(events) == ["token", "done"]


async def test_progress_flag_on_prepends_progress_general(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """플래그 on — 비추천(일반 대화) 턴에서도 progress 가 맨 앞에 오고 이후 순서는 불변."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
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


# ─────────── #396 — 느린 추천 턴의 stage 시퀀스 (다회 emit·어휘 확장) ───────────


async def test_progress_stage_sequence_for_slow_recommend_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """매핑을 태우고 검색·rerank·push 도 성공하는 턴의 stage 시퀀스를 고정한다.

    동시에 **기존 6종의 상대 순서가 불변**임도 같은 턴으로 검증한다 — progress 를 제거한
    타입 시퀀스가 flag-off 시퀀스와 정확히 동일해야 한다.
    """
    monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
    off_types = _types(await _run_recommend_with_mapping("stage-seq-289-off"))
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    on_events = await _run_recommend_with_mapping("stage-seq-289-on")
    on_types = _types(on_events)
    stages = [e["data"]["stage"] for e in on_events if e["type"] == "progress"]
    assert stages == ["analyzing", "mapping", "searching", "reranking", "publishing"]
    assert [t for t in on_types if t != "progress"] == off_types


async def test_progress_mapping_not_repeated_after_expansion_remap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """전개 성공 후의 재매핑은 같은 논리 단계의 연장이라 `mapping` 을 다시 내지 않는다."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    events = await _run_recommend_needs_expansion("expand-mapping-once-289")
    stages = [e["data"]["stage"] for e in events if e["type"] == "progress"]
    assert stages.count("mapping") == 1
    assert "expanding" in stages
    assert stages.index("mapping") < stages.index("expanding")


# ─────────── #396 — 분기 정직성: relaxing 은 실제로 probe 한 턴에서만 나온다 ───────────


async def test_progress_relaxing_absent_when_turn_has_results_without_relaxation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0건이 아니라 자동 완화 루프가 아예 안 도는 턴에는 `relaxing` 이 없다(거짓 신호 금지)."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    events = await _run_recommend_with_mapping("relax-honesty-nonzero-289")
    stages = [e["data"]["stage"] for e in events if e["type"] == "progress"]
    assert "relaxing" not in stages


async def test_progress_relaxing_present_when_auto_relax_actually_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0건이라 자동 완화 루프가 실제로 probe 한 턴에는 `relaxing` 이 나온다."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    events = await _run_recommend_needs_relaxation("relax-honesty-zero-289")
    stages = [e["data"]["stage"] for e in events if e["type"] == "progress"]
    assert "relaxing" in stages


# ─────────── #396 라운드 1 — 분기 정직성: mapping 은 카테고리 신호가 있을 때만 나온다 ───────────


async def test_progress_mapping_absent_when_turn_has_no_category_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """카테고리 신호가 전혀 없는 첫 턴(categoryQueries 없음·prior 없음)에는 `mapping` 이 없다.

    `_prepare_recommendation` 의 `else` 분기는 `carry`(리파인 승계) 판정이 `prior is not None`
    가드를 요구해서, prior 없는 첫 턴은 신호가 하나도 없어도 이 분기로 떨어진다 — 그 분기에
    들어왔다는 사실 자체는 "매핑을 태운다"는 뜻이 아니다. 매퍼는 이 턴에서 실제로 아무 일도
    하지 않는데 예전 구현은 여기서도 `mapping` 을 냈다(#396 라운드 1 결함, 거짓 신호).
    같은 턴에서 `analyzing`·`searching` 은 여전히 나온다는 것도 함께 단언한다 — 안 그러면
    "그냥 progress 를 다 껐다"와 구별되지 않는다.
    """
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    events = await _run_recommend_no_category_signal("mapping-honesty-no-signal-289")
    stages = [e["data"]["stage"] for e in events if e["type"] == "progress"]
    assert "mapping" not in stages
    assert "analyzing" in stages
    assert "searching" in stages


# ─────────── #396 — 새 stage 6종의 문구 config 주입: 비우면 message 키가 없다 ───────────


@pytest.mark.parametrize(
    "stage,message_field",
    [
        ("mapping", "progress_mapping_message"),
        ("expanding", "progress_expanding_message"),
        ("searching", "progress_searching_message"),
        ("retrying", "progress_retrying_message"),
        ("relaxing", "progress_relaxing_message"),
        ("reranking", "progress_reranking_message"),
        ("publishing", "progress_publishing_message"),
    ],
)
async def test_progress_message_key_absent_for_new_stage_when_config_empty(
    stage: str, message_field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """새 stage 6종 각각 config 문구를 비우면 그 프레임의 `data` 에 `message` 키가 없다.

    (기존 `analyzing` 테스트와 같은 규약 — `test_progress_message_key_absent_when_config_message_empty`.)
    """
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    if stage == "retrying":
        monkeypatch.setattr(get_settings(), "spring_max_retries", 1)
    monkeypatch.setattr(get_settings(), message_field, "")
    events = await _NEW_STAGE_DRIVERS[stage](f"empty-msg-{stage}-289")
    frame = next(e for e in events if e["type"] == "progress" and e["data"]["stage"] == stage)
    assert frame["data"] == {"stage": stage}


# ─────────── #396 — 플래그 off: 새 emit 지점도 하나도 안 나간다 ───────────


async def test_progress_flag_off_no_new_stage_frames_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """플래그 off 면 mapping/searching/reranking/publishing 등 새 emit 지점도 전부 억제된다."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
    events = await _run_recommend_with_mapping("off-no-leak-289")
    assert "progress" not in _types(events)


async def test_progress_flag_off_no_relaxing_or_expanding_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """플래그 off 면 relaxing·expanding 처럼 조건부 emit 도 안 나간다."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
    relax_events = await _run_recommend_needs_relaxation("off-no-leak-relax-289")
    expand_events = await _run_recommend_needs_expansion("off-no-leak-expand-289")
    assert "progress" not in _types(relax_events)
    assert "progress" not in _types(expand_events)


# ─────────── #396 — 관측: stage 별 최초 발생 ms, 판매자 progress 는 섞이지 않는다 ───────────


async def test_progress_stages_recorded_with_ms_in_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """구매자 progress 프레임의 stage 별 최초 발생 시각이 `started` 기준 ms 로 기록된다."""
    monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
    request = _req("무선 이어폰 추천해줘", "thread-observe-stages-289")
    context = await session_context._default_repository.touch(
        BuyerSessionInput(
            request.session_id,
            request.thread_id,
            "member",
            buyer_owner_id(_member(), get_settings()),
        )
    )
    observer = RequestObservation(
        request_id="req-289-observe-stages",
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

    llm = FakeLLM(decompose=DEFAULT_DECOMPOSE)
    t = 0.0
    async for frame in _production_run_buyer_turn(
        request,
        _member(),
        llm=llm,
        search=_make_search(DEFAULT_PRODUCTS),
        push_fn=_RecordingPush(),
        map_categories=_map_stub,
        observer=observer,
    ):
        t += 1.0
        observer.record_frame(frame, t)

    assert set(observer.progress_stages) == {
        "analyzing",
        "mapping",
        "searching",
        "reranking",
        "publishing",
    }
    assert all(isinstance(ms, int) for ms in observer.progress_stages.values())
    # 최초 발생 순서는 stage 발생 순서와 같아야 한다(ms 는 started 기준 오름차순).
    ordered = sorted(observer.progress_stages, key=lambda s: observer.progress_stages[s])
    assert ordered == ["analyzing", "mapping", "searching", "reranking", "publishing"]


def test_seller_progress_text_only_not_recorded_in_progress_stages() -> None:
    """판매자 `progress`(`{"text": …}`)는 `stage` 가 없어 `progress_stages` 에 섞이지 않는다."""
    observer = RequestObservation(
        request_id="req-289-seller-observe",
        conversation_id="conv",
        thread_id="thread",
        user_id="7",
        brand_id="3",
        role="seller",
        store=None,  # type: ignore[arg-type]
        message_length=0,
        message_hash="h",
        started=0.0,
        pending_message="",
        pending_key="k",
    )
    observer.record_frame(
        'data: {"type": "progress", "data": {"text": "매출 이상 분석 중…"}}\n\n', 1.0
    )
    assert observer.progress_stages == {}


def test_record_frame_survives_malformed_frames_and_still_marks_first_event() -> None:
    """비-JSON·깨진 프레임에서도 `record_frame` 은 예외를 던지지 않고, `first_event_at` 은
    파싱 성공 여부와 무관하게 "빈 프레임이 아니다"만으로 기록된다(#396, 파싱 1회 통합 리뷰).

    파싱을 한 곳(`_parse_frame_payload`)으로 합치면서 `first_event_at` 조건이 실수로
    `payload is not None` 으로 바뀌면 이 테스트가 깨진다 — 그게 이 통합의 가장 깨지기 쉬운
    지점이다.
    """
    observer = RequestObservation(
        request_id="req-289-malformed",
        conversation_id="conv",
        thread_id="thread",
        user_id="42",
        brand_id=None,
        role="buyer",
        store=None,  # type: ignore[arg-type]
        message_length=0,
        message_hash="h",
        started=0.0,
        pending_message="",
        pending_key="k",
    )
    observer.record_frame("data: {this is not json\n\n", 1.0)
    assert observer.first_event_at == 1.0
    assert observer.progress_stages == {}
    assert observer.assistant_parts == []

    observer2 = RequestObservation(
        request_id="req-289-malformed-2",
        conversation_id="conv",
        thread_id="thread",
        user_id="42",
        brand_id=None,
        role="buyer",
        store=None,  # type: ignore[arg-type]
        message_length=0,
        message_hash="h",
        started=0.0,
        pending_message="",
        pending_key="k",
    )
    # 파싱은 성공하지만 dict 가 아닌 JSON(리스트) — 역시 예외 없이 흡수돼야 한다.
    observer2.record_frame("data: [1, 2, 3]\n\n", 2.0)
    assert observer2.first_event_at == 2.0
    assert observer2.progress_stages == {}


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
    검증한다. 초 경계의 `report.generatedAt` 차이로 바이트 동일성 검증이 흔들리지 않도록 시계를
    고정한다.
    """
    from app.agents.seller.orchestrator import PipelineResult
    from app.agents.seller.schemas import RouteDecision

    # [#583] 기준 시각은 app/core/clock.py 로 옮겨졌다 — 종전에는 seller_api 의 모듈 지역
    # `datetime` 을 갈아끼웠지만 그 import 가 사라졌으므로 now_kst 를 직접 고정한다.
    # 산출 문자열은 종전과 동일하다(2026-08-09T12:00:00+09:00).
    def _fixed_now_kst():
        return datetime(2026, 8, 9, 12, 0, 0, tzinfo=KST)

    hitl.set_checkpointer(InMemorySaver())
    try:
        identity = Identity(user_id=None, is_guest=False, seller_id="7", brand_id="3")
        request = SellerChatRequest(
            session_id="s-289-seller", thread_id="t-289-seller", message="지난달 매출 그래프 보여줘"
        )

        async def _route_stub(question, context, recent_turns=(), screen=None):
            return RouteDecision(category="analysis", reason="stub", confidence=0.9)

        async def fake_pipeline(question, context, *, today, emit, recent_turns=(), screen=None):
            await emit("매출 이상 분석 중…")
            return PipelineResult(kind="report", text="6월 매출 보고서 본문")

        monkeypatch.setattr(seller_api, "now_kst", _fixed_now_kst)

        def _run() -> list[str]:
            monkeypatch.setattr(seller_api, "route_question", _route_stub)
            monkeypatch.setattr(seller_api, "run_analysis_pipeline", fake_pipeline)

            async def _drive() -> list[str]:
                return [line async for line in seller_api._seller_stream(request, identity)]

            return asyncio.run(_drive())

        monkeypatch.setattr(get_settings(), "progress_events_enabled", False)
        off_lines = _run()
        monkeypatch.setattr(get_settings(), "progress_events_enabled", True)
        on_lines = _run()
        assert on_lines == off_lines
        off_types = [json.loads(line[len("data: ") :].strip())["type"] for line in off_lines]
        # report 는 kind=="report" 최종 산출에 1회 동반된다(이슈 #296, api-spec §3.2 v0.24.0).
        assert off_types == ["meta", "progress", "token", "report", "done"]
        progress_payload = json.loads(off_lines[1][len("data: ") :].strip())
        assert progress_payload["data"] == {"text": "매출 이상 분석 중…"}
    finally:
        hitl.set_checkpointer(None)


# ─────────── R4-1 — #289 가 뒀던 운영 레인(jwks) 기동 가드를 #396 에서 제거, 가드
# 되살아나지 않았음을 고정 ───────────


def test_progress_events_enabled_succeeds_startup_in_jwks_mode() -> None:
    """운영(jwks)에서 PROGRESS_EVENTS_ENABLED=true 여도 Settings 기동이 성공한다.

    #289 가 계약 미등재 사고를 막으려 뒀던 가드를, 등재(api-spec §3.1 v0.21.0)·FE 확인
    완료(2026-08-06)로 #396 에서 제거했다 — 이 테스트는 가드가 되살아나지 않았음을 고정한다.
    """
    from app.core.config import Settings

    Settings(
        auth_mode="jwks",
        jwks_url="http://x",
        pii_hash_pepper="p",
        internal_api_token="tok",
        google_api_key="k",
        progress_events_enabled=True,
    )  # ok


def test_progress_events_disabled_succeeds_startup_in_jwks_mode() -> None:
    """progress 롤백은 재시도 0회와 짝지으면 jwks 모드에서 정상 기동한다."""
    from app.core.config import Settings

    Settings(
        auth_mode="jwks",
        jwks_url="http://x",
        pii_hash_pepper="p",
        internal_api_token="tok",
        google_api_key="k",
        spring_max_retries=0,
        progress_events_enabled=False,
    )  # ok


def test_progress_events_disabled_rejects_startup_with_retries_enabled() -> None:
    """progress off + 재시도 1회는 미룬 I-1 직렬 12s로 first-token 가드가 거절한다."""
    import pytest
    from pydantic import ValidationError
    from app.core.config import Settings

    with pytest.raises(ValidationError, match="STREAM_FIRST_TOKEN_TIMEOUT_S"):
        Settings(
            auth_mode="jwks",
            jwks_url="http://x",
            pii_hash_pepper="p",
            internal_api_token="tok",
            google_api_key="k",
            spring_max_retries=1,
            progress_events_enabled=False,
        )


def test_progress_events_enabled_succeeds_startup_in_staging_even_with_dev_auth() -> None:
    """R5-1 — dev 인증으로 도는 staging(실 FE 가 붙을 수 있음) 조합에서도 기동이 성공한다.

    #289 가 계약 미등재 사고를 막으려 뒀던 가드를, 등재(api-spec §3.1 v0.21.0)·FE 확인
    완료(2026-08-06)로 #396 에서 제거했다 — 이 테스트는 가드가 되살아나지 않았음을 고정한다.
    """
    from app.core.config import Settings

    Settings(
        auth_mode="dev",
        app_environment="staging",
        progress_events_enabled=True,
    )  # ok


def test_progress_events_enabled_succeeds_startup_in_local_dev() -> None:
    """가드를 넓혀도 로컬(기본 `local`)·실측 하네스 경로는 계속 살아 있다.

    실측 하네스(`evals/first_event_budget/measure_first_event.py`)는 `AUTH_MODE=dev`만
    핀으로 박고 `app_environment`는 기본값(`local`)이라 이 조합과 정확히 같다.
    """
    from app.core.config import Settings

    Settings(auth_mode="dev", app_environment="local", progress_events_enabled=True)  # ok
