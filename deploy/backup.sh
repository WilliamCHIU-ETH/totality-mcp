#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
umask 077
mkdir -p backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
file="backups/totality-${stamp}.dump"
[ ! -e "$file" ] || { echo 'Backup exists; refusing overwrite' >&2; exit 1; }
docker compose exec -T rag-db pg_dump -U postgres -d totality -Fc --no-owner > "$file.partial"
mv "$file.partial" "$file"
sha256sum "$file" > "$file.sha256"
printf '%s\n' "$file"
# No pruning by default. Keep at least 3 verified copies; review capacity before deleting.
