"""대화 저장 (api-spec §6.3 a) — user/assistant 턴 + 상태 + 부분 텍스트 보존 (이슈 #33).

pg-profile 의 conversation_turns 일반 테이블로 이관 — checkpointer 가 **아니다**(재개용이
아니라 감사·구조화 로그 상관관계 조회용, db/profile/init/01_conversation_turns.sql). 유닛
테스트는 계속 인메모리(ConversationStore)를 주입해 실 인프라 없이 빠르게 돈다
(app/pipelines/artifact_store.py 와 동일 원칙 — 실 스토어 자체 검증은 tests/integration/).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import math
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol

from app.core import session_context
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.pg_resilience import (
    hardened_pg_conninfo,
    is_state_store_unavailable,
    run_with_query_timeout,
)
from app.core.session_context import (
    BuyerSessionInput,
    SessionStateUnavailable,
    resolve_touch_register_on_connection,
)

logger = get_logger(__name__)


class TurnStatus(str, Enum):
    """어시스턴트 응답 저장 상태 (api-spec §6.3 a). PENDING 은 user 저장 직후 초기값."""

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class Turn:
    """한 턴 = user 메시지 원문 + assistant 응답(부분 포함) + 상태."""

    turn_id: str
    conversation_id: str
    thread_id: str | None
    user_id: str | None
    role: str
    user_text: str
    assistant_text: str = ""
    status: TurnStatus = TurnStatus.PENDING
    # [이슈 #321] 보존 스윕의 비교 대상 — ConversationStore(인메모리)가 주입 clock 으로 찍는다.
    # PgConversationStore 는 이 필드를 쓰지 않는다 — 서버 컬럼(`created_at timestamptz
    # DEFAULT now()`)이 시계 권위라 SQL 의 `now()` 를 그대로 쓴다(비대칭은 의도적이다,
    # "고치지" 말 것 — 분산 앱 서버 시계보다 단일 DB 시계가 신뢰 가능하다).
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class CommittedTurn:
    """사용자 턴과 buyer lifecycle context의 commit 결과."""

    turn_id: str
    context_id: str | None


class ConversationStoreProtocol(Protocol):
    """대화 저장소 공유 계약 — ConversationStore(인메모리)·PgConversationStore(pg-profile)."""

    async def save_user_message(
        self,
        conversation_id: str,
        user_id: str | None,
        role: str,
        text: str,
        *,
        thread_id: str | None = None,
        buyer_session: BuyerSessionInput | None = None,
    ) -> CommittedTurn: ...

    async def finalize_assistant(
        self, turn_id: str, assistant_text: str, status: TurnStatus
    ) -> None: ...

    async def get_turn(self, turn_id: str) -> Turn | None: ...

    async def turns_for(self, conversation_id: str) -> list[Turn]: ...

    async def delete_turns_for_user(self, user_id: str) -> int: ...

    async def purge_expired_turns(self, retention_days: float) -> int: ...


class ConversationStore:
    """인메모리 대화 저장소(테스트 전용). conversationId(=sessionId) 별로 턴을 순서대로 보관한다."""

    # 인메모리 안전 상한(테스트/dev 폴백 전용) — pg-profile(디스크 기반)엔 적용하지 않는다.
    # [한계] 전역 FIFO라 한 사용자가 상한을 채우면 무관한 타 사용자의 확정 턴도 축출될 수 있다
    # (cross-tenant). 프로덕션 경로(PgConversationStore)는 이 한계가 없다.
    _MAX_TURNS = 5000

    def __init__(self, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._turns: dict[str, Turn] = {}
        self._by_conversation: dict[str, list[str]] = {}
        self._order: deque[str] = deque()
        self._seq = itertools.count(1)
        # [이슈 #321] 보존 스윕 테스트가 가변 fake clock 을 주입해 시간을 전진시킨다
        # (`time.sleep` 금지 — `reset_store()` 가 테스트마다 새 스토어를 만드므로 생성자 주입이
        # monkeypatch 보다 깨끗하다, `graph_journal`·`test_profile_graph_merge.py` 와 같은 관례).
        self._clock = clock

    async def save_user_message(
        self,
        conversation_id: str,
        user_id: str | None,
        role: str,
        text: str,
        *,
        thread_id: str | None = None,
        buyer_session: BuyerSessionInput | None = None,
    ) -> CommittedTurn:
        """사용자 메시지 수신 즉시 저장(§6.3 a). turn_id 를 반환한다(assistant 마감에 사용)."""
        context_id = None
        if buyer_session is not None:
            context = await session_context._default_repository.touch(buyer_session)
            context_id = context.context_id
        turn_id = f"turn-{next(self._seq)}"
        self._turns[turn_id] = Turn(
            turn_id=turn_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            user_id=user_id,
            role=role,
            user_text=text,
            created_at=self._clock(),
        )
        self._by_conversation.setdefault(conversation_id, []).append(turn_id)
        self._order.append(turn_id)
        self._evict_if_needed()
        return CommittedTurn(turn_id, context_id)

    def _evict_if_needed(self) -> None:
        """상한 초과 시 **확정된** 턴부터 축출(무제한 메모리 증가 방지).

        진행 중(PENDING) 턴은 건너뛴다 — 응답 중 축출되면 finalize 가 유실되기 때문.
        모두 PENDING인 병리적 경우엔 축출을 보류(상한 일시 초과, 곧 확정되며 해소)."""
        attempts = len(self._order)
        while len(self._turns) > self._MAX_TURNS and attempts > 0:
            attempts -= 1
            old_id = self._order.popleft()
            turn = self._turns.get(old_id)
            if turn is None:
                continue  # 이미 제거된 참조
            if turn.status is TurnStatus.PENDING:
                self._order.append(old_id)  # 진행 중 — 축출 보류, 뒤로 미룸
                continue
            self._turns.pop(old_id, None)
            ids = self._by_conversation.get(turn.conversation_id)
            if ids and old_id in ids:
                ids.remove(old_id)
                if not ids:
                    del self._by_conversation[turn.conversation_id]

    async def finalize_assistant(
        self, turn_id: str | CommittedTurn, assistant_text: str, status: TurnStatus
    ) -> None:
        """어시스턴트 응답을 상태와 함께 마감한다. FAILED/CANCELLED 도 부분 텍스트를 보존한다."""
        normalized_turn_id = _turn_id(turn_id)
        turn = self._turns.get(normalized_turn_id)
        if turn is None:
            # 축출됐거나 미지의 turn — 응답이 저장소에서 유실됨(관측 가능하게 경고).
            logger.warning(
                "finalize on evicted/unknown turn_id=%s (assistant 응답 유실)",
                normalized_turn_id,
            )
            return
        turn.assistant_text = assistant_text
        turn.status = status

    async def get_turn(self, turn_id: str | CommittedTurn) -> Turn | None:
        return self._turns.get(_turn_id(turn_id))

    async def turns_for(self, conversation_id: str) -> list[Turn]:
        return [self._turns[t] for t in self._by_conversation.get(conversation_id, [])]

    async def delete_turns_for_user(self, user_id: str) -> int:
        """전체 초기화 — 이 사용자의 전사록을 지운다 (#358, REQ-PGRAPH-061)."""
        doomed = [tid for tid, turn in self._turns.items() if turn.user_id == user_id]
        for turn_id in doomed:
            self._turns.pop(turn_id, None)
        for conversation_id, turn_ids in list(self._by_conversation.items()):
            remaining = [t for t in turn_ids if t in self._turns]
            if remaining:
                self._by_conversation[conversation_id] = remaining
            else:
                self._by_conversation.pop(conversation_id, None)
        return len(doomed)

    async def purge_expired_turns(self, retention_days: float) -> int:
        """보존 스윕 한 배치(이슈 #321) — cutoff 이전 턴 중 가장 오래된 것부터 배치 상한만큼.

        `_evict_if_needed` 는 PENDING 을 일부러 건너뛰지만(진행 중 응답 유실 방지), 시간 만료는
        그 규칙을 **따르지 않는다** — retention_days 만큼 묵은 PENDING 은 죽은 스트림이라, 예외를
        두면 TTL 이 지우려던 것이 정확히 그만큼 영구히 남는다.
        """
        settings = get_settings()
        cutoff = self._clock() - timedelta(days=retention_days)
        expired = sorted(
            (turn_id for turn_id, turn in self._turns.items() if turn.created_at < cutoff),
            key=lambda turn_id: self._turns[turn_id].created_at,
        )
        doomed = expired[: settings.conversation_retention_batch_size]
        for turn_id in doomed:
            turn = self._turns.pop(turn_id, None)
            if turn is None:
                continue
            ids = self._by_conversation.get(turn.conversation_id)
            if ids and turn_id in ids:
                ids.remove(turn_id)
                if not ids:
                    del self._by_conversation[turn.conversation_id]
            with contextlib.suppress(ValueError):
                self._order.remove(turn_id)
        return len(doomed)


class PgConversationStore:
    """pg-profile conversation_turns 테이블 기반 스토어. ConversationStore 와 동일 인터페이스."""

    def __init__(self, pool) -> None:  # noqa: ANN001 - psycopg_pool.AsyncConnectionPool(지연 임포트)
        self._pool = pool

    async def setup(self) -> None:
        """기존 볼륨은 이전 논리 순서로 백필하고 신규 turn은 DB sequence로 정렬한다."""
        settings = get_settings()
        migration_timeout_ms = max(1, math.ceil(settings.state_store_migration_timeout_s * 1000))

        async def _run() -> None:
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (str(migration_timeout_ms),),
                    )
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        ("schema:conversation_turns:sequence_id",),
                    )
                    await conn.execute(
                        """
                        DO $$
                        BEGIN
                            IF NOT EXISTS (
                                SELECT 1
                                FROM information_schema.columns
                                WHERE table_schema = current_schema()
                                  AND table_name = 'conversation_turns'
                                  AND column_name = 'sequence_id'
                            ) THEN
                                ALTER TABLE conversation_turns ADD COLUMN sequence_id bigint;
                                CREATE SEQUENCE IF NOT EXISTS conversation_turns_sequence_id_seq;
                                ALTER SEQUENCE conversation_turns_sequence_id_seq
                                    OWNED BY conversation_turns.sequence_id;
                                WITH ordered AS (
                                    SELECT turn_id,
                                           row_number() OVER (ORDER BY created_at, turn_id) AS seq
                                    FROM conversation_turns
                                )
                                UPDATE conversation_turns AS turns
                                SET sequence_id = ordered.seq
                                FROM ordered
                                WHERE turns.turn_id = ordered.turn_id;
                                PERFORM setval(
                                    'conversation_turns_sequence_id_seq',
                                    GREATEST(
                                        COALESCE((SELECT MAX(sequence_id) FROM conversation_turns), 0)
                                            + 1,
                                        1
                                    ),
                                    false
                                );
                                ALTER TABLE conversation_turns
                                    ALTER COLUMN sequence_id
                                    SET DEFAULT nextval('conversation_turns_sequence_id_seq');
                                ALTER TABLE conversation_turns
                                    ALTER COLUMN sequence_id SET NOT NULL;
                            END IF;
                        END $$
                        """
                    )
                    await conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_conversation_turns_sequence "
                        "ON conversation_turns (conversation_id, sequence_id)"
                    )
                    # 기존 볼륨에는 thread_id가 없으므로 런타임 멱등 migration으로 보강한다.
                    # 기존 턴은 방 정보를 복원할 수 없어 NULL을 유지하고, 신규 턴부터 기록한다.
                    await conn.execute(
                        "ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS thread_id text"
                    )
                    await conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_conversation_turns_thread "
                        "ON conversation_turns (conversation_id, thread_id)"
                    )
                    # 전체 초기화가 사용자별로 지운다(#358, SPEC-PROFILE-GRAPH-149 §7.2).
                    # 이 인덱스가 없으면 그 DELETE 가 풀스캔이라, 대화가 쌓일수록 초기화 하나가
                    # 테이블 전체를 훑는다(§12 선결조건 7).
                    await conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_conversation_turns_user "
                        "ON conversation_turns (user_id)"
                    )
                    # 보존 스윕(이슈 #321) 의 `WHERE created_at < ...` 조회용. 기존 인덱스
                    # (conversation_id, sequence_id)·(conversation_id, thread_id)·(user_id) 는
                    # 선두 컬럼이 이 조건과 맞지 않아 못 쓴다. `CONCURRENTLY` 는 쓰지 않는다 —
                    # 트랜잭션 밖에서만 가능한데 이 setup() 은 전체가 트랜잭션이다.
                    await conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_conversation_turns_created_at "
                        "ON conversation_turns (created_at)"
                    )

        await asyncio.wait_for(_run(), timeout=settings.state_store_migration_timeout_s)

    async def _execute(self, sql: str, params: tuple) -> int:
        """쓰기 쿼리(연결 획득+실행)를 실행 상한으로 감싼다.

        pg 가 응답 없이 멈추면 이 await 가 영영 안 끝나 commit_user_message() 가 반환하지
        못하고 해당 threadId의 동시 스트림 슬롯이 영구히 잠긴다(§2.9 a, PR #48 후속 리뷰).
        """

        async def _run() -> int:
            async with self._pool.connection() as conn:
                cursor = await conn.execute(sql, params)
                return cursor.rowcount

        return await run_with_query_timeout(_run())

    async def save_user_message(
        self,
        conversation_id: str,
        user_id: str | None,
        role: str,
        text: str,
        *,
        thread_id: str | None = None,
        buyer_session: BuyerSessionInput | None = None,
    ) -> CommittedTurn:
        turn_id = uuid.uuid4().hex
        if buyer_session is None:
            await self._execute(
                "INSERT INTO conversation_turns "
                "(turn_id, conversation_id, thread_id, user_id, role, user_text) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (turn_id, conversation_id, thread_id, user_id, role, text),
            )
            return CommittedTurn(turn_id, None)

        async def _run() -> str:
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    context = await resolve_touch_register_on_connection(conn, buyer_session)
                    await self._insert_turn_on_connection(
                        conn,
                        turn_id,
                        conversation_id,
                        thread_id,
                        user_id,
                        role,
                        text,
                        context.context_id,
                        buyer_session.session_id,
                    )
                    return context.context_id

        try:
            context_id = await run_with_query_timeout(_run())
        except Exception as exc:
            if is_state_store_unavailable(exc):
                raise SessionStateUnavailable from exc
            raise
        return CommittedTurn(turn_id, context_id)

    async def _insert_turn_on_connection(
        self,
        conn,
        turn_id: str,
        conversation_id: str,
        thread_id: str | None,
        user_id: str | None,
        role: str,
        text: str,
        context_id: str,
        session_id: str,
    ) -> None:
        await conn.execute(
            "INSERT INTO conversation_turns "
            "(turn_id, conversation_id, thread_id, user_id, role, user_text, "
            "context_id, session_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                turn_id,
                conversation_id,
                thread_id,
                user_id,
                role,
                text,
                context_id,
                session_id,
            ),
        )

    async def finalize_assistant(
        self, turn_id: str | CommittedTurn, assistant_text: str, status: TurnStatus
    ) -> None:
        normalized_turn_id = _turn_id(turn_id)
        rowcount = await self._execute(
            "UPDATE conversation_turns SET assistant_text = %s, status = %s WHERE turn_id = %s",
            (assistant_text, status.value, normalized_turn_id),
        )
        if rowcount == 0:
            logger.warning("conversation turn finalize 대상 없음: turn_id=%s", normalized_turn_id)

    async def get_turn(self, turn_id: str | CommittedTurn) -> Turn | None:
        # 읽기도 쓰기(_execute)와 동일 실행 상한 — 타임아웃이 없으면 pg 가 멈출 때 이 await 가
        # 영영 안 끝날 뿐 아니라 연결이 풀에 물린 채 반환되지 않아, 풀 고갈로 타임아웃이 걸린
        # 쓰기 경로(슬롯 확보/마감)까지 연쇄로 막힌다(PR #48 후속 리뷰).
        async def _run() -> Turn | None:
            async with self._pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT turn_id, conversation_id, thread_id, user_id, role, user_text, "
                        "assistant_text, status, created_at "
                        "FROM conversation_turns WHERE turn_id = %s",
                        (_turn_id(turn_id),),
                    )
                ).fetchone()
            return _row_to_turn(row) if row else None

        return await run_with_query_timeout(_run())

    async def turns_for(self, conversation_id: str) -> list[Turn]:
        async def _run() -> list[Turn]:
            async with self._pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT turn_id, conversation_id, thread_id, user_id, role, user_text, "
                        "assistant_text, status, created_at "
                        "FROM conversation_turns WHERE conversation_id = %s "
                        "ORDER BY sequence_id",
                        (conversation_id,),
                    )
                ).fetchall()
            return [_row_to_turn(row) for row in rows]

        return await run_with_query_timeout(_run())

    async def delete_turns_for_user(self, user_id: str) -> int:
        """전체 초기화 — 이 사용자의 전사록을 물리 삭제하고 지운 행 수를 돌려준다.

        컬럼이 `text` 라 호출부가 `str(user_id)` 로 넘긴다(BIGINT 표기 불일치, SPEC §12-7).
        저장되는 값은 회원 id 문자열이고(`observability` 의 `identity.user_id or subject`),
        게스트 턴은 게스트 subject 라 매칭되지 않는다 — 이 경로에 게스트는 없으므로 정상이다.

        `setup()` 이 만드는 `idx_conversation_turns_user` 가 없으면 이 DELETE 가 풀스캔이다.
        """
        return await self._execute("DELETE FROM conversation_turns WHERE user_id = %s", (user_id,))

    async def purge_expired_turns(self, retention_days: float) -> int:
        """보존 스윕 한 배치(이슈 #321) — 호출부(`app/pipelines/scheduler.py`)가 배치 상한에
        도달할 때까지 반복 호출한다(ConversationStore 인메모리 구현과 같은 계약).

        `now()` 는 **서버 시각**으로 계산한다(`graph_journal.py` 와 같은 방식, Pg 가 시계
        권위다). cutoff 는 `make_interval(days => ...)` 가 아니라 `interval '1 day' * %s` 로
        곱한다 — `make_interval` 의 `days` 인자는 정수라 `conversation_retention_days` 같은
        실수(float) 설정을 바인딩하면 `UndefinedFunction` 으로 죽는다(실측). `FOR UPDATE
        SKIP LOCKED` 로 동시에 도는 `finalize_assistant` UPDATE 에 막히지 않고 건너뛴다.
        배치당 짧은 트랜잭션 1개로 끝난다 — 한 번에 통째로 지우면 장수 트랜잭션이 autovacuum 을
        막아 bloat 가 터진다. 중간에 타임아웃이 나도 DELETE 는 멱등이라 다음 tick 이 이어받는다.
        """
        settings = get_settings()
        return await self._execute(
            "DELETE FROM conversation_turns "  # noqa: S608 - 리터럴만 조립, 파라미터는 바인딩
            "WHERE turn_id IN ("
            "    SELECT turn_id FROM conversation_turns "
            "    WHERE created_at < now() - (interval '1 day' * %s) "
            "    ORDER BY created_at "
            "    LIMIT %s "
            "    FOR UPDATE SKIP LOCKED"
            ")",
            (retention_days, settings.conversation_retention_batch_size),
        )


