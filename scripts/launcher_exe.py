import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


APP_URL = "http://127.0.0.1:8000/login/"
HEALTH_URL = "http://127.0.0.1:8000/status-saude/"
PORT = "8000"
INSTALL_ROOT = Path(r"C:\Krill_CD_Web")
DEFAULT_MASTER_PASSWORD = "460852"


def exe_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def log_path():
    return exe_dir() / "Assistente_CD_Krill_launcher.log"


def write_log(message):
    stamp = time.strftime("%d/%m/%Y %H:%M:%S")
    with log_path().open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def show_error(message):
    write_log(message)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Central CD Krill", message)
        root.destroy()
    except Exception:
        pass


def hidden_kwargs():
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def server_is_online():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=3) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


def is_package_dir(path):
    return (
        (path / "scripts" / "instalar_no_pc.ps1").exists()
        and (path / "sistema_web" / "manage.py").exists()
        and (path / "wheelhouse").exists()
        and (path / "instaladores").exists()
    )


def installation_ready():
    return (
        (INSTALL_ROOT / "sistema_web" / "manage.py").exists()
        and (INSTALL_ROOT / "venv" / "Scripts" / "python.exe").exists()
        and (INSTALL_ROOT / "venv" / "Scripts" / "waitress-serve.exe").exists()
        and (INSTALL_ROOT / "postgresql_portatil" / "pgsql" / "bin" / "psql.exe").exists()
    )


def run_installer_if_needed():
    if installation_ready():
        return

    package_root = exe_dir()
    if not is_package_dir(package_root):
        raise FileNotFoundError(
            "Instalacao incompleta e pacote do pendrive nao encontrado. "
            "Abra o Assistente_CD_Krill.exe dentro da pasta PACOTE_PENDRIVE_ASSISTENTE_KRILL."
        )

    installer = package_root / "scripts" / "instalar_no_pc.ps1"
    write_log("Instalacao local nao encontrada. Iniciando instalador automatico.")
    env = os.environ.copy()
    env.setdefault("KRILL_MASTER_PASSWORD", DEFAULT_MASTER_PASSWORD)
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(installer),
        ],
        cwd=str(package_root),
        env=env,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("A instalacao automatica nao foi concluida.")
    if not installation_ready():
        raise RuntimeError("A instalacao terminou, mas o sistema nao ficou completo em C:\\Krill_CD_Web.")


def find_app_dir():
    here = exe_dir()
    candidates = [
        here / "ambiente_web_krill",
        here / "sistema_web",
        Path(r"C:\Krill_CD_Web\sistema_web"),
    ]
    for candidate in candidates:
        if (candidate / "manage.py").exists():
            return candidate
    raise FileNotFoundError(
        "Não encontrei a pasta do sistema web. "
        "Deixe o Assistente_CD_Krill.exe na pasta logistica ou instale pelo pacote do pendrive."
    )


def find_runtime(app_dir):
    candidates = [
        (app_dir / ".venv" / "Scripts" / "python.exe", app_dir / ".venv" / "Scripts" / "waitress-serve.exe"),
        (app_dir.parent / "venv" / "Scripts" / "python.exe", app_dir.parent / "venv" / "Scripts" / "waitress-serve.exe"),
        (Path(r"C:\Krill_CD_Web\venv\Scripts\python.exe"), Path(r"C:\Krill_CD_Web\venv\Scripts\waitress-serve.exe")),
    ]
    for python, waitress in candidates:
        if python.exists() and waitress.exists():
            return python, waitress
    raise FileNotFoundError(
        "Não encontrei o Python interno do sistema. "
        "Se estiver no PC do trabalho, rode primeiro o instalador do pendrive."
    )


def read_config(app_dir):
    values = {}
    paths = [
        app_dir / "config_postgresql_local.txt",
        app_dir.parent / "config_postgresql_local.txt",
        Path(r"C:\Krill_CD_Web\config_postgresql_local.txt"),
    ]
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                key, value = stripped.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def postgres_paths(app_dir):
    cfg = read_config(app_dir)
    pg_bin = Path(cfg.get("POSTGRES_BIN") or r"C:\Krill_CD_Web\postgresql_portatil\pgsql\bin")
    pg_data = Path(cfg.get("POSTGRES_DATA") or r"C:\Krill_CD_Web\banco_postgres_dados")
    pg_log = pg_data.parent / "postgresql.log"
    return cfg, pg_bin, pg_data, pg_log


def database_is_online(app_dir):
    cfg, pg_bin, _, _ = postgres_paths(app_dir)
    psql = pg_bin / "psql.exe"
    if not psql.exists():
        write_log(f"psql.exe não encontrado em {psql}.")
        return False

    env = os.environ.copy()
    env["PGPASSWORD"] = cfg.get("POSTGRES_PASSWORD", "")
    command = [
        str(psql),
        "-h",
        cfg.get("POSTGRES_HOST", "127.0.0.1"),
        "-p",
        cfg.get("POSTGRES_PORT", "55432"),
        "-U",
        cfg.get("POSTGRES_USER", "postgres"),
        "-d",
        cfg.get("POSTGRES_DB", "assistente_krill"),
        "-tAc",
        "select 1;",
    ]
    try:
        result = subprocess.run(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
            **hidden_kwargs(),
        )
    except Exception as exc:
        write_log(f"Falha ao testar o banco: {exc}")
        return False

    if result.returncode == 0 and result.stdout.strip() == "1":
        return True

    detail = (result.stderr or result.stdout or "").strip()
    write_log(f"Banco não respondeu ao teste. Código {result.returncode}. {detail}")
    return False


