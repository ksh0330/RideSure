[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot "common.ps1")
Set-Location -LiteralPath $script:ProjectRoot

$llmScript = Join-Path $script:ProjectRoot "llm_server.py"
$appScript = Join-Path $script:ProjectRoot "app.py"
Stop-ManagedPythonProcess "app" $appScript (Join-Path $script:RunDirectory "app.pid")
Stop-ManagedPythonProcess "llm_server" $llmScript (Join-Path $script:RunDirectory "llm_server.pid")

docker info *> $null
if ($LASTEXITCODE -eq 0) {
    docker compose --profile v2 stop neo4j neo4j-v2
    Assert-LastExitCode "Neo4j v1/v2 stop"
    Write-Host "[stop] Neo4j v1/v2 containers stopped; named data/log volumes were preserved."
}
else {
    Write-Warning "Docker Desktop is unavailable; Neo4j could not be stopped."
}
