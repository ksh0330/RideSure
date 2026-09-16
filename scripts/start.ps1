[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "common.ps1")
Set-Location -LiteralPath $script:ProjectRoot
Assert-Venv

& $script:VenvPython -m scripts.checks config
Assert-LastExitCode "Configuration validation"

docker info *> $null
Assert-LastExitCode "Docker Desktop check"
Write-Host "Starting Neo4j v1/v2 (no volume deletion is performed)..."
docker compose --profile v2 up -d --wait neo4j neo4j-v2
if ($LASTEXITCODE -ne 0) {
    throw "Neo4j v1/v2 startup failed. Existing-volume credentials may differ from .env; no volume was deleted."
}

& $script:VenvPython data_insert_v2.py status
Assert-LastExitCode "Neo4j v2 data verification (run data_insert_v2.py import if empty)"

$settings = Get-RideSureSettings
$llmScript = Join-Path $script:ProjectRoot "llm_server.py"
$appScript = Join-Path $script:ProjectRoot "app.py"
$llmPid = Join-Path $script:RunDirectory "llm_server.pid"
$appPid = Join-Path $script:RunDirectory "app.pid"

Start-ManagedPythonProcess `
    "llm_server" `
    $llmScript `
    $llmPid `
    "$($settings.llm_base_url)/health" `
    180
Start-ManagedPythonProcess `
    "app" `
    $appScript `
    $appPid `
    "http://127.0.0.1:$($settings.app_port)/health" `
    60

Write-Host ""
Write-Host "RideSure is ready."
Write-Host "Web:           http://127.0.0.1:$($settings.app_port)"
Write-Host "API docs:      http://127.0.0.1:$($settings.app_port)/docs"
$neo4jHttpPort = if ([string]::IsNullOrWhiteSpace($env:NEO4J_HTTP_PORT)) { 7474 } else { $env:NEO4J_HTTP_PORT }
Write-Host "Neo4j Browser: http://127.0.0.1:$neo4jHttpPort"
$neo4jV2HttpPort = if ([string]::IsNullOrWhiteSpace($env:NEO4J_V2_HTTP_PORT)) { 7475 } else { $env:NEO4J_V2_HTTP_PORT }
Write-Host "Neo4j v2:      http://127.0.0.1:$neo4jV2HttpPort"
Write-Host "Logs:          $script:LogDirectory"
