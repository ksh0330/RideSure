Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
$script:VenvPython = Join-Path $script:ProjectRoot ".venv\Scripts\python.exe"
$script:RunDirectory = Join-Path $script:ProjectRoot ".run"
$script:LogDirectory = Join-Path $script:ProjectRoot "logs"

function Assert-LastExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code $LASTEXITCODE)."
    }
}

function Assert-Venv {
    if (-not (Test-Path -LiteralPath $script:VenvPython)) {
        throw "Virtual environment is missing. Run scripts/setup.ps1 first."
    }
}

function Get-RideSureSettings {
    Assert-Venv
    $json = & $script:VenvPython -m scripts.checks settings
    Assert-LastExitCode "Configuration read"
    return $json | ConvertFrom-Json
}

function Test-HttpReady([string]$Uri) {
    try {
        $response = Invoke-WebRequest -Uri $Uri -TimeoutSec 3 -UseBasicParsing
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Wait-HttpReady([string]$Name, [string]$Uri, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpReady $Uri) {
            Write-Host "[ok] $Name is ready: $Uri"
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "$Name did not become ready within $TimeoutSeconds seconds: $Uri"
}

function Get-ManagedProcess([string]$PidFile, [string]$ScriptPath) {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $null
    }
    $rawPid = (Get-Content -Raw -LiteralPath $PidFile).Trim()
    if ($rawPid -notmatch '^\d+$') {
        Remove-Item -LiteralPath $PidFile -Force
        return $null
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$rawPid" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $PidFile -Force
        return $null
    }
    if ($process.CommandLine -notlike "*$ScriptPath*") {
        throw "PID file $PidFile points to a process not owned by this RideSure script."
    }
    return $process
}

function Start-ManagedPythonProcess(
    [string]$Name,
    [string]$ScriptPath,
    [string]$PidFile,
    [string]$HealthUri,
    [int]$TimeoutSeconds
) {
    $managed = Get-ManagedProcess $PidFile $ScriptPath
    if ($null -ne $managed) {
        Wait-HttpReady $Name $HealthUri $TimeoutSeconds
        Write-Host "[skip] $Name is already managed by PID $($managed.ProcessId)."
        return
    }
    if (Test-HttpReady $HealthUri) {
        throw "$Name port already answers HTTP but has no RideSure PID file. Stop that process manually to avoid taking ownership of an unrelated process."
    }

    New-Item -ItemType Directory -Force -Path $script:RunDirectory, $script:LogDirectory | Out-Null
    $stdout = Join-Path $script:LogDirectory "$Name.stdout.log"
    $stderr = Join-Path $script:LogDirectory "$Name.stderr.log"
    $argument = '"{0}"' -f $ScriptPath
    $startParameters = @{
        FilePath = $script:VenvPython
        ArgumentList = $argument
        WorkingDirectory = $script:ProjectRoot
        RedirectStandardOutput = $stdout
        RedirectStandardError = $stderr
        WindowStyle = "Hidden"
        PassThru = $true
    }
    $process = Start-Process @startParameters
    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ascii
    try {
        Wait-HttpReady $Name $HealthUri $TimeoutSeconds
    }
    catch {
        Write-Host "Last $Name stderr lines:"
        if (Test-Path -LiteralPath $stderr) {
            Get-Content -LiteralPath $stderr -Tail 20
        }
        throw
    }
    Write-Host "[start] $Name PID=$($process.Id) logs=$stdout / $stderr"
}

function Stop-ManagedPythonProcess([string]$Name, [string]$ScriptPath, [string]$PidFile) {
    $managed = Get-ManagedProcess $PidFile $ScriptPath
    if ($null -eq $managed) {
        Write-Host "[skip] No managed $Name process is running."
        return
    }
    # On Windows, .venv\Scripts\python.exe can remain as a launcher parent while
    # the base Python child owns the listening port. Discover and validate the
    # complete descendant tree so stop never leaves that project child behind.
    $descendants = @()
    $frontier = @([int]$managed.ProcessId)
    while ($frontier.Count -gt 0) {
        $next = @()
        foreach ($parentId in $frontier) {
            $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$parentId" -ErrorAction SilentlyContinue)
            foreach ($child in $children) {
                if ($child.Name -eq "conhost.exe") {
                    # A private console host is an implementation detail of the
                    # hidden Windows process and exits with its launcher.
                    continue
                }
                if ($child.CommandLine -notlike "*$ScriptPath*") {
                    throw "Refusing to stop $Name because descendant PID $($child.ProcessId) does not match $ScriptPath."
                }
                $descendants += $child
                $next += [int]$child.ProcessId
            }
        }
        $frontier = $next
    }

    $stopIds = @($descendants | Select-Object -ExpandProperty ProcessId)
    [array]::Reverse($stopIds)
    $stopIds += [int]$managed.ProcessId
    try {
        foreach ($processId in $stopIds) {
            Stop-Process -Id $processId -ErrorAction SilentlyContinue
        }
        foreach ($processId in $stopIds) {
            Wait-Process -Id $processId -Timeout 20 -ErrorAction SilentlyContinue
        }
    }
    finally {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
    Write-Host "[stop] $Name PID tree=$($stopIds -join ',')"
}
