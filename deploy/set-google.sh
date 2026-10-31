#!/usr/bin/env bash
# Guarda as credenciais do Google (OAuth) no servidor sem mostrar o segredo na tela.
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
read -rp  "Cole o ID do cliente (termina em .apps.googleusercontent.com) e aperte Enter: " CID; echo
read -rsp "Cole a chave secreta do cliente (não aparece enquanto cola) e aperte Enter: " SEC; echo
CID="$(echo -n "$CID" | tr -d '[:space:]')"; SEC="$(echo -n "$SEC" | tr -d '[:space:]')"
[[ "$CID" == *.apps.googleusercontent.com ]] || { echo "O ID não parece certo (precisa terminar em .apps.googleusercontent.com). Nada foi alterado."; exit 1; }
[ ${#SEC} -ge 20 ] || { echo "A chave secreta parece curta demais (tamanho ${#SEC}). Nada foi alterado."; exit 1; }
sed -i '/^GOOGLE_CLIENT_ID=/d;/^GOOGLE_CLIENT_SECRET=/d' .env
echo "GOOGLE_CLIENT_ID=$CID" >> .env; echo "GOOGLE_CLIENT_SECRET=$SEC" >> .env
docker compose up -d --force-recreate api >/dev/null
echo "PRONTO: Google guardado (segredo com tamanho ${#SEC}). Agora conecte em Agenda > Configurar."
