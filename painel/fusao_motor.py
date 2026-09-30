"""Peças do Worker que passam a rodar dentro do Django do Gestão CD 2.

Google Directions, stream de indicadores, carga XML no mesmo fluxo do e-mail
cargas@wbjp.com.br, compositor de viagem e o download do app do motorista.
"""

from __future__ import annotations

import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import (
    ChamadoSaldo,
    PessoaTurno,
    Separacao,
    TmsGeofence,
    TmsRascunho,
    TmsRascunhoNfe,
    TmsRomaneio,
    TmsRota,
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
                "position": indice,
                "side": lado,
                "empty": False,
                "loadingOrder": indice,
                "storeCode": _loja_codigo(row.loja_destino),
                "storeName": row.loja_destino,
                "estimatedWeightKg": peso_row,
                "notaFiscal": nota.numero if nota else "",
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
    loja = destino if transferencia else (romaneios[0].loja_destino or "")
    viagem = TmsViagem.objects.create(
        motorista_nome=motorista,
        veiculo_id=veiculo.placa,
        loja_codigo=_loja_codigo(loja)[:40],
        loja_nome=(loja or "")[:160],
        origem_cd=origem[:7],
        cd_atual=origem[:7],
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
        row.save(update_fields=["placa", "motorista", "atualizado_por", "updated_at"])
    return {"ok": True, "viagemId": viagem.pk, "notice": f"Composição confirmada na viagem {viagem.pk}."}


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
    status = resultado.pop("status", 200)
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
    status = resultado.pop("status", 200)
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
