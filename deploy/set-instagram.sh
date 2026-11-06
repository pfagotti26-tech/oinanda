#!/usr/bin/env bash
# Liga o Direct do Instagram no Oi Nanda (app "Oi Nanda" na Meta, produto Instagram com login do Instagram).
# A chave secreta não aparece na tela nem no histórico.
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
clean(){ printf '%s' "$1" | sed 's/\x1b\[20[01]~//g' | tr -cd 'A-Za-z0-9_-'; }
read -rp "ID do app do Instagram (Instagram app ID, só números): " AID; AID="$(clean "$AID")"
[[ "$AID" =~ ^[0-9]{6,}$ ]] || { echo "O ID do app tem só números. Nada foi alterado."; exit 1; }
read -rsp "Chave secreta do app do Instagram (Instagram app secret; não aparece enquanto cola): " SEC; echo
SEC="$(clean "$SEC")"
[ ${#SEC} -ge 20 ] || { echo "Essa chave parece curta demais. Nada foi alterado."; exit 1; }
VT="$(grep '^IG_VERIFY_TOKEN=' .env | cut -d= -f2- || true)"
[ -n "$VT" ] || VT="oinanda-ig-$(openssl rand -hex 12)"
sed -i '/^IG_APP_ID=/d;/^IG_APP_SECRET=/d;/^IG_VERIFY_TOKEN=/d' .env
{ echo "IG_APP_ID=$AID"; echo "IG_APP_SECRET=$SEC"; echo "IG_VERIFY_TOKEN=$VT"; } >> .env
docker compose up -d --force-recreate api >/dev/null
HOST="$(grep '^APP_HOSTS=' .env | cut -d= -f2 | cut -d, -f1 | sed 's#https\?://##')"
echo "PRONTO: Instagram configurado no servidor."
echo
echo "Agora, no app da Meta (Instagram > Configuração da API com login do Instagram):"
echo "  1) Configurar webhooks:"
echo "     URL de callback:      https://$HOST/api/ig/webhook"
echo "     Token de verificação: $VT"
echo "     Assine os campos:     messages, messaging_postbacks, messaging_seen e comments"
echo "  2) Configurar login comercial do Instagram > URL de redirecionamento OAuth:"
echo "     https://$HOST/api/ig/callback"
