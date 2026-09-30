param(
    [string]$Url = $env:RENDER_SENTINELA_URL,
    [int]$IntervalSeconds = 120
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if ([string]::IsNullOrWhiteSpace($Url)) {
    $Url = "https://modelo-teste-operacional.onrender.com"
}

$Url = $Url.TrimEnd("/")
$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogFile = Join-Path $RepoRoot "logs_sentinela_render.txt"
$MaxLogBytes = 1024 * 1024

function Write-SentinelaLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Write-Host $line
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    try {
        $item = Get-Item -LiteralPath $LogFile -ErrorAction SilentlyContinue
        if ($item -and $item.Length -gt $MaxLogBytes) {
            $old = "$LogFile.old"
            if (Test-Path -LiteralPath $old) {
                Remove-Item -LiteralPath $old -Force
            }
            Move-Item -LiteralPath $LogFile -Destination $old -Force
        }
    } catch {
        Write-Host "Nao foi possivel rotacionar log: $($_.Exception.Message)"
    }
}

function Invoke-SentinelaPing {
    param([string]$Path)
    $stamp = [Uri]::EscapeDataString((Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmssfff"))
    $target = "$Url$Path"
    if ($target.Contains("?")) {
        $target = "$target&sentinela=windows&t=$stamp"
    } else {
        $target = "$target`?sentinela=windows&t=$stamp"
    }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-WebRequest `
        -Uri $target `
        -UseBasicParsing `
        -TimeoutSec 45 `
        -Headers @{
            "Cache-Control" = "no-cache"
            "User-Agent" = "ModeloTeste-SentinelaWindows/1.0"
        }
    $sw.Stop()
    return "HTTP $($response.StatusCode) $Path $($sw.ElapsedMilliseconds)ms"
}

Write-SentinelaLog "Sentinela Windows iniciada para $Url. Intervalo: ${IntervalSeconds}s."
Write-SentinelaLog "Importante: o notebook pode ficar bloqueado, mas não pode suspender."

$cycle = 0
while ($true) {
    $cycle += 1
    try {
        $result = Invoke-SentinelaPing -Path "/manter-online/"
        Write-SentinelaLog "OK $result"
        if (($cycle % 10) -eq 0) {
            $health = Invoke-SentinelaPing -Path "/health/"
            Write-SentinelaLog "OK $health"
        }
    } catch {
        Write-SentinelaLog "ERRO $($_.Exception.Message)"
        try {
            $login = Invoke-SentinelaPing -Path "/login/"
            Write-SentinelaLog "RECUPERACAO $login"
        } catch {
            Write-SentinelaLog "ERRO_RECUPERACAO $($_.Exception.Message)"
        }
    }
    Start-Sleep -Seconds $IntervalSeconds
}
