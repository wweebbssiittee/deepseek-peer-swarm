$ErrorActionPreference = 'Stop'
$swarmTokenPath = Join-Path $env:LOCALAPPDATA 'DeepSeekPeerSwarm\access-token.txt'
if (-not (Test-Path -LiteralPath $swarmTokenPath)) { Write-Host 'Swarm has not been started.'; exit 0 }
$swarmToken = (Get-Content -LiteralPath $swarmTokenPath -Raw).Trim()
try {
    Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8767/api/shutdown' -Headers @{ 'X-Swarm-Token' = $swarmToken } -TimeoutSec 40 | Out-Null
    Write-Host 'Swarm paused and dashboard stopped. Start again, then Resume to continue.'
} catch { Write-Host 'Could not reach the swarm. It may already be stopped.' }
