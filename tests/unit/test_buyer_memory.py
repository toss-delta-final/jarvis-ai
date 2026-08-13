"""구매자 채팅 선택적 계층형 메모리 (이슈 #653)."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json

from langgraph.store.memory import InMemoryStore
import pytest

from app.agents.buyer import memory as buyer_memory
from app.agents.buyer.memory import (
    compact_buyer_memory,
    estimate_tokens,
    prepare_buyer_memory,
)
from app.core.conversation import Turn, TurnStatus
from app.core.llm import LLMError


def _turn(
    number: int,
    *,
    thread_id: str = "room-a",
    user: str | None = None,
    assistant: str | None = None,
    status: TurnStatus = TurnStatus.COMPLETED,
) -> Turn:
    return Turn(
        turn_id=f"turn-{number}",
        conversation_id="conversation",
        thread_id=thread_id,
        user_id="u1",
        role="member",
        user_text=user or f"사용자 질문 {number}",
        assistant_text=assistant or f"AI 답변 {number}",
        status=status,
    )


async def _prepare(
    turns: list[Turn],
    *,
    store: InMemoryStore | None = None,
    thread_id: str = "room-a",
    recent_turn_limit: int = 3,
    recent_token_cap: int = 1_000,
    situation_token_cap: int = 400,
    compaction_trigger_tokens: int = 1_200,
    compaction_input_token_cap: int = 4_000,
):
    return await prepare_buyer_memory(
        turns,
        thread_id=thread_id,
        thread_key=f"context:{thread_id}",
        store=store or InMemoryStore(),
        recent_turn_limit=recent_turn_limit,
        recent_token_cap=recent_token_cap,
        situation_token_cap=situation_token_cap,
        compaction_trigger_tokens=compaction_trigger_tokens,
        compaction_input_token_cap=compaction_input_token_cap,
    )


async def test_recent_memory_uses_same_thread_completed_turns_only() -> None:
    context = await _prepare(
        [
            _turn(1, status=TurnStatus.COMPLETED),
            _turn(2, status=TurnStatus.FAILED),
            _turn(3, status=TurnStatus.CANCELLED),
            _turn(4, status=TurnStatus.PENDING),
            _turn(5, thread_id="room-b"),
        ]
    )

    assert [turn.turn_id for turn in context.recent_turns] == ["turn-1"]
    assert context.compaction_turns == ()


async def test_new_thread_has_no_same_room_memory() -> None:
    context = await _prepare([_turn(1), _turn(2)], thread_id="new-room")

    assert context.recent_turns == ()
    assert context.situation.is_empty
    assert context.compaction_turns == ()


async def test_recent_memory_keeps_only_latest_three_complete_pairs() -> None:
    context = await _prepare([_turn(number) for number in range(1, 6)])

    assert [turn.turn_id for turn in context.recent_turns] == ["turn-3", "turn-4", "turn-5"]


async def test_oversized_latest_pair_is_clipped_without_dropping_either_role() -> None:
    context = await _prepare(
        [_turn(1, user="가" * 100, assistant="나" * 100)],
        recent_token_cap=40,
    )

    [recent] = context.recent_turns
    assert recent.user_text
    assert recent.assistant_text
    assert context.recent_tokens <= 40


async def test_low_value_evicted_turns_do_not_trigger_compaction() -> None:
    context = await _prepare(
        [
            _turn(1, user="네", assistant="확인했습니다"),
            _turn(2, user="고마워", assistant="천만에요"),
            _turn(3, user="새 질문", assistant="새 답변"),
        ],
        recent_turn_limit=1,
        compaction_trigger_tokens=1,
    )

    assert context.evicted_tokens == 0
    assert context.compaction_triggered is False
    assert context.compaction_turns == ()


async def test_high_value_evicted_turns_trigger_only_after_batch_threshold() -> None:
    turns = [
        _turn(1, user="가" * 20, assistant="나" * 20),
        _turn(2, user="다" * 20, assistant="라" * 20),
        _turn(3, user="현재", assistant="응답"),
    ]

    below = await _prepare(turns, recent_turn_limit=1, compaction_trigger_tokens=100)
    reached = await _prepare(turns, recent_turn_limit=1, compaction_trigger_tokens=80)

    assert below.compaction_triggered is False
    assert reached.compaction_triggered is True
    assert [turn.turn_id for turn in reached.compaction_turns] == ["turn-1", "turn-2"]


async def test_compaction_batch_is_bounded_and_keeps_oldest_cursor_order() -> None:
    context = await _prepare(
        [
            _turn(
                number,
                user=chr(0xAC00 + number) * 40,
                assistant=chr(0xB098 + number) * 40,
            )
            for number in range(1, 7)
        ],
        recent_turn_limit=1,
        compaction_trigger_tokens=1,
        compaction_input_token_cap=160,
    )

    assert (
        sum(
            estimate_tokens(turn.user_text) + estimate_tokens(turn.assistant_text)
            for turn in context.compaction_turns
        )
        <= 160
    )
    assert [turn.turn_id for turn in context.compaction_turns] == ["turn-1", "turn-2"]


async def test_invalid_stored_situation_fails_open() -> None:
    store = InMemoryStore()
    await store.aput(
        ("buyer_situation_memory_v1", "context:room-a"),
        "situation",
        {"situation": "not-an-object", "compactedThroughTurnId": 123},
    )

    context = await _prepare([_turn(1)], store=store)

    assert context.situation.is_empty
    assert context.compacted_through_turn_id is None


class _FakeLLM:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def complete(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _UsageObserver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def record_model_call(
        self,
        model: str,
        *,
        usage_reserved: bool = False,
        purpose: str | None = None,
    ) -> int:
        self.calls.append((model, usage_reserved))
        assert purpose == "memory_compaction"
        return 7


async def test_compaction_redacts_pii_caps_summary_and_advances_cursor() -> None:
    store = InMemoryStore()
    context = await _prepare(
        [
            _turn(1, user="여행 일정은 제주도로 정했어", assistant="제주 여행으로 기억할게요"),
            _turn(2, user="연락처는 010-1234-5678", assistant="연락처를 확인했어요"),
            _turn(3, user="현재 질문", assistant="현재 답변"),
        ],
        store=store,
        recent_turn_limit=1,
        compaction_trigger_tokens=1,
    )
    llm = _FakeLLM(
        json.dumps(
            {
                "topic": "제주 여행 010-1234-5678",
                "currentGoal": "가" * 200,
                "situationalFacts": ["7월에 출발", "user@example.com"],
                "decisions": ["제주도로 결정"],
                "openQuestions": ["숙소는 어디로 할까"],
            },
            ensure_ascii=False,
        )
    )

    changed = await compact_buyer_memory(
        context,
        store=store,
        thread_key="context:room-a",
        llm=llm,
        situation_token_cap=80,
        max_tokens=256,
    )
    loaded = await _prepare([_turn(3)], store=store, situation_token_cap=80)

    assert changed is True
    assert loaded.compacted_through_turn_id == "turn-2"
    assert loaded.situation_tokens <= 80
    rendered = str(loaded.situation.to_prompt())
    assert "010-1234-5678" not in rendered
    assert "user@example.com" not in rendered
    assert "[전화번호]" in rendered


async def test_compaction_reserves_explicit_usage_call_id() -> None:
    store = InMemoryStore()
    context = await _prepare(
        [
            _turn(1, user="가" * 30, assistant="나" * 30),
            _turn(2, user="현재", assistant="응답"),
        ],
        store=store,
        recent_turn_limit=1,
        compaction_trigger_tokens=1,
    )
    observer = _UsageObserver()

    changed = await compact_buyer_memory(
        context,
        store=store,
        thread_key="context:room-a",
        llm=_FakeLLM('{"topic":"새 주제"}'),
        situation_token_cap=400,
        max_tokens=256,
        observer=observer,
        model_id="fast-model",
    )

    assert changed is True
    assert observer.calls == [("fast-model", True)]


async def test_compaction_does_not_hold_mutation_lock_during_llm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """느린 외부 LLM 대기 동안 DB advisory lock 연결을 점유하지 않는다."""
    store = InMemoryStore()
    context = await _prepare(
        [
            _turn(1, user="가" * 30, assistant="나" * 30),
            _turn(2, user="현재", assistant="응답"),
        ],
        store=store,
        recent_turn_limit=1,
        compaction_trigger_tokens=1,
    )
    lock_held = False

    @asynccontextmanager
    async def recording_lock(*args, **kwargs):
        del args, kwargs
        nonlocal lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    class LockAwareLLM(_FakeLLM):
        async def complete(self, **kwargs) -> str:
            assert lock_held is False
            return await super().complete(**kwargs)

    monkeypatch.setattr(buyer_memory, "mutation_lock", recording_lock)

    changed = await compact_buyer_memory(
        context,
        store=store,
        thread_key="context:room-a",
        llm=LockAwareLLM('{"topic":"새 주제"}'),
        situation_token_cap=400,
        max_tokens=256,
    )

    assert changed is True


async def test_compaction_failure_keeps_previous_situation_and_cursor() -> None:
    store = InMemoryStore()
    await store.aput(
        ("buyer_situation_memory_v1", "context:room-a"),
        "situation",
        {
            "situation": {
                "topic": "기존 주제",
                "currentGoal": "기존 목표",
                "situationalFacts": [],
                "decisions": [],
                "openQuestions": [],
            },
            "compactedThroughTurnId": "turn-1",
        },
    )
    context = await _prepare(
        [
            _turn(1, user="이전", assistant="이전 답"),
            _turn(2, user="가" * 30, assistant="나" * 30),
            _turn(3, user="현재", assistant="현재 답"),
        ],
        store=store,
        recent_turn_limit=1,
        compaction_trigger_tokens=1,
    )

    changed = await compact_buyer_memory(
        context,
        store=store,
        thread_key="context:room-a",
        llm=_FakeLLM(LLMError("compactor unavailable")),
        situation_token_cap=400,
        max_tokens=256,
    )
    loaded = await _prepare([_turn(3)], store=store)

    assert changed is False
    assert loaded.compacted_through_turn_id == "turn-1"
    assert loaded.situation.topic == "기존 주제"


class _ReadFailingStore(InMemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_reads = False
        self.write_count = 0

    async def aget(self, namespace, key):
        if self.fail_reads:
            raise TimeoutError("store unavailable")
        return await super().aget(namespace, key)

    async def aput(self, namespace, key, value, *, index=None, ttl=None) -> None:
        self.write_count += 1
        await super().aput(namespace, key, value, index=index, ttl=ttl)


async def test_compaction_read_failure_never_overwrites_unknown_latest_state() -> None:
    store = _ReadFailingStore()
    context = await _prepare(
        [
            _turn(1, user="가" * 30, assistant="나" * 30),
            _turn(2, user="현재", assistant="응답"),
        ],
        store=store,
        recent_turn_limit=1,
        compaction_trigger_tokens=1,
    )
    store.fail_reads = True

    changed = await compact_buyer_memory(
        context,
        store=store,
        thread_key="context:room-a",
        llm=_FakeLLM('{"topic":"새 주제"}'),
        situation_token_cap=400,
        max_tokens=256,
    )

    assert changed is False
    assert store.write_count == 0


def test_token_estimator_is_conservative_for_korean() -> None:
    assert estimate_tokens("가나다라마바사") == 7
