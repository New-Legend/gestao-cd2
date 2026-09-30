$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $AppDir ".venv\Scripts\python.exe"
$Waitress = Join-Path $AppDir ".venv\Scripts\waitress-serve.exe"
$PgBin = "C:\Krill_CD_Web\postgresql_portatil\pgsql\bin"
$PgData = "C:\Krill_CD_Web\banco_postgres_dados"
$PgLog = "C:\Krill_CD_Web\postgresql.log"
$Port = 8000

function Get-LanIps {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.PrefixOrigin -ne "WellKnown"
        } |
        Select-Object -ExpandProperty IPAddress -Unique
}

if (-not (Test-Path $VenvPython)) {
    throw ("Ambiente Python n" + [char]0x00E3 + "o encontrado. Reinstale o sistema ou rode o instalador.")
}

& (Join-Path $PgBin "pg_ctl.exe") -D $PgData status *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Iniciando banco PostgreSQL local..."
    & (Join-Path $PgBin "pg_ctl.exe") -D $PgData -l $PgLog start
    Start-Sleep -Seconds 3
}

Push-Location $AppDir
& $VenvPython manage.py migrate --noinput
& $VenvPython manage.py collectstatic --noinput *> $null
Pop-Location

$ips = @(Get-LanIps)
Write-Host ""
Write-Host "ASSISTENTE CD KRILL - MODO SERVIDOR DA REDE" -ForegroundColor Yellow
Write-Host ""
Write-Host "Neste computador, abra:" -ForegroundColor Cyan
Write-Host "  http://127.0.0.1:$Port/login/"
Write-Host ""
Write-Host ("Nos outros computadores da mesma rede, tente um destes endere" + [char]0x00E7 + "os:") -ForegroundColor Cyan
if ($ips.Count -eq 0) {
    Write-Host ("  N" + [char]0x00E3 + "o encontrei IP da rede. Confira se o cabo/Wi-Fi est" + [char]0x00E1 + " conectado.")
} else {
    foreach ($ip in $ips) {
        Write-Host "  http://$ip`:$Port/login/"
    }
}
Write-Host ""
Write-Host "Importante: este PC precisa ficar ligado enquanto os outros estiverem usando."
Write-Host ("Se algum colega n" + [char]0x00E3 + "o conseguir acessar, rode LIBERAR_FIREWALL_REDE.bat como administrador.")
Write-Host ""

Start-Process "http://127.0.0.1:$Port/login/"
Push-Location $AppDir
& $Waitress --listen=0.0.0.0:$Port assistente_krill_web.wsgi:application
Pop-Location

Write-Host ""
Write-Host "Servidor encerrado. Tentando gerar backup de fechamento..." -ForegroundColor Yellow
Push-Location $AppDir
& $VenvPython manage.py shell -c "from painel.utils import create_backup, network_backup_dir; create_backup('local'); create_backup('rede_50') if network_backup_dir() else None; print('Backup de fechamento conclu\u00eddo.')"
Pop-Location
