$Port = 8000
Write-Host ("Endere" + [char]0x00E7 + "os para acessar o Central CD Krill") -ForegroundColor Yellow
Write-Host ""
Write-Host "Neste computador:"
Write-Host "  http://127.0.0.1:$Port/login/"
Write-Host ""
Write-Host "Em outros computadores da mesma rede:"
$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
        $_.IPAddress -notlike "127.*" -and
        $_.IPAddress -notlike "169.254.*" -and
        $_.PrefixOrigin -ne "WellKnown"
    } |
    Select-Object -ExpandProperty IPAddress -Unique
if ($ips) {
    foreach ($ip in $ips) {
        Write-Host "  http://$ip`:$Port/login/"
    }
} else {
    Write-Host ("  N" + [char]0x00E3 + "o encontrei IP da rede.")
}