def _row_to_turn(row: tuple) -> Turn:
    (
        turn_id,
        conversation_id,
        thread_id,
        user_id,
        role,
        user_text,
        assistant_text,
        status,
        created_at,
    ) = row
    return Turn(
        turn_id=turn_id,
        conversation_id=conversation_id,
        thread_id=thread_id,
        user_id=user_id,
        role=role,
        user_text=user_text,
        assistant_text=assistant_text,
        status=TurnStatus(status),
        # [F1, #321 리뷰 2라운드] 없이 두면 default_factory 가 "읽은 시각"을 채워 pg 조회
        # 결과의 created_at 이 조용히 거짓이 된다 — 실제 삽입 시각(서버 컬럼)을 그대로 싣는다.
        created_at=created_at,
    )


def _turn_id(value: str | CommittedTurn) -> str:
    return value.turn_id if isinstance(value, CommittedTurn) else value


def conversation_key(subject: str | None, session_id: str) -> str:
    """대화 저장 키를 **신원에 스코프**한다(registry_key와 동일 IDOR 방지).

    session_id(요청 본문 유래)만으로 키잉하면 다른 신원이 같은 session_id 를 실어 한 대화에
    턴을 혼입시킬 수 있다(프로필 스캔 오염·히스토리 노출). subject(검증된 sub)를 접두어로 묶어
    사용자 간 대화 혼입을 막는다. 신원 없음(dev 무토큰)은 "anon".
    """
    return f"{subject or 'anon'}:{session_id}"


