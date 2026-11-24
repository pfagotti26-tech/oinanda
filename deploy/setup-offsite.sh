#!/usr/bin/env bash
# Liga a cópia diária do backup fora do servidor, num Hetzner Storage Box. Rodar uma vez.
# Antes: no painel da Hetzner, crie o Storage Box e ative "SSH support" e "External reachability".
set -euo pipefail
cd "$(dirname "$0")"
read -rp "Usuário do Storage Box (aparece no painel, ex.: u123456): " U
U=$(echo "$U" | tr -d ' '); [[ "$U" =~ ^u[0-9]+(-sub[0-9]+)?$ ]] || { echo "Usuário inválido. É algo como u123456."; exit 1; }
H="$U.your-storagebox.de"
[ -f /root/.ssh/oinanda_offsite ] || ssh-keygen -q -t ed25519 -N "" -f /root/.ssh/oinanda_offsite -C "oinanda-backup"
echo; echo "Agora digite a senha do Storage Box quando pedir (só desta vez; depois o servidor entra pela chave)."
ssh-copy-id -p 23 -s -i /root/.ssh/oinanda_offsite.pub -o StrictHostKeyChecking=accept-new "$U@$H"
ssh -p 23 -i /root/.ssh/oinanda_offsite -o BatchMode=yes "$U@$H" mkdir -p oinanda 2>/dev/null || true
sed -i '/^OFFSITE_TARGET=/d' .env; echo "OFFSITE_TARGET=$U@$H" >> .env
NEWKEY=0; grep -q '^OFFSITE_KEY=.' .env || { sed -i '/^OFFSITE_KEY=/d' .env; echo "OFFSITE_KEY=$(openssl rand -base64 33 | tr -d '\n/+=' | cut -c1-40)" >> .env; NEWKEY=1; }
echo; echo "Testando: mandando o backup de agora..."
bash backup.sh
K=$(grep -m1 '^OFFSITE_KEY=' .env | cut -d= -f2-)
echo
echo "================================================================"
echo " PRONTO. Todo dia às 03:30 vai uma cópia criptografada para o Storage Box."
[ "$NEWKEY" = 1 ] && echo " GUARDE AGORA a senha da cópia no seu gerenciador de senhas:" || echo " Senha da cópia (a mesma de antes):"
echo
echo "     $K"
echo
echo " Sem ela, a cópia lá fora não abre. Se o servidor sumir, é ela"
echo " que traz o Oi Nanda de volta. Não mande por WhatsApp nem e-mail."
echo "================================================================"
