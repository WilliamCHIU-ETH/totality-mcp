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

-- Release v2 migration (also available standalone in deploy/).
-- Idempotent owner migration. Never run with runtime or loader credentials.
BEGIN;
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='rag_owner') THEN
  CREATE ROLE rag_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
 END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='rag_loader') THEN
  CREATE ROLE rag_loader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
 END IF;
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='rag_runtime') THEN
  CREATE ROLE rag_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
 END IF;
END $$;
ALTER ROLE rag_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE rag_loader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE rag_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE rag_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
REVOKE rag_owner FROM rag_loader,rag_reader,rag_runtime;
REVOKE rag_loader FROM rag_reader,rag_runtime;
ALTER TABLE rag.releases ADD COLUMN IF NOT EXISTS videos jsonb;
ALTER TABLE rag.releases ADD COLUMN IF NOT EXISTS sealed boolean NOT NULL DEFAULT true;
GRANT USAGE ON SCHEMA rag TO rag_reader,rag_loader,rag_runtime;
GRANT rag_reader TO rag_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA rag TO rag_reader,rag_loader;
REVOKE INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON ALL TABLES IN SCHEMA rag FROM rag_reader,rag_runtime,rag_loader;
REVOKE CREATE ON SCHEMA rag FROM rag_reader,rag_runtime,rag_loader;
ALTER ROLE rag_runtime SET default_transaction_read_only=on;
-- All writes run through narrow functions; no loader UPDATE/DELETE/DDL grant.
CREATE OR REPLACE FUNCTION rag.stage_release(m jsonb, v jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,rag AS $$
DECLARE old rag.releases;
BEGIN
 PERFORM pg_advisory_xact_lock(8162251);
 IF m->>'release_id' !~ '^r1-[a-f0-9]{20}$' OR jsonb_typeof(v)<>'array'
    OR jsonb_array_length(v)<>(m->>'videos')::int THEN RAISE EXCEPTION 'invalid release metadata'; END IF;
 SELECT * INTO old FROM rag.releases WHERE release_id=m->>'release_id';
 IF FOUND THEN
  IF old.manifest<>m OR old.videos IS DISTINCT FROM v THEN RAISE EXCEPTION 'immutable release conflict'; END IF;
  RETURN old.sealed;
 END IF;
 INSERT INTO rag.releases(release_id,manifest,videos,sealed) VALUES(m->>'release_id',m,v,false);
 RETURN false;
END $$;
CREATE OR REPLACE FUNCTION rag.put_chunk(p jsonb, words text, vec vector) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,rag,public AS $$
BEGIN
 PERFORM 1 FROM rag.releases WHERE release_id=p->>'release_id' AND NOT sealed FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'release not writable'; END IF;
 IF NOT EXISTS (SELECT FROM rag.releases r,jsonb_array_elements(r.videos) v
                WHERE r.release_id=p->>'release_id' AND v->>'video_id'=p->>'video_id')
 THEN RAISE EXCEPTION 'unknown release video'; END IF;
 INSERT INTO rag.chunks(chunk_id,release_id,video_id,content_sha256,model,dimensions,payload,keywords,embedding)
 VALUES(p->>'chunk_id',p->>'release_id',p->>'video_id',p->>'content_sha256',
        'text-embedding-3-small',1536,p,to_tsvector('simple',words),vec);
END $$;
CREATE OR REPLACE FUNCTION rag.seal_release(rid text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,rag AS $$
DECLARE m jsonb; v jsonb;
BEGIN
 SELECT manifest,videos INTO m,v FROM rag.releases WHERE release_id=rid FOR UPDATE;
 IF NOT FOUND OR v IS NULL THEN RAISE EXCEPTION 'missing release metadata'; END IF;
 IF (SELECT count(*) FROM rag.chunks WHERE release_id=rid)<>(m->>'chunks')::int
 OR (SELECT count(DISTINCT video_id) FROM rag.chunks WHERE release_id=rid)<>jsonb_array_length(v)
 THEN RAISE EXCEPTION 'incomplete release'; END IF;
 UPDATE rag.releases SET sealed=true WHERE release_id=rid;
END $$;
CREATE OR REPLACE FUNCTION rag.activate_release(rid text, expected text, allow_initial boolean DEFAULT false, rollback boolean DEFAULT false) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,rag AS $$
DECLARE actual text; m jsonb; v jsonb; ready boolean;
BEGIN
 -- One lock also serializes the empty-table initial publication case.
 PERFORM pg_advisory_xact_lock(8162251);
 SELECT release_id INTO actual FROM rag.active_release WHERE singleton FOR UPDATE;
 IF actual IS DISTINCT FROM expected THEN RAISE EXCEPTION 'CAS base mismatch'; END IF;
 IF actual IS NULL AND NOT allow_initial THEN RAISE EXCEPTION 'initial activation requires allow_initial'; END IF;
 SELECT manifest,videos,sealed INTO m,v,ready FROM rag.releases WHERE release_id=rid;
 IF NOT FOUND OR NOT ready OR v IS NULL THEN RAISE EXCEPTION 'release not ready'; END IF;
 IF NOT rollback AND (m->>'base_release') IS DISTINCT FROM actual THEN RAISE EXCEPTION 'candidate base mismatch'; END IF;
 PERFORM rag.seal_release(rid);
 INSERT INTO rag.active_release VALUES(true,rid) ON CONFLICT(singleton) DO UPDATE SET release_id=excluded.release_id;
END $$;
REVOKE ALL ON FUNCTION rag.stage_release(jsonb,jsonb),rag.put_chunk(jsonb,text,vector),rag.seal_release(text),rag.activate_release(text,text,boolean,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rag.stage_release(jsonb,jsonb),rag.put_chunk(jsonb,text,vector),rag.seal_release(text),rag.activate_release(text,text,boolean,boolean) TO rag_loader;
ALTER SCHEMA rag OWNER TO rag_owner;
ALTER TABLE rag.releases OWNER TO rag_owner;
ALTER TABLE rag.chunks OWNER TO rag_owner;
ALTER TABLE rag.active_release OWNER TO rag_owner;
ALTER FUNCTION rag.stage_release(jsonb,jsonb) OWNER TO rag_owner;
ALTER FUNCTION rag.put_chunk(jsonb,text,vector) OWNER TO rag_owner;
ALTER FUNCTION rag.seal_release(text) OWNER TO rag_owner;
ALTER FUNCTION rag.activate_release(text,text,boolean,boolean) OWNER TO rag_owner;
COMMIT;
