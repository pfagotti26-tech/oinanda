#!/usr/bin/env bash
# Liga a transcrição de áudio (a Nanda passa a entender áudios). A chave não aparece na tela nem no histórico.
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
echo "Qual serviço de transcrição?"
echo "  1) OpenAI (gpt-4o-mini-transcribe)"
echo "  2) Groq (whisper-large-v3-turbo, mais barato)"
read -rp "Digite 1 ou 2: " OPT
case "$OPT" in
  1) URL="https://api.openai.com/v1/audio/transcriptions"; MODELS="https://api.openai.com/v1/models"; MODEL="gpt-4o-mini-transcribe" ;;
  2) URL="https://api.groq.com/openai/v1/audio/transcriptions"; MODELS="https://api.groq.com/openai/v1/models"; MODEL="whisper-large-v3-turbo" ;;
  *) echo "Opção inválida. Nada foi alterado."; exit 1 ;;
esac
read -rsp "Cole a chave (não aparece enquanto cola) e aperte Enter: " KEY; echo
KEY="$(echo -n "$KEY" | tr -d '[:space:]')"
[ -n "$KEY" ] || { echo "Chave vazia. Nada foi alterado."; exit 1; }
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$MODELS" -H "Authorization: Bearer $KEY")
if [ "$CODE" != "200" ]; then echo "O serviço recusou a chave (código $CODE). Nada foi alterado."; exit 1; fi
sed -i '/^TRANSCRIBE_API_KEY=/d;/^TRANSCRIBE_URL=/d;/^TRANSCRIBE_MODEL=/d' .env
{ echo "TRANSCRIBE_API_KEY=$KEY"; echo "TRANSCRIBE_URL=$URL"; echo "TRANSCRIBE_MODEL=$MODEL"; } >> .env
docker compose up -d --force-recreate api >/dev/null
echo "PRONTO: transcrição ligada ($MODEL). Os próximos áudios dos clientes já chegam com texto."
