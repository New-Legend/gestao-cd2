"""
Teste controlado de carga no Render oficial da Central CD Krill.

O script conecta no banco configurado em .env.reserva, cria dados com prefixo
STRESS_, acessa o Render via HTTP com varios usuarios e limpa os registros ao
final. Ele tambem gera um dumpdata local antes de escrever qualquer coisa.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import random
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import requests


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RENDER_URL = "https://assistente-cd-krill-teste.onrender.com"
PASSWORD = "123456"
STRESS_DATE = date(2099, 1, 31)
CSRF_RE = re.compile(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']')


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"Arquivo de ambiente nao encontrado: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def setup_django(env_file: Path):
    load_env_file(env_file)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "assistente_krill_web.settings")
    sys.path.insert(0, str(BASE_DIR))
    import django

    django.setup()


def django_imports():
    from django.contrib.auth.models import User
    from django.core.management import call_command
    from django.utils import timezone
    from painel.models import (
        ChecklistFrota,
        ColaboradorAusencia,
        ColaboradorFerias,
        Equipamento,
        EquipamentoManutencao,
        ExpedicaoPlanejamento,
        ExpedicaoVinculo,
        Loja,
        PerfilAcesso,
        PessoaTurno,
        SistemaNotificacao,
        VeiculoFrota,
    )
    from painel.registry import ALL_PERMISSION_KEYS

    return {
        "User": User,
        "call_command": call_command,
        "timezone": timezone,
        "ChecklistFrota": ChecklistFrota,
        "ColaboradorAusencia": ColaboradorAusencia,
        "ColaboradorFerias": ColaboradorFerias,
        "Equipamento": Equipamento,
        "EquipamentoManutencao": EquipamentoManutencao,
        "ExpedicaoPlanejamento": ExpedicaoPlanejamento,
        "ExpedicaoVinculo": ExpedicaoVinculo,
        "Loja": Loja,
        "PerfilAcesso": PerfilAcesso,
        "PessoaTurno": PessoaTurno,
        "SistemaNotificacao": SistemaNotificacao,
        "VeiculoFrota": VeiculoFrota,
        "ALL_PERMISSION_KEYS": ALL_PERMISSION_KEYS,
    }


@dataclass
class StressUser:
    username: str
    first_name: str
    last_name: str
    cargo: str
    cd: str
    permissions: list[str]


DRIVERS = [
    ("allan", "Allan", "STH8D95", True),
    ("anderson", "Anderson", "STR6A35", False),
    ("brendon", "Brendon", "EYA1H33", False),
    ("edilson", "Edilson", "ESP1316", True),
    ("eduardo", "Eduardo", "FXJ7564", False),
    ("godoi", "Godoi", "GAQ4571", True),
    ("jefferson", "Jefferson", "CQU6J55", True),
    ("johnlennon", "John Lennon", "EKJ1380", True),
    ("josepaulo", "Jose Paulo", "FJD2J56", True),
    ("kenidy", "Kenidy", "FPN1F09", False),
    ("kleber", "Kleber", "STRESSKLB", False),
    ("marciel", "Marciel", "EGG7260", False),
    ("muller", "Muller", "FWH8J76", False),
    ("pablokalid", "Pablo Kalid", "SUJ6J21", True),
    ("pauloneto", "Paulo Neto", "FGA3738", False),
    ("paulosergio", "Paulo Sergio", "GJR4D46", False),
    ("renato", "Renato", "STY0F57", True),
    ("robervaldo", "Robervaldo", "GEZ1B94", True),
    ("marluza", "Marluza", "STRESSMLZ", False),
]


def stress_users(run_id: str) -> list[StressUser]:
    master_perms = ["__ALL__"]
    frota_perms = [
        "painel",
        "frota_hub",
        "painel_frota",
        "checklist_frota",
        "carregamento_veiculos",
        "aprovacoes_carregamento",
        "expedicao_planejamento",
        "relatorio_paletes_cd",
        "vincular_cargas_expedicao",
        "aprovar_lancamento_manual_expedicao",
        "notificar_carga_vinculada",
        "notificar_alteracao_carga",
        "notificar_motorista_vinculado",
        "notificar_checklist_motorista",
        "receber_alertas_expedicao",
        "visualizar_cds_unificados",
        "consultar_registros",
        "criar_registros",
    ]
    expedicao_perms = [
        "painel",
        "expedicao",
        "expedicao_planejamento",
        "relatorio_paletes_cd",
        "receber_alertas_expedicao",
        "visualizar_cds_unificados",
        "consultar_registros",
        "criar_registros",
    ]
    colaboradores_perms = [
        "painel",
        "colaboradores_hub",
        "ferias_colaboradores",
        "ausencias_colaboradores",
        "pessoas_turno",
        "funcoes_turno",
        "alertas_ferias",
        "mapa_calor_ferias",
        "capacidade_operacao",
        "visualizar_gargalos_colaboradores",
        "gerenciar_ferias_colaboradores",
        "notificacoes_ferias_colaboradores",
        "consultar_registros",
        "criar_registros",
    ]
    users = [
        StressUser(f"stress_master_{run_id}", "Stress", "Master", "master", "806", master_perms),
        StressUser(f"stress_mauricio_{run_id}", "Stress Mauricio", "Frota", "supervisor_frota", "801", frota_perms),
        StressUser(f"stress_marcela_{run_id}", "Stress Marcela", "Frota", "assistente", "801", frota_perms),
        StressUser(f"stress_leilane_{run_id}", "Stress Leilane", "Expedicao", "supervisor_conferencia_expedicao", "806", frota_perms),
        StressUser(f"stress_stephanie_{run_id}", "Stress Stephanie", "Expedicao", "supervisor_conferencia_expedicao", "801", expedicao_perms),
        StressUser(f"stress_expedicao801_{run_id}", "Stress Expedicao", "801", "assistente", "801", expedicao_perms),
        StressUser(f"stress_expedicao806_{run_id}", "Stress Expedicao", "806", "assistente", "806", expedicao_perms),
        StressUser(f"stress_andressa_{run_id}", "Stress Andressa", "Colaboradores", "analista", "806", colaboradores_perms),
        StressUser(f"stress_naka_{run_id}", "Stress Naka", "Gestor", "gestor_cd", "806", frota_perms + colaboradores_perms),
    ]
    for login, nome, _placa, _plataforma in DRIVERS:
        users.append(
            StressUser(
                f"stress_{login}_{run_id}",
                nome,
                "Motorista",
                "motorista",
                "801",
                ["painel", "checklist_frota"],
            )
        )
    return users


def backup_database(imports: dict, run_id: str) -> Path:
    backup_dir = BASE_DIR / "backups"
    backup_dir.mkdir(exist_ok=True)
    output = backup_dir / f"backup_pre_stress_render_{run_id}.json"
    labels = [
        "auth.User",
        "painel.PerfilAcesso",
        "painel.Loja",
        "painel.VeiculoFrota",
        "painel.ExpedicaoPlanejamento",
        "painel.ExpedicaoVinculo",
        "painel.ChecklistFrota",
        "painel.ColaboradorFerias",
        "painel.ColaboradorAusencia",
        "painel.PessoaTurno",
        "painel.Equipamento",
        "painel.EquipamentoManutencao",
        "painel.SistemaNotificacao",
    ]
    imports["call_command"](
        "dumpdata",
        *labels,
        "--natural-foreign",
        "--natural-primary",
        output=str(output),
    )
    return output


def cleanup(imports: dict, run_id: str) -> dict[str, int]:
    User = imports["User"]
    stress_users_qs = User.objects.filter(username__startswith="stress_").filter(username__endswith=run_id)
    stress_user_ids = list(stress_users_qs.values_list("id", flat=True))
    models = [
        imports["ExpedicaoVinculo"],
        imports["ExpedicaoPlanejamento"],
        imports["ChecklistFrota"],
        imports["ColaboradorAusencia"],
        imports["ColaboradorFerias"],
        imports["PessoaTurno"],
        imports["EquipamentoManutencao"],
        imports["Equipamento"],
        imports["SistemaNotificacao"],
    ]
    counts: dict[str, int] = {}
    for model in models:
        if model.__name__ == "SistemaNotificacao":
            qs = (
                model.objects.filter(titulo__icontains=run_id)
                | model.objects.filter(mensagem__icontains=run_id)
                | model.objects.filter(usuario_id__in=stress_user_ids)
            )
        else:
            qs = model.objects.filter(observacao__icontains=run_id)
            if hasattr(model, "criado_por"):
                qs = qs | model.objects.filter(criado_por_id__in=stress_user_ids)
        deleted, _ = qs.delete()
        counts[model.__name__] = deleted
    vf_qs = imports["VeiculoFrota"].objects.filter(motorista__startswith=f"STRESS {run_id}")
    if stress_user_ids:
        vf_qs = vf_qs | imports["VeiculoFrota"].objects.filter(criado_por_id__in=stress_user_ids)
    vf_deleted, _ = vf_qs.delete()
    counts["VeiculoFrota"] = vf_deleted
    users_deleted, _ = stress_users_qs.delete()
    counts["User"] = users_deleted
    return counts


def seed_data(imports: dict, run_id: str, rows_multiplier: int) -> list[StressUser]:
    User = imports["User"]
    PerfilAcesso = imports["PerfilAcesso"]
    Loja = imports["Loja"]
    VeiculoFrota = imports["VeiculoFrota"]
    ExpedicaoPlanejamento = imports["ExpedicaoPlanejamento"]
    ExpedicaoVinculo = imports["ExpedicaoVinculo"]
    ChecklistFrota = imports["ChecklistFrota"]
    ColaboradorFerias = imports["ColaboradorFerias"]
    ColaboradorAusencia = imports["ColaboradorAusencia"]
    PessoaTurno = imports["PessoaTurno"]
    Equipamento = imports["Equipamento"]
    EquipamentoManutencao = imports["EquipamentoManutencao"]
    all_permissions = list(imports["ALL_PERMISSION_KEYS"])

    users = stress_users(run_id)
    created_users = {}
    for item in users:
        user, _created = User.objects.update_or_create(
            username=item.username,
            defaults={
                "first_name": item.first_name[:150],
                "last_name": item.last_name[:150],
                "email": "",
                "is_active": True,
                "is_staff": False,
                "is_superuser": item.cargo == "master",
            },
        )
        user.set_password(PASSWORD)
        user.save()
        perms = all_permissions if item.permissions == ["__ALL__"] else sorted(set(item.permissions))
        PerfilAcesso.objects.update_or_create(
            user=user,
            defaults={"cargo": item.cargo, "cd_padrao": item.cd, "permissoes": perms},
        )
        created_users[item.username] = user

    for login, nome, placa, plataforma in DRIVERS:
        username = f"stress_{login}_{run_id}"
        VeiculoFrota.objects.create(
            cd_unidade="801",
            data=STRESS_DATE,
            motorista=f"STRESS {run_id} {nome}",
            placa=placa,
            tipo_caminhao="Plataforma" if plataforma else "Truck",
            usuario_motorista=created_users.get(username),
            ativo=True,
            observacao=f"{run_id} cadastro temporario para teste de stress",
            criado_por=created_users.get(f"stress_master_{run_id}"),
        )

    lojas = list(Loja.objects.filter(ativa=True).order_by("codigo", "nome")[:30])
    if not lojas:
        raise SystemExit("Nenhuma loja ativa encontrada para o teste.")

    planejamento_rows = []
    idx = 0
    for loja in lojas:
        for cd in ("801", "806"):
            planejamento_rows.append(
                ExpedicaoPlanejamento(
                    cd_unidade=cd,
                    data=STRESS_DATE,
                    loja=loja.nome,
                    qtd_paletes=8 + (idx % 10),
                    pode_remontar=idx % 3 == 0,
                    qtd_remontavel=idx % 3,
                    status="planejado",
                    observacao=f"{run_id} saldo temporario {idx}",
                    criado_por=created_users.get(f"stress_expedicao{cd}_{run_id}"),
                )
            )
            idx += 1
    ExpedicaoPlanejamento.objects.bulk_create(planejamento_rows, batch_size=100, ignore_conflicts=True)

    veiculos = list(VeiculoFrota.objects.filter(motorista__startswith=f"STRESS {run_id}"))
    vinculos = []
    for idx in range(max(220, rows_multiplier * 140)):
        loja = lojas[idx % len(lojas)]
        veiculo = veiculos[idx % len(veiculos)]
        cd = "801" if idx % 3 != 0 else "806"
        vinculos.append(
            ExpedicaoVinculo(
                cd_unidade=cd,
                data=STRESS_DATE,
                loja=loja.nome,
                placa=veiculo.placa,
                motorista=veiculo.motorista,
                qtd_paletes=(idx % 12) + 1,
                periodo="manha" if idx % 2 == 0 else "tarde",
                status="planejado",
                observacao=f"{run_id} vinculo temporario {idx}",
                criado_por=created_users.get(f"stress_mauricio_{run_id}"),
            )
        )
    ExpedicaoVinculo.objects.bulk_create(vinculos, batch_size=100)

    checklists = []
    for idx in range(max(300, rows_multiplier * 160)):
        login, nome, placa, _plataforma = DRIVERS[idx % len(DRIVERS)]
        checklists.append(
            ChecklistFrota(
                cd_unidade="801" if idx % 2 == 0 else "806",
                data=STRESS_DATE,
                tipo_checklist="saida" if idx % 3 else "retorno",
                motorista=f"STRESS {run_id} {nome}",
                veiculo="Caminhao",
                placa=placa,
                loja_destino=lojas[idx % len(lojas)].nome,
                km_inicial=10000 + idx,
                km_final=10100 + idx if idx % 3 == 0 else 0,
                observacao=f"{run_id} checklist temporario {idx}",
                criado_por=created_users.get(f"stress_{login}_{run_id}"),
            )
        )
    ChecklistFrota.objects.bulk_create(checklists, batch_size=100)

    setores = ["separacao", "recebimento", "expedicao", "conferencia", "frota", "motorista"]
    pessoas = []
    ferias_rows = []
    ausencias = []
    for idx in range(max(140, rows_multiplier * 70)):
        setor = setores[idx % len(setores)]
        pessoas.append(
            PessoaTurno(
                cd_unidade="801" if idx % 2 == 0 else "806",
                data=STRESS_DATE,
                setor=setor,
                setor_detalhado=f"STRESS rua {idx % 8}",
                turno="manha" if idx % 2 == 0 else "tarde",
                funcao="Operador" if setor != "motorista" else "Motorista",
                quadro_atual=20,
                planejado=22,
                ativos_dia=18,
                atestados=idx % 3,
                afastados=idx % 2,
                ferias=idx % 4,
                folgas=idx % 2,
                faltas_sem_justificativa=idx % 2,
                observacao=f"{run_id} quadro temporario {idx}",
                criado_por=created_users.get(f"stress_andressa_{run_id}"),
            )
        )
        ferias_rows.append(
            ColaboradorFerias(
                cd_unidade="801" if idx % 2 == 0 else "806",
                data=STRESS_DATE,
                colaborador=f"STRESS {run_id} Colaborador {idx}",
                setor=setor,
                setor_detalhado=f"STRESS area {idx % 5}",
                funcao="Operador",
                inicio_ferias=STRESS_DATE + timedelta(days=idx % 10),
                fim_ferias=STRESS_DATE + timedelta(days=(idx % 10) + 10),
                retorno_previsto=STRESS_DATE + timedelta(days=(idx % 10) + 11),
                status="programado",
                notificar_supervisor=True,
                observacao=f"{run_id} ferias temporarias {idx}",
                criado_por=created_users.get(f"stress_andressa_{run_id}"),
            )
        )
        ausencias.append(
            ColaboradorAusencia(
                cd_unidade="801" if idx % 2 == 0 else "806",
                data=STRESS_DATE,
                colaborador=f"STRESS {run_id} Ausente {idx}",
                setor=setor,
                setor_detalhado=f"STRESS area {idx % 5}",
                funcao="Operador",
                tipo="atestado",
                inicio=STRESS_DATE,
                fim=STRESS_DATE + timedelta(days=idx % 4),
                status="ativo",
                notificar_supervisor=True,
                observacao=f"{run_id} ausencia temporaria {idx}",
                criado_por=created_users.get(f"stress_andressa_{run_id}"),
            )
        )
    PessoaTurno.objects.bulk_create(pessoas, batch_size=100)
    ColaboradorFerias.objects.bulk_create(ferias_rows, batch_size=100)
    ColaboradorAusencia.objects.bulk_create(ausencias, batch_size=100)

    equipamentos = []
    for idx in range(max(100, rows_multiplier * 50)):
        equipamentos.append(
            Equipamento(
                cd_unidade="801" if idx % 2 == 0 else "806",
                data=STRESS_DATE,
                colaborador=f"STRESS {run_id} Colaborador {idx}",
                setor=setores[idx % len(setores)],
                tipo="coletor",
                equipamento=f"Coletor STRESS {idx}",
                patrimonio=f"STRESS-{run_id}-{idx:04d}",
                coletor_tipo="Coletor",
                status="em_uso",
                observacao=f"{run_id} equipamento temporario {idx}",
                criado_por=created_users.get(f"stress_andressa_{run_id}"),
            )
        )
    equipamentos = Equipamento.objects.bulk_create(equipamentos, batch_size=100)
    manutencoes = []
    for idx, eq in enumerate(equipamentos[: max(60, rows_multiplier * 30)]):
        manutencoes.append(
            EquipamentoManutencao(
                cd_unidade=eq.cd_unidade,
                data=STRESS_DATE,
                equipamento_cadastrado=eq,
                patrimonio=eq.patrimonio,
                equipamento=eq.equipamento,
                tipo="coletor",
                problema="Teste de stress",
                destino="TI",
                responsavel="STRESS",
                status="aberto",
                observacao=f"{run_id} manutencao temporaria {idx}",
                criado_por=created_users.get(f"stress_andressa_{run_id}"),
            )
        )
    EquipamentoManutencao.objects.bulk_create(manutencoes, batch_size=100)
    return users


def csrf_from(html: str) -> str:
    match = CSRF_RE.search(html)
    return match.group(1) if match else ""


def login_session(base_url: str, username: str) -> tuple[requests.Session | None, str]:
    session = requests.Session()
    session.headers.update({"User-Agent": "CentralCDKrill-StressTest/1.0"})
    try:
        response = session.get(f"{base_url}/login/", timeout=45)
        token = csrf_from(response.text)
        payload = {"username": username, "password": PASSWORD, "csrfmiddlewaretoken": token}
        headers = {"Referer": f"{base_url}/login/"}
        login_response = session.post(f"{base_url}/login/", data=payload, headers=headers, timeout=45, allow_redirects=True)
        if login_response.status_code >= 400 or "/login/" in login_response.url:
            return None, f"login falhou {username}: status {login_response.status_code} url {login_response.url}"
        return session, ""
    except Exception as exc:
        return None, f"login erro {username}: {exc}"


def timed_get(session: requests.Session, url: str, path: str) -> tuple[bool, float, int, str, str]:
    start = time.perf_counter()
    try:
        response = session.get(url, timeout=30)
        elapsed = (time.perf_counter() - start) * 1000
        ok = 200 <= response.status_code < 400
        return ok, elapsed, response.status_code, "", path
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return False, elapsed, 0, str(exc), path


def render_load(base_url: str, users: Iterable[StressUser], duration: int, workers: int, login_workers: int) -> dict:
    users = list(users)
    sessions: list[tuple[StressUser, requests.Session]] = []
    login_errors = []

    def login_worker(user: StressUser):
        session, error = login_session(base_url, user.username)
        return user, session, error

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(login_workers, max(1, len(users)))) as executor:
        futures = [executor.submit(login_worker, user) for user in users]
        for future in concurrent.futures.as_completed(futures):
            user, session, error = future.result()
            if session:
                sessions.append((user, session))
            else:
                login_errors.append(error)

    paths = [
        "/",
        "/frota/",
        "/colaboradores/",
        "/modulo/checklist_frota/",
        "/modulo/expedicao_planejamento/",
        "/modulo/carregamento_veiculos/",
        "/modulo/expedicao/",
        "/modulo/relatorio_paletes_cd/",
        "/modulo/ferias_colaboradores/",
        "/modulo/ausencias_colaboradores/",
        "/modulo/equipamentos/",
        "/health/",
    ]
    deadline = time.time() + duration
    results: list[tuple[bool, float, int, str]] = []

    def worker(worker_id: int):
        local_results = []
        rnd = random.Random(worker_id)
        while time.time() < deadline:
            user, session = rnd.choice(sessions)
            path = rnd.choice(paths)
            sep = "&" if "?" in path else "?"
            url = f"{base_url}{path}{sep}data={STRESS_DATE.isoformat()}&stress=1"
            local_results.append(timed_get(session, url, path))
            time.sleep(rnd.uniform(0.03, 0.16))
        return local_results

    if sessions:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker, idx) for idx in range(workers)]
            for future in concurrent.futures.as_completed(futures):
                results.extend(future.result())

    latencies = [elapsed for _ok, elapsed, _status, _error, _path in results]
    errors = [item for item in results if not item[0]]
    status_counts: dict[int, int] = {}
    path_buckets: dict[str, list[float]] = {}
    for _ok, elapsed, status, _error, path in results:
        status_counts[status] = status_counts.get(status, 0) + 1
        path_buckets.setdefault(path, []).append(elapsed)

    def pct(value: int) -> float:
        if not latencies:
            return 0.0
        sorted_values = sorted(latencies)
        index = min(len(sorted_values) - 1, int(len(sorted_values) * value / 100))
        return sorted_values[index]

    return {
        "sessions": len(sessions),
        "login_errors": login_errors,
        "requests": len(results),
        "errors": len(errors),
        "status_counts": status_counts,
        "path_metrics": {
            path: {
                "requests": len(values),
                "avg_ms": statistics.mean(values),
                "max_ms": max(values),
            }
            for path, values in sorted(path_buckets.items())
        },
        "avg_ms": statistics.mean(latencies) if latencies else 0.0,
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "max_ms": max(latencies) if latencies else 0.0,
        "sample_errors": errors[:10],
    }


def write_report(path: Path, run_id: str, backup_path: Path, seed_seconds: float, load_result: dict, cleanup_counts: dict[str, int], cleanup_error: str = ""):
    lines = [
        "Central CD Krill - Teste controlado no Render oficial",
        f"Run ID: {run_id}",
        f"Data/hora: {datetime.now():%d/%m/%Y %H:%M:%S}",
        f"Data temporaria usada: {STRESS_DATE.isoformat()}",
        f"Backup antes do teste: {backup_path}",
        "",
        "Resumo de carga HTTP no Render",
        f"Sessoes autenticadas: {load_result['sessions']}",
        f"Falhas de login: {len(load_result['login_errors'])}",
        f"Requisicoes: {load_result['requests']}",
        f"Erros HTTP/timeout: {load_result['errors']}",
        f"Status: {load_result['status_counts']}",
        f"Tempo medio: {load_result['avg_ms']:.0f} ms",
        f"P50: {load_result['p50_ms']:.0f} ms",
        f"P95: {load_result['p95_ms']:.0f} ms",
        f"Maximo: {load_result['max_ms']:.0f} ms",
        f"Tempo para semear dados no banco: {seed_seconds:.1f}s",
        "",
        "Falhas de login",
    ]
    lines.extend([f"- {err}" for err in load_result["login_errors"]] or ["- nenhuma"])
    lines.extend(["", "Amostra de erros"])
    for ok, elapsed, status, error, path in load_result["sample_errors"]:
        lines.append(f"- {path} ok={ok} status={status} tempo={elapsed:.0f}ms erro={error}")
    if not load_result["sample_errors"]:
        lines.append("- nenhuma")
    lines.extend(["", "Tempo por tela"])
    for url_path, metrics in load_result.get("path_metrics", {}).items():
        lines.append(
            f"- {url_path}: {metrics['requests']} req | medio {metrics['avg_ms']:.0f} ms | max {metrics['max_ms']:.0f} ms"
        )
    lines.extend(["", "Limpeza"])
    if cleanup_error:
        lines.append(f"ERRO NA LIMPEZA: {cleanup_error}")
    for model, count in cleanup_counts.items():
        lines.append(f"- {model}: {count}")
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.reserva")
    parser.add_argument("--base-url", default=DEFAULT_RENDER_URL)
    parser.add_argument("--duration", type=int, default=90)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--login-workers", type=int, default=4)
    parser.add_argument("--rows-multiplier", type=int, default=6)
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()

    env_file = BASE_DIR / args.env_file
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    setup_django(env_file)
    imports = django_imports()

    report_dir = BASE_DIR / "backups"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"relatorio_stress_render_{run_id}.txt"

    print(f"Iniciando teste {run_id} no Render oficial.", flush=True)
    print("Gerando backup local...", flush=True)
    backup_path = backup_database(imports, run_id)
    cleanup(imports, run_id)

    cleanup_counts: dict[str, int] = {}
    cleanup_error = ""
    try:
        print("Criando usuarios e dados temporarios STRESS_...", flush=True)
        seed_start = time.perf_counter()
        users = seed_data(imports, run_id, args.rows_multiplier)
        seed_seconds = time.perf_counter() - seed_start
        print(f"Rodando carga HTTP por {args.duration}s com {args.workers} workers...", flush=True)
        load_result = render_load(args.base_url.rstrip("/"), users, args.duration, args.workers, args.login_workers)
    finally:
        if args.keep_data:
            cleanup_counts = {"mantido": 0}
        else:
            print("Limpando dados temporarios STRESS_...", flush=True)
            try:
                cleanup_counts = cleanup(imports, run_id)
            except Exception as exc:  # noqa: BLE001 - precisa ir para o relatorio
                cleanup_error = str(exc)

    write_report(report_path, run_id, backup_path, seed_seconds, load_result, cleanup_counts, cleanup_error)
    print(f"Relatorio: {report_path}", flush=True)
    print(f"Sessoes: {load_result['sessions']} | Requests: {load_result['requests']} | Erros: {load_result['errors']}", flush=True)
    print(f"P50: {load_result['p50_ms']:.0f}ms | P95: {load_result['p95_ms']:.0f}ms | Max: {load_result['max_ms']:.0f}ms", flush=True)
    if load_result["login_errors"]:
        print(f"Falhas de login: {len(load_result['login_errors'])}", flush=True)
    if cleanup_error:
        print(f"ERRO NA LIMPEZA: {cleanup_error}", flush=True)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
