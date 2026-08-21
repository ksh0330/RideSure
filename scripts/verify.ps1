[CmdletBinding()]
param(
    [switch]$SkipInference,
    [switch]$AllowCpu
)

. (Join-Path $PSScriptRoot "common.ps1")
Set-Location -LiteralPath $script:ProjectRoot
Assert-Venv

try {
    & $script:VenvPython -m scripts.checks config
    Assert-LastExitCode "Configuration validation"

    $environmentArgs = @("-m", "scripts.checks", "environment")
    if (-not $AllowCpu) {
        $environmentArgs += "--require-cuda"
    }
    & $script:VenvPython @environmentArgs
    Assert-LastExitCode "Python/GPU/model verification"

    docker info *> $null
    Assert-LastExitCode "Docker Desktop check"
    $composeStatus = docker compose ps --status running --format json neo4j
    Assert-LastExitCode "Neo4j container status"
    if ([string]::IsNullOrWhiteSpace(($composeStatus | Out-String))) {
        throw "Neo4j is not running. Run scripts/start.ps1 first."
    }

    & $script:VenvPython -m scripts.checks database
    Assert-LastExitCode "Neo4j data verification"
    & $script:VenvPython -m scripts.checks javascript
    Assert-LastExitCode "index.html JavaScript syntax verification"

    $serviceArgs = @("-m", "scripts.checks", "services")
    if ($SkipInference) {
        $serviceArgs += "--skip-inference"
    }
    & $script:VenvPython @serviceArgs
    Assert-LastExitCode "Live service verification"
    Write-Host "[ok] All RideSure verification checks passed."
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
