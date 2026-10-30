# Oi Nanda — site

Site estático (HTML/CSS/JS puro). Não precisa de build, backend nem banco de dados.

## Estrutura
- `index.html` — página principal e páginas de segmento (navegação por `#studios`, `#clinicas`, `#lancamentos`)
- `privacidade/index.html` — Política de Privacidade (abre em `/privacidade/`; exclusão de dados em `/privacidade/#exclusao`)
- `termos/index.html` — Termos de Uso (abre em `/termos/`)
- `shots/` — imagens do produto e foto do topo
- `favicon.png`, `og.png` — ícone da aba e imagem de compartilhamento

## Como hospedar
Servir a pasta inteira como está, na raiz do domínio (`oinanda.com.br`).
Não converter para React nem alterar arquivos.

## Pendências
- Número de WhatsApp do site: variável `WA_NUMERO` no `index.html`.
