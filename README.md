# Personal Finance Dashboard MVP

Локальный MVP на Django для учета личных финансов и инвестиций. Проект использует SQLite, Django templates, HTMX и skeleton-слой для импорта из XLS/XLSX, PDF, API и ручного ввода.

## Что уже есть

- базовая структура Django-проекта под локальную разработку;
- доменная модель для институтов, счетов, продуктов, валют, транзакций, импортов и снапшотов;
- Django admin для основных сущностей;
- дашборд и списки на Django templates + HTMX partials;
- skeleton import pipeline с `ImportJob`, `RawImportFile`, XLS/PDF parser stubs и API adapter base;
- конфигурация через `.env` с SQLite по умолчанию и возможностью перейти на PostgreSQL через переменные окружения.

## Структура проекта

```text
personal-finance/
├─ apps/
│  ├─ accounts/
│  ├─ common/
│  ├─ core/
│  ├─ dashboard/
│  ├─ imports/
│  ├─ institutions/
│  └─ products/
├─ config/
├─ data/
│  ├─ processed/
│  ├─ raw/
│  └─ samples/
├─ docs/
├─ media/
│  └─ imports/
├─ scripts/
├─ static/
├─ templates/
├─ .env
├─ manage.py
└─ requirements.txt
```

## Локальный запуск

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py bootstrap_local_data
python manage.py createsuperuser
python manage.py sync_nbrb_rates --start-date 2024-01-01
python manage.py runserver
```

Быстрый запуск через bat:

```bat
run_local.bat
```

После запуска:

- дашборд: `http://127.0.0.1:8000/`
- admin: `http://127.0.0.1:8000/admin/`

## Базовый поток работы

1. Выполните `python manage.py bootstrap_local_data`, чтобы создать базовые валюты, источники и demo-записи.
2. Добавьте счета и продукты вручную или через import pipeline.
3. Загружайте XLS/XLSX или PDF на странице `/imports/upload/`.
4. Проверяйте историю импортов на странице `/imports/history/`.

## Bootstrap command

- Команда локальной инициализации:

```bash
python manage.py bootstrap_local_data
```

- Она создает `USD`, `EUR`, `RUB`, `BYN`, два demo-института, базовые import sources, demo accounts, products, transactions и snapshots.
- Команда идемпотентна и подходит для повторного запуска на локальной SQLite-базе.

## Условия токенов Finstore (ставка, срок, выплаты)

Поля продукта: `annual_rate_pct`, `maturity_date`, `income_schedule`, `next_income_date`.

Импорт справочника из CSV/JSON:

```bash
python manage.py import_finstore_token_terms --file data/samples/finstore_token_terms.example.csv
```

Синхронизация через файл или переменные окружения `FINSTORE_TERMS_FILE` / `FINSTORE_TERMS_URL`:

```bash
python manage.py sync_finstore_token_terms --file path/to/terms.csv
python manage.py sync_finstore_token_terms --recompute-dates-only
```

