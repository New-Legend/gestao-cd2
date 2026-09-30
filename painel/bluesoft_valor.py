"""Recálculo do valor acumulado BlueSoft.

Tradução direta de tmsCalcularGrupoValores, tmsValorGrupoRows e do
fechamento em tmsRomaneioStatus / tmsRomaneioEdit no index.ts do Worker.

O número informado é o total acumulado que a BlueSoft mostra para o CD,
a loja e a data. valor_total_carga guarda só a diferença contra o
lançamento anterior. O ERP recebe essa diferença, não a soma dos totais.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import TmsRascunhoNfe, TmsRomaneio, TmsRomaneioNfe

_EPSILON = 2.220446049250313e-16
_LOJA = re.compile(r"\b(?:LJ|LOJA)\s*0*(\d{1,3})\b", re.IGNORECASE)


def round_money(value) -> float:
    numeric = float(value or 0)
    return round((numeric + _EPSILON) * 100) / 100


def valor_participa(status: str) -> bool:
    return status != "cancelado"


def loja_codigo(loja: str) -> str:
    match = _LOJA.search(str(loja or "").upper())
    if not match:
        return ""
    return f"LJ{int(match.group(1)):02d}"


def loja_chave(loja: str) -> str:
    return loja_codigo(loja) or str(loja or "").strip().upper()


def mesmo_grupo(data_a, cd_a, loja_a, data_b, cd_b, loja_b) -> bool:
    return data_a == data_b and str(cd_a or "").strip().upper() == str(cd_b or "").strip().upper() and loja_chave(loja_a) == loja_chave(loja_b)


def status_operational_date(existing: date | None, status: str) -> date:
    current = timezone.localdate()
    if not existing:
        return current
    if existing < current and status not in {"rascunho", "cancelado"}:
        return current
    return existing


@dataclass
class _Linha:
    id: int
    created_at: datetime | None
    numero_romaneio: str
    valor_total_carga: float
    valor_acumulado_bluesoft: float


@dataclass
class GrupoCalculo:
    ok: bool
    total: float
    deltas: dict
    snapshots: dict
    updates: list
    error: str = ""


def _sort_key(row: _Linha):
    created = row.created_at.isoformat() if row.created_at else ""
    return (created, row.id)


def calcular_grupo_valores(rows: list[_Linha]) -> GrupoCalculo:
    ordered = sorted(rows, key=_sort_key)
    deltas: dict[int, float] = {}
    snapshots: dict[int, float] = {}
    updates = []
    acumulado_anterior = 0.0
    for row in ordered:
        valor_atual = round_money(row.valor_total_carga)
        snapshot_informado = round_money(row.valor_acumulado_bluesoft)
        snapshot = snapshot_informado if snapshot_informado > 0 else round_money(acumulado_anterior + valor_atual)
        if snapshot + 0.009 < acumulado_anterior:
            return GrupoCalculo(
                ok=False,
                total=acumulado_anterior,
                deltas=deltas,
                snapshots=snapshots,
                updates=updates,
                error=f"O acumulado do {row.numero_romaneio} ficou menor que um lançamento anterior.",
            )
        valor_novo = round_money(snapshot - acumulado_anterior)
        deltas[row.id] = valor_novo
        snapshots[row.id] = snapshot
        if abs(valor_novo - valor_atual) >= 0.01 or abs(snapshot - snapshot_informado) >= 0.01:
            updates.append({"id": row.id, "valor_total": valor_novo, "valor_acumulado": snapshot})
        acumulado_anterior = snapshot
    return GrupoCalculo(ok=True, total=acumulado_anterior, deltas=deltas, snapshots=snapshots, updates=updates)


def _linha(row: TmsRomaneio) -> _Linha:
    return _Linha(
        id=row.pk,
        created_at=row.created_at,
        numero_romaneio=row.numero_romaneio,
        valor_total_carga=row.valor_total_carga,
        valor_acumulado_bluesoft=row.valor_acumulado_bluesoft,
    )


def grupo_rows(data, cd_origem: str, loja_destino: str, exclude_id: int | None = None) -> list[_Linha]:
    loja = str(loja_destino or "").strip()
    if not loja:
        return []
    codigo = loja_codigo(loja)
    found = TmsRomaneio.objects.filter(data=data, cd_origem=cd_origem).exclude(status="cancelado")
    if exclude_id:
        found = found.exclude(pk=exclude_id)
    linhas = []
    for row in found.order_by("created_at", "id"):
        destino = row.loja_destino or ""
        if codigo:
            if not destino.upper().startswith(codigo):
                continue
        elif destino != loja:
            continue
        linhas.append(_linha(row))
    return linhas


def _gravar(updates: list, username: str, skip_id: int | None = None) -> None:
    now = timezone.now()
    for update in updates:
        if skip_id is not None and update["id"] == skip_id:
            continue
        TmsRomaneio.objects.filter(pk=update["id"]).update(
            valor_total_carga=update["valor_total"],
            valor_acumulado_bluesoft=update["valor_acumulado"],
            atualizado_por=username,
            updated_at=now,
        )


def resolver_snapshot(data, cd_origem: str, loja_destino: str, snapshot: float, numero: str, linha_id: int, created_at, valor_total_atual: float = 0) -> tuple[GrupoCalculo, _Linha]:
    """Monta o grupo com a linha informada usando o acumulado BlueSoft como snapshot."""
    anteriores = grupo_rows(data, cd_origem, loja_destino, exclude_id=linha_id if linha_id > 0 else None)
    linha = _Linha(
        id=linha_id,
        created_at=created_at,
        numero_romaneio=numero,
        valor_total_carga=valor_total_atual,
        valor_acumulado_bluesoft=round_money(snapshot),
    )
    return calcular_grupo_valores([*anteriores, linha]), linha


def chaves_ja_vinculadas(chaves: list[str], ignore_rascunho_id: int = 0) -> set[str]:
    """tmsExistingNfKeys: chave já gravada num romaneio ou num rascunho de e-mail."""
    unique = []
    seen = set()
    for chave in chaves:
        texto = str(chave or "").strip()
        if not texto or texto in seen:
            continue
        seen.add(texto)
        unique.append(texto)
    found: set[str] = set()
    for offset in range(0, len(unique), 50):
        chunk = unique[offset : offset + 50]
        found.update(TmsRomaneioNfe.objects.filter(chave_acesso__in=chunk).values_list("chave_acesso", flat=True))
        drafts = TmsRascunhoNfe.objects.filter(chave_acesso__in=chunk)
        if ignore_rascunho_id:
            drafts = drafts.exclude(rascunho_id=ignore_rascunho_id)
        found.update(drafts.values_list("chave_acesso", flat=True))
    return found


def _notas_unicas(payload_notas: list[dict]) -> tuple[list[dict], int]:
    unique = []
    seen = set()
    repetidas = 0
    for nota in payload_notas or []:
        chave = str((nota or {}).get("chave_acesso") or (nota or {}).get("chaveAcesso") or "").strip()
        if not chave:
            continue
        if chave in seen:
            repetidas += 1
            continue
        seen.add(chave)
        bruto = (nota or {}).get("valor", (nota or {}).get("valor_total", (nota or {}).get("valorTotal", 0)))
        try:
            valor = float(str(bruto).replace(",", "."))
        except (TypeError, ValueError):
            valor = 0
        unique.append(
            {
                "chave_acesso": chave,
                "valor": valor,
                "numero": str((nota or {}).get("numero") or "")[:20],
                "serie": str((nota or {}).get("serie") or "1")[:8],
            }
        )
    return unique, repetidas


def criar_romaneio_com_notas(row: TmsRomaneio, payload_notas: list[dict], username: str, ignore_rascunho_id: int = 0) -> dict:
    """Peneira de tmsExistingNfKeys e delta de tmsFinalizeRomaneioXml.

    valor_total_carga fica com a soma das notas inéditas. O acumulado BlueSoft
    é essa soma mais o total já lançado no mesmo CD, loja e data.
    """
    notas, repetidas = _notas_unicas(payload_notas)
    existentes = chaves_ja_vinculadas([nota["chave_acesso"] for nota in notas], ignore_rascunho_id)
    ineditas = [nota for nota in notas if nota["chave_acesso"] not in existentes]
    descartadas = repetidas + len(notas) - len(ineditas)
    if not ineditas:
        return {
            "sucesso": False,
            "erro": "Todas as NF-e enviadas já estão vinculadas a romaneios. Nenhum lançamento duplicado foi criado.",
            "descartadas": descartadas,
        }
    valor_total = round_money(sum(nota["valor"] for nota in ineditas))
    grupo = calcular_grupo_valores(grupo_rows(row.data, row.cd_origem, row.loja_destino))
    if not grupo.ok:
        return {"sucesso": False, "erro": grupo.error or "A sequência de valores desta loja precisa ser revisada."}
    acumulado = round_money(grupo.total + valor_total)
    try:
        with transaction.atomic():
            _gravar(grupo.updates, username)
            row.valor_total_carga = valor_total
            row.valor_acumulado_bluesoft = acumulado
            row.quantidade_nfes = len(ineditas)
            row.status = row.status or "pendente_conferencia"
            row.atualizado_por = username
            row.criado_por = row.criado_por or username
            row.save()
            TmsRomaneioNfe.objects.bulk_create(
                [
                    TmsRomaneioNfe(
                        romaneio=row,
                        chave_acesso=nota["chave_acesso"],
                        numero=nota["numero"],
                        serie=nota["serie"],
                        valor_total=round_money(nota["valor"]),
                        status_conferencia="pendente",
                    )
                    for nota in ineditas
                ]
            )
    except IntegrityError:
        return {"sucesso": False, "erro": "Uma das NF-e já foi vinculada enquanto esta prévia estava aberta. Leia os XMLs novamente."}
    return {
        "sucesso": True,
        "romaneio": row.pk,
        "qtd_notas_novas": len(ineditas),
        "descartadas": descartadas,
        "valor_faturado": valor_total,
        "valor_acumulado_bluesoft": acumulado,
    }


def lancar_snapshot(row: TmsRomaneio, snapshot: float, username: str) -> str:
    """Aplica o acumulado digitado e grava só o delta. Devolve o erro, ou vazio."""
    snapshot = round_money(snapshot)
    with transaction.atomic():
        if row.pk:
            calculo_antigo = calcular_grupo_valores(grupo_rows(row.data, row.cd_origem, row.loja_destino, exclude_id=row.pk))
            if not calculo_antigo.ok:
                return calculo_antigo.error or "A sequência atual desta loja precisa ser revisada antes da edição."
        else:
            calculo_antigo = None
        linha_id = row.pk or 2_000_000_000
        created = row.created_at or timezone.now()
        # No Worker, a edição zera o valor da carga e usa o número do formulário como snapshot.
        calculo, _linha = resolver_snapshot(
            row.data,
            row.cd_origem,
            row.loja_destino,
            snapshot,
            row.numero_romaneio,
            linha_id,
            created,
            valor_total_atual=0,
        )
        if not calculo.ok:
            return calculo.error or "O acumulado informado é menor que um lançamento posterior desta loja."
        if calculo_antigo:
            _gravar(calculo_antigo.updates, username)
        _gravar(calculo.updates, username, skip_id=linha_id)
        row.valor_total_carga = calculo.deltas.get(linha_id, 0)
        row.valor_acumulado_bluesoft = calculo.snapshots.get(linha_id, snapshot)
        row.atualizado_por = username
        row.save()
    return ""


def recalcular_exclusao(row: TmsRomaneio, username: str) -> str:
    if not valor_participa(row.status):
        return ""
    calculo = calcular_grupo_valores(grupo_rows(row.data, row.cd_origem, row.loja_destino, exclude_id=row.pk))
    if not calculo.ok:
        return calculo.error or "A sequência de valores desta loja precisa ser revisada antes da exclusão."
    _gravar(calculo.updates, username)
    return ""


def recalcular_status(row: TmsRomaneio, novo_status: str, username: str) -> tuple[bool, str]:
    """Porta o bloco de tmsRomaneioStatus que mexe no acumulado. Não grava o status."""
    nova_data = status_operational_date(row.data, novo_status)
    mudou_participacao = valor_participa(row.status) != valor_participa(novo_status)
    mudou_data = nova_data != row.data
    if not mudou_participacao and not mudou_data:
        row.data = nova_data
        return True, ""
    antigo = calcular_grupo_valores(grupo_rows(row.data, row.cd_origem, row.loja_destino, exclude_id=row.pk))
    if not antigo.ok:
        return False, antigo.error or "A sequência de valores desta loja precisa ser revisada antes de mudar o status."
    if mudou_data:
        novos = grupo_rows(nova_data, row.cd_origem, row.loja_destino, exclude_id=row.pk)
        projetada = _Linha(
            id=row.pk,
            created_at=row.created_at,
            numero_romaneio=row.numero_romaneio,
            valor_total_carga=row.valor_total_carga,
            valor_acumulado_bluesoft=row.valor_acumulado_bluesoft,
        )
        linhas = [*novos, projetada] if valor_participa(novo_status) else novos
        calculo = calcular_grupo_valores(linhas)
        if not calculo.ok:
            return False, calculo.error or "A sequência de valores desta loja precisa ser revisada antes de mudar o status."
        _gravar(antigo.updates, username)
        _gravar(calculo.updates, username, skip_id=row.pk)
        if valor_participa(novo_status):
            row.valor_total_carga = calculo.deltas.get(row.pk, row.valor_total_carga)
            row.valor_acumulado_bluesoft = calculo.snapshots.get(row.pk, row.valor_acumulado_bluesoft)
    else:
        linhas = [*grupo_rows(row.data, row.cd_origem, row.loja_destino, exclude_id=row.pk)]
        if valor_participa(novo_status):
            linhas.append(_linha(row))
        calculo = calcular_grupo_valores(linhas)
        if not calculo.ok:
            return False, calculo.error or "A sequência de valores desta loja precisa ser revisada antes de mudar o status."
        _gravar(calculo.updates, username, skip_id=row.pk)
        if valor_participa(novo_status):
            row.valor_total_carga = calculo.deltas.get(row.pk, row.valor_total_carga)
            row.valor_acumulado_bluesoft = calculo.snapshots.get(row.pk, row.valor_acumulado_bluesoft)
    row.data = nova_data
    return True, ""
