from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.common.models import Currency
from apps.imports.models import ImportJob, ImportSource
from apps.imports.services.integrations.binance import BinanceApiError, BinanceClient, BinanceSymbol, decimal_from_binance
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product


USD_LIKE_ASSETS = {'USD', 'USDT', 'USDC', 'FDUSD', 'BUSD', 'TUSD', 'DAI', 'RWUSD'}
FIAT_ACCOUNT_ASSETS = USD_LIKE_ASSETS | {'EUR', 'GBP', 'BYN', 'RUB', 'TRY', 'UAH', 'PLN'}
PRICE_QUOTES = ('USDT', 'USDC', 'FDUSD', 'BUSD', 'USD')
MONEY_QUANT = Decimal('0.01')
UNIT_QUANT = Decimal('0.000001')
BINANCE_DAILY_SNAPSHOT_MAX_DAYS = 30


@dataclass
class BinanceSyncResult:
	scope: str
	job_id: int | None = None
	rows_detected: int = 0
	records_created: int = 0
	records_updated: int = 0
	skipped: int = 0
	details: dict[str, Any] = field(default_factory=dict)


def _money(value: Decimal) -> Decimal:
	return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _units(value: Decimal) -> Decimal:
	return value.quantize(UNIT_QUANT, rounding=ROUND_HALF_UP)


def _timestamp_ms_to_datetime(value: Any) -> datetime:
	raw = int(value)
	return datetime.fromtimestamp(raw / 1000, tz=timezone.get_current_timezone())


def _ms_to_utc_date(value_ms: int) -> date:
	return datetime.fromtimestamp(int(value_ms) / 1000, tz=dt_timezone.utc).date()


def normalize_binance_asset(asset: str) -> str:
	asset = asset.upper().strip()
	if asset.startswith('LD') and len(asset) > 2:
		return asset[2:]
	return asset


def _ensure_currency(code: str, *, usd_rate: Decimal | None = None, name: str | None = None) -> Currency:
	code = normalize_binance_asset(code)
	default_rate = Decimal('1') if code in USD_LIKE_ASSETS else Decimal('0')
	currency, _ = Currency.objects.update_or_create(
		code=code,
		defaults={
			'name': name or code,
			'symbol': code,
			'usd_rate': usd_rate if usd_rate is not None else default_rate,
			'metadata': {'source': 'binance', 'auto_provisioned': True},
		},
	)
	return currency


def ensure_binance_reference_data() -> tuple[FinancialInstitution, ImportSource]:
	usd = _ensure_currency('USD', usd_rate=Decimal('1'), name='US Dollar')
	institution, _ = FinancialInstitution.objects.update_or_create(
		slug='binance',
		defaults={
			'name': 'Binance',
			'institution_type': FinancialInstitution.InstitutionType.CRYPTO_EXCHANGE,
			'country': 'ZZ',
			'website': 'https://www.binance.com/',
			'base_currency': usd,
			'metadata': {'integration': 'api', 'auto_provisioned': True},
		},
	)
	source, _ = ImportSource.objects.update_or_create(
		code=BinanceClient.source_code,
		defaults={
			'institution': institution,
			'name': 'Binance API',
			'source_type': ImportSource.SourceType.API,
			'is_active': True,
			'config': {'parser': 'binance-api', 'permissions': ['read-only']},
		},
	)
	return institution, source


def _ensure_account(institution: FinancialInstitution, asset: str, *, wallet: str = 'spot', update_balance: bool = False, balance: Decimal = Decimal('0')) -> Account:
	asset = normalize_binance_asset(asset)
	currency = _ensure_currency(asset)
	external_id = f'binance:{wallet}:{asset.upper()}'
	account, _ = Account.objects.update_or_create(
		institution=institution,
		external_id=external_id,
		defaults={
			'name': f'Binance {wallet.title()} {asset.upper()}',
			'account_type': Account.AccountType.WALLET,
			'currency': currency,
			'metadata': {
				'source': 'binance',
				'wallet': wallet,
				'asset': asset.upper(),
				'current_balance_source': 'api_snapshot' if update_balance else 'ledger_only',
			},
			'is_active': True,
		},
	)
	if update_balance:
		account.current_balance = _money(balance)
		account.current_balance_usd = _money(account.current_balance * (currency.usd_rate or Decimal('0')))
		account.save(update_fields=['current_balance', 'current_balance_usd', 'updated_at'])
	return account


def _ensure_product(institution: FinancialInstitution, asset: str, *, area: str = 'spot') -> Product:
	asset = normalize_binance_asset(asset)
	usd = _ensure_currency('USD', usd_rate=Decimal('1'), name='US Dollar')
	external_id = f'binance:{area}:{asset.upper()}'
	product, _ = Product.objects.update_or_create(
		institution=institution,
		external_id=external_id,
		defaults={
			'name': f'Binance {asset.upper()} {area.title()}',
			'symbol': asset.upper(),
			'product_type': Product.ProductType.CRYPTO,
			'currency': usd,
			'metadata': {'source': 'binance', 'asset': asset.upper(), 'product_area': area},
			'is_active': True,
		},
	)
	return product


def _price_index(ticker_rows: list[dict]) -> dict[str, Decimal]:
	index: dict[str, Decimal] = {}
	for row in ticker_rows:
		symbol = str(row.get('symbol', '')).upper()
		if symbol:
			index[symbol] = decimal_from_binance(row.get('price'))
	return index


def _asset_price_usd(asset: str, prices: dict[str, Decimal]) -> tuple[Decimal, str]:
	asset = normalize_binance_asset(asset)
	if asset in USD_LIKE_ASSETS:
		return Decimal('1'), asset
	for quote in PRICE_QUOTES:
		symbol = f'{asset}{quote}'
		price = prices.get(symbol)
		if price:
			return price, symbol
	return Decimal('0'), ''


