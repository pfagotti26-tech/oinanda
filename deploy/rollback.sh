#!/usr/bin/env bash
# Volta o sistema para a versão que estava no ar antes da última atualização.
#   bash rollback.sh              -> volta só o sistema; os dados ficam como estão (o normal)
#   bash rollback.sh --com-banco  -> volta também o banco para o backup feito antes da atualização
#                                    (perde o que entrou depois dele: use só se a atualização estragou dados)
set -euo pipefail
cd "$(dirname "$0")"
[ -f backups/.versao-anterior ] || { echo "Não achei a versão anterior. Ela é gravada pelo update.sh a cada atualização."; exit 1; }
PREV=$(cat backups/.versao-anterior)
echo "Voltando para a versão $(git -C .. log -1 --format='%h %s' "$PREV")..."
git -C .. reset -q --hard "$PREV"
if [ "${1:-}" = "--com-banco" ]; then
  BK=$(cat backups/.ultimo-antes-update 2>/dev/null || true)
  [ -n "$BK" ] && [ -f "backups/$BK" ] || { echo "Não achei o backup de antes da atualização."; exit 1; }
  bash restore.sh "backups/$BK"
fi
docker compose up -d --build --remove-orphans
for i in $(seq 1 40); do docker compose exec -T api python -c "import urllib.request;urllib.request.urlopen('http://localhost:8001/api/health')" 2>/dev/null && { OK=1; break; }; sleep 3; done
[ "${OK:-}" = 1 ] && echo "VOLTOU: https://app.oinanda.com.br está na versão $(git -C .. log -1 --format='%h')." || echo "O sistema ainda não respondeu. Veja: docker compose logs api --tail 50"
echo "Atenção: o próximo update.sh traz a versão nova de novo. Rode só depois da correção."
