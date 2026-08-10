"""PgConversationStore 통합 테스트 (이슈 #33, api-spec §6.3 a) — 실 pg-profile 필요.

`docker compose up -d pg-profile` 로 컨테이너가 떠 있어야 통과한다. 기본 pytest 실행에서는
@pytest.mark.integration 으로 제외된다(pyproject.toml addopts) — 명시적으로
`uv run pytest tests/integration -m integration` 로 실행한다.

ConversationStore(인메모리)는 유닛 테스트가 계속 쓰므로 여기서 건드리지 않는다
(tests/conftest.py InMemory 격리 컨벤션과 동일 원칙). 키는 매 테스트 uuid 로 발급해
재실행 간 충돌을 피한다.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from app.core import conversation as conversation_module
from app.core.conversation import CommittedTurn, PgConversationStore, TurnStatus
from app.core.config import get_settings
from app.core.session_context import BuyerSessionInput, SessionContextRepository
from app.main import app

pytestmark = pytest.mark.integration


def _conversation_id() -> str:
    return f"it-conv-{uuid.uuid4().hex}"


@pytest.fixture
async def pool():
    p = AsyncConnectionPool(get_settings().profile_db_url, open=False)
    await p.open(wait=True)
    try:
        await PgConversationStore(p).setup()
        await SessionContextRepository(pool=p).initialize()
        yield p
    finally:
        await p.close()


async def test_save_and_get_turn_roundtrip(pool) -> None:
    store = PgConversationStore(pool)
    conversation_id = _conversation_id()
    thread_id = f"room-{uuid.uuid4().hex}"
    turn_id = await store.save_user_message(
        conversation_id,
        "u1",
        "member",
        "안녕하세요",
        thread_id=thread_id,
    )
    assert isinstance(turn_id, CommittedTurn)
    assert turn_id.context_id is None
    turn = await store.get_turn(turn_id)
    assert turn is not None
    assert turn.conversation_id == conversation_id
    assert turn.thread_id == thread_id
    assert turn.user_id == "u1"
    assert turn.role == "member"
    assert turn.user_text == "안녕하세요"
    assert turn.assistant_text == ""
    assert turn.status == TurnStatus.PENDING
    # [F1, #321 리뷰 2라운드] _row_to_turn 이 created_at 을 안 실으면 default_factory 가
    # "읽은 시각"으로 채워 삽입 시각이 조용히 거짓이 된다 — 실제 DB 컬럼 값(방금 삽입된
    # 시각)이 왕복되는지 잰다.
    assert turn.created_at is not None
    assert (datetime.now(UTC) - turn.created_at) < timedelta(minutes=1)


async def test_finalize_assistant_updates_status_and_text(pool) -> None:
    store = PgConversationStore(pool)
    conversation_id = _conversation_id()
    turn_id = await store.save_user_message(conversation_id, "u1", "member", "질문")
    await store.finalize_assistant(turn_id, "부분 응답", TurnStatus.CANCELLED)
    turn = await store.get_turn(turn_id)
    assert turn is not None
    assert turn.assistant_text == "부분 응답"
    assert turn.status == TurnStatus.CANCELLED


async def test_turns_for_returns_ordered_by_creation(pool) -> None:
    store = PgConversationStore(pool)
    conversation_id = _conversation_id()
    ids = [
        await store.save_user_message(conversation_id, "u1", "member", f"m{i}") for i in range(3)
    ]
    for tid in ids:
        await store.finalize_assistant(tid, "ok", TurnStatus.COMPLETED)
    turns = await store.turns_for(conversation_id)
    assert [t.user_text for t in turns] == ["m0", "m1", "m2"]


async def test_setup_backfills_legacy_rows_in_previous_logical_order(pool) -> None:
    store = PgConversationStore(pool)
    conversation_id = _conversation_id()
    suffix = uuid.uuid4().hex
    first_id, second_id = f"z-{suffix}", f"a-{suffix}"
    created_at = "2026-07-20T00:00:00+00:00"
    async with pool.connection() as conn:
        for turn_id, text in ((first_id, "physical-first"), (second_id, "physical-second")):
            await conn.execute(
                "INSERT INTO conversation_turns "
                "(turn_id, conversation_id, role, user_text, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (turn_id, conversation_id, "member", text, created_at),
            )
        await conn.execute("ALTER TABLE conversation_turns DROP COLUMN sequence_id")

    other_pool = AsyncConnectionPool(get_settings().profile_db_url, open=False)
    await other_pool.open(wait=True)
    try:
        await asyncio.gather(store.setup(), PgConversationStore(other_pool).setup())
    finally:
        await other_pool.close()

    turns = await store.turns_for(conversation_id)
    assert [turn.turn_id for turn in turns] == [second_id, first_id]


async def test_turns_for_uses_insert_order_when_timestamps_match(pool) -> None:
    store = PgConversationStore(pool)
    conversation_id = _conversation_id()
    suffix = uuid.uuid4().hex
    first_id, second_id = f"z-{suffix}", f"a-{suffix}"
    created_at = "2026-07-21T00:00:00+00:00"
    async with pool.connection() as conn:
        for turn_id, text in ((first_id, "first"), (second_id, "second")):
            await conn.execute(
                "INSERT INTO conversation_turns "
                "(turn_id, conversation_id, role, user_text, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (turn_id, conversation_id, "member", text, created_at),
            )

    turns = await store.turns_for(conversation_id)
    assert [turn.turn_id for turn in turns] == [first_id, second_id]


async def test_get_turn_missing_returns_none(pool) -> None:
    store = PgConversationStore(pool)
    assert await store.get_turn(f"missing-{uuid.uuid4().hex}") is None


async def test_scoped_by_conversation_id(pool) -> None:
    store = PgConversationStore(pool)
    conv_a, conv_b = _conversation_id(), _conversation_id()
    await store.save_user_message(conv_a, "A", "member", "a")
    await store.save_user_message(conv_b, "B", "member", "b")
    turns_a = await store.turns_for(conv_a)
    turns_b = await store.turns_for(conv_b)
    assert len(turns_a) == 1 and turns_a[0].user_text == "a"
    assert len(turns_b) == 1 and turns_b[0].user_text == "b"


async def test_purge_expired_turns_deletes_only_before_cutoff(pool) -> None:
    """이슈 #321 — 컷오프 이전 턴만 지워지고 이후 턴은 남는다."""
    store = PgConversationStore(pool)
    conversation_id = _conversation_id()
    old_committed = await store.save_user_message(conversation_id, None, "user", "old")
    new_committed = await store.save_user_message(conversation_id, None, "user", "new")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE conversation_turns SET created_at = now() - interval '200 days' "
            "WHERE turn_id = %s",
            (old_committed.turn_id,),
        )

    # 전역 스윕이라 conversation_id 로 좁혀지지 않는다 — 다른(이전) 테스트가 남긴 만료 행이
    # 섞일 수 있어 정확한 카운트 대신 "내 오래된 턴은 지워지고 내 새 턴은 남는가"만 잰다.
    deleted = await store.purge_expired_turns(retention_days=90.0)

    assert deleted >= 1
    assert await store.get_turn(old_committed.turn_id) is None
    assert await store.get_turn(new_committed.turn_id) is not None


