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
Write-Output "healthz"
Invoke-RestMethod "http://127.0.0.1:8320/healthz"
Write-Output "models"
Invoke-RestMethod "http://127.0.0.1:8320/v1/models" -Headers $headers | ConvertTo-Json -Depth 5
Write-Output "main probe"
$body = @{ model = "claude-router-main"; max_tokens = 16; messages = @(@{ role = "user"; content = "Reply exactly: ROUTER_MAIN_OK" }) } | ConvertTo-Json -Depth 8
Invoke-RestMethod "http://127.0.0.1:8320/v1/messages" -Method Post -Headers $headers -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 8

Write-Output "aibox probe"
if (-not $env:AIBOX_API_KEY) {
  Write-Output "(skipped: AIBOX_API_KEY not set; AI-BOX routes are disabled)"
} else {
  $aiboxModels = @(
    @{ name = "claude-router-aibox-cheap";      expect = "ROUTER_AIBOX_CHEAP_OK" },
    @{ name = "claude-router-aibox-engineering"; expect = "ROUTER_AIBOX_ENGINEERING_OK" },
    @{ name = "claude-router-aibox-review";     expect = "ROUTER_AIBOX_REVIEW_OK" }
  )
  foreach ($m in $aiboxModels) {
    Write-Output "aibox probe: $($m.name)"
    $body = @{ model = $m.name; max_tokens = 16; messages = @(@{ role = "user"; content = "Reply exactly: $($m.expect)" }) } | ConvertTo-Json -Depth 8
    Invoke-RestMethod "http://127.0.0.1:8320/v1/messages" -Method Post -Headers $headers -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 8
  }
}

Write-Output "status"
Invoke-RestMethod "http://127.0.0.1:8320/router/status" -Headers $headers | ConvertTo-Json -Depth 20
