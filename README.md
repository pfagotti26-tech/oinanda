# Oi Nanda — site

Site estático (HTML/CSS/JS puro). Não precisa de build, backend nem banco de dados.

## Estrutura
- `site/` — o site publicado (oinanda.com.br)
- `app/` — o painel do sistema (app.oinanda.com.br), HTML/JS sem build
- `backend/` — a API (FastAPI + MongoDB); testes em `backend/tests`
- `deploy/` — instalação no servidor (Hetzner): Docker + Caddy com HTTPS automático

Dentro de `site/`:
- `index.html` — página principal e páginas de segmento (navegação por `#studios`, `#clinicas`, `#lancamentos`)
- `privacidade/index.html` — Política de Privacidade (abre em `/privacidade/`; exclusão de dados em `/privacidade/#exclusao`)
- `termos/index.html` — Termos de Uso (abre em `/termos/`)
- `shots/` — imagens do produto e foto do topo
- `favicon.png`, `og.png` — ícone da aba e imagem de compartilhamento

## Como hospedar (Hetzner)
No servidor, como root:

    curl -fsSL https://raw.githubusercontent.com/pfagotti26-tech/oinanda/main/deploy/install.sh | bash

Depois que o DNS de oinanda.com.br apontar para o servidor: `bash /opt/oinanda/deploy/go-live.sh`.
Para publicar mudanças do GitHub: `bash /opt/oinanda/deploy/update.sh`.

No servidor também: `backup.sh` (roda todo dia 03:30, guarda 14), `restore.sh`, `monitor.sh` (a cada 5 min, avisa pelo app ntfy se `NTFY_TOPIC` estiver no .env) e `logs.sh [api|web|mongo]`.

## Pendências
- Número de WhatsApp do site: variável `WA_NUMERO` no `site/index.html`.
