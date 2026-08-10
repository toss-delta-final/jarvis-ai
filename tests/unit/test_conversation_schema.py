"""conversation_turns의 session-primary + thread 병기 계약(#186)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agents.profile import session_activity
from app.core.conversation import PgConversationStore

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INIT_SQL = _REPO_ROOT / "db/profile/init/01_conversation_turns.sql"


class _Context:
    def __init__(self, value) -> None:
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args) -> bool:
        return False


class _Cursor:
    def __init__(self, *, row=None, rowcount: int = 1) -> None:
        self._row = row
        self.rowcount = rowcount

    async def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, *, row=None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    def transaction(self):
        return _Context(self)

    async def execute(self, sql: str, params: tuple | None = None):
        self.calls.append((sql, params))
        return _Cursor(row=self.row)


class _Pool:
    def __init__(self, conn: _Connection) -> None:
        self.conn = conn

    def connection(self):
        return _Context(self.conn)


def test_fresh_profile_schema_declares_thread_id_and_lookup_index() -> None:
    sql = _INIT_SQL.read_text(encoding="utf-8")

    assert "thread_id" in sql
    assert "idx_conversation_turns_thread" in sql


def test_fresh_profile_schema_declares_created_at_retention_index() -> None:
    """보존 스윕(이슈 #321)의 `WHERE created_at < ...` 이 풀스캔이 안 되려면 필요하다."""
    sql = _INIT_SQL.read_text(encoding="utf-8")

    assert "idx_conversation_turns_created_at" in sql


async def test_runtime_setup_adds_thread_id_for_existing_volumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Connection()

    async def _skip_activity_schema(_conn) -> None:
        return None

    monkeypatch.setattr(session_activity, "ensure_schema_on_connection", _skip_activity_schema)
    await PgConversationStore(_Pool(conn)).setup()

    sql = "\n".join(statement for statement, _params in conn.calls)
    assert "ADD COLUMN IF NOT EXISTS thread_id text" in sql
    assert "idx_conversation_turns_thread" in sql
    assert "idx_conversation_turns_created_at" in sql
    assert "CONCURRENTLY" not in sql  # setup() 은 트랜잭션 안이라 못 쓴다


async def test_pg_store_inserts_thread_id() -> None:
    conn = _Connection()
    store = PgConversationStore(_Pool(conn))

    try:
        await store.save_user_message(
            "member:session-a",
            "member",
            "guest",
            "질문",
            thread_id="room-a",
        )
    except TypeError as exc:
        pytest.fail(f"save_user_message가 thread_id를 받지 못함: {exc}")

    sql, params = conn.calls[-1]
    assert "thread_id" in sql
    assert params is not None
    assert params[2] == "room-a"


async def test_pg_store_reads_thread_id_into_turn() -> None:
    inserted_at = datetime(2026, 1, 1, tzinfo=UTC)
    conn = _Connection(
        row=(
            "turn-1",
            "member:session-a",
            "room-a",
            "member",
            "guest",
            "질문",
            "",
            "PENDING",
            inserted_at,
        )
    )

    try:
        turn = await PgConversationStore(_Pool(conn)).get_turn("turn-1")
    except ValueError as exc:
        pytest.fail(f"조회 row의 thread_id를 Turn으로 매핑하지 못함: {exc}")

    assert turn is not None
    assert getattr(turn, "thread_id", None) == "room-a"
    # [F1, #321 리뷰 2라운드] created_at 이 default_factory("읽은 시각")로 새지 않고
    # 실제 삽입 시각(서버 컬럼 값)을 그대로 실어야 한다.
    assert turn.created_at == inserted_at


async def test_purge_expired_turns_issues_bounded_batch_delete_with_skip_locked() -> None:
    """이슈 #321 — 유계 배치 + 동시 finalize_assistant 를 건너뛰는 `FOR UPDATE SKIP LOCKED`."""
    conn = _Connection()
    store = PgConversationStore(_Pool(conn))

    deleted = await store.purge_expired_turns(retention_days=90.0)

    assert deleted == 1  # _Cursor 기본 rowcount
    sql, params = conn.calls[-1]
    assert "DELETE FROM conversation_turns" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "interval '1 day' * %s" in sql
    assert "ORDER BY created_at" in sql
    assert "LIMIT %s" in sql
    assert params[0] == 90.0