def _collect_snapshot_price_symbols(balances: list[dict[str, Any]], ticker_prices: dict[str, Decimal]) -> set[str]:
	symbols: set[str] = set()
	for row in balances:
		asset = row['asset']
		if asset in FIAT_ACCOUNT_ASSETS:
			continue
		_, price_symbol = _asset_price_usd(asset, ticker_prices)
		if price_symbol:
			symbols.add(price_symbol.upper())
	return symbols


def _daily_kline_close_index(
	client: BinanceClient,
	price_symbols: set[str],
	start_time_ms: int,
	end_time_ms: int,
) -> tuple[dict[tuple[str, date], Decimal], dict[str, str]]:
	index: dict[tuple[str, date], Decimal] = {}
	kline_errors: dict[str, str] = {}
	day_ms = 24 * 60 * 60 * 1000
	padded_start = max(0, start_time_ms - day_ms)
	padded_end = end_time_ms + day_ms
	for symbol in sorted(price_symbols):
		try:
			rows = client.fetch_klines(
				symbol,
				interval='1d',
				start_time=padded_start,
				end_time=padded_end,
				limit=BINANCE_DAILY_SNAPSHOT_MAX_DAYS + 2,
			)
		except BinanceApiError as exc:
			kline_errors[symbol] = str(exc)
			continue
		if not isinstance(rows, list):
			continue
		for row in rows:
			if not row or len(row) < 5:
				continue
			day = _ms_to_utc_date(int(row[0]))
			index[(symbol.upper(), day)] = decimal_from_binance(row[4])
	return index, kline_errors


def _historical_asset_price_usd(
	asset: str,
	as_of_date: date,
	*,
	ticker_prices: dict[str, Decimal],
	daily_closes: dict[tuple[str, date], Decimal],
) -> tuple[Decimal, str]:
	asset = normalize_binance_asset(asset)
	if asset in USD_LIKE_ASSETS:
		return Decimal('1'), asset
	_, price_symbol = _asset_price_usd(asset, ticker_prices)
	if not price_symbol:
		return Decimal('0'), ''
	price = daily_closes.get((price_symbol.upper(), as_of_date), Decimal('0'))
	return price, price_symbol


def _aggregate_spot_balance_rows(balance_rows: list[dict]) -> list[dict[str, Any]]:
	asset_totals: dict[str, dict[str, Any]] = {}
	for row in balance_rows:
		free = decimal_from_binance(row.get('free'))
		locked = decimal_from_binance(row.get('locked'))
		if not free and not locked:
			continue
		raw_asset = str(row.get('asset', '')).upper()
		asset = normalize_binance_asset(raw_asset)
		if asset not in asset_totals:
			asset_totals[asset] = {'asset': asset, 'raw_assets': [], 'raw_balances': {}, 'free': Decimal('0'), 'locked': Decimal('0')}
		asset_totals[asset]['raw_assets'].append(raw_asset)
		asset_totals[asset]['raw_balances'][raw_asset] = {
			'free': str(free),
			'locked': str(locked),
			'total': str(free + locked),
		}
		asset_totals[asset]['free'] += free
		asset_totals[asset]['locked'] += locked
	return list(asset_totals.values())


def _snapshot_fingerprint(*, snapshot_type: str, area: str, asset: str, update_time_ms: int) -> str:
	return f'binance:daily:{snapshot_type}:{area}:{asset.upper()}:{update_time_ms}'


def _snapshot_exists(institution: FinancialInstitution, fingerprint: str) -> bool:
	return BalanceSnapshot.objects.filter(
		institution=institution,
		metadata__snapshot_fingerprint=fingerprint,
	).exists()


def _non_flexible_spot_units(product: Product) -> Decimal:
	metadata = product.metadata if isinstance(product.metadata, dict) else {}
	raw_balances = metadata.get('raw_balances')
	if not isinstance(raw_balances, dict):
		return product.units or Decimal('0')
	total = Decimal('0')
	for raw_asset, balance in raw_balances.items():
		if str(raw_asset).upper().startswith('LD'):
			continue
		if isinstance(balance, dict):
			total += Decimal(str(balance.get('total') or '0'))
	return total


