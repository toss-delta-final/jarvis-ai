-- 대화 저장(감사·조회 전용) 테이블 (이슈 #33, api-spec §6.3 a).
--
-- ConversationStore(인메모리 placeholder)를 대체 — checkpointer 가 아니다(재개용 아니라
-- 감사·구조화 로그 상관관계 조회용, app/core/conversation.py PgConversationStore 참고).
--
-- docker-entrypoint-initdb.d 는 컨테이너가 "완전히 새로" 뜰 때(빈 볼륨) 1회만 실행한다.

CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id         text PRIMARY KEY,
    sequence_id     bigserial NOT NULL,
    conversation_id text NOT NULL,
    thread_id       text,
    context_id      uuid,
    session_id      text,
    user_id         text,
    role            text NOT NULL,
    user_text       text NOT NULL,
    assistant_text  text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'PENDING',
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- turns_for(conversation_id) 조회 + 실제 INSERT 순서 정렬용.
CREATE INDEX IF NOT EXISTS idx_conversation_turns_sequence
    ON conversation_turns (conversation_id, sequence_id);

-- session(접속) 내 thread(방)별 턴 조회용. 기존 볼륨의 과거 행은 NULL을 허용한다.
CREATE INDEX IF NOT EXISTS idx_conversation_turns_thread
    ON conversation_turns (conversation_id, thread_id);

-- 보존 스윕(이슈 #321) 의 `WHERE created_at < ...` 조회용 — 신규 볼륨도 기존 볼륨과 같은
-- 인덱스를 갖도록 app/core/conversation.py::PgConversationStore.setup() 의 멱등 마이그레이션과
-- 짝을 맞춘다.
CREATE INDEX IF NOT EXISTS idx_conversation_turns_created_at
    ON conversation_turns (created_at);
