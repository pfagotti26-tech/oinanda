#!/usr/bin/env bash
# Garante que o deploy/.env tem todas as variáveis do sistema (não apaga nada que já exista).
set -euo pipefail
cd "$(dirname "$0")"
touch .env; chmod 600 .env
add(){ grep -q "^$1=" .env || echo "$1=$2" >> .env; }
add ACME_EMAIL pfagotti26@gmail.com
add APP_HOSTS app.oinanda.com.br
add APP_ORIGINS https://app.oinanda.com.br
add ADMIN_EMAIL pfagotti26@gmail.com
add NTFY_TOPIC ""
# etapa 2 — WhatsApp
add META_APP_SECRET ""
grep -q '^WA_VERIFY_TOKEN=' .env || echo "WA_VERIFY_TOKEN=$(openssl rand -hex 16)" >> .env
grep -q '^SECRET_KEY=' .env || echo "SECRET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')" >> .env
add APP_PUBLIC_URL https://app.oinanda.com.br
# etapa 3 — Nanda (IA)
add ANTHROPIC_API_KEY ""
add AI_MODEL claude-haiku-5-5
# etapa 5 — Google Agenda
add GOOGLE_CLIENT_ID ""
add GOOGLE_CLIENT_SECRET ""
# botão "Entrar com a Meta" (Embedded Signup)
add META_APP_ID 1164112056262465
add META_ES_CONFIG_ID 1750456949367836
add META_SYSTEM_USER_ID 61596234233958
add META_SYSTEM_TOKEN ""
