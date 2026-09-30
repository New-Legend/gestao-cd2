# Plano Diretor — Gestão CD 2

## Diagnóstico da Fusão

### Do Assistente Krill
- `assistente_krill_web/` (settings, wsgi, urls)
- `painel/` (models, views, forms, 70+ migrations PostgreSQL)
- Autenticação, permissões, XML/NF, romaneio/logística

### Do Gestão CD (visual)
- Templates Fusão em `templates/painel/` (shell, cards, abas, top bar Krill)
- Assets em `static/` + Tailwind CDN / CSS Fusão no `base.html`
- Paleta: `#061d37`, `#073b73`, `#ffd21f`, `#d71920`

## Correção crítica — Top bar

Botão de usuário compacto (chip com inicial + username), sem `width: 100%` no mobile. Mantém dropdown (PWA, push, perfil, sair) e CSRF no logout.

## Deploy

1. Publicar este diretório como repo `gestao-cd-2`
2. Render → `render.yaml` + `DATABASE_URL` do PostgreSQL atual
3. Validar login master e fluxos de frota/expedição

Ver `README.md` na raiz deste pacote.
