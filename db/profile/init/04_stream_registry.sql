-- 공유 스트림 레지스트리 (이슈 #476 완료 조건 3, docs/specs/DESIGN-SHARED-STREAM-REGISTRY-476.md)
-- STREAM_REGISTRY_BACKEND=shared 일 때만 사용한다. 기본 배포(memory)는 이 테이블을 만들지도
-- 읽지도 않는다.

-- §2.9(a) 활성 스트림 슬롯. lease_expires_at 이 지난 행은 "없는 것"으로 취급하며, 획득은
-- 조건부 UPSERT 한 문장으로 만료 행을 탈취한다(청소와 획득을 쪼개면 두 워커가 같은 만료
-- 슬롯을 동시에 가져갈 수 있다).
CREATE TABLE IF NOT EXISTS active_streams (
    stream_key text PRIMARY KEY,
    stream_token uuid NOT NULL,
    -- buyer session scope. 판매자 스트림·신원 없는 dev 요청은 NULL 이며 fence 의미가 없다.
    owner_id text,
    session_id text,
    instance_id text NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT active_streams_scope_pairing CHECK (
        (owner_id IS NULL) = (session_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS active_streams_scope_idx
    ON active_streams (owner_id, session_id, lease_expires_at)
    WHERE owner_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS active_streams_lease_idx
    ON active_streams (lease_expires_at);

-- (owner_id, session_id) 스코프 예약. 활성 스트림이 아니므로 activeStreams 관측에 세지 않는다.
CREATE TABLE IF NOT EXISTS stream_scope_fences (
    owner_id text NOT NULL,
    session_id text NOT NULL,
    fence_token uuid NOT NULL,
    instance_id text NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, session_id)
);

CREATE INDEX IF NOT EXISTS stream_scope_fences_lease_idx
    ON stream_scope_fences (lease_expires_at);