def wait_database(app_dir, seconds=45):
    for _ in range(seconds):
        if database_is_online(app_dir):
            return True
        time.sleep(1)
    return False


def pg_ctl_status(pg_ctl, pg_data):
    return subprocess.run(
        [str(pg_ctl), "-D", str(pg_data), "status"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **hidden_kwargs(),
    ).returncode


def start_postgres(app_dir):
    _, pg_bin, pg_data, pg_log = postgres_paths(app_dir)
    pg_ctl = pg_bin / "pg_ctl.exe"
    if not pg_ctl.exists() or not pg_data.exists():
        raise FileNotFoundError("PostgreSQL portátil não encontrado.")

    write_log("Iniciando PostgreSQL local.")
    subprocess.run(
        [str(pg_ctl), "-D", str(pg_data), "-l", str(pg_log), "-w", "-t", "60", "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=70,
        **hidden_kwargs(),
    )
    return wait_database(app_dir)


def stop_postgres(app_dir):
    _, pg_bin, pg_data, _ = postgres_paths(app_dir)
    pg_ctl = pg_bin / "pg_ctl.exe"
    if not pg_ctl.exists() or not pg_data.exists():
        return
    write_log("Parando PostgreSQL local para reinício seguro.")
    subprocess.run(
        [str(pg_ctl), "-D", str(pg_data), "-m", "fast", "-w", "-t", "30", "stop"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=40,
        **hidden_kwargs(),
    )


def ensure_postgres(app_dir):
    _, pg_bin, pg_data, _ = postgres_paths(app_dir)
    pg_ctl = pg_bin / "pg_ctl.exe"
    if not pg_ctl.exists() or not pg_data.exists():
        raise FileNotFoundError("PostgreSQL portátil não encontrado.")

    if database_is_online(app_dir):
        return True

    if pg_ctl_status(pg_ctl, pg_data) == 0:
        write_log("PostgreSQL estava iniciado, mas não aceitava consultas. Reiniciando.")
        stop_postgres(app_dir)

    if start_postgres(app_dir):
        return True

    raise RuntimeError("O banco de dados não respondeu depois da tentativa de iniciar.")


def stop_web_servers():
    if os.name != "nt":
        return
    script = (
        "$needle='assistente_krill_web.wsgi'; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and $_.Name -notlike 'powershell*' -and $_.CommandLine -like \"*$needle*\" } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            **hidden_kwargs(),
        )
    except Exception as exc:
        write_log(f"Não consegui encerrar servidores web antigos: {exc}")


def run_manage(python, app_dir, *args):
    result = subprocess.run(
        [str(python), "manage.py", *args],
        cwd=str(app_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
        **hidden_kwargs(),
    )
    if result.returncode != 0:
        write_log(result.stderr or result.stdout or f"manage.py {' '.join(args)} falhou.")
        raise RuntimeError(f"Falha ao preparar o sistema ({' '.join(args)}).")


def start_server(waitress, app_dir):
    write_log("Iniciando servidor web.")
    subprocess.Popen(
        [str(waitress), f"--listen=0.0.0.0:{PORT}", "assistente_krill_web.wsgi:application"],
        cwd=str(app_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **hidden_kwargs(),
    )
    for _ in range(30):
        if server_is_online():
            return True
        time.sleep(1)
    return False


def main():
    try:
        run_installer_if_needed()
        app_dir = find_app_dir()
        python, waitress = find_runtime(app_dir)
        server_online = server_is_online()
        db_online = database_is_online(app_dir)

        if server_online and db_online:
            webbrowser.open(APP_URL)
            return

        ensure_postgres(app_dir)

        if server_online and not db_online:
            stop_web_servers()
            run_manage(python, app_dir, "migrate", "--noinput")
            run_manage(python, app_dir, "collectstatic", "--noinput")
            if not start_server(waitress, app_dir):
                raise RuntimeError("O servidor não respondeu depois de reiniciar.")
        elif not server_is_online():
            stop_web_servers()
            run_manage(python, app_dir, "migrate", "--noinput")
            run_manage(python, app_dir, "collectstatic", "--noinput")
            if not start_server(waitress, app_dir):
                raise RuntimeError("O servidor não respondeu depois de iniciar.")

        webbrowser.open(APP_URL)
    except Exception as exc:
        show_error(
            "Não consegui abrir o Central CD Krill.\n\n"
            f"Detalhe: {exc}\n\n"
            f"Um log foi salvo em:\n{log_path()}"
        )


if __name__ == "__main__":
    main()
