#!/bin/sh
# First empty-volume init only. Existing installations use the owner migration.
set -eu
RAG_READER_PASSWORD="$(cat /run/secrets/reader_password)"
RAG_LOADER_PASSWORD="$(cat /run/secrets/loader_password)"
export RAG_READER_PASSWORD RAG_LOADER_PASSWORD
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv reader_password RAG_READER_PASSWORD
\getenv loader_password RAG_LOADER_PASSWORD
ALTER ROLE rag_runtime LOGIN PASSWORD :'reader_password';
CREATE ROLE rag_ingest LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'loader_password';
GRANT rag_loader TO rag_ingest;
ALTER ROLE rag_runtime SET default_transaction_read_only=on;
SQL
unset RAG_READER_PASSWORD RAG_LOADER_PASSWORD
