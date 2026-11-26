#!/usr/bin/env bash
# Define para onde vão as respostas dos e-mails do Oi Nanda (quando o cliente clica em "Responder").
# Uso: bash set-reply.sh atendimento@oinanda.com   (depois rode o update.sh ou: docker compose up -d api)
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
E="${1:-}"; [[ "$E" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[a-z]{2,}$ ]] || { echo "Uso: bash set-reply.sh atendimento@oinanda.com"; exit 1; }
sed -i '/^MAIL_REPLY_TO=/d' .env; echo "MAIL_REPLY_TO=$E" >> .env
docker compose up -d api >/dev/null
echo "Pronto: as respostas dos e-mails agora vão para $E."
