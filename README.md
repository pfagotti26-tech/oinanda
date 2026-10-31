# Oi Nanda — site

Site estático (HTML/CSS/JS puro). Não precisa de build, backend nem banco de dados.

## Estrutura
- `site/` — o site publicado
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

## Pendências
- Número de WhatsApp do site: variável `WA_NUMERO` no `site/index.html`.