def sync_spot_balances(client: BinanceClient | None = None, *, create_snapshots: bool = True, dry_run: bool = False) -> BinanceSyncResult:
	client = client or BinanceClient()
	payload = client.fetch_account()
	balances = _aggregate_spot_balance_rows(payload.get('balances', []))
	prices = _price_index(client.fetch_ticker_prices())
	result = BinanceSyncResult(scope='spot-balances', rows_detected=len(balances))
	if dry_run:
		result.details = {'assets': [row['asset'] for row in balances], 'raw_assets': {row['asset']: row['raw_assets'] for row in balances}}
		return result

	institution, source = ensure_binance_reference_data()
	idempotency_key = f'binance:spot-balances:{timezone.localdate().isoformat()}'
	job, _ = ImportJob.objects.get_or_create(
		source=source,
		idempotency_key=idempotency_key,
		defaults={
			'institution': institution,
			'status': ImportJob.Status.PARSING,
			'file_type': 'api',
			'parser_name': 'binance-spot-balances',
			'started_at': timezone.now(),
			'details': {'scope': 'spot-balances'},
		},
	)

	created = 0
	updated = 0
	missing_prices: list[str] = []
	seen_account_external_ids: set[str] = set()
	seen_product_external_ids: set[str] = set()
	with transaction.atomic():
		for row in balances:
			asset = row['asset']
			raw_assets = row['raw_assets']
			raw_balances = row['raw_balances']
			free = row['free']
			locked = row['locked']
			total = free + locked
			account_external_id = f'binance:spot:{asset}'
			seen_account_external_ids.add(account_external_id)
			if asset in FIAT_ACCOUNT_ASSETS:
				_ensure_account(institution, asset, wallet='spot', update_balance=True, balance=total)
				created += 1
				if create_snapshots:
					account = Account.objects.get(institution=institution, external_id=account_external_id)
					BalanceSnapshot.objects.create(
						institution=institution,
						account=account,
						currency=account.currency,
						balance=total,
						balance_usd=_money(total * (account.currency.usd_rate or Decimal('0'))),
						captured_at=timezone.now(),
						metadata={'source': 'binance', 'wallet': 'spot', 'asset': asset, 'raw_assets': raw_assets, 'raw_balances': raw_balances, 'free': str(free), 'locked': str(locked), 'import_job_id': job.pk},
					)
				continue

			_ensure_currency(asset)
			_ensure_account(institution, asset, wallet='spot', update_balance=False)
			product = _ensure_product(institution, asset, area='spot')
			seen_product_external_ids.add(product.external_id)
			price_usd, price_symbol = _asset_price_usd(asset, prices)
			if not price_usd:
				missing_prices.append(asset)
			product.units = _units(total)
			product.current_price = price_usd
			product.current_value_usd = _money(total * price_usd)
			metadata = product.metadata if isinstance(product.metadata, dict) else {}
			metadata.update({'source': 'binance', 'asset': asset, 'raw_assets': raw_assets, 'raw_balances': raw_balances, 'product_area': 'spot', 'free': str(free), 'locked': str(locked), 'price_symbol': price_symbol})
			product.metadata = metadata
			product.is_active = total != Decimal('0')
			product.save(update_fields=['units', 'current_price', 'current_value_usd', 'metadata', 'is_active', 'updated_at'])
			updated += 1
			if create_snapshots:
				BalanceSnapshot.objects.create(
					institution=institution,
					product=product,
					currency=product.currency,
					balance=product.units,
					balance_usd=product.current_value_usd,
					captured_at=timezone.now(),
					metadata={'source': 'binance', 'wallet': 'spot', 'asset': asset, 'raw_assets': raw_assets, 'raw_balances': raw_balances, 'free': str(free), 'locked': str(locked), 'price_symbol': price_symbol, 'import_job_id': job.pk},
				)

		stale_products = Product.objects.filter(
			institution=institution,
			external_id__startswith='binance:spot:',
			metadata__source='binance',
		).exclude(external_id__in=seen_product_external_ids)
		for product in stale_products:
			product.units = Decimal('0')
			product.current_value_usd = Decimal('0')
			product.is_active = False
			metadata = product.metadata if isinstance(product.metadata, dict) else {}
			metadata['stale_after_normalization'] = True
			metadata.setdefault('zero_balance_from', timezone.localdate().isoformat())
			product.metadata = metadata
			product.save(update_fields=['units', 'current_value_usd', 'is_active', 'metadata', 'updated_at'])
			if create_snapshots:
				BalanceSnapshot.objects.create(
					institution=institution,
					product=product,
					currency=product.currency,
					balance=Decimal('0'),
					balance_usd=Decimal('0'),
					captured_at=timezone.now(),
					metadata={
						'source': 'binance',
						'wallet': metadata.get('product_area', 'spot'),
						'asset': metadata.get('asset', product.symbol),
						'stale_after_normalization': True,
						'import_job_id': job.pk,
					},
				)

		stale_accounts = Account.objects.filter(
			institution=institution,
			external_id__startswith='binance:spot:',
			metadata__source='binance',
		).exclude(external_id__in=seen_account_external_ids)
		for account in stale_accounts:
			account.current_balance = Decimal('0')
			account.current_balance_usd = Decimal('0')
			account.is_active = False
			metadata = account.metadata if isinstance(account.metadata, dict) else {}
			metadata['stale_after_normalization'] = True
			metadata.setdefault('zero_balance_from', timezone.localdate().isoformat())
			account.metadata = metadata
			account.save(update_fields=['current_balance', 'current_balance_usd', 'is_active', 'metadata', 'updated_at'])
			if create_snapshots:
				BalanceSnapshot.objects.create(
					institution=institution,
					account=account,
					currency=account.currency,
					balance=Decimal('0'),
					balance_usd=Decimal('0'),
					captured_at=timezone.now(),
					metadata={
						'source': 'binance',
						'wallet': metadata.get('wallet', 'spot'),
						'asset': metadata.get('asset', ''),
						'stale_after_normalization': True,
						'import_job_id': job.pk,
					},
				)

		job.status = ImportJob.Status.SAVED
		job.rows_detected = len(balances)
		job.records_created = created + updated
		job.details = {'scope': 'spot-balances', 'missing_prices': sorted(set(missing_prices))}
		job.finished_at = timezone.now()
		job.error_message = ''
		job.save(update_fields=['status', 'rows_detected', 'records_created', 'details', 'finished_at', 'error_message', 'updated_at'])

	result.job_id = job.pk
	result.records_created = created
	result.records_updated = updated
	result.details = job.details
	return result


