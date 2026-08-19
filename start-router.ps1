$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
  throw "Virtual environment missing. Run: python -m venv .venv; python -m pip install -r requirements.txt"
}

$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
  throw "Missing .env. Copy .env.example to .env and fill secrets locally."
}

Get-Content $envFile | ForEach-Object {
  $line = $_.Trim()
  if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
    $parts = $line.Split("=", 2)
    [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
  }
}

if (-not $env:SMART_ROUTER_KEY) { throw "SMART_ROUTER_KEY is missing from .env" }
if (-not $env:PROXYPAL_API_KEY) { throw "PROXYPAL_API_KEY is missing from .env" }
if (-not $env:AIBOX_API_KEY) { Write-Warning "AIBOX_API_KEY is missing; AI-BOX routes will remain disabled." }

$xkiroTokenPath = "G:\linhnh\claude\local-bin\xkiro.token"
if (Test-Path $xkiroTokenPath) {
  $xkiroToken = (Get-Content $xkiroTokenPath -Raw).Trim()
  if ($xkiroToken) {
    [Environment]::SetEnvironmentVariable("XKIRO_API_KEY", $xkiroToken, "Process")
    Write-Host "Loaded XKIRO_API_KEY from $xkiroTokenPath"
  }
}

$proxyPolicy = Join-Path $PSScriptRoot "apply-proxypal-policy.ps1"
if (Test-Path $proxyPolicy) {
  & $proxyPolicy -WaitSeconds 60
}

# Stop any stale router instance already bound to the router port before starting.
$port = 8320
$listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
  $pids = $listeners.OwningProcess | Sort-Object -Unique
  foreach ($pid_ in $pids) {
    $proc = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
    if ($proc) {
      Write-Host "Stopping stale router process (PID $pid_) bound to port $port..."
      Stop-Process -Id $pid_ -Force
    }
  }
  Start-Sleep -Milliseconds 500
}

& $python -m uvicorn router:app --host 127.0.0.1 --port $port --no-access-log --log-level ($env:SMART_ROUTER_LOG_LEVEL.ToLower())
