#!/usr/bin/env bash
# Manda o backup do dia para fora do servidor (Hetzner Storage Box), criptografado.
# Vai junto o .env: sem ele, as chaves guardadas no banco (WhatsApp, Instagram...) não abrem.
# Configurar uma vez: bash setup-offsite.sh   ·   Chamado pelo backup.sh todo dia.
set -euo pipefail
cd "$(dirname "$0")"
val(){ grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- || true; }
T=$(val OFFSITE_TARGET); K=$(val OFFSITE_KEY); KEEP=$(val OFFSITE_DAYS); KEEP=${KEEP:-30}
[ -n "$T" ] && [ -n "$K" ] || exit 0   # ainda não configurado
SSH="ssh -p 23 -i /root/.ssh/oinanda_offsite -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
DB=${1:-$(ls -1t backups/oinanda-2*.archive.gz | head -1)}; DB=$(basename "$DB")
MD=${2:-$(ls -1t backups/media-*.tgz 2>/dev/null | head -1 || true)}; MD=$(basename "${MD:-x}")
OUT="oinanda-$(date +%F).tar.enc"; TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
FILES=("backups/$DB" ".env"); [ -f "backups/$MD" ] && FILES+=("backups/$MD")
tar cf - "${FILES[@]}" | OFFSITE_KEY="$K" openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:OFFSITE_KEY -out "$TMP/$OUT"
rsync -q -e "$SSH" "$TMP/$OUT" "$T:oinanda/$OUT"
# guarda os últimos $KEEP dias lá fora
CUT="oinanda-$(date -d "-$KEEP days" +%F)"
for f in $(echo "ls -1 oinanda" | sftp -q -P 23 -i /root/.ssh/oinanda_offsite -o BatchMode=yes -b - "$T" 2>/dev/null | grep -o 'oinanda-[0-9-]*\.tar\.enc' || true); do
  [[ "$f" < "$CUT" ]] && echo "rm oinanda/$f" | sftp -q -P 23 -i /root/.ssh/oinanda_offsite -o BatchMode=yes -b - "$T" >/dev/null 2>&1 || true
done
SZ=$(du -k "$TMP/$OUT" | cut -f1)
docker compose exec -T mongo mongosh oinanda --quiet --eval "db.settings.updateOne({id:'backup'},{\$set:{offsite_at:new Date(),offsite_kb:$SZ}},{upsert:true})" >/dev/null || true
echo "$(date -Is) cópia fora do servidor ok: $OUT ($(du -h "$TMP/$OUT" | cut -f1))"
