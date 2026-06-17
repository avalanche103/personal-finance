# GCP deployment (Cloud Run + Cloud SQL)

Deploy the Django app to **Google Cloud Run** with **Cloud SQL PostgreSQL**.

Local SQLite and ephemeral Cloud Run disk are not suitable for production; use PostgreSQL and plan object storage (GCS) later for uploaded import files.

## Prerequisites

- [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) (`gcloud`) authenticated
- Billing enabled on the GCP project
- Docker is **not** required locally — images are built with Cloud Build

## Quick start

1. Copy env template:

```powershell
Copy-Item deploy\gcp\env.example deploy\gcp\.env
```

2. Edit `deploy/gcp/.env` — at minimum confirm `GCP_PROJECT_ID` and `GCP_REGION`.

3. One-time infrastructure setup (APIs, Artifact Registry, Cloud SQL, secrets):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\gcp\setup.ps1
```

4. Build and deploy:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\gcp\deploy.ps1
```

5. Load local data and admin user:

```powershell
python scripts/export_cloud_fixture.py
powershell -ExecutionPolicy Bypass -File deploy\gcp\bootstrap-data.ps1
```

The fixture includes user `admin` with the same password hash as your local SQLite database.

## Environment variables (Cloud Run)

| Variable | Purpose |
|----------|---------|
| `DJANGO_SECRET_KEY` | From Secret Manager |
| `DJANGO_DEBUG` | `false` in production |
| `DJANGO_ALLOWED_HOSTS` | Cloud Run hostname |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://<service-url>` |
| `DJANGO_USE_WHITENOISE` | `true` — static files |
| `DATABASE_ENGINE` | `django.db.backends.postgresql` |
| `DATABASE_NAME` / `USER` / `PASSWORD` | Cloud SQL credentials |
| `DATABASE_HOST` | `/cloudsql/PROJECT:REGION:INSTANCE` |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Optional, via secrets |

## Load local SQLite data into Cloud SQL

```powershell
python scripts/export_cloud_fixture.py
powershell -ExecutionPolicy Bypass -File deploy\gcp\bootstrap-data.ps1
```

`scripts/export_cloud_fixture.py` sanitizes invalid UTF-8 from Windows-1251 text fields and exports finance data plus import job metadata (without raw uploaded files).

To refresh cloud data after local changes, re-run both commands above.

## Scheduled sync (NBRB / Binance)

On GCP replace `scripts/sync_daily_finance.ps1` with **Cloud Scheduler** → HTTP call to a protected admin endpoint, or a **Cloud Run Job** on a cron schedule.

## Costs (approximate)

- Cloud SQL `db-f1-micro` — low but not free
- Cloud Run — pay per request; scales to zero when idle
- Cloud Build — small charge per build

## Troubleshooting

- **502 on startup** — check Cloud Run logs: `gcloud run services logs read personal-finance --region REGION`
- **Database connection** — ensure Cloud SQL Admin API is enabled and `--add-cloudsql-instances` matches `DATABASE_HOST`
- **CSRF errors** — add the exact `https://….run.app` URL to `DJANGO_CSRF_TRUSTED_ORIGINS`
- **Uploads disappear after restart** — Cloud Run disk is ephemeral; use Google Cloud Storage for `media/` and `data/raw/` in a follow-up step
