"""Menu e grade da operação Gestão CD dentro do shell do Assistente."""

CATALOGO = (
    (
        "Cargas",
        (
            ("/tms/", "fa-gauge-high", "Dashboard TMS"),
            ("/tms/romaneios/novo/", "fa-route", "Novo romaneio"),
            ("/tms/romaneios/", "fa-list-check", "Romaneios"),
            ("/tms/romaneios/rascunhos/", "fa-inbox", "Pré-romaneios"),
            ("/tms/divergencias/", "fa-triangle-exclamation", "Divergências"),
            ("/tms/expedicao/", "fa-truck-ramp-box", "Central de expedição"),
            ("/tms/executar/", "fa-clipboard-list", "Executar OT"),
            ("/tms/acompanhamento/", "fa-truck-fast", "Acompanhamento"),
            ("/tms/mdfe/", "fa-file-contract", "MDF-e"),
            ("/tms/viagens/vincular/", "fa-link", "Vincular viagem"),
            ("/tms/relatorio/", "fa-file-lines", "Relatório TMS"),
            ("/tms/rotas/otimizar/", "fa-wand-magic-sparkles", "Otimizar rotas"),
        ),
    ),
    (
        "Pátio",
        (
            ("/recebimento-operacional/", "fa-dolly", "Recebimento"),
            ("/recebimento-operacional/portaria/", "fa-door-open", "Portaria"),
            ("/recebimento-operacional/novo/", "fa-plus", "Novo recebimento"),
            ("/recebimento-operacional/divergencias/", "fa-scale-unbalanced", "Divergências de recebimento"),
            ("/sem-saldo/", "fa-box-open", "Sem saldo"),
            ("/vale-palete/", "fa-pallet", "Vale palete"),
            ("/yms/", "fa-warehouse", "Pátio e docas"),
            ("/wms/mapa/", "fa-map", "Mapa do armazém"),
            ("/separacao-controle/", "fa-boxes-stacked", "Separação"),
            ("/avaria-controle/", "fa-burst", "Avarias"),
            ("/expedicao-controle/", "fa-truck-arrow-right", "Controle de expedição"),
            ("/produtos-cd/", "fa-barcode", "Produtos do CD"),
            ("/veiculos-disponibilidade/", "fa-truck", "Veículos"),
        ),
    ),
    (
        "Torre",
        (
            ("/tms/torre-controle/", "fa-location-crosshairs", "Torre de controle"),
            ("/tms/monitoramento/mapa/", "fa-map-location-dot", "Mapa ao vivo"),
            ("/tms/painel-tv/", "fa-tv", "Painel TV"),
            ("/tms/rotas/", "fa-route", "Rotas"),
            ("/tms/viagens/capacidade/", "fa-weight-hanging", "Capacidade"),
            ("/dashboard/indicadores/", "fa-chart-line", "Indicadores"),
            ("/dashboard/rupturas/", "fa-chart-column", "Rupturas"),
            ("/tms/zero-toque/", "fa-hand-pointer", "Zero toque"),
            ("/tms/checklist-viagem/", "fa-list-check", "Checklist da viagem"),
            ("/tms/motorista/painel/", "fa-id-card", "Painel do motorista"),
            ("/tms/transferencias/", "fa-right-left", "Transferências"),
            ("/tms/recebimento/", "fa-truck-ramp-box", "Receber transferência"),
            ("/tms/logistica-reversa/", "fa-rotate-left", "Logística reversa"),
            ("/tms/geofencing/cercas/", "fa-draw-polygon", "Cercas"),
            ("/tms/geofencing/dispositivos/", "fa-mobile-screen", "Dispositivos"),
            ("/hub-produtividade/", "fa-chart-pie", "Produtividade"),
            ("/alertas/", "fa-bell", "Alertas"),
            ("/buscar/", "fa-magnifying-glass", "Busca"),
        ),
    ),
)


def _caminho(path):
    texto = path or "/"
    return texto if texto.endswith("/") else f"{texto}/"


def grupos_menu(path):
    """Marca só o link mais específico de todo o menu."""
    atual = _caminho(path)
    grupos = []
    melhor_href = ""
    for _titulo, itens in CATALOGO:
        links = [
            {"href": href, "icon": icone, "label": rotulo, "active": False}
            for href, icone, rotulo in itens
        ]
        for link in links:
            href = link["href"]
            if (atual == href or atual.startswith(href)) and len(href) > len(melhor_href):
                melhor_href = href
        grupos.append({"title": _titulo, "links": links, "open": False})
    if melhor_href:
        for grupo in grupos:
            for link in grupo["links"]:
                if link["href"] == melhor_href:
                    link["active"] = True
                    grupo["open"] = True
    elif atual.startswith(("/tms/", "/wms/", "/yms/", "/patio-docas", "/dashboard/")):
        grupos[0]["open"] = True
    return grupos
