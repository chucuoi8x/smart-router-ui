$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) { throw "Missing .env" }
Get-Content $envFile | ForEach-Object {
  $line = $_.Trim()
  if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
    $parts = $line.Split("=", 2)
    [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
  }
}
if (-not $env:SMART_ROUTER_KEY) { throw "SMART_ROUTER_KEY is missing" }
$headers = @{ Authorization = "Bearer $env:SMART_ROUTER_KEY" }
Invoke-RestMethod "http://127.0.0.1:8320/router/aibox/sync" -Method Post -Headers $headers | ConvertTo-Json -Depth 20
