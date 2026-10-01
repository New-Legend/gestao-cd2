"""Peças do Worker que passam a rodar dentro do Django do Gestão CD 2.

Google Directions, stream de indicadores, carga XML no mesmo fluxo do e-mail
cargas@wbjp.com.br, compositor de viagem, mapa 2D do baú, posição do
dispositivo, simulação da frota e transição de transferência.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import math
import os
import re
from datetime import timedelta
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections import defaultdict

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import F, Q, Sum
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import (
    AuditLog,
    ChamadoSaldo,
    FrotaDemoSimulacao,
    LogisticaDispositivo,
    PessoaTurno,
    Recebimento,
    Separacao,
    SistemaNotificacao,
    TmsTransferenciaRecebimento,
    TmsGeofence,
    TmsRascunho,
    TmsRascunhoNfe,
    TmsRomaneio,
    TmsRota,
    TmsTelemetria,
    TmsVeiculo,
    TmsViagem,
    TmsViagemParada,
)
from .romaneio_logica import classificar_nota, mesclar_notas, montar_payload, parse_nfe_xml, produtos_xml

CAIXA_CARGAS = "cargas@wbjp.com.br"
APK_URL = os.environ.get(
    "DRIVER_APP_APK_URL",
    "https://expo.dev/artifacts/eas/20Sxa0NqwhMnRQ8vkVKG_GZ55uB6mvhbNuJgqzjp7jI.apk",
).strip()


class RotaErro(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _k():
    from . import krill_telas as k

    return k


def _json_body(request):
    try:
        payload = json.loads(request.body.decode() or "{}")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _digitos(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _loja_codigo(texto: str) -> str:
    bruto = (texto or "").strip().upper()
    achou = re.search(r"LJ\d+", bruto)
    if achou:
        return achou.group(0)
    return bruto.split()[0] if bruto else ""


def chave_google() -> str:
    chave = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not chave or chave == "SUA_CHAVE_AQUI":
        return ""
    return chave


def _http_json(url: str) -> tuple[int, dict]:
    pedido = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(pedido, timeout=12) as resposta:
            return resposta.status, json.loads(resposta.read().decode() or "{}")
    except urllib.error.HTTPError as erro:
        bruto = erro.read().decode() or "{}"
        try:
            payload = json.loads(bruto)
        except json.JSONDecodeError:
            payload = {}
        return erro.code, payload if isinstance(payload, dict) else {}


def _coordenada(latitude, longitude):
    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return {"latitude": lat, "longitude": lng}


def _parametro(coordenada: dict) -> str:
    return f"{coordenada['latitude']:.6f},{coordenada['longitude']:.6f}"


def coordenada_cd(cd: str):
    digitos = _digitos(cd)[-3:]
    if not digitos:
        return None
    row = TmsGeofence.objects.filter(id=f"geo_cd_{digitos}", tipo="CD", ativo=True).first()
    if not row:
        return None
    return _coordenada(row.latitude, row.longitude)


def coordenadas_lojas(cd: str, lojas: list[str]) -> dict:
    codigos = {loja.upper() for loja in lojas if loja}
    achadas = {}
    if not codigos:
        return achadas
    consulta = TmsRota.objects.filter(ativa=True).order_by("id")
    preferidas = []
    outras = []
    for row in consulta:
        codigo = (row.loja_codigo or "").upper()
        if codigo not in codigos:
            continue
        if (row.cd_origem or "").endswith(_digitos(cd)[-3:]):
            preferidas.append(row)
        else:
            outras.append(row)
    for row in preferidas + outras:
        codigo = (row.loja_codigo or "").upper()
        if codigo in achadas:
            continue
        ponto = _coordenada(row.latitude, row.longitude)
        if ponto:
            achadas[codigo] = ponto
    return achadas


def _mensagem_coordenadas(cd: str, cd_falta: bool, lojas: list[str]) -> str:
    partes = []
    if cd_falta:
        partes.append(f"o {cd} não tem coordenadas ativas em Geofences")
    if lojas:
        partes.append(f"sem coordenadas em Rotas TMS: {', '.join(lojas)}")
    return f"Cálculo bloqueado: {'; '.join(partes)}. Cadastre latitude e longitude no cadastro mestre; endereço em texto não é usado."


def calcular_google(cd_origem: str, lojas: list[str], api_key: str) -> dict:
    if len(lojas) > 25:
        raise RotaErro("O limite é de 25 destinos por cálculo.")
    origem = coordenada_cd(cd_origem)
    pontos = coordenadas_lojas(cd_origem, lojas)
    faltando = [loja for loja in lojas if loja.upper() not in pontos]
    if not origem or faltando:
        raise RotaErro(_mensagem_coordenadas(cd_origem, not origem, faltando))
    destinos = [pontos[loja.upper()] for loja in lojas]
    paradas = [_parametro(ponto) for ponto in destinos]
    consulta = {
        "origin": _parametro(origem),
        "destination": paradas[-1],
        "mode": "driving",
        "language": "pt-BR",
        "key": api_key,
    }
    if len(paradas) > 1:
        consulta["waypoints"] = "optimize:true|" + "|".join(paradas[:-1])
    url = "https://maps.googleapis.com/maps/api/directions/json?" + urllib.parse.urlencode(consulta)
    status, payload = _http_json(url)
    rota = (payload.get("routes") or [None])[0] if isinstance(payload, dict) else None
    pernas = (rota or {}).get("legs") or []
    if status != 200 or payload.get("status") != "OK" or not pernas:
        raise RotaErro(f"Google Maps retornou: {payload.get('status') or f'HTTP {status}'}.", status=502)
    ordem = list(rota.get("waypoint_order") or [])
    if ordem:
        lojas_ordem = [lojas[indice] for indice in ordem] + [lojas[-1]]
    else:
        lojas_ordem = list(lojas)
    distancia = round(sum(float((perna.get("distance") or {}).get("value") or 0) for perna in pernas) / 1000, 2)
    minutos = int(-(-sum(float((perna.get("duration") or {}).get("value") or 0) for perna in pernas) // 60))
    return {
        "ok": True,
        "fonte": "google",
        "cd_origem": cd_origem,
        "origem": origem,
        "lojas": lojas_ordem,
        "destinos": destinos,
        "distance_km": distancia,
        "duration_minutes": minutos,
        "waypoint_order": ordem,
    }


def otimizar_google(rows, cd: str, api_key: str) -> list:
    lojas = []
    for row in rows:
        codigo = _loja_codigo(row.loja_destino)
        if codigo and codigo not in lojas:
            lojas.append(codigo)
    if len(rows) < 2:
        raise RotaErro("É necessário ter pelo menos 2 destinos para otimizar a rota.")
    calculo = calcular_google(cd, lojas, api_key)
    ordem = {codigo: indice for indice, codigo in enumerate(calculo["lojas"])}
    ranked = sorted(rows, key=lambda row: (ordem.get(_loja_codigo(row.loja_destino), 10**6), row.id))
    trechos = max(len(calculo["lojas"]), 1)
    for index, row in enumerate(ranked, start=1):
        row.ordem_entrega = index
        row.distancia_rota_km = round(calculo["distance_km"] / trechos, 2)
        row.tempo_rota_minutos = max(1, int(calculo["duration_minutes"] / trechos))
        row.save(update_fields=["ordem_entrega", "distancia_rota_km", "tempo_rota_minutos", "updated_at"])
    return ranked


def foto_kpis(cd: str) -> dict:
    codigo = _digitos(cd)[-3:] or "806"
    desde = timezone.localdate() - timezone.timedelta(days=7)
    abertos = ChamadoSaldo.objects.filter(cd_unidade=codigo, status="aberto", data__gte=desde)
    ruptura = abertos.aggregate(total=Sum("diferenca")).get("total") or 0
    unidades = Separacao.objects.filter(cd_unidade=codigo, data__gte=desde).aggregate(total=Sum("unidades")).get("total") or 0
    return {
        "cd": codigo,
        "pendencias": abertos.count(),
        "ruptura": float(abs(ruptura)),
        "funcionarios": PessoaTurno.objects.filter(cd_unidade=codigo).count(),
        "unidades": float(unidades),
        "updatedAt": timezone.now().isoformat(),
    }


def responder_kpis(request):
    k = _k()
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão para acompanhar os indicadores."}, status=403)
    pedido = request.GET.get("cd") or k.current_cd_code(request)
    codigo = _digitos(pedido)[-3:]
    if codigo and codigo not in k.cd_codes(request) and not request.user.is_superuser:
        return JsonResponse({"error": "CD não autorizado."}, status=403)
    foto = foto_kpis(codigo or k.current_cd_code(request))
    aceita = request.headers.get("Accept", "")
    if "text/event-stream" in aceita or request.GET.get("stream") == "1":

        def eventos():
            yield "retry: 15000\n"
            yield f"data: {json.dumps(foto_kpis(foto['cd']), ensure_ascii=False)}\n\n"

        resposta = StreamingHttpResponse(eventos(), content_type="text/event-stream")
        resposta["Cache-Control"] = "no-cache"
        resposta["X-Accel-Buffering"] = "no"
        return resposta
    romaneios = k.romaneios_qs(request)
    foto.update(
        {
            "ok": True,
            "fonte": "stream",
            "romaneios_abertos": romaneios.filter(status__in=k.ABERTOS).count(),
            "em_transporte": romaneios.filter(status="em_transporte").count(),
            "divergencias_abertas": k.TmsDivergencia.objects.filter(status="aberta").count(),
        }
    )
    return JsonResponse(foto)


def _xmls_de_arquivos(arquivos) -> list[tuple[str, str]]:
    saida = []
    for arquivo in arquivos:
        nome = getattr(arquivo, "name", None) or "NF-e"
        bruto = arquivo.read()
        if nome.lower().endswith(".zip"):
            try:
                pacote = zipfile.ZipFile(io.BytesIO(bruto))
            except zipfile.BadZipFile:
                continue
            for item in pacote.namelist():
                if item.lower().endswith(".xml"):
                    saida.append((item, pacote.read(item).decode("utf-8", errors="replace")))
            continue
        if isinstance(bruto, bytes):
            bruto = bruto.decode("utf-8", errors="replace")
        saida.append((nome, bruto))
    return saida


def gravar_xmls(arquivos, remetente: str, assunto: str) -> dict:
    pares = _xmls_de_arquivos(arquivos)
    if not pares:
        return {"ok": False, "error": "Nenhum anexo XML ou ZIP com XML de NF-e foi encontrado."}
    notas = []
    erros = []
    for nome, xml in pares:
        parsed = parse_nfe_xml(xml, nome)
        if not parsed.get("ok"):
            erros.append(parsed.get("error") or f"{nome}: XML inválido.")
            continue
        notas.append(classificar_nota(parsed["nf"], produtos_xml(xml)))
    if not notas:
        return {"ok": False, "error": "Os anexos não contêm uma NF-e XML válida."}
    grupos = defaultdict(list)
    for nota in notas:
        grupos[(nota.get("emitenteCnpj"), nota.get("destinatarioCnpj"), nota.get("dataEmissao"))].append(nota)
    salvas = 0
    ignoradas = 0
    criados = 0
    for grupo in grupos.values():
        mescla = mesclar_notas([], grupo)
        erros.extend(mescla["erros"])
        if mescla["erros"] or not mescla["notas"]:
            continue
        montado = montar_payload(mescla["notas"], mescla["duplicadas"])
        if not montado.get("ok"):
            erros.append(montado.get("error") or "Carga ignorada.")
            ignoradas += len(mescla["notas"])
            continue
        payload = montado["payload"]
        rascunho = TmsRascunho.objects.create(
            status="pendente_conferencia",
            cd_origem=str(payload["cdOrigem"])[:7],
            loja_destino=str(payload["lojaDestino"])[:160],
        )
        novas = 0
        for nota in payload["notas"]:
            _, created = TmsRascunhoNfe.objects.get_or_create(rascunho=rascunho, chave_acesso=nota["chaveAcesso"])
            if created:
                novas += 1
            else:
                ignoradas += 1
        if not novas:
            rascunho.delete()
            continue
        salvas += novas
        criados += 1
        ignoradas += int(montado.get("duplicateCount") or 0)
    if not criados:
        return {"ok": False, "error": erros[0] if erros else "Nenhuma NF-e nova foi gravada.", "erros": erros}
    return {"ok": True, "salvas": salvas, "ignoradas": ignoradas, "rascunhos": criados, "erros": erros[:8]}


def _veiculo(valor: str):
    texto = (valor or "").strip()
    if not texto:
        return None
    return TmsVeiculo.objects.filter(ativo=True, placa__iexact=texto).first() or TmsVeiculo.objects.filter(ativo=True, id=texto).first()


def _motorista(payload: dict) -> str:
    nome = str(payload.get("motorista_nome") or payload.get("motorista") or "").strip()
    if nome:
        return nome[:160]
    bruto = str(payload.get("motorista_usuario_id") or "").strip()
    if bruto.isdigit():
        usuario = User.objects.filter(pk=int(bruto)).first()
        if usuario:
            return (usuario.get_full_name() or usuario.username)[:160]
        return ""
    return bruto[:160]


def _romaneios_livres(ids=None):
    ligados = set(TmsViagemParada.objects.exclude(romaneio_id=None).values_list("romaneio_id", flat=True))
    consulta = TmsRomaneio.objects.exclude(status__in=["cancelado", "finalizado", "entregue"])
    if ids is not None:
        consulta = consulta.filter(pk__in=ids)
    return [row for row in consulta.order_by("-id") if row.pk not in ligados]


@login_required
def api_compositor(request):
    k = _k()
    if request.method != "GET":
        return JsonResponse({"ok": False, "error": "Método não permitido."}, status=405)
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão para compor viagens."}, status=403)
    romaneios = _romaneios_livres()
    rascunhos = list(TmsRascunho.objects.filter(status="pendente_conferencia").order_by("-id")[:80])
    motoristas = sorted({pessoa.funcao for pessoa in PessoaTurno.objects.filter(setor="motorista") if pessoa.funcao})
    motoristas.extend(sorted({row.motorista for row in TmsRomaneio.objects.exclude(motorista="")[:80] if row.motorista}))
    vistos = []
    for nome in motoristas:
        if nome not in vistos:
            vistos.append(nome)
    return JsonResponse(
        {
            "ok": True,
            "permissions": {"mixed": True, "transfer": True},
            "allowPalletCapacityOverride": os.environ.get("TMS_ALLOW_PALLET_OVERRIDE") == "1",
            "allowBottomUpCatalogBypass": False,
            "cds": k.cd_codes(request),
            "sessionCd": k.current_cd_code(request),
            "drivers": [{"id": nome, "name": nome} for nome in vistos],
            "vehicles": [
                {
                    "plate": row.placa,
                    "maxKg": row.capacidade_max_kg,
                    "maxPallets": row.capacidade_max_pallets,
                    "maxM3": row.capacidade_max_m3,
                }
                for row in TmsVeiculo.objects.filter(ativo=True).order_by("placa")
            ],
            "manifests": [
                {
                    "id": row.pk,
                    "number": row.numero_romaneio,
                    "cd": row.cd_origem,
                    "storeCode": _loja_codigo(row.loja_destino),
                    "store": row.loja_destino,
                    "pallets": row.total_paletes,
                    "weightKg": row.peso_bruto_kg,
                    "notes": ", ".join(row.nfes.values_list("numero", flat=True)[:8]),
                }
                for row in romaneios[:80]
            ],
            "pendingDrafts": [
                {
                    "id": row.pk,
                    "number": f"PRE-{row.pk}",
                    "cd": row.cd_origem,
                    "store": row.loja_destino,
                    "nfes": row.nfes.count(),
                    "volumes": 0,
                    "weightKg": 0,
                    "pallets": 0,
                }
                for row in rascunhos
            ],
        }
    )


def _planejar(veiculo: TmsVeiculo, romaneios: list[TmsRomaneio]) -> dict:
    paletes = sum(int(row.total_paletes or 0) for row in romaneios)
    peso = sum(float(row.peso_bruto_kg or 0) for row in romaneios)
    slots = []
    esquerda = 0.0
    direita = 0.0
    total = len(romaneios)
    for indice, row in enumerate(romaneios, start=1):
        lado = "left" if indice % 2 else "right"
        nota = row.nfes.order_by("id").first()
        peso_row = float(row.peso_bruto_kg or 0)
        if lado == "left":
            esquerda += peso_row
        else:
            direita += peso_row
        slots.append(
            {
                "position": (indice + 1) // 2,
                "side": lado,
                "empty": False,
                "loadingOrder": indice,
                "deliveryOrder": total - indice + 1,
                "storeCode": _loja_codigo(row.loja_destino),
                "storeName": row.loja_destino,
                "estimatedWeightKg": peso_row,
                "notaFiscal": nota.numero if nota else "",
                "palletCount": int(row.total_paletes or 0),
            }
        )
    override = os.environ.get("TMS_ALLOW_PALLET_OVERRIDE") == "1" and paletes > int(veiculo.capacidade_max_pallets or 0)
    return {
        "ok": True,
        "totals": {"pallets": paletes, "weightKg": round(peso, 2)},
        "vehicle": {
            "plate": veiculo.placa,
            "capacityPallets": veiculo.capacidade_max_pallets,
            "maxWeightKg": veiculo.capacidade_max_kg,
            "capacityWeightKg": veiculo.capacidade_max_kg,
        },
        "plan": {"slots": slots, "balanceDifferenceKg": round(abs(esquerda - direita), 2)},
        "capacityOverrideApplied": bool(override and peso <= float(veiculo.capacidade_max_kg or 0)),
    }


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def api_planejamento(request):
    k = _k()
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão para compor viagens."}, status=403)
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "JSON inválido."}, status=400)
    veiculo = _veiculo(str(payload.get("veiculo_id") or payload.get("placa") or ""))
    if not veiculo:
        return JsonResponse({"ok": False, "error": "Veículo não encontrado para esta placa."}, status=422)
    ids = [int(item) for item in payload.get("romaneio_ids") or [] if str(item).isdigit() and int(item) > 0]
    romaneios = list(TmsRomaneio.objects.filter(pk__in=ids))
    if not romaneios:
        return JsonResponse({"ok": False, "error": "Selecione ao menos um romaneio."}, status=422)
    return JsonResponse(_planejar(veiculo, romaneios))


def _compor(payload: dict, transferencia: bool, username: str) -> dict:
    veiculo = _veiculo(str(payload.get("veiculo_id") or payload.get("placa") or ""))
    if not veiculo:
        return {"ok": False, "error": "Veículo não encontrado para esta placa.", "status": 422}
    motorista = _motorista(payload)
    if not motorista:
        return {"ok": False, "error": "Selecione o motorista.", "status": 422}
    ids = [int(item) for item in payload.get("romaneio_ids") or [] if str(item).isdigit() and int(item) > 0]
    romaneios = _romaneios_livres(ids)
    if len(romaneios) != len(ids) or not romaneios:
        return {"ok": False, "error": "Um ou mais romaneios não estão livres para composição.", "status": 422}
    origem = _digitos(payload.get("origem_cd"))[-3:] or (romaneios[0].cd_origem or "")
    destino = _digitos(payload.get("destino_cd"))[-3:]
    if transferencia and (not origem or not destino or origem == destino):
        return {"ok": False, "error": "Informe CD de origem e CD de destino diferentes.", "status": 422}
    plano = _planejar(veiculo, romaneios)
    peso = plano["totals"]["weightKg"]
    paletes = plano["totals"]["pallets"]
    if veiculo.capacidade_max_kg and peso > veiculo.capacidade_max_kg:
        return {"ok": False, "error": "Peso acima do limite do veículo.", "status": 422}
    if veiculo.capacidade_max_pallets and paletes > veiculo.capacidade_max_pallets and not plano["capacityOverrideApplied"]:
        return {"ok": False, "error": "Capacidade de paletes excedida.", "status": 422}
    loja = f"CD {destino}" if transferencia else (romaneios[0].loja_destino or "")
    viagem = TmsViagem.objects.create(
        motorista_nome=motorista,
        veiculo_id=veiculo.placa,
        loja_codigo=_loja_codigo(loja)[:40],
        loja_nome=(loja or "")[:160],
        origem_cd=origem[:7],
        cd_atual=origem[:7],
        destino_cd=destino[:7] if transferencia else "",
        tipo_operacao="transferencia_cd" if transferencia else "entrega",
        status_transferencia="embarcado" if transferencia else "",
        status="atribuida",
        status_logistico="em_patio",
        peso_total_kg=peso,
    )
    for ordem, row in enumerate(romaneios, start=1):
        TmsViagemParada.objects.create(
            id=f"parada-{viagem.pk}-{ordem}",
            viagem=viagem,
            ordem=ordem,
            romaneio_id=row.pk,
            paletes=int(row.total_paletes or 0),
            status="pendente",
        )
        row.placa = veiculo.placa
        row.motorista = motorista
        row.atualizado_por = username[:160]
        campos = ["placa", "motorista", "atualizado_por", "updated_at"]
        if transferencia and row.status not in {"cancelado", "finalizado"}:
            row.status = "em_transferencia"
            campos.append("status")
        row.save(update_fields=campos)
    if transferencia:
        _avisar_operacao(
            viagem,
            "Transferência embarcada",
            f"Transferência #{viagem.pk} saiu do planejamento: CD {origem} para CD {destino}, placa {veiculo.placa}.",
            f"/tms/viagens/{viagem.pk}/",
            "transferencia_embarcada",
        )
    else:
        _avisar_operacao(
            viagem,
            "Nova viagem",
            f"Viagem #{viagem.pk} vinculada à placa {veiculo.placa}, motorista {motorista}.",
            f"/tms/viagens/{viagem.pk}/",
            "nova_viagem",
        )
    aviso = f"Composição confirmada na viagem {viagem.pk}."
    if transferencia:
        aviso = f"Transferência #{viagem.pk} embarcada e romaneios bloqueados para nova composição."
    return {
        "ok": True,
        "viagemId": viagem.pk,
        "status": "embarcado" if transferencia else "atribuida",
        "notice": aviso,
    }


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def api_carga_mista(request):
    k = _k()
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão para compor viagens."}, status=403)
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "JSON inválido."}, status=400)
    resultado = _compor(payload, False, k.username(request.user))
    status = resultado.pop("status") if isinstance(resultado.get("status"), int) else 200
    return JsonResponse(resultado, status=status)


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def api_transferencia(request):
    k = _k()
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão para transferir."}, status=403)
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "JSON inválido."}, status=400)
    resultado = _compor(payload, True, k.username(request.user))
    status = resultado.pop("status") if isinstance(resultado.get("status"), int) else 200
    return JsonResponse(resultado, status=status)


@login_required
def tms_viagens(request):
    k = _k()
    blocked = k._gate(request, "Compor viagem")
    if blocked:
        return blocked
    aviso = ""
    if request.method == "POST":
        ids = request.POST.getlist("romaneio_id")
        payload = {
            "veiculo_id": request.POST.get("placa") or "",
            "motorista_nome": request.POST.get("motorista") or "",
            "origem_cd": request.POST.get("origem_cd") or "",
            "destino_cd": request.POST.get("destino_cd") or "",
            "romaneio_ids": ids,
        }
        resultado = _compor(payload, request.POST.get("modo") == "transferencia", k.username(request.user))
        if resultado.get("ok"):
            return redirect(f"/tms/viagens/{resultado['viagemId']}/")
        aviso = resultado.get("error") or "Não foi possível compor a viagem."
    romaneios = _romaneios_livres()[:40]
    veiculos = list(TmsVeiculo.objects.filter(ativo=True).order_by("placa"))
    return k.render_screen(
        request,
        k.blank_screen(
            title="Compor viagem",
            eyebrow="TMS",
            lead="A mesma composição do Gestão CD: uma placa, um motorista e os romaneios livres. Transferência pede dois CDs.",
            actions=[{"href": "/tms/viagens/vincular/", "label": "Vincular"}, {"href": "/tms/romaneios/", "label": "Romaneios"}],
            extra_html=_html_compositor_bau(),
            cards=[{"label": "Livres", "value": len(romaneios), "hint": "romaneios sem viagem"}, {"label": "Veículos", "value": len(veiculos), "hint": "placas ativas"}],
            form={
                "action": "/tms/viagens/",
                "title": "Nova composição",
                "submit": "Confirmar composição",
                "fields": [
                    {"name": "modo", "label": "Operação", "type": "select", "value": "mista", "options": [("mista", "Entrega multi-loja"), ("transferencia", "Transferência CD a CD")]},
                    {"name": "motorista", "label": "Motorista", "type": "text", "value": "", "required": True},
                    {"name": "placa", "label": "Placa", "type": "text", "value": "", "required": True},
                    {"name": "origem_cd", "label": "CD origem", "type": "text", "value": k.current_cd_code(request)},
                    {"name": "destino_cd", "label": "CD destino", "type": "text", "value": ""},
                    {
                        "name": "romaneio_id",
                        "label": "Romaneios",
                        "type": "select",
                        "multiple": True,
                        "value": "",
                        "required": True,
                        "options": [(str(row.pk), f"{row.numero_romaneio} · {row.loja_destino} · {row.total_paletes} pal") for row in romaneios],
                    },
                ],
            },
            tables=[
                {
                    "title": "Romaneios livres",
                    "lead": aviso,
                    "headers": ["Número", "CD", "Loja", "Paletes", "Peso"],
                    "rows": [
                        [k.cell(row.numero_romaneio), k.cell(row.cd_origem), k.cell(row.loja_destino), k.cell(row.total_paletes), k.cell(row.peso_bruto_kg)]
                        for row in romaneios
                    ],
                }
            ],
        ),
    )


def _fmt_num(valor) -> str:
    numero = float(valor or 0)
    if abs(numero - round(numero)) < 0.05:
        texto = f"{int(round(numero)):,}"
    else:
        texto = f"{numero:,.1f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _slots_ordenados(plano: dict) -> list[dict]:
    slots = list((plano.get("plan") or {}).get("slots") or [])
    return sorted(slots, key=lambda slot: (int(slot.get("position") or 0), 0 if slot.get("side") == "left" else 1))


def html_planta_bau(plano: dict, placa: str = "") -> str:
    slots = _slots_ordenados(plano)
    totais = plano.get("totals") or {}
    veiculo = plano.get("vehicle") or {}
    ultima = max((int(slot.get("position") or 0) for slot in slots), default=0)
    placas = escape(placa or veiculo.get("plate") or "Veículo")
    cartoes = []
    for slot in slots:
        vazio = bool(slot.get("empty"))
        lado = "Esq." if slot.get("side") == "left" else "Dir."
        loja = "Posição livre" if vazio else f"{slot.get('storeCode') or ''} · {slot.get('storeName') or ''}"
        meta = ""
        if not vazio:
            meta = f"{_fmt_num(slot.get('estimatedWeightKg'))} kg · NF-e {slot.get('notaFiscal') or '—'} · entrega {slot.get('deliveryOrder') or '—'}ª"
        cartoes.append(
            "<article class=\"load-plan-slot{vazio}\">"
            "<div class=\"load-plan-slot-head\"><span>P{pos} · {lado}</span><span>{estado}</span></div>"
            "<strong class=\"load-plan-slot-store\">{loja}</strong>"
            "<div class=\"load-plan-slot-meta\">{meta}</div></article>".format(
                vazio=" is-empty" if vazio else "",
                pos=int(slot.get("position") or 0),
                lado=lado,
                estado="Livre" if vazio else f"Carga {slot.get('loadingOrder') or ''}",
                loja=escape(loja),
                meta=escape(meta),
            )
        )
    entrega = sorted([slot for slot in slots if not slot.get("empty")], key=lambda slot: int(slot.get("deliveryOrder") or 0))
    carga = sorted([slot for slot in slots if not slot.get("empty")], key=lambda slot: int(slot.get("loadingOrder") or 0))
    itens_entrega = "".join(
        f"<li><strong>{escape(str(slot.get('deliveryOrder')))}ª · {escape(str(slot.get('storeCode') or ''))} - {escape(str(slot.get('storeName') or ''))}</strong>"
        "<small>Descarregar nesta ordem, a partir da porta.</small></li>"
        for slot in entrega
    ) or "<li>Nenhuma carga posicionada.</li>"
    itens_carga = "".join(
        f"<li><strong>{escape(str(slot.get('loadingOrder')))}ª · {escape(str(slot.get('storeCode') or ''))} - {escape(str(slot.get('storeName') or ''))}</strong>"
        f"<small>{escape(_fmt_num(slot.get('palletCount')))} paletes · {escape(_fmt_num(slot.get('estimatedWeightKg')))} kg · entra primeiro junto à cabine.</small></li>"
        for slot in carga
    ) or "<li>Nenhuma carga posicionada.</li>"
    return (
        "<div class=\"load-plan-summary\">"
        f"<span><small>Ocupação</small><strong>{escape(_fmt_num(totais.get('pallets')))} / {escape(_fmt_num(veiculo.get('capacityPallets')))} paletes</strong></span>"
        f"<span><small>Peso total</small><strong>{escape(_fmt_num(totais.get('weightKg')))} kg</strong></span>"
        f"<span><small>Diferença lateral</small><strong>{escape(_fmt_num((plano.get('plan') or {}).get('balanceDifferenceKg')))} kg</strong></span>"
        "</div>"
        f"<section class=\"load-plan-truck\" aria-label=\"Planta do baú do {placas}\">"
        "<div class=\"load-plan-cab\"><i aria-hidden=\"true\"></i><strong>Cabine do motorista</strong></div>"
        "<div class=\"load-plan-body\"><div class=\"load-plan-grid\">"
        + ("".join(cartoes) or "<article class=\"load-plan-slot is-empty\"><strong class=\"load-plan-slot-store\">Baú vazio</strong></article>")
        + "</div></div>"
        f"<div class=\"load-plan-door\"><i aria-hidden=\"true\"></i><strong>Portas do baú · última posição P{ultima or '—'}</strong></div>"
        "</section>"
        "<section class=\"load-plan-details\"><div><h3>Sequência de entrega</h3><ol>"
        f"{itens_entrega}</ol></div><div><h3>Regra de carregamento</h3><ol>{itens_carga}</ol></div></section>"
    )


def _html_compositor_bau() -> str:
    return """