Синхронизация ставок и дат погашения с [castle.by/calendar](https://castle.by/calendar/) (страницы `/bond/<id>/`):

```bash
python manage.py sync_castle_token_terms --dry-run
python manage.py sync_castle_token_terms --platform finstore
python manage.py sync_castle_token_terms --delay 1.5
```

По умолчанию запрашиваются только страницы `/bond/<id>/` для токенов, которые уже есть в базе (один запрос к календарю + N страниц выпусков).

После импорта истории Finstore `next_income_date` можно оценить по операциям `Получение дохода`, если задан `income_schedule`.

## Импорт

- XLS/XLSX: `pandas` + `openpyxl`, сейчас реализован preview skeleton.
- PDF: отдельный parser layer на `pypdf`, сейчас сохраняет excerpt и требует ручной проверки.
- API: предусмотрен слой `apps/imports/services/integrations/` для отдельных клиентов по каждому источнику.
- Идемпотентность: `ImportJob` использует `idempotency_key`, основанный на источнике, checksum и имени файла.

## Курсы валют НБ РБ

- История курсов хранится в модели `ExchangeRateHistory`.
- Реализован клиент официального API НБ РБ через `https://api.nbrb.by/exrates/`.
- Поддержан backfill для `USD`, `EUR` и `RUB` с сохранением официального курса в BYN и кросс-курса к USD.
- Команда загрузки:

```bash
python manage.py sync_nbrb_rates --start-date 2024-01-01
```

- Команда создает или обновляет `ImportSource` с кодом `nbrb-exrates-api`, пишет `ImportJob` в историю импортов и сохраняет актуальные `Cur_ID` в `Currency.metadata`.
- В текущей базе уже загружена история `USD`, `EUR`, `RUB` за период с `2024-01-01` по текущую дату.
- Дашборд и страница курсов читают данные только из локальной базы; запросы к API НБ РБ выполняются по расписанию (каждые 8 часов) через `scripts/sync_daily_finance.ps1`, как и синхронизация Binance.

## Пересчет USD полей

- Команда ручного пересчета:

```bash
python manage.py recalculate_usd_values
```

- Пересчитываются поля `Account.current_balance_usd`, `Transaction.amount_usd`, `BalanceSnapshot.balance_usd` и `Product.current_value_usd`.
- При синхронизации НБ РБ пересчет вызывается автоматически после сохранения новых курсов.

## Binance

Интеграция Binance использует только read-only API key из `.env`; ключ с правом withdrawal не нужен и не должен использоваться:

```env
BINANCE_API_KEY=your_read_only_key
BINANCE_API_SECRET=your_read_only_secret
BINANCE_API_BASE_URL=https://api.binance.com
```

Базовый запуск синхронизирует текущие Spot-балансы и USD-оценку:

```bash
python manage.py sync_binance --spot --snapshots
```

История Spot-сделок импортируется по явному списку пар, чтобы не обходить весь рынок:

```bash
python manage.py sync_binance --history --symbols BTCUSDT,ETHUSDT --start-date 2026-01-01
python manage.py sync_binance --transfers --start-date 2026-01-01
python manage.py sync_binance --earn --funding
python manage.py sync_binance --spot --dry-run
python manage.py sync_binance --spot --snapshots --skip-missing-credentials
```

Spot snapshot является источником текущих балансов, а импортированные сделки/комиссии помечаются как `exclude_from_account_balance`, чтобы не удваивать портфель в dashboard.

## Страница истории курсов

- UI страница доступна по адресу `/exchange-rates/`.
- На странице есть график и таблица по `USD`, `EUR`, `RUB` с фильтром периода.

## Исторический отчет портфеля

- Страница отчета доступна по адресу `/portfolio-report/`.
- Можно выбрать дату и получить портфельную оценку в USD, разбивку по институтам, аккаунтам и продуктам.
- Если исторических `BalanceSnapshot` еще нет, отчет использует текущие балансы и текущие продуктовые позиции как fallback.

## Dashboard FX block

- На главной странице добавлен блок последних курсов `USD`, `EUR`, `RUB` (данные из локальной базы).
- Для каждой валюты показывается последний курс НБ РБ в BYN и изменение к предыдущему дню.

## Регулярное обновление локально

- PowerShell script для регулярной синхронизации: `scripts/sync_daily_finance.ps1`.
- CMD-обертка для Планировщика Windows: `scripts/sync_daily_finance.cmd`.
- Скрипт регистрации задачи: `scripts/register_daily_finance_task.ps1`.
- Запуск каждые 8 часов подтягивает последние 7 дней курсов, обновляет Binance Spot/Earn/Funding и затем делает пересчет USD-полей.
- Те же синхронизации можно запустить вручную на странице `/imports/upload/` кнопками **Sync NBRB rates** и **Sync Binance**.
- Пример регистрации в Планировщике Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\register_daily_finance_task.ps1
```

- Пример удаления задачи:

```powershell
schtasks /Delete /TN "PersonalFinanceDailySync" /F
```

## Документация

- Короткие архитектурные заметки лежат в `docs/architecture.md`.

## Архитектурные заметки

- `FinancialInstitution` является верхним уровнем владения.
- `Account` привязан к институту.
- `Product` привязан к институту, а не к счету.
- `Transaction` хранит валюту операции и USD-представление.
- `BalanceSnapshot` сохраняет историческое состояние на дату.
- По умолчанию база локальная SQLite, но настройки уже разделены под переход на PostgreSQL/GCP.

## Следующий этап

- нормализация входных файлов в отдельный слой mapping rules;
- сохранение нормализованных транзакций и снапшотов из parser results;
- курсы валют и автоматический пересчет в USD;
- API adapters для конкретных банков, брокеров и бирж;
- расширение UI до более детальной аналитики.