# Render Keep-Alive

O Render no plano gratuito pode dormir quando fica sem acessos. Para reduzir esse problema, o projeto usa camadas de sentinela.

## 1. Sentinela Windows

Arquivos na pasta do projeto:

- `INICIAR_SENTINELA_RENDER.bat`
- `INSTALAR_SENTINELA_RENDER_TAREFA.bat`
- `REMOVER_SENTINELA_RENDER_TAREFA.bat`
- `scripts/sentinela_render_windows.ps1`

Esse modo é o mais importante, porque não depende de aba de navegador. Ele chama:

- `https://modelo-teste-operacional.onrender.com/manter-online/`
- `https://modelo-teste-operacional.onrender.com/health/`

Uso manual:

1. Abra a pasta do projeto.
2. Dê duplo clique em `INICIAR_SENTINELA_RENDER.bat`.
3. Deixe a janela aberta.

Uso automático no Windows:

1. Dê duplo clique em `INSTALAR_SENTINELA_RENDER_TAREFA.bat`.
2. Se aparecer sucesso, a tarefa vai iniciar quando o usuário entrar no Windows.
3. Para remover, use `REMOVER_SENTINELA_RENDER_TAREFA.bat`.

Limite: o notebook pode ficar bloqueado por senha, mas não pode suspender. Se suspender, desligar ou ficar sem internet, o ping para.

## 2. Tela Sentinela no sistema

A tela `/sentinela/` continua existindo, mas depende do navegador. Ela ajuda, mas pode parar se o navegador pausar a aba.

## 3. GitHub Actions

O projeto também usa um workflow do GitHub Actions em:

`.github/workflows/keep-render-awake.yml`

Ele roda a cada 5 minutos e chama:

- `https://modelo-teste-operacional.onrender.com/health/`
- `https://modelo-teste-operacional.onrender.com/status-saude/`
- `https://modelo-teste-operacional.onrender.com/manter-online/`
- `https://modelo-teste-operacional.onrender.com/login/`

Para conferir no GitHub:

1. Abra o repositório `New-Legend/assistente-cd-krill-teste`.
2. Entre em `Actions`.
3. Se aparecer botão para habilitar workflows, habilite.
4. Abra o workflow `Manter Render Online`.
5. Use `Run workflow` para testar manualmente.

Observação: no plano gratuito, nenhuma solução é garantia absoluta. A solução mais estável é um monitor externo dedicado ou o plano pago do Render.
