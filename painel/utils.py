import json
import os
import platform
import shutil
import socket
import subprocess
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .models import BackupLog, ConfiguracaoSistema


_CONFIG_CACHE = {}
_CONFIG_CACHE_SECONDS = 20


def read_key_value_file(path):
    values = {}
    path = Path(path)
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def get_config(chave, default=""):
    now = time.monotonic()
    cached = _CONFIG_CACHE.get(chave)
    if cached and now < cached["expires_at"]:
        return cached["value"]
    obj = ConfiguracaoSistema.objects.filter(chave=chave).first()
    value = obj.valor if obj else default
    _CONFIG_CACHE[chave] = {"value": value, "expires_at": now + _CONFIG_CACHE_SECONDS}
    return value


def set_config(chave, valor, user=None, descricao=""):
    obj, _created = ConfiguracaoSistema.objects.update_or_create(
        chave=chave,
        defaults={"valor": valor, "descricao": descricao, "atualizado_por": user},
    )
    _CONFIG_CACHE[chave] = {"value": valor, "expires_at": time.monotonic() + _CONFIG_CACHE_SECONDS}
    return obj


def bool_config(chave, default=False):
    valor = str(get_config(chave, "sim" if default else "nao") or "").strip().lower()
    return valor in {"1", "sim", "true", "on", "ativo"}


def int_config(chave, default):
    try:
        return int(str(get_config(chave, str(default)) or default).strip())
    except (TypeError, ValueError):
        return default


def date_config(chave):
    valor = str(get_config(chave, "") or "").strip()
    if not valor:
        return None
    try:
        return timezone.datetime.fromisoformat(valor).date()
    except ValueError:
        return None


def pilot_trial_state(today=None):
    if not bool_config("piloto_teste_ativo", False):
        return {"enabled": False, "status": "inactive"}
    start = date_config("piloto_teste_inicio")
    if not start:
        return {"enabled": False, "status": "not_configured"}
    today = today or timezone.localdate()
    trial_days = max(1, int_config("piloto_teste_dias", 30))
    read_only_days = max(0, int_config("piloto_teste_consulta_dias", 7))
    trial_end = start + timezone.timedelta(days=trial_days)
    read_only_end = trial_end + timezone.timedelta(days=read_only_days)
    if today < trial_end:
        status = "active"
    elif today < read_only_end:
        status = "read_only"
    else:
        status = "expired"
    return {
        "enabled": True,
        "status": status,
        "start": start,
        "trial_end": trial_end,
        "read_only_end": read_only_end,
        "days_left": max(0, (trial_end - today).days),
        "read_only_days_left": max(0, (read_only_end - today).days),
    }


def postgres_config():
    cfg = read_key_value_file(Path(settings.BASE_DIR) / "config_postgresql_local.txt")
    cfg.setdefault("POSTGRES_HOST", "127.0.0.1")
    cfg.setdefault("POSTGRES_PORT", "55432")
    cfg.setdefault("POSTGRES_USER", "postgres")
    cfg.setdefault("POSTGRES_DB", "assistente_krill")
    cfg.setdefault("POSTGRES_BIN", r"C:\Krill_CD_Web\postgresql_portatil\pgsql\bin")
    return cfg


def get_lan_ips():
    ips = []
    try:
        hostname = socket.gethostname()
        for result in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = result[4][0]
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def server_urls(port=8000):
    urls = [f"http://127.0.0.1:{port}/login/"]
    urls.extend(f"http://{ip}:{port}/login/" for ip in get_lan_ips())
    return urls


def database_status():
    try:
        with connection.cursor() as cursor:
            cursor.execute("select current_database(), current_user, inet_server_port()")
            banco, usuario, porta = cursor.fetchone()
        return {"ok": True, "banco": banco, "usuario": usuario, "porta": porta, "mensagem": "Conectado"}
    except Exception as exc:
        return {"ok": False, "banco": "", "usuario": "", "porta": "", "mensagem": str(exc)}


def disk_status(path=None):
    target = path or r"C:\Krill_CD_Web"
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        usage = shutil.disk_usage(Path(settings.BASE_DIR).anchor)
    return {
        "total_gb": round(usage.total / (1024**3), 1),
        "livre_gb": round(usage.free / (1024**3), 1),
        "usado_gb": round(usage.used / (1024**3), 1),
        "percentual_usado": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
    }


def recent_log_lines(path, limit=35):
    path = Path(path)
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []


