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
