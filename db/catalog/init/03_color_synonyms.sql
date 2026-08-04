-- 색상 표기 동의어 검수 사전 (이슈 #258 — I-1 색상 질의 확장 A 파트).
--
-- term 은 카탈로그 실재 표기 자연키이고, canonical/status 는 사람 검수 결과다. LLM 우선
-- 배정(seed_llm_assignment)과 배치 최근접 임베딩 추정(batch_embedding_unverified)은
-- provenance로 구분하며 둘 다 제안만 만들고 자동 승인하지 않는다. 임베딩은
-- gemini-embedding-001(dim 1536, 수동 L2 정규화)을 사용한다.
--
-- docker-entrypoint-initdb.d 는 빈 볼륨에서만 1회 실행된다. 기존 DB에는 다음처럼 재실행하면
-- 구 provenance 데이터 이행과 CHECK 교체까지 멱등 수행한다:
-- docker exec -i jarvis-ai-pg-catalog-1 psql -U jarvis -d catalog < db/catalog/init/03_color_synonyms.sql

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS color_synonyms (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    term             text NOT NULL UNIQUE,
    canonical        text,
    status           text NOT NULL DEFAULT 'pending_review'
                     CHECK (status IN ('pending_review', 'approved', 'rejected')),
    embedding        vector(1536),
    embedding_model  text,
    provenance       text NOT NULL
                     CHECK (provenance IN (
                         'seed_llm_assignment',
                         'batch_embedding_unverified',
                         'human'
                     )),
    doc_count        int,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

BEGIN;

-- 2026-08-04 마지막 실제 구축은 assign_color_clusters 기반 LLM 우선 배정으로 실행됐다.
-- 따라서 구 seed_pipeline 행은 seed_llm_assignment로 이행한다. batch_harvest는 최근접
-- 임베딩만으로 만든 미검증 제안이므로 batch_embedding_unverified로 이행한다.
-- status·canonical은 갱신 대상에서 제외해 approved/rejected 사람 검수 결과를 보존한다.
ALTER TABLE color_synonyms
    DROP CONSTRAINT IF EXISTS color_synonyms_provenance_check;

UPDATE color_synonyms
SET provenance = CASE provenance
    WHEN 'seed_pipeline' THEN 'seed_llm_assignment'
    WHEN 'batch_harvest' THEN 'batch_embedding_unverified'
    ELSE provenance
END
WHERE provenance IN ('seed_pipeline', 'batch_harvest');

ALTER TABLE color_synonyms
    ADD CONSTRAINT color_synonyms_provenance_check
    CHECK (provenance IN (
        'seed_llm_assignment',
        'batch_embedding_unverified',
        'human'
    ));

COMMIT;

CREATE INDEX IF NOT EXISTS idx_color_synonyms_embedding_hnsw
    ON color_synonyms USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_color_synonyms_canonical
    ON color_synonyms (canonical);