def _persist_spot_snapshot_balances(
	institution: FinancialInstitution,
	job: ImportJob,
	*,
	balances: list[dict[str, Any]],
	ticker_prices: dict[str, Decimal],
	daily_closes: dict[tuple[str, date], Decimal],
	captured_at: datetime,
	update_time_ms: int,
	snapshot_type: str = 'spot',
) -> tuple[int, list[str]]:
	created = 0
	missing_prices: list[str] = []
	price_date = _ms_to_utc_date(update_time_ms)
	for row in balances:
		asset = row['asset']
		raw_assets = row['raw_assets']
		raw_balances = row['raw_balances']
		free = row['free']
		locked = row['locked']
		total = free + locked
		fingerprint = _snapshot_fingerprint(snapshot_type=snapshot_type, area='spot', asset=asset, update_time_ms=update_time_ms)
		if _snapshot_exists(institution, fingerprint):
			continue

		if asset in FIAT_ACCOUNT_ASSETS:
			account = _ensure_account(institution, asset, wallet='spot', update_balance=False)
			BalanceSnapshot.objects.create(
				institution=institution,
				account=account,
				currency=account.currency,
				balance=total,
				balance_usd=_money(total),
				captured_at=captured_at,
				metadata={
					'source': 'binance',
					'snapshot_type': snapshot_type,
					'snapshot_fingerprint': fingerprint,
					'wallet': 'spot',
					'asset': asset,
					'raw_assets': raw_assets,
					'raw_balances': raw_balances,
					'free': str(free),
					'locked': str(locked),
					'price_usd': '1',
					'update_time_ms': update_time_ms,
					'import_job_id': job.pk,
				},
			)
			created += 1
			continue

		_ensure_currency(asset)
		_ensure_account(institution, asset, wallet='spot', update_balance=False)
		product = _ensure_product(institution, asset, area='spot')
		price_usd, price_symbol = _historical_asset_price_usd(
			asset,
			price_date,
			ticker_prices=ticker_prices,
			daily_closes=daily_closes,
		)
		if not price_usd:
			missing_prices.append(asset)
		BalanceSnapshot.objects.create(
			institution=institution,
			product=product,
			currency=product.currency,
			balance=_units(total),
			balance_usd=_money(total * price_usd),
			captured_at=captured_at,
			metadata={
				'source': 'binance',
				'snapshot_type': snapshot_type,
				'snapshot_fingerprint': fingerprint,
				'wallet': 'spot',
				'asset': asset,
				'raw_assets': raw_assets,
				'raw_balances': raw_balances,
				'product_area': 'spot',
				'free': str(free),
				'locked': str(locked),
				'price_symbol': price_symbol,
				'price_usd': str(price_usd),
				'price_date': price_date.isoformat(),
				'flexible_earn_included': any(str(item).upper().startswith('LD') for item in raw_assets),
				'update_time_ms': update_time_ms,
				'import_job_id': job.pk,
			},
		)
		created += 1
	return created, missing_prices


def _persist_earn_locked_snapshot_balances(
	institution: FinancialInstitution,
	job: ImportJob,
	*,
	locked_totals: dict[str, dict[str, Any]],
	prices: dict[str, Decimal],
	captured_at: datetime,
	update_time_ms: int,
) -> tuple[int, list[str]]:
	created = 0
	missing_prices: list[str] = []
	for asset, summary in locked_totals.items():
		if asset in FIAT_ACCOUNT_ASSETS:
			continue
		amount = summary['amount']
		if not amount:
			continue
		fingerprint = _snapshot_fingerprint(snapshot_type='earn_locked', area='earn_locked', asset=asset, update_time_ms=update_time_ms)
		if _snapshot_exists(institution, fingerprint):
			continue
		product = _ensure_product(institution, asset, area='earn_locked')
		price_usd, price_symbol = _asset_price_usd(asset, prices)
		if not price_usd:
			missing_prices.append(asset)
		BalanceSnapshot.objects.create(
			institution=institution,
			product=product,
			currency=product.currency,
			balance=_units(amount),
			balance_usd=_money(amount * price_usd),
			captured_at=captured_at,
			metadata={
				'source': 'binance',
				'snapshot_type': 'earn_locked',
				'snapshot_fingerprint': fingerprint,
				'asset': asset,
				'raw_assets': summary['raw_assets'],
				'product_area': 'earn_locked',
				'price_symbol': price_symbol,
				'payloads': summary['payloads'],
				'update_time_ms': update_time_ms,
				'import_job_id': job.pk,
			},
		)
		created += 1
	return created, missing_prices


def sync_daily_account_snapshots(
	client: BinanceClient | None = None,
	*,
	days: int = BINANCE_DAILY_SNAPSHOT_MAX_DAYS,
	dry_run: bool = False,
) -> BinanceSyncResult:
	client = client or BinanceClient()
	days = max(1, min(int(days), BINANCE_DAILY_SNAPSHOT_MAX_DAYS))
	end_time = int(timezone.now().timestamp() * 1000)
	start_time = end_time - days * 24 * 60 * 60 * 1000
	spot_snapshots = client.fetch_account_snapshots('SPOT', start_time=start_time, end_time=end_time, limit=days)
	result = BinanceSyncResult(scope='daily-snapshots', rows_detected=len(spot_snapshots))
	if dry_run:
		result.details = {
			'days_requested': days,
			'spot_snapshot_days': len(spot_snapshots),
			'flexible_earn_note': 'Flexible Earn appears in SPOT snapshots as LD* assets and is merged into spot products.',
			'earn_locked_note': 'Locked Simple Earn has no Binance daily history API.',
		}
		return result

	institution, source = ensure_binance_reference_data()
	ticker_prices = _price_index(client.fetch_ticker_prices())
	price_symbols: set[str] = set()
	for snapshot in spot_snapshots:
		data = snapshot.get('data') if isinstance(snapshot.get('data'), dict) else {}
		price_symbols.update(_collect_snapshot_price_symbols(_aggregate_spot_balance_rows(data.get('balances', [])), ticker_prices))
	daily_closes, kline_errors = _daily_kline_close_index(client, price_symbols, start_time, end_time)
	job, _ = ImportJob.objects.get_or_create(
		source=source,
		idempotency_key=f'binance:daily-snapshots:{timezone.localdate().isoformat()}',
		defaults={
			'institution': institution,
			'status': ImportJob.Status.PARSING,
			'file_type': 'api',
			'parser_name': 'binance-daily-snapshots',
			'started_at': timezone.now(),
			'details': {'scope': 'daily-snapshots', 'days': days},
		},
	)

	created = 0
	missing_prices: set[str] = set()
	with transaction.atomic():
		for snapshot in spot_snapshots:
			update_time_ms = int(snapshot.get('updateTime') or 0)
			if not update_time_ms:
				continue
			data = snapshot.get('data') if isinstance(snapshot.get('data'), dict) else {}
			balances = _aggregate_spot_balance_rows(data.get('balances', []))
			captured_at = _timestamp_ms_to_datetime(update_time_ms)
			day_created, day_missing = _persist_spot_snapshot_balances(
				institution,
				job,
				balances=balances,
				ticker_prices=ticker_prices,
				daily_closes=daily_closes,
				captured_at=captured_at,
				update_time_ms=update_time_ms,
			)
			created += day_created
			missing_prices.update(day_missing)

		try:
			locked = client.fetch_simple_earn_locked_positions()
		except BinanceApiError:
			locked = {}
		locked_rows = locked.get('rows', []) if isinstance(locked, dict) else []
		locked_totals: dict[str, dict[str, Any]] = {}
		for row in locked_rows:
			raw_asset = str(row.get('asset') or row.get('rewardAsset') or '').upper()
			asset = normalize_binance_asset(raw_asset)
			if not asset:
				continue
			if asset not in locked_totals:
				locked_totals[asset] = {'amount': Decimal('0'), 'raw_assets': [], 'payloads': []}
			locked_totals[asset]['amount'] += decimal_from_binance(row.get('totalAmount') or row.get('amount') or row.get('principalAmount'))
			locked_totals[asset]['raw_assets'].append(raw_asset)
			locked_totals[asset]['payloads'].append(row)
		if locked_totals:
			update_time_ms = int(timezone.now().timestamp() * 1000)
			locked_created, locked_missing = _persist_earn_locked_snapshot_balances(
				institution,
				job,
				locked_totals=locked_totals,
				prices=ticker_prices,
				captured_at=timezone.now(),
				update_time_ms=update_time_ms,
			)
			created += locked_created
			missing_prices.update(locked_missing)

		job.status = ImportJob.Status.SAVED
		job.rows_detected = len(spot_snapshots)
		job.records_created = created
		job.details = {
			'scope': 'daily-snapshots',
			'days_requested': days,
			'spot_snapshot_days': len(spot_snapshots),
			'snapshots_created': created,
			'flexible_earn_in_spot_snapshots': True,
			'earn_locked_historical': False,
			'earn_locked_current_snapshots': len(locked_totals),
			'missing_prices': sorted(missing_prices),
			'kline_errors': kline_errors,
		}
		job.finished_at = timezone.now()
		job.error_message = ''
		job.save(update_fields=['status', 'rows_detected', 'records_created', 'details', 'finished_at', 'error_message', 'updated_at'])

	result.job_id = job.pk
	result.records_created = created
	result.details = job.details
	return result


