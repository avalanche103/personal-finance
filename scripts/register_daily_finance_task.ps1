$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = 'PersonalFinanceDailySync'
$taskRunner = Join-Path $scriptDir 'sync_daily_finance.cmd'

schtasks /Create /SC DAILY /TN $taskName /TR $taskRunner /ST 08:00 /F
Write-Host "Task '$taskName' created."