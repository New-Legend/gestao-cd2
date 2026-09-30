# Gestão CD 2

Repositório unificado: **backend Django Assistente Krill** + **UI moderna Gestão CD** (paleta Rede Krill `#061d37` / `#073b73` / `#ffd21f`).

## Estrutura

```
gestao-cd-2/
├── assistente_krill_web/   # settings, urls, wsgi/asgi
├── painel/                 # models, views, forms, 70+ migrations, admin
├── templates/painel/       # HTML unificado (base.html Fusão + telas operacionais)
├── static/                 # assets PWA / imagens
├── manage.py
├── requirements.txt
├── render.yaml             # deploy Render
└── docs/                   # log Fusão + documentação
```

- Layout principal: `templates/painel/base.html` (top bar com menu de usuário compacto)
- Home / Central: `/` (`dashboard`) e `/home/` (alias)
- Banco: mesma PostgreSQL via `DATABASE_URL` (ex.: `gestaocd-db` no Render)

## Top bar (correção)

O botão de usuário na top bar deixou de ocupar 100% da largura no mobile. Agora é um chip compacto (inicial + `username` + ▾), alinhado ao sino e ao seletor de CD, na cor corporativa.

## Subir como repositório GitHub `gestao-cd-2`

Este diretório já é a raiz Django limpa. Para publicar em um repo novo:

```bash
# 1) No GitHub: criar repositório vazio New-Legend/gestao-cd-2
# 2) Localmente, a partir desta pasta:
cd gestao-cd-2
git init
git add .
git commit -m "feat: Gestão CD 2 — Django Krill + UI unificada"
git branch -M main
git remote add origin https://github.com/New-Legend/gestao-cd-2.git
git push -u origin main
```

## Render

1. New Web Service → conectar `gestao-cd-2`
2. Usar `render.yaml` (rootDir `.`)
3. Ligar `DATABASE_URL` ao PostgreSQL existente (`gestaocd-db` / o mesmo do piloto)
4. Definir `MODELO_TESTE_MASTER_PASSWORD` e demais secrets

## Local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py ensure_master
python manage.py runserver 0.0.0.0:8000
# http://127.0.0.1:8000/login/
```

Ou os scripts `.bat` / PowerShell de rede já incluídos.

## O que foi preservado

- Models, views, forms e migrações PostgreSQL
- Auth, permissões, CSRF, PWA / Web Push
- Rotinas de romaneio / logística / XML
- Scripts de infraestrutura e keepalive Render
