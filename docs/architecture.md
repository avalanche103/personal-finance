# Architecture Notes

## Current shape

- `apps/common` holds shared models, exchange-rate history, local bootstrap data and management commands.
- `apps/institutions`, `apps/accounts`, `apps/products` keep the domain split aligned with future PostgreSQL migration.
- `apps/imports` separates upload storage, parser stubs and API integrations.
- `apps/dashboard` stays read-oriented: HTMX fragments, dashboard pages and reporting views.

## MVP conventions

- `FinancialInstitution` is the top-level owner for both accounts and products.
- `Product` belongs to an institution, not to an account.
- USD values are denormalized into model fields for fast dashboard reads.
- `ExchangeRateHistory` is the source of truth for FX history; `Currency.usd_rate` is only the latest cached rate.
- Import jobs must remain idempotent through `idempotency_key` and raw-file checksum handling.

## Local startup flow

1. `python manage.py migrate`
2. `python manage.py bootstrap_local_data`
3. `python manage.py sync_nbrb_rates --start-date 2024-01-01`
4. `python manage.py runserver`

## Next scaffold-level targets

- Replace placeholder parser outputs with normalized transaction mapping.
- Add fixtures or factory helpers for broader test coverage.
- Move more valuation logic behind explicit service functions as reporting grows.