<section class="krill-panel load-plan-panel" data-bau-painel>
  <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:end">
    <div>
      <h3>Mapa 2D do baú</h3>
      <p class="load-plan-status" data-bau-status>Selecione a placa e os romaneios. P1 fica junto à cabine; a última posição, junto à porta.</p>
    </div>
    <button class="load-plan-open" type="button" data-bau-abrir>Montar mapa 2D</button>
  </div>
  <div data-bau-planta></div>
</section>
<script>
(function () {
  var painel = document.querySelector("[data-bau-painel]");
  var botao = document.querySelector("[data-bau-abrir]");
  if (!painel || !botao) return;
  function token() {
    var campo = document.querySelector("[name=csrfmiddlewaretoken]");
    return campo ? campo.value : "";
  }
  function selecionados() {
    var lista = document.querySelector(".krill-form select[name=romaneio_id]");
    if (!lista) return [];
    return Array.prototype.filter.call(lista.options, function (opcao) { return opcao.selected; }).map(function (opcao) { return Number(opcao.value); });
  }
  botao.addEventListener("click", function () {
    var formulario = document.querySelector(".krill-form");
    var placa = formulario ? (formulario.querySelector("[name=placa]") || {}).value || "" : "";
    var status = painel.querySelector("[data-bau-status]");
    var planta = painel.querySelector("[data-bau-planta]");
    status.textContent = "Calculando as posições no baú...";
    botao.disabled = true;
    fetch("/api/viagens/planejamento-carga", {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json", "Accept": "application/json", "X-CSRFToken": token()},
      body: JSON.stringify({veiculo_id: placa, romaneio_ids: selecionados()})
    }).then(function (resposta) {
      return resposta.json().then(function (payload) {
        if (!resposta.ok || !payload.ok) throw new Error(payload.error || "Não foi possível montar o mapa.");
        return payload;
      });
    }).then(function (payload) {
      return fetch("/tms/viagens/mapa-previa/", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json", "Accept": "text/html", "X-CSRFToken": token()},
        body: JSON.stringify(payload)
      }).then(function (resposta) { return resposta.text(); });
    }).then(function (html) {
      planta.innerHTML = html;
      status.textContent = "Vista superior do baú. P1 junto à cabine.";
    }).catch(function (erro) {
      status.textContent = erro && erro.message ? erro.message : "Falha ao montar o mapa.";
    }).finally(function () { botao.disabled = false; });
  });
})();
</script>
"""


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def tms_mapa_previa(request):
    k = _k()
    if not k.allowed(request.user):
        return HttpResponse("Sem permissão para ver o mapa do baú.", status=403)
    payload = _json_body(request)
    if not payload or not payload.get("plan"):
        return HttpResponse("Mapa indisponível.", status=422)
    placa = str((payload.get("vehicle") or {}).get("plate") or "")
    return HttpResponse(html_planta_bau(payload, placa))


def _plano_da_viagem(viagem: TmsViagem) -> dict:
    paradas = list(viagem.paradas.order_by("ordem", "id"))
    ids = [parada.romaneio_id for parada in paradas if parada.romaneio_id]
    romaneios = list(TmsRomaneio.objects.filter(pk__in=ids))
    ordem = {pk: indice for indice, pk in enumerate(ids)}
    romaneios.sort(key=lambda row: ordem.get(row.pk, 999))
    veiculo = _veiculo(viagem.veiculo_id)
    if not veiculo:
        veiculo = TmsVeiculo(
            id="avulso",
            placa=viagem.veiculo_id or "—",
            capacidade_max_kg=0,
            capacidade_max_pallets=0,
            capacidade_max_m3=0,
            ativo=True,
        )
    if not romaneios:
        return {
            "ok": True,
            "totals": {"pallets": 0, "weightKg": 0},
            "vehicle": {"plate": veiculo.placa, "capacityPallets": veiculo.capacidade_max_pallets, "maxWeightKg": veiculo.capacidade_max_kg},
            "plan": {"slots": [], "balanceDifferenceKg": 0},
        }
    return _planejar(veiculo, romaneios)


@login_required
def tms_viagem_mapa(request, pk):
    k = _k()
    blocked = k._gate(request, "Mapa do baú")
    if blocked:
        return blocked
    viagem = TmsViagem.objects.filter(pk=pk).first()
    if not viagem:
        return k.render_screen(
            request,
            k.blank_screen(title="Viagem não encontrada", error="Esta viagem pode ter sido encerrada ou o identificador não existe.", actions=[{"href": "/tms/viagens/", "label": "Compor viagem"}]),
            status=404,
        )
    plano = _plano_da_viagem(viagem)
    return k.render_screen(
        request,
        k.blank_screen(
            title=f"Mapa 2D · viagem #{viagem.pk}",
            eyebrow="TMS / Veículo",
            lead="Vista superior do baú. P1 fica junto à cabine; a última posição, junto à porta.",
            actions=[{"href": f"/tms/viagens/{viagem.pk}/", "label": "Voltar à viagem"}, {"href": "/tms/viagens/", "label": "Compor viagem"}],
            extra_html=html_planta_bau(plano, viagem.veiculo_id),
        ),
    )


def _haversine(lat1, lng1, lat2, lng2) -> float:
    radianos = math.radians
    delta_lat = radianos(lat2 - lat1)
    delta_lng = radianos(lng2 - lng1)
    a = math.sin(delta_lat / 2) ** 2 + math.cos(radianos(lat1)) * math.cos(radianos(lat2)) * math.sin(delta_lng / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def _segredo_geofence() -> str:
    return os.environ.get("GEOFENCING_JWT_SECRET", "").strip()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _sha256(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()


def _jwt_assinar(payload: dict) -> str:
    cabecalho = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    corpo = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    assinatura = hmac.new(_segredo_geofence().encode(), f"{cabecalho}.{corpo}".encode(), hashlib.sha256).digest()
    return f"{cabecalho}.{corpo}.{_b64url(assinatura)}"


def _jwt_ler(token: str) -> dict | None:
    partes = token.split(".")
    if len(partes) != 3 or not _segredo_geofence():
        return None
    try:
        recebido = _b64url_decode(partes[2])
        payload = json.loads(_b64url_decode(partes[1]))
    except (ValueError, json.JSONDecodeError):
        return None
    esperado = hmac.new(_segredo_geofence().encode(), f"{partes[0]}.{partes[1]}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(esperado, recebido) or not isinstance(payload, dict):
        return None
    expira = payload.get("exp")
    if not isinstance(expira, (int, float)) or expira <= timezone.now().timestamp():
        return None
    return payload


def _dispositivo_por_jwt(token: str):
    payload = _jwt_ler(token)
    if not payload:
        return None
    device_id = str(payload.get("device_id") or "").strip()
    jti = str(payload.get("jti") or "").strip()
    sub = str(payload.get("sub") or "").strip()
    if not device_id or not jti or not sub.isdigit():
        return None
    dispositivo = LogisticaDispositivo.objects.filter(
        identificador=device_id,
        token_hash=_sha256(jti),
        motorista_usuario_id=int(sub),
        ativo=True,
    ).first()
    if not dispositivo:
        return None
    dispositivo.ultimo_ping = timezone.now()
    dispositivo.save(update_fields=["ultimo_ping"])
    return dispositivo


def _dispositivo_ativo(request):
    autorizacao = request.headers.get("Authorization") or ""
    token = autorizacao[7:].strip() if autorizacao.lower().startswith("bearer ") else ""
    token = token or (request.headers.get("X-Device-Id") or "").strip()
    if not token:
        return None
    if token.count(".") == 2:
        return _dispositivo_por_jwt(token)
    return LogisticaDispositivo.objects.filter(identificador=token, ativo=True).first()


def emitir_dispositivo(ator, driver_id: int, nome: str) -> tuple[int, dict]:
    if not _segredo_geofence():
        return 503, {"ok": False, "error": "A chave JWT do geofencing ainda não está configurada."}
    usuario = User.objects.filter(pk=driver_id, is_active=True).first()
    if not usuario:
        return 422, {"ok": False, "error": "O usuário não é um motorista ativo."}
    if LogisticaDispositivo.objects.filter(motorista_usuario=usuario, ativo=True).exclude(token_hash="").exists():
        return 409, {"ok": False, "error": "Este motorista já possui um aparelho ativo. Revogue-o antes de gerar outro token."}
    device_id = f"device_{uuid.uuid4()}"
    jti = str(uuid.uuid4())
    emitido = int(timezone.now().timestamp())
    token = _jwt_assinar(
        {
            "sub": str(usuario.pk),
            "device_id": device_id,
            "jti": jti,
            "iat": emitido,
            "exp": emitido + 180 * 24 * 60 * 60,
            "scope": "geofencing:write",
        }
    )
    exibido = (usuario.get_full_name() or usuario.username).strip()
    LogisticaDispositivo.objects.create(
        identificador=device_id[:80],
        nome=(nome or "Aparelho Android")[:160] or "Aparelho Android",
        tipo="celular",
        motorista=exibido[:160],
        motorista_usuario=usuario,
        token_hash=_sha256(jti),
        ativo=True,
    )
    _auditar(ator, "criou_dispositivo_geofence", "logistica_dispositivos", device_id, f"{nome} · motorista {usuario.pk}")
    return 201, {"ok": True, "device": {"id": device_id, "driverId": usuario.pk, "name": (nome or "Aparelho Android")[:100]}, "token": token}


def revogar_dispositivo(ator, device_id: str) -> tuple[int, dict]:
    dispositivo = LogisticaDispositivo.objects.filter(identificador=device_id, ativo=True).first()
    if not dispositivo:
        return 404, {"ok": False, "error": "Dispositivo não encontrado ou já revogado."}
    dispositivo.ativo = False
    dispositivo.revogado_em = timezone.now()
    dispositivo.save(update_fields=["ativo", "revogado_em"])
    _auditar(ator, "revogou_dispositivo_geofence", "logistica_dispositivos", device_id, device_id)
    return 200, {"ok": True, "revoked": True}


def _numero(valor):
    if isinstance(valor, bool) or not isinstance(valor, (int, float, str)):
        return None
    bruto = str(valor).strip().replace(",", ".")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", bruto):
        return None
    try:
        return float(bruto)
    except ValueError:
        return None


def _inteiro_positivo(valor):
    numero = _numero(valor)
    if numero is None or not numero.is_integer() or numero <= 0:
        return None
    return int(numero)


def _quando(valor):
    if not isinstance(valor, str) or not valor.strip():
        return None
    quando = parse_datetime(valor.strip().replace(" ", "T"))
    if quando is None:
        return None
    if timezone.is_naive(quando):
        quando = timezone.make_aware(quando, timezone.get_current_timezone())
    return quando


def _nome_motorista_localizacao(payload: dict, dispositivo: LogisticaDispositivo) -> tuple[str, str]:
    nome = str(payload.get("motorista") or payload.get("driverName") or payload.get("motorista_nome") or "").strip()
    bruto = payload.get("driverId", payload.get("motoristaId"))
    if bruto is not None and not str(bruto).strip().isdigit():
        nome = nome or str(bruto).strip()
    if dispositivo.motorista_usuario_id and bruto is not None and str(bruto).strip().isdigit():
        if int(str(bruto).strip()) != dispositivo.motorista_usuario_id:
            return "", "O dispositivo não pertence ao motorista informado."
    if dispositivo.motorista and nome and nome.casefold() != dispositivo.motorista.casefold():
        return "", "O dispositivo não pertence ao motorista informado."
    if bruto is not None and str(bruto).strip().isdigit() and dispositivo.motorista and dispositivo.motorista.strip().isdigit():
        if str(bruto).strip() != dispositivo.motorista.strip():
            return "", "O dispositivo não pertence ao motorista informado."
    resolvido = nome or (dispositivo.motorista or "")
    if not resolvido and bruto is not None and str(bruto).strip().isdigit():
        usuario = User.objects.filter(pk=int(str(bruto).strip())).first()
        if usuario:
            resolvido = (usuario.get_full_name() or usuario.username).strip()
            if dispositivo.motorista and resolvido.casefold() != dispositivo.motorista.casefold():
                return "", "O dispositivo não pertence ao motorista informado."
    if not resolvido:
        return "", "driverId inválido."
    return resolvido, ""


def _viagem_ativa(motorista: str, viagem_id):
    consulta = TmsViagem.objects.exclude(status__in=["finalizada", "cancelada"]).exclude(status_logistico="retornou_base")
    if motorista:
        consulta = consulta.filter(motorista_nome__iexact=motorista)
    if viagem_id:
        return consulta.filter(pk=viagem_id).first()
    return consulta.order_by("-id").first()


def _coordenada_parada(parada: TmsViagemParada, viagem: TmsViagem):
    romaneio = TmsRomaneio.objects.filter(pk=parada.romaneio_id).first() if parada.romaneio_id else None
    codigo = _loja_codigo(romaneio.loja_destino) if romaneio else ""
    nome = (romaneio.loja_destino if romaneio else "") or codigo
    rota = None
    if codigo:
        rota = TmsRota.objects.filter(ativa=True, loja_codigo__iexact=codigo).exclude(latitude=None).exclude(longitude=None).first()
    ponto = _coordenada(rota.latitude, rota.longitude) if rota else None
    if ponto:
        return ponto["latitude"], ponto["longitude"], 300, f"rota-{codigo}", nome or codigo
    cerca = None
    if codigo:
        cerca = TmsGeofence.objects.filter(ativo=True, tipo="LOJA").filter(Q(nome__icontains=codigo) | Q(pk__icontains=codigo)).first()
    if not cerca and viagem.destino_cd:
        digitos = _digitos(viagem.destino_cd)[-3:]
        cerca = TmsGeofence.objects.filter(pk=f"geo_cd_{digitos}", ativo=True).first()
    if cerca:
        return cerca.latitude, cerca.longitude, int(cerca.raio_metros or 300), cerca.pk, cerca.nome
    return None


@csrf_exempt
@require_http_methods(["POST"])
def api_driver_location(request):
    dispositivo = _dispositivo_ativo(request)
    if not dispositivo:
        return JsonResponse({"ok": False, "error": "Dispositivo não autorizado ou revogado."}, status=401)
    payload = _json_body(request)
    if payload is None:
        return JsonResponse({"ok": False, "error": "Informe um payload JSON de telemetria."}, status=422)
    motorista, erro_motorista = _nome_motorista_localizacao(payload, dispositivo)
    if erro_motorista:
        status = 403 if "não pertence" in erro_motorista else 422
        return JsonResponse({"ok": False, "error": erro_motorista}, status=status)
    bruto_viagem = payload.get("tripId", payload.get("viagemId", None))
    viagem_id = None
    if bruto_viagem not in (None, ""):
        viagem_id = _inteiro_positivo(bruto_viagem)
        if not viagem_id:
            return JsonResponse({"ok": False, "error": "tripId inválido."}, status=422)
    latitude = _numero(payload.get("latitude", payload.get("lat")))
    longitude = _numero(payload.get("longitude", payload.get("lng")))
    precisao = _numero(payload.get("accuracy", payload.get("precisao", payload.get("precisaoMetros", 0))))
    if precisao is None:
        precisao = 0
    quando = _quando(payload.get("timestamp", payload.get("capturedAt", payload.get("ocorreuEm"))))
    if latitude is None or not (-90 <= latitude <= 90):
        return JsonResponse({"ok": False, "error": "latitude inválida."}, status=422)
    if longitude is None or not (-180 <= longitude <= 180):
        return JsonResponse({"ok": False, "error": "longitude inválida."}, status=422)
    if precisao < 0 or precisao > 1000:
        return JsonResponse({"ok": False, "error": "precisão do GPS inválida."}, status=422)
    if quando is None:
        return JsonResponse({"ok": False, "error": "timestamp inválido."}, status=422)
    viagem = _viagem_ativa(motorista, viagem_id)
    if not viagem:
        mensagem = "A viagem informada não está ativa para este motorista." if viagem_id else "Nenhuma viagem ativa foi encontrada para este motorista."
        return JsonResponse({"ok": False, "error": mensagem}, status=404)
    TmsTelemetria.objects.create(viagem=viagem, placa=viagem.veiculo_id[:20], latitude=latitude, longitude=longitude, ocorreu_em=quando)
    dispositivo.latitude = latitude
    dispositivo.longitude = longitude
    dispositivo.ultimo_ping = quando
    dispositivo.save(update_fields=["latitude", "longitude", "ultimo_ping"])
    parada = viagem.paradas.exclude(status="entregue").order_by("ordem", "id").first()
    if not parada:
        return JsonResponse({"ok": True, "stored": True, "tripId": viagem.pk, "tripCompleted": True, "currentStop": None})
    coordenada = _coordenada_parada(parada, viagem)
    resposta = {
        "ok": True,
        "stored": True,
        "tripId": viagem.pk,
        "tripCompleted": False,
        "currentStop": {
            "id": parada.pk,
            "order": parada.ordem,
            "status": parada.status,
            "geofenceId": coordenada[3] if coordenada else "",
            "name": coordenada[4] if coordenada else "",
        },
    }
    if coordenada:
        distancia = _haversine(latitude, longitude, coordenada[0], coordenada[1])
        resposta["distanceToNextStopMeters"] = round(distancia)
        resposta["insideGeofence"] = distancia <= coordenada[2] + min(150, precisao)
    else:
        resposta["distanceToNextStopMeters"] = None
        resposta["insideGeofence"] = False
    return JsonResponse(resposta)


def _pode_frota(user) -> bool:
    k = _k()
    if k.allowed(user):
        return True
    views = k._views()
    return any(views.user_has_perm(user, chave) for chave in ("painel_frota", "veiculos_frota", "tms_romaneios"))


def _cd_operacao(request) -> str:
    digitos = _digitos(_k().current_cd_code(request))
    if len(digitos) > 3:
        digitos = digitos[-3:]
    return digitos or "806"


def _frota_snapshot(cd: str) -> dict:
    demo = FrotaDemoSimulacao.objects.filter(cd_codigo=cd, numero_romaneio="TST-0000").first()
    status = demo.status if demo else "em_carregamento"
    cerca = TmsGeofence.objects.filter(pk=f"geo_cd_{cd}", tipo="CD", ativo=True).first()
    if not cerca:
        cerca = TmsGeofence.objects.filter(tipo="CD", ativo=True, nome__icontains=cd).first()
    latitude = float(cerca.latitude) if cerca else -23.95
    longitude = float(cerca.longitude) if cerca else -46.33
    ativos = TmsVeiculo.objects.filter(ativo=True).count()
    parados = TmsVeiculo.objects.filter(ativo=False).count()
    base = TmsRomaneio.objects.filter(Q(cd_origem=cd) | Q(cd_origem__iendswith=cd)).exclude(numero_romaneio="TST-0000")
    transito = base.filter(status="em_transporte").count()
    carregando = base.filter(status__in=["conferido", "aguardando_conferencia", "aguardando_complemento"]).count()
    return {
        "demo": {
            "cd_codigo": cd,
            "numero_romaneio": "TST-0000",
            "status": status,
            "hard_lock": 1 if demo and demo.hard_lock else 0,
            "notas_incluidas": demo.notas_incluidas if demo else 0,
            "notas_futuras": demo.notas_futuras if demo else 0,
            "waypoint_atual": demo.waypoint_atual if demo else "doca",
            "updated_at": demo.updated_at.isoformat() if demo else "",
        },
        "waypoints": {
            "doca": {"latitude": latitude + 0.00045, "longitude": longitude - 0.00035, "label": f"Doca CD {cd}"},
            "portaria": {"latitude": latitude - 0.00045, "longitude": longitude + 0.00045, "label": f"Portaria CD {cd}"},
        },
        "summary": {
            "disponivel": max(0, ativos - transito - carregando),
            "manutencao": parados,
            "em_transito": transito + (1 if status == "em_transito" else 0),
            "em_carregamento": carregando + (1 if status == "em_carregamento" else 0),
        },
    }


def html_painel_frota(cd: str) -> str:
    snapshot = _frota_snapshot(cd)
    estado = "Em trânsito" if snapshot["demo"]["status"] == "em_transito" else "Em carregamento"
    regra = (
        "Romaneio bloqueado na saída. Novas NF-es seguem para um romaneio futuro."
        if snapshot["demo"]["hard_lock"]
        else "Na doca: NF-es da mesma loja entram no romaneio ativo."
    )
    dados = json.dumps(snapshot, ensure_ascii=False).replace("<", "\\u003c")
    resumo = snapshot["summary"]
    return f"""
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="">
    <section class="frota-airport" aria-label="Painel de Frota">
      <aside class="frota-airport-availability">
        <header><span class="eyebrow">Frota / CD {escape(cd)}</span><h2>Painel de disponibilidade</h2><p>Posição operacional em tempo real.</p></header>
        <article class="frota-demo-vehicle" data-frota-demo-card>
          <div><strong>TST-0000</strong><span class="status-pill" data-frota-demo-status>{escape(estado)}</span></div>
          <p><i class="fa-solid fa-location-dot" aria-hidden="true"></i> <span data-frota-demo-rule>{escape(regra)}</span></p>
          <small data-frota-demo-updated>Pronto para a simulação</small>
        </article>
        <div class="frota-airport-statuses" aria-label="Situação da frota">
          <article><i class="fa-solid fa-square-parking" aria-hidden="true"></i><span>Disponível</span><strong data-frota-summary="disponivel">{resumo["disponivel"]}</strong></article>
          <article><i class="fa-solid fa-screwdriver-wrench" aria-hidden="true"></i><span>Indisponível</span><strong data-frota-summary="manutencao">{resumo["manutencao"]}</strong></article>
          <article><i class="fa-solid fa-road" aria-hidden="true"></i><span>Em trânsito</span><strong data-frota-summary="em_transito">{resumo["em_transito"]}</strong></article>
          <article><i class="fa-solid fa-box-open" aria-hidden="true"></i><span>Em carregamento</span><strong data-frota-summary="em_carregamento">{resumo["em_carregamento"]}</strong></article>
        </div>
      </aside>
      <div class="frota-airport-map-wrap">
        <div class="frota-airport-map" data-frota-airport-map aria-label="Mapa operacional do CD {escape(cd)}"></div>
        <section class="frota-airport-simulator" aria-label="Simulador de waypoints">
          <div><span class="eyebrow">Simulador da diretoria</span><h3>Waypoints do TST-0000</h3><p data-frota-demo-feedback hidden role="alert"></p></div>
          <div class="frota-airport-actions">
            <button class="warning" type="button" data-frota-waypoint="doca"><i class="fa-solid fa-box-open" aria-hidden="true"></i> Entrar na doca</button>
            <button class="secondary" type="button" data-frota-waypoint="incluir_nf"><i class="fa-solid fa-file-circle-plus" aria-hidden="true"></i> Incluir NF adicional</button>
            <button type="button" data-frota-waypoint="portaria"><i class="fa-solid fa-road" aria-hidden="true"></i> Cruzar portaria</button>
            <button class="secondary" type="button" data-frota-waypoint="reiniciar" aria-label="Reiniciar simulação" title="Reiniciar simulação"><i class="fa-solid fa-rotate-right" aria-hidden="true"></i></button>
          </div>
        </section>
      </div>
    </section>
    <script>(function(){{
      var state={dados};var mapNode=document.querySelector('[data-frota-airport-map]');if(!mapNode)return;var map,marker,line;
      function showFeedback(message){{var feedback=document.querySelector('[data-frota-demo-feedback]');if(feedback){{feedback.hidden=false;feedback.textContent=message||'Não foi possível atualizar a simulação.';}}}}
      function statusLabel(){{return state.demo.status==='em_transito'?'Em trânsito':'Em carregamento';}}
      function render(){{var status=document.querySelector('[data-frota-demo-status]'),rule=document.querySelector('[data-frota-demo-rule]'),clock=document.querySelector('[data-frota-demo-updated]');if(status)status.textContent=statusLabel();if(rule)rule.textContent=state.demo.hard_lock?'Romaneio bloqueado na saída. Novas NF-es seguem para um romaneio futuro.':'Na doca: NF-es da mesma loja entram no romaneio ativo.';if(clock)clock.textContent='Pronto para a simulação';document.querySelectorAll('[data-frota-summary]').forEach(function(node){{var key=node.getAttribute('data-frota-summary');node.textContent=String((state.summary&&state.summary[key])||0);}});}}
      function draw(){{if(!window.L||!state.waypoints)return;var target=state.demo.status==='em_transito'?state.waypoints.portaria:state.waypoints.doca;if(!target)return;if(!map){{map=window.L.map(mapNode,{{zoomControl:true,attributionControl:true}}).setView([target.latitude,target.longitude],17);window.L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);window.L.circleMarker([state.waypoints.doca.latitude,state.waypoints.doca.longitude],{{radius:9,color:'#0f766e',fillColor:'#14b8a6',fillOpacity:1}}).bindTooltip('Doca').addTo(map);window.L.circleMarker([state.waypoints.portaria.latitude,state.waypoints.portaria.longitude],{{radius:9,color:'#b45309',fillColor:'#f59e0b',fillOpacity:1}}).bindTooltip('Portaria').addTo(map);line=window.L.polyline([[state.waypoints.doca.latitude,state.waypoints.doca.longitude],[state.waypoints.portaria.latitude,state.waypoints.portaria.longitude]],{{color:'#64748b',dashArray:'6 7'}}).addTo(map);marker=window.L.marker([target.latitude,target.longitude]).addTo(map);}}else{{marker.setLatLng([target.latitude,target.longitude]);map.panTo([target.latitude,target.longitude]);}}marker.bindPopup('<strong>TST-0000</strong><br>'+statusLabel());}}
      function update(action){{var buttons=document.querySelectorAll('[data-frota-waypoint]');buttons.forEach(function(button){{button.disabled=true;}});fetch('/api/frota/aeroporto/',{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json',Accept:'application/json'}},body:JSON.stringify({{action:action}})}}).then(function(response){{return response.json().then(function(payload){{if(!response.ok||!payload.ok)throw new Error(payload.error||'Não foi possível atualizar a simulação.');return payload;}});}}).then(function(payload){{state=payload.data;render();draw();}}).catch(function(error){{showFeedback(error.message);}}).finally(function(){{buttons.forEach(function(button){{button.disabled=false;}});}});}}
      document.querySelectorAll('[data-frota-waypoint]').forEach(function(button){{button.addEventListener('click',function(){{update(button.getAttribute('data-frota-waypoint'));}});}});
      render();if(window.L)draw();else{{var loader=document.createElement('script');loader.src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';loader.crossOrigin='';loader.addEventListener('load',draw);document.head.appendChild(loader);}}
      if(state.waypoints&&state.waypoints.doca&&state.waypoints.portaria){{var route='https://router.project-osrm.org/route/v1/driving/'+state.waypoints.doca.longitude+','+state.waypoints.doca.latitude+';'+state.waypoints.portaria.longitude+','+state.waypoints.portaria.latitude+'?overview=full&geometries=geojson';fetch(route).then(function(response){{return response.ok?response.json():null;}}).then(function(payload){{if(!payload||!payload.routes||!payload.routes[0]||!line)return;line.setLatLngs(payload.routes[0].geometry.coordinates.map(function(point){{return [point[1],point[0]];}}));}}).catch(function(){{}});}}
    }})();</script>
    """


def _romaneio_tst(cd: str):
    return TmsRomaneio.objects.filter(numero_romaneio="TST-0000").filter(Q(cd_origem=cd) | Q(cd_origem__iendswith=cd))


@csrf_exempt
@login_required
@require_http_methods(["GET", "POST"])
def api_frota_aeroporto(request):
    if not _pode_frota(request.user):
        return JsonResponse({"ok": False, "error": "Sem acesso ao painel da frota."}, status=403)
    cd = _cd_operacao(request)
    if request.method == "GET":
        return JsonResponse({"ok": True, "data": _frota_snapshot(cd)})
    payload = _json_body(request) or {}
    acao = str(payload.get("action") or "").strip()
    if acao not in {"doca", "portaria", "reiniciar", "incluir_nf"}:
        return JsonResponse({"ok": False, "error": "Ação inválida."}, status=422)
    atual = FrotaDemoSimulacao.objects.filter(cd_codigo=cd, numero_romaneio="TST-0000").first()
    if acao == "doca" and atual and atual.hard_lock:
        return JsonResponse({"ok": False, "error": "O TST-0000 já saiu do CD. Reinicie a demonstração para voltar à doca."}, status=409)
    if acao == "incluir_nf":
        if atual:
            if atual.hard_lock:
                atual.notas_futuras += 1
            else:
                atual.notas_incluidas += 1
                _romaneio_tst(cd).update(quantidade_nfes=F("quantidade_nfes") + 1, atualizado_por=_k().username(request.user)[:160])
            atual.save(update_fields=["notas_incluidas", "notas_futuras", "updated_at"])
        return JsonResponse({"ok": True, "data": _frota_snapshot(cd)})
    status = "em_transito" if acao == "portaria" else "em_carregamento"
    hard_lock = acao == "portaria"
    waypoint = "portaria" if acao == "portaria" else "doca"
    demo, _criado = FrotaDemoSimulacao.objects.get_or_create(cd_codigo=cd, numero_romaneio="TST-0000")
    demo.status = status
    demo.hard_lock = hard_lock
    demo.waypoint_atual = waypoint
    if acao == "reiniciar":
        demo.notas_incluidas = 0
        demo.notas_futuras = 0
    demo.save()
    campos_romaneio = {"status": "em_transporte" if status == "em_transito" else "conferido", "atualizado_por": _k().username(request.user)[:160]}
    if acao == "portaria":
        campos_romaneio["data_saida"] = timezone.now()
    _romaneio_tst(cd).update(**campos_romaneio)
    if acao == "reiniciar":
        _romaneio_tst(cd).update(quantidade_nfes=0)
    if acao == "portaria":
        _avisar_cd(cd, "Saída do CD", "TST-0000 cruzou a Portaria do CD e está em trânsito.", "/frota/simulador/", "frota_saida_cd")
    elif acao == "doca":
        _avisar_cd(cd, "Romaneio conferido", "TST-0000 está na doca e permanece aberto para NF-es da mesma loja.", "/frota/simulador/", "frota_doca")
    return JsonResponse({"ok": True, "data": _frota_snapshot(cd)})


def _cd_do_usuario(request, cd: str) -> bool:
    if getattr(request.user, "is_superuser", False):
        return True
    pedido = _digitos(cd)[-3:]
    liberados = {_digitos(codigo)[-3:] for codigo in _k().cd_codes(request)}
    return bool(pedido) and pedido in liberados


def _romaneios_da_viagem(viagem: TmsViagem):
    ids = [parada.romaneio_id for parada in viagem.paradas.all() if parada.romaneio_id]
    return TmsRomaneio.objects.filter(pk__in=ids)


def _cd_notificacao(cd: str) -> str:
    digitos = _digitos(cd)[-3:]
    return digitos if digitos in {"801", "806"} else ""


def _avisar_usuarios(titulo: str, mensagem: str, url: str, evento: str, cd: str, payload: dict, motorista_nome: str = ""):
    views = _k()._views()
    categoria = evento[:30]
    unidade = _cd_notificacao(cd)
    vistos = set()
    chaves = ("tms_krill", "tms_expedicao", "receber_alertas_expedicao", "notificar_motorista_vinculado")
    for usuario in User.objects.filter(is_active=True):
        if not any(views.user_has_perm(usuario, chave) for chave in chaves):
            continue
        views.create_system_notification(usuario, titulo[:120], mensagem, url, categoria, unidade, payload)
        vistos.add(usuario.pk)
    nome = (motorista_nome or "").strip()
    if not nome:
        return
    for usuario in User.objects.filter(is_active=True):
        if usuario.pk in vistos:
            continue
        exibido = (usuario.get_full_name() or usuario.username or "").strip()
        if exibido.casefold() == nome.casefold() or usuario.username.casefold() == nome.casefold():
            views.create_system_notification(usuario, titulo[:120], mensagem, url, categoria, unidade, payload)


def _avisar_operacao(viagem: TmsViagem, titulo: str, mensagem: str, url: str, evento: str):
    """Manda o mesmo aviso para o sino e para o Web Push de quem opera o CD e do motorista."""
    if SistemaNotificacao.objects.filter(categoria=evento[:30], payload__viagem_id=viagem.pk).exists():
        return
    _avisar_usuarios(
        titulo,
        mensagem,
        url,
        evento,
        viagem.cd_atual or viagem.origem_cd or "",
        {"viagem_id": viagem.pk, "placa": viagem.veiculo_id, "evento": evento},
        viagem.motorista_nome,
    )


def _avisar_cd(cd: str, titulo: str, mensagem: str, url: str, evento: str):
    limite = timezone.now() - timedelta(seconds=45)
    if SistemaNotificacao.objects.filter(categoria=evento[:30], cd_unidade=_cd_notificacao(cd), criado_em__gte=limite).exists():
        return
    _avisar_usuarios(titulo, mensagem, url, evento, cd, {"cd": cd, "evento": evento})


def _gravar_recibo(viagem: TmsViagem, ator: str) -> str:
    romaneios = list(_romaneios_da_viagem(viagem))
    notas = []
    manifestos = []
    paletes = 0
    for row in romaneios:
        numeros = [nota for nota in row.nfes.values_list("numero", flat=True) if nota]
        notas.extend(numeros)
        paletes += int(row.total_paletes or 0)
        manifestos.append(
            {
                "romaneioId": row.pk,
                "numero": row.numero_romaneio,
                "origemCd": row.cd_origem,
                "notasFiscais": numeros,
                "paletes": int(row.total_paletes or 0),
                "pesoKg": float(row.peso_bruto_kg or 0),
            }
        )
    recibo = str(uuid.uuid4())
    TmsTransferenciaRecebimento.objects.create(
        id=recibo,
        viagem=viagem,
        cd_origem=(viagem.origem_cd or "")[:7],
        cd_destino=(viagem.destino_cd or "")[:7],
        placa=(viagem.veiculo_id or "")[:20],
        motorista_nome=(viagem.motorista_nome or "")[:160],
        total_paletes=paletes,
        peso_total_kg=float(viagem.peso_total_kg or 0),
        romaneios_json=manifestos,
        notas_fiscais_json=list(dict.fromkeys(notas)),
        recebido_por=ator[:160],
    )
    destino = _digitos(viagem.destino_cd)[-3:]
    if destino in {"801", "806"}:
        Recebimento.objects.create(
            cd_unidade=destino,
            data=timezone.localdate(),
            fornecedor=f"Transferência do CD {viagem.origem_cd}"[:120],
            nota_fiscal=", ".join(dict.fromkeys(notas))[:60],
            produto="transferencia_cd",
            paletes=paletes,
            motorista=(viagem.motorista_nome or "")[:120],
            agendado=False,
            forma_pagamento="nao_paga",
            observacao=f"Recibo {recibo} da viagem {viagem.pk}",
        )
    return recibo


def _auditar(user, acao, modulo, objeto_id, detalhe):
    k = _k()
    AuditLog.objects.create(
        user=user if getattr(user, "pk", None) else None,
        usuario_nome=k.username(user)[:160],
        acao=acao[:80],
        modulo=modulo[:80],
        objeto_id=str(objeto_id or "")[:40],
        detalhe=detalhe,
    )


def _quer_json(request) -> bool:
    tipo = (request.content_type or "").lower()
    accept = (request.headers.get("Accept") or "").lower()
    return "application/json" in tipo or "application/json" in accept


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def api_transferencia_transicao(request, pk):
    k = _k()
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão para alterar esta transferência."}, status=403)
    payload = _json_body(request) if _quer_json(request) else request.POST
    if payload is None:
        resposta = {"ok": False, "error": "Dados da transição inválidos."}
        return JsonResponse(resposta, status=422)
    acao = str(payload.get("action") or "").strip().lower()
    viagem = TmsViagem.objects.filter(pk=pk, tipo_operacao="transferencia_cd").first()
    if not viagem:
        return JsonResponse({"ok": False, "error": "Transferência não encontrada."}, status=404)
    alvo = viagem.destino_cd if acao == "confirmar_recebimento" else viagem.origem_cd
    if not _cd_do_usuario(request, alvo):
        return JsonResponse({"ok": False, "error": "Você não possui acesso ao CD desta etapa."}, status=403)
    ator = k.username(request.user)[:160]
    if acao == "confirmar_recebimento":
        if viagem.status_transferencia != "aguardando_recebimento":
            corpo = {"ok": False, "error": "A transferência ainda não está aguardando recebimento neste CD."}
            return _resposta_transicao(request, pk, corpo, 409)
        if not _romaneios_da_viagem(viagem).exists():
            corpo = {"ok": False, "error": "Esta transferência não possui romaneios para incorporar."}
            return _resposta_transicao(request, pk, corpo, 409)
        mudou = TmsViagem.objects.filter(pk=viagem.pk, status_transferencia="aguardando_recebimento").update(
            status="finalizada",
            status_logistico="retornou_base",
            status_transferencia="recebida_cd",
        )
        if not mudou:
            corpo = {"ok": False, "error": "A entrada já foi confirmada por outro operador."}
            return _resposta_transicao(request, pk, corpo, 409)
        viagem.paradas.exclude(status="entregue").update(status="entregue")
        _romaneios_da_viagem(viagem).update(status="recebido_transferencia_cd", atualizado_por=ator)
        recibo = _gravar_recibo(viagem, ator)
        _auditar(request.user, "confirmou_recebimento_transferencia_cd", "tms_viagens", viagem.pk, f"CD {viagem.destino_cd}")
        _avisar_operacao(
            viagem,
            "Transferência recebida",
            f"A placa {viagem.veiculo_id or 'sem placa'} foi recebida no CD {viagem.destino_cd}.",
            "/tms/recebimento/",
            "transferencia_recebida",
        )
        return _resposta_transicao(request, pk, {"ok": True, "viagemId": viagem.pk, "status": "recebida_cd", "receiptId": recibo}, 200)
    esperado = {"iniciar_transporte": "embarcado", "aguardar_recebimento": "em_transporte_cd"}.get(acao)
    if not esperado:
        return _resposta_transicao(request, pk, {"ok": False, "error": "Ação de transferência inválida."}, 409)
    if viagem.status_transferencia != esperado:
        return _resposta_transicao(request, pk, {"ok": False, "error": f"A transferência não está em {esperado.replace('_', ' ')}."}, 409)
    if acao == "iniciar_transporte":
        mudou = TmsViagem.objects.filter(pk=viagem.pk, status_transferencia="embarcado").update(
            status="em_rota",
            status_logistico="em_transito",
            status_transferencia="em_transporte_cd",
        )
        novo = "em_transporte_cd"
    else:
        mudou = TmsViagem.objects.filter(pk=viagem.pk, status_transferencia="em_transporte_cd").update(
            status="chegou_loja",
            status_logistico="em_descarregamento",
            cd_atual=(viagem.destino_cd or "")[:7],
            status_transferencia="aguardando_recebimento",
        )
        novo = "aguardando_recebimento"
        if mudou:
            _romaneios_da_viagem(viagem).update(status="aguardando_recebimento_cd", atualizado_por=ator)
    if not mudou:
        return _resposta_transicao(request, pk, {"ok": False, "error": "A transição já foi processada por outro evento."}, 409)
    _auditar(request.user, "alterou_status_transferencia_cd", "tms_viagens", viagem.pk, novo)
    if novo == "em_transporte_cd":
        _avisar_operacao(
            viagem,
            "Saída do CD confirmada",
            f"Viagem {viagem.veiculo_id or 'sem placa'} saiu do CD {viagem.origem_cd} a caminho do CD {viagem.destino_cd}.",
            "/tms/acompanhamento/",
            "saida_cd",
        )
    else:
        _avisar_operacao(
            viagem,
            "Chegada confirmada",
            f"Viagem {viagem.veiculo_id or 'sem placa'} chegou ao CD {viagem.destino_cd}. Status: aguardando recebimento.",
            "/tms/recebimento/",
            "chegada_destino",
        )
    return _resposta_transicao(request, pk, {"ok": True, "viagemId": viagem.pk, "status": novo}, 200)


def _resposta_transicao(request, pk, corpo: dict, status: int):
    if _quer_json(request):
        return JsonResponse(corpo, status=status)
    destino = f"/tms/viagens/{pk}/"
    if corpo.get("ok"):
        return redirect(f"{destino}?aviso={urllib.parse.quote('Transição registrada: ' + str(corpo.get('status') or 'ok').replace('_', ' '))}")
    return redirect(f"{destino}?erro={urllib.parse.quote(corpo.get('error') or 'Falha na transferência.')}")


@csrf_exempt
@require_http_methods(["POST"])
def api_email_cargas(request):
    k = _k()
    token = os.environ.get("CARGAS_WEBHOOK_TOKEN", "").strip()
    autorizado = bool(token and request.headers.get("X-Cargas-Token") == token)
    if not autorizado:
        if not getattr(request.user, "is_authenticated", False) or not k.allowed(request.user):
            return JsonResponse({"ok": False, "error": "Sem permissão para receber carga."}, status=403)
    destino = (request.POST.get("to") or request.GET.get("to") or CAIXA_CARGAS).strip().lower()
    if destino != CAIXA_CARGAS:
        return JsonResponse({"ok": False, "error": "Esta caixa não recebe carga."}, status=422)
    arquivos = request.FILES.getlist("xml_nf") or request.FILES.getlist("anexos")
    if getattr(request.user, "is_authenticated", False):
        remetente = request.POST.get("from") or k.username(request.user)
    else:
        remetente = request.POST.get("from") or ""
    resultado = gravar_xmls(arquivos, remetente[:180], (request.POST.get("subject") or "carga")[:240])
    return JsonResponse(resultado, status=200 if resultado.get("ok") else 422)


@login_required
def tms_receber_carga(request):
    k = _k()
    blocked = k._gate(request, "Carga por XML")
    if blocked:
        return blocked
    aviso = ""
    if request.method == "POST":
        resultado = gravar_xmls(request.FILES.getlist("xml_nf"), k.username(request.user), "upload")
        if resultado.get("ok"):
            return redirect("/tms/romaneios/rascunhos/")
        aviso = resultado.get("error") or "Não foi possível gravar a carga."
    rascunhos = list(TmsRascunho.objects.filter(status="pendente_conferencia").order_by("-id")[:40])
    return k.render_screen(
        request,
        k.blank_screen(
            title="Carga por XML",
            eyebrow="TMS",
            lead=f"O mesmo lote da caixa {CAIXA_CARGAS}: XML ou ZIP vira pré-romaneio e a nota antiga já vinculada fica de fora.",
            actions=[{"href": "/tms/romaneios/rascunhos/", "label": "Pré-romaneios"}, {"href": "/tms/romaneios/novo/", "label": "Novo romaneio"}],
            form={
                "action": "/tms/romaneios/rascunhos/receber/",
                "title": "Enviar NF-e",
                "submit": "Gravar pré-romaneio",
                "multipart": True,
                "fields": [{"name": "xml_nf", "label": "XML ou ZIP", "type": "file", "accept": ".xml,.zip", "multiple": True, "required": True}],
            },
            tables=[
                {
                    "title": "Pré-romaneios aguardando",
                    "lead": aviso,
                    "headers": ["ID", "CD", "Loja", "Notas"],
                    "rows": [[k.cell(row.pk), k.cell(row.cd_origem), k.cell(row.loja_destino), k.cell(row.nfes.count())] for row in rascunhos],
                }
            ],
        ),
    )


def apk_motorista(request):
    if request.method == "HEAD":
        resposta = HttpResponse(status=200, content_type="application/vnd.android.package-archive")
        resposta["Content-Disposition"] = 'attachment; filename="gestao-cd-motorista.apk"'
        resposta["Cache-Control"] = "no-store"
        return resposta
    return redirect(APK_URL)


@login_required
def api_geofence_zonas(request):
    k = _k()
    if not k.allowed(request.user):
        return JsonResponse({"error": "Sem permissão."}, status=403)
    zonas = [
        {"id": row.id, "nome": row.nome, "tipo": row.tipo, "latitude": row.latitude, "longitude": row.longitude, "raio_metros": row.raio_metros}
        for row in TmsGeofence.objects.filter(ativo=True)
    ]
    return JsonResponse({"results": zonas})


@csrf_exempt
@login_required
@require_http_methods(["POST"])
def api_importar_xml(request):
    k = _k()
    if not k.allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão."}, status=403)
    arquivos = request.FILES.getlist("xml_nf") or request.FILES.getlist("arquivo")
    pares = _xmls_de_arquivos(arquivos)
    notas = []
    erros = []
    for nome, xml in pares:
        parsed = parse_nfe_xml(xml, nome)
        if parsed.get("ok"):
            notas.append(classificar_nota(parsed["nf"], produtos_xml(xml)))
        else:
            erros.append(parsed.get("error"))
    return JsonResponse({"ok": bool(notas), "notas": notas, "erros": erros}, status=200 if notas else 422)
