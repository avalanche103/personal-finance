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

function Test-GcloudCommand {
    param([string[]]$Args)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    & gcloud @Args 2>$null | Out-Null
    $ok = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $previous
    return $ok
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

Write-Host "Project: $Project  Region: $Region"

gcloud config set project $Project | Out-Null

$apis = @(
    'run.googleapis.com',
    'sqladmin.googleapis.com',
    'artifactregistry.googleapis.com',
    'cloudbuild.googleapis.com',
    'secretmanager.googleapis.com',
    'compute.googleapis.com'
)
Write-Host 'Enabling APIs...'
foreach ($api in $apis) {
    Write-Host "  $api"
    gcloud services enable $api --quiet
}

Write-Host 'Creating Artifact Registry repository...'
if (-not (Test-GcloudCommand @('artifacts', 'repositories', 'describe', $ArRepo, "--location=$Region"))) {
    gcloud artifacts repositories create $ArRepo `
        --repository-format=docker `
        --location=$Region `
        --description='Personal Finance Docker images'
}

Write-Host 'Creating Cloud SQL instance (may take several minutes)...'
if (-not (Test-GcloudCommand @('sql', 'instances', 'describe', $SqlInstance))) {
    gcloud sql instances create $SqlInstance `
        --database-version=POSTGRES_15 `
        --tier=db-f1-micro `
        --region=$Region `
        --storage-type=SSD `
        --storage-size=10GB `
        --backup `
        --quiet
}

Write-Host 'Creating database and user...'
if (-not (Test-GcloudCommand @('sql', 'databases', 'describe', $DbName, "--instance=$SqlInstance"))) {
    gcloud sql databases create $DbName --instance=$SqlInstance --quiet
}

$previous = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'
$existingUsers = @(gcloud sql users list --instance=$SqlInstance --format='value(name)' 2>$null)
$ErrorActionPreference = $previous
$userExists = $existingUsers -contains $DbUser
if (-not $userExists) {
    $DbPassword = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })
    gcloud sql users create $DbUser --instance=$SqlInstance --password=$DbPassword --quiet
    Write-Host "Created DB user '$DbUser'. Password stored in Secret Manager."
} else {
    Write-Host "DB user '$DbUser' already exists - rotating password into Secret Manager."
    $DbPassword = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object { [char]$_ })
    gcloud sql users set-password $DbUser --instance=$SqlInstance --password=$DbPassword --quiet
}

$pwFile = Join-Path $env:TEMP "personal-finance-db-password.txt"
Set-Content -Path $pwFile -Value $DbPassword -NoNewline -Encoding ascii
if (Test-GcloudCommand @('secrets', 'describe', 'db-password')) {
    gcloud secrets versions add db-password --data-file=$pwFile
} else {
    gcloud secrets create db-password --data-file=$pwFile
}
Remove-Item $pwFile -Force

$DjangoSecret = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 50 | ForEach-Object { [char]$_ })
$djangoFile = Join-Path $env:TEMP "personal-finance-django-secret.txt"
Set-Content -Path $djangoFile -Value $DjangoSecret -NoNewline -Encoding ascii
if (Test-GcloudCommand @('secrets', 'describe', 'django-secret-key')) {
    Write-Host 'django-secret-key already exists - keeping current value.'
} else {
    gcloud secrets create django-secret-key --data-file=$djangoFile
    Write-Host 'Created django-secret-key secret.'
}
Remove-Item $djangoFile -Force

$ProjectNumber = (gcloud projects describe $Project --format='value(projectNumber)').Trim()
$RunServiceAccount = "$ProjectNumber-compute@developer.gserviceaccount.com"
Write-Host "Granting Cloud Run SA access to secrets and Cloud SQL ($RunServiceAccount)..."
foreach ($role in @('roles/secretmanager.secretAccessor', 'roles/cloudsql.client')) {
    gcloud projects add-iam-policy-binding $Project `
        --member="serviceAccount:$RunServiceAccount" `
        --role=$role `
        --quiet | Out-Null
    Write-Host "  granted $role"
}

Write-Host ''
Write-Host 'Setup complete.'
Write-Host "Cloud SQL connection: $ConnectionName"
Write-Host 'Next: powershell -ExecutionPolicy Bypass -File deploy\gcp\deploy.ps1'
