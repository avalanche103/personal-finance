param(
    [string]$EnvFile = "$PSScriptRoot\.env",
    [switch]$SkipBuild
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
$SqlInstance = $env:GCP_SQL_INSTANCE
$ArRepo = $env:GCP_AR_REPO
$DbName = $env:GCP_DB_NAME
$DbUser = $env:GCP_DB_USER
$ConnectionName = "$Project`:$Region`:$SqlInstance"
$Image = "$Region-docker.pkg.dev/$Project/$ArRepo/app"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$Fixture = Join-Path $RepoRoot 'data\cloud_fixture.json'
$JobName = 'personal-finance-loaddata'

if (-not (Test-Path $Fixture)) {
    throw "Missing $Fixture - run: python scripts/export_cloud_fixture.py"
}

gcloud config set project $Project | Out-Null

if (-not $SkipBuild) {
    Write-Host "Building image with fixture: $Image"
    Push-Location $RepoRoot
    try {
        gcloud builds submit --tag "$Image`:latest" .
    } finally {
        Pop-Location
    }
}

$CloudSqlHost = "/cloudsql/$ConnectionName"
$JobEnv = @(
    "DJANGO_DEBUG=false",
    "DATABASE_ENGINE=django.db.backends.postgresql",
    "DATABASE_NAME=$DbName",
    "DATABASE_USER=$DbUser",
    "DATABASE_HOST=$CloudSqlHost",
    "TIME_ZONE=Europe/Minsk"
) -join ','

Write-Host "Creating/updating Cloud Run job: $JobName"
$previous = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
gcloud run jobs describe $JobName --region $Region 2>$null | Out-Null
$jobExists = $LASTEXITCODE -eq 0
$ErrorActionPreference = $previous

if ($jobExists) {
    gcloud run jobs update $JobName `
        --image "$Image`:latest" `
        --region $Region `
        --set-cloudsql-instances $ConnectionName `
        --set-env-vars $JobEnv `
        --set-secrets "DATABASE_PASSWORD=db-password:latest,DJANGO_SECRET_KEY=django-secret-key:latest" `
        --command deploy/gcp/job-loaddata.sh `
        --task-timeout 900 `
        --max-retries 0 `
        --memory 1Gi `
        --cpu 1 `
        --quiet
} else {
    gcloud run jobs create $JobName `
        --image "$Image`:latest" `
        --region $Region `
        --set-cloudsql-instances $ConnectionName `
        --set-env-vars $JobEnv `
        --set-secrets "DATABASE_PASSWORD=db-password:latest,DJANGO_SECRET_KEY=django-secret-key:latest" `
        --command deploy/gcp/job-loaddata.sh `
        --task-timeout 900 `
        --max-retries 0 `
        --memory 1Gi `
        --cpu 1 `
        --quiet
}

Write-Host 'Running loaddata job...'
gcloud run jobs execute $JobName --region $Region --wait

Write-Host ''
Write-Host 'Done. Admin user "admin" should match your local password from the fixture.'
