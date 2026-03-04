CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE doc_chunks (
    id            BIGSERIAL PRIMARY KEY,
    doc_path      TEXT NOT NULL,
    doc_hash      TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL,
    chunk_text    TEXT NOT NULL,
    embedding     vector(384) NOT NULL,
    fts           tsvector GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (doc_hash, chunk_index)
);

CREATE INDEX idx_doc_chunks_fts ON doc_chunks USING GIN (fts);

CREATE INDEX idx_doc_chunks_embedding
    ON doc_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_doc_chunks_doc_hash ON doc_chunks (doc_hash);
CREATE INDEX idx_doc_chunks_doc_path ON doc_chunks (doc_path);
