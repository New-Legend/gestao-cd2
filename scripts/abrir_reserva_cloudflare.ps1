$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $AppDir ".venv\Scripts\python.exe"
$Waitress = Join-Path $AppDir ".venv\Scripts\waitress-serve.exe"
$CloudflaredCandidates = @(
    "C:\Program Files (x86)\cloudflared\cloudflared.exe",
    "C:\Program Files\cloudflared\cloudflared.exe",
    "cloudflared.exe"
)
$Port = 8000
$LogDir = Join-Path $AppDir "logs_reserva"
$WaitressOut = Join-Path $LogDir "waitress.out.log"
$WaitressErr = Join-Path $LogDir "waitress.err.log"
$TunnelOut = Join-Path $LogDir "cloudflared.out.log"
$TunnelErr = Join-Path $LogDir "cloudflared.err.log"

function Import-KeyValueEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        return
    }

    Get-Content $Path -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }
        $key, $value = $line.Split("=", 2)
        $key = $key.Trim()
        $value = $value.Trim().Trim('"')
        if ($key) {
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

function Find-Cloudflared {
    foreach ($candidate in $CloudflaredCandidates) {
        try {
            $cmd = Get-Command $candidate -ErrorAction Stop
            if ($cmd.Source) {
                return $cmd.Source
            }
            if ($cmd.Path) {
                return $cmd.Path
            }
        } catch {
            if (Test-Path $candidate) {
                return $candidate
            }
        }
    }
    throw "cloudflared nao encontrado. Instale pelo winget: winget install --id Cloudflare.cloudflared"
}

function Test-PortListening {
    param([int]$Port)
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    return $null -ne $listener
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory=$true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Comando falhou: $FilePath $($Arguments -join ' ')"
    }
}

function Wait-ForTunnelUrl {
    param([string]$OutFile, [string]$ErrFile)
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        $text = ""
        if (Test-Path $OutFile) {
            $text += "`n" + (Get-Content $OutFile -Raw -ErrorAction SilentlyContinue)
        }
        if (Test-Path $ErrFile) {
            $text += "`n" + (Get-Content $ErrFile -Raw -ErrorAction SilentlyContinue)
        }
        $match = [regex]::Match($text, "https://[a-zA-Z0-9-]+\.trycloudflare\.com")
        if ($match.Success) {
            return $match.Value
        }
        Start-Sleep -Seconds 2
    }
    return $null
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Remove-Item $WaitressOut, $WaitressErr, $TunnelOut, $TunnelErr -Force -ErrorAction SilentlyContinue

if (-not (Test-Path $VenvPython)) {
    throw "Ambiente Python nao encontrado em .venv. Prepare o projeto antes de abrir o servidor reserva."
}
if (-not (Test-Path $Waitress)) {
    throw "waitress-serve nao encontrado. Rode: .\.venv\Scripts\pip.exe install -r requirements.txt"
}

$Cloudflared = Find-Cloudflared

Import-KeyValueEnv (Join-Path $AppDir ".env")
Import-KeyValueEnv (Join-Path $AppDir ".env.reserva")

if (-not $env:DJANGO_SECRET_KEY) {
    $env:DJANGO_SECRET_KEY = "django-insecure-central-cd-krill-reserva-local"
}
$env:DEBUG = if ($env:DEBUG) { $env:DEBUG } else { "False" }
$env:ALLOWED_HOSTS = if ($env:ALLOWED_HOSTS) { $env:ALLOWED_HOSTS } else { "*,.trycloudflare.com,localhost,127.0.0.1" }
$env:CSRF_TRUSTED_ORIGINS = if ($env:CSRF_TRUSTED_ORIGINS) { $env:CSRF_TRUSTED_ORIGINS } else { "https://*.trycloudflare.com,http://127.0.0.1:$Port,http://localhost:$Port" }
$env:SESSION_COOKIE_SECURE = if ($env:SESSION_COOKIE_SECURE) { $env:SESSION_COOKIE_SECURE } else { "False" }
$env:CSRF_COOKIE_SECURE = if ($env:CSRF_COOKIE_SECURE) { $env:CSRF_COOKIE_SECURE } else { "False" }

