-- 판매자 분석 저장 계층 (이슈 #585, docs/specs/DESIGN-SELLER-ANALYSIS-STORE-585.md)
-- OPS-RUNTIME.md §1.4 확정 DDL을 그대로 옮긴다. 전부 CREATE TABLE/INDEX IF NOT EXISTS —
-- 두 번 적용해도 안전하다. 이후 변경은 06_*.sql로 "추가"만 한다(이 파일은 수정하지 않는다).
--
-- app/agents/seller/analysis_store.py의 ensure_schema()가 부팅 시 같은 문장을 복제해
-- idempotent 생성 + 존재 검증을 한다 — 이 파일이 DDL 정본이며, 06_*.sql 추가 시 ensure_schema()도
-- 같은 커밋에서 함께 갱신한다(docs/specs/DESIGN-SELLER-ANALYSIS-STORE-585.md §9).
--
-- CHECK 제약은 두지 않는다 — 어휘는 애플리케이션 Literal(app/agents/seller/analysis_records.py)로
-- 강제한다(schemas.py 관행, CLAUDE.md 컨벤션).

-- ⓪ 무인 분석 대상 — 접속 시 자동 등록(결정 110). 개인 단위 아님 · 무기한.
CREATE TABLE IF NOT EXISTS seller_analysis_targets (
  brand_id      bigint PRIMARY KEY,
  seller_id     bigint      NOT NULL,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sat_active
  ON seller_analysis_targets (last_seen_at DESC);

-- ① 고객 피처 스냅샷 (개인 단위 — 14일 후 삭제, 호출자는 후속 이슈)
CREATE TABLE IF NOT EXISTS seller_analysis_snapshots (
  id                   uuid PRIMARY KEY,
  brand_id             bigint      NOT NULL,
  period_from          date        NOT NULL,
  period_to            date        NOT NULL,
  computed_at          timestamptz NOT NULL DEFAULT now(),
  source               text        NOT NULL,          -- "i38_v1"
  feature_spec_version text        NOT NULL,          -- "fe_v1"
  total_customers      integer     NOT NULL,
  row_limit            integer     NOT NULL,
  truncated            boolean     NOT NULL,
  insufficient_cohort  boolean     NOT NULL,
  scaler_params        jsonb       NOT NULL,
  pca_used             boolean     NOT NULL,
  pca_params           jsonb,
  silhouette           double precision,
  random_state         integer     NOT NULL,
  clusters             jsonb       NOT NULL,          -- rule_label · display_label 포함
  feature_rows         jsonb       NOT NULL,          -- 최대 1000행 (1~2MB)
  holds                jsonb       NOT NULL DEFAULT '[]',
  UNIQUE (brand_id, period_to, feature_spec_version)  -- 중복 기동 최종 방어선
);
CREATE INDEX IF NOT EXISTS ix_sas_brand_computed
  ON seller_analysis_snapshots (brand_id, computed_at DESC);

-- ② 보고서 (집계 — 무기한)
CREATE TABLE IF NOT EXISTS seller_analysis_reports (
  id             uuid PRIMARY KEY,
  brand_id       bigint      NOT NULL,
  trigger_type   text        NOT NULL,   -- scheduled_daily|scheduled_weekly|event|manual
  period_from    date        NOT NULL,
  period_to      date        NOT NULL,
  compared_from  date,
  compared_to    date,
  title          text        NOT NULL,
  summary        text        NOT NULL,
  report_md      text        NOT NULL,
  segments       jsonb       NOT NULL DEFAULT '[]',
  holds          jsonb       NOT NULL DEFAULT '[]',
  verified       boolean     NOT NULL,
  score_total    integer,
  attempts       integer     NOT NULL,
  snapshot_id    uuid REFERENCES seller_analysis_snapshots(id) ON DELETE SET NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  read_at        timestamptz
);
CREATE INDEX IF NOT EXISTS ix_sar_brand_created
  ON seller_analysis_reports (brand_id, created_at DESC);

-- ③ 추천 (집계 — 무기한)
CREATE TABLE IF NOT EXISTS seller_analysis_recommendations (
  id                  uuid PRIMARY KEY,
  report_id           uuid NOT NULL REFERENCES seller_analysis_reports(id) ON DELETE CASCADE,
  brand_id            bigint   NOT NULL,
  rank                integer  NOT NULL,
  action_type         text     NOT NULL,   -- CHECK 없음 — 애플리케이션 Literal로 막는다
  target_kind         text     NOT NULL DEFAULT 'product',
  segment_label       text     NOT NULL DEFAULT '',
  product_ids         bigint[] NOT NULL DEFAULT '{}',
  title               text     NOT NULL,
  rationale           text     NOT NULL,
  expected_effect     text     NOT NULL DEFAULT '',
  changes              jsonb    NOT NULL DEFAULT '[]',
  effectiveness_score double precision NOT NULL DEFAULT 0.5,
  status              text     NOT NULL DEFAULT 'proposed',  -- proposed|applied|expired|superseded
  applied_at          timestamptz,
  draft_id            text,
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (report_id, rank)
);
CREATE INDEX IF NOT EXISTS ix_sarec_brand_status
  ON seller_analysis_recommendations (brand_id, status, created_at DESC);
-- 07 결정 49: rec_id로 되돌려 쓰는 경로(find_by_draft_id)가 이 인덱스를 탄다.
CREATE INDEX IF NOT EXISTS ix_sarec_draft ON seller_analysis_recommendations (draft_id);

-- ④ 성과 측정 (집계 — 무기한)
CREATE TABLE IF NOT EXISTS seller_analysis_outcomes (
  id                    uuid PRIMARY KEY,
  rec_id                uuid NOT NULL
                        REFERENCES seller_analysis_recommendations(id) ON DELETE CASCADE,
  brand_id              bigint      NOT NULL,
  product_id            bigint,
  action_type           text        NOT NULL,
  applied_at            timestamptz NOT NULL,
  measured_at           timestamptz NOT NULL DEFAULT now(),
  metric_key            text        NOT NULL,
  window_days           integer     NOT NULL,
  outcome_spec_version  text        NOT NULL DEFAULT 'oc_v1',
  treated_pre_succ      integer, treated_pre_trials  integer,
  treated_post_succ     integer, treated_post_trials integer,
  -- 확장(대조군) 자리 — v1은 전부 NULL
  control_pre_succ      integer, control_pre_trials  integer,
  control_post_succ     integer, control_post_trials integer,
  control_products      integer,
  delta_pp              double precision,
  p_value               double precision,
  verdict               text        NOT NULL,
  confounders           jsonb       NOT NULL DEFAULT '[]',
  UNIQUE (rec_id, metric_key)
);
CREATE INDEX IF NOT EXISTS ix_saout_rank
  ON seller_analysis_outcomes (brand_id, action_type, verdict, outcome_spec_version);
