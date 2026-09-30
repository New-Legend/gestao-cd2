$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $AppDir ".env.reserva"

function New-LocalSecret {
    return "django-insecure-reserva-" + ([guid]::NewGuid().ToString("N")) + ([guid]::NewGuid().ToString("N")) + ([guid]::NewGuid().ToString("N"))
}

Write-Host ""
Write-Host "CENTRAL CD KRILL - CONFIGURAR BANCO DO MODO RESERVA" -ForegroundColor Yellow
Write-Host ""
Write-Host "Cole aqui a DATABASE_URL atual do banco online." -ForegroundColor Cyan
Write-Host "Dica: no Render, abra Environment, revele/copie DATABASE_URL e cole aqui." -ForegroundColor Cyan
Write-Host "Nada disso sera enviado para o GitHub." -ForegroundColor Cyan
Write-Host ""

$databaseUrl = Read-Host "DATABASE_URL"
$databaseUrl = $databaseUrl.Trim()

if (-not $databaseUrl) {
    throw "DATABASE_URL vazia. Configuracao cancelada."
}
if ($databaseUrl -notmatch "^postgres(ql)?://") {
    throw "DATABASE_URL invalida. Ela precisa comecar com postgres:// ou postgresql://"
}

$secret = New-LocalSecret
$lines = @(
    "# Central CD Krill - ambiente reserva do notebook",
    "# Arquivo local. Nao enviar para GitHub.",
    "DATABASE_URL=$databaseUrl",
    "DJANGO_SECRET_KEY=$secret",
    "DEBUG=False",
    "ALLOWED_HOSTS=*,.trycloudflare.com,localhost,127.0.0.1",
    "CSRF_TRUSTED_ORIGINS=https://*.trycloudflare.com,http://127.0.0.1:8000,http://localhost:8000",
    "SESSION_COOKIE_SECURE=False",
    "CSRF_COOKIE_SECURE=False"
)

Set-Content -LiteralPath $EnvFile -Value $lines -Encoding UTF8
Write-Host ""
Write-Host "Banco reserva configurado em .env.reserva." -ForegroundColor Green
Write-Host "Agora rode ABRIR_RESERVA_CLOUDFLARE.bat para testar." -ForegroundColor Green
