#!/bin/sh
# Sourced by official PostgreSQL entrypoint during FIRST empty-volume initialization.
# No shell tracing; never echo secrets. Secret file readable inside DB container.
set -eu
RAG_READER_PASSWORD="$(cat /run/secrets/reader_password)"
export RAG_READER_PASSWORD
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv reader_password RAG_READER_PASSWORD
CREATE ROLE rag_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'reader_password';
GRANT rag_reader TO rag_runtime;
ALTER ROLE rag_runtime SET default_transaction_read_only=on;
SQL
unset RAG_READER_PASSWORD
