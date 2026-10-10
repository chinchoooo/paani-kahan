# Open Paani Kahan on a phone on the same Wi-Fi (Windows + Finch/Docker on WSL2).
# Run in PowerShell as Administrator:   powershell -ExecutionPolicy Bypass -File scripts\phone-lan.ps1
# Undo:                                 powershell -ExecutionPolicy Bypass -File scripts\phone-lan.ps1 -Remove
param([switch]$Remove, [int]$Port = 8081)

if ($Remove) {
  netsh interface portproxy delete v4tov4 listenport=$Port listenaddress=0.0.0.0 | Out-Null
  Remove-NetFirewallRule -DisplayName "Paani Kahan phone" -ErrorAction SilentlyContinue
  Write-Host "Removed the phone access rule."
  exit
}

# The app is published on this laptop's localhost:8080 (WSL forwards it). Expose it on the Wi-Fi as :8081.
netsh interface portproxy delete v4tov4 listenport=$Port listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy add v4tov4 listenport=$Port listenaddress=0.0.0.0 connectport=8080 connectaddress=127.0.0.1
if (-not (Get-NetFirewallRule -DisplayName "Paani Kahan phone" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "Paani Kahan phone" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private,Domain | Out-Null
}

$ip = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
       Select-Object -First 1).IPv4Address.IPAddress
$url = "http://${ip}:$Port"
Write-Host ""
Write-Host "On your phone (same Wi-Fi), open:  $url" -ForegroundColor Green
Write-Host "Location, offline and install need HTTPS: use 'finch compose --profile phone up -d' for that."
Write-Host ""
Write-Host "To show this as a QR code in the app, add this line to .env and restart the app:"
Write-Host "  PHONE_LAN_URL=$url"
Write-Host "If the phone can't connect: set this Wi-Fi to 'Private' in Windows network settings."
