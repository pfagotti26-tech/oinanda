#!/usr/bin/env bash
# Guarda a chave da IA no servidor sem ela aparecer na tela nem no histórico.
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
read -rsp "Cole a chave da Anthropic (não aparece enquanto cola) e aperte Enter: " KEY; echo
KEY="$(echo -n "$KEY" | tr -d '[:space:]')"
[[ "$KEY" == sk-ant-* ]] || { echo "Isso não parece uma chave da Anthropic (começa com sk-ant-). Nada foi alterado."; exit 1; }
MODEL="$(grep '^AI_MODEL=' .env | cut -d= -f2-)"
CODE=$(curl -s -o /tmp/ai-check.json -w '%{http_code}' https://api.anthropic.com/v1/messages \
  -H "x-api-key: $KEY" -H 'anthropic-version: 2023-06-01' -H 'content-type: application/json' \
  -d "{\"model\":\"${MODEL:-claude-haiku-5-5}\",\"max_tokens\":5,\"messages\":[{\"role\":\"user\",\"content\":\"oi\"}]}")
if [ "$CODE" != "200" ]; then
  echo "A Anthropic recusou a chave (código $CODE):"; grep -o '"message":"[^"]*"' /tmp/ai-check.json || true
  rm -f /tmp/ai-check.json; echo "Nada foi alterado."; exit 1
fi
rm -f /tmp/ai-check.json
sed -i '/^ANTHROPIC_API_KEY=/d' .env && echo "ANTHROPIC_API_KEY=$KEY" >> .env
docker compose up -d --force-recreate api >/dev/null
echo "PRONTO: chave válida e guardada (tamanho ${#KEY}). A Nanda já pode responder."
