#!/usr/bin/env bash
# Liga a voz da Nanda (respostas em áudio) com a chave da ElevenLabs. A chave não aparece na tela nem no histórico.
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
read -rsp "Cole a chave da API da ElevenLabs (não aparece enquanto cola) e aperte Enter: " KEY; echo
KEY="$(echo -n "$KEY" | tr -d '[:space:]')"
[ ${#KEY} -ge 20 ] || { echo "A chave parece curta demais (tamanho ${#KEY}). Nada foi alterado."; exit 1; }
CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "xi-api-key: $KEY" https://api.elevenlabs.io/v1/user)
[ "$CODE" = "200" ] || { echo "A ElevenLabs não aceitou a chave (resposta $CODE). Nada foi alterado."; exit 1; }
sed -i '/^ELEVENLABS_API_KEY=/d' .env
echo "ELEVENLABS_API_KEY=$KEY" >> .env
docker compose up -d --force-recreate api >/dev/null
echo "PRONTO: voz da Nanda ligada (chave com tamanho ${#KEY}). Cada empresa ativa em Nanda (IA) > Voz."