Write-Host ""
Write-Host "CENTRAL CD KRILL - SERVIDOR RESERVA CLOUDFLARE" -ForegroundColor Yellow
Write-Host ""
Write-Host "Este modo e plano B. O PWA oficial continua sendo o Render." -ForegroundColor Cyan
Write-Host "Nao instale o PWA por este link temporario." -ForegroundColor Cyan
Write-Host ""

if (-not $env:DATABASE_URL) {
    Write-Host "ATENCAO: DATABASE_URL nao configurado." -ForegroundColor Yellow
    Write-Host "O modo reserva foi bloqueado para evitar dados separados do Render." -ForegroundColor Yellow
    Write-Host "Rode CONFIGURAR_BANCO_RESERVA.bat e cole a DATABASE_URL atual do banco online." -ForegroundColor Yellow
    Write-Host ""
    throw "DATABASE_URL ausente no .env.reserva."
}

Push-Location $AppDir
Invoke-Checked $VenvPython manage.py migrate --noinput
Invoke-Checked $VenvPython manage.py collectstatic --noinput
Pop-Location

$waitressProcess = $null
if (Test-PortListening $Port) {
    Write-Host "Porta $Port ja esta em uso. Vou reaproveitar o servidor local existente." -ForegroundColor Yellow
} else {
    Write-Host "Iniciando servidor local na porta $Port..."
    $waitressProcess = Start-Process -FilePath $Waitress -ArgumentList @("--listen=127.0.0.1:$Port", "assistente_krill_web.wsgi:application") -WorkingDirectory $AppDir -RedirectStandardOutput $WaitressOut -RedirectStandardError $WaitressErr -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 5
}

Write-Host "Abrindo tunnel Cloudflare..."
$tunnelProcess = Start-Process -FilePath $Cloudflared -ArgumentList @("tunnel", "--url", "http://127.0.0.1:$Port", "--no-autoupdate") -WorkingDirectory $AppDir -RedirectStandardOutput $TunnelOut -RedirectStandardError $TunnelErr -PassThru -WindowStyle Hidden
$tunnelUrl = Wait-ForTunnelUrl -OutFile $TunnelOut -ErrFile $TunnelErr

Write-Host ""
if ($tunnelUrl) {
    Write-Host "LINK RESERVA TEMPORARIO:" -ForegroundColor Green
    Write-Host "  $tunnelUrl/login/" -ForegroundColor Green
    Write-Host ""
    Set-Content -Path (Join-Path $AppDir "LINK_RESERVA_ATUAL.txt") -Value @(
        "Central CD Krill - link reserva temporario",
        "",
        "$tunnelUrl/login/",
        "",
        "Gerado em: $(Get-Date -Format 'dd/MM/yyyy HH:mm:ss')",
        "Nao instalar PWA por este link. Usar apenas em emergencia."
    ) -Encoding UTF8
    Start-Process "$tunnelUrl/login/"
} else {
    Write-Host "Nao consegui capturar o link automaticamente." -ForegroundColor Yellow
    Write-Host "Veja os logs em: $LogDir" -ForegroundColor Yellow
}

Write-Host "Logs:"
Write-Host "  $WaitressOut"
Write-Host "  $WaitressErr"
Write-Host "  $TunnelOut"
Write-Host "  $TunnelErr"
Write-Host ""
Write-Host "Deixe esta janela aberta enquanto o modo reserva estiver em uso."
Write-Host "Pressione ENTER para encerrar o tunnel reserva."
[void](Read-Host)

if ($tunnelProcess -and -not $tunnelProcess.HasExited) {
    Stop-Process -Id $tunnelProcess.Id -Force -ErrorAction SilentlyContinue
}
if ($waitressProcess -and -not $waitressProcess.HasExited) {
    Stop-Process -Id $waitressProcess.Id -Force -ErrorAction SilentlyContinue
}

Write-Host "Servidor reserva encerrado."
