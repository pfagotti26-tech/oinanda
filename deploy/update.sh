#!/usr/bin/env bash
# Atualiza site e sistema com a versão mais recente do GitHub (main).
set -euo pipefail
cd "$(dirname "$0")"
git -C .. pull --ff-only
bash ensure-env.sh
chmod +x *.sh
echo "30 3 * * * root $(pwd)/backup.sh >> /var/log/oinanda-backup.log 2>&1" > /etc/cron.d/oinanda-backup
echo "*/5 * * * * root $(pwd)/monitor.sh >> /var/log/oinanda-monitor.log 2>&1" > /etc/cron.d/oinanda-monitor
mkdir -p backups
docker compose up -d --build --remove-orphans
docker compose exec -T web caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || docker compose restart web
docker image prune -f >/dev/null
echo "Aguardando o sistema responder..."
for i in $(seq 1 40); do docker compose exec -T api python -c "import urllib.request;urllib.request.urlopen('http://localhost:8001/api/health')" 2>/dev/null && { OK=1; break; }; sleep 3; done
[ "${OK:-}" = 1 ] && echo "PRONTO: https://app.oinanda.com.br  ($(git -C .. log -1 --format='%h %s'))" || echo "O sistema ainda não respondeu. Veja: docker compose logs api --tail 50"
