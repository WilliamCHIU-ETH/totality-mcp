#!/bin/sh
# Restores into a NEW throwaway DB, never replaces production.
set -eu
cd "$(dirname "$0")/.."
[ "$#" -eq 1 ] || { echo 'Usage: sh deploy/restore_verify.sh backups/file.dump' >&2; exit 2; }
file="$1"
[ -r "$file" ] || exit 2
sha256sum -c "$file.sha256"
verify="rag_restore_$(date -u +%Y%m%d%H%M%S)_$$"
docker compose exec -T rag-db createdb -U postgres "$verify"
# Only this script's newly created DB may be removed; failures leave it for diagnosis.
docker compose exec -T rag-db pg_restore -U postgres --no-owner --exit-on-error -d "$verify" < "$file"
docker compose exec -T rag-db psql -U postgres -d "$verify" -v ON_ERROR_STOP=1 -c 'SELECT release_id,count(*) FROM rag.chunks GROUP BY release_id; SELECT count(*) FROM rag.chunks WHERE vector_dims(embedding)<>1536;'
docker compose exec -T rag-db dropdb -U postgres "$verify"
echo 'Restore completed in isolated DB. Counts printed above; compare them to the build manifest. Semantic/source audit remains separate.'
