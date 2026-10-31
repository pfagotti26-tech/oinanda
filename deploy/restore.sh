#!/usr/bin/env bash
# Restaura um backup:  bash restore.sh backups/oinanda-AAAA-MM-DD-HHMM.archive.gz
set -euo pipefail
cd "$(dirname "$0")"
[ -f "${1:-}" ] || { echo "Uso: bash restore.sh backups/ARQUIVO.archive.gz"; ls -1 backups; exit 1; }
read -rp "Isto SUBSTITUI os dados atuais pelos do backup $1. Digite RESTAURAR para continuar: " ok
[ "$ok" = RESTAURAR ] || exit 1
docker compose exec -T mongo mongorestore --drop --archive="/backups/$(basename "$1")" --gzip
echo "Restaurado."
