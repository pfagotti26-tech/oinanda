#!/usr/bin/env bash
# Instala o site do Oi Nanda neste servidor (Ubuntu 24.04). Pode rodar de novo sem problema.
# Uso (como root):  curl -fsSL https://raw.githubusercontent.com/pfagotti26-tech/oinanda/main/deploy/install.sh | bash
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Rode como root."; exit 1; }

echo "==> 1/4 Instalando Docker, git e firewall"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 ufw git curl >/dev/null
systemctl enable --now docker >/dev/null

echo "==> 2/4 Firewall: só SSH (22) e site (80/443) abertos"
ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null

echo "==> 3/4 Baixando o código"
if [ -d /opt/oinanda/.git ]; then git -C /opt/oinanda pull -q --ff-only
else git clone -q https://github.com/pfagotti26-tech/oinanda.git /opt/oinanda; fi
cd /opt/oinanda/deploy
IP="$(curl -4fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
TEST_HOST="$(echo "$IP" | tr . -).sslip.io"
if [ ! -f .env ]; then
  umask 077
  cat > .env <<ENV
# --- Oi Nanda: configuração do servidor (NÃO enviar para o GitHub) ---
# endereços atendidos pelo site (HTTPS automático). Na virada do DNS: bash /opt/oinanda/deploy/go-live.sh
SITE_HOSTS=${TEST_HOST}
WWW_HOST=http://www.invalid
ACME_EMAIL=pfagotti26@gmail.com
ENV
fi
bash ensure-env.sh; chmod +x *.sh

echo "==> 4/4 Subindo o site"
bash update.sh
echo
echo "PRONTO. Endereço de teste: https://${TEST_HOST}"
echo "(o certificado HTTPS pode levar 1 minuto na primeira vez)"
