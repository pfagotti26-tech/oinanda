#!/usr/bin/env bash
# Monitoramento (a cada 5 min): se o sistema não responder 2 vezes seguidas, ou o disco passar de 85%,
# manda aviso no celular pelo app ntfy (tópico em NTFY_TOPIC no .env). Sem tópico, só grava em /var/log/oinanda-monitor.log.
cd "$(dirname "$0")"
TOPIC="$(grep '^NTFY_TOPIC=' .env | cut -d= -f2- | tr -d '"')"
STATE=/var/tmp/oinanda-monitor.fail
avisa(){ echo "$(date -Is) $1"; [ -n "$TOPIC" ] && curl -fsS -m 10 -H "Title: Oi Nanda" -H "Priority: high" -d "$1" "https://ntfy.sh/$TOPIC" >/dev/null || true; }
if curl -fsS -m 10 https://app.oinanda.com.br/api/health >/dev/null; then
  [ -f "$STATE" ] && { rm -f "$STATE"; avisa "Sistema voltou a responder."; }
else
  n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 )); echo $n > "$STATE"
  [ "$n" = 2 ] && avisa "Sistema fora do ar: app.oinanda.com.br não responde há 10 min."
fi
uso=$(df --output=pcent / | tail -1 | tr -dc 0-9)
[ "$uso" -gt 85 ] && avisa "Disco do servidor em ${uso}%."
exit 0
