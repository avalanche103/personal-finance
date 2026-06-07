$ErrorActionPreference = 'Stop'

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptRoot

Set-Location $projectRoot

$venvPython = Join-Path $projectRoot 'venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }

$startDate = (Get-Date).AddDays(-7).ToString('yyyy-MM-dd')

& $python manage.py sync_nbrb_rates --start-date $startDate
& $python manage.py sync_binance --spot --snapshots --skip-missing-credentials
& $python manage.py sync_binance --earn --funding --skip-missing-credentials
& $python manage.py sync_binance --daily-snapshots --snapshot-days 30 --skip-missing-credentials
& $python manage.py recalculate_usd_values