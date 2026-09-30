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

## Migração visual Krill (pós-Live)

## Shell unificado (anti-Frankenstein)

- Uma só navegação: sidebar escura (desktop fixa / mobile hamburger + X)
- Topbar só com título da página + ações (CD, sino, utilizador) — sem logo/seta duplicados
- Home sem barra de abas residuais; centrais expandem para links reais `module_list`


### Padrão obrigatório nas telas
- Shell mobile-first com abas (home), cards em balões, menus expansíveis e modais leves (`hidden` por padrão)
- CSS partilhado: `static/painel/css/gestao-cd-modern.css` (bloco GC2 Krill UI)
- Modais globais: `static/painel/js/gc2-modals.js` (`data-gc2-open-modal` / `data-gc2-close-modal`)
- Hubs em balões: home, frota, colaboradores, centrais personalizadas
- Módulos operacionais (`module_list` / `module_form`) e painéis admin com wrapper `gc2-page` + correções responsive

### Estado (2026-09-30)
- Pacote `gestao-cd-2/` já está em `main` do monorepo `New-Legend/gestao-cd`
- Visual moderno transplantado: logo Rede Krill, X da sidebar, top bar, home grids, `static/`
- Login / offline / PWA usam ícones Gestão CD + logo Krill
- Blueprint monorepo na raiz: `/render.yaml` com `rootDir: gestao-cd-2`
- Blueprint standalone (repo só com esta pasta): `gestao-cd-2/render.yaml` (`rootDir: .`)

### Passos no Render
1. Conectar o serviço ao repo `New-Legend/gestao-cd` **ou** Blueprint com o `render.yaml` da raiz
2. Confirmar `rootDir = gestao-cd-2` (monorepo) ou `.` (repo dedicado)
3. Ligar `DATABASE_URL` ao PostgreSQL existente (`gestaocd-db` / piloto)
4. Definir `MODELO_TESTE_MASTER_PASSWORD` e demais secrets
5. Validar login master e fluxos de frota/expedição

Ver `README.md` na raiz deste pacote.
