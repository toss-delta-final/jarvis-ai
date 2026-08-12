"""구매자 그래프의 같은 방 메모리 lifecycle 배선 (이슈 #653)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents.buyer import graph as buyer_graph
from app.agents.buyer.cart.state import PendingAdd, get_cart_store
from app.agents.buyer.session_state import context_thread_key
from app.core.auth import Identity
from app.core.config import get_settings
from app.core.conversation import TurnStatus, conversation_key, get_conversation_store
from app.core.observability import start_observation
from app.core.session_context import BuyerSessionInput
from tests._fakes import FakeLLM


def _identity() -> Identity:
    return Identity(user_id="buyer-653", is_guest=False, seller_id=None, subject="buyer-653")


def _request(message: str, *, session_id: str = "session-653", thread_id: str = "room-a"):
    return SimpleNamespace(
        session_id=session_id,
        thread_id=thread_id,
        message=message,
        condition_actions=[],
    )


def _session(request) -> BuyerSessionInput:  # noqa: ANN001
    return BuyerSessionInput(request.session_id, request.thread_id, "member", "buyer-653")


async def _seed_turn(
    *,
    session_id: str,
    thread_id: str,
    user: str,
    assistant: str,
    status: TurnStatus = TurnStatus.COMPLETED,
) -> None:
    store = await get_conversation_store()
    committed = await store.save_user_message(
        conversation_key("buyer-653", session_id),
        "buyer-653",
        "member",
        user,
        thread_id=thread_id,
        buyer_session=BuyerSessionInput(session_id, thread_id, "member", "buyer-653"),
    )
    await store.finalize_assistant(committed.turn_id, assistant, status)


async def _observer(request):  # noqa: ANN001
    observation = start_observation(
        request_id="request-653",
        identity=_identity(),
        conversation_id=request.session_id,
        thread_id=request.thread_id,
        message=request.message,
        store=await get_conversation_store(),
        now=0.0,
        buyer_session=_session(request),
    )
    await observation.commit_user_message()
    return observation


async def _collect(request, observer, llm):  # noqa: ANN001
    events: list[dict] = []
    async for frame in buyer_graph.run_buyer_turn(
        request,
        _identity(),
        observer=observer,
        llm=llm,
    ):
        line = frame.strip()
        if line.startswith("data:"):
            events.append(json.loads(line.removeprefix("data:").strip()))
    return events


def _general_llm() -> FakeLLM:
    return FakeLLM(decompose={"intent": "general", "reply": "이어가 볼게요", "filters": {}})


def _fast_prompt(llm: FakeLLM) -> str:
    return next(user for tier, user in llm.calls if tier == "fast")


async def test_second_turn_in_same_room_injects_recent_conversation() -> None:
    await _seed_turn(
        session_id="session-653",
        thread_id="room-a",
        user="제주 여행 얘기하자",
        assistant="좋아요, 제주 여행을 같이 정해봐요",
    )
    request = _request("아까 얘기 계속하자")
    observer = await _observer(request)
    llm = _general_llm()

    await _collect(request, observer, llm)

    prompt = _fast_prompt(llm)
    assert "RECENT_CONVERSATION" in prompt
    assert "제주 여행 얘기하자" in prompt
    assert "좋아요, 제주 여행을 같이 정해봐요" in prompt


async def test_new_room_does_not_inject_previous_room_conversation() -> None:
    await _seed_turn(
        session_id="session-653",
        thread_id="room-a",
        user="다른 방 비밀 맥락",
        assistant="다른 방 답변",
    )
    request = _request("새 대화 시작", thread_id="room-b")
    observer = await _observer(request)
    llm = _general_llm()

    await _collect(request, observer, llm)

    prompt = _fast_prompt(llm)
    assert "RECENT_CONVERSATION" not in prompt
    assert "다른 방 비밀 맥락" not in prompt


async def test_thread_adoption_is_verified_before_memory_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thread 소유권 검증 실패 요청은 대화 메모리 저장소를 먼저 읽지 않는다."""

    class CountingConversationStore:
        def __init__(self) -> None:
            self.reads = 0

        async def turns_for(self, key: str):
            del key
            self.reads += 1
            return []

    async def reject_adoption(*args, **kwargs):
        del args, kwargs
        raise buyer_graph.SessionStateUnavailable

    store = CountingConversationStore()
    observer = SimpleNamespace(
        store=store,
        pending_key="owner:session-653",
        context_id="context-653",
        request_id="request-653",
    )
    monkeypatch.setattr(buyer_graph, "ensure_thread_adopted", reject_adoption)

    with pytest.raises(buyer_graph.SessionStateUnavailable):
        await _collect(_request("검증되지 않은 방"), observer, _general_llm())

    assert store.reads == 0


async def test_pending_cart_turn_omits_free_conversation_memory() -> None:
    await _seed_turn(
        session_id="session-653",
        thread_id="room-a",
        user="옵션과 무관한 과거 대화",
        assistant="과거 답변",
    )
    request = _request("2번으로")
    observer = await _observer(request)
    cart_store = await get_cart_store()
    await cart_store.set_pending(
        context_thread_key(observer.context_id, request.thread_id),
        PendingAdd(product_id=101, quantity=1),
    )
    llm = _general_llm()

    await _collect(request, observer, llm)

    prompt = _fast_prompt(llm)
    assert "PENDING_CART:" in prompt
    assert "RECENT_CONVERSATION" not in prompt
    assert "옵션과 무관한 과거 대화" not in prompt


async def test_pending_cart_turn_does_not_start_memory_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "buyer_memory_recent_turns", 1)
    monkeypatch.setattr(get_settings(), "buyer_memory_compaction_trigger_tokens", 1)
    for number in (1, 2):
        await _seed_turn(
            session_id="session-653",
            thread_id="room-a",
            user=f"옵션과 무관한 고가치 과거 대화 {number}",
            assistant=f"과거 답변 {number}",
        )
    called = 0

    async def count_compaction(*args, **kwargs):
        del args, kwargs
        nonlocal called
        called += 1
        return True

    monkeypatch.setattr(buyer_graph, "compact_buyer_memory", count_compaction)
    request = _request("2번으로")
    observer = await _observer(request)
    await (await get_cart_store()).set_pending(
        context_thread_key(observer.context_id, request.thread_id),
        PendingAdd(product_id=101, quantity=1),
    )

    await _collect(request, observer, _general_llm())

    assert called == 0
    assert observer.evicted_history_tokens > 0
    assert observer.memory_compaction_triggered is False


async def test_compaction_exception_is_fail_open_after_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "buyer_memory_recent_turns", 1)
    monkeypatch.setattr(get_settings(), "buyer_memory_compaction_trigger_tokens", 1)
    await _seed_turn(
        session_id="session-653",
        thread_id="room-a",
        user="첫 번째로 충분히 긴 고가치 대화",
        assistant="첫 번째 긴 답변",
    )
    await _seed_turn(
        session_id="session-653",
        thread_id="room-a",
        user="두 번째로 충분히 긴 고가치 대화",
        assistant="두 번째 긴 답변",
    )
    called = 0

    async def fail_compaction(*args, **kwargs):
        del args, kwargs
        nonlocal called
        called += 1
        raise RuntimeError("compaction failed")

    monkeypatch.setattr(buyer_graph, "compact_buyer_memory", fail_compaction, raising=False)
    request = _request("현재 일반 대화")
    observer = await _observer(request)

    events = await _collect(request, observer, _general_llm())

    assert called == 1
    assert events[-1]["type"] == "done"
