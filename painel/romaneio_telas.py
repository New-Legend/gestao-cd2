"""Telas do ciclo do romaneio: XML, edição, impressão, foto e vínculo."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.html import escape
from django.views.decorators.http import require_http_methods

from . import krill_telas as k
from . import romaneio_logica as logica
from .models import TmsRomaneioLacreFoto, TmsRomaneioPrevia


def _inteiro(value, default=0) -> int:
    try:
        return int(float(value or default))
    except (TypeError, ValueError):
        return default


def _operacional(request) -> dict:
    return {
        "placa": request.POST.get("placa") or "",
        "motorista": request.POST.get("motorista") or "",
        "lacre_01": request.POST.get("lacre_01") or "",
        "lacre_02": request.POST.get("lacre_02") or "",
        "lacre_03": request.POST.get("lacre_03") or "",
        "total_paletes": request.POST.get("total_paletes_fisicos") or request.POST.get("total_paletes") or "",
        "ajuste_manual": request.POST.get("ajuste_manual_paletes") == "1",
        "paletes_chep": _inteiro(request.POST.get("paletes_chep_manual")),
        "paletes_descartavel": _inteiro(request.POST.get("paletes_descartavel_manual")),
    }


def _tela_erro(request, titulo, mensagem, voltar="/tms/romaneios/novo/"):
    return k.render_screen(
        request,
        k.blank_screen(title=titulo, eyebrow="TMS", error=mensagem, actions=[{"href": voltar, "label": "Voltar"}]),
        status=422,
    )


def _form_confirmacao(token: str, payload: dict, mensagem=""):
    notas = payload.get("notas") or []
    return {
        "action": "/tms/romaneios/novo/",
        "title": "Conferência do romaneio",
        "submit": "Salvar romaneio",
        "multipart": True,
        "hidden": {"action": "confirm", "import_token": token},
        "fields": [
            {"name": "placa", "label": "Placa", "type": "text", "value": "", "required": True},
            {"name": "motorista", "label": "Motorista", "type": "text", "value": "", "required": True},
            {"name": "lacre_01", "label": "Lacre 1", "type": "text", "value": "", "required": True},
            {"name": "lacre_02", "label": "Lacre 2", "type": "text", "value": "", "required": True},
            {"name": "lacre_03", "label": "Lacre 3", "type": "text", "value": "", "required": True},
            {"name": "total_paletes_fisicos", "label": "Paletes físicos", "type": "number", "value": "", "required": True},
            {"name": "ajuste_manual_paletes", "label": "Ajuste manual (1 para abater CHEP/descartável)", "type": "text", "value": ""},
            {"name": "paletes_chep_manual", "label": "CHEP manual", "type": "number", "value": "0"},
            {"name": "paletes_descartavel_manual", "label": "Descartável manual", "type": "number", "value": "0"},
            {"name": "foto_lacres", "label": "Foto dos lacres", "type": "file", "value": "", "accept": "image/jpeg,image/png,image/webp,image/gif"},
        ],
        "_resumo": mensagem,
        "_notas": len(notas),
        "_valor": payload.get("totalValor"),
        "_loja": payload.get("lojaDestino"),
        "_cd": payload.get("cdOrigem"),
        "_duplicadas": payload.get("duplicadas") or 0,
    }


@login_required
@require_http_methods(["GET", "POST"])
def tms_romaneios_novo(request):
    blocked = k._gate(request, "Novo romaneio")
    if blocked:
        return blocked
    if request.method == "POST" and request.FILES.getlist("xml_nf"):
        previa = logica.preparar_previa(request.FILES.getlist("xml_nf"), k.username(request.user))
        if not previa.get("ok"):
            return _tela_erro(request, "XML do romaneio", previa.get("error") or "Não foi possível processar os XMLs enviados.")
        return _render_previa(request, previa["token"], previa["payload"])
    if request.method == "POST" and (request.POST.get("action") == "confirm"):
        draft = logica.carregar_previa(request.POST.get("import_token") or "", k.username(request.user))
        payload = draft.payload_json if draft else None
        if not payload:
            return _tela_erro(request, "XML do romaneio", "A prévia do XML expirou ou não foi encontrada. Envie os arquivos novamente.")
        resultado = logica.finalizar_xml(payload, _operacional(request), k.username(request.user))
        if not resultado.get("sucesso"):
            return _render_previa(request, draft.token, payload, resultado.get("erro") or "Não foi possível salvar o romaneio.")
        foto = logica.salvar_foto(resultado["romaneio"], request.FILES.get("foto_lacres"), k.username(request.user))
        if foto:
            from .bluesoft_valor import recalcular_exclusao
            from .models import TmsRomaneio

            row = TmsRomaneio.objects.filter(pk=resultado["romaneio"]).first()
            if row:
                recalcular_exclusao(row, k.username(request.user))
                row.delete()
            return _render_previa(request, draft.token, payload, foto)
        TmsRomaneioPrevia.objects.filter(pk=draft.pk).delete()
        return redirect(f"/tms/romaneios/{resultado['romaneio']}/?conference=1")
    if request.method == "POST":
        return k.tms_romaneios(request)
    token = (request.GET.get("import_token") or "").strip()
    if token:
        draft = logica.carregar_previa(token, k.username(request.user))
        if not draft:
            return _tela_erro(request, "XML do romaneio", "A prévia do XML expirou ou não foi encontrada. Envie os arquivos novamente.")
        return _render_previa(request, token, draft.payload_json)
    return k.render_screen(
        request,
        k.blank_screen(
            title="Novo romaneio",
            eyebrow="TMS",
            lead="O XML descarta a NF-e que já saiu e fatura só a soma do que sobrou. Sem XML, a lista de chaves faz a mesma peneira. Sem nota nenhuma, o campo é o total acumulado da BlueSoft.",
            actions=[{"href": "/tms/romaneios/", "label": "Voltar"}],
            form={
                "action": "/tms/romaneios/novo/",
                "title": "Ler XML da NF-e",
                "submit": "Gerar prévia",
                "multipart": True,
                "fields": [
                    {"name": "xml_nf", "label": "Arquivos XML", "type": "file", "value": "", "multiple": True, "accept": ".xml,text/xml,application/xml", "required": True},
                ],
            },
            secondary_form={
                "action": "/tms/romaneios/novo/",
                "title": "Ou lançar sem XML",
                "submit": "Salvar rascunho",
                "fields": [
                    {"name": "numero_romaneio", "label": "Número", "type": "text", "value": "", "required": True},
                    {"name": "data", "label": "Data", "type": "date", "value": k.today().isoformat()},
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


def _render_previa(request, token, payload, mensagem=""):
    form = _form_confirmacao(token, payload, mensagem)
    notas = payload.get("notas") or []
    return k.render_screen(
        request,
        k.blank_screen(
            title="Conferência do romaneio",
            eyebrow="TMS",
            lead=f"{payload.get('cdOrigem') or '-'} → {payload.get('lojaDestino') or '-'}. {len(notas)} NF-e nova(s), {payload.get('duplicadas') or 0} descartada(s). A faturar {k.brl(payload.get('totalValor'))}.",
            error=mensagem,
            actions=[{"href": "/tms/romaneios/novo/", "label": "Ler outros XMLs"}],
            cards=[
                {"label": "NF-e novas", "value": len(notas), "hint": "depois da peneira"},
                {"label": "A faturar", "value": k.brl(payload.get("totalValor")), "hint": "soma das notas inéditas"},
                {"label": "Peso", "value": f"{float(payload.get('totalPesoBruto') or 0):.3f} kg", "hint": "bruto do XML"},
                {"label": "Descartadas", "value": payload.get("duplicadas") or 0, "hint": "já vinculadas ou repetidas"},
            ],
            tables=[
                {
                    "title": "Notas da prévia",
                    "lead": "",
                    "headers": ["NF-e", "Série", "Chave", "Valor", "Palete"],
                    "rows": [
                        [
                            k.cell(nota.get("numero") or "-"),
                            k.cell(nota.get("serie") or "-"),
                            k.cell(nota.get("chaveAcesso") or "-"),
                            k.cell(k.brl(nota.get("valorTotal"))),
                            k.cell(nota.get("classificacaoPalete") or "PBR"),
                        ]
                        for nota in notas
                    ],
                }
            ],
            form=form,
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def tms_romaneio_editar(request, pk):
    blocked = k._gate(request, "Editar romaneio")
    if blocked:
        return blocked
    row = k.romaneios_qs(request).filter(pk=pk).first()
    if not row:
        return k.render_screen(request, k.blank_screen(title="Romaneio não encontrado", actions=[{"href": "/tms/romaneios/", "label": "Voltar"}]), status=404)
    if row.status == "cancelado":
        return k.render_screen(
            request,
            k.blank_screen(title="Romaneio cancelado", error="Este romaneio não pode ser editado.", actions=[{"href": f"/tms/romaneios/{row.pk}/", "label": "Voltar"}]),
            status=409,
        )
    if request.method == "POST":
        lacres = logica.separar_lacres(row.lacres)
        resultado = logica.editar_romaneio(
            row,
            {
                "data": request.POST.get("data") or row.data.isoformat(),
                "cd_origem": request.POST.get("cd_origem") or row.cd_origem,
                "loja_destino": request.POST.get("loja_destino") or row.loja_destino,
                "motorista": request.POST.get("motorista") or "",
                "placa": request.POST.get("placa") or "",
                "snapshot": request.POST.get("valor_total_carga") or 0,
                "lacre_01": request.POST.get("lacre_01") if request.POST.get("lacre_01") is not None else (lacres[0] if len(lacres) > 0 else ""),
                "lacre_02": request.POST.get("lacre_02") or "",
                "lacre_03": request.POST.get("lacre_03") or "",
                "paletes_pbr": _inteiro(request.POST.get("paletes_pbr")),
                "paletes_chep": _inteiro(request.POST.get("paletes_chep")),
                "paletes_descartavel": _inteiro(request.POST.get("paletes_descartavel")),
                "total_paletes": _inteiro(request.POST.get("total_paletes")),
                "observacoes": request.POST.get("observacoes") or "",
            },
            k.username(request.user),
        )
        if not resultado.get("sucesso"):
            return _tela_erro(request, "Editar romaneio", resultado.get("erro") or "Não foi possível salvar.", f"/tms/romaneios/{row.pk}/editar/")
        foto = logica.salvar_foto(row.pk, request.FILES.get("foto_lacres"), k.username(request.user))
        if foto:
            return _tela_erro(request, "Foto dos lacres", foto, f"/tms/romaneios/{row.pk}/editar/")
        return redirect(f"/tms/romaneios/{row.pk}/")
    lacres = logica.separar_lacres(row.lacres)
    while len(lacres) < 3:
        lacres.append("")
    return k.render_screen(
        request,
        k.blank_screen(
            title=f"Editar {row.numero_romaneio}",
            eyebrow="TMS",
            lead="Informe o total acumulado exibido no filtro da BlueSoft. O sistema grava só a diferença deste lançamento para o mesmo CD, loja e data.",
            actions=[{"href": f"/tms/romaneios/{row.pk}/", "label": "Voltar"}],
            cards=[
                {"label": "A faturar hoje", "value": k.brl(row.valor_total_carga), "hint": "diferença já gravada"},
                {"label": "Acumulado", "value": k.brl(row.valor_acumulado_bluesoft), "hint": "último snapshot"},
            ],
            form={
                "action": f"/tms/romaneios/{row.pk}/editar/",
                "title": "Dados do romaneio",
                "submit": "Salvar edição",
                "multipart": True,
                "fields": [
                    {"name": "data", "label": "Data", "type": "date", "value": row.data.isoformat()},
                    {"name": "cd_origem", "label": "CD", "type": "text", "value": row.cd_origem},
                    {"name": "loja_destino", "label": "Loja", "type": "text", "value": row.loja_destino, "wide": True},
                    {"name": "motorista", "label": "Motorista", "type": "text", "value": row.motorista},
                    {"name": "placa", "label": "Placa", "type": "text", "value": row.placa},
                    {"name": "valor_total_carga", "label": "Total acumulado na BlueSoft", "type": "number", "value": row.valor_acumulado_bluesoft or 0},
                    {"name": "paletes_pbr", "label": "PBR", "type": "number", "value": row.paletes_pbr},
                    {"name": "paletes_chep", "label": "CHEP", "type": "number", "value": row.paletes_chep},
                    {"name": "paletes_descartavel", "label": "Descartável", "type": "number", "value": row.paletes_descartavel},
                    {"name": "total_paletes", "label": "Paletes", "type": "number", "value": row.total_paletes},
                    {"name": "lacre_01", "label": "Lacre 1", "type": "text", "value": lacres[0]},
                    {"name": "lacre_02", "label": "Lacre 2", "type": "text", "value": lacres[1]},
                    {"name": "lacre_03", "label": "Lacre 3", "type": "text", "value": lacres[2]},
                    {"name": "foto_lacres", "label": "Foto dos lacres", "type": "file", "value": "", "accept": "image/jpeg,image/png,image/webp,image/gif"},
                    {"name": "observacoes", "label": "Observações", "type": "textarea", "value": row.observacoes, "wide": True},
                ],
            },
        ),
    )


@login_required
def tms_romaneio_imprimir(request, pk):
    blocked = k._gate(request, "Romaneio")
    if blocked:
        return blocked
    row = k.romaneios_qs(request).filter(pk=pk).first()
    if not row:
        return HttpResponse("<main><h1>Romaneio não encontrado</h1></main>", status=404, content_type="text/html")
    notas = list(row.nfes.all())
    pendentes = sum(1 for nota in notas if nota.status_conferencia != "conferida")
    if notas and pendentes:
        if "application/json" in (request.headers.get("Accept") or ""):
            return JsonResponse({"error": "Bloqueio Fiscal: Faltam notas a serem bipadas.", "pendentes": pendentes, "total": len(notas)}, status=403)
        return k.render_screen(
            request,
            k.blank_screen(
                title="Impressão bloqueada",
                eyebrow="TMS",
                error="Bloqueio Fiscal: Faltam notas a serem bipadas.",
                actions=[{"href": f"/tms/romaneios/{row.pk}/", "label": "Voltar à conferência"}],
            ),
            status=403,
        )
    linhas = "".join(
        f"<tr><td>{escape(nota.numero or '-')}</td><td>{escape(nota.serie or '-')}</td><td>{escape(k.brl(nota.valor_total))}</td><td>{escape(str(nota.peso_bruto_kg or 0))} kg</td><td>{escape(str(nota.total_volumes or 0))}</td></tr>"
        for nota in notas
    )
    html = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Romaneio {escape(row.numero_romaneio)}</title>
    <style>body{{font-family:sans-serif;margin:24px;color:#111}}h1{{font-size:22px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:6px;text-align:left}}@media print{{.no-print{{display:none}}}}</style></head>
    <body><header><h1>ROMANEIO DE TRANSPORTE</h1><p>REDE KRILL SUPERMERCADOS · {escape(k.ROMANEIO_STATUS.get(row.status, row.status))}</p><p><strong>{escape(row.numero_romaneio)}</strong> · {escape(row.data.strftime('%d/%m/%Y'))} {escape(row.hora or '')}</p></header>
    <h2>1. Dados da viagem</h2><p>Motorista {escape(row.motorista or '-')} · Placa {escape(row.placa or '-')} · CD {escape(row.cd_origem or '-')} · Lacres {escape(row.lacres or '-')}</p>
    <h2>2. Destino</h2><p>{escape(row.loja_destino or '-')} · {escape(row.endereco_loja or '-')} · {escape(row.cidade_loja or '-')} · CEP {escape(row.cep_loja or '-')} · CNPJ {escape(row.destinatario_cnpj or '-')}</p>
    <h2>3. Dados fiscais</h2><p>{len(notas) or row.quantidade_nfes} NF-e · {escape(k.brl(row.valor_total_carga))}</p>
    <table><thead><tr><th>NF-e</th><th>Série</th><th>Valor</th><th>Peso</th><th>Volumes</th></tr></thead><tbody>{linhas}</tbody></table>
    <h2>4. Físico</h2><p>{row.total_paletes} paletes · {row.peso_bruto_kg} kg · {row.total_volumes} volumes · PBR {row.paletes_pbr} · CHEP {row.paletes_chep} · Descartável {row.paletes_descartavel}</p>
    <h2>Termo de transferência de responsabilidade</h2><p>A carga, os documentos fiscais e os equipamentos relacionados foram transferidos para transporte e recebimento no destino indicado.</p>
    <p class="no-print"><button onclick="window.print()">Imprimir</button> <a href="/tms/romaneios/{row.pk}/">Voltar</a></p></body></html>"""
    return HttpResponse(html)


