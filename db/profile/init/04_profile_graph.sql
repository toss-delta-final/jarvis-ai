-- 개인화 그래프 변경 안전장치 — 감사 · 멱등 원장 · 개인화 중지 플래그 (이슈 #358)
--
-- 이 파일은 docker-entrypoint 가 **빈 볼륨 최초 기동에서만** 실행한다. 이미 데이터가 있는
-- 볼륨에는 적용되지 않으므로 정본은 앱 측 idempotent 마이그레이션
-- (app/agents/profile/graph_journal.py::_ensure_schema)이다 — 두 정의를 함께 고친다.
--
-- 테이블을 셋으로 나눈 이유는 graph_journal.py 모듈 docstring 참조: 원장이 들고 있는 최초 응답
-- 본문에 라벨 원문이 섞이는데(api-spec §3.9.1 `edge.to`), 감사에는 라벨 원문을 둘 수 없다
-- (REQ-PGRAPH-081 [HARD]). 보존 기간도 서로 다르다(REQ-PGRAPH-044).

-- 변경 감사 — 지문만. 전체 초기화도 이 테이블은 지우지 않는다(REQ-PGRAPH-062).
CREATE TABLE IF NOT EXISTS profile_graph_audit (
    id bigserial PRIMARY KEY,
    request_id text NOT NULL,
    actor_fp text NOT NULL,                 -- peppered HMAC. raw userId 금지
    action text NOT NULL,
    edge_id_before text,
    edge_id_after text,
    predicate text,                         -- 고정 enum 이라 원문 기록 가능
    object_fp text,                         -- 라벨의 peppered HMAC. 라벨 원문 금지
    graph_version_before text NOT NULL,
    graph_version_after text NOT NULL,
    -- 이 변경의 자연 키(파생 키) **지문** — 감사 쓰기를 멱등으로 만든다. 크래시 재개가 "감사는
    -- 썼는데 완료 표시 전에 끊긴" 지점에서 다시 시작하면 한 변경이 두 행이 되고, 그건
    -- REQ-PGRAPH-080 을 깬다. 파생 키 원문에는 userId 가 들어 있어 지문으로 바꿔 담는다.
    mutation_fp text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT profile_graph_audit_action_check CHECK (action IN (
        'edgeUpdate', 'edgeDelete', 'graphReset', 'personalizationToggle'
    ))
);

CREATE INDEX IF NOT EXISTS idx_profile_graph_audit_actor
    ON profile_graph_audit (actor_fp, created_at DESC);
-- 감사 쓰기 멱등 — 부분 인덱스인 이유는 구 행의 mutation_fp 가 NULL 이기 때문이다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_graph_audit_mutation
    ON profile_graph_audit (mutation_fp) WHERE mutation_fp IS NOT NULL;
-- revision 하한 조회용 — 문서가 손상돼도 revision 이 되돌아가면 안 된다(REQ-PGRAPH-042).
CREATE INDEX IF NOT EXISTS idx_profile_graph_audit_version
    ON profile_graph_audit (actor_fp, graph_version_after);
CREATE INDEX IF NOT EXISTS idx_profile_graph_audit_created
    ON profile_graph_audit (created_at);

-- 멱등 원장 — 파생 키 profile-graph-{action}:{userId}:{scopeId}:{ifMatch} 가 자연 키다
-- (REQ-PGRAPH-043). user_id 는 **원문** — 지문화하면 pepper 회전 시 원장 히트가 전부 증발한다.
CREATE TABLE IF NOT EXISTS profile_graph_idempotency (
    derived_key text PRIMARY KEY,
    user_id bigint NOT NULL,
    scope_id text,
    request_fp text,                        -- 같은 If-Match·다른 본문의 오재생 차단
    status text NOT NULL DEFAULT 'processing',
    claim_token text,
    lease_expires_at timestamptz,
    response_payload jsonb,                 -- 라벨 미포함 최소 필드
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT profile_graph_idempotency_status_check
        CHECK (status IN ('processing', 'completed'))
);

-- 크래시 잔재 재선점용 — processing 인데 lease 가 만료된 행만 훑는다.
CREATE INDEX IF NOT EXISTS idx_profile_graph_idem_lease
    ON profile_graph_idempotency (lease_expires_at) WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS idx_profile_graph_idem_scope
    ON profile_graph_idempotency (user_id, scope_id);
CREATE INDEX IF NOT EXISTS idx_profile_graph_idem_created
    ON profile_graph_idempotency (created_at);

-- 개인화 중지 — 전용 저장 위치여야 한다(REQ-PGRAPH-050). 요약 항목에 두면 전체 초기화가
-- 지워 중지가 조용히 풀리고, 그래프 문서에 두면 reader 가 조회를 두 번 해야 한다.
-- disabled_spans: 중지 구간 목록 [{"from": iso, "to": iso}, …] (REQ-PGRAPH-055, #359).
-- 배치가 감쇠에서 그만큼을 빼려면 "언제부터 언제까지 시간이 흐르지 않았나"를 알아야 하는데,
-- 그 값을 그래프 문서에 두면 중지·재개가 문서를 고치게 되어 §3.9.5 응답의 graphVersion·
-- 감사·멱등 원장이 전부 거짓이 되고 그래프 잠금까지 잡아야 한다(§7.1·§7.2).
CREATE TABLE IF NOT EXISTS profile_personalization_state (
    user_id bigint PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT true,
    disabled_at timestamptz,
    disabled_spans jsonb NOT NULL DEFAULT '[]'::jsonb,
    graph_version text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
