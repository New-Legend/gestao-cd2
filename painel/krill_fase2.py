"""Fase 2 — motores do Worker que a primeira leva ainda não traduziu.

Bipagem de NF-e, MDF-e, baixa manual, KM/palete, descarga, rota local,
telemetria e os atalhos que o Gestão CD expunha e o Django ainda não tinha.
O stream de /api/stream/kpis, a matriz do Google e o compositor de viagem
estão em fusao_motor.py. O acumulado BlueSoft está em bluesoft_valor.py.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime
from urllib.parse import quote

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import krill_telas as k
from .bluesoft_valor import criar_romaneio_com_notas, lancar_snapshot, recalcular_exclusao, recalcular_status
from .models import (
    AuditLog,
    ChamadoSaldo,
    LogisticaDispositivo,
    PageBuilderTela,
    PessoaTurno,
    TmsContaPalete,
    TmsDevolucao,
    TmsExcecaoOperacional,
    TmsGeofence,
    TmsGeofenceEvento,
    TmsMdfeManifesto,
    TmsMdfeRomaneio,
    TmsRascunho,
    TmsRascunhoNfe,
    TmsRomaneio,
    TmsRomaneioNfe,
    TmsRota,
    TmsTelemetria,
    TmsVeiculo,
    TmsViagem,
    WmsPosicao,
    YmsDoca,
)

DESCARGA_TARIFAS = {"sider_carreta": 25, "bau": 30, "granel": 60}
STATUS_LIVRES = {
    "rascunho",
    "conferido",
    "aguardando_conferencia",
    "aguardando_complemento",
    "em_transporte",
    "entregue",
    "finalizado",
    "cancelado",
}
TRANSITO = {"em_transito", "em_transporte", "em_descarregamento"}
MDFE_OBRIGATORIOS = (
    ("emitente_razao_social", "Razão social do emitente"),
    ("emitente_cnpj", "CNPJ do emitente"),
    ("emitente_ie", "Inscrição estadual do emitente"),
    ("uf_carregamento", "UF de carregamento"),
    ("uf_descarregamento", "UF de descarregamento"),
    ("motorista_nome", "Nome do motorista"),
    ("motorista_cpf", "CPF do motorista"),
    ("placa", "Placa do veículo"),
    ("lacres", "Lacres"),
)


def _audit(user, acao, modulo, objeto_id, detalhe, depois=None):
    AuditLog.objects.create(
        user=user if getattr(user, "pk", None) else None,
        usuario_nome=k.username(user),
        acao=acao[:80],
        modulo=modulo[:80],
        objeto_id=str(objeto_id or "")[:40],
        detalhe=detalhe,
        dados_depois=depois or {},
    )


def _json(request):
    try:
        payload = json.loads(request.body.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _text(value) -> str:
    return str(value or "").strip()


def _moeda(value) -> float:
    return round(float(value or 0) * 100) / 100


def _normalizar_modal(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn").lower()
    if re.search(r"granel|rebater", normalized):
        return "granel"
    if "bau" in normalized:
        return "bau"
    return "sider_carreta"


def calcular_descarga(veiculo_tipo, quantidade, sider=0, bau=0, gerados=0, taxa_sider=None, taxa_bau=None, taxa_granel=None):
    modal_veiculo = _normalizar_modal(veiculo_tipo)
    total = max(0, float(quantidade or 0))
    paletes_sider = max(0, float(sider or 0))
    paletes_bau = max(0, float(bau or 0))
    paletes_gerados = max(0, float(gerados or 0))
    if paletes_sider == 0 and paletes_bau == 0 and paletes_gerados == 0 and total > 0:
        if modal_veiculo == "granel":
            paletes_gerados = total
        elif modal_veiculo == "bau":
            paletes_bau = total
        else:
            paletes_sider = total
    taxa_sider = DESCARGA_TARIFAS["sider_carreta"] if taxa_sider is None else max(0, float(taxa_sider))
    taxa_bau = DESCARGA_TARIFAS["bau"] if taxa_bau is None else max(0, float(taxa_bau))
    taxa_granel = DESCARGA_TARIFAS["granel"] if taxa_granel is None else max(0, float(taxa_granel))
    valor_sider = _moeda(paletes_sider * taxa_sider)
    valor_bau = _moeda(paletes_bau * taxa_bau)
    valor_fechados = _moeda(valor_sider + valor_bau)
    valor_granel = _moeda(paletes_gerados * taxa_granel)
    fechados = paletes_sider + paletes_bau
    base = fechados + paletes_gerados
    valor = _moeda(valor_fechados + valor_granel)
    componentes = sum(1 for item in (paletes_sider, paletes_bau, paletes_gerados) if item > 0)
    if componentes > 1:
        modal = "mista"
    elif paletes_gerados > 0:
        modal = "granel"
    elif paletes_bau > 0:
        modal = "bau"
    else:
        modal = "sider_carreta"
    taxa_fechado = _moeda(valor_fechados / fechados) if fechados else taxa_sider
    taxa = _moeda(valor / base) if base else taxa_fechado
    return {
        "modal": modal,
        "quantidade_base": base,
        "taxa_unitaria": taxa,
        "taxa_fechado": taxa_fechado,
        "taxa_granel": taxa_granel,
        "valor_sider": valor_sider,
        "valor_bau": valor_bau,
        "valor_fechados": valor_fechados,
        "valor_granel": valor_granel,
        "valor_descarga": valor,
    }


def _lacres(texto: str) -> list[str]:
    return [item for item in re.split(r"[\s,;|/]+", str(texto or "")) if item.strip()][:3]


def _bypass_conferencia() -> bool:
    return os.environ.get("TMS_NFE_CONFERENCIA_BYPASS", "").strip() in {"1", "true", "sim"}


def _pendentes(romaneio: TmsRomaneio) -> int:
    return romaneio.nfes.exclude(status_conferencia="conferida").count()


def aplicar_status_romaneio(row: TmsRomaneio, status: str, user, justificativa: str = "") -> tuple[bool, str, int]:
    """Tradução dos portões de tmsRomaneioStatus, inclusive o acumulado BlueSoft."""
    if status not in STATUS_LIVRES:
        return False, "Situação não permitida.", 422
    if status == "conferido" and not _bypass_conferencia():
        return False, "A conferência fiscal só é liberada pela bipagem de 100% das NF-e no embarque.", 409
    if status in {"em_transporte", "entregue", "finalizado"} and not _bypass_conferencia() and _pendentes(row) > 0:
        return False, "A saída está bloqueada: ainda há NF-e pendentes de bipagem no embarque.", 422
    if status == "rascunho" and row.status != "rascunho" and len(_text(justificativa)) < 5:
        return False, "Informe uma justificativa com ao menos cinco caracteres para voltar o romaneio a rascunho.", 422
    if status == "conferido":
        paletes = int(row.total_paletes or 0)
        seals = _lacres(row.lacres)
        if not _text(row.placa) or not _text(row.motorista) or len(seals) != 3 or paletes < 0:
            return False, "A conferência exige placa, motorista, três lacres e total de paletes válido.", 422
        vehicle = TmsVeiculo.objects.filter(placa__iexact=_text(row.placa), ativo=True).first()
        if vehicle and vehicle.capacidade_max_pallets and paletes > vehicle.capacidade_max_pallets:
            return False, "A ocupação informada ultrapassa a capacidade cadastrada do veículo.", 422
    with transaction.atomic():
        ok, message = recalcular_status(row, status, k.username(user))
        if not ok:
            transaction.set_rollback(True)
            return False, message, 422
        now = timezone.now()
        row.status = status
        row.atualizado_por = k.username(user)
        if status == "em_transporte" and not row.data_saida:
            row.data_saida = now
        if status == "entregue" and not row.data_chegada_loja:
            row.data_chegada_loja = now
        if status == "finalizado" and not row.data_retorno_cd:
            row.data_retorno_cd = now
        row.save()
    if status == "rascunho" and _text(justificativa):
        TmsExcecaoOperacional.objects.create(
            romaneio=row,
            acao="voltar_rascunho",
            justificativa=_text(justificativa),
            usuario=k.username(user),
        )
    _audit(user, "mudou_status", "tms_romaneios", row.pk, f"{row.numero_romaneio}: {status}", {"status": status})
    return True, f"Romaneio {row.numero_romaneio} atualizado para {k.ROMANEIO_STATUS.get(status, status)}.", 200


def _conference(row: TmsRomaneio) -> dict:
    notas = list(row.nfes.all())
    total = len(notas)
    conferidas = sum(1 for note in notas if note.status_conferencia == "conferida")
    return {
        "ok": True,
        "romaneio": {"id": row.pk, "numero": row.numero_romaneio, "status": row.status},
        "total": total,
        "conferidas": conferidas,
        "pendentes": total - conferidas,
        "progresso": f"{conferidas}/{total}",
        "allConferidas": total > 0 and conferidas == total,
        "notas": [
            {
                "id": note.pk,
                "chave_acesso": note.chave_acesso,
                "numero": note.numero,
                "serie": note.serie,
                "status_conferencia": note.status_conferencia,
            }
            for note in notas
        ],
    }


def _mdfe_errors(payload: dict, ids: list[int] | None = None) -> list[str]:
    errors = []
    for key, label in MDFE_OBRIGATORIOS:
        if not _text(payload.get(key)):
            errors.append(f"{label} é obrigatório.")
    if len(_digits(payload.get("emitente_cnpj"))) != 14:
        errors.append("CNPJ do emitente deve conter 14 dígitos.")
    if len(_digits(payload.get("motorista_cpf"))) != 11:
        errors.append("CPF do motorista deve conter 11 dígitos.")
    for key, label in (("uf_carregamento", "UF de carregamento"), ("uf_descarregamento", "UF de descarregamento")):
        if not re.fullmatch(r"[A-Z]{2}", _text(payload.get(key)).upper()):
            errors.append(f"{label} inválida.")
    chosen = ids if ids is not None else payload.get("romaneio_ids")
    if not isinstance(chosen, list) or not chosen:
        errors.append("Informe ao menos um romaneio para o manifesto.")
    return errors


def _mdfe_payload(payload: dict, romaneios: list[TmsRomaneio]) -> dict:
    return {
        "tipo": "MDF-e",
        "versao": "3.00",
        "identificacao": {
            "numero": _text(payload.get("numero")),
            "serie": _text(payload.get("serie") or "1"),
            "data_emissao": _text(payload.get("data_emissao") or k.today().isoformat()),
            "status": "rascunho",
        },
        "emitente": {
            "razao_social": _text(payload.get("emitente_razao_social")),
            "cnpj": _digits(payload.get("emitente_cnpj")),
            "inscricao_estadual": _text(payload.get("emitente_ie")),
        },
        "transporte": {
            "uf_carregamento": _text(payload.get("uf_carregamento")).upper(),
            "uf_descarregamento": _text(payload.get("uf_descarregamento")).upper(),
            "motorista": {"nome": _text(payload.get("motorista_nome")), "cpf": _digits(payload.get("motorista_cpf"))},
            "veiculo": {"placa": _text(payload.get("placa")).upper(), "rntrc": _text(payload.get("rntrc"))},
            "lacres": _text(payload.get("lacres")),
        },
        "documentos": [
            {
                "romaneio_id": row.pk,
                "numero_romaneio": row.numero_romaneio,
                "quantidade_nfes": row.quantidade_nfes,
                "valor_total_carga": row.valor_total_carga,
                "loja_destino": row.loja_destino,
                "cd_origem": row.cd_origem,
            }
            for row in romaneios
        ],
        "observacoes": _text(payload.get("observacoes")),
    }


def _rota_local(cd_origem: str, lojas: list[str]) -> dict | None:
    rotas = {row.loja_codigo.upper(): row for row in TmsRota.objects.filter(cd_origem=cd_origem, ativa=True)}
    missing = [loja for loja in lojas if loja.upper() not in rotas]
    if missing:
        return None
    ordered = sorted(lojas, key=lambda loja: (rotas[loja.upper()].distancia_km, loja))
    distancia = sum(rotas[loja.upper()].distancia_km for loja in ordered)
    minutos = sum(rotas[loja.upper()].tempo_previsto_minutos for loja in ordered)
    return {
        "ok": True,
        "fonte": "tabela_interna",
        "cd_origem": cd_origem,
        "lojas": ordered,
        "distance_km": round(distancia, 2),
        "duration_minutes": minutos,
        "notice": "GOOGLE_MAPS_API_KEY ausente. A ordem usa a tabela interna de distâncias.",
    }


@login_required
@require_http_methods(["GET", "POST"])
def api_romaneio_bipar(request, pk):
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão para conferir o embarque."}, status=403)
    row = k.romaneios_qs(request).filter(pk=pk).first()
    if not row:
        return JsonResponse({"error": "Romaneio não encontrado."}, status=404)
    if request.method == "GET":
        return JsonResponse(_conference(row))
    if "json" in (request.content_type or ""):
        payload = _json(request)
        if payload is None:
            return JsonResponse({"error": "Informe a chave de acesso da NF-e em formato JSON."}, status=400)
        chave = _digits(payload.get("chave_acesso"))
    else:
        chave = _digits(request.POST.get("chave_acesso"))
    if not re.fullmatch(r"\d{44}", chave):
        return JsonResponse({"error": "A chave de acesso deve conter exatamente 44 dígitos."}, status=400)
    if row.status in {"cancelado", "finalizado"}:
        return JsonResponse({"error": "Este romaneio não está disponível para conferência."}, status=409)
    note = row.nfes.filter(chave_acesso=chave).first()
    if not note:
        return JsonResponse({"error": "Nota não pertence a esta carga."}, status=400)
    if note.status_conferencia == "conferida":
        return JsonResponse({"error": "Esta NF-e já foi bipada.", "codigo": "NFE_DUPLICADA"}, status=409)
    updated = TmsRomaneioNfe.objects.filter(pk=note.pk, romaneio_id=row.pk, status_conferencia="pendente").update(status_conferencia="conferida")
    if not updated:
        return JsonResponse({"error": "Esta NF-e já foi bipada. Atualize a conferência.", "codigo": "NFE_DUPLICADA"}, status=409)
    row.refresh_from_db()
    if row.nfes.exists() and not row.nfes.exclude(status_conferencia="conferida").exists() and row.status in {"pendente_conferencia", "rascunho", "aguardando_conferencia"}:
        seals = _lacres(row.lacres)
        vehicle = TmsVeiculo.objects.filter(placa__iexact=_text(row.placa), ativo=True).first()
        paletes = int(row.total_paletes or 0)
        capacidade_ok = not vehicle or not vehicle.capacidade_max_pallets or paletes <= vehicle.capacidade_max_pallets
        if _text(row.placa) and _text(row.motorista) and len(seals) == 3 and paletes >= 0 and capacidade_ok:
            row.status = "conferido"
            row.atualizado_por = k.username(request.user)
            row.save(update_fields=["status", "atualizado_por", "updated_at"])
            _audit(request.user, "conferiu_embarque", "tms_romaneios", row.pk, f"Romaneio {row.numero_romaneio} conferido por bipagem")
    wants_html = "text/html" in (request.headers.get("Accept") or "") and "json" not in (request.headers.get("Accept") or "")
    if request.POST and wants_html:
        return redirect(f"/tms/romaneios/{row.pk}/")
    return JsonResponse(_conference(row))


@login_required
@require_http_methods(["POST"])
def tms_romaneio_status(request, pk):
    blocked = k._gate(request, "Romaneio")
    if blocked:
        return blocked
    row = k.romaneios_qs(request).filter(pk=pk).first()
    if not row:
        return redirect("/tms/romaneios/")
    ok, message, code = aplicar_status_romaneio(row, request.POST.get("status") or "", request.user, request.POST.get("justificativa") or "")
    destino = request.POST.get("return_to") or f"/tms/romaneios/{row.pk}/"
    if not destino.startswith("/"):
        destino = f"/tms/romaneios/{row.pk}/"
    if not ok:
        return k.render_screen(
            request,
            k.blank_screen(title="Situação do romaneio", eyebrow="TMS", error=message, actions=[{"href": destino, "label": "Voltar"}]),
            status=code,
        )
    return redirect(destino)


@login_required
@require_http_methods(["GET", "POST"])
def tms_romaneio_km(request, pk):
    blocked = k._gate(request, "KM do romaneio")
    if blocked:
        return blocked
    row = get_object_or_404(k.romaneios_qs(request), pk=pk)
    if request.method == "POST":
        pbr = max(0, int(float(request.POST.get("paletes_pbr_devolvidos") or 0)))
        descartavel = max(0, int(float(request.POST.get("paletes_descartaveis_devolvidos") or 0)))
        if pbr > int(row.paletes_pbr or 0) or descartavel > int(row.paletes_descartavel or 0):
            return k.render_screen(
                request,
                k.blank_screen(
                    title="Retorno de paletes",
                    eyebrow="TMS",
                    error="O retorno não pode ultrapassar os paletes enviados no romaneio.",
                    actions=[{"href": f"/tms/romaneios/{row.pk}/", "label": "Voltar"}],
                ),
                status=422,
            )
        row.km_saida = float(request.POST.get("km_saida") or 0)
        row.km_chegada = float(request.POST.get("km_chegada") or 0)
        row.recebido_por = _text(request.POST.get("recebido_por"))[:160]
        row.comprovante_entrega = _text(request.POST.get("comprovante_entrega"))[:240]
        row.atualizado_por = k.username(request.user)
        for field, attr in (("data_saida", "data_saida"), ("data_chegada_loja", "data_chegada_loja"), ("data_retorno_cd", "data_retorno_cd")):
            raw = _text(request.POST.get(field))
            if raw:
                try:
                    parsed = datetime.fromisoformat(raw)
                    if timezone.is_naive(parsed):
                        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
                    setattr(row, attr, parsed)
                except ValueError:
                    pass
        row.save()
        TmsContaPalete.objects.update_or_create(
            romaneio=row,
            defaults={
                "cd_origem": row.cd_origem,
                "filial": row.loja_destino or "Destino não informado",
                "paletes_pbr_enviados": int(row.paletes_pbr or 0),
                "paletes_descartaveis_enviados": int(row.paletes_descartavel or 0),
                "paletes_pbr_devolvidos": pbr,
                "paletes_descartaveis_devolvidos": descartavel,
                "criado_por": k.username(request.user),
            },
        )
        _audit(request.user, "atualizou_km", "tms_romaneios", row.pk, f"KM {row.numero_romaneio}")
        return redirect(f"/tms/romaneios/{row.pk}/")
    return k.render_screen(
        request,
        k.blank_screen(
            title=f"KM · {row.numero_romaneio}",
            eyebrow="TMS",
            lead="Quilometragem e retorno de paletes. A devolução não pode passar do que saiu.",
            actions=[{"href": f"/tms/romaneios/{row.pk}/", "label": "Voltar"}],
            form={
                "action": f"/tms/romaneios/{row.pk}/km/",
                "title": "Fechar KM",
                "submit": "Salvar",
                "fields": [
                    {"name": "km_saida", "label": "KM saída", "type": "number", "value": row.km_saida or 0},
                    {"name": "km_chegada", "label": "KM chegada", "type": "number", "value": row.km_chegada or 0},
                    {"name": "paletes_pbr_devolvidos", "label": "PBR devolvidos", "type": "number", "value": 0},
                    {"name": "paletes_descartaveis_devolvidos", "label": "Descartáveis devolvidos", "type": "number", "value": 0},
                    {"name": "recebido_por", "label": "Recebido por", "type": "text", "value": row.recebido_por},
                    {"name": "comprovante_entrega", "label": "Comprovante", "type": "text", "value": row.comprovante_entrega},
                ],
            },
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def tms_romaneio_bluesoft(request, pk):
    blocked = k._gate(request, "Acumulado BlueSoft")
    if blocked:
        return blocked
    row = get_object_or_404(k.romaneios_qs(request), pk=pk)
    if request.method == "POST":
        try:
            snapshot = float(str(request.POST.get("valor_total_carga") or "0").replace(",", "."))
        except ValueError:
            snapshot = 0
        erro = lancar_snapshot(row, snapshot, k.username(request.user))
        if erro:
            return k.render_screen(
                request,
                k.blank_screen(
                    title="Acumulado BlueSoft",
                    eyebrow="TMS",
                    error=erro,
                    actions=[{"href": f"/tms/romaneios/{row.pk}/", "label": "Voltar"}],
                ),
                status=422,
            )
        return redirect(f"/tms/romaneios/{row.pk}/")
    return k.render_screen(
        request,
        k.blank_screen(
            title=f"BlueSoft · {row.numero_romaneio}",
            eyebrow="TMS",
            lead="Informe o total acumulado exibido no filtro da BlueSoft. O sistema grava só a diferença deste lançamento para o mesmo CD, loja e data.",
            actions=[{"href": f"/tms/romaneios/{row.pk}/", "label": "Voltar"}],
            cards=[
                {"label": "A faturar", "value": k.brl(row.valor_total_carga), "hint": "diferença deste romaneio"},
                {"label": "Acumulado", "value": k.brl(row.valor_acumulado_bluesoft), "hint": "snapshot BlueSoft"},
            ],
            form={
                "action": f"/tms/romaneios/{row.pk}/bluesoft/",
                "title": "Total acumulado na BlueSoft",
                "submit": "Recalcular diferença",
                "fields": [
                    {"name": "valor_total_carga", "label": "Total acumulado na BlueSoft", "type": "number", "value": row.valor_acumulado_bluesoft or 0},
                ],
            },
        ),
    )


@login_required
@require_http_methods(["POST"])
def tms_romaneio_devolucao(request, pk):
    blocked = k._gate(request, "Devolução")
    if blocked:
        return blocked
    row = get_object_or_404(k.romaneios_qs(request), pk=pk)
    TmsDevolucao.objects.create(
        romaneio=row,
        data=k.parse_date(request.POST.get("data"), k.today()),
        tipo=request.POST.get("tipo") or "parcial",
        motivo=_text(request.POST.get("motivo"))[:240],
        quantidade_nfes=max(0, int(float(request.POST.get("quantidade_nfes") or 0))),
        valor_devolvido=float(request.POST.get("valor_devolvido") or 0),
        paletes_pbr=max(0, int(float(request.POST.get("paletes_pbr") or 0))),
        paletes_chep=max(0, int(float(request.POST.get("paletes_chep") or 0))),
        paletes_descartavel=max(0, int(float(request.POST.get("paletes_descartavel") or 0))),
        descricao=_text(request.POST.get("descricao")),
        responsavel=_text(request.POST.get("responsavel") or k.username(request.user))[:160],
        status=request.POST.get("status") or "aberta",
        criado_por=k.username(request.user),
    )
    _audit(request.user, "registrou_devolucao", "tms_devolucoes", row.pk, f"Devolução {row.numero_romaneio}")
    return redirect(f"/tms/romaneios/{row.pk}/")


@login_required
@require_http_methods(["POST"])
def tms_romaneio_excluir(request, pk):
    blocked = k._gate(request, "Romaneio")
    if blocked:
        return blocked
    row = k.romaneios_qs(request).filter(pk=pk).first()
    if not row:
        return redirect("/tms/romaneios/")
    if row.status not in {"rascunho", "pendente_conferencia", "cancelado"}:
        return k.render_screen(
            request,
            k.blank_screen(
                title="Excluir romaneio",
                error="Só rascunho, pendente de conferência ou cancelado pode ser excluído.",
                actions=[{"href": f"/tms/romaneios/{row.pk}/", "label": "Voltar"}],
            ),
            status=409,
        )
    numero = row.numero_romaneio
    with transaction.atomic():
        erro = recalcular_exclusao(row, k.username(request.user))
        if erro:
            transaction.set_rollback(True)
        else:
            row.delete()
    if erro:
        return k.render_screen(
            request,
            k.blank_screen(title="Excluir romaneio", error=erro, actions=[{"href": f"/tms/romaneios/{pk}/", "label": "Voltar"}]),
            status=422,
        )
    _audit(request.user, "excluiu", "tms_romaneios", pk, numero)
    return redirect("/tms/romaneios/")


@login_required
@require_http_methods(["GET", "POST"])
def api_tms_romaneios(request):
    if not k.allowed(request.user):
        return JsonResponse({"sucesso": False, "error": "Sem permissão."}, status=403)
    if request.method == "POST":
        payload = _json(request)
        if payload is None:
            return JsonResponse({"sucesso": False, "erro": "JSON inválido."}, status=400)
        numero = _text(payload.get("numero_romaneio"))
        loja = _text(payload.get("loja_destino"))
        notas = payload.get("notas") or payload.get("payload_notas") or []
        if not numero or not loja:
            return JsonResponse({"sucesso": False, "erro": "Informe o número do romaneio e a loja."}, status=422)
        if not isinstance(notas, list):
            return JsonResponse({"sucesso": False, "erro": "Informe a lista de notas."}, status=422)
        if TmsRomaneio.objects.filter(numero_romaneio=numero).exists():
            return JsonResponse({"sucesso": False, "erro": "Já existe um romaneio com esse número."}, status=422)
        try:
            ignorar = int(payload.get("ignore_rascunho_id") or 0)
        except (TypeError, ValueError):
            ignorar = 0
        row = TmsRomaneio(
            numero_romaneio=numero[:40],
            data=k.parse_date(str(payload.get("data") or payload.get("data_operacional") or ""), k.today()),
            cd_origem=_text(payload.get("cd_origem") or k.current_cd_code(request))[:7],
            loja_destino=loja[:160],
            motorista=_text(payload.get("motorista"))[:160],
            placa=_text(payload.get("placa")).upper()[:20],
            status="pendente_conferencia",
            criado_por=k.username(request.user),
            atualizado_por=k.username(request.user),
        )
        resultado = criar_romaneio_com_notas(row, notas, k.username(request.user), ignorar)
        return JsonResponse(resultado, status=201 if resultado.get("sucesso") else 422)
    rows = k.romaneios_qs(request).order_by("-data", "-id")[:100]
    return JsonResponse(
        {
            "results": [
                {
                    "id": row.pk,
                    "numero_romaneio": row.numero_romaneio,
                    "status": row.status,
                    "loja_destino": row.loja_destino,
                    "placa": row.placa,
                    "cd_origem": row.cd_origem,
                }
                for row in rows
            ]
        }
    )


@login_required
def api_romaneio_por_chave(request):
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sessão sem permissão."}, status=403)
    chave = _digits(request.GET.get("chave"))
    if len(chave) != 44:
        return JsonResponse({"ok": False, "error": "Informe a chave de acesso de 44 dígitos."}, status=422)
    note = TmsRomaneioNfe.objects.select_related("romaneio").filter(chave_acesso=chave).order_by("-romaneio_id").first()
    if note and k.romaneios_qs(request).filter(pk=note.romaneio_id).exists():
        row = note.romaneio
        return JsonResponse(
            {
                "ok": True,
                "kind": "romaneio",
                "romaneioId": row.pk,
                "numero": row.numero_romaneio,
                "status": row.status,
                "redirectUrl": f"/tms/romaneios/{row.pk}/",
            }
        )
    draft = TmsRascunhoNfe.objects.select_related("rascunho").filter(chave_acesso=chave, rascunho__status="pendente_conferencia").order_by("-id").first()
    if draft:
        return JsonResponse(
            {
                "ok": True,
                "kind": "pre_romaneio",
                "draftId": draft.rascunho_id,
                "status": draft.rascunho.status,
                "redirectUrl": f"/tms/romaneios/rascunhos/?highlight={draft.rascunho_id}",
            }
        )
    return JsonResponse({"ok": False, "error": "Nenhum romaneio vinculado a esta NF-e."}, status=404)


@login_required
@require_http_methods(["POST"])
def api_mdfe_validar(request):
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão para MDF-e."}, status=403)
    payload = _json(request)
    if payload is None:
        return JsonResponse({"error": "JSON inválido."}, status=400)
    errors = _mdfe_errors(payload)
    body = {"ok": not errors, "errors": errors, "payload": _mdfe_payload(payload, [])}
    return JsonResponse(body, status=422 if errors else 200)


@login_required
@require_http_methods(["GET", "POST"])
def api_mdfe_manifestos(request):
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão para MDF-e."}, status=403)
    if request.method == "GET":
        rows = TmsMdfeManifesto.objects.all()[:100]
        return JsonResponse(
            {
                "results": [
                    {
                        "id": row.pk,
                        "numero": row.numero,
                        "serie": row.serie,
                        "status": row.status,
                        "data_emissao": row.data_emissao.isoformat(),
                        "placa": row.placa,
                        "motorista_nome": row.motorista_nome,
                        "uf_carregamento": row.uf_carregamento,
                        "uf_descarregamento": row.uf_descarregamento,
                    }
                    for row in rows
                ]
            }
        )
    payload = _json(request)
    if payload is None:
        return JsonResponse({"error": "JSON inválido ou falha ao salvar o manifesto."}, status=400)
    ids = []
    for raw in payload.get("romaneio_ids") or []:
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if number > 0:
            ids.append(number)
    rows = list(TmsRomaneio.objects.filter(pk__in=ids))
    errors = _mdfe_errors(payload, ids)
    if len(rows) != len(set(ids)):
        errors.append("Um ou mais romaneios não foram encontrados.")
    cds = sorted({_digits(row.cd_origem) or row.cd_origem for row in rows})
    if len(cds) > 1:
        errors.append(
            f"Cada CD fecha a própria carga: este manifesto mistura romaneios de {', '.join(cds)}. "
            "Emita um MDF-e por CD; a placa do caminhão faz o vínculo entre as cargas."
        )
    if errors:
        return JsonResponse({"ok": False, "errors": errors, "payload": _mdfe_payload(payload, rows)}, status=422)
    numero = _text(payload.get("numero")) or f"MDFE-{int(timezone.now().timestamp() * 1000)}"
    documento = _mdfe_payload({**payload, "numero": numero}, rows)
    manifesto = TmsMdfeManifesto.objects.create(
        numero=numero,
        serie=_text(payload.get("serie") or "1")[:8],
        status="rascunho",
        data_emissao=k.parse_date(payload.get("data_emissao"), k.today()),
        emitente_razao_social=_text(payload.get("emitente_razao_social"))[:180],
        emitente_cnpj=_digits(payload.get("emitente_cnpj"))[:14],
        emitente_ie=_text(payload.get("emitente_ie"))[:20],
        uf_carregamento=_text(payload.get("uf_carregamento")).upper()[:2],
        uf_descarregamento=_text(payload.get("uf_descarregamento")).upper()[:2],
        motorista_nome=_text(payload.get("motorista_nome"))[:160],
        motorista_cpf=_digits(payload.get("motorista_cpf"))[:11],
        placa=_text(payload.get("placa")).upper()[:20],
        rntrc=_text(payload.get("rntrc"))[:20],
        lacres=_text(payload.get("lacres"))[:240],
        observacoes=_text(payload.get("observacoes")),
        payload_json=documento,
        criado_por=k.username(request.user),
        atualizado_por=k.username(request.user),
    )
    TmsMdfeRomaneio.objects.bulk_create([TmsMdfeRomaneio(manifesto=manifesto, romaneio=row) for row in rows])
    _audit(request.user, "criou", "tms_mdfe_manifestos", manifesto.pk, f"MDF-e {numero}")
    return JsonResponse({"ok": True, "id": manifesto.pk, "numero": numero, "payload": documento}, status=201)


@login_required
def api_mdfe_detalhe(request, pk):
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão para MDF-e."}, status=403)
    manifesto = TmsMdfeManifesto.objects.filter(pk=pk).first()
    if not manifesto:
        return JsonResponse({"error": "Manifesto não encontrado."}, status=404)
    romaneios = [link.romaneio for link in manifesto.vinculos.select_related("romaneio")]
    return JsonResponse(
        {
            "id": manifesto.pk,
            "numero": manifesto.numero,
            "serie": manifesto.serie,
            "status": manifesto.status,
            "payload": manifesto.payload_json,
            "romaneios": [{"id": row.pk, "numero_romaneio": row.numero_romaneio, "cd_origem": row.cd_origem, "loja_destino": row.loja_destino} for row in romaneios],
        }
    )


@login_required
@require_http_methods(["POST"])
def api_forcar_baixa(request):
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão para forçar baixa por geofence."}, status=403)
    payload = _json(request) or {}
    try:
        viagem_id = int(payload.get("viagem_id") or request.POST.get("viagem_id") or 0)
    except (TypeError, ValueError):
        viagem_id = 0
    if viagem_id <= 0:
        return JsonResponse({"ok": False, "error": "Viagem inválida."}, status=422)
    trip = TmsViagem.objects.filter(pk=viagem_id).first()
    if not trip:
        return JsonResponse({"ok": False, "error": "Viagem não encontrada."}, status=404)
    status = trip.status_logistico or trip.status or ""
    if status not in TRANSITO:
        return JsonResponse({"ok": False, "error": "A baixa manual só está disponível para cargas em trânsito."}, status=422)
    trip.status_logistico = "entregue"
    trip.status = "finalizada"
    trip.save(update_fields=["status_logistico", "status"])
    TmsGeofenceEvento.objects.create(
        viagem=trip,
        geofence_tipo="LOJA",
        loja_codigo="OVERRIDE",
        acao="enter",
        ocorreu_em=timezone.now(),
    )
    _audit(request.user, "forcou_baixa_geofence", "tms_viagens", trip.pk, f"Baixa manual (sinal fraco) na viagem {trip.pk}")
    return JsonResponse({"ok": True, "viagemId": trip.pk, "notice": "Baixa forçada registrada. Ecossistema GPS/Geofence simulado com sucesso."})


@login_required
def tms_viagem_detalhe(request, pk):
    blocked = k._gate(request, "Viagem")
    if blocked:
        return blocked
    trip = TmsViagem.objects.filter(pk=pk).first()
    if not trip:
        return k.render_screen(
            request,
            k.blank_screen(title="Viagem não encontrada", error="Esta viagem pode ter sido encerrada ou o identificador não existe.", actions=[{"href": "/tms/expedicao/", "label": "Central de Expedição"}]),
            status=404,
        )
    paradas = list(trip.paradas.order_by("ordem"))
    romaneio_ids = [item.romaneio_id for item in paradas if item.romaneio_id]
    romaneios = list(TmsRomaneio.objects.filter(pk__in=romaneio_ids))
    vehicle = TmsVeiculo.objects.filter(pk=trip.veiculo_id).first() or TmsVeiculo.objects.filter(placa__iexact=trip.veiculo_id).first()
    in_transit = (trip.status_logistico or trip.status) in TRANSITO
    forms = []
    if in_transit:
        forms.append({"action": "/api/tms/viagens/forcar-baixa", "fields": {"viagem_id": trip.pk}, "label": "Forçar baixa"})
    if trip.tipo_operacao == "transferencia_cd":
        etapa = {
            "embarcado": ("iniciar_transporte", "Iniciar transporte"),
            "em_transporte_cd": ("aguardar_recebimento", "Aguardar recebimento"),
            "aguardando_recebimento": ("confirmar_recebimento", "Confirmar recebimento"),
        }.get(trip.status_transferencia)
        if etapa:
            forms.append({"action": f"/api/tms/transferencias/{trip.pk}/transicoes", "fields": {"action": etapa[0]}, "label": etapa[1]})
    aviso = request.GET.get("aviso") or ""
    erro = request.GET.get("erro") or ""
    return k.render_screen(
        request,
        k.blank_screen(
            title=f"Viagem #{trip.pk}",
            eyebrow="TMS",
            lead=f"{trip.motorista_nome or 'Motorista não informado'} · {trip.veiculo_id or 'sem veículo'}",
            notice=aviso,
            error=erro,
            actions=[
                {"href": f"/tms/viagens/{trip.pk}/mapa/", "label": "Mapa 2D do baú"},
                {"href": "/tms/expedicao/", "label": "Central de Expedição"},
                {"href": "/tms/acompanhamento/", "label": "Acompanhamento"},
            ],
            cards=[
                {"label": "Situação", "value": trip.status_logistico or trip.status, "hint": trip.origem_cd or "-"},
                {"label": "Paletes", "value": sum(item.paletes for item in paradas), "hint": f"capacidade {vehicle.capacidade_max_pallets if vehicle else '-'}"},
                {"label": "Peso", "value": k.num(trip.peso_total_kg), "hint": "kg"},
                {"label": "NF-e", "value": sum(row.quantidade_nfes for row in romaneios), "hint": f"{len(romaneios)} romaneio(s)"},
            ],
            tables=[
                {
                    "title": "Cargas vinculadas",
                    "lead": "",
                    "headers": ["Romaneio", "Loja", "Paletes", "Valor", "Ação"],
                    "rows": [
                        [
                            k.cell(row.numero_romaneio, href=f"/tms/romaneios/{row.pk}/"),
                            k.cell(row.loja_destino or "-"),
                            k.cell(row.total_paletes),
                            k.cell(k.brl(row.valor_total_carga)),
                            k.cell("", forms=forms if index == 0 else []),
                        ]
                        for index, row in enumerate(romaneios)
                    ]
                    or [[k.cell("Nenhum romaneio vinculado."), k.cell(""), k.cell(""), k.cell(""), k.cell("", forms=forms)]],
                }
            ],
        ),
    )


@login_required
@require_http_methods(["POST"])
def api_retorno_rota(request):
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão para lançar retorno de rota."}, status=403)
    payload = _json(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Payload inválido."}, status=422)
    try:
        romaneio_id = int(payload.get("romaneio_id") or 0)
    except (TypeError, ValueError):
        romaneio_id = 0
    paletes = max(0, int(float(payload.get("paletes_pbr") or 0)))
    vasilhames = max(0, int(float(payload.get("vasilhames") or 0)))
    if romaneio_id <= 0:
        return JsonResponse({"ok": False, "error": "Informe o ID do romaneio de retorno."}, status=422)
    if paletes + vasilhames <= 0:
        return JsonResponse({"ok": False, "error": "Informe ao menos 1 palete PBR ou vasilhame."}, status=422)
    row = TmsRomaneio.objects.filter(pk=romaneio_id).first()
    if row:
        TmsDevolucao.objects.create(
            romaneio=row,
            data=k.today(),
            tipo="retorno_rota",
            motivo="Retorno de rota",
            paletes_pbr=paletes,
            paletes_descartavel=vasilhames,
            descricao=f"{paletes} PBR / {vasilhames} vasilhames",
            responsavel=k.username(request.user),
            criado_por=k.username(request.user),
        )
    _audit(
        request.user,
        "lancou_retorno_rota",
        "logistica_reversa",
        romaneio_id,
        f"Retorno de rota ROM {row.numero_romaneio if row else romaneio_id}: {paletes} PBR / {vasilhames} vasilhames",
    )
    return JsonResponse(
        {
            "ok": True,
            "romaneioId": romaneio_id,
            "numero": row.numero_romaneio if row else str(romaneio_id),
            "paletesPbr": paletes,
            "vasilhames": vasilhames,
            "notice": f"Retorno de rota registrado: {paletes} palete(s) PBR e {vasilhames} vasilhame(s).",
        }
    )


@login_required
@require_http_methods(["POST"])
def api_descarga_calcular(request):
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão para calcular descarga."}, status=403)
    payload = _json(request)
    if payload is None:
        return JsonResponse({"error": "JSON inválido."}, status=400)
    calculo = calcular_descarga(
        str(payload.get("modal_descarga") or payload.get("veiculo_tipo") or ""),
        payload.get("quantidade_paletes") or 0,
        payload.get("paletes_fechados_sider") or 0,
        payload.get("paletes_fechados_bau") or 0,
        payload.get("paletes_gerados") or 0,
    )
    return JsonResponse({"ok": True, "descricao": calculo["modal"], **calculo})


@login_required
@require_http_methods(["POST"])
def api_rotas_calcular(request):
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão para calcular rotas."}, status=403)
    payload = _json(request)
    if payload is None:
        return JsonResponse({"error": "Use POST com JSON."}, status=400)
    cd_origem = _digits(payload.get("cd_origem")) or k.current_cd_code(request)[:3]
    lojas = []
    for item in payload.get("lojas") or []:
        code = _text(item).upper()
        if code and code not in lojas:
            lojas.append(code)
    if not cd_origem or not lojas:
        return JsonResponse({"error": "Informe o CD de origem e pelo menos uma loja (ex.: LJ04)."}, status=422)
    from .fusao_motor import RotaErro, calcular_google, chave_google

    chave = chave_google()
    if chave:
        try:
            return JsonResponse(calcular_google(cd_origem, lojas, chave))
        except RotaErro as erro:
            return JsonResponse({"error": str(erro)}, status=erro.status)
    local = _rota_local(cd_origem, lojas)
    if not local:
        return JsonResponse({"error": "Segredo GOOGLE_MAPS_API_KEY não configurado e há loja sem distância na tabela interna."}, status=422)
    return JsonResponse(local)


@login_required
def api_inbox(request):
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão."}, status=403)
    divergencias = k.TmsDivergencia.objects.filter(status="aberta").order_by("-id")[:20]
    return JsonResponse(
        {
            "results": [
                {"tipo": "divergencia", "id": row.pk, "texto": row.descricao, "quando": row.data.isoformat()}
                for row in divergencias
            ]
        }
    )


@login_required
def api_kpis(request):
    from .fusao_motor import responder_kpis

    return responder_kpis(request)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def api_telemetry_ping(request):
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão."}, status=403)
    payload = _json(request) or {}
    try:
        viagem_id = int(payload.get("viagem_id") or 0)
        latitude = float(payload.get("latitude"))
        longitude = float(payload.get("longitude"))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Informe viagem_id, latitude e longitude."}, status=422)
    trip = TmsViagem.objects.filter(pk=viagem_id).first()
    if not trip:
        return JsonResponse({"ok": False, "error": "Viagem não encontrada."}, status=404)
    TmsTelemetria.objects.create(viagem=trip, placa=_text(payload.get("placa") or trip.veiculo_id)[:20], latitude=latitude, longitude=longitude, ocorreu_em=timezone.now())
    return JsonResponse({"ok": True, "viagemId": trip.pk})


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def api_geofence_trigger(request):
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão."}, status=403)
    payload = _json(request) or {}
    try:
        viagem_id = int(payload.get("viagem_id") or 0)
    except (TypeError, ValueError):
        viagem_id = 0
    trip = TmsViagem.objects.filter(pk=viagem_id).first()
    if not trip:
        return JsonResponse({"ok": False, "error": "Viagem não encontrada."}, status=404)
    acao = _text(payload.get("acao") or "enter")[:10]
    TmsGeofenceEvento.objects.create(
        viagem=trip,
        geofence_tipo=_text(payload.get("geofence_tipo") or "LOJA")[:20],
        loja_codigo=_text(payload.get("loja_codigo"))[:40],
        acao=acao or "enter",
        ocorreu_em=timezone.now(),
    )
    cerca_id = _text(payload.get("geofence_id"))
    cerca = TmsGeofence.objects.filter(pk=cerca_id, ativo=True).first() if acao == "enter" and cerca_id else None
    if cerca and cerca.tipo == "LOJA" and trip.status_logistico in TRANSITO:
        trip.status_logistico = "em_descarregamento"
        trip.save(update_fields=["status_logistico"])
    if acao == "enter" and cerca and cerca.tipo in {"LOJA", "CD"}:
        from .fusao_motor import _avisar_operacao

        evento = "chegada_destino" if cerca.tipo == "CD" else "chegada_loja"
        _avisar_operacao(
            trip,
            "Chegada confirmada",
            f"Viagem {trip.veiculo_id or 'sem placa'} chegou a {cerca.nome}. Status: {'aguardando recebimento' if cerca.tipo == 'CD' else 'em descarregamento'}.",
            "/tms/recebimento/" if cerca.tipo == "CD" else "/tms/acompanhamento/",
            evento,
        )
    return JsonResponse({"ok": True})


@login_required
@require_http_methods(["GET", "POST"])
def api_dispositivos(request):
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão."}, status=403)
    if request.method == "POST":
        payload = _json(request) or {}
        acao = _text(payload.get("action") or request.POST.get("action")).lower()
        if acao in {"criar", "revogar"}:
            from .fusao_motor import emitir_dispositivo, revogar_dispositivo

            if acao == "revogar":
                status, corpo = revogar_dispositivo(request.user, _text(payload.get("deviceId") or payload.get("id") or request.POST.get("device_id")))
            else:
                try:
                    driver_id = int(payload.get("driverId") or payload.get("motoristaId") or request.POST.get("driver_id") or 0)
                except (TypeError, ValueError):
                    driver_id = 0
                status, corpo = emitir_dispositivo(request.user, driver_id, _text(payload.get("name") or payload.get("nome") or request.POST.get("name")))
            return JsonResponse(corpo, status=status)
        identificador = _text(payload.get("identificador") or request.POST.get("identificador"))[:80]
        if not identificador:
            return JsonResponse({"error": "Informe o identificador."}, status=422)
        device, _created = LogisticaDispositivo.objects.update_or_create(
            identificador=identificador,
            defaults={
                "nome": _text(payload.get("nome") or request.POST.get("nome"))[:160],
                "tipo": _text(payload.get("tipo") or "celular")[:40],
                "motorista": _text(payload.get("motorista"))[:160],
                "ativo": True,
            },
        )
        return JsonResponse({"ok": True, "id": device.pk, "identificador": device.identificador})
    return JsonResponse(
        {
            "results": [
                {"id": row.pk, "identificador": row.identificador, "nome": row.nome, "motorista": row.motorista, "ativo": row.ativo, "ultimo_ping": row.ultimo_ping.isoformat() if row.ultimo_ping else None}
                for row in LogisticaDispositivo.objects.all()[:100]
            ]
        }
    )


@login_required
@require_http_methods(["POST"])
def api_localizacao(request):
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão."}, status=403)
    payload = _json(request) or {}
    device = LogisticaDispositivo.objects.filter(identificador=_text(payload.get("identificador"))).first()
    if not device:
        return JsonResponse({"error": "Dispositivo não encontrado."}, status=404)
    try:
        device.latitude = float(payload.get("latitude"))
        device.longitude = float(payload.get("longitude"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Latitude e longitude inválidas."}, status=422)
    device.ultimo_ping = timezone.now()
    device.save()
    return JsonResponse({"ok": True, "identificador": device.identificador})


@login_required
def api_motoristas(request):
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão."}, status=403)
    pessoas = PessoaTurno.objects.filter(setor="motorista", cd_unidade__in=k.cd_codes(request)).order_by("funcao")[:80]
    placas = TmsRomaneio.objects.exclude(motorista="").order_by("-data").values_list("motorista", "placa")[:40]
    nomes = {nome for nome, _placa in placas}
    nomes.update(pessoa.funcao for pessoa in pessoas if pessoa.funcao)
    return JsonResponse({"results": sorted(nomes)})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_checkin_removido(request):
    return JsonResponse(
        {"ok": False, "error": "Check-in antigo removido. Use /api/geofence-trigger."},
        status=410,
    )


@login_required
def api_wms_mapa(request):
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão."}, status=403)
    posicoes = WmsPosicao.objects.filter(cd_codigo__in=k.cd_codes(request)).order_by("rua", "nivel", "codigo")
    return JsonResponse(
        {
            "results": [
                {"codigo": row.codigo, "rua": row.rua, "nivel": row.nivel, "status": row.status, "produto": row.produto_nome, "paletes": row.quantidade_paletes}
                for row in posicoes
            ]
        }
    )


@login_required
def page_builder_item(request, pk):
    blocked = k._gate(request, "Page builder")
    if blocked:
        return blocked
    tela = get_object_or_404(PageBuilderTela, pk=pk)
    headers, rows = k._preview_fonte(tela.fonte, request)
    return k.render_screen(
        request,
        k.blank_screen(
            title=tela.nome,
            eyebrow="Page builder",
            lead=dict(k.DATA_SOURCES).get(tela.fonte, tela.fonte),
            actions=[{"href": "/configuracoes/page-builder/", "label": "Catálogo"}, {"href": f"/configuracoes/page-builder/{tela.pk}/visualizar/", "label": "Visualizar"}],
            tables=[{"title": "Prévia", "lead": "", "headers": headers, "rows": rows}],
        ),
    )


@login_required
def page_builder_visualizar(request, pk):
    return page_builder_item(request, pk)


@login_required
@require_http_methods(["POST"])
def sem_saldo_finalizar(request, pk):
    blocked = k._gate(request, "Sem saldo")
    if blocked:
        return blocked
    chamado = k.operacional(ChamadoSaldo, request).filter(pk=pk).first()
    if chamado:
        chamado.status = "concluido"
        chamado.save(update_fields=["status", "atualizado_em"])
    return redirect("/sem-saldo/")


@login_required
@require_http_methods(["POST"])
def sem_saldo_excluir(request, pk):
    blocked = k._gate(request, "Sem saldo")
    if blocked:
        return blocked
    k.operacional(ChamadoSaldo, request).filter(pk=pk).delete()
    return redirect("/sem-saldo/")


@login_required
def tms_mdfe_tela(request):
    blocked = k._gate(request, "MDF-e")
    if blocked:
        return blocked
    rows = list(TmsMdfeManifesto.objects.all()[:40])
    return k.render_screen(
        request,
        k.blank_screen(
            title="MDF-e",
            eyebrow="TMS",
            lead="A validação e o manifesto fechado estão em /api/mdfe/validar e /api/mdfe/manifestos. Cada CD fecha a própria carga.",
            actions=[{"href": "/tms/romaneios/", "label": "Romaneios"}],
            cards=[{"label": "Manifestos", "value": len(rows), "hint": "rascunhos recentes"}],
            tables=[
                {
                    "title": "Manifestos",
                    "lead": "",
                    "headers": ["Número", "Placa", "Motorista", "UF", "Situação"],
                    "rows": [
                        [k.cell(row.numero), k.cell(row.placa), k.cell(row.motorista_nome), k.cell(f"{row.uf_carregamento}→{row.uf_descarregamento}"), k.cell(row.status)]
                        for row in rows
                    ],
                }
            ],
        ),
    )


def erro_executar(message: str) -> str:
    return f"/tms/executar/?erro={quote(message)}"