@login_required
def tms_romaneio_lacre_foto(request, pk):
    blocked = k._gate(request, "Romaneio")
    if blocked:
        return blocked
    row = get_object_or_404(k.romaneios_qs(request), pk=pk)
    foto = TmsRomaneioLacreFoto.objects.filter(romaneio=row).first()
    if not foto:
        return k.render_screen(request, k.blank_screen(title="Foto dos lacres", error="Nenhuma foto anexada.", actions=[{"href": f"/tms/romaneios/{row.pk}/", "label": "Voltar"}]), status=404)
    return HttpResponse(bytes(foto.conteudo), content_type=foto.mime_type)


@login_required
@require_http_methods(["POST"])
def api_romaneio_vinculo(request, pk):
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão para editar romaneios."}, status=403)
    row = k.romaneios_qs(request).filter(pk=pk).first()
    if not row:
        return JsonResponse({"ok": False, "error": "Romaneio não encontrado no seu CD."}, status=404)
    if "json" in (request.content_type or ""):
        import json

        try:
            body = json.loads(request.body.decode() or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"ok": False, "error": "Não foi possível ler placa e motorista."}, status=400)
    else:
        body = request.POST
    resultado = logica.vincular_veiculo(row, body, k.username(request.user))
    status = 200 if resultado.get("ok") else resultado.get("status") or 422
    return JsonResponse(resultado, status=status)