def reprice_binance_daily_snapshots(
	client: BinanceClient | None = None,
	*,
	dry_run: bool = False,
) -> BinanceSyncResult:
	client = client or BinanceClient()
	institution = FinancialInstitution.objects.get(slug='binance')
	snapshots = list(
		BalanceSnapshot.objects.filter(
			institution=institution,
			metadata__snapshot_type='spot',
		).order_by('captured_at')
	)
	result = BinanceSyncResult(scope='reprice-daily-snapshots', rows_detected=len(snapshots))
	if not snapshots:
		result.details = {'snapshots_updated': 0}
		return result

	start_ms = min(int(item.metadata.get('update_time_ms') or 0) for item in snapshots if item.metadata.get('update_time_ms'))
	end_ms = max(int(item.metadata.get('update_time_ms') or 0) for item in snapshots if item.metadata.get('update_time_ms'))
	ticker_prices = _price_index(client.fetch_ticker_prices())
	price_symbols: set[str] = set()
	for snapshot in snapshots:
		asset = (snapshot.metadata or {}).get('asset') or snapshot.currency.code
		if asset in FIAT_ACCOUNT_ASSETS or snapshot.account_id:
			continue
		_, price_symbol = _asset_price_usd(asset, ticker_prices)
		if price_symbol:
			price_symbols.add(price_symbol.upper())
	daily_closes, kline_errors = _daily_kline_close_index(client, price_symbols, start_ms, end_ms)
	result.details = {
		'price_symbols': sorted(price_symbols),
		'daily_closes_loaded': len(daily_closes),
		'kline_errors': kline_errors,
	}
	if dry_run:
		return result

	updated = 0
	missing_prices: set[str] = set()
	with transaction.atomic():
		for snapshot in snapshots:
			metadata = snapshot.metadata if isinstance(snapshot.metadata, dict) else {}
			asset = metadata.get('asset') or snapshot.currency.code
			update_time_ms = int(metadata.get('update_time_ms') or 0)
			if asset in FIAT_ACCOUNT_ASSETS or snapshot.account_id:
				new_usd = _money(snapshot.balance or Decimal('0'))
				metadata['price_usd'] = '1'
				if snapshot.balance_usd != new_usd:
					snapshot.balance_usd = new_usd
					snapshot.metadata = metadata
					snapshot.save(update_fields=['balance_usd', 'metadata', 'updated_at'])
					updated += 1
				continue
			if not update_time_ms:
				continue
			price_date = _ms_to_utc_date(update_time_ms)
			price_usd, price_symbol = _historical_asset_price_usd(
				asset,
				price_date,
				ticker_prices=ticker_prices,
				daily_closes=daily_closes,
			)
			if not price_usd:
				missing_prices.add(asset)
				continue
			new_usd = _money((snapshot.balance or Decimal('0')) * price_usd)
			metadata['price_symbol'] = price_symbol
			metadata['price_usd'] = str(price_usd)
			metadata['price_date'] = price_date.isoformat()
			if snapshot.balance_usd != new_usd:
				snapshot.balance_usd = new_usd
				snapshot.metadata = metadata
				snapshot.save(update_fields=['balance_usd', 'metadata', 'updated_at'])
				updated += 1

	result.records_updated = updated
	result.details = {
		'snapshots_seen': len(snapshots),
		'snapshots_updated': updated,
		'missing_prices': sorted(missing_prices),
	}
	return result


def _symbol_info(symbol_map: dict[str, BinanceSymbol], symbol: str) -> BinanceSymbol:
	if symbol in symbol_map:
		return symbol_map[symbol]
	raise ValueError(f'Binance symbol is not available in exchangeInfo: {symbol}')


