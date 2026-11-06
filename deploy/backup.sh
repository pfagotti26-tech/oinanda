#!/usr/bin/env bash
# Backup do banco em deploy/backups (um por dia, guarda 14). Roda sozinho todo dia às 03:30.
set -euo pipefail
cd "$(dirname "$0")"
F="oinanda-$(date +%F-%H%M).archive.gz"
docker compose exec -T mongo mongodump --db=oinanda --archive="/backups/$F" --gzip --quiet
find backups -name 'oinanda-*.archive.gz' -mtime +14 -delete
echo "$(date -Is) backup ok: $F ($(du -h "backups/$F" | cut -f1))"
# arquivos das conversas (fotos, áudios, documentos): guarda 7 dias
M="media-$(date +%F-%H%M).tgz"
docker compose exec -T api tar czf - -C /media . > "backups/$M" 2>/dev/null || rm -f "backups/$M"
find backups -name 'media-*.tgz' -mtime +7 -delete
[ -f "backups/$M" ] && echo "$(date -Is) backup dos arquivos ok: $M ($(du -h "backups/$M" | cut -f1))"
