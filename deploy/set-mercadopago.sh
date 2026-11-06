#!/usr/bin/env bash
# Liga a cobrança das assinaturas pelo Mercado Pago (conta da Fagotti Marketing Direto).
# O token não aparece na tela nem no histórico.
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
clean(){ printf '%s' "$1" | sed 's/\x1b\[20[01]~//g' | tr -cd 'A-Za-z0-9_-'; }
read -rsp "Cole o Access Token de PRODUÇÃO do Mercado Pago (começa com APP_USR-) e aperte Enter: " TOK; echo
TOK="$(clean "$TOK")"
[[ "$TOK" == APP_USR-* ]] || { echo "Isso não parece o Access Token de produção (começa com APP_USR-). Nada foi alterado."; exit 1; }
CODE=$(curl -s -o /tmp/mp-check.json -w '%{http_code}' https://api.mercadopago.com/users/me -H "Authorization: Bearer $TOK")
if [ "$CODE" != "200" ]; then echo "O Mercado Pago recusou o token (código $CODE). Nada foi alterado."; rm -f /tmp/mp-check.json; exit 1; fi
WHO=$(python3 -c "import json;d=json.load(open('/tmp/mp-check.json'));print((d.get('company') or {}).get('brand_name') or d.get('nickname') or '', '·', d.get('email') or '')" 2>/dev/null || true)
rm -f /tmp/mp-check.json
echo "Conta do Mercado Pago: $WHO"
read -rsp "Cole a ASSINATURA SECRETA das notificações (Webhooks) ou só aperte Enter para pular: " SEC; echo
SEC="$(clean "$SEC")"
sed -i '/^MP_ACCESS_TOKEN=/d;/^MP_WEBHOOK_SECRET=/d' .env
echo "MP_ACCESS_TOKEN=$TOK" >> .env
[ -n "$SEC" ] && echo "MP_WEBHOOK_SECRET=$SEC" >> .env
docker compose up -d --force-recreate api >/dev/null
echo "PRONTO: cobrança ligada${SEC:+ com verificação das notificações}."
echo "No painel do Mercado Pago (Suas integrações > Webhooks), use a URL: https://$(grep '^APP_HOSTS=' .env | cut -d= -f2 | cut -d, -f1 | sed 's#https\?://##')/api/billing/webhook"
echo "e marque o evento: Planos e assinaturas."
