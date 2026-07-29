"""판매자 대화 스레드(thread.py) 검증 — 공용 checkpointer(InMemorySaver 주입) 기반.

실 PG 없음 — conftest 의 _isolate_seller_persistence 가 hitl.set_checkpointer(재수출 =
checkpoint.set_checkpointer)로 InMemorySaver 를 주입한다. 검증 항목:
  - 네임스페이스: seller-chat:{sellerId}:{threadId} (IDOR — seller 접두 격리)
  - record_turn ↔ load_recent_turns round-trip (순서 보존)
  - 절단: 기록(seller_chat_record_max_chars)·주입(seller_chat_context_max_chars)·
    최근 N턴 상한(seller_chat_context_turns)
  - degrade: 조회·기록 실패는 예외 없이 계속(맥락은 부가 데이터)
  - 입력 조립: 맥락 없으면 질문 원문 그대로(기존 계약 불변), 있으면 라벨 포함
  - set_checkpointer 교체 시 recorder 그래프 캐시 무효화(reset hook)
"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.seller import checkpoint, thread
from app.agents.seller.context import SellerContext

_CTX = SellerContext(seller_id=7, brand_id=3)
_OTHER = SellerContext(seller_id=8, brand_id=3)


def test_chat_thread_id_namespace() -> None:
    """seller_id 접두 + seller-chat 네임스페이스 — HITL(seller-draft:)과 충돌 없음."""
    assert thread.chat_thread_id(_CTX, "t-1") == "seller-chat:7:t-1"
    assert thread.chat_config(_CTX, "t-1") == {"configurable": {"thread_id": "seller-chat:7:t-1"}}


def test_record_and_load_roundtrip() -> None:
    async def run() -> list[thread.Turn]:
        await thread.record_turn(_CTX, "t-1", "어제 매출 알려줘", "어제 매출은 120만원입니다.")
        await thread.record_turn(_CTX, "t-1", "그럼 지난주는?", "지난주 매출은 800만원입니다.")
        return await thread.load_recent_turns(_CTX, "t-1")

    turns = asyncio.run(run())
    assert turns == [
        ("user", "어제 매출 알려줘"),
        ("assistant", "어제 매출은 120만원입니다."),
        ("user", "그럼 지난주는?"),
        ("assistant", "지난주 매출은 800만원입니다."),
    ]


def test_load_is_isolated_per_seller_and_thread() -> None:
    """타 판매자·타 스레드에서는 보이지 않는다 — seller 접두가 곧 IDOR 차단."""

    async def run() -> tuple[list[thread.Turn], list[thread.Turn], list[thread.Turn]]:
        await thread.record_turn(_CTX, "t-1", "질문", "답변")
        same_thread_other_seller = await thread.load_recent_turns(_OTHER, "t-1")
        other_thread_same_seller = await thread.load_recent_turns(_CTX, "t-2")
        own = await thread.load_recent_turns(_CTX, "t-1")
        return same_thread_other_seller, other_thread_same_seller, own

    other_seller, other_thread, own = asyncio.run(run())
    assert other_seller == []
    assert other_thread == []
    assert own == [("user", "질문"), ("assistant", "답변")]


def test_record_truncates_and_skips_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """기록은 seller_chat_record_max_chars 절단, 빈 assistant 는 기록하지 않는다."""
    from app.core.config import get_settings

    cap = get_settings().seller_chat_record_max_chars

    async def run() -> list[thread.Turn]:
        await thread.record_turn(_CTX, "t-1", "질문", "가" * (cap + 100))
        await thread.record_turn(_CTX, "t-1", "무시될 질문", "   ")  # 빈 답변 — 미기록
        return await thread.load_recent_turns(_CTX, "t-1")

    turns = asyncio.run(run())
    assert len(turns) == 2  # 빈 답변 쌍은 기록되지 않았다
    assert len(turns[1][1]) <= cap


def test_load_truncates_per_message_and_limits_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """주입은 메시지당 seller_chat_context_max_chars, 최근 max_turns 쌍으로 절단한다."""

    async def run() -> list[thread.Turn]:
        for i in range(5):
            await thread.record_turn(_CTX, "t-1", f"질문{i} " + "나" * 500, f"답변{i}")
        return await thread.load_recent_turns(_CTX, "t-1", max_turns=2)

    turns = asyncio.run(run())
    from app.core.config import get_settings

    cap = get_settings().seller_chat_context_max_chars
    assert len(turns) == 4  # 2쌍 = 메시지 4개 (최근 것만)
    assert turns[0][1].startswith("질문3")
    assert all(len(text) <= cap for _, text in turns)


def test_load_failure_degrades_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """조회 실패는 [] — 맥락은 부가 데이터, 응답을 죽이지 않는다."""

    async def boom() -> None:
        raise RuntimeError("pg down")

    monkeypatch.setattr(thread, "_get_graph", boom)
    assert asyncio.run(thread.load_recent_turns(_CTX, "t-1")) == []


def test_record_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """기록 실패는 예외 없이 warning 만 — best-effort 계약."""

    async def boom() -> None:
        raise RuntimeError("pg down")

    monkeypatch.setattr(thread, "_get_graph", boom)
    asyncio.run(thread.record_turn(_CTX, "t-1", "질문", "답변"))  # 예외가 새면 테스트 실패


def test_build_contextual_input_without_turns_is_identity() -> None:
    """맥락이 없으면 질문 원문 그대로 — 기존 supervisor 입력 계약 불변."""
    assert thread.build_contextual_input("지난달 매출?", []) == "지난달 매출?"
    assert thread.render_recent_turns([]) == ""


def test_build_contextual_input_with_turns_has_labels() -> None:
    turns = [("user", "어제 매출?"), ("assistant", "120만원입니다.")]
    text = thread.build_contextual_input("그럼 지난주는?", turns)
    assert "[최근 대화]" in text
    assert "사용자: 어제 매출?" in text
    assert "상담원: 120만원입니다." in text
    assert text.endswith("[이번 질문] 그럼 지난주는?")
    # 참고 맥락 라벨 — 오라우팅 방지 명시.
    assert "참고 맥락" in text


def test_set_checkpointer_resets_recorder_graph() -> None:
    """checkpointer 교체(reset hook) 후에는 이전 저장소의 턴이 보이지 않는다."""

    async def run() -> tuple[list[thread.Turn], list[thread.Turn]]:
        await thread.record_turn(_CTX, "t-1", "질문", "답변")
        before = await thread.load_recent_turns(_CTX, "t-1")
        checkpoint.set_checkpointer(InMemorySaver())  # 교체 — recorder 그래프 무효화
        after = await thread.load_recent_turns(_CTX, "t-1")
        return before, after

    before, after = asyncio.run(run())
    assert before == [("user", "질문"), ("assistant", "답변")]
    assert after == []


def test_hitl_reexports_shared_set_checkpointer() -> None:
    """hitl.set_checkpointer 는 공용 checkpoint.set_checkpointer 재수출이다(테스트 호환)."""
    from app.agents.seller import hitl

    assert hitl.set_checkpointer is checkpoint.set_checkpointer


# ── recorder ↔ create_agent 체크포인트 상호운용 (설계 리스크 검증) ────────────────


def test_recorder_interops_with_general_agent_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """general(create_agent) 이 누적한 스레드를 recorder 가 읽고, recorder 가 기록한
    턴을 general 이 다음 invoke 에서 본다 — messages 채널(add_messages) 상호운용."""
    from langchain_core.messages import AIMessage
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from app.agents.seller import workers

    class _FakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
            return self

    fake = _FakeModel(
        messages=iter([AIMessage(content="120만원입니다."), AIMessage(content="확인했습니다.")])
    )
    monkeypatch.setattr(workers, "init_seller_model", lambda tier: fake)

    async def run() -> tuple[list[thread.Turn], list[str]]:
        checkpointer = await checkpoint.get_checkpointer()  # conftest 주입 InMemorySaver
        agent = workers.build_general_agent(today="2026-07-29", checkpointer=checkpointer)
        config = thread.chat_config(_CTX, "t-1")

        # 1) general invoke — 스레드에 (user, assistant) 자연 누적.
        from langchain_core.messages import HumanMessage

        await agent.ainvoke(
            {"messages": [HumanMessage(content="어제 매출 알려줘")]},
            config=config,
            context=_CTX,
        )
        # 2) recorder 가 general 의 체크포인트를 읽는다.
        after_general = await thread.load_recent_turns(_CTX, "t-1")

        # 3) recorder 기록 → general 이 다음 invoke 에서 본다.
        await thread.record_turn(_CTX, "t-1", "1번 적용해줘", "초안을 생성했습니다.")
        await agent.ainvoke(
            {"messages": [HumanMessage(content="고마워")]}, config=config, context=_CTX
        )
        snapshot = await agent.aget_state(config)
        texts = [
            m.content
            for m in snapshot.values["messages"]
            if isinstance(m.content, str) and m.content
        ]
        return after_general, texts

    after_general, texts = asyncio.run(run())
    assert after_general == [("user", "어제 매출 알려줘"), ("assistant", "120만원입니다.")]
    # general 의 최종 상태에 recorder 가 기록한 턴이 순서대로 포함된다.
    assert texts == [
        "어제 매출 알려줘",
        "120만원입니다.",
        "1번 적용해줘",
        "초안을 생성했습니다.",
        "고마워",
        "확인했습니다.",
    ]
