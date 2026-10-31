#!/usr/bin/env bash
# Liga o domínio oficial. Rode só DEPOIS que o DNS de oinanda.com.br apontar para este servidor.
set -euo pipefail
cd "$(dirname "$0")"
IP="$(curl -4fsS https://api.ipify.org)"
for h in oinanda.com.br www.oinanda.com.br; do
  r="$(getent ahostsv4 "$h" | awk '{print $1; exit}')"
  [ "$r" = "$IP" ] || { echo "$h ainda aponta para '${r:-nada}', e não para $IP. Espere o DNS propagar e rode de novo."; exit 1; }
done
TEST_HOST="$(echo "$IP" | tr . -).sslip.io"
sed -i "s|^SITE_HOSTS=.*|SITE_HOSTS=oinanda.com.br, ${TEST_HOST}|; s|^WWW_HOST=.*|WWW_HOST=www.oinanda.com.br|" .env
docker compose up -d --force-recreate web
echo "PRONTO: https://oinanda.com.br"
