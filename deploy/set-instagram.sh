#!/usr/bin/env bash
# Liga o Direct do Instagram no Oi Nanda (app "Oi Nanda" na Meta, API do Instagram com login unificado).
# Pede: ID do app, chave secreta do app (não aparece na tela nem no histórico) e ID da configuração do login.
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
clean(){ printf '%s' "$1" | sed 's/\x1b\[20[01]~//g' | tr -cd 'A-Za-z0-9_-'; }
OLDCFG0="$(grep '^IG_CONFIG_ID=' .env | cut -d= -f2- || true)"
OLDAID="$(grep '^IG_APP_ID=' .env | cut -d= -f2- || true)"; OLDSEC="$(grep '^IG_APP_SECRET=' .env | cut -d= -f2- || true)"
read -rp "ID do app (Configurações do app > Básico, só números)${OLDAID:+ [Enter mantém $OLDAID]}: " AID; AID="$(clean "$AID")"; [ -n "$AID" ] || AID="$OLDAID"
[[ "$AID" =~ ^[0-9]{6,}$ ]] || { echo "O ID do app tem só números. Nada foi alterado."; exit 1; }
read -rsp "Chave secreta do app (Configurações do app > Básico > Mostrar; não aparece enquanto cola)${OLDSEC:+ [Enter mantém a atual]}: " SEC; echo
SEC="$(clean "$SEC")"; [ -n "$SEC" ] || SEC="$OLDSEC"
[ ${#SEC} -ge 20 ] || { echo "Essa chave parece curta demais. Nada foi alterado."; exit 1; }
read -rp "ID da configuração do login (config_id, só números)${OLDCFG0:+ [Enter mantém $OLDCFG0]}: " CFG; CFG="$(clean "$CFG")"
[ -z "$CFG" ] || [[ "$CFG" =~ ^[0-9]{6,}$ ]] || { echo "O ID da configuração tem só números. Nada foi alterado."; exit 1; }
VT="$(grep '^IG_VERIFY_TOKEN=' .env | cut -d= -f2- || true)"
[ -n "$VT" ] || VT="oinanda-ig-$(openssl rand -hex 12)"
OLDCFG="$(grep '^IG_CONFIG_ID=' .env | cut -d= -f2- || true)"
[ -n "$CFG" ] || CFG="$OLDCFG"
sed -i '/^IG_APP_ID=/d;/^IG_APP_SECRET=/d;/^IG_VERIFY_TOKEN=/d;/^IG_CONFIG_ID=/d' .env
{ echo "IG_APP_ID=$AID"; echo "IG_APP_SECRET=$SEC"; echo "IG_VERIFY_TOKEN=$VT"; [ -z "$CFG" ] || echo "IG_CONFIG_ID=$CFG"; } >> .env
docker compose up -d --force-recreate api >/dev/null
HOST="$(grep '^APP_HOSTS=' .env | cut -d= -f2 | cut -d, -f1 | sed 's#https\?://##')"
echo "PRONTO: Instagram configurado no servidor."
[ -n "$CFG" ] || echo "Falta o ID da configuração: rode este script de novo quando tiver criado a configuração."
echo
echo "No app da Meta (Casos de uso > Instagram):"
echo "  URL de redirecionamento: https://$HOST/api/ig/callback"
echo "  Webhooks:"
echo "     URL de callback:      https://$HOST/api/ig/webhook"
echo "     Token de verificação: $VT"
echo "     Campos:               messages, messaging_postbacks, messaging_seen e comments"
