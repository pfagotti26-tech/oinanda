#!/usr/bin/env bash
# Guarda o token permanente do usuário do sistema "oinanda-sistema" sem mostrar na tela.
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
read -rsp "Cole o token do usuário do sistema oinanda-sistema (não aparece enquanto cola) e aperte Enter: " TOK; echo
TOK="$(echo -n "$TOK" | tr -d '[:space:]')"
[[ "$TOK" == EAA* ]] || { echo "Isso não parece um token da Meta (começa com EAA). Nada foi alterado."; exit 1; }
CODE=$(curl -s -o /dev/null -w '%{http_code}' "https://graph.facebook.com/v21.0/me?access_token=$TOK")
[ "$CODE" = "200" ] || { echo "A Meta recusou esse token (código $CODE). Nada foi alterado."; exit 1; }
sed -i '/^META_SYSTEM_TOKEN=/d' .env && echo "META_SYSTEM_TOKEN=$TOK" >> .env
docker compose up -d --force-recreate api >/dev/null
echo "PRONTO: token permanente guardado (tamanho ${#TOK})."
