#!/usr/bin/env bash
# Mostra os últimos registros do sistema. Uso: bash logs.sh [api|web|mongo] (padrão: api)
cd "$(dirname "$0")"; docker compose logs "${1:-api}" --tail "${2:-100}"
