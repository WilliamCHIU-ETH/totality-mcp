-- Run as dedicated deployment owner, never as the MCP reader.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS rag;
REVOKE ALL ON SCHEMA rag FROM PUBLIC;
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='rag_reader') THEN
  CREATE ROLE rag_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS rag.releases (
 release_id text PRIMARY KEY,
 manifest jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rag.chunks (
 chunk_id text PRIMARY KEY,
 release_id text NOT NULL REFERENCES rag.releases,
 video_id text NOT NULL,
 content_sha256 text NOT NULL,
 model text NOT NULL CHECK (model='text-embedding-3-small'),
 dimensions int NOT NULL CHECK (dimensions=1536),
 payload jsonb NOT NULL,
 keywords tsvector NOT NULL,
 embedding vector(1536) NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_release_video ON rag.chunks(release_id,video_id);
CREATE INDEX IF NOT EXISTS chunks_keywords ON rag.chunks USING gin(keywords);
-- No ANN index: exact scan at this corpus size.
CREATE TABLE IF NOT EXISTS rag.active_release (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 release_id text NOT NULL REFERENCES rag.releases
);
GRANT USAGE ON SCHEMA rag TO rag_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA rag TO rag_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA rag GRANT SELECT ON TABLES TO rag_reader;
ALTER ROLE rag_reader SET default_transaction_read_only=on;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
DO $$ BEGIN
 EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC',current_database());
END $$;