async def test_purge_expired_turns_deletes_pending_rows_too(pool) -> None:
    """PENDING(assistant_text 미확정) 도 컷오프를 넘으면 지운다 — 죽은 스트림 방치 방지."""
    store = PgConversationStore(pool)
    conversation_id = _conversation_id()
    committed = await store.save_user_message(conversation_id, None, "user", "old")
    turn = await store.get_turn(committed.turn_id)
    assert turn is not None
    assert turn.status is TurnStatus.PENDING
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE conversation_turns SET created_at = now() - interval '200 days' "
            "WHERE turn_id = %s",
            (committed.turn_id,),
        )

    # [F5, #321 리뷰 2라운드] 전역 스윕이라 conversation_id 로 좁혀지지 않는다 — pg-profile은
    # 여러 worktree 가 공유해 다른 레인이 남긴 만료 행이 같은 배치에 섞이면 `== 1` 이 깨진다
    # (형제 테스트 `..._deletes_only_before_cutoff` 와 같은 이유·같은 형태로 고정한다).
    deleted = await store.purge_expired_turns(retention_days=90.0)

    assert deleted >= 1
    assert await store.get_turn(committed.turn_id) is None


async def test_state_persists_across_store_instances() -> None:
    """재시작·다중 인스턴스 스모크 — 새 연결로도 이전에 쓴 값이 보인다."""
    conversation_id = _conversation_id()
    pool_a = AsyncConnectionPool(get_settings().profile_db_url, open=False)
    await pool_a.open(wait=True)
    try:
        store_a = PgConversationStore(pool_a)
        await store_a.setup()
        turn_id = await store_a.save_user_message(conversation_id, "u1", "member", "영속성 확인")
    finally:
        await pool_a.close()

    pool_b = AsyncConnectionPool(get_settings().profile_db_url, open=False)
    await pool_b.open(wait=True)
    try:
        turn = await PgConversationStore(pool_b).get_turn(turn_id)
    finally:
        await pool_b.close()
    assert turn is not None and turn.user_text == "영속성 확인"


async def test_get_conversation_store_connects_to_real_postgres() -> None:
    """app.core.conversation.get_conversation_store() 가 실제로 pg-profile 에 연결된다."""
    conversation_module.set_store(None)
    try:
        store = await conversation_module.get_conversation_store()
        assert isinstance(store, PgConversationStore)
        conversation_id = _conversation_id()
        turn_id = await store.save_user_message(conversation_id, "u1", "member", "연결 확인")
        turn = await store.get_turn(turn_id)
        assert turn is not None and turn.user_text == "연결 확인"
    finally:
        conversation_module.set_store(None)


async def test_get_conversation_store_concurrent_calls_single_connection() -> None:
    """동시 get_conversation_store() 호출이 커넥션 풀을 중복 생성하지 않는다(PR #48 리뷰)."""
    conversation_module.set_store(None)
    try:
        stores = await asyncio.gather(
            *(conversation_module.get_conversation_store() for _ in range(10))
        )
        assert len({id(s) for s in stores}) == 1
    finally:
        conversation_module.set_store(None)