_store: ConversationStoreProtocol | None = None
_pool_ctx: object | None = None  # AsyncConnectionPool cm — 앱 수명 동안 GC 방지
_fallback_warned = False
_init_lock = asyncio.Lock()
_pending_cleanup: list[
    object
] = []  # set_store() 가 못 닫은 이전 풀 — get_conversation_store() 진입 시 정리


def set_store(store: ConversationStoreProtocol | None) -> None:
    """store 교체(테스트용) — None 이면 다음 사용 시 재초기화한다.

    기존 `_pool_ctx`(실제 연결된 풀)가 있으면 정리 대기열에 넣는다. 이 함수는 sync 라
    여기서 직접 await 할 수 없고, `asyncio.get_running_loop()` fire-and-forget 태스크
    방식은 **실행 중인 루프가 없으면 조용히 스킵**된다 — `tests/conftest.py` 의 sync
    autouse fixture 가 정확히 그 상황이라(이벤트 루프 시작 전) 풀이 정리 없이 영구
    누수된다(app/core/pg_store.py·app/agents/profile/processed_events.py 에서 이미
    고친 것과 동일 버그가 이 모듈에 재도입돼 있었다, PR #48 후속 리뷰). 대신 다음
    `get_conversation_store()` 호출(반드시 async 컨텍스트) 시점에 확실히 정리한다.
    """
    global _store, _pool_ctx
    old_pool = _pool_ctx
    _store = store
    _pool_ctx = None
    if old_pool is not None:
        _pending_cleanup.append(old_pool)


