# Operação Fusão CD — Log conclusivo da migração visual

**Branch:** `feature/fusao-cd`  
**PR:** [#49](https://github.com/New-Legend/gestao-cd/pull/49)  
**Status:** Migração visual **concluída** — pronta para revisão/aprovação.

## Objetivo

Vestir o Django Assistente Krill com o shell/visual do Gestão CD (paleta Rede Krill), **sem** alterar `views.py`, `models.py`, CSRF, PWA/service worker (exceto branding), scripts de infraestrutura nem seletores JS operacionais.

## Paleta oficial Rede Krill

| Token | Hex | Uso |
|-------|-----|-----|
| Primária escura | `#061d37` | Topbar, sidebar, headers, boards |
| Primária média | `#073b73` | Botões primários, links, destaques |
| Destaque amarelo | `#ffd21f` | Accent, progresso, badges secundários |
| Destaque vermelho | `#d71920` | Erros, zerar saldo, alertas críticos |

## Inventário final — telas adaptadas

| Área | Artefatos | Observação |
|------|-----------|------------|
| Shell | `base.html`, `_pwa_head.html`, `_pwa_register.html`, `manifest.webmanifest`, `service_worker.js` | Shell ERP + branding Gestão CD |
| Home / hubs | `dashboard.html`, `frota_hub.html`, `colaboradores_hub.html`, `central_personalizada.html` | Cards Fusão |
| Operação | `module_list.html` (checklist, expedição, férias, lojas prontas, pallets), `module_form.html`, `includes/pallet_balance_blocks.html` | Forms/`data-*` intactos |
| BI / gestão | `relatorios.html`, `painel_gestao.html`, `auditoria.html` | Gráficos CSS + tabelas |
| Admin | `usuarios.html`, `personalizar_interface.html`, `recursos_sistema.html`, `diagnostico.html`, `preferencias.html` | Permissões/reset senha intactos |
| Infra UI | `central_servidor.html`, `sentinela_servidor.html`, `importar.html`, `corrigir_cd.html` | Backups/CSRF intactos |
| Conta / PWA | `login.html`, `offline.html`, `solicitar_senha.html`, `solicitar_acesso.html`, `solicitar_acesso_logado.html`, `perfil_usuario.html`, `minhas_solicitacoes.html`, `notificacoes_push.html` | Entrada unificada Gestão CD |
| Conteúdo | `apresentacao_sistema.html`, `treinamento_sistema.html` | Branding Gestão CD |

## Revisão final rápida (checklist técnico)

| Checagem | Resultado |
|----------|-----------|
| Forms `method="post"` sem `{% csrf_token %}` | **Nenhum** |
| Actions críticas (zerar saldo, vincular carga, reset senha, backups, permissões) | **Preservadas** |
| JS operacional (`data-checklist-*`, `js-vehicle-*`, `data-ready-load-*`, `data-permission-*`, `data-access-*`) | **Intactos** |
| Submit buttons em páginas shell sem classe `.btn` | **Nenhum** |
| Páginas que estendem `base` sem classe `fusao-*` | **Nenhuma** |
| Branding “Modelo de Teste” em templates de UI | **Removido** (ícones estáticos mantêm path de arquivo) |

## Regras respeitadas do início ao fim

1. Nenhum apagamento de `views.py` / `models.py` / lógica de gargalos.
2. Todos os forms operacionais mantêm `name=` e `{% csrf_token %}`.
3. Web Push / PWA continuam ativos; só o nome exibido passou a Gestão CD.
4. Scripts de infraestrutura (`.bat`, `.ps1`, `stress_render_oficial.py`) não foram tocados.
5. Alterações foram preferencialmente CSS + classes wrapper — sem reescrever fluxos Django.

## Como revisar o PR #49

1. Login / Offline / Esqueci senha / Solicitar acesso — branding CD.
2. Check-list Frota + Painel Frota — seções, OK/Problema, CSRF.
3. Expedição — salvar saldo, zerar saldo, vínculo motorista/placa, modal Montar carga.
4. Colaboradores — mapa de calor / gargalos.
5. Relatórios + Painel Gerencial — tabelas/barras/export.
6. Gestão de Acesso — permissões, presets, reset senha.
7. Admin residual — Preferências, Auditoria, Central do Servidor, Personalizar, Recursos, Diagnóstico.

**GGWP — Operação Fusão CD encerrada no código. Aguardando aprovação humana do PR #49.**

---

## Merge definitivo (2026-09-30) — sistema único em `gestao-cd-2`

Molde: commit `647f4fb` (Informar Loja Pronta).

### O que foi unificado
- **PILOT_MODULES** = todos os módulos do `registry.py` (menu deixa de esconder Recebimento, Separação, Avarias, etc.)
- Telas operacionais dedicadas (mesmo motor `module_list` + POST):
  - `lojas_prontas.html`
  - `expedicao_planejamento.html` (+ carregamento)
  - `relatorio_paletes_cd.html`
  - `expedicao.html`
  - `expedicao_manual.html`
  - `paletes_rede.html`
- Bodies partilhados em `templates/painel/includes/*_body.html`
- Aliases Krill → Gestão CD em `module_key_alias` (sem rotas duplicadas em `urls.py`)
- Shell: Home em grids; topbar logo Krill + Gestão CD; hamburger nas telas internas

### Aliases Assistente Krill (exemplos)
| Alias Krill | Destino Django |
|---|---|
| `produtividade_cd` | `separacao` |
| `painel_estacao` | `ressuprimento_painel` |
| `avaria_triagem` | `avarias` |
| `expedicao_controle` | `expedicao` |
| `historico_saldos_paletes` | `relatorio_paletes_cd` |
| `veiculos_disponibilidade` | `veiculos_frota` |
| `loja_pronta` | `lojas_prontas_carregamento` |

Worker-only features sem model Django (TMS geofence APIs, page-builder Cloudflare) permanecem fora do Render Django — o CRUD operacional vive no banco Gestao CD via `module_list`.
