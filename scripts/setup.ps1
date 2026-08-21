[CmdletBinding()]
param(
    [switch]$SkipModelDownload,
    [switch]$AllowCpu
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
Set-Location -LiteralPath $ProjectRoot

function Assert-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code $LASTEXITCODE)."
    }
}

function Get-DotEnvValue([string]$Name) {
    $prefix = "$Name="
    $line = Get-Content -LiteralPath ".env" | Where-Object {
        $_.TrimStart().StartsWith($prefix)
    } | Select-Object -Last 1
    if ($null -eq $line) { return $null }
    return $line.Substring($line.IndexOf("=") + 1).Trim().Trim('"').Trim("'")
}

Write-Host "Checking prerequisites..."
if ($PSVersionTable.PSVersion.Major -lt 5) {
    throw "PowerShell 5.1 or newer is required."
}
py -3.12 --version
Assert-Exit "Python 3.12 check"
docker version --format "Docker server {{.Server.Version}}"
Assert-Exit "Docker Desktop check"
docker compose version
Assert-Exit "Docker Compose check"
node --version
Assert-Exit "Node.js check (used only for index.html JavaScript verification)"
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
if ($LASTEXITCODE -ne 0 -and -not $AllowCpu) {
    throw "An NVIDIA GPU/driver is required for the default verified setup. Use -AllowCpu only for an intentionally slow CPU-only setup."
}

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Warning ".env was created from .env.example and was not overwritten."
    Write-Host "Set NEO4J_PASS and KAKAO_MAP_JAVASCRIPT_KEY in .env, then run this command again."
    exit 2
}
Write-Host "Existing .env will be preserved."

$requiredNames = @(
    "NEO4J_URI", "NEO4J_USER", "NEO4J_PASS", "MODEL_ID", "HF_HOME",
    "LLM_BASE_URL", "APP_HOST", "APP_PORT", "LLM_HOST", "LLM_PORT",
    "KAKAO_MAP_JAVASCRIPT_KEY"
)
$invalidNames = @()
foreach ($name in $requiredNames) {
    $value = Get-DotEnvValue $name
    if ([string]::IsNullOrWhiteSpace($value) -or $value -match '^(your_|change_me|replace_me)') {
        $invalidNames += $name
    }
}
if ($invalidNames.Count -gt 0) {
    throw "Required .env setting(s) are missing or placeholders: $($invalidNames -join ', '). Values were not printed."
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Creating isolated Python 3.12 virtual environment..."
    py -3.12 -m venv .venv
    Assert-Exit "Virtual environment creation"
}

& $VenvPython -m pip install --upgrade "pip<27"
Assert-Exit "pip bootstrap"
& $VenvPython -m pip install --index-url https://download.pytorch.org/whl/cu128 -r requirements-cuda.txt
Assert-Exit "PyTorch CUDA 12.8 installation"
& $VenvPython -m pip install -r requirements-lock.txt
Assert-Exit "Python dependency installation"

& $VenvPython -m scripts.checks config
Assert-Exit "Configuration validation"

Write-Host "Starting Neo4j without deleting or recreating named volumes..."
docker compose up -d --wait neo4j
if ($LASTEXITCODE -ne 0) {
    throw "Neo4j startup failed. Existing-volume credentials may differ from .env. NEO4J_AUTH cannot change an existing volume password; no volume was deleted."
}

& $VenvPython data_insert.py
Assert-Exit "Empty/complete/partial database inspection and CSV import"

if (-not $SkipModelDownload) {
    & $VenvPython -m scripts.download_model
    Assert-Exit "EXAONE model download/validation"
}

$environmentArgs = @("-m", "scripts.checks", "environment")
if (-not $AllowCpu) { $environmentArgs += "--require-cuda" }
& $VenvPython @environmentArgs
Assert-Exit "Python/GPU/model verification"

& (Join-Path $PSScriptRoot "start.ps1")
Assert-Exit "RideSure service startup"
$verifyArgs = @()
if ($AllowCpu) { $verifyArgs += "-AllowCpu" }
$verifyCommand = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
    (Join-Path $PSScriptRoot "verify.ps1")
) + $verifyArgs
& powershell @verifyCommand
Assert-Exit "Final RideSure verification"
Write-Host "Setup complete. Services are running; use scripts/stop.ps1 when finished."
