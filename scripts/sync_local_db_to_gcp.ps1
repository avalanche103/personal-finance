$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$logPath = Join-Path $projectRoot 'data\cache\gcp_daily_sync.log'

Set-Location $projectRoot

$venvPython = Join-Path $projectRoot 'venv\Scripts\python.exe'
$dotVenvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) {
    $venvPython
} elseif (Test-Path $dotVenvPython) {
    $dotVenvPython
} else {
    'python'
}

Start-Transcript -Path $logPath -Append
try {
    Write-Host "Starting local database sync to GCP at $(Get-Date -Format o)"

    & $python scripts\export_cloud_fixture.py
    if ($LASTEXITCODE -ne 0) {
        throw "Fixture export failed with exit code $LASTEXITCODE."
    }

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy\gcp\bootstrap-data.ps1
    if ($LASTEXITCODE -ne 0) {
        throw "GCP bootstrap failed with exit code $LASTEXITCODE."
    }

    Write-Host "Local database sync to GCP completed at $(Get-Date -Format o)"
} finally {
    Stop-Transcript
}
