$ErrorActionPreference = 'Stop'
$swarmRoot = Split-Path -Parent $PSScriptRoot
$swarmPython = Join-Path $swarmRoot '.venv\Scripts\python.exe'
$swarmHome = Join-Path $env:LOCALAPPDATA 'DeepSeekPeerSwarm'
New-Item -ItemType Directory -Path $swarmHome -Force | Out-Null
try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8767/api/health' -TimeoutSec 2
    if ($health.app -eq 'deepseek-peer-swarm') {
        Start-Process 'http://127.0.0.1:8767'
        exit 0
    }
    throw 'Port 8767 is used by another application.'
} catch [System.Net.WebException] { }
if (-not (Test-Path -LiteralPath $swarmPython)) {
    python -m venv (Join-Path $swarmRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed.' }
}
& $swarmPython -c 'import fastapi, httpx, uvicorn, swarm'
if ($LASTEXITCODE -ne 0) {
    & $swarmPython -m pip install -r (Join-Path $swarmRoot 'requirements.lock')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $swarmPython -m pip install --no-deps -e $swarmRoot
    if ($LASTEXITCODE -ne 0) { throw 'Harness installation failed.' }
}
Start-Process -FilePath $swarmPython -ArgumentList @('-m', 'swarm', '--open') -WorkingDirectory $swarmRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $swarmHome 'server.stdout.log') -RedirectStandardError (Join-Path $swarmHome 'server.stderr.log') | Out-Null
