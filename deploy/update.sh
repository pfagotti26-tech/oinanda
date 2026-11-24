#!/usr/bin/env bash
# Atualiza site e sistema com a versão mais recente do GitHub (main). Três travas:
#  1. roda os testes automáticos antes: se algum falhar, nada muda no ar;
#  2. faz backup do banco antes de trocar a versão;
#  3. se o sistema não voltar a responder, volta sozinho para a versão anterior.
# Voltar na mão a qualquer momento: bash rollback.sh
set -euo pipefail
# tudo dentro de main(): o bash lê o script inteiro antes de começar, então o git pull pode trocar este arquivo sem bagunçar a execução
main(){
cd "$(dirname "$0")"
mkdir -p backups
PREV=$(git -C .. rev-parse HEAD)
git -C .. pull --ff-only
bash ensure-env.sh
chmod +x *.sh
echo "30 3 * * * root $(pwd)/backup.sh >> /var/log/oinanda-backup.log 2>&1" > /etc/cron.d/oinanda-backup
echo "*/5 * * * * root $(pwd)/monitor.sh >> /var/log/oinanda-monitor.log 2>&1" > /etc/cron.d/oinanda-monitor

echo "1/3 Testes automáticos..."
if ! { docker build -q --target test -t oinanda-test -f api.Dockerfile .. >/tmp/oinanda-test.log 2>&1 && docker run --rm oinanda-test >>/tmp/oinanda-test.log 2>&1; }; then
  tail -30 /tmp/oinanda-test.log
  git -C .. reset -q --hard "$PREV"
  echo "PAROU: os testes falharam. Nada mudou no ar: o sistema segue com a versão que já estava rodando."
  exit 1
fi
echo "    $(grep -Eo '[0-9]+ passed' /tmp/oinanda-test.log | tail -1)"

echo "2/3 Backup antes de atualizar..."
[ "$PREV" != "$(git -C .. rev-parse HEAD)" ] && echo "$PREV" > backups/.versao-anterior
PRE=1 bash backup.sh | sed 's/^/    /'

echo "3/3 Subindo a versão nova..."
docker compose up -d --build --remove-orphans
# Caddyfile: o container enxerga o arquivo antigo depois do git pull (montagem de arquivo único), então só recriar aplica.
# Antes de aplicar, confere o arquivo novo num container à parte: com erro, o site segue com a configuração anterior.
if docker compose exec -T web cat /etc/caddy/Caddyfile 2>/dev/null | cmp -s - Caddyfile; then
  :
elif docker run --rm --env-file .env -v "$(pwd)/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
  echo "Aplicando a configuração nova do site..."
  docker compose up -d --force-recreate --no-deps web
else
  echo "AVISO: o Caddyfile novo tem erro. O site continua com a configuração anterior."
  echo "Para ver o erro: docker run --rm --env-file .env -v \"\$(pwd)/Caddyfile:/etc/caddy/Caddyfile:ro\" caddy:2 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile"
fi
docker image prune -f >/dev/null
echo "Aguardando o sistema responder..."
for i in $(seq 1 40); do docker compose exec -T api python -c "import urllib.request;urllib.request.urlopen('http://localhost:8001/api/health')" 2>/dev/null && { OK=1; break; }; sleep 3; done
if [ "${OK:-}" = 1 ]; then
  echo "PRONTO: https://app.oinanda.com.br  ($(git -C .. log -1 --format='%h %s'))"
elif [ -f backups/.versao-anterior ] && [ "$(cat backups/.versao-anterior)" != "$(git -C .. rev-parse HEAD)" ]; then
  echo "A versão nova não respondeu em 2 minutos. Voltando sozinho para a anterior..."
  docker compose logs api --tail 30 > /tmp/oinanda-falha.log 2>&1 || true
  bash rollback.sh
  echo "O erro da versão nova ficou em /tmp/oinanda-falha.log"
  exit 1
else
  echo "O sistema ainda não respondeu. Veja: docker compose logs api --tail 50"
fi
}
main "$@"
