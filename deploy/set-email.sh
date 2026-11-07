#!/usr/bin/env bash
# Liga os e-mails do Oi Nanda (nova senha, boas-vindas, convite, fim do teste) com a chave do Resend.
# A chave não aparece na tela nem no histórico. Enter sem colar nada mantém a chave atual.
set -euo pipefail
cd "$(dirname "$0")"
bash ensure-env.sh
OLD="$(grep '^RESEND_API_KEY=' .env | cut -d= -f2- || true)"
read -rsp "Cole a chave do Resend (começa com re_; não aparece enquanto cola)${OLD:+ [Enter mantém a atual]}: " KEY; echo
KEY="$(printf '%s' "$KEY" | sed 's/\x1b\[20[01]~//g' | tr -d '[:space:]')"
[ -n "$KEY" ] || KEY="$OLD"
[[ "$KEY" == re_* ]] || { echo "Isso não parece uma chave do Resend (começa com re_). Nada foi alterado."; exit 1; }
CODE=$(curl -s -o /tmp/rs.json -w '%{http_code}' https://api.resend.com/domains -H "Authorization: Bearer $KEY")
if [ "$CODE" != "200" ]; then
  echo "O Resend recusou a chave (código $CODE). Nada foi alterado."; rm -f /tmp/rs.json; exit 1
fi
if grep -q '"name":"oinanda.com.br"' /tmp/rs.json; then
  grep -q '"status":"verified"' /tmp/rs.json && echo "Domínio oinanda.com.br verificado no Resend." || echo "ATENÇÃO: o domínio oinanda.com.br ainda não está verificado no Resend (falta o DNS). Os e-mails só saem depois disso."
else
  echo "ATENÇÃO: o domínio oinanda.com.br ainda não foi adicionado no Resend (Domains > Add domain)."
fi
rm -f /tmp/rs.json
sed -i '/^RESEND_API_KEY=/d' .env && echo "RESEND_API_KEY=$KEY" >> .env
grep -q '^MAIL_FROM=' .env || echo "MAIL_FROM=Oi Nanda <nanda@oinanda.com.br>" >> .env
docker compose up -d --force-recreate api >/dev/null
echo "PRONTO: e-mails ligados (chave com ${#KEY} caracteres)."