def sync_spot_history(
	client: BinanceClient | None = None,
	*,
	symbols: list[str],
	start_time: int | None = None,
	end_time: int | None = None,
	dry_run: bool = False,
) -> BinanceSyncResult:
	client = client or BinanceClient()
	symbols = [symbol.upper() for symbol in symbols]
	symbol_map = client.fetch_symbol_map()
	trades_by_symbol = {symbol: client.fetch_my_trades(symbol, start_time=start_time, end_time=end_time) for symbol in symbols}
	rows = sum(len(rows) for rows in trades_by_symbol.values())
	result = BinanceSyncResult(scope='spot-history', rows_detected=rows)
	if dry_run:
		result.details = {'symbols': symbols}
		return result

	institution, source = ensure_binance_reference_data()
	job, created_job = ImportJob.objects.get_or_create(
		source=source,
		idempotency_key=f'binance:spot-history:{start_time or "all"}:{end_time or "now"}:{"-".join(symbols)}',
		defaults={
			'institution': institution,
			'status': ImportJob.Status.PARSING,
			'file_type': 'api',
			'parser_name': 'binance-spot-history',
			'started_at': timezone.now(),
			'details': {'scope': 'spot-history', 'symbols': symbols},
		},
	)
	if not created_job and job.status == ImportJob.Status.SAVED:
		result.job_id = job.pk
		result.skipped = rows
		result.details = {'already_imported': True, 'symbols': symbols}
		return result

	created = 0
	with transaction.atomic():
		for symbol, trades in trades_by_symbol.items():
			info = _symbol_info(symbol_map, symbol)
			product = _ensure_product(institution, info.base_asset, area='spot')
			account = _ensure_account(institution, info.quote_asset, wallet='spot', update_balance=False)
			for trade in trades:
				trade_id = str(trade.get('id'))
				is_buyer = bool(trade.get('isBuyer'))
				qty = decimal_from_binance(trade.get('qty'))
				quote_qty = decimal_from_binance(trade.get('quoteQty'))
				price = decimal_from_binance(trade.get('price'))
				quantity = qty if is_buyer else -qty
				amount = -quote_qty if is_buyer else quote_qty
				_, was_created = Transaction.objects.update_or_create(
					import_fingerprint=f'binance:trade:{symbol}:{trade_id}',
					defaults={
						'account': account,
						'product': product,
						'import_job': job,
						'transaction_type': Transaction.TransactionType.TRADE,
						'currency': account.currency,
						'external_id': trade_id,
						'amount': _money(amount),
						'amount_usd': _money(amount * (account.currency.usd_rate or Decimal('0'))),
						'quantity': _units(quantity),
						'unit_price': price,
						'occurred_at': _timestamp_ms_to_datetime(trade.get('time')),
						'description': f'Binance {symbol} {"buy" if is_buyer else "sell"}',
						'metadata': {'source': 'binance', 'symbol': symbol, 'payload': trade, 'exclude_from_account_balance': True},
					},
				)
				if was_created:
					created += 1
				commission = decimal_from_binance(trade.get('commission'))
				if commission:
					commission_asset = str(trade.get('commissionAsset', '')).upper()
					fee_account = _ensure_account(institution, commission_asset, wallet='spot', update_balance=False)
					fee_product = None if commission_asset in FIAT_ACCOUNT_ASSETS else _ensure_product(institution, commission_asset, area='spot')
					_, fee_created = Transaction.objects.update_or_create(
						import_fingerprint=f'binance:trade-fee:{symbol}:{trade_id}',
						defaults={
							'account': fee_account,
							'product': fee_product,
							'import_job': job,
							'transaction_type': Transaction.TransactionType.FEE,
							'currency': fee_account.currency,
							'external_id': f'{trade_id}:fee',
							'amount': _money(-commission if commission_asset in FIAT_ACCOUNT_ASSETS else Decimal('0')),
							'amount_usd': Decimal('0'),
							'quantity': _units(-commission),
							'unit_price': Decimal('0'),
							'occurred_at': _timestamp_ms_to_datetime(trade.get('time')),
							'description': f'Binance {symbol} fee',
							'metadata': {'source': 'binance', 'symbol': symbol, 'payload': trade, 'exclude_from_account_balance': True},
						},
					)
					if fee_created:
						created += 1

		job.status = ImportJob.Status.SAVED
		job.rows_detected = rows
		job.records_created = created
		job.details = {'scope': 'spot-history', 'symbols': symbols}
		job.finished_at = timezone.now()
		job.error_message = ''
		job.save(update_fields=['status', 'rows_detected', 'records_created', 'details', 'finished_at', 'error_message', 'updated_at'])

	result.job_id = job.pk
	result.records_created = created
	result.details = job.details
	return result


