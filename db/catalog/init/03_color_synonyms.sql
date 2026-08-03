-- 색상 표기 동의어 검수 사전 (이슈 #258 — I-1 색상 질의 확장 A 파트).
--
-- term 은 카탈로그 실재 표기 자연키이고, canonical/status 는 사람 검수 결과다. 임베딩 기반
-- 군집과 LLM 다듬기는 제안만 만들며 자동 승인하지 않는다. 임베딩은
-- gemini-embedding-001(dim 1536, 수동 L2 정규화)을 사용한다.
--
-- docker-entrypoint-initdb.d 는 빈 볼륨에서만 1회 실행된다. 기존 DB에는 다음처럼 수동 적용한다:
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
                     CHECK (provenance IN ('seed_pipeline', 'batch_harvest', 'human')),
    doc_count        int,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_color_synonyms_embedding_hnsw
    ON color_synonyms USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_color_synonyms_canonical
    ON color_synonyms (canonical);