async def _drain_pending_cleanup(*, propagate_errors: bool = False) -> None:
    """대기열의 이전 풀들을 닫는다 — 다른(이미 소멸한) 이벤트 루프에서 만들어진 풀일 수 있다.

    `AsyncConnectionPool` 은 백그라운드 워커 태스크를 그 풀을 만든 이벤트 루프에 묶어
    두므로 cross-loop close 가 `CancelledError` 를 낼 수 있다. `BaseException` 째로
    삼키면 이 await 지점에서 **현재 태스크 자체**가 실제로 취소되는 경우까지 함께
    삼켜져 취소가 무시되는 안티패턴이 된다(processed_events.py 와 동일 근거·수정,
    PR #47 후속 리뷰) — `task.cancelling()` 으로 구분해 실제 취소 요청만 다시 던진다.
    """
    first_error: Exception | None = None
    while _pending_cleanup:
        pool = _pending_cleanup.pop()
        try:
            await pool.close()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
        except Exception as exc:
            logger.warning("conversation pool cleanup failed", exc_info=True)
            if first_error is None:
                first_error = exc
    if propagate_errors and first_error is not None:
        raise first_error


async def close_store() -> None:
    """지금 열려 있는 풀을 **이 이벤트 루프에서** 닫는다 (이슈 #208).

    sync `set_store()` 가 미룬 close 는 보통 다른 루프에서 실행된다. 살아 있는 풀을 남긴 채
    루프가 닫히면 teardown 의 `_cancel_all_tasks()` 가 취소를 삼키는 psycopg 워커와 교착한다
    (app/agents/profile/processed_events.py `close_pool` 과 동일 근거).
    """
    set_store(None)
    await _drain_pending_cleanup(propagate_errors=True)


