"""Telas do Gestão CD (Worker) traduzidas para Django.

O que o Assistente já operava em /modulo/ continua naquele motor.
Aqui ficam os fluxos que só existiam no Worker: TMS, WMS, YMS,
indicadores, capacidade, telemetria e o catálogo do page builder.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import timedelta
from io import StringIO
from urllib.parse import quote

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import (
    AuditLog,
    Avaria,
    BackupLog,
    ChamadoSaldo,
    Conferencia,
    Equipamento,
    Expedicao,
    PageBuilderTela,
    PaleteRedeMovimentacao,
    PaleteRedeSaldo,
    Pendencia,
    PessoaTurno,
    ProdutoGtin,
    Recebimento,
    RecebimentoAgenda,
    Separacao,
    TmsDevolucao,
    TmsDivergencia,
    TmsGeofence,
    TmsProdutoCapacidade,
    TmsRomaneio,
    TmsRota,
    TmsTelemetria,
    TmsVeiculo,
    TmsViagem,
    VeiculoFrota,
    WmsPosicao,
    YmsDoca,
    YmsMovimentacao,
)

ROMANEIO_STATUS = {
    "rascunho": "Rascunho",
    "pendente_conferencia": "Pendente de conferência",
    "aguardando_conferencia": "Aguardando conferência",
    "aguardando_complemento": "Aguardando complemento",
    "conferido": "Conferido",
    "em_transporte": "Em transporte",
    "entregue": "Entregue",
    "finalizado": "Finalizado",
    "cancelado": "Cancelado",
}
DIVERGENCIA_TIPO = {
    "atraso": "Atraso",
    "nf": "NF-e",
    "palete": "Palete",
    "lacre": "Lacre",
    "avaria": "Avaria",
    "loja": "Divergência loja",
    "operacional": "Operacional",
    "outro": "Outro",
}
DIVERGENCIA_SEV = {"normal": "Normal", "atencao": "Atenção", "critica": "Crítica"}
DIVERGENCIA_STATUS = {"aberta": "Aberta", "tratada": "Tratada", "cancelada": "Cancelada"}
ABERTOS = (
    "rascunho",
    "pendente_conferencia",
    "aguardando_conferencia",
    "aguardando_complemento",
    "conferido",
    "em_transporte",
    "entregue",
)
DATA_SOURCES = [
    ("tms_romaneios", "Romaneios / TMS"),
    ("recebimentos", "Recebimento operacional"),
    ("recebimento_chegadas", "Chegadas da Portaria"),
    ("yms_docas", "Doca / Pátio"),
    ("paletes_cd", "Conta-corrente de paletes"),
    ("recebimento_divergencias", "Divergências do recebimento"),
    ("vale_paletes", "Vale-Palete Digital"),
]
PAGE_ACTIONS = [
    ("Criar romaneio", "/tms/romaneios/novo/"),
    ("Cargas prontas", "/tms/romaneios/rascunhos/"),
    ("Exportar romaneios", "/tms/romaneios/exportar/"),
    ("Novo recebimento", "/recebimento-operacional/novo/"),
    ("Fila de chegadas", "/recebimento-operacional/"),
    ("Divergências do recebimento", "/recebimento-operacional/divergencias/"),
    ("Relatório de recebimento", "/recebimento-relatorio/"),
    ("Cobrança de descarga", "/recebimento-descarga-relatorio/"),
    ("Novo vale-palete", "/vale-palete/novo/"),
    ("Conta-corrente de paletes", "/vale-palete/conta-corrente/"),
    ("Gestão de docas", "/yms/"),
    ("Registrar chegada", "/yms/patio/"),
    ("Romaneios", "/tms/romaneios/"),
]


def cell(text, tone="", href="", forms=None):
    return {"text": "" if text is None else str(text), "tone": tone, "href": href or "", "forms": forms or []}


def blank_screen(**kwargs):
    base = {
        "title": "",
        "eyebrow": "Gestão CD",
        "lead": "",
        "actions": [],
        "notice": "",
        "error": "",
        "denied": False,
        "cards": [],
        "filters": [],
        "form": None,
        "tables": [],
        "docks": [],
        "streets": [],
        "show_map": False,
        "map_trips": [],
        "map_zones": [],
    }
    base.update(kwargs)
    return base


def _views():
    from . import views

    return views


def allowed(user) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    views = _views()
    return views.user_has_perm(user, "tms_krill") or views.user_has_perm(user, "tms_expedicao") or views.user_has_perm(user, "painel")


def cd_codes(request) -> list[str]:
    views = _views()
    return list(views.cd_values(views.current_cd(request)))


def current_cd_code(request) -> str:
    return str(_views().current_cd(request))


def today():
    return timezone.localdate()


def parse_date(value, default):
    from datetime import datetime

    try:
        return datetime.strptime(value or "", "%Y-%m-%d").date()
    except ValueError:
        return default


def brl(value) -> str:
    formatted = f"{float(value or 0):,.2f}"
    return "R$ " + formatted.replace(",", "X").replace(".", ",").replace("X", ".")


def num(value) -> str:
    return f"{float(value or 0):,.0f}".replace(",", ".")


def tone_for(status: str) -> str:
    if status in {"finalizado", "entregue", "conferido", "tratada", "livre", "concluido", "ok", "LIVRE"}:
        return "ok"
    if status in {"cancelado", "cancelada", "aberta", "critica", "BLOQUEADO", "danger", "aguardando_doca"}:
        return "danger"
    return "warning"


def status_cell(code: str, labels: dict) -> dict:
    return cell(labels.get(code, code or "-"), tone_for(code))


def render_screen(request, screen, status=200):
    ctx = _views().context_base(request)
    ctx["screen"] = screen
    ctx["page_title"] = screen["title"]
    return render(request, "painel/krill_tela.html", ctx, status=status)


def deny(request, title):
    return render_screen(
        request,
        blank_screen(title=title, denied=True, error="Sem permissão para esta tela."),
        status=403,
    )


def romaneios_qs(request):
    query = Q()
    for code in cd_codes(request):
        query |= Q(cd_origem=code) | Q(cd_origem__iendswith=code)
    return TmsRomaneio.objects.filter(query)


def operacional(model, request):
    return model.objects.filter(cd_unidade__in=cd_codes(request))


def fmt_dt(value) -> str:
    if not value:
        return "-"
    return timezone.localtime(value).strftime("%d/%m/%Y %H:%M")


def table_from(qs, specs):
    headers = [label for _attr, label in specs]
    rows = []
    for obj in qs:
        row = []
        for attr, _label in specs:
            value = obj
            for part in attr.split("."):
                value = getattr(value, part, "") if value is not None else ""
            if hasattr(value, "strftime"):
                value = value.strftime("%d/%m/%Y") if getattr(value, "hour", None) is None else fmt_dt(value)
            row.append(cell(value if value not in (None, "") else "-"))
        rows.append(row)
    return {"title": "", "lead": "", "headers": headers, "rows": rows}


def csv_response(filename, headers, rows):
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    response = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def username(user) -> str:
    return getattr(user, "username", "") or ""


@login_required
def alias_home(request):
    return redirect("/")


@login_required
def alias_to(request, target):
    return redirect(target)


def _gate(request, title):
    if not allowed(request.user):
        return deny(request, title)
    return None


@login_required
@require_http_methods(["GET"])
def tms_dashboard(request):
    blocked = _gate(request, "Dashboard TMS")
    if blocked:
        return blocked
    inicio = parse_date(request.GET.get("inicio"), today())
    fim = parse_date(request.GET.get("fim"), inicio)
    rows = list(romaneios_qs(request).filter(data__gte=inicio, data__lte=fim))
    finalizados = sum(1 for row in rows if row.status == "finalizado")
    conferidos = sum(1 for row in rows if row.status in {"conferido", "aguardando_conferencia"})
    em_transporte = sum(1 for row in rows if row.status == "em_transporte")
    paletes = sum(row.total_paletes or 0 for row in rows)
    valor = sum(row.valor_total_carga or 0 for row in rows)
    km = sum(row.km_rodado for row in rows)
    devolucoes = list(TmsDevolucao.objects.filter(romaneio__in=[row.id for row in rows], data__gte=inicio, data__lte=fim))
    divergencias = list(TmsDivergencia.objects.filter(data__gte=inicio, data__lte=fim).select_related("romaneio"))
    abertas = [row for row in divergencias if row.status == "aberta"]
    by_status: dict[str, int] = {}
    by_loja: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        by_loja[row.loja_destino or "Loja não informada"] = by_loja.get(row.loja_destino or "Loja não informada", 0) + (row.total_paletes or 0)
    produtividade = round((finalizados / len(rows)) * 100) if rows else 0
    return render_screen(
        request,
        blank_screen(
            title="Dashboard TMS",
            eyebrow="TMS",
            lead="Indicadores de romaneios, entregas, paletes, KM, devoluções e divergências no período.",
            actions=[
                {"href": "/tms/relatorio/", "label": "Relatório"},
                {"href": "/tms/painel-tv/", "label": "Modo TV"},
                {"href": "/tms/romaneios/", "label": "Romaneios"},
            ],
            filters=[
                {"name": "inicio", "label": "Início", "type": "date", "value": inicio.isoformat()},
                {"name": "fim", "label": "Fim", "type": "date", "value": fim.isoformat()},
            ],
            cards=[
                {"label": "Romaneios", "value": len(rows), "hint": "cargas no período"},
                {"label": "Conferidos", "value": conferidos, "hint": "romaneio pronto"},
                {"label": "Em transporte", "value": em_transporte, "hint": "cargas na rua"},
                {"label": "Finalizados", "value": finalizados, "hint": f"{produtividade}% concluído"},
                {"label": "Paletes", "value": paletes, "hint": "total transportado"},
                {"label": "Valor da carga", "value": brl(valor), "hint": "somatório informado"},
                {"label": "KM rodado", "value": num(km), "hint": "quando informado"},
                {"label": "Devoluções", "value": len(devolucoes), "hint": brl(sum(item.valor_devolvido or 0 for item in devolucoes))},
                {"label": "Divergências abertas", "value": len(abertas), "hint": f"{len(divergencias)} no período"},
            ],
            tables=[
                {
                    "title": "Situação dos romaneios",
                    "lead": "Distribuição operacional do período.",
                    "headers": ["Situação", "Total"],
                    "rows": [[status_cell(key, ROMANEIO_STATUS), cell(total)] for key, total in sorted(by_status.items(), key=lambda item: -item[1])],
                },
                {
                    "title": "Lojas com mais paletes",
                    "lead": "Concentração de carga por destino.",
                    "headers": ["Loja", "Paletes"],
                    "rows": [[cell(loja), cell(total)] for loja, total in sorted(by_loja.items(), key=lambda item: -item[1])[:12]],
                },
                {
                    "title": "Divergências abertas",
                    "lead": "Pontos que ainda precisam de tratativa.",
                    "headers": ["Data", "Romaneio", "Tipo", "Severidade", "Descrição"],
                    "rows": [
                        [
                            cell(row.data.strftime("%d/%m/%Y")),
                            cell(row.romaneio.numero_romaneio if row.romaneio else "-"),
                            cell(DIVERGENCIA_TIPO.get(row.tipo, row.tipo)),
                            cell(DIVERGENCIA_SEV.get(row.severidade, row.severidade), tone_for(row.severidade)),
                            cell(row.descricao),
                        ]
                        for row in abertas[:8]
                    ],
                },
            ],
        ),
    )


@login_required
def tms_painel_tv(request):
    return tms_dashboard(request)


@login_required
@require_http_methods(["GET"])
def tms_relatorio(request):
    blocked = _gate(request, "Relatório TMS")
    if blocked:
        return blocked
    inicio = parse_date(request.GET.get("inicio"), today())
    fim = parse_date(request.GET.get("fim"), inicio)
    rows = list(romaneios_qs(request).filter(data__gte=inicio, data__lte=fim).order_by("data", "hora", "id"))
    devolucoes = list(TmsDevolucao.objects.filter(data__gte=inicio, data__lte=fim, romaneio__in=[row.id for row in rows] or [0]))
    divergencias = list(TmsDivergencia.objects.filter(data__gte=inicio, data__lte=fim))
    return render_screen(
        request,
        blank_screen(
            title="Relatório TMS",
            eyebrow="TMS",
            lead="Fechamento por período para acompanhar expedição, transporte, devoluções e divergências.",
            actions=[{"href": f"/tms/relatorio/exportar/?inicio={inicio.isoformat()}&fim={fim.isoformat()}", "label": "Baixar CSV"}],
            filters=[
                {"name": "inicio", "label": "Início", "type": "date", "value": inicio.isoformat()},
                {"name": "fim", "label": "Fim", "type": "date", "value": fim.isoformat()},
            ],
            cards=[
                {"label": "Romaneios", "value": len(rows), "hint": "cargas geradas"},
                {"label": "Paletes", "value": sum(row.total_paletes or 0 for row in rows), "hint": "total transportado"},
                {"label": "Valor", "value": brl(sum(row.valor_total_carga or 0 for row in rows)), "hint": "valor da carga"},
                {"label": "KM", "value": num(sum(row.km_rodado for row in rows)), "hint": "informado no retorno"},
                {"label": "Devoluções", "value": len(devolucoes), "hint": brl(sum(row.valor_devolvido or 0 for row in devolucoes))},
                {"label": "Divergências", "value": len(divergencias), "hint": f"{sum(1 for row in divergencias if row.status == 'aberta')} abertas"},
            ],
            tables=[
                {
                    "title": "Romaneios do período",
                    "lead": "Base operacional do relatório.",
                    "headers": ["Romaneio", "Data", "Loja", "Motorista", "Situação", "NF-e", "Paletes", "Valor", "KM"],
                    "rows": [
                        [
                            cell(row.numero_romaneio, href=f"/tms/romaneios/{row.id}/"),
                            cell(f"{row.data:%d/%m/%Y} {row.hora}"),
                            cell(row.loja_destino or "-"),
                            cell(f"{row.motorista or '-'} · {row.placa or '-'}"),
                            status_cell(row.status, ROMANEIO_STATUS),
                            cell(row.quantidade_nfes),
                            cell(row.total_paletes),
                            cell(brl(row.valor_total_carga)),
                            cell(num(row.km_rodado) if row.km_rodado else "-"),
                        ]
                        for row in rows
                    ],
                }
            ],
        ),
    )


@login_required
def tms_relatorio_export(request):
    blocked = _gate(request, "Relatório TMS")
    if blocked:
        return blocked
    inicio = parse_date(request.GET.get("inicio"), today())
    fim = parse_date(request.GET.get("fim"), inicio)
    rows = romaneios_qs(request).filter(data__gte=inicio, data__lte=fim).order_by("data", "hora", "id")
    return csv_response(
        f"gestao_cd_tms_{inicio}_{fim}.csv",
        ["numero", "data", "cd", "loja", "motorista", "placa", "nfes", "valor", "paletes", "status", "km"],
        [
            [row.numero_romaneio, row.data.isoformat(), row.cd_origem, row.loja_destino, row.motorista, row.placa, row.quantidade_nfes, row.valor_total_carga, row.total_paletes, row.status, row.km_rodado]
            for row in rows
        ],
    )


def _romaneio_table(rows):
    return {
        "title": "Romaneios",
        "lead": f"{len(rows)} registro(s).",
        "headers": ["Romaneio", "Data", "Loja", "Motorista", "Placa", "Paletes", "Valor", "Situação"],
        "rows": [
            [
                cell(row.numero_romaneio, href=f"/tms/romaneios/{row.id}/"),
                cell(f"{row.data:%d/%m/%Y} {row.hora}"),
                cell(row.loja_destino or "-"),
                cell(row.motorista or "-"),
                cell(row.placa or "-"),
                cell(row.total_paletes),
                cell(brl(row.valor_total_carga)),
                status_cell(row.status, ROMANEIO_STATUS),
            ]
            for row in rows
        ],
    }


def _notas_do_formulario(texto: str) -> list[dict]:
    notas = []
    for linha in (texto or "").splitlines():
        linha = linha.strip()
        if not linha:
            continue
        if ";" in linha:
            chave_bruta, _, valor_bruto = linha.partition(";")
        else:
            partes = linha.split()
            chave_bruta = partes[0]
            valor_bruto = partes[1] if len(partes) > 1 else "0"
        chave = re.sub(r"\D", "", chave_bruta)
        if not chave:
            continue
        try:
            valor = float(str(valor_bruto).strip().replace(".", "").replace(",", ".")) if "," in str(valor_bruto) else float(str(valor_bruto).strip().replace(",", "."))
        except ValueError:
            valor = 0
        notas.append({"chave_acesso": chave, "valor": valor})
    return notas


@login_required
@require_http_methods(["GET", "POST"])
def tms_romaneios(request):
    blocked = _gate(request, "Romaneios")
    if blocked:
        return blocked
    notice = ""
    if request.method == "POST":
        numero = (request.POST.get("numero_romaneio") or "").strip()
        loja = (request.POST.get("loja_destino") or "").strip()
        if not numero or not loja:
            notice = "Informe o número do romaneio e a loja."
        elif TmsRomaneio.objects.filter(numero_romaneio=numero).exists():
            notice = "Já existe um romaneio com esse número."
        else:
            from .bluesoft_valor import criar_romaneio_com_notas, lancar_snapshot

            notas = _notas_do_formulario(request.POST.get("notas") or "")
            row = TmsRomaneio(
                numero_romaneio=numero,
                data=parse_date(request.POST.get("data"), today()),
                hora=(request.POST.get("hora") or "")[:8],
                cd_origem=current_cd_code(request)[:7],
                loja_destino=loja,
                motorista=(request.POST.get("motorista") or "").strip(),
                placa=(request.POST.get("placa") or "").strip().upper(),
                quantidade_nfes=int(float(request.POST.get("quantidade_nfes") or 0)),
                total_paletes=int(float(request.POST.get("total_paletes") or 0)),
                paletes_pbr=int(float(request.POST.get("total_paletes") or 0)),
                status="pendente_conferencia" if notas else "rascunho",
                observacoes=(request.POST.get("observacoes") or "").strip(),
                criado_por=username(request.user),
                atualizado_por=username(request.user),
            )
            if notas:
                resultado = criar_romaneio_com_notas(row, notas, username(request.user))
                if not resultado["sucesso"]:
                    notice = resultado["erro"]
                else:
                    aviso = f"Romaneio gravado. {resultado['qtd_notas_novas']} NF-e nova(s)"
                    if resultado["descartadas"]:
                        aviso += f", {resultado['descartadas']} já expedida(s) descartada(s)"
                    aviso += f". A faturar {brl(resultado['valor_faturado'])}."
                    return redirect(f"/tms/romaneios/?notice={quote(aviso)}")
            else:
                erro = lancar_snapshot(row, float(request.POST.get("valor_total_carga") or 0), username(request.user))
                if erro:
                    notice = erro
                else:
                    return redirect("/tms/romaneios/")
    data = parse_date(request.GET.get("data"), today())
    q = (request.GET.get("q") or "").strip()
    status = request.GET.get("status") or ""
    loja = (request.GET.get("loja") or "").strip()
    found = romaneios_qs(request).filter(data=data)
    if q:
        found = found.filter(Q(numero_romaneio__icontains=q) | Q(loja_destino__icontains=q) | Q(motorista__icontains=q) | Q(placa__icontains=q) | Q(lacres__icontains=q))
    if status:
        found = found.filter(status=status)
    if loja:
        found = found.filter(loja_destino__icontains=loja)
    rows = list(found.order_by("loja_destino", "hora", "id")[:120])
    rascunhos = romaneios_qs(request).filter(status__in=["rascunho", "pendente_conferencia"]).count()
    return render_screen(
        request,
        blank_screen(
            title="Romaneios / TMS",
            eyebrow="TMS",
            lead="Gestão de cargas, frotas e roteirização.",
            notice=notice or request.GET.get("notice") or "",
            actions=[
                {"href": "/tms/romaneios/novo/", "label": "Novo romaneio"},
                {"href": "/tms/romaneios/rascunhos/", "label": f"Pré-romaneios ({rascunhos})"},
                {"href": f"/tms/romaneios/exportar/?data={data.isoformat()}", "label": "Exportar"},
            ],
            filters=[
                {"name": "data", "label": "Data", "type": "date", "value": data.isoformat()},
                {"name": "q", "label": "Pesquisar", "type": "text", "value": q},
                {"name": "loja", "label": "Loja", "type": "text", "value": loja},
                {"name": "status", "label": "Situação", "type": "select", "value": status, "options": [("", "Todas")] + list(ROMANEIO_STATUS.items())},
            ],
            cards=[
                {"label": "Listados", "value": len(rows), "hint": "conforme filtros"},
                {"label": "Conferidos", "value": sum(1 for row in rows if row.status in {"conferido", "aguardando_conferencia"}), "hint": "romaneio pronto"},
                {"label": "Em transporte", "value": sum(1 for row in rows if row.status == "em_transporte"), "hint": "cargas na rua"},
                {"label": "Rascunhos", "value": sum(1 for row in rows if row.status == "rascunho"), "hint": "pendentes de conferência"},
                {"label": "Finalizados", "value": sum(1 for row in rows if row.status == "finalizado"), "hint": "ciclo encerrado"},
                {"label": "Paletes", "value": sum(row.total_paletes or 0 for row in rows), "hint": "PBR, CHEP e descartável"},
            ],
            tables=[_romaneio_table(rows)],
        ),
    )


@login_required
def tms_romaneios_novo(request):
    blocked = _gate(request, "Novo romaneio")
    if blocked:
        return blocked
    if request.method == "POST":
        return tms_romaneios(request)
    return render_screen(
        request,
        blank_screen(
            title="Novo romaneio",
            eyebrow="TMS",
            lead="Com NF-e na lista, as chaves já vinculadas são descartadas e o valor faturado é só a soma do que sobrou. Sem NF-e, o campo acima é o total acumulado da BlueSoft e o sistema grava a diferença do dia.",
            actions=[{"href": "/tms/romaneios/", "label": "Voltar"}],
            form={
                "action": "/tms/romaneios/novo/",
                "title": "Dados da carga",
                "submit": "Salvar rascunho",
                "fields": [
                    {"name": "numero_romaneio", "label": "Número", "type": "text", "value": "", "required": True},
                    {"name": "data", "label": "Data", "type": "date", "value": today().isoformat()},
                    {"name": "hora", "label": "Hora", "type": "text", "value": ""},
                    {"name": "loja_destino", "label": "Loja destino", "type": "text", "value": "", "required": True},
                    {"name": "motorista", "label": "Motorista", "type": "text", "value": ""},
                    {"name": "placa", "label": "Placa", "type": "text", "value": ""},
                    {"name": "quantidade_nfes", "label": "NF-e", "type": "number", "value": "0"},
                    {"name": "total_paletes", "label": "Paletes", "type": "number", "value": "0"},
                    {"name": "valor_total_carga", "label": "Total acumulado na BlueSoft", "type": "number", "value": "0"},
                    {"name": "notas", "label": "NF-e (chave;valor, uma por linha)", "type": "textarea", "value": "", "wide": True},
                    {"name": "observacoes", "label": "Observações", "type": "textarea", "value": "", "wide": True},
                ],
            },
        ),
    )


@login_required
def tms_romaneio_detalhe(request, pk):
    blocked = _gate(request, "Romaneio")
    if blocked:
        return blocked
    row = get_object_or_404(romaneios_qs(request), pk=pk)
    devolucoes = list(row.devolucoes.all())
    divergencias = list(row.divergencias.all())
    notas = list(row.nfes.all())
    conferidas = sum(1 for note in notas if note.status_conferencia == "conferida")
    return render_screen(
        request,
        blank_screen(
            title=row.numero_romaneio,
            eyebrow="TMS / Romaneio",
            lead=f"{row.loja_destino or 'Sem loja'} · {row.motorista or 'Sem motorista'} · {row.placa or 'Sem placa'}",
            actions=[
                {"href": "/tms/romaneios/", "label": "Lista"},
                {"href": f"/tms/romaneios/{row.pk}/editar/", "label": "Editar"},
                {"href": f"/tms/romaneios/{row.pk}/imprimir/", "label": "Imprimir"},
                {"href": f"/tms/romaneios/{row.pk}/km/", "label": "KM e paletes"},
                {"href": f"/tms/romaneios/{row.pk}/bluesoft/", "label": "Acumulado BlueSoft"},
                {"href": "/tms/mdfe/", "label": "MDF-e"},
            ],
            cards=[
                {"label": "Situação", "value": ROMANEIO_STATUS.get(row.status, row.status), "hint": row.cd_origem},
                {"label": "Paletes", "value": row.total_paletes, "hint": f"PBR {row.paletes_pbr} · CHEP {row.paletes_chep}"},
                {"label": "A faturar", "value": brl(row.valor_total_carga), "hint": f"acumulado BlueSoft {brl(row.valor_acumulado_bluesoft)}"},
                {"label": "Conferência", "value": f"{conferidas}/{len(notas)}", "hint": "bipadas antes da impressão"},
                {"label": "KM", "value": num(row.km_rodado) if row.km_rodado else "-", "hint": f"ordem {row.ordem_entrega or '-'}"},
            ],
            form={
                "action": f"/api/romaneios/{row.pk}/bipar/",
                "title": "Bipar NF-e",
                "submit": "Conferir nota",
                "fields": [{"name": "chave_acesso", "label": "Chave de 44 dígitos", "type": "text", "value": "", "required": True}],
            },
            tables=[
                {
                    "title": "NF-e do embarque",
                    "lead": f"{conferidas} conferida(s) de {len(notas)}.",
                    "headers": ["Número", "Chave", "Situação"],
                    "rows": [
                        [cell(note.numero or "-"), cell(note.chave_acesso), status_cell(note.status_conferencia, {"pendente": "Pendente", "conferida": "Conferida"})]
                        for note in notas
                    ],
                },
                {
                    "title": "Linha do tempo",
                    "lead": row.observacoes or "Sem observações.",
                    "headers": ["Marco", "Quando"],
                    "rows": [
                        [cell("Saída do CD"), cell(fmt_dt(row.data_saida))],
                        [cell("Chegada na loja"), cell(fmt_dt(row.data_chegada_loja))],
                        [cell("Retorno ao CD"), cell(fmt_dt(row.data_retorno_cd))],
                        [cell("Recebido por"), cell(row.recebido_por or "-")],
                        [cell("Lacres"), cell(row.lacres or "-")],
                    ],
                },
                {
                    "title": "Divergências",
                    "lead": "",
                    "headers": ["Data", "Tipo", "Descrição", "Situação"],
                    "rows": [
                        [cell(item.data.strftime("%d/%m/%Y")), cell(DIVERGENCIA_TIPO.get(item.tipo, item.tipo)), cell(item.descricao), status_cell(item.status, DIVERGENCIA_STATUS)]
                        for item in divergencias
                    ],
                },
                {
                    "title": "Devoluções",
                    "lead": "",
                    "headers": ["Data", "Tipo", "Motivo", "Valor"],
                    "rows": [[cell(item.data.strftime("%d/%m/%Y")), cell(item.tipo), cell(item.motivo or "-"), cell(brl(item.valor_devolvido))] for item in devolucoes],
                },
            ],
        ),
    )


@login_required
def tms_romaneios_export(request):
    blocked = _gate(request, "Romaneios")
    if blocked:
        return blocked
    data = parse_date(request.GET.get("data"), today())
    rows = romaneios_qs(request).filter(data=data)
    return csv_response(
        f"romaneios_{data}.csv",
        ["numero", "data", "loja", "motorista", "placa", "paletes", "valor", "status"],
        [[row.numero_romaneio, row.data.isoformat(), row.loja_destino, row.motorista, row.placa, row.total_paletes, row.valor_total_carga, row.status] for row in rows],
    )


@login_required
def tms_rascunhos(request):
    blocked = _gate(request, "Pré-romaneios")
    if blocked:
        return blocked
    rows = list(romaneios_qs(request).filter(status__in=["rascunho", "pendente_conferencia"]).order_by("-data", "-id")[:120])
    screen_table = _romaneio_table(rows)
    screen_table["title"] = "Cargas prontas e rascunhos"
    return render_screen(
        request,
        blank_screen(
            title="Pré-romaneios",
            eyebrow="TMS",
            lead="Rascunhos e cargas ainda pendentes de conferência no CD ativo.",
            actions=[{"href": "/tms/romaneios/novo/", "label": "Novo"}, {"href": "/tms/romaneios/", "label": "Todos"}],
            cards=[{"label": "Pendentes", "value": len(rows), "hint": "rascunho ou conferência"}],
            tables=[screen_table],
        ),
    )


@login_required
def tms_rascunhos_feed(request):
    if not allowed(request.user):
        return JsonResponse({"ok": False}, status=403)
    total = romaneios_qs(request).filter(status__in=["rascunho", "pendente_conferencia"]).count()
    return JsonResponse({"ok": True, "total": total})


@login_required
@require_http_methods(["GET", "POST"])
def tms_divergencias(request):
    blocked = _gate(request, "Divergências TMS")
    if blocked:
        return blocked
    if request.method == "POST":
        descricao = (request.POST.get("descricao") or "").strip()
        if descricao:
            romaneio = None
            raw_id = request.POST.get("romaneio_id") or ""
            if raw_id.isdigit():
                romaneio = romaneios_qs(request).filter(pk=int(raw_id)).first()
            TmsDivergencia.objects.create(
                romaneio=romaneio,
                data=parse_date(request.POST.get("data"), today()),
                tipo=request.POST.get("tipo") or "operacional",
                severidade=request.POST.get("severidade") or "normal",
                descricao=descricao,
                responsavel=(request.POST.get("responsavel") or username(request.user))[:160],
                status=request.POST.get("status") or "aberta",
                solucao=(request.POST.get("solucao") or "").strip(),
                criado_por=username(request.user),
                atualizado_por=username(request.user),
            )
        return redirect("/tms/divergencias/")
    rows = list(TmsDivergencia.objects.select_related("romaneio").order_by("-data", "-id")[:160])
    options = [(str(row.id), f"{row.numero_romaneio} - {row.loja_destino or '-'}") for row in romaneios_qs(request).order_by("-data", "-id")[:100]]
    return render_screen(
        request,
        blank_screen(
            title="Divergências TMS",
            eyebrow="TMS",
            lead="Registre atraso, lacre, palete, NF-e, avaria ou divergência apontada pela loja.",
            actions=[{"href": "/tms/dashboard/", "label": "Dashboard"}, {"href": "/tms/relatorio/", "label": "Relatório"}],
            form={
                "action": "/tms/divergencias/",
                "title": "Nova divergência",
                "submit": "Salvar divergência",
                "fields": [
                    {"name": "data", "label": "Data", "type": "date", "value": today().isoformat()},
                    {"name": "romaneio_id", "label": "Romaneio", "type": "select", "value": "", "options": [("", "Sem vínculo")] + options},
                    {"name": "tipo", "label": "Tipo", "type": "select", "value": "operacional", "options": list(DIVERGENCIA_TIPO.items())},
                    {"name": "severidade", "label": "Severidade", "type": "select", "value": "normal", "options": list(DIVERGENCIA_SEV.items())},
                    {"name": "status", "label": "Situação", "type": "select", "value": "aberta", "options": list(DIVERGENCIA_STATUS.items())},
                    {"name": "responsavel", "label": "Responsável", "type": "text", "value": username(request.user)},
                    {"name": "descricao", "label": "Descrição", "type": "textarea", "value": "", "wide": True, "required": True},
                    {"name": "solucao", "label": "Solução", "type": "textarea", "value": "", "wide": True},
                ],
            },
            tables=[
                {
                    "title": "Divergências registradas",
                    "lead": f"{len(rows)} registro(s) mais recentes.",
                    "headers": ["Data", "Romaneio", "Loja", "Tipo", "Severidade", "Descrição", "Situação", "Ação"],
                    "rows": [
                        [
                            cell(row.data.strftime("%d/%m/%Y")),
                            cell(row.romaneio.numero_romaneio if row.romaneio else "-"),
                            cell(row.romaneio.loja_destino if row.romaneio else "-"),
                            cell(DIVERGENCIA_TIPO.get(row.tipo, row.tipo)),
                            cell(DIVERGENCIA_SEV.get(row.severidade, row.severidade), tone_for(row.severidade)),
                            cell(row.descricao),
                            status_cell(row.status, DIVERGENCIA_STATUS),
                            cell(
                                "",
                                forms=[
                                    {"action": f"/tms/divergencias/{row.id}/status/", "fields": {"status": "tratada"}, "label": "Tratar"},
                                    {"action": f"/tms/divergencias/{row.id}/status/", "fields": {"status": "aberta"}, "label": "Reabrir"},
                                    {"action": f"/tms/divergencias/{row.id}/status/", "fields": {"status": "cancelada"}, "label": "Cancelar"},
                                ],
                            ),
                        ]
                        for row in rows
                    ],
                }
            ],
        ),
    )


@login_required
@require_http_methods(["POST"])
def tms_divergencia_status(request, pk):
    blocked = _gate(request, "Divergências TMS")
    if blocked:
        return blocked
    status = request.POST.get("status") or ""
    if status in DIVERGENCIA_STATUS:
        TmsDivergencia.objects.filter(pk=pk).update(status=status, atualizado_por=username(request.user), updated_at=timezone.now())
    return redirect("/tms/divergencias/")


def _mover_romaneio(row: TmsRomaneio, status: str, user) -> None:
    now = timezone.now()
    row.status = status
    row.atualizado_por = username(user)
    if status == "em_transporte" and not row.data_saida:
        row.data_saida = now
    if status == "entregue" and not row.data_chegada_loja:
        row.data_chegada_loja = now
    if status == "finalizado":
        row.data_retorno_cd = now
    row.save()


@login_required
@require_http_methods(["GET", "POST"])
def tms_executar(request):
    blocked = _gate(request, "Executar OT")
    if blocked:
        return blocked
    if request.method == "POST":
        row = romaneios_qs(request).filter(pk=request.POST.get("romaneio_id") or 0).first()
        status = request.POST.get("status") or ""
        allowed_steps = {
            "rascunho": "conferido",
            "conferido": "em_transporte",
            "aguardando_conferencia": "em_transporte",
            "em_transporte": "entregue",
            "entregue": "finalizado",
        }
        if row and allowed_steps.get(row.status) == status:
            from .krill_fase2 import aplicar_status_romaneio, erro_executar

            ok, message, _code = aplicar_status_romaneio(row, status, request.user, request.POST.get("justificativa") or "")
            if not ok:
                return redirect(erro_executar(message))
        return redirect("/tms/executar/")
    data = parse_date(request.GET.get("data"), today())
    status = request.GET.get("status") or ""
    q = (request.GET.get("q") or "").strip()
    found = romaneios_qs(request).filter(data=data).exclude(status__in=["cancelado", "finalizado"])
    if status:
        found = found.filter(status=status)
    if q:
        found = found.filter(Q(numero_romaneio__icontains=q) | Q(loja_destino__icontains=q) | Q(motorista__icontains=q) | Q(placa__icontains=q))
    rows = list(found.order_by("ordem_entrega", "hora", "id")[:120])
    body = []
    for row in rows:
        nxt = {"conferido": ("em_transporte", "Iniciar"), "em_transporte": ("entregue", "Chegou"), "entregue": ("finalizado", "Finalizar")}.get(row.status)
        forms = []
        if nxt:
            forms.append({"action": "/tms/executar/", "fields": {"romaneio_id": row.id, "status": nxt[0]}, "label": nxt[1]})
        body.append(
            [
                cell(row.ordem_entrega or "-"),
                cell(row.numero_romaneio, href=f"/tms/romaneios/{row.id}/"),
                cell(f"{row.cd_origem} → {row.loja_destino or '-'}"),
                cell(f"{row.motorista or '-'} · {row.placa or '-'}"),
                status_cell(row.status, ROMANEIO_STATUS),
                cell(f"{row.distancia_rota_km or '-'} km"),
                cell("", forms=forms),
            ]
        )
    return render_screen(
        request,
        blank_screen(
            title="Executar ordem de transporte",
            eyebrow="TMS",
            lead="A saída só libera depois da bipagem de 100% das NF-e. Conferir pelo botão livre foi bloqueado de propósito.",
            error=(request.GET.get("erro") or ""),
            actions=[{"href": "/tms/rotas/", "label": "Rotas"}, {"href": "/tms/acompanhamento/", "label": "Acompanhamento"}],
            filters=[
                {"name": "data", "label": "Data", "type": "date", "value": data.isoformat()},
                {"name": "q", "label": "Pesquisar", "type": "text", "value": q},
                {"name": "status", "label": "Situação", "type": "select", "value": status, "options": [("", "Todas")] + list(ROMANEIO_STATUS.items())},
            ],
            cards=[
                {"label": "Total do dia", "value": len(rows), "hint": "ordens encontradas"},
                {"label": "Para executar", "value": sum(1 for row in rows if row.status in {"rascunho", "conferido", "aguardando_conferencia"}), "hint": "aguardando saída"},
                {"label": "Em transporte", "value": sum(1 for row in rows if row.status == "em_transporte"), "hint": "na rua"},
                {"label": "Entregues", "value": sum(1 for row in rows if row.status == "entregue"), "hint": "aguardando finalização"},
            ],
            tables=[{"title": "Ordens do dia", "lead": "", "headers": ["Ordem", "OT", "Rota", "Motorista", "Situação", "Distância", "Ação"], "rows": body}],
        ),
    )


@login_required
def tms_acompanhamento(request):
    blocked = _gate(request, "Acompanhamento TMS")
    if blocked:
        return blocked
    status = request.GET.get("status") or ""
    found = romaneios_qs(request)
    found = found.filter(status=status) if status else found.filter(status__in=ABERTOS)
    rows = list(found.order_by("ordem_entrega", "-data", "id")[:120])
    rotas = list(TmsRota.objects.filter(ativa=True))
    body = []
    for row in rows:
        rota = _rota_da_loja(row, rotas)
        body.append(
            [
                cell(row.ordem_entrega or "-"),
                cell(row.numero_romaneio, href=f"/tms/romaneios/{row.id}/"),
                cell(row.loja_destino or "-"),
                cell(f"{row.motorista or '-'} · {row.placa or '-'}"),
                status_cell(row.status, ROMANEIO_STATUS),
                cell(fmt_dt(row.data_saida)),
                cell(fmt_dt(row.data_chegada_loja)),
                cell(num(row.km_rodado) if row.km_rodado else "-"),
                cell(f"{rota.distancia_km} km · {rota.tempo_previsto_minutos} min" if rota else "-"),
            ]
        )
    return render_screen(
        request,
        blank_screen(
            title="Acompanhamento de entrega",
            eyebrow="TMS",
            lead="Cargas em aberto, em transporte, chegada na loja, KM e previsão cadastrada por rota.",
            actions=[
                {"href": "/tms/dashboard/", "label": "Dashboard"},
                {"href": "/tms/romaneios/", "label": "Romaneios"},
                {"href": "/tms/rotas/", "label": "Rotas"},
                {"href": "/tms/monitoramento/mapa/", "label": "Mapa ao vivo"},
            ],
            filters=[{"name": "status", "label": "Situação", "type": "select", "value": status, "options": [("", "Abertos")] + list(ROMANEIO_STATUS.items())}],
            cards=[
                {"label": "Em aberto", "value": len(rows), "hint": "conforme filtro"},
                {"label": "Conferidos", "value": sum(1 for row in rows if row.status in {"conferido", "aguardando_conferencia", "aguardando_complemento"}), "hint": "romaneio pronto"},
                {"label": "Em transporte", "value": sum(1 for row in rows if row.status == "em_transporte"), "hint": "saíram do CD"},
                {"label": "Entregues", "value": sum(1 for row in rows if row.status == "entregue"), "hint": "aguardando finalização"},
            ],
            tables=[{"title": "Entregas", "lead": "", "headers": ["Ordem", "Romaneio", "Loja", "Motorista", "Situação", "Saída", "Chegada", "KM", "Rota prevista"], "rows": body}],
        ),
    )


def _rota_da_loja(romaneio: TmsRomaneio, rotas: list[TmsRota]):
    dest = (romaneio.loja_destino or "").casefold()
    best = None
    for rota in rotas:
        code = (rota.loja_codigo or "").casefold()
        name = (rota.loja_nome or "").casefold()
        if (code and code in dest) or (name and name in dest) or (dest and name and dest in name):
            if best is None or rota.distancia_km < best.distancia_km:
                best = rota
    return best


def otimizar_romaneios(rows, rotas):
    """Ordena pelo cadastro de rotas (menor distância primeiro) quando não há chave do Google."""
    ranked = sorted(rows, key=lambda row: ((_rota_da_loja(row, rotas).distancia_km if _rota_da_loja(row, rotas) else 10**9), row.id))
    for index, row in enumerate(ranked, start=1):
        rota = _rota_da_loja(row, rotas)
        row.ordem_entrega = index
        if rota:
            row.distancia_rota_km = rota.distancia_km
            row.tempo_rota_minutos = rota.tempo_previsto_minutos
        row.save(update_fields=["ordem_entrega", "distancia_rota_km", "tempo_rota_minutos", "updated_at"])
    return ranked


@login_required
@require_http_methods(["GET", "POST"])
def tms_rotas(request):
    blocked = _gate(request, "Rotas TMS")
    if blocked:
        return blocked
    notice = ""
    if request.method == "POST" and request.POST.get("action") == "criar":
        codigo = (request.POST.get("loja_codigo") or "").strip()
        nome = (request.POST.get("loja_nome") or "").strip()
        if codigo and nome:
            TmsRota.objects.create(
                cd_origem=current_cd_code(request)[:7],
                loja_codigo=codigo,
                loja_nome=nome,
                distancia_km=float(request.POST.get("distancia_km") or 0),
                tempo_previsto_minutos=int(float(request.POST.get("tempo_previsto_minutos") or 0)),
                endereco=(request.POST.get("endereco") or "").strip(),
                criado_por=username(request.user),
            )
            notice = "Rota gravada."
    rows = list(TmsRota.objects.filter(cd_origem__in=cd_codes(request) + [current_cd_code(request)]))
    return render_screen(
        request,
        blank_screen(
            title="Rotas TMS",
            eyebrow="TMS",
            lead="Distância e tempo previstos por loja. A otimização usa este cadastro para ordenar as entregas do dia.",
            notice=notice,
            actions=[{"href": "/tms/rotas/otimizar/", "label": "Otimizar o dia"}],
            cards=[{"label": "Rotas ativas", "value": sum(1 for row in rows if row.ativa), "hint": f"{len(rows)} no CD"}],
            form={
                "action": "/tms/rotas/",
                "title": "Nova rota",
                "submit": "Salvar rota",
                "hidden": {"action": "criar"},
                "fields": [
                    {"name": "loja_codigo", "label": "Código da loja", "type": "text", "value": "", "required": True},
                    {"name": "loja_nome", "label": "Nome", "type": "text", "value": "", "required": True},
                    {"name": "distancia_km", "label": "Distância (km)", "type": "number", "value": "0"},
                    {"name": "tempo_previsto_minutos", "label": "Tempo (min)", "type": "number", "value": "0"},
                    {"name": "endereco", "label": "Endereço", "type": "text", "value": "", "wide": True},
                ],
            },
            tables=[
                {
                    "title": "Cadastro",
                    "lead": "",
                    "headers": ["CD", "Código", "Loja", "Km", "Minutos", "Ativa"],
                    "rows": [[cell(row.cd_origem), cell(row.loja_codigo), cell(row.loja_nome), cell(row.distancia_km), cell(row.tempo_previsto_minutos), cell("sim" if row.ativa else "não", "ok" if row.ativa else "danger")] for row in rows],
                }
            ],
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def tms_otimizar(request):
    blocked = _gate(request, "Otimizar rota")
    if blocked:
        return blocked
    rows = list(romaneios_qs(request).filter(data=today()).exclude(status__in=["cancelado", "finalizado"]))
    rotas = list(TmsRota.objects.filter(ativa=True))
    if request.method == "POST":
        from .fusao_motor import RotaErro, chave_google, otimizar_google

        chave = chave_google()
        if chave:
            try:
                otimizar_google(rows, current_cd_code(request), chave)
                return redirect("/tms/executar/")
            except RotaErro:
                pass
        otimizar_romaneios(rows, rotas)
        return redirect("/tms/executar/")
    return render_screen(
        request,
        blank_screen(
            title="Otimizar rota do dia",
            eyebrow="TMS",
            lead="Ordena as ordens de hoje pela menor distância cadastrada em Rotas. Sem chave do Google, o cálculo fica na base interna.",
            cards=[{"label": "Ordens de hoje", "value": len(rows), "hint": "serão reordenadas"}, {"label": "Rotas", "value": len(rotas), "hint": "cadastro usado"}],
            form={"action": "/tms/rotas/otimizar/", "title": "Aplicar ordem", "submit": "Otimizar e abrir OT", "fields": []},
            tables=[_romaneio_table(rows)],
        ),
    )


def validar_carga(placa: str, itens: list[dict]) -> dict:
    vehicle = TmsVeiculo.objects.filter(placa__iexact=(placa or "").strip(), ativo=True).first()
    if not vehicle:
        return {"ok": False, "error": "Veículo não encontrado para esta placa."}
    peso = volume = pallets = 0.0
    linhas = []
    if not itens:
        return {"ok": False, "error": "Informe ao menos um produto."}
    for item in itens:
        ean = str(item.get("ean") or "").strip()
        qtd = float(item.get("qtd_caixas") or 0)
        produto = TmsProdutoCapacidade.objects.filter(ean=ean).first()
        if not produto:
            return {"ok": False, "error": f"EAN {ean or '-'} não encontrado na base interna de peso."}
        if qtd <= 0:
            return {"ok": False, "error": f"Informe a quantidade de fardos do EAN {ean}."}
        peso += produto.peso_kg_fardo * qtd
        volume += produto.volume_m3_fardo * qtd
        pallets += produto.paletes_por_fardo * qtd
        linhas.append({"ean": ean, "descricao": produto.descricao, "qtd": qtd})
    falhas = []
    if peso > vehicle.capacidade_max_kg:
        falhas.append("peso")
    if volume > vehicle.capacidade_max_m3:
        falhas.append("cubagem")
    if pallets > vehicle.capacidade_max_pallets:
        falhas.append("pallets")
    payload = {
        "ok": not falhas,
        "placa": vehicle.placa,
        "linhas": linhas,
        "totals": {"pesoTotalKg": round(peso, 2), "volumeTotalM3": round(volume, 3), "palletsTotal": round(pallets, 2)},
        "vehicle": {"maxKg": vehicle.capacidade_max_kg, "maxM3": vehicle.capacidade_max_m3, "maxPallets": vehicle.capacidade_max_pallets},
        "remaining": {
            "kg": round(vehicle.capacidade_max_kg - peso, 2),
            "m3": round(vehicle.capacidade_max_m3 - volume, 3),
            "pallets": round(vehicle.capacidade_max_pallets - pallets, 2),
        },
    }
    if falhas:
        payload["error"] = f"Carga bloqueada: limite de {', '.join(falhas)} excedido."
    return payload


def _itens_from_request(request):
    if request.content_type and "json" in request.content_type:
        try:
            payload = json.loads(request.body.decode() or "{}")
        except json.JSONDecodeError:
            payload = {}
        return payload.get("placa") or "", payload.get("produtos") or []
    eans = request.POST.getlist("ean")
    qtds = request.POST.getlist("qtd_caixas")
    itens = [{"ean": ean, "qtd_caixas": qtd} for ean, qtd in zip(eans, qtds) if str(ean).strip()]
    return request.POST.get("placa") or "", itens


@login_required
@require_http_methods(["GET", "POST"])
def tms_capacidade(request):
    blocked = _gate(request, "Validar capacidade")
    if blocked:
        return blocked
    result = None
    if request.method == "POST":
        placa, itens = _itens_from_request(request)
        result = validar_carga(placa, itens)
    produtos = list(TmsProdutoCapacidade.objects.order_by("descricao"))
    notice = ""
    error = ""
    if result:
        if result.get("ok"):
            totals = result["totals"]
            notice = f"Carga liberada: {totals['pesoTotalKg']} kg e {totals['volumeTotalM3']} m³. Saldo: {result['remaining']['kg']} kg, {result['remaining']['m3']} m³ e {result['remaining']['pallets']} pal."
        else:
            error = result.get("error") or "Não foi possível validar a carga."
    return render_screen(
        request,
        blank_screen(
            title="Validar a carga",
            eyebrow="TMS / Capacidade",
            lead="Informe a placa e os fardos. O peso sai da base interna, sem digitação manual de kg ou cubagem.",
            notice=notice,
            error=error,
            actions=[{"href": "/recebimento/produtos/pesos/", "label": "Base de produtos"}, {"href": "/tms/romaneios/rascunhos/", "label": "Cargas prontas"}],
            form={
                "action": "/tms/viagens/capacidade/",
                "title": "Motor de capacidade",
                "submit": "Validar carga",
                "fields": [
                    {"name": "placa", "label": "Placa", "type": "text", "value": request.POST.get("placa", ""), "required": True},
                    {"name": "ean", "label": "EAN / GTIN", "type": "text", "value": "", "required": True},
                    {"name": "qtd_caixas", "label": "Fardos", "type": "number", "value": "1", "required": True},
                ],
            },
            tables=[
                {
                    "title": "Base interna",
                    "lead": "EAN homologado, peso e cubagem por fardo.",
                    "headers": ["EAN", "Produto", "Kg/fardo", "m³/fardo"],
                    "rows": [[cell(row.ean), cell(row.descricao), cell(row.peso_kg_fardo), cell(row.volume_m3_fardo)] for row in produtos],
                }
            ],
        ),
    )


@login_required
@require_http_methods(["POST"])
def api_validar_capacidade(request):
    if not allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão."}, status=403)
    placa, itens = _itens_from_request(request)
    return JsonResponse(validar_carga(placa, itens), json_dumps_params={"ensure_ascii": False})


def _live_payload(request):
    codes = cd_codes(request)
    trips = list(
        TmsViagem.objects.exclude(status__in=["finalizada", "cancelada"])
        .exclude(status_logistico="retornou_base")
        .filter(Q(origem_cd__in=codes) | Q(cd_atual__in=codes))
        .order_by("id")[:40]
    )
    zones = [
        {"id": zone.id, "name": zone.nome, "type": zone.tipo, "latitude": zone.latitude, "longitude": zone.longitude, "radiusMeters": zone.raio_metros}
        for zone in TmsGeofence.objects.filter(ativo=True)
    ]
    payload_trips = []
    for trip in trips:
        point = trip.telemetrias.order_by("-ocorreu_em").first()
        stop = trip.paradas.order_by("ordem").first()
        vehicle = TmsVeiculo.objects.filter(Q(id=trip.veiculo_id) | Q(placa__iexact=trip.veiculo_id)).first()
        payload_trips.append(
            {
                "id": trip.id,
                "status": trip.status_logistico or trip.status,
                "driver": {"name": trip.motorista_nome or ""},
                "vehicle": {"plate": (vehicle.placa if vehicle else "") or trip.veiculo_id},
                "nextStop": {"name": f"{trip.loja_codigo} {trip.loja_nome}".strip()} if stop or trip.loja_nome else None,
                "lastPosition": {"lat": point.latitude, "lng": point.longitude, "timestamp": point.ocorreu_em.isoformat()} if point else None,
            }
        )
    return {"ok": True, "trips": payload_trips, "geofences": zones}


@login_required
def tms_mapa(request):
    blocked = _gate(request, "Torre de Controle")
    if blocked:
        return blocked
    payload = _live_payload(request)
    return render_screen(
        request,
        blank_screen(
            title="Torre de Controle",
            eyebrow="TMS / Monitoramento",
            lead="Posição mais recente dos veículos em operação e cercas virtuais da Rede Krill.",
            actions=[{"href": "/tms/zero-toque/", "label": "Zero Toque"}, {"href": "/tms/acompanhamento/", "label": "Acompanhamento"}, {"href": "/tms/torre-controle/", "label": "Torre live"}],
            show_map=True,
            map_trips=payload["trips"],
            map_zones=payload["geofences"],
            cards=[
                {"label": "Viagens", "value": len(payload["trips"]), "hint": "em operação"},
                {"label": "Cercas", "value": len(payload["geofences"]), "hint": "CD, loja e checkpoint"},
            ],
            tables=[
                {
                    "title": "Viagens em campo",
                    "lead": "A lista vale mesmo se o mapa externo não carregar.",
                    "headers": ["Placa", "Motorista", "Situação", "Próxima", "Sinal"],
                    "rows": [
                        [
                            cell(trip["vehicle"]["plate"] or "-"),
                            cell(trip["driver"]["name"] or "-"),
                            cell(trip["status"]),
                            cell((trip.get("nextStop") or {}).get("name") or "-"),
                            cell("com sinal" if trip.get("lastPosition") else "sem sinal", "ok" if trip.get("lastPosition") else "warning"),
                        ]
                        for trip in payload["trips"]
                    ],
                }
            ],
        ),
    )


@login_required
def tms_torre(request):
    return tms_mapa(request)


@login_required
def api_telemetry_live(request):
    if not allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão."}, status=403)
    return JsonResponse(_live_payload(request), json_dumps_params={"ensure_ascii": False})


@login_required
@require_http_methods(["GET", "POST"])
def tms_cercas(request):
    blocked = _gate(request, "Cercas virtuais")
    if blocked:
        return blocked
    notice = ""
    if request.method == "POST":
        nome = (request.POST.get("nome") or "").strip()
        ident = (request.POST.get("id") or "").strip() or f"geo_{timezone.now().strftime('%H%M%S')}"
        if nome:
            TmsGeofence.objects.update_or_create(
                id=ident[:80],
                defaults={
                    "nome": nome,
                    "tipo": request.POST.get("tipo") or "LOJA",
                    "latitude": float(request.POST.get("latitude") or 0),
                    "longitude": float(request.POST.get("longitude") or 0),
                    "raio_metros": max(50, min(2000, int(float(request.POST.get("raio_metros") or 300)))),
                    "ativo": True,
                },
            )
            notice = "Cerca gravada."
    rows = list(TmsGeofence.objects.all())
    return render_screen(
        request,
        blank_screen(
            title="Cercas virtuais",
            eyebrow="TMS / Geofencing",
            lead="CD, loja e checkpoint usados pela telemetria e pela torre.",
            notice=notice,
            actions=[{"href": "/tms/geofencing/dispositivos/", "label": "Dispositivos"}, {"href": "/tms/monitoramento/mapa/", "label": "Mapa"}],
            form={
                "action": "/tms/geofencing/cercas/",
                "title": "Nova cerca",
                "submit": "Salvar cerca",
                "fields": [
                    {"name": "id", "label": "Identificador", "type": "text", "value": ""},
                    {"name": "nome", "label": "Nome", "type": "text", "value": "", "required": True},
                    {"name": "tipo", "label": "Tipo", "type": "select", "value": "LOJA", "options": [("CD", "CD"), ("LOJA", "Loja"), ("CHECKPOINT", "Checkpoint")]},
                    {"name": "latitude", "label": "Latitude", "type": "number", "value": "-24.00"},
                    {"name": "longitude", "label": "Longitude", "type": "number", "value": "-46.40"},
                    {"name": "raio_metros", "label": "Raio (m)", "type": "number", "value": "300"},
                ],
            },
            tables=[
                {
                    "title": "Cercas",
                    "lead": "",
                    "headers": ["Id", "Nome", "Tipo", "Lat", "Lng", "Raio"],
                    "rows": [[cell(row.id), cell(row.nome), cell(row.tipo), cell(row.latitude), cell(row.longitude), cell(row.raio_metros)] for row in rows],
                }
            ],
        ),
    )


@login_required
def tms_dispositivos(request):
    blocked = _gate(request, "Dispositivos")
    if blocked:
        return blocked
    veiculos = list(TmsVeiculo.objects.filter(ativo=True))
    rows = []
    for veiculo in veiculos:
        point = TmsTelemetria.objects.filter(placa__iexact=veiculo.placa).order_by("-ocorreu_em").first()
        rows.append([cell(veiculo.placa), cell(veiculo.id), cell(fmt_dt(point.ocorreu_em) if point else "sem sinal", "ok" if point else "warning")])
    return render_screen(
        request,
        blank_screen(
            title="Dispositivos de telemetria",
            eyebrow="TMS / Geofencing",
            lead="Veículos homologados e o último sinal recebido.",
            actions=[{"href": "/tms/geofencing/cercas/", "label": "Cercas"}],
            tables=[{"title": "Frota rastreada", "lead": "", "headers": ["Placa", "Id", "Último sinal"], "rows": rows}],
        ),
    )


@login_required
def tms_zero_toque(request):
    blocked = _gate(request, "Zero Toque")
    if blocked:
        return blocked
    eventos = list(TmsViagem.objects.filter(origem_cd__in=cd_codes(request)).prefetch_related("eventos_geofence")[:40])
    rows = []
    for trip in eventos:
        for event in list(trip.eventos_geofence.all())[:4]:
            rows.append([cell(trip.id), cell(trip.motorista_nome or "-"), cell(event.geofence_tipo), cell(event.acao), cell(fmt_dt(event.ocorreu_em))])
    return render_screen(
        request,
        blank_screen(
            title="Central Zero Toque",
            eyebrow="TMS",
            lead="Eventos de cerca confirmados sem intervenção manual.",
            cards=[{"label": "Eventos", "value": len(rows), "hint": "últimos lidos"}],
            tables=[{"title": "Eventos", "lead": "", "headers": ["Viagem", "Motorista", "Cerca", "Ação", "Quando"], "rows": rows}],
        ),
    )


@login_required
def tms_checklist(request):
    blocked = _gate(request, "Checklist de viagem")
    if blocked:
        return blocked
    rows = list(romaneios_qs(request).exclude(status__in=["cancelado", "finalizado"]).order_by("-data")[:80])
    body = []
    for row in rows:
        faltas = [name for name, ok in (("placa", row.placa), ("motorista", row.motorista), ("lacre", row.lacres), ("loja", row.loja_destino)) if not ok]
        body.append([cell(row.numero_romaneio, href=f"/tms/romaneios/{row.id}/"), cell(row.loja_destino or "-"), cell(", ".join(faltas) if faltas else "completo", "danger" if faltas else "ok")])
    return render_screen(
        request,
        blank_screen(
            title="Checklist de viagem",
            eyebrow="TMS",
            lead="Placa, motorista, lacre e loja precisam estar preenchidos antes da saída.",
            tables=[{"title": "Pendências do checklist", "lead": "", "headers": ["Romaneio", "Loja", "Situação"], "rows": body}],
        ),
    )


@login_required
def tms_motorista(request):
    blocked = _gate(request, "Painel do motorista")
    if blocked:
        return blocked
    nome = username(request.user)
    rows = list(romaneios_qs(request).filter(Q(motorista__icontains=nome) | Q(motorista="")).exclude(status__in=["cancelado", "finalizado"]).order_by("-data")[:40])
    if not rows:
        rows = list(romaneios_qs(request).exclude(status__in=["cancelado", "finalizado"]).order_by("-data")[:40])
    return render_screen(
        request,
        blank_screen(
            title="Painel do motorista",
            eyebrow="TMS",
            lead="Ordens em aberto. Quando o login coincide com o motorista do romaneio, a lista fica restrita a ele.",
            tables=[_romaneio_table(rows)],
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def tms_transferencias(request):
    blocked = _gate(request, "Transferências")
    if blocked:
        return blocked
    if request.method == "POST":
        destino = (request.POST.get("loja_destino") or "").strip()
        if destino:
            numero = f"TR-{timezone.now().strftime('%H%M%S')}"
            TmsRomaneio.objects.create(
                numero_romaneio=numero,
                data=today(),
                cd_origem=(request.POST.get("cd_origem") or current_cd_code(request))[:7],
                loja_destino=destino,
                total_paletes=int(float(request.POST.get("total_paletes") or 0)),
                status="rascunho",
                observacoes=f"Transferência para CD/loja {destino}",
                criado_por=username(request.user),
            )
            return redirect("/tms/romaneios/")
    rows = list(romaneios_qs(request).filter(observacoes__icontains="Transferência").order_by("-id")[:40])
    return render_screen(
        request,
        blank_screen(
            title="Transferências entre CDs",
            eyebrow="TMS",
            lead="Uma transferência vira rascunho de romaneio no CD de origem.",
            form={
                "action": "/tms/transferencias/",
                "title": "Nova transferência",
                "submit": "Gerar rascunho",
                "fields": [
                    {"name": "cd_origem", "label": "CD origem", "type": "text", "value": current_cd_code(request)},
                    {"name": "loja_destino", "label": "Destino", "type": "text", "value": "", "required": True},
                    {"name": "total_paletes", "label": "Paletes", "type": "number", "value": "0"},
                ],
            },
            tables=[_romaneio_table(rows)],
        ),
    )


@login_required
def tms_recebimento(request):
    blocked = _gate(request, "Recebimento de transferência")
    if blocked:
        return blocked
    rows = list(romaneios_qs(request).filter(status__in=["entregue", "em_transporte"]).order_by("-data")[:80])
    return render_screen(
        request,
        blank_screen(
            title="Recebimento de transferência",
            eyebrow="TMS",
            lead="Cargas que já saíram e ainda precisam de confirmação na chegada.",
            tables=[_romaneio_table(rows)],
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def tms_vincular(request):
    blocked = _gate(request, "Vincular viagem")
    if blocked:
        return blocked
    notice = ""
    if request.method == "POST":
        row = romaneios_qs(request).filter(pk=request.POST.get("romaneio_id") or 0).first()
        if row and row.status in {"rascunho", "pendente_conferencia", "conferido", "aguardando_conferencia"}:
            row.placa = (request.POST.get("placa") or row.placa).strip().upper()
            row.motorista = (request.POST.get("motorista") or row.motorista).strip()
            row.atualizado_por = username(request.user)
            row.save(update_fields=["placa", "motorista", "atualizado_por", "updated_at"])
            notice = f"{row.numero_romaneio} vinculado a {row.placa or 'sem placa'}."
        else:
            notice = "Romaneio não encontrado ou já em transporte."
    options = [(str(row.id), f"{row.numero_romaneio} · {row.loja_destino}") for row in romaneios_qs(request).exclude(status__in=["finalizado", "cancelado", "em_transporte"])[:80]]
    return render_screen(
        request,
        blank_screen(
            title="Vincular motorista e veículo",
            eyebrow="TMS",
            lead="A placa e o motorista podem mudar antes do fechamento fiscal, sem alterar a NF-e.",
            notice=notice,
            form={
                "action": "/tms/viagens/vincular/",
                "title": "Vínculo",
                "submit": "Vincular",
                "fields": [
                    {"name": "romaneio_id", "label": "Romaneio", "type": "select", "value": "", "options": options},
                    {"name": "motorista", "label": "Motorista", "type": "text", "value": ""},
                    {"name": "placa", "label": "Placa", "type": "text", "value": ""},
                ],
            },
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def tms_reversa(request):
    blocked = _gate(request, "Logística reversa")
    if blocked:
        return blocked
    if request.method == "POST":
        row = romaneios_qs(request).filter(pk=request.POST.get("romaneio_id") or 0).first()
        if row:
            TmsDevolucao.objects.create(
                romaneio=row,
                data=today(),
                tipo=request.POST.get("tipo") or "parcial",
                motivo=(request.POST.get("motivo") or "").strip(),
                valor_devolvido=float(request.POST.get("valor_devolvido") or 0),
                descricao=(request.POST.get("descricao") or "").strip(),
                responsavel=username(request.user),
                criado_por=username(request.user),
            )
            return redirect("/tms/logistica-reversa/")
    rows = list(TmsDevolucao.objects.select_related("romaneio").order_by("-data", "-id")[:80])
    options = [(str(row.id), row.numero_romaneio) for row in romaneios_qs(request).order_by("-id")[:80]]
    return render_screen(
        request,
        blank_screen(
            title="Logística reversa",
            eyebrow="TMS",
            lead="Devoluções parciais ou totais ligadas a um romaneio.",
            form={
                "action": "/tms/logistica-reversa/",
                "title": "Nova devolução",
                "submit": "Registrar",
                "fields": [
                    {"name": "romaneio_id", "label": "Romaneio", "type": "select", "value": "", "options": options},
                    {"name": "tipo", "label": "Tipo", "type": "select", "value": "parcial", "options": [("parcial", "Parcial"), ("total", "Total")]},
                    {"name": "motivo", "label": "Motivo", "type": "text", "value": ""},
                    {"name": "valor_devolvido", "label": "Valor", "type": "number", "value": "0"},
                    {"name": "descricao", "label": "Descrição", "type": "textarea", "value": "", "wide": True},
                ],
            },
            tables=[
                {
                    "title": "Devoluções",
                    "lead": "",
                    "headers": ["Data", "Romaneio", "Tipo", "Motivo", "Valor", "Situação"],
                    "rows": [
                        [cell(row.data.strftime("%d/%m/%Y")), cell(row.romaneio.numero_romaneio), cell(row.tipo), cell(row.motivo or "-"), cell(brl(row.valor_devolvido)), status_cell(row.status, DIVERGENCIA_STATUS)]
                        for row in rows
                    ],
                }
            ],
        ),
    )


def _ops_screen(request, title, eyebrow, lead, model, specs, actions=None, extra_filter=None):
    blocked = _gate(request, title)
    if blocked:
        return blocked
    qs = operacional(model, request).order_by("-data", "-id")
    if extra_filter:
        qs = extra_filter(qs)
    rows = list(qs[:120])
    built = table_from(rows, specs)
    built["title"] = title
    return render_screen(
        request,
        blank_screen(title=title, eyebrow=eyebrow, lead=lead, actions=actions or [], cards=[{"label": "Registros", "value": len(rows), "hint": "CD ativo"}], tables=[built]),
    )


@login_required
def sem_saldo(request, tipo="normal"):
    if tipo == "avaria":
        return _ops_screen(
            request,
            "Sem saldo · avaria",
            "Operação",
            "Avarias do CD ativo. O lançamento completo continua no módulo de avarias.",
            Avaria,
            [("data", "Data"), ("produto", "Produto"), ("tipo", "Tipo"), ("caixas", "Caixas"), ("status", "Situação")],
            [{"href": "/modulo/avarias/", "label": "Abrir módulo"}],
        )
    return _ops_screen(
        request,
        "Sem saldo",
        "Operação",
        "Chamados de saldo do CD ativo.",
        ChamadoSaldo,
        [("data", "Data"), ("produto", "Produto"), ("endereco", "Endereço"), ("saldo_sistema", "Sistema"), ("saldo_fisico", "Físico"), ("status", "Situação")],
        [{"href": "/modulo/chamados_saldo/", "label": "Novo chamado"}, {"href": "/sem-saldo/exportar/", "label": "CSV"}],
    )


@login_required
def sem_saldo_export(request, tipo="normal"):
    blocked = _gate(request, "Sem saldo")
    if blocked:
        return blocked
    if tipo == "avaria":
        rows = operacional(Avaria, request)
        return csv_response("avarias.csv", ["data", "produto", "tipo", "status"], [[row.data, row.produto, row.tipo, row.status] for row in rows])
    rows = operacional(ChamadoSaldo, request)
    return csv_response("sem_saldo.csv", ["data", "produto", "sistema", "fisico", "status"], [[row.data, row.produto, row.saldo_sistema, row.saldo_fisico, row.status] for row in rows])


@login_required
def sem_saldo_busca(request):
    blocked = _gate(request, "Buscar produto")
    if blocked:
        return blocked
    q = (request.GET.get("q") or "").strip()
    produtos = operacional(ProdutoGtin, request)
    chamados = operacional(ChamadoSaldo, request)
    if q:
        produtos = produtos.filter(Q(gtin__icontains=q) | Q(descricao__icontains=q))
        chamados = chamados.filter(Q(gtin__icontains=q) | Q(produto__icontains=q))
    return render_screen(
        request,
        blank_screen(
            title="Buscar produto sem saldo",
            eyebrow="Operação",
            lead="Cruza o GTIN cadastrado com os chamados de saldo.",
            filters=[{"name": "q", "label": "GTIN ou nome", "type": "text", "value": q}],
            tables=[
                table_from(produtos[:40], [("gtin", "GTIN"), ("descricao", "Produto"), ("endereco", "Endereço")]),
                table_from(chamados[:40], [("produto", "Chamado"), ("status", "Situação"), ("numero_glpi", "GLPI")]),
            ],
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def recebimento_operacional(request, portaria=False):
    title = "Portaria de recebimento" if portaria else "Recebimento operacional"
    model = RecebimentoAgenda if portaria else Recebimento
    specs = (
        [("data", "Data"), ("empresa", "Empresa"), ("tipo_caminhao", "Veículo"), ("paletes", "Paletes"), ("periodo", "Período")]
        if portaria
        else [("data", "Data"), ("fornecedor", "Fornecedor"), ("nota_fiscal", "NF"), ("produto", "Produto"), ("paletes", "Paletes"), ("motorista", "Motorista")]
    )
    if request.method == "POST" and not portaria:
        blocked = _gate(request, title)
        if blocked:
            return blocked
        Recebimento.objects.create(
            cd_unidade=current_cd_code(request)[:3] or "806",
            data=parse_date(request.POST.get("data"), today()),
            fornecedor=(request.POST.get("fornecedor") or "").strip() or "Fornecedor",
            nota_fiscal=(request.POST.get("nota_fiscal") or "").strip(),
            produto=(request.POST.get("produto") or "").strip() or "Carga",
            quantidade=float(request.POST.get("quantidade") or 0),
            paletes=int(float(request.POST.get("paletes") or 0)),
            motorista=(request.POST.get("motorista") or "").strip(),
            criado_por=request.user if request.user.is_authenticated else None,
        )
        return redirect("/recebimento-operacional/")
    actions = [
        {"href": "/recebimento-operacional/novo/", "label": "Novo"},
        {"href": "/recebimento-operacional/portaria/", "label": "Portaria"},
        {"href": "/recebimento-operacional/divergencias/", "label": "Divergências"},
        {"href": "/modulo/recebimentos/", "label": "Módulo completo"},
    ]
    return _ops_screen(request, title, "Recebimento", "Fila operacional do CD ativo.", model, specs, actions)


@login_required
def recebimento_novo(request):
    blocked = _gate(request, "Novo recebimento")
    if blocked:
        return blocked
    if request.method == "POST":
        return recebimento_operacional(request, portaria=False)
    return render_screen(
        request,
        blank_screen(
            title="Novo recebimento",
            eyebrow="Recebimento",
            lead="Grava a carga no recebimento do CD ativo.",
            form={
                "action": "/recebimento-operacional/novo/",
                "title": "Carga",
                "submit": "Salvar",
                "fields": [
                    {"name": "data", "label": "Data", "type": "date", "value": today().isoformat()},
                    {"name": "fornecedor", "label": "Fornecedor", "type": "text", "value": "", "required": True},
                    {"name": "nota_fiscal", "label": "Nota fiscal", "type": "text", "value": ""},
                    {"name": "produto", "label": "Produto", "type": "text", "value": ""},
                    {"name": "quantidade", "label": "Quantidade", "type": "number", "value": "0"},
                    {"name": "paletes", "label": "Paletes", "type": "number", "value": "0"},
                    {"name": "motorista", "label": "Motorista", "type": "text", "value": ""},
                ],
            },
        ),
    )


@login_required
def recebimento_divergencias(request):
    return _ops_screen(
        request,
        "Divergências do recebimento",
        "Recebimento",
        "Conferências com diferença entre o esperado e o contado.",
        Conferencia,
        [("data", "Data"), ("produto", "Produto"), ("qtd_esperada", "Esperado"), ("qtd_conferida", "Conferido"), ("status", "Situação")],
        [{"href": "/modulo/conferencias/", "label": "Módulo"}],
    )


@login_required
def recebimento_export(request):
    blocked = _gate(request, "Recebimento")
    if blocked:
        return blocked
    rows = operacional(Recebimento, request)
    return csv_response("recebimento.csv", ["data", "fornecedor", "nf", "produto", "paletes"], [[row.data, row.fornecedor, row.nota_fiscal, row.produto, row.paletes] for row in rows])


@login_required
def recebimento_relatorio(request):
    blocked = _gate(request, "Relatório de recebimento")
    if blocked:
        return blocked
    rows = list(operacional(Recebimento, request)[:200])
    paletes = sum(row.paletes or 0 for row in rows)
    return render_screen(
        request,
        blank_screen(
            title="Relatório de recebimento",
            eyebrow="Recebimento",
            lead="Fechamento das cargas registradas no CD.",
            cards=[{"label": "Cargas", "value": len(rows), "hint": "no recorte"}, {"label": "Paletes", "value": paletes, "hint": "somados"}],
            tables=[table_from(rows, [("data", "Data"), ("fornecedor", "Fornecedor"), ("nota_fiscal", "NF"), ("paletes", "Paletes"), ("valor", "Valor")])],
        ),
    )


@login_required
def recebimento_descarga(request):
    blocked = _gate(request, "Descarga")
    if blocked:
        return blocked
    agenda = list(operacional(RecebimentoAgenda, request)[:80])
    return render_screen(
        request,
        blank_screen(
            title="Parâmetros e descarga",
            eyebrow="Recebimento",
            lead="Agenda prevista e o volume de paletes que entra pela portaria.",
            cards=[{"label": "Agendamentos", "value": len(agenda), "hint": "CD ativo"}],
            tables=[table_from(agenda, [("data", "Data"), ("empresa", "Empresa"), ("tipo_caminhao", "Veículo"), ("paletes", "Paletes"), ("periodo", "Período")])],
        ),
    )


@login_required
def recebimento_whatsapp(request):
    blocked = _gate(request, "WhatsApp do recebimento")
    if blocked:
        return blocked
    rows = []
    for item in operacional(Recebimento, request).order_by("-data", "-id")[:40]:
        texto = f"Recebimento {item.nota_fiscal or item.produto}: {item.paletes} paletes de {item.fornecedor}."
        rows.append([cell(item.fornecedor), cell(item.nota_fiscal or "-"), cell(texto), cell("Abrir WhatsApp", href=f"https://wa.me/?text={texto.replace(' ', '%20')}")])
    return render_screen(
        request,
        blank_screen(
            title="WhatsApp do recebimento",
            eyebrow="Recebimento",
            lead="Mensagem pronta a partir da carga registrada.",
            tables=[{"title": "Mensagens", "lead": "", "headers": ["Fornecedor", "NF", "Texto", "Ação"], "rows": rows}],
        ),
    )


@login_required
def recebimento_produtos(request):
    return _ops_screen(
        request,
        "Produtos do recebimento",
        "Recebimento",
        "GTIN cadastrado no CD.",
        ProdutoGtin,
        [("gtin", "GTIN"), ("descricao", "Produto"), ("categoria", "Categoria"), ("endereco", "Endereço")],
        [{"href": "/modulo/gtins/", "label": "Módulo"}, {"href": "/recebimento/produtos/pesos/", "label": "Pesos"}],
    )


@login_required
@require_http_methods(["GET", "POST"])
def recebimento_produto_novo(request):
    blocked = _gate(request, "Novo produto")
    if blocked:
        return blocked
    if request.method == "POST":
        gtin = (request.POST.get("gtin") or "").strip()
        descricao = (request.POST.get("descricao") or "").strip()
        if gtin and descricao:
            ProdutoGtin.objects.get_or_create(
                cd_unidade=current_cd_code(request)[:3] or "806",
                gtin=gtin,
                defaults={"descricao": descricao, "data": today(), "criado_por": request.user},
            )
        return redirect("/recebimento-produtos/")
    return render_screen(
        request,
        blank_screen(
            title="Novo produto",
            eyebrow="Recebimento",
            form={
                "action": "/recebimento-produtos/novo/",
                "title": "GTIN",
                "submit": "Salvar",
                "fields": [
                    {"name": "gtin", "label": "GTIN", "type": "text", "value": "", "required": True},
                    {"name": "descricao", "label": "Descrição", "type": "text", "value": "", "required": True},
                ],
            },
        ),
    )


@login_required
def recebimento_produtos_csv(request):
    blocked = _gate(request, "Produtos")
    if blocked:
        return blocked
    rows = operacional(ProdutoGtin, request)
    return csv_response("produtos.csv", ["gtin", "descricao", "endereco"], [[row.gtin, row.descricao, row.endereco] for row in rows])


@login_required
@require_http_methods(["GET", "POST"])
def importar_nf(request):
    blocked = _gate(request, "Importar NF")
    if blocked:
        return blocked
    notice = ""
    if request.method == "POST":
        Recebimento.objects.create(
            cd_unidade=current_cd_code(request)[:3] or "806",
            data=today(),
            fornecedor=(request.POST.get("fornecedor") or "NF importada").strip(),
            nota_fiscal=(request.POST.get("nota_fiscal") or "").strip(),
            produto=(request.POST.get("produto") or "Item da NF").strip(),
            quantidade=float(request.POST.get("quantidade") or 0),
            paletes=int(float(request.POST.get("paletes") or 0)),
            criado_por=request.user,
        )
        notice = "Nota lançada no recebimento."
    return render_screen(
        request,
        blank_screen(
            title="Importar nota fiscal",
            eyebrow="Recebimento",
            lead="Lança a NF no recebimento do CD. O XML completo continua no módulo de importação.",
            notice=notice,
            actions=[{"href": "/importar/", "label": "Importar planilha"}],
            form={
                "action": "/recebimento-produtos/importar-nf/",
                "title": "Dados da nota",
                "submit": "Lançar",
                "fields": [
                    {"name": "nota_fiscal", "label": "Número", "type": "text", "value": ""},
                    {"name": "fornecedor", "label": "Fornecedor", "type": "text", "value": ""},
                    {"name": "produto", "label": "Produto", "type": "text", "value": ""},
                    {"name": "quantidade", "label": "Quantidade", "type": "number", "value": "0"},
                    {"name": "paletes", "label": "Paletes", "type": "number", "value": "0"},
                ],
            },
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def pesos_produtos(request):
    blocked = _gate(request, "Pesos")
    if blocked:
        return blocked
    if request.method == "POST":
        ean = (request.POST.get("ean") or "").strip()
        if ean:
            TmsProdutoCapacidade.objects.update_or_create(
                ean=ean,
                defaults={
                    "descricao": (request.POST.get("descricao") or ean).strip(),
                    "peso_kg_fardo": float(request.POST.get("peso_kg_fardo") or 0),
                    "volume_m3_fardo": float(request.POST.get("volume_m3_fardo") or 0),
                },
            )
        return redirect("/recebimento/produtos/pesos/")
    rows = list(TmsProdutoCapacidade.objects.order_by("descricao"))
    return render_screen(
        request,
        blank_screen(
            title="Base de peso e cubagem",
            eyebrow="Capacidade",
            lead="Esta base alimenta a validação de carga. Sem ela o EAN não libera a viagem.",
            form={
                "action": "/recebimento/produtos/pesos/",
                "title": "Homologar EAN",
                "submit": "Salvar",
                "fields": [
                    {"name": "ean", "label": "EAN", "type": "text", "value": "", "required": True},
                    {"name": "descricao", "label": "Descrição", "type": "text", "value": ""},
                    {"name": "peso_kg_fardo", "label": "Kg por fardo", "type": "number", "value": "0"},
                    {"name": "volume_m3_fardo", "label": "m³ por fardo", "type": "number", "value": "0"},
                ],
            },
            tables=[{"title": "Homologados", "lead": "", "headers": ["EAN", "Produto", "Kg", "m³"], "rows": [[cell(row.ean), cell(row.descricao), cell(row.peso_kg_fardo), cell(row.volume_m3_fardo)] for row in rows]}],
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def vale_palete(request):
    blocked = _gate(request, "Vale-palete")
    if blocked:
        return blocked
    if request.method == "POST":
        PaleteRedeMovimentacao.objects.create(
            cd_unidade=current_cd_code(request)[:3] or "806",
            data=today(),
            operacao=request.POST.get("operacao") or "reserva",
            tipo_palete=request.POST.get("tipo_palete") or "pbr",
            quantidade=int(float(request.POST.get("quantidade") or 0)),
            destino_nome=(request.POST.get("destino_nome") or "").strip(),
            solicitante=username(request.user),
            status="aberto",
            criado_por=request.user,
        )
        return redirect("/vale-palete/")
    movimentos = list(operacional(PaleteRedeMovimentacao, request).order_by("-data", "-id")[:80])
    return render_screen(
        request,
        blank_screen(
            title="Vale-palete",
            eyebrow="Paletes",
            lead="Movimentos de pallet da rede no CD ativo.",
            actions=[{"href": "/vale-palete/conta-corrente/", "label": "Conta-corrente"}, {"href": "/modulo/paletes_rede/", "label": "Módulo"}],
            tables=[table_from(movimentos, [("data", "Data"), ("operacao", "Operação"), ("tipo_palete", "Tipo"), ("quantidade", "Qtd"), ("destino_nome", "Destino"), ("status", "Situação")])],
        ),
    )


@login_required
def vale_palete_novo(request):
    blocked = _gate(request, "Novo vale-palete")
    if blocked:
        return blocked
    if request.method == "POST":
        return vale_palete(request)
    return render_screen(
        request,
        blank_screen(
            title="Novo vale-palete",
            eyebrow="Paletes",
            form={
                "action": "/vale-palete/novo/",
                "title": "Movimento",
                "submit": "Lançar",
                "fields": [
                    {"name": "operacao", "label": "Operação", "type": "select", "value": "reserva", "options": [("reserva", "Reserva"), ("transferencia", "Transferência"), ("retirada", "Retirada"), ("ajuste", "Ajuste")]},
                    {"name": "tipo_palete", "label": "Tipo", "type": "select", "value": "pbr", "options": [("pbr", "PBR"), ("chep", "CHEP"), ("descartavel", "Descartável")]},
                    {"name": "quantidade", "label": "Quantidade", "type": "number", "value": "1"},
                    {"name": "destino_nome", "label": "Destino", "type": "text", "value": ""},
                ],
            },
        ),
    )


@login_required
def vale_conta(request):
    blocked = _gate(request, "Conta-corrente")
    if blocked:
        return blocked
    rows = list(PaleteRedeSaldo.objects.all()[:120])
    return render_screen(
        request,
        blank_screen(
            title="Conta-corrente de paletes",
            eyebrow="Paletes",
            lead="Saldo, quebrados, reservados e disponível.",
            cards=[{"label": "Locais", "value": len(rows), "hint": "saldos cadastrados"}],
            tables=[
                {
                    "title": "Saldos",
                    "lead": "",
                    "headers": ["Local", "Tipo", "Total", "Quebrados", "Reservados", "Disponível"],
                    "rows": [[cell(row.local_nome), cell(row.tipo_palete), cell(row.quantidade), cell(row.quebrados), cell(row.reservados), cell(row.disponivel)] for row in rows],
                }
            ],
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def wms_mapa(request):
    blocked = _gate(request, "Mapa do armazém")
    if blocked:
        return blocked
    if request.method == "POST":
        pos = WmsPosicao.objects.filter(pk=request.POST.get("position_id") or 0, cd_codigo__in=cd_codes(request)).first()
        if pos:
            status = request.POST.get("status") or "LIVRE"
            pos.status = status if status in {"LIVRE", "OCUPADO", "BLOQUEADO"} else "LIVRE"
            if pos.status == "OCUPADO":
                pos.produto_ean = (request.POST.get("produto_ean") or "").strip()
                pos.produto_nome = (request.POST.get("produto_nome") or pos.produto_ean).strip()
                pos.quantidade_paletes = float(request.POST.get("quantidade_paletes") or 0)
            else:
                pos.produto_ean = ""
                pos.produto_nome = ""
                pos.quantidade_paletes = 0
            pos.save()
        return redirect("/wms/mapa/")
    posicoes = list(WmsPosicao.objects.filter(cd_codigo__in=cd_codes(request)))
    streets = []
    for rua in sorted({item.rua for item in posicoes}):
        streets.append(
            {
                "rua": rua,
                "slots": [
                    {
                        "id": item.id,
                        "codigo": item.codigo,
                        "nivel": item.nivel,
                        "status": item.status,
                        "produto": item.produto_nome or item.produto_ean,
                        "paletes": item.quantidade_paletes,
                    }
                    for item in posicoes
                    if item.rua == rua
                ],
            }
        )
    return render_screen(
        request,
        blank_screen(
            title="Mapa do armazém",
            eyebrow="WMS / Slotting",
            lead="Ruas, níveis e ocupação de pallets do CD ativo.",
            cards=[
                {"label": "Posições", "value": len(posicoes), "hint": "na planta"},
                {"label": "Livres", "value": sum(1 for item in posicoes if item.status == "LIVRE"), "hint": "disponíveis"},
                {"label": "Ocupadas", "value": sum(1 for item in posicoes if item.status == "OCUPADO"), "hint": "com produto"},
                {"label": "Bloqueadas", "value": sum(1 for item in posicoes if item.status == "BLOQUEADO"), "hint": "indisponíveis"},
                {"label": "Pallets", "value": round(sum(item.quantidade_paletes or 0 for item in posicoes), 1), "hint": "alocados"},
            ],
            streets=streets,
        ),
    )


@login_required
@require_http_methods(["POST"])
def api_wms_alocar(request):
    if not allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão."}, status=403)
    try:
        payload = json.loads(request.body.decode() or "{}")
    except json.JSONDecodeError:
        payload = request.POST
    pos = WmsPosicao.objects.filter(pk=payload.get("positionId") or payload.get("position_id") or 0).first()
    if not pos:
        return JsonResponse({"ok": False, "error": "Posição não encontrada."}, status=404)
    status = payload.get("status") or "LIVRE"
    pos.status = status if status in {"LIVRE", "OCUPADO", "BLOQUEADO"} else "LIVRE"
    pos.produto_ean = "" if pos.status != "OCUPADO" else str(payload.get("productEan") or "")
    pos.quantidade_paletes = 0 if pos.status != "OCUPADO" else float(payload.get("pallets") or 0)
    pos.save()
    return JsonResponse({"ok": True, "position": {"id": pos.id, "status": pos.status, "produto_ean": pos.produto_ean, "quantidade_paletes": pos.quantidade_paletes}})


def _yms_minutes(start, end=None) -> int:
    end = end or timezone.now()
    return max(0, int((end - start).total_seconds() // 60))


@login_required
@require_http_methods(["GET", "POST"])
def yms_patio(request):
    blocked = _gate(request, "Pátio")
    if blocked:
        return blocked
    cd = current_cd_code(request)[:7]
    notice = request.GET.get("ok") or ""
    error = request.GET.get("erro") or ""
    if request.method == "POST":
        action = request.POST.get("action") or "chegada"
        if action == "chegada":
            placa = (request.POST.get("placa") or "").strip().upper()
            transportadora = (request.POST.get("transportadora") or "").strip()
            motorista = (request.POST.get("motorista") or "").strip()
            if not placa or not transportadora or not motorista:
                return redirect(f"/yms/?erro={_quote('Informe placa, transportadora e motorista.')}")
            with transaction.atomic():
                dock = YmsDoca.objects.select_for_update().filter(cd_codigo=cd, ativa=True, status="livre").order_by("codigo").first()
                now = timezone.now()
                tipo = "carregamento" if request.POST.get("tipo_operacao") == "carregamento" else "descarregamento"
                mov = YmsMovimentacao.objects.create(
                    cd_codigo=cd,
                    placa=placa,
                    transportadora=transportadora,
                    motorista=motorista,
                    nota_fiscal=(request.POST.get("nota_fiscal") or "").strip(),
                    tipo_operacao=tipo,
                    status="aguardando_doca",
                    chegada_em=now,
                    criado_por=username(request.user),
                )
                if dock:
                    dock.status = "carregando" if tipo == "carregamento" else "descarregando"
                    dock.save(update_fields=["status"])
                    mov.doca = dock
                    mov.status = "em_doca"
                    mov.doca_atribuida_em = now
                    mov.save(update_fields=["doca", "status", "doca_atribuida_em"])
                    return redirect(f"/yms/?ok={_quote('Veículo alocado na ' + dock.codigo + '.')}")
            return redirect(f"/yms/?ok={_quote('Veículo registrado na fila.')}")
        mov = YmsMovimentacao.objects.filter(pk=request.POST.get("movimentacao_id") or 0, cd_codigo=cd).exclude(status__in=["finalizada", "cancelada"]).first()
        if not mov:
            return redirect(f"/yms/?erro={_quote('Movimentação não encontrada.')}")
        if action == "documentacao":
            mov.status = "aguardando_documentacao"
            mov.save(update_fields=["status"])
            if mov.doca_id:
                YmsDoca.objects.filter(pk=mov.doca_id).update(status="aguardando_documentacao")
        elif action == "finalizar":
            mov.status = "finalizada"
            mov.saida_em = timezone.now()
            mov.save(update_fields=["status", "saida_em"])
            if mov.doca_id:
                YmsDoca.objects.filter(pk=mov.doca_id).update(status="livre")
        elif action == "alocar" and not mov.doca_id:
            with transaction.atomic():
                dock = YmsDoca.objects.select_for_update().filter(cd_codigo=cd, ativa=True, status="livre").order_by("codigo").first()
                if not dock:
                    return redirect(f"/yms/?erro={_quote('Nenhuma doca livre neste momento.')}")
                dock.status = "carregando" if mov.tipo_operacao == "carregamento" else "descarregando"
                dock.save(update_fields=["status"])
                mov.doca = dock
                mov.status = "em_doca"
                mov.doca_atribuida_em = timezone.now()
                mov.save(update_fields=["doca", "status", "doca_atribuida_em"])
        return redirect(f"/yms/?ok={_quote('Movimentação atualizada.')}")
    docks = list(YmsDoca.objects.filter(cd_codigo=cd, ativa=True))
    ativos = list(YmsMovimentacao.objects.filter(cd_codigo=cd).exclude(status__in=["finalizada", "cancelada"]).select_related("doca"))
    encerrados = YmsMovimentacao.objects.filter(cd_codigo=cd, status="finalizada", saida_em__isnull=False)
    media = 0
    if encerrados.exists():
        media = round(sum(_yms_minutes(row.chegada_em, row.saida_em) for row in encerrados) / encerrados.count())
    waiting = sum(1 for row in ativos if row.status == "aguardando_doca")
    body = []
    for mov in ativos:
        forms = []
        if mov.status == "aguardando_doca":
            forms.append({"action": "/yms/", "fields": {"action": "alocar", "movimentacao_id": mov.id}, "label": "Alocar doca"})
        if mov.status == "em_doca":
            forms.append({"action": "/yms/", "fields": {"action": "documentacao", "movimentacao_id": mov.id}, "label": "Documentos"})
        forms.append({"action": "/yms/", "fields": {"action": "finalizar", "movimentacao_id": mov.id}, "label": "Liberar"})
        body.append(
            [
                cell(fmt_dt(mov.chegada_em)),
                cell(mov.placa),
                cell(f"{mov.transportadora} · {mov.motorista}"),
                cell(mov.nota_fiscal or "-"),
                cell("Carregamento" if mov.tipo_operacao == "carregamento" else "Descarregamento"),
                cell(mov.doca.codigo if mov.doca else "Fila"),
                cell(f"{_yms_minutes(mov.chegada_em)} min"),
                cell(mov.status, tone_for(mov.status)),
                cell("", forms=forms),
            ]
        )
    return render_screen(
        request,
        blank_screen(
            title="Pátio e docas",
            eyebrow=f"Expedição / CD {cd}",
            lead="Ocupação das docas e liberação dos veículos. A primeira doca livre é vinculada na chegada.",
            notice=notice,
            error=error,
            cards=[
                {"label": "Docas ativas", "value": len(docks), "hint": f"{sum(1 for dock in docks if dock.status == 'livre')} livre(s)"},
                {"label": "No pátio", "value": len(ativos), "hint": "em atendimento ou fila"},
                {"label": "Fila", "value": waiting, "hint": "aguardando alocação"},
                {"label": "Permanência média", "value": f"{media} min", "hint": f"{encerrados.count()} ciclo(s)"},
            ],
            docks=[{"codigo": dock.codigo, "nome": dock.nome or dock.codigo, "status": dock.status} for dock in docks],
            form={
                "action": "/yms/",
                "title": "Registro de chegada",
                "submit": "Registrar chegada",
                "hidden": {"action": "chegada"},
                "fields": [
                    {"name": "placa", "label": "Placa", "type": "text", "value": "", "required": True},
                    {"name": "transportadora", "label": "Transportadora", "type": "text", "value": "", "required": True},
                    {"name": "motorista", "label": "Motorista", "type": "text", "value": "", "required": True},
                    {"name": "nota_fiscal", "label": "Nota fiscal", "type": "text", "value": ""},
                    {"name": "tipo_operacao", "label": "Operação", "type": "select", "value": "descarregamento", "options": [("descarregamento", "Descarregamento"), ("carregamento", "Carregamento")]},
                ],
            },
            tables=[{"title": "Veículos em atendimento", "lead": "", "headers": ["Chegada", "Placa", "Transporte", "NF", "Operação", "Doca", "Permanência", "Situação", "Ações"], "rows": body}],
        ),
    )


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text)


@login_required
def dashboard_indicadores(request):
    blocked = _gate(request, "Indicadores")
    if blocked:
        return blocked
    days = int(request.GET.get("dias") or 7)
    if days not in {7, 30, 90}:
        days = 7
    start = today() - timedelta(days=days)
    faltas = operacional(ChamadoSaldo, request).filter(data__gte=start).count()
    ruptura = operacional(Avaria, request).filter(data__gte=start).count()
    funcionarios = operacional(PessoaTurno, request).filter(data__gte=start).count()
    unidades = operacional(Separacao, request).filter(data__gte=start).aggregate(total=Sum("unidades"))["total"] or 0
    romaneios = list(romaneios_qs(request).filter(data__gte=start))
    recebimentos = operacional(Recebimento, request).filter(data__gte=start)
    by_status: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for row in romaneios:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        key = row.data.strftime("%d/%m")
        by_day[key] = by_day.get(key, 0) + 1
    alertas = []
    for row in operacional(ChamadoSaldo, request).filter(status="aberto")[:5]:
        alertas.append([cell("Ruptura / saldo", "danger"), cell(row.produto), cell("sem saldo", href="/sem-saldo/")])
    for row in TmsDivergencia.objects.filter(status="aberta")[:5]:
        alertas.append([cell("Divergência TMS", "danger"), cell(row.descricao), cell("abrir", href="/tms/divergencias/")])
    atividades = [[cell(fmt_dt(row.criado_em)), cell(row.modulo or "-"), cell(row.detalhe or row.acao), cell(row.usuario_nome or "-")] for row in AuditLog.objects.order_by("-id")[:10]]
    return render_screen(
        request,
        blank_screen(
            title="Dashboards e indicadores",
            eyebrow="Resultados",
            lead="Indicadores consolidados do CD para priorizar a operação.",
            actions=[{"href": f"/dashboard/indicadores/exportar/?dias={days}", "label": "Exportar CSV"}, {"href": "/dashboard/rupturas/", "label": "Rupturas"}],
            filters=[{"name": "dias", "label": "Período", "type": "select", "value": str(days), "options": [("7", "7 dias"), ("30", "30 dias"), ("90", "90 dias")]}],
            cards=[
                {"label": "Pendências de saldo", "value": faltas, "hint": f"últimos {days} dias"},
                {"label": "Avarias", "value": ruptura, "hint": "no período"},
                {"label": "Pessoas no turno", "value": funcionarios, "hint": "lançamentos"},
                {"label": "Unidades separadas", "value": num(unidades), "hint": "no período"},
                {"label": "Romaneios", "value": len(romaneios), "hint": "no período"},
                {"label": "Recebimentos", "value": recebimentos.count(), "hint": "cargas"},
                {"label": "Paletes recebidos", "value": recebimentos.aggregate(total=Sum("paletes"))["total"] or 0, "hint": "somados"},
            ],
            tables=[
                {"title": "Romaneios por dia", "lead": "", "headers": ["Dia", "Total"], "rows": [[cell(day), cell(total)] for day, total in by_day.items()]},
                {"title": "Situação", "lead": "", "headers": ["Status", "Total"], "rows": [[status_cell(key, ROMANEIO_STATUS), cell(total)] for key, total in by_status.items()]},
                {"title": "Alertas", "lead": "", "headers": ["Tipo", "Detalhe", "Ação"], "rows": alertas},
                {"title": "Atividades recentes", "lead": "", "headers": ["Quando", "Módulo", "Ação", "Usuário"], "rows": atividades},
            ],
        ),
    )


@login_required
def dashboard_indicadores_export(request):
    blocked = _gate(request, "Indicadores")
    if blocked:
        return blocked
    days = int(request.GET.get("dias") or 7)
    start = today() - timedelta(days=days if days in {7, 30, 90} else 7)
    return csv_response(
        "gestao_cd_dashboard_indicadores.csv",
        ["indicador", "valor"],
        [
            ["sem_saldo", operacional(ChamadoSaldo, request).filter(data__gte=start).count()],
            ["avarias", operacional(Avaria, request).filter(data__gte=start).count()],
            ["romaneios", romaneios_qs(request).filter(data__gte=start).count()],
            ["recebimentos", operacional(Recebimento, request).filter(data__gte=start).count()],
        ],
    )


@login_required
def dashboard_rupturas(request):
    blocked = _gate(request, "Rupturas")
    if blocked:
        return blocked
    chamados = list(operacional(ChamadoSaldo, request).order_by("-data")[:80])
    avarias = list(operacional(Avaria, request).order_by("-data")[:80])
    return render_screen(
        request,
        blank_screen(
            title="Rupturas",
            eyebrow="Resultados",
            lead="Sem saldo e avaria no mesmo painel.",
            actions=[{"href": "/dashboard/rupturas/exportar/", "label": "CSV"}, {"href": "/sem-saldo/", "label": "Sem saldo"}],
            cards=[{"label": "Chamados", "value": len(chamados), "hint": "saldo"}, {"label": "Avarias", "value": len(avarias), "hint": "registro"}],
            tables=[
                table_from(chamados, [("data", "Data"), ("produto", "Produto"), ("diferenca", "Diferença"), ("status", "Situação")]),
                table_from(avarias, [("data", "Data"), ("produto", "Produto"), ("tipo", "Tipo"), ("status", "Situação")]),
            ],
        ),
    )


@login_required
def dashboard_rupturas_export(request):
    blocked = _gate(request, "Rupturas")
    if blocked:
        return blocked
    rows = operacional(ChamadoSaldo, request)
    return csv_response("rupturas.csv", ["produto", "diferenca", "status"], [[row.produto, row.diferenca, row.status] for row in rows])


@login_required
def separacao_controle(request):
    return _ops_screen(request, "Controle de separação", "Operação", "Ondas e unidades do CD.", Separacao, [("data", "Data"), ("loja", "Loja"), ("unidades", "Unidades"), ("paletes", "Paletes"), ("status", "Situação")], [{"href": "/modulo/separacao/", "label": "Módulo"}])


@login_required
def avaria_controle(request):
    return _ops_screen(request, "Controle de avaria", "Operação", "Avarias do CD.", Avaria, [("data", "Data"), ("produto", "Produto"), ("tipo", "Tipo"), ("caixas", "Caixas"), ("status", "Situação")], [{"href": "/modulo/avarias/", "label": "Módulo"}])


@login_required
def expedicao_controle(request):
    return _ops_screen(request, "Controle de expedição", "Expedição", "Saídas registradas no módulo de expedição do CD.", Expedicao, [("data", "Data"), ("loja", "Loja"), ("placa", "Placa"), ("motorista", "Motorista"), ("qtd_paletes", "Paletes"), ("status", "Situação")], [{"href": "/modulo/expedicao/", "label": "Módulo"}, {"href": "/tms/expedicao/", "label": "Central TMS"}])


@login_required
def produtos_cd(request):
    return recebimento_produtos(request)


@login_required
def veiculos_disponibilidade(request):
    return _ops_screen(request, "Disponibilidade de veículos", "Frota", "Status operacional da frota do CD.", VeiculoFrota, [("placa", "Placa"), ("motorista", "Motorista"), ("tipo_caminhao", "Tipo"), ("status_operacional", "Situação"), ("local_atual", "Local")], [{"href": "/modulo/veiculos_frota/", "label": "Módulo"}, {"href": "/frota/", "label": "Central da frota"}])


@login_required
def alertas(request):
    blocked = _gate(request, "Alertas")
    if blocked:
        return blocked
    pendencias = list(operacional(Pendencia, request).order_by("-data")[:40])
    divergencias = list(TmsDivergencia.objects.filter(status="aberta")[:40])
    return render_screen(
        request,
        blank_screen(
            title="Alertas",
            eyebrow="Operação",
            lead="Pendências do dia e divergências TMS ainda abertas.",
            cards=[{"label": "Pendências", "value": len(pendencias), "hint": "CD"}, {"label": "Divergências", "value": len(divergencias), "hint": "abertas"}],
            tables=[
                table_from(pendencias, [("data", "Data"), ("setor", "Setor"), ("descricao", "Descrição"), ("status", "Situação")]),
                {
                    "title": "Divergências TMS",
                    "lead": "",
                    "headers": ["Data", "Tipo", "Descrição"],
                    "rows": [[cell(row.data.strftime("%d/%m/%Y")), cell(DIVERGENCIA_TIPO.get(row.tipo, row.tipo)), cell(row.descricao)] for row in divergencias],
                },
            ],
        ),
    )


@login_required
def backup_tela(request):
    blocked = _gate(request, "Backup")
    if blocked:
        return blocked
    rows = list(BackupLog.objects.order_by("-criado_em")[:40])
    return render_screen(
        request,
        blank_screen(
            title="Backup",
            eyebrow="Sistema",
            lead="Últimas cópias registradas pelo motor do Assistente.",
            tables=[{"title": "Histórico", "lead": "", "headers": ["Quando", "Destino", "Status", "Arquivo"], "rows": [[cell(fmt_dt(row.criado_em)), cell(row.destino), cell(row.status, tone_for(row.status if row.status != "ok" else "ok")), cell(row.arquivo or "-")] for row in rows]}],
        ),
    )


@login_required
def auditoria_tela(request):
    blocked = _gate(request, "Auditoria")
    if blocked:
        return blocked
    rows = list(AuditLog.objects.order_by("-id")[:80])
    return render_screen(
        request,
        blank_screen(
            title="Auditoria",
            eyebrow="Sistema",
            lead="Trilha do que foi gravado no Gestão CD 2.",
            actions=[{"href": "/painel-master/", "label": "Painel master"}],
            tables=[{"title": "Eventos", "lead": "", "headers": ["Quando", "Usuário", "Ação", "Módulo", "Detalhe"], "rows": [[cell(fmt_dt(row.criado_em)), cell(row.usuario_nome or "-"), cell(row.acao), cell(row.modulo or "-"), cell(row.detalhe or "-")] for row in rows]}],
        ),
    )


@login_required
def ativos(request, tipo=""):
    mapping = {
        "coletores": "operacional",
        "computadores": "administrativo",
        "empilhadeiras": "empilhadeira",
        "paleteiras": "operacional",
    }
    extra = (lambda qs: qs.filter(tipo=mapping[tipo])) if tipo in mapping else None
    title = f"Ativos · {tipo}" if tipo else "Ativos"
    return _ops_screen(request, title, "Equipamentos", "Patrimônio do CD.", Equipamento, [("colaborador", "Colaborador"), ("tipo", "Tipo"), ("equipamento", "Equipamento"), ("patrimonio", "Patrimônio"), ("status", "Situação")], [{"href": "/modulo/equipamentos/", "label": "Módulo"}], extra)


@login_required
def buscar(request):
    blocked = _gate(request, "Busca")
    if blocked:
        return blocked
    q = (request.GET.get("q") or "").strip()
    romaneios = romaneios_qs(request)
    recebimentos = operacional(Recebimento, request)
    produtos = operacional(ProdutoGtin, request)
    if q:
        romaneios = romaneios.filter(Q(numero_romaneio__icontains=q) | Q(loja_destino__icontains=q) | Q(placa__icontains=q) | Q(motorista__icontains=q))
        recebimentos = recebimentos.filter(Q(fornecedor__icontains=q) | Q(nota_fiscal__icontains=q) | Q(produto__icontains=q))
        produtos = produtos.filter(Q(gtin__icontains=q) | Q(descricao__icontains=q))
    else:
        romaneios = romaneios.none()
        recebimentos = recebimentos.none()
        produtos = produtos.none()
    return render_screen(
        request,
        blank_screen(
            title="Busca",
            eyebrow="Gestão CD",
            lead="Procura romaneio, recebimento e GTIN no CD ativo.",
            filters=[{"name": "q", "label": "Termo", "type": "text", "value": q}],
            tables=[
                _romaneio_table(list(romaneios[:20])),
                table_from(recebimentos[:20], [("fornecedor", "Fornecedor"), ("nota_fiscal", "NF"), ("produto", "Produto")]),
                table_from(produtos[:20], [("gtin", "GTIN"), ("descricao", "Produto")]),
            ],
        ),
    )


@login_required
def qualidade_dados(request):
    blocked = _gate(request, "Qualidade dos dados")
    if blocked:
        return blocked
    sem_placa = romaneios_qs(request).filter(Q(placa="") | Q(motorista="")).exclude(status__in=["cancelado", "finalizado"]).count()
    sem_peso = TmsViagem.objects.filter(Q(origem_cd__in=cd_codes(request)) | Q(cd_atual__in=cd_codes(request)), peso_total_kg__lte=0).exclude(status__in=["finalizada", "cancelada"]).count()
    abertos = operacional(ChamadoSaldo, request).filter(status="aberto").count()
    return render_screen(
        request,
        blank_screen(
            title="Qualidade dos dados",
            eyebrow="Sistema",
            lead="O que ainda impede a expedição e o saldo de fecharem limpos.",
            cards=[
                {"label": "Romaneios sem placa ou motorista", "value": sem_placa, "hint": "checklist"},
                {"label": "Viagens sem peso", "value": sem_peso, "hint": "capacidade"},
                {"label": "Chamados de saldo abertos", "value": abertos, "hint": "ruptura"},
            ],
        ),
    )


@login_required
def hub_produtividade(request):
    return _ops_screen(request, "Hub de produtividade", "Operação", "Separação do CD para leitura rápida de unidades e paletes.", Separacao, [("data", "Data"), ("loja", "Loja"), ("setor", "Setor"), ("unidades", "Unidades"), ("paletes", "Paletes")], [{"href": "/modulo/separacao_produtividade/", "label": "Produtividade"}])


@login_required
def frota_simulador(request):
    blocked = _gate(request, "Simulador de frota")
    if blocked:
        return blocked
    notice = ""
    placa = (request.POST.get("placa") or request.GET.get("placa") or "").strip()
    peso = float(request.POST.get("peso") or request.GET.get("peso") or 0)
    if placa:
        veiculo = TmsVeiculo.objects.filter(placa__iexact=placa, ativo=True).first()
        if not veiculo:
            notice = "Placa sem capacidade homologada."
        elif peso > veiculo.capacidade_max_kg:
            notice = f"Acima do limite: {peso} kg contra {veiculo.capacidade_max_kg} kg."
        else:
            notice = f"Dentro do limite. Saldo de {round(veiculo.capacidade_max_kg - peso, 1)} kg."
    return render_screen(
        request,
        blank_screen(
            title="Simulador de frota",
            eyebrow="Frota",
            lead="Compara um peso informado com a capacidade homologada da placa.",
            notice=notice,
            form={
                "action": "/frota/simulador/",
                "title": "Simulação",
                "submit": "Simular",
                "fields": [
                    {"name": "placa", "label": "Placa", "type": "text", "value": placa},
                    {"name": "peso", "label": "Peso (kg)", "type": "number", "value": peso or ""},
                ],
            },
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def page_builder(request):
    blocked = _gate(request, "Page builder")
    if blocked:
        return blocked
    if request.method == "POST":
        nome = (request.POST.get("nome") or "").strip()
        fonte = request.POST.get("fonte") or ""
        if nome and fonte in dict(DATA_SOURCES):
            PageBuilderTela.objects.create(nome=nome, fonte=fonte, criado_por=username(request.user))
        return redirect("/configuracoes/page-builder/")
    telas = list(PageBuilderTela.objects.all()[:40])
    preview_headers, preview_rows = _preview_fonte(request.GET.get("fonte") or "tms_romaneios", request)
    return render_screen(
        request,
        blank_screen(
            title="Page builder",
            eyebrow="Configuração",
            lead="Catálogo de fontes e ações do Gestão CD, com prévia dos dados reais do CD.",
            actions=[{"href": "/personalizar-interface/", "label": "Personalizar interface"}],
            filters=[{"name": "fonte", "label": "Prévia", "type": "select", "value": request.GET.get("fonte") or "tms_romaneios", "options": DATA_SOURCES}],
            form={
                "action": "/configuracoes/page-builder/",
                "title": "Salvar uma tela",
                "submit": "Criar tela",
                "fields": [
                    {"name": "nome", "label": "Nome", "type": "text", "value": "", "required": True},
                    {"name": "fonte", "label": "Fonte", "type": "select", "value": "tms_romaneios", "options": DATA_SOURCES},
                ],
            },
            tables=[
                {"title": "Ações disponíveis", "lead": "", "headers": ["Ação", "Destino"], "rows": [[cell(label), cell(href, href=href)] for label, href in PAGE_ACTIONS]},
                {"title": "Telas salvas", "lead": "", "headers": ["Nome", "Fonte", "Quando"], "rows": [[cell(row.nome), cell(dict(DATA_SOURCES).get(row.fonte, row.fonte)), cell(fmt_dt(row.criado_em))] for row in telas]},
                {"title": "Prévia da fonte", "lead": "", "headers": preview_headers, "rows": preview_rows},
            ],
        ),
    )


def _preview_fonte(fonte, request):
    if fonte == "yms_docas":
        rows = YmsDoca.objects.filter(cd_codigo__in=cd_codes(request))[:8]
        return ["Doca", "Status"], [[cell(row.codigo), cell(row.status, tone_for(row.status))] for row in rows]
    if fonte == "paletes_cd":
        rows = PaleteRedeSaldo.objects.all()[:8]
        return ["Local", "Tipo", "Disponível"], [[cell(row.local_nome), cell(row.tipo_palete), cell(row.disponivel)] for row in rows]
    if fonte == "vale_paletes":
        rows = operacional(PaleteRedeMovimentacao, request)[:8]
        return ["Data", "Destino", "Qtd"], [[cell(row.data.strftime("%d/%m/%Y")), cell(row.destino_nome or "-"), cell(row.quantidade)] for row in rows]
    if fonte in {"recebimentos", "recebimento_chegadas", "recebimento_divergencias"}:
        rows = operacional(Recebimento, request)[:8]
        return ["Fornecedor", "NF", "Paletes"], [[cell(row.fornecedor), cell(row.nota_fiscal or "-"), cell(row.paletes)] for row in rows]
    rows = romaneios_qs(request).order_by("-data")[:8]
    return ["Romaneio", "Loja", "Situação"], [[cell(row.numero_romaneio), cell(row.loja_destino or "-"), status_cell(row.status, ROMANEIO_STATUS)] for row in rows]


@login_required
def api_geofences(request):
    if not allowed(request.user):
        return JsonResponse({"ok": False}, status=403)
    return JsonResponse(
        {
            "ok": True,
            "geofences": [
                {"id": row.id, "nome": row.nome, "tipo": row.tipo, "latitude": row.latitude, "longitude": row.longitude, "raio_metros": row.raio_metros, "ativo": row.ativo}
                for row in TmsGeofence.objects.filter(ativo=True)
            ],
        },
        json_dumps_params={"ensure_ascii": False},
    )
