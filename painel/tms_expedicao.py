"""Central de Expedição — tradução de tmsExpeditionPage / tmsExpeditionSnapshot (Worker TS)."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .models import TmsGeofenceEvento, TmsVeiculo, TmsViagem

OPEN_LOGISTIC = ("em_transito", "em_descarregamento", "em_transito_retorno")
STATUS_ORDER = {"em_patio": 0, "em_transito": 1, "em_descarregamento": 2}


def trip_status_label(status: str) -> str:
    labels = {
        "em_patio": "Em pátio",
        "em_transito": "Em trânsito",
        "em_descarregamento": "Em descarregamento",
        "em_transito_retorno": "Em trânsito de retorno",
        "retornou_base": "Retornou à base",
        "atribuida": "Atribuída",
        "em_rota": "Em rota",
        "chegou_loja": "Chegou à loja",
        "finalizada": "Finalizada",
        "cancelada": "Cancelada",
    }
    return labels.get(status or "", status or "")


def trip_status_class(status: str) -> str:
    if status in {"chegou_loja", "finalizada", "retornou_base"}:
        return "ok"
    if status == "cancelada":
        return "danger"
    return "paused"


def event_label(geofence_tipo: str, acao: str, loja_codigo: str = "") -> str:
    loja = f" {loja_codigo}" if loja_codigo else ""
    if geofence_tipo == "CD" and acao == "exit":
        return "Saída do CD"
    if geofence_tipo == "CD" and acao == "enter":
        return "Retorno ao CD"
    if geofence_tipo == "LOJA" and acao == "enter":
        return f"Chegada na loja{loja}"
    if geofence_tipo == "LOJA" and acao == "dwell":
        return f"Entrega confirmada{(' · ' + loja_codigo) if loja_codigo else ''}"
    if geofence_tipo == "LOJA" and acao == "exit":
        return f"Saída da loja{loja}"
    return "Evento de geofence confirmado"


def expedition_validation(max_kg, max_pallets, max_m3, weight_kg, volume_m3, pallets, plate: str) -> dict:
    max_kg = float(max_kg or 0)
    max_pallets = float(max_pallets or 0)
    max_m3 = float(max_m3 or 0)
    weight_kg = float(weight_kg or 0)
    volume_m3 = float(volume_m3 or 0)
    pallets = float(pallets or 0)
    if not plate or max_kg <= 0 or max_pallets <= 0 or max_m3 <= 0:
        return {
            "tone": "danger",
            "title": "Capacidade pendente",
            "detail": "O veículo não possui todos os limites físicos homologados.",
        }
    failures = []
    if weight_kg > max_kg:
        failures.append("peso")
    if volume_m3 > max_m3:
        failures.append("cubagem")
    if pallets > max_pallets:
        failures.append("pallets")
    if failures:
        return {
            "tone": "danger",
            "title": "Carga bloqueada",
            "detail": f"Limite de {', '.join(failures)} excedido.",
        }
    if weight_kg <= 0 or volume_m3 <= 0 or pallets <= 0:
        return {
            "tone": "warning",
            "title": "Aguardando cálculo",
            "detail": "Complete o peso, a cubagem e os pallets para liberar a expedição.",
        }
    return {
        "tone": "ok",
        "title": "Pronta para expedir",
        "detail": "Peso, cubagem e ocupação estão dentro dos limites do veículo.",
    }


def expedition_action(status: str, tone: str, trip_id: int, first_romaneio_id) -> dict:
    if tone == "danger":
        return {"label": "Revisar capacidade", "href": "/tms/viagens/capacidade/", "icon": "fa-triangle-exclamation"}
    if status == "em_patio" and first_romaneio_id:
        return {"label": "Conferir carga", "href": "/tms/romaneios/", "icon": "fa-clipboard-check"}
    if status in OPEN_LOGISTIC:
        return {"label": "Detalhes da viagem", "href": "/tms/acompanhamento/", "icon": "fa-route"}
    return {"label": "Ver romaneios", "href": "/tms/romaneios/", "icon": "fa-list-check"}


def _vehicle_for(trip: TmsViagem, vehicles: list[TmsVeiculo]) -> TmsVeiculo | None:
    key = (trip.veiculo_id or "").strip().upper()
    for vehicle in vehicles:
        if not vehicle.ativo:
            continue
        if vehicle.id == trip.veiculo_id or (vehicle.placa or "").strip().upper() == key:
            return vehicle
    return None


def expedition_snapshot(cd_codes: list[str]) -> dict:
    # Worker: SUM(tms_viagem_cargas.paletes). Aqui os pallets planejados ficam na parada.
    trips = list(
        TmsViagem.objects.exclude(status__in=["finalizada", "cancelada"])
        .exclude(status_logistico="retornou_base")
        .filter(Q(origem_cd__in=cd_codes) | Q(cd_atual__in=cd_codes))
        .annotate(paradas_total=Count("paradas"), paletes_planejados=Sum("paradas__paletes"))
        .order_by("created_at", "id")[:80]
    )
    trip_ids = [trip.id for trip in trips]
    vehicles = list(TmsVeiculo.objects.filter(ativo=True))
    events = list(
        TmsGeofenceEvento.objects.filter(viagem_id__in=trip_ids).order_by("-ocorreu_em", "-id")[:320]
    ) if trip_ids else []
    events_by_trip: dict[int, list] = {}
    for event in events:
        events_by_trip.setdefault(event.viagem_id, []).append(event)

    mapped = []
    for trip in trips:
        vehicle = _vehicle_for(trip, vehicles)
        plate = (vehicle.placa if vehicle else "") or trip.veiculo_id or ""
        stops = list(trip.paradas.order_by("ordem", "id"))
        pallets = int(trip.paletes_planejados or 0)
        first_romaneio = next((stop.romaneio_id for stop in stops if stop.romaneio_id), None)
        validation = expedition_validation(
            vehicle.capacidade_max_kg if vehicle else 0,
            vehicle.capacidade_max_pallets if vehicle else 0,
            vehicle.capacidade_max_m3 if vehicle else 0,
            trip.peso_total_kg,
            trip.volume_total_m3,
            pallets,
            plate,
        )
        raw = list(reversed((events_by_trip.get(trip.id) or [])[:4]))
        if raw:
            timeline = [
                {
                    "label": event_label(event.geofence_tipo, event.acao, event.loja_codigo),
                    "when": event.ocorreu_em.strftime("%d/%m/%Y %H:%M"),
                    "tone": "current" if index == len(raw) - 1 else "done",
                }
                for index, event in enumerate(raw)
            ]
        else:
            waiting = "Aguardando saída do CD" if trip.status_logistico == "em_patio" else "Aguardando sinal da telemetria"
            timeline = [{"label": waiting, "when": "", "tone": "current"}]
        status = trip.status_logistico or trip.status
        mapped.append(
            {
                "id": trip.id,
                "status": status,
                "status_label": trip_status_label(status),
                "status_class": trip_status_class(status),
                "driver": trip.motorista_nome or "Motorista não informado",
                "vehicle": {
                    "plate": plate or "Veículo não informado",
                    "maxKg": float(vehicle.capacidade_max_kg if vehicle else 0),
                    "maxPallets": int(vehicle.capacidade_max_pallets if vehicle else 0),
                    "maxM3": float(vehicle.capacidade_max_m3 if vehicle else 0),
                },
                "destination": f"{trip.loja_codigo or '-'}{' · ' + trip.loja_nome if trip.loja_nome else ''}",
                "originCd": trip.origem_cd or "CD não informado",
                "totals": {
                    "weightKg": float(trip.peso_total_kg or 0),
                    "volumeM3": float(trip.volume_total_m3 or 0),
                    "pallets": pallets,
                    "stops": int(trip.paradas_total or len(stops)),
                },
                "validation": validation,
                "timeline": timeline,
                "action": expedition_action(status, validation["tone"], trip.id, first_romaneio),
            }
        )
    mapped.sort(key=lambda row: (STATUS_ORDER.get(row["status"], 3), row["id"]))
    return {
        "summary": {
            "total": len(mapped),
            "approved": sum(1 for row in mapped if row["validation"]["tone"] == "ok"),
            "attention": sum(1 for row in mapped if row["validation"]["tone"] == "warning"),
            "blocked": sum(1 for row in mapped if row["validation"]["tone"] == "danger"),
        },
        "trips": mapped,
    }


def _cd_codes(request) -> list[str]:
    from .views import cd_values, current_cd

    return cd_values(current_cd(request))


def _allowed(user) -> bool:
    from .views import user_has_perm

    return user.is_superuser or user_has_perm(user, "tms_expedicao")


@login_required
@require_GET
def tms_expedicao_page(request):
    from .views import context_base

    if not _allowed(request.user):
        ctx = context_base(request)
        ctx.update({"denied": True, "message": "Sem permissão para a Central de Expedição.", "snapshot": None})
        return render(request, "painel/tms_expedicao.html", ctx, status=403)
    snapshot = expedition_snapshot(_cd_codes(request))
    ctx = context_base(request)
    ctx.update({"page_title": "Central de Expedição", "snapshot": snapshot})
    return render(request, "painel/tms_expedicao.html", ctx)


@login_required
@require_GET
def tms_expedicao_api(request):
    if not _allowed(request.user):
        return JsonResponse({"ok": False, "error": "Sem permissão para acessar a Central de Expedição."}, status=403)
    return JsonResponse({"ok": True, **expedition_snapshot(_cd_codes(request))}, json_dumps_params={"ensure_ascii": False})