async def get_conversation_store() -> ConversationStoreProtocol:
    """대화 저장소 — pg-profile(conversation_turns) 지연 초기화, 실패 시 dev 한정 인메모리 폴백.

    락 없는 지연 초기화는 콜드 스타트 시 동시 요청이 커넥션 풀을 중복 생성해
    앞선 풀(들)이 close 없이 버려지는 레이스가 있다 — `_init_lock` 으로 초기화
    블록 전체를 직렬화한다(app/core/pg_store.py 와 동일 패턴, PR #48 리뷰).
    """
    global _store, _pool_ctx, _fallback_warned
    await _drain_pending_cleanup()
    settings = get_settings()
    await asyncio.wait_for(
        _init_lock.acquire(),
        timeout=settings.state_store_query_timeout_s,
    )
    try:
        if _store is None:
            pool = None
            try:
                from psycopg_pool import AsyncConnectionPool  # noqa: PLC0415

                pool = AsyncConnectionPool(
                    hardened_pg_conninfo(settings.profile_db_url),
                    open=False,
                    min_size=settings.state_store_pool_min_size,
                    max_size=settings.state_store_pool_max_size,
                    timeout=settings.state_store_query_timeout_s,
                )
                await asyncio.wait_for(
                    pool.open(wait=True), timeout=settings.state_store_connect_timeout_s
                )
                store = PgConversationStore(pool)
                await store.setup()
                _pool_ctx = pool
                _store = store
            except asyncio.CancelledError:
                # disconnect 등으로 pool.open() 도중 요청 태스크가 취소되면 CancelledError
                # (BaseException)라 아래 except Exception 이 못 잡아, 방금 만든 풀(+백그라운드
                # 워커)이 close 없이 샜다 — get_conversation_store() 는 open_stream 진입 전
                # chat/seller 핸들러에서 호출되므로 이 취소가 실제로 도달한다(store.py·
                # processed_events.py 와 동일 클래스, PR #48 후속 리뷰). 여기 풀은 이 루프에서
                # 방금 만든 것이라 취소는 항상 실제 취소 — best-effort 로 닫고 그대로 전파한다.
                if pool is not None:
                    with contextlib.suppress(Exception):
                        await pool.close()
                raise
            except Exception as exc:
                if pool is not None:
                    # open() 부분 실패(타임아웃 등) — 이미 생성된 풀을 닫아 커넥션 누수 방지.
                    with contextlib.suppress(Exception):
                        await pool.close()
                if settings.auth_mode == "jwks":
                    raise  # 운영 — 폴백 금지(대화 저장·감사 로그가 조용히 증발하면 안 된다)
                if not _fallback_warned:
                    logger.warning(
                        "pg-profile conversation_turns 연결 실패(%s) — 인메모리 폴백 "
                        "(dev 전용: 프로세스 재시작 시 대화 이력 증발)",
                        exc,
                    )
                    _fallback_warned = True
                _store = ConversationStore()
    finally:
        _init_lock.release()
    return _store


def reset_store() -> None:
    """테스트용 — 저장소 초기화(인메모리로 되돌림).

    `_init_lock` 도 새로 만든다 — pytest-asyncio 는 테스트 함수마다 새 이벤트 루프를
    쓰는데, 모듈 전역 asyncio.Lock 을 여러 루프에 걸쳐 재사용하면 이전 루프에 묶인
    내부 상태로 다음 테스트에서 락 획득이 영원히 안 풀리는 hang 이 발생할 수 있다
    (app/core/pg_store.py 와 동일 문제, 실제 재현·수정 이력은 docs/lessons.md).
    """
    global _init_lock
    session_context.reset()
    set_store(ConversationStore())
    _init_lock = asyncio.Lock()
