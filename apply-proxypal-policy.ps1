param(
  [int]$WaitSeconds = 60
)

$ErrorActionPreference = "Stop"
$configPath = Join-Path $env:APPDATA "ProxyPal\proxy-config.yaml"
$deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $WaitSeconds))

while (-not (Test-Path $configPath)) {
  if ([DateTime]::UtcNow -ge $deadline) {
    throw "ProxyPal config was not created within $WaitSeconds seconds: $configPath"
  }
  Start-Sleep -Seconds 1
}

while ($null -eq (Get-Process -Name "cli-proxy-api" -ErrorAction SilentlyContinue)) {
  if ([DateTime]::UtcNow -ge $deadline) {
    throw "ProxyPal backend did not start within $WaitSeconds seconds"
  }
  Start-Sleep -Seconds 1
}

# Let ProxyPal finish its startup-time YAML generation before enforcing policy.
Start-Sleep -Seconds 2

$lines = [System.Collections.Generic.List[string]]::new()
[System.IO.File]::ReadAllLines($configPath) | ForEach-Object { [void]$lines.Add($_) }

function Set-TopLevelScalar {
  param(
    [System.Collections.Generic.List[string]]$Lines,
    [string]$Name,
    [string]$Value
  )

  $pattern = "^" + [Regex]::Escape($Name) + ":\s*"
  for ($i = 0; $i -lt $Lines.Count; $i++) {
    if ($Lines[$i] -match $pattern) {
      $Lines[$i] = "${Name}: $Value"
      return
    }
  }

  $anchor = $Lines.Count
  for ($i = 0; $i -lt $Lines.Count; $i++) {
    if ($Lines[$i] -eq "# Quota exceeded behavior") {
      $anchor = $i
      break
    }
  }
  $Lines.Insert($anchor, "${Name}: $Value")
}

Set-TopLevelScalar $lines "passthrough-headers" "true"
Set-TopLevelScalar $lines "request-retry" "1"
Set-TopLevelScalar $lines "max-retry-credentials" "4"
Set-TopLevelScalar $lines "max-retry-interval" "2"
Set-TopLevelScalar $lines "disable-cooling" "false"

$routingIndex = -1
for ($i = 0; $i -lt $lines.Count; $i++) {
  if ($lines[$i] -match "^routing:\s*$") {
    $routingIndex = $i
    break
  }
}
if ($routingIndex -lt 0) {
  [void]$lines.Add("routing:")
  [void]$lines.Add('  strategy: "weighted-round-robin"')
  [void]$lines.Add("  session-affinity: false")
} else {
  $blockEnd = $lines.Count
  for ($i = $routingIndex + 1; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match "^[^\s#][^:]*:\s*") {
      $blockEnd = $i
      break
    }
  }

  $strategyIndex = -1
  $affinityIndex = -1
  for ($i = $routingIndex + 1; $i -lt $blockEnd; $i++) {
    if ($lines[$i] -match "^\s{2}strategy:\s*") { $strategyIndex = $i }
    if ($lines[$i] -match "^\s{2}session-affinity:\s*") { $affinityIndex = $i }
  }

  if ($strategyIndex -ge 0) {
    $lines[$strategyIndex] = '  strategy: "weighted-round-robin"'
  } else {
    $lines.Insert($routingIndex + 1, '  strategy: "weighted-round-robin"')
    $blockEnd++
  }

  if ($affinityIndex -ge 0) {
    $lines[$affinityIndex] = "  session-affinity: false"
  } else {
    $lines.Insert($routingIndex + 2, "  session-affinity: false")
  }
}

$tempPath = "$configPath.tmp-$PID"
try {
  $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
  [System.IO.File]::WriteAllLines($tempPath, $lines, $utf8NoBom)
  Move-Item -LiteralPath $tempPath -Destination $configPath -Force
  # CLIProxyAPI watches content-change events but may ignore an atomic rename on Windows.
  # Rewrite the already-validated content once so the running backend reloads it.
  [System.IO.File]::WriteAllLines($configPath, $lines, $utf8NoBom)
} finally {
  if (Test-Path $tempPath) {
    Remove-Item -LiteralPath $tempPath -Force -Confirm:$false
  }
}

$appConfigPath = Join-Path $env:APPDATA "ProxyPal\config.json"
$appConfig = Get-Content $appConfigPath -Raw | ConvertFrom-Json
$managementKey = [string]$appConfig.managementKey
if (-not $managementKey) {
  throw "ProxyPal management key is unavailable; runtime policy was not reloaded"
}

try {
  $headers = @{ "X-Management-Key" = $managementKey }
  $response = Invoke-WebRequest `
    -Uri "http://127.0.0.1:8317/v0/management/config.yaml" `
    -Method Put `
    -Headers $headers `
    -ContentType "application/yaml" `
    -InFile $configPath `
    -UseBasicParsing `
    -TimeoutSec 20
  if ([int]$response.StatusCode -ne 200) {
    throw "ProxyPal management API returned HTTP $([int]$response.StatusCode)"
  }
} finally {
  $managementKey = $null
  $headers = $null
}

Write-Output "ProxyPal routing/retry policy applied and reloaded without displaying credentials."
