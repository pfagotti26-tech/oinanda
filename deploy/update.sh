#!/usr/bin/env bash
# Atualiza o site com a versão mais recente do GitHub (main).
set -euo pipefail
cd "$(dirname "$0")"
git -C .. pull --ff-only
docker compose up -d
docker compose exec -T web caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || docker compose restart web
echo "Atualizado: $(git -C .. log -1 --format='%h %s')"
