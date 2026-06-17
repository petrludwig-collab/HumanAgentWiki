-- HumanAgentWiki — PostgreSQL schema (pgvector)
-- Run once against a fresh database:  psql -d humanagentwiki -f schema.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- One row per chunk (a note is split into chunks, typically by heading).
CREATE TABLE IF NOT EXISTS chunks (
    id          bigserial PRIMARY KEY,
    file        text NOT NULL,          -- source markdown file (relative path)
    category    text,                   -- user-defined top-level category
    node_type   text,                   -- e.g. 'note' | 'hub'
    title       text,
    links       text[],                 -- [[wikilinks]] found in the chunk
    text        text NOT NULL,          -- chunk content
    meta        jsonb DEFAULT '{}',     -- optional/extensible metadata (date, tags, custom fields)
    embedding   vector(1024),           -- BGE-M3 (1024-dim); change dim if you swap models
    tsv         tsvector,               -- full-text (hybrid search)
    updated_at  timestamptz DEFAULT now()
);

-- File registry for incremental indexing: content hash per file -> re-embed only what changed.
CREATE TABLE IF NOT EXISTS files (
    file        text PRIMARY KEY,
    hash        text NOT NULL,          -- sha256 of file contents
    updated_at  timestamptz DEFAULT now()
);

-- Indexes: vector (HNSW, cosine) + full-text (GIN) + links (GIN) + trigram (fuzzy).
CREATE INDEX IF NOT EXISTS chunks_hnsw  ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv   ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_links ON chunks USING gin (links);
CREATE INDEX IF NOT EXISTS chunks_trgm  ON chunks USING gin (text gin_trgm_ops);
