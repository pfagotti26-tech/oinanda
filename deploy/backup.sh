#!/usr/bin/env bash
# Backup do banco em deploy/backups (um por dia, guarda 14). Roda sozinho todo dia às 03:30.
# Com a cópia fora do servidor configurada (setup-offsite.sh), manda uma cópia criptografada para o Storage Box.
# PRE=1 bash backup.sh  -> só o banco, rápido (o update.sh usa antes de cada atualização)
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p backups
TAG=$([ "${PRE:-}" = 1 ] && echo "antes-update-" || true)
F="oinanda-${TAG}$(date +%F-%H%M).archive.gz"
docker compose exec -T mongo mongodump --db=oinanda --archive="/backups/$F" --gzip --quiet
find backups -name 'oinanda-*.archive.gz' -mtime +14 -delete
echo "$(date -Is) backup ok: $F ($(du -h "backups/$F" | cut -f1))"
[ "${PRE:-}" = 1 ] && { echo "$F" > backups/.ultimo-antes-update; exit 0; }
# registra no banco para o admin (Visão geral › Operação) mostrar quando foi o último backup
SZ=$(du -k "backups/$F" | cut -f1)
docker compose exec -T mongo mongosh oinanda --quiet --eval "db.settings.updateOne({id:'backup'},{\$set:{last_at:new Date(),file:'$F',size_kb:$SZ}},{upsert:true})" >/dev/null || true
# arquivos das conversas (fotos, áudios, documentos): guarda 7 dias
M="media-$(date +%F-%H%M).tgz"
docker compose exec -T api tar czf - -C /media . > "backups/$M" 2>/dev/null || rm -f "backups/$M"
find backups -name 'media-*.tgz' -mtime +7 -delete
[ -f "backups/$M" ] && echo "$(date -Is) backup dos arquivos ok: $M ($(du -h "backups/$M" | cut -f1))"
# cópia fora do servidor (se o servidor sumir, os dados não somem junto)
bash offsite.sh "$F" "$M" || echo "$(date -Is) AVISO: a cópia fora do servidor falhou. Veja acima."
