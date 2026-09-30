$Port = 8000
$RuleName = "Central CD Krill Web porta $Port"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)

Write-Host "Liberacao de firewall para o Central CD Krill" -ForegroundColor Yellow
Write-Host "Porta: $Port"
Write-Host ""

if (-not $isAdmin) {
    Write-Host "Este comando precisa ser executado como administrador." -ForegroundColor Red
    Write-Host "No PC servidor, clique com o botao direito em LIBERAR_FIREWALL_REDE.bat e escolha:"
    Write-Host "Executar como administrador"
    exit 1
}

$existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host ("A regra j" + [char]0x00E1 + " existe. Atualizando...")
    $existing | Remove-NetFirewallRule
}

New-NetFirewallRule `
    -DisplayName $RuleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $Port `
    -Profile Domain,Private `
    | Out-Null

Write-Host "Firewall liberado para a rede local." -ForegroundColor Green
