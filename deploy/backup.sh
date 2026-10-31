#!/usr/bin/env bash
# Backup do banco em deploy/backups (um por dia, guarda 14). Roda sozinho todo dia às 03:30.
set -euo pipefail
cd "$(dirname "$0")"
F="oinanda-$(date +%F-%H%M).archive.gz"
docker compose exec -T mongo mongodump --db=oinanda --archive="/backups/$F" --gzip --quiet
find backups -name 'oinanda-*.archive.gz' -mtime +14 -delete
echo "$(date -Is) backup ok: $F ($(du -h "backups/$F" | cut -f1))"
