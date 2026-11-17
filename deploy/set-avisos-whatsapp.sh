#!/usr/bin/env bash
# Liga os avisos do Oi Nanda por WhatsApp (cobrança, fim do teste, uso da Nanda) saindo do número do próprio Oi Nanda.
# Precisa de três dados da conta de WhatsApp do Oi Nanda na Meta: o ID do número, o ID da conta (WABA) e o token de acesso.
# O token não aparece na tela nem no histórico. Enter sem colar nada mantém o valor atual.
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
ask() { # nome da variável, pergunta, segredo?
  local old; old="$(grep "^$1=" .env | cut -d= -f2- || true)"; local v
  if [ "${3:-}" = "s" ]; then read -rsp "$2${old:+ [Enter mantém o atual]}: " v; echo; else read -rp "$2${old:+ [Enter mantém $old]}: " v; fi
  v="$(printf '%s' "$v" | sed 's/\x1b\[20[01]~//g' | tr -d '[:space:]')"; [ -n "$v" ] || v="$old"
  [ -n "$v" ] || { echo "Faltou informar: $2. Nada foi alterado."; exit 1; }
  printf '%s' "$v"
}
PHONE="$(ask PLATFORM_WA_PHONE_ID 'ID do número do Oi Nanda na Meta (só dígitos)')"
WABA="$(ask PLATFORM_WA_WABA_ID 'ID da conta de WhatsApp (WABA) do Oi Nanda (só dígitos)')"
TOKEN="$(ask PLATFORM_WA_TOKEN 'Cole o token de acesso (não aparece enquanto cola)' s)"
CODE=$(curl -s -o /tmp/wa.json -w '%{http_code}' "https://graph.facebook.com/v21.0/$PHONE?fields=display_phone_number,verified_name" -H "Authorization: Bearer $TOKEN")
if [ "$CODE" != "200" ]; then echo "A Meta recusou esses dados (código $CODE). Nada foi alterado."; rm -f /tmp/wa.json; exit 1; fi
echo "Número encontrado: $(grep -o '"display_phone_number":"[^"]*"' /tmp/wa.json | cut -d'"' -f4) ($(grep -o '"verified_name":"[^"]*"' /tmp/wa.json | cut -d'"' -f4))"
rm -f /tmp/wa.json
for k in PLATFORM_WA_PHONE_ID PLATFORM_WA_WABA_ID PLATFORM_WA_TOKEN; do sed -i "/^$k=/d" .env; done
{ echo "PLATFORM_WA_PHONE_ID=$PHONE"; echo "PLATFORM_WA_WABA_ID=$WABA"; echo "PLATFORM_WA_TOKEN=$TOKEN"; } >> .env
docker compose up -d --force-recreate api >/dev/null
echo "PRONTO: avisos por WhatsApp ligados. Agora entre no admin, em Avisos › Textos automáticos, e envie os modelos para aprovação da Meta."
