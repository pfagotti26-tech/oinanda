#!/usr/bin/env bash
# Guarda o ID da configuração do "Entrar com a Meta" (Facebook Login for Business > Configurações).
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
read -rp "Cole o ID da configuração (só números) e aperte Enter: " CFG
CFG="$(echo -n "$CFG" | tr -dc '0-9')"
[ ${#CFG} -ge 6 ] || { echo "Isso não parece um ID de configuração. Nada foi alterado."; exit 1; }
sed -i '/^META_ES_CONFIG_ID=/d' .env && echo "META_ES_CONFIG_ID=$CFG" >> .env
docker compose up -d --force-recreate api >/dev/null
echo "PRONTO: botão Entrar com a Meta ativado (configuração $CFG)."