async def test_buyer_context_thread_and_turn_commit_together(pool) -> None:
    store = PgConversationStore(pool)
    suffix = uuid.uuid4().hex
    session_id = f"buyer-{suffix}"
    thread_id = f"thread-{suffix}"
    committed = await store.save_user_message(
        f"7:{session_id}",
        "7",
        "member",
        "활동 갱신",
        thread_id=thread_id,
        buyer_session=BuyerSessionInput(session_id, thread_id, "member", "7"),
    )

    async with pool.connection() as conn:
        context = await (
            await conn.execute(
                "SELECT context_id, state FROM chat_session_contexts WHERE session_id=%s",
                (session_id,),
            )
        ).fetchone()
        thread = await (
            await conn.execute(
                "SELECT context_id FROM chat_session_threads WHERE context_id=%s AND thread_id=%s",
                (committed.context_id, thread_id),
            )
        ).fetchone()
        turn_linkage = await (
            await conn.execute(
                "SELECT context_id, session_id FROM conversation_turns WHERE turn_id=%s",
                (committed.turn_id,),
            )
        ).fetchone()
    assert context is not None
    assert (str(context[0]), context[1]) == (committed.context_id, "active")
    assert thread is not None
    assert str(thread[0]) == committed.context_id
    assert turn_linkage is not None
    assert (str(turn_linkage[0]), turn_linkage[1]) == (committed.context_id, session_id)
    assert await store.get_turn(committed.turn_id) is not None


async def test_buyer_chat_operational_failure_returns_503_and_rolls_back_atomic_turn(
    pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실 PG transaction 중 연결계열 실패는 공개 503이며 context/thread/turn을 남기지 않는다."""
    store = PgConversationStore(pool)
    suffix = uuid.uuid4().hex
    session_id = f"buyer-fail-{suffix}"
    thread_id = f"thread-fail-{suffix}"

    async def fail_insert(*_args, **_kwargs):
        raise psycopg.OperationalError("controlled connection loss")

    monkeypatch.setattr(store, "_insert_turn_on_connection", fail_insert)

    async def get_store():
        return store

    monkeypatch.setattr("app.api.chat.get_conversation_store", get_store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/chat",
            json={"sessionId": session_id, "threadId": thread_id, "message": "질문"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STATE_UNAVAILABLE"
    async with pool.connection() as conn:
        context_count = (
            await (
                await conn.execute(
                    "SELECT count(*) FROM chat_session_contexts WHERE session_id=%s",
                    (session_id,),
                )
            ).fetchone()
        )[0]
        turn_count = (
            await (
                await conn.execute(
                    "SELECT count(*) FROM conversation_turns WHERE session_id=%s",
                    (session_id,),
                )
            ).fetchone()
        )[0]
    assert context_count == 0
    assert turn_count == 0


async def test_seller_turn_has_no_buyer_context(pool) -> None:
    store = PgConversationStore(pool)
    seller_session = f"seller-{uuid.uuid4().hex}"
    committed = await store.save_user_message(
        f"12:{seller_session}",
        "12",
        "seller",
        "판매자",
    )

    async with pool.connection() as conn:
        count = (
            await (
                await conn.execute(
                    "SELECT count(*) FROM chat_session_contexts WHERE session_id=%s",
                    (seller_session,),
                )
            ).fetchone()
        )[0]
        turn_linkage = await (
            await conn.execute(
                "SELECT context_id, session_id FROM conversation_turns WHERE turn_id=%s",
                (committed.turn_id,),
            )
        ).fetchone()
    assert committed.context_id is None
    assert count == 0
    assert turn_linkage == (None, None)


async def test_turn_insert_failure_rolls_back_buyer_context_and_thread(
    pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PgConversationStore(pool)
    session_id = f"rollback-{uuid.uuid4().hex}"
    conversation_id = f"99:{session_id}"
    thread_id = f"thread-{uuid.uuid4().hex}"

    async def _fail_insert(*args, **kwargs):
        raise RuntimeError("turn insert unavailable")

    monkeypatch.setattr(store, "_insert_turn_on_connection", _fail_insert)

    with pytest.raises(RuntimeError, match="turn insert unavailable"):
        await store.save_user_message(
            conversation_id,
            "99",
            "member",
            "원자성 확인",
            thread_id=thread_id,
            buyer_session=BuyerSessionInput(session_id, thread_id, "member", "99"),
        )

    assert await store.turns_for(conversation_id) == []
    async with pool.connection() as conn:
        context = await (
            await conn.execute(
                "SELECT 1 FROM chat_session_contexts WHERE session_id=%s",
                (session_id,),
            )
        ).fetchone()
        thread = await (
            await conn.execute(
                "SELECT 1 FROM chat_session_threads WHERE thread_id=%s",
                (thread_id,),
            )
        ).fetchone()
        linkage = await (
            await conn.execute(
                "SELECT context_id, session_id FROM conversation_turns WHERE conversation_id=%s",
                (conversation_id,),
            )
        ).fetchall()
    assert context is None
    assert thread is None
    assert linkage == []