def sync_deposits_withdrawals(
	client: BinanceClient | None = None,
	*,
	start_time: int | None = None,
	end_time: int | None = None,
	dry_run: bool = False,
) -> BinanceSyncResult:
	client = client or BinanceClient()
	deposits = client.fetch_deposits(start_time=start_time, end_time=end_time)
	withdrawals = client.fetch_withdrawals(start_time=start_time, end_time=end_time)
	rows = len(deposits) + len(withdrawals)
	result = BinanceSyncResult(scope='transfers', rows_detected=rows)
	if dry_run:
		result.details = {'deposits': len(deposits), 'withdrawals': len(withdrawals)}
		return result

	institution, source = ensure_binance_reference_data()
	job, created_job = ImportJob.objects.get_or_create(
		source=source,
		idempotency_key=f'binance:transfers:{start_time or "all"}:{end_time or "now"}',
		defaults={
			'institution': institution,
			'status': ImportJob.Status.PARSING,
			'file_type': 'api',
			'parser_name': 'binance-transfers',
			'started_at': timezone.now(),
			'details': {'scope': 'transfers'},
		},
	)
	if not created_job and job.status == ImportJob.Status.SAVED:
		result.job_id = job.pk
		result.skipped = rows
		result.details = {'already_imported': True}
		return result

	created = 0
	with transaction.atomic():
		for tx_type, rows_payload in [('deposit', deposits), ('withdrawal', withdrawals)]:
			for row in rows_payload:
				asset = str(row.get('coin') or row.get('asset') or '').upper()
				amount = decimal_from_binance(row.get('amount'))
				account = _ensure_account(institution, asset, wallet='spot', update_balance=False)
				external_id = str(row.get('txId') or row.get('id') or row.get('applyTime') or row.get('insertTime'))
				occurred_raw = row.get('insertTime') or row.get('applyTime') or row.get('successTime')
				occurred_at = _timestamp_ms_to_datetime(occurred_raw) if str(occurred_raw).isdigit() else timezone.now()
				signed_amount = amount if tx_type == 'deposit' else -amount
				_, was_created = Transaction.objects.update_or_create(
					import_fingerprint=f'binance:{tx_type}:{asset}:{external_id}',
					defaults={
						'account': account,
						'import_job': job,
						'transaction_type': Transaction.TransactionType.DEPOSIT if tx_type == 'deposit' else Transaction.TransactionType.WITHDRAWAL,
						'currency': account.currency,
						'external_id': external_id,
						'amount': _money(signed_amount if asset in FIAT_ACCOUNT_ASSETS else Decimal('0')),
						'amount_usd': Decimal('0'),
						'quantity': _units(signed_amount),
						'unit_price': Decimal('0'),
						'occurred_at': occurred_at,
						'description': f'Binance {asset} {tx_type}',
						'metadata': {'source': 'binance', 'payload': row, 'exclude_from_account_balance': True},
					},
				)
				if was_created:
					created += 1

		job.status = ImportJob.Status.SAVED
		job.rows_detected = rows
		job.records_created = created
		job.details = {'scope': 'transfers', 'deposits': len(deposits), 'withdrawals': len(withdrawals)}
		job.finished_at = timezone.now()
		job.error_message = ''
		job.save(update_fields=['status', 'rows_detected', 'records_created', 'details', 'finished_at', 'error_message', 'updated_at'])

	result.job_id = job.pk
	result.records_created = created
	result.details = job.details
	return result


