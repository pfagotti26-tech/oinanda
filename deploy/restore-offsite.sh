#!/usr/bin/env bash
# Traz de volta uma cópia do Storage Box (servidor novo, ou teste de restauração).
#   bash restore-offsite.sh                 -> lista as cópias lá fora
#   bash restore-offsite.sh 2026-11-24      -> baixa e abre a cópia desse dia em deploy/recuperado/
# Pede a senha da cópia (a que você guardou no gerenciador de senhas). Não mexe no sistema no ar:
# depois, para usar, rode: bash restore.sh recuperado/backups/oinanda-....archive.gz
set -euo pipefail
cd "$(dirname "$0")"
T=$(grep -m1 '^OFFSITE_TARGET=' .env 2>/dev/null | cut -d= -f2- || true)
[ -n "$T" ] || read -rp "Usuário@endereço do Storage Box (ex.: u123456@u123456.your-storagebox.de): " T
KEYF=/root/.ssh/oinanda_offsite; SFTP=(sftp -q -P 23 -o StrictHostKeyChecking=accept-new); [ -f $KEYF ] && SFTP+=(-i $KEYF)
if [ -z "${1:-}" ]; then echo "ls -1 oinanda" | "${SFTP[@]}" -b - "$T" | grep -o 'oinanda-[0-9-]*\.tar\.enc'; exit 0; fi
F="oinanda-$1.tar.enc"; mkdir -p recuperado
"${SFTP[@]}" "$T:oinanda/$F" "recuperado/$F" >/dev/null
read -rsp "Senha da cópia: " K; echo
OFFSITE_KEY="$K" openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:OFFSITE_KEY -in "recuperado/$F" | tar xf - -C recuperado
rm -f "recuperado/$F"
echo "Aberto em deploy/recuperado/:"; find recuperado -type f | sed 's/^/  /'
echo "Para restaurar o banco: cp recuperado/backups/oinanda-*.archive.gz backups/ && bash restore.sh backups/<arquivo>"
echo "O .env recuperado fica em recuperado/.env (num servidor novo, copie para deploy/.env antes de subir)."