def diagnostics_snapshot():
    cfg = postgres_config()
    pg_bin = Path(cfg.get("POSTGRES_BIN", ""))
    pg_data = Path(cfg.get("POSTGRES_DATA", r"C:\Krill_CD_Web\banco_postgres_dados"))
    install_root = Path(r"C:\Krill_CD_Web")
    network_dir = network_backup_dir()
    return {
        "gerado_em": timezone.localtime(),
        "computador": socket.gethostname(),
        "sistema": platform.platform(),
        "python": platform.python_version(),
        "debug": settings.DEBUG,
        "versao": settings.APP_VERSION,
        "base_dir": str(settings.BASE_DIR),
        "install_root": str(install_root),
        "urls": server_urls(),
        "ips": get_lan_ips(),
        "banco": database_status(),
        "disco": disk_status(str(install_root) if install_root.exists() else None),
        "postgres": {
            "host": cfg.get("POSTGRES_HOST", "127.0.0.1"),
            "porta": cfg.get("POSTGRES_PORT", "55432"),
            "database": cfg.get("POSTGRES_DB", "assistente_krill"),
            "bin": str(pg_bin),
            "bin_ok": pg_bin.exists(),
            "data": str(pg_data),
            "data_ok": pg_data.exists(),
        },
        "pasta_50": {
            "configurada": get_config("pasta_50_backup", ""),
            "acessivel": bool(network_dir),
            "caminho_valido": str(network_dir or ""),
            "backup_automatico": get_config("backup_automatico_50", "sim"),
        },
        "logs": {
            "launcher": recent_log_lines(settings.BASE_DIR.parent / "Assistente_CD_Krill_launcher.log"),
            "postgres": recent_log_lines(install_root / "postgresql.log"),
            "servidor": recent_log_lines(settings.BASE_DIR / "servidor_web_erros.log"),
        },
    }


def backup_base_dir():
    base = Path(r"C:\Krill_CD_Web\backups")
    if not base.parent.exists():
        base = Path(settings.BASE_DIR) / "backups"
    base.mkdir(parents=True, exist_ok=True)
    return base


def network_backup_dir():
    value = get_config("pasta_50_backup", "").strip()
    if not value:
        return None
    path = Path(value)
    return path if path.exists() and path.is_dir() else None


def backup_filename(prefix="assistente_krill"):
    stamp = timezone.localtime().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}.dump"


def run_pg_dump(destination):
    cfg = postgres_config()
    pg_dump = Path(cfg["POSTGRES_BIN"]) / "pg_dump.exe"
    env = os.environ.copy()
    env["PGPASSWORD"] = cfg.get("POSTGRES_PASSWORD", "")
    cmd = [
        str(pg_dump),
        "-h",
        cfg["POSTGRES_HOST"],
        "-p",
        cfg["POSTGRES_PORT"],
        "-U",
        cfg["POSTGRES_USER"],
        "-Fc",
        "-f",
        str(destination),
        cfg["POSTGRES_DB"],
    ]
    completed = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=180)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Falha ao gerar backup").strip())


def cleanup_old_backups(folder, keep=15):
    dumps = sorted(Path(folder).glob("assistente_krill_*.dump"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in dumps[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def create_backup(destino="local", user=None):
    folder = backup_base_dir() if destino == "local" else network_backup_dir()
    if folder is None:
        log = BackupLog.objects.create(
            destino="rede_50",
            status="erro",
            mensagem="Pasta .50 não configurada ou inacessível.",
            criado_por=user,
        )
        return log

    folder.mkdir(parents=True, exist_ok=True)
    file_path = folder / backup_filename()
    try:
        run_pg_dump(file_path)
        size = file_path.stat().st_size if file_path.exists() else 0
        cleanup_old_backups(folder, keep=15)
        log = BackupLog.objects.create(
            destino=destino,
            status="ok",
            arquivo=str(file_path),
            tamanho_bytes=size,
            mensagem="Backup criado com sucesso.",
            criado_por=user,
        )
        return log
    except Exception as exc:
        BackupLog.objects.create(
            destino=destino,
            status="erro",
            arquivo=str(file_path),
            mensagem=str(exc),
            criado_por=user,
        )
        raise


def backup_if_due(user=None):
    automatic = get_config("backup_automatico_50", "sim").lower() in {"sim", "true", "1"}
    if not automatic or not network_backup_dir():
        return None
    last_ok = BackupLog.objects.filter(destino="rede_50", status="ok").first()
    if last_ok and timezone.now() - last_ok.criado_em < timedelta(hours=20):
        return last_ok
    try:
        return create_backup("rede_50", user=user)
    except Exception:
        return None


def publish_server_address(user=None, port=8000):
    folder = network_backup_dir()
    if folder is None:
        return None
    urls = server_urls(port=port)
    payload = {
        "sistema": "Modelo de Teste",
        "atualizado_em": timezone.localtime().strftime("%d/%m/%Y %H:%M:%S"),
        "computador": socket.gethostname(),
        "enderecos": urls,
        "principal": urls[1] if len(urls) > 1 else urls[0],
    }
    (folder / "assistente_cd_krill_endereco.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    text = [
        "Modelo de Teste - ENDEREÇO OFICIAL",
        f"Atualizado em: {payload['atualizado_em']}",
        f"Computador servidor: {payload['computador']}",
        "",
        "Endereços para acessar:",
        *urls,
        "",
        "Se não abrir, confirme se o PC servidor está ligado e se o firewall foi liberado.",
    ]
    (folder / "assistente_cd_krill_endereco.txt").write_text("\n".join(text), encoding="utf-8")
    set_config("ultimo_endereco_publicado", payload["principal"], user=user, descricao="Endereço publicado na pasta .50")
    return payload
