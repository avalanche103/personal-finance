param(
    [string]$EnvFile = "$PSScriptRoot\.env"
)

$ErrorActionPreference = 'Stop'

function Load-EnvFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        throw "Missing $Path - copy env.example to .env and edit values."
    }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#') -or $line -notmatch '=') { return }
        $name, $value = $line.Split('=', 2)
        Set-Item -Path "Env:$($name.Trim())" -Value $value.Trim()
    }
}

Load-EnvFile $EnvFile

$Project = $env:GCP_PROJECT_ID
$Region = $env:GCP_REGION
$Service = $env:GCP_SERVICE
$SqlInstance = $env:GCP_SQL_INSTANCE
$ArRepo = $env:GCP_AR_REPO
$DbName = $env:GCP_DB_NAME
$DbUser = $env:GCP_DB_USER
$ConnectionName = "$Project`:$Region`:$SqlInstance"
$Image = "$Region-docker.pkg.dev/$Project/$ArRepo/app"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path

gcloud config set project $Project | Out-Null

Write-Host "Building image: $Image"
Push-Location $RepoRoot
try {
    gcloud builds submit --tag "$Image`:latest" .
} finally {
    Pop-Location
}

$CloudSqlHost = "/cloudsql/$ConnectionName"
$BaseEnv = @(
    "DJANGO_DEBUG=false",
    "DJANGO_USE_WHITENOISE=true",
    "DATABASE_ENGINE=django.db.backends.postgresql",
    "DATABASE_NAME=$DbName",
    "DATABASE_USER=$DbUser",
    "DATABASE_HOST=$CloudSqlHost",
    "TIME_ZONE=Europe/Minsk",
    "DJANGO_ALLOWED_HOSTS=.run.app"
) -join ','

Write-Host 'Deploying to Cloud Run...'
gcloud run deploy $Service `
    --image "$Image`:latest" `
    --region $Region `
    --platform managed `
    --allow-unauthenticated `
    --add-cloudsql-instances $ConnectionName `
    --set-env-vars $BaseEnv `
    --set-secrets "DATABASE_PASSWORD=db-password:latest,DJANGO_SECRET_KEY=django-secret-key:latest" `
    --memory 512Mi `
    --cpu 1 `
    --min-instances 0 `
    --max-instances 2 `
    --timeout 300 `
    --quiet

$ServiceUrl = gcloud run services describe $Service --region $Region --format='value(status.url)' 2>$null
if (-not $ServiceUrl) {
    Write-Warning 'Service URL not available yet - check Cloud Run logs and redeploy.'
    exit 1
}

$HostOnly = ([uri]$ServiceUrl).Host
$EnvUpdates = "^@^DJANGO_CSRF_TRUSTED_ORIGINS=$ServiceUrl^@^DJANGO_ALLOWED_HOSTS=$HostOnly,.run.app"

Write-Host "Service URL: $ServiceUrl"
Write-Host 'Updating CSRF and ALLOWED_HOSTS...'
gcloud run services update $Service `
    --region $Region `
    --update-env-vars $EnvUpdates `
    --quiet

Write-Host ''
Write-Host 'Deploy complete.'
Write-Host "Open: $ServiceUrl"
Write-Host ''
Write-Host 'Create admin user (one-off) via Cloud Console or a Cloud Run Job with manage.py createsuperuser.'