def sync_earn_and_funding(client: BinanceClient | None = None, *, dry_run: bool = False) -> BinanceSyncResult:
	client = client or BinanceClient()
	errors: dict[str, str] = {}
	try:
		funding_assets = client.fetch_funding_assets()
	except BinanceApiError as exc:
		funding_assets = []
		errors['funding_assets'] = str(exc)
	try:
		flexible = client.fetch_simple_earn_flexible_positions()
	except BinanceApiError as exc:
		flexible = {}
		errors['flexible_positions'] = str(exc)
	try:
		locked = client.fetch_simple_earn_locked_positions()
	except BinanceApiError as exc:
		locked = {}
		errors['locked_positions'] = str(exc)
	flexible_rows = flexible.get('rows', []) if isinstance(flexible, dict) else []
	locked_rows = locked.get('rows', []) if isinstance(locked, dict) else []
	flexible_totals: dict[str, dict[str, Any]] = {}
	for row in flexible_rows:
		raw_asset = str(row.get('asset') or row.get('rewardAsset') or '').upper()
		asset = normalize_binance_asset(raw_asset)
		if not asset:
			continue
		if asset not in flexible_totals:
			flexible_totals[asset] = {'amount': Decimal('0'), 'raw_assets': [], 'payloads': []}
		flexible_totals[asset]['amount'] += decimal_from_binance(row.get('totalAmount') or row.get('amount') or row.get('principalAmount'))
		flexible_totals[asset]['raw_assets'].append(raw_asset)
		flexible_totals[asset]['payloads'].append(row)
	locked_totals: dict[str, dict[str, Any]] = {}
	for row in locked_rows:
		raw_asset = str(row.get('asset') or row.get('rewardAsset') or '').upper()
		asset = normalize_binance_asset(raw_asset)
		if not asset:
			continue
		if asset not in locked_totals:
			locked_totals[asset] = {'amount': Decimal('0'), 'raw_assets': [], 'payloads': []}
		locked_totals[asset]['amount'] += decimal_from_binance(row.get('totalAmount') or row.get('amount') or row.get('principalAmount'))
		locked_totals[asset]['raw_assets'].append(raw_asset)
		locked_totals[asset]['payloads'].append(row)
	rows = len(funding_assets) + len(flexible_rows) + len(locked_rows)
	result = BinanceSyncResult(scope='earn-funding', rows_detected=rows)
	if dry_run:
		result.details = {'funding_assets': len(funding_assets), 'flexible_positions': len(flexible_rows), 'locked_positions': len(locked_rows), 'errors': errors}
		return result

	institution, source = ensure_binance_reference_data()
	prices = _price_index(client.fetch_ticker_prices())
	job, _ = ImportJob.objects.get_or_create(
		source=source,
		idempotency_key=f'binance:earn-funding:{timezone.localdate().isoformat()}',
		defaults={
			'institution': institution,
			'status': ImportJob.Status.PARSING,
			'file_type': 'api',
			'parser_name': 'binance-earn-funding',
			'started_at': timezone.now(),
			'details': {'scope': 'earn-funding'},
		},
	)

	updated = 0
	seen_funding_account_external_ids: set[str] = set()
	seen_funding_product_external_ids: set[str] = set()
	seen_locked_product_external_ids: set[str] = set()
	locked_fetch_ok = 'locked_positions' not in errors
	funding_fetch_ok = 'funding_assets' not in errors
	with transaction.atomic():
		for row in funding_assets:
			raw_asset = str(row.get('asset', '')).upper()
			asset = normalize_binance_asset(raw_asset)
			free = decimal_from_binance(row.get('free'))
			locked_amount = decimal_from_binance(row.get('locked'))
			freeze = decimal_from_binance(row.get('freeze'))
			total = free + locked_amount + freeze
			seen_funding_account_external_ids.add(f'binance:funding:{asset}')
			_ensure_account(institution, asset, wallet='funding', update_balance=asset in FIAT_ACCOUNT_ASSETS, balance=total)
			if asset not in FIAT_ACCOUNT_ASSETS:
				product = _ensure_product(institution, asset, area='funding')
				seen_funding_product_external_ids.add(product.external_id)
				price_usd, price_symbol = _asset_price_usd(asset, prices)
				product.units = _units(total)
				product.current_price = price_usd
				product.current_value_usd = _money(total * price_usd)
				product.is_active = total != Decimal('0')
				product.metadata = {'source': 'binance', 'asset': asset, 'raw_asset': raw_asset, 'product_area': 'funding', 'price_symbol': price_symbol, 'payload': row}
				product.save(update_fields=['units', 'current_price', 'current_value_usd', 'is_active', 'metadata', 'updated_at'])
			updated += 1

		for asset, summary in flexible_totals.items():
			if asset in FIAT_ACCOUNT_ASSETS:
				continue
			product = _ensure_product(institution, asset, area='spot')
			price_usd, price_symbol = _asset_price_usd(asset, prices)
			current_units = product.units or Decimal('0')
			amount = max(current_units, summary['amount'])
			product.units = _units(amount)
			product.current_price = price_usd
			product.current_value_usd = _money(amount * price_usd)
			product.is_active = amount != Decimal('0')
			metadata = product.metadata if isinstance(product.metadata, dict) else {}
			metadata.update({
				'source': 'binance',
				'asset': asset,
				'product_area': 'spot',
				'flexible_earn_amount': str(summary['amount']),
				'flexible_earn_raw_assets': summary['raw_assets'],
				'flexible_earn_payloads': summary['payloads'],
				'flexible_earn_strategy': 'replace_if_greater',
				'price_symbol': price_symbol,
			})
			product.metadata = metadata
			product.save(update_fields=['units', 'current_price', 'current_value_usd', 'is_active', 'metadata', 'updated_at'])
			updated += 1

		for asset, summary in locked_totals.items():
			if asset in FIAT_ACCOUNT_ASSETS:
				continue
			product = _ensure_product(institution, asset, area='earn_locked')
			seen_locked_product_external_ids.add(product.external_id)
			price_usd, price_symbol = _asset_price_usd(asset, prices)
			amount = summary['amount']
			product.units = _units(amount)
			product.current_price = price_usd
			product.current_value_usd = _money(amount * price_usd)
			product.is_active = amount != Decimal('0')
			metadata = product.metadata if isinstance(product.metadata, dict) else {}
			metadata.update({
				'source': 'binance',
				'asset': asset,
				'raw_assets': summary['raw_assets'],
				'product_area': 'earn_locked',
				'price_symbol': price_symbol,
				'payloads': summary['payloads'],
			})
			product.metadata = metadata
			product.save(update_fields=['units', 'current_price', 'current_value_usd', 'is_active', 'metadata', 'updated_at'])
			updated += 1

		if funding_fetch_ok:
			stale_funding_products = Product.objects.filter(
				institution=institution,
				external_id__startswith='binance:funding:',
				metadata__source='binance',
			).exclude(external_id__in=seen_funding_product_external_ids)
			for product in stale_funding_products:
				product.units = Decimal('0')
				product.current_value_usd = Decimal('0')
				product.is_active = False
				metadata = product.metadata if isinstance(product.metadata, dict) else {}
				metadata['stale_after_normalization'] = True
				metadata.setdefault('zero_balance_from', timezone.localdate().isoformat())
				product.metadata = metadata
				product.save(update_fields=['units', 'current_value_usd', 'is_active', 'metadata', 'updated_at'])
				updated += 1

			stale_funding_accounts = Account.objects.filter(
				institution=institution,
				external_id__startswith='binance:funding:',
				metadata__source='binance',
			).exclude(external_id__in=seen_funding_account_external_ids)
			for account in stale_funding_accounts:
				account.current_balance = Decimal('0')
				account.current_balance_usd = Decimal('0')
				account.is_active = False
				metadata = account.metadata if isinstance(account.metadata, dict) else {}
				metadata['stale_after_normalization'] = True
				metadata.setdefault('zero_balance_from', timezone.localdate().isoformat())
				account.metadata = metadata
				account.save(update_fields=['current_balance', 'current_balance_usd', 'is_active', 'metadata', 'updated_at'])
				updated += 1

		if locked_fetch_ok:
			stale_locked_products = Product.objects.filter(
				institution=institution,
				external_id__startswith='binance:earn_locked:',
				metadata__source='binance',
			).exclude(external_id__in=seen_locked_product_external_ids)
			for product in stale_locked_products:
				product.units = Decimal('0')
				product.current_value_usd = Decimal('0')
				product.is_active = False
				metadata = product.metadata if isinstance(product.metadata, dict) else {}
				metadata['stale_after_normalization'] = True
				metadata.setdefault('zero_balance_from', timezone.localdate().isoformat())
				product.metadata = metadata
				product.save(update_fields=['units', 'current_value_usd', 'is_active', 'metadata', 'updated_at'])
				updated += 1

		job.status = ImportJob.Status.SAVED
		job.rows_detected = rows
		job.records_created = updated
		job.details = {
			'scope': 'earn-funding',
			'funding_assets': len(funding_assets),
			'flexible_positions_seen': len(flexible_rows),
			'flexible_assets_updated': len(flexible_totals),
			'locked_positions': len(locked_rows),
			'locked_assets_updated': len(locked_totals),
			'errors': errors,
		}
		job.finished_at = timezone.now()
		job.error_message = ''
		job.save(update_fields=['status', 'rows_detected', 'records_created', 'details', 'finished_at', 'error_message', 'updated_at'])

	result.job_id = job.pk
	result.records_updated = updated
	result.details = job.details
	return result
