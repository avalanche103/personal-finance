from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.common.models import Currency, ExchangeRateHistory
from apps.imports.models import ImportJob, ImportSource
from apps.imports.services.integrations.nbrb import NBRBExchangeRatesClient
from apps.products.models import Product


TRACKED_CURRENCIES = ('USD', 'EUR', 'RUB')
BYN_CODE = 'BYN'


def _parse_rate_date(raw_date: str) -> date:
	return datetime.fromisoformat(raw_date.replace('Z', '+00:00')).date()


def _upsert_currency(code: str, name: str, usd_rate: Decimal, metadata: dict, is_base: bool = False) -> Currency:
	currency, _ = Currency.objects.update_or_create(
		code=code,
		defaults={
			'name': name,
			'usd_rate': usd_rate,
			'is_base': is_base,
			'metadata': metadata,
		},
	)
	return currency


def get_usd_conversion_rate(currency: Currency, target_date: date, rate_cache: dict | None = None) -> Decimal:
	rate_cache = rate_cache if rate_cache is not None else {}
	cache_key = (currency.code, target_date.isoformat())
	if cache_key in rate_cache:
		return rate_cache[cache_key]

	if currency.code == 'USD':
		rate_cache[cache_key] = Decimal('1')
		return rate_cache[cache_key]

	if currency.code == BYN_CODE:
		usd_row = ExchangeRateHistory.objects.filter(
			currency__code='USD',
			rate_date__lte=target_date,
			source=ExchangeRateHistory.Source.NBRB,
		).order_by('-rate_date').first()
		if usd_row and usd_row.rate_byn:
			rate_cache[cache_key] = Decimal('1') / usd_row.rate_byn
			return rate_cache[cache_key]

	history_row = ExchangeRateHistory.objects.filter(
		currency=currency,
		rate_date__lte=target_date,
		source=ExchangeRateHistory.Source.NBRB,
	).order_by('-rate_date').first()
	if history_row:
		rate_cache[cache_key] = history_row.usd_cross_rate
		return rate_cache[cache_key]

	default_rate = currency.usd_rate if currency.usd_rate else Decimal('0')
	rate_cache[cache_key] = default_rate
	return default_rate


def recalculate_usd_valuations() -> dict:
	today = timezone.localdate()
	rate_cache: dict[tuple[str, str], Decimal] = {}
	updated = {
		'accounts': 0,
		'transactions': 0,
		'balance_snapshots': 0,
		'products': 0,
	}

	for account in Account.objects.select_related('currency').all():
		rate = get_usd_conversion_rate(account.currency, today, rate_cache)
		account.current_balance_usd = (account.current_balance or Decimal('0')) * rate
		account.save(update_fields=['current_balance_usd', 'updated_at'])
		updated['accounts'] += 1

	for transaction in Transaction.objects.select_related('currency').all():
		rate = get_usd_conversion_rate(transaction.currency, transaction.occurred_at.date(), rate_cache)
		transaction.amount_usd = (transaction.amount or Decimal('0')) * rate
		transaction.save(update_fields=['amount_usd', 'updated_at'])
		updated['transactions'] += 1

	for snapshot in BalanceSnapshot.objects.select_related('currency').all():
		rate = get_usd_conversion_rate(snapshot.currency, snapshot.captured_at.date(), rate_cache)
		snapshot.balance_usd = (snapshot.balance or Decimal('0')) * rate
		snapshot.save(update_fields=['balance_usd', 'updated_at'])
		updated['balance_snapshots'] += 1

	for product in Product.objects.select_related('currency').all():
		rate = get_usd_conversion_rate(product.currency, today, rate_cache)
		product.current_value_usd = (product.units or Decimal('0')) * (product.current_price or Decimal('0')) * rate
		product.save(update_fields=['current_value_usd', 'updated_at'])
		updated['products'] += 1

	return updated


def sync_nbrb_rate_history(start_date: date, end_date: date | None = None) -> dict:
	end_date = end_date or timezone.localdate()
	client = NBRBExchangeRatesClient()
	descriptors = client.fetch_currencies([*TRACKED_CURRENCIES])

	source, _ = ImportSource.objects.get_or_create(
		code=client.source_code,
		defaults={
			'name': 'NBRB Exchange Rates API',
			'source_type': ImportSource.SourceType.API,
			'config': {'tracked_currencies': list(TRACKED_CURRENCIES)},
		},
	)

	idempotency_key = f'nbrb-rates:{start_date.isoformat()}:{end_date.isoformat()}:{"-".join(TRACKED_CURRENCIES)}'
	job, created = ImportJob.objects.get_or_create(
		source=source,
		idempotency_key=idempotency_key,
		defaults={
			'status': ImportJob.Status.PARSING,
			'file_type': 'api',
			'parser_name': 'nbrb-exrates-api',
			'original_filename': '',
			'started_at': timezone.now(),
			'details': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
		},
	)

	if not created and job.status == ImportJob.Status.SAVED:
		return {
			'job_id': job.pk,
			'created': False,
			'records_created': 0,
			'rows_detected': job.rows_detected,
			'stored_total': job.records_created,
		}

	by_currency_rows = {
		code: client.fetch_rate_dynamics_in_chunks(descriptor.cur_id, start_date, end_date)
		for code, descriptor in descriptors.items()
	}

	usd_descriptor = descriptors['USD']
	usd_rows = by_currency_rows['USD']
	usd_by_date = {
		_parse_rate_date(row['Date']): client.official_rate_per_unit(row, usd_descriptor.scale)
		for row in usd_rows
	}

	records_created = 0
	rows_detected = sum(len(rows) for rows in by_currency_rows.values())

	with transaction.atomic():
		_upsert_currency(BYN_CODE, 'Belarusian Ruble', Decimal('0'), {'source': 'manual'}, is_base=False)
		_upsert_currency('USD', 'US Dollar', Decimal('1'), {'nbrb_cur_id': usd_descriptor.cur_id, 'scale': usd_descriptor.scale})

		for code, descriptor in descriptors.items():
			latest_usd_rate = Decimal('1') if code == 'USD' else Decimal('0')
			currency = _upsert_currency(
				code,
				descriptor.name,
				latest_usd_rate,
				{'nbrb_cur_id': descriptor.cur_id, 'scale': descriptor.scale, 'date_start': descriptor.date_start, 'date_end': descriptor.date_end},
			)

			for row in by_currency_rows[code]:
				rate_date = _parse_rate_date(row['Date'])
				rate_byn_per_unit = client.official_rate_per_unit(row, descriptor.scale)
				usd_byn_per_unit = usd_by_date.get(rate_date)
				if usd_byn_per_unit is None:
					continue

				usd_cross_rate = Decimal('1') if code == 'USD' else rate_byn_per_unit / usd_byn_per_unit
				_, was_created = ExchangeRateHistory.objects.update_or_create(
					currency=currency,
					rate_date=rate_date,
					source=ExchangeRateHistory.Source.NBRB,
					defaults={
						'rate_byn': rate_byn_per_unit,
						'usd_cross_rate': usd_cross_rate,
						'scale': descriptor.scale,
						'source_currency_id': descriptor.cur_id,
						'payload': row,
					},
				)
				if was_created:
					records_created += 1
				latest_usd_rate = usd_cross_rate

			currency.usd_rate = latest_usd_rate
			currency.save(update_fields=['usd_rate', 'updated_at'])

		job.status = ImportJob.Status.SAVED
		job.rows_detected = rows_detected
		job.records_created = ExchangeRateHistory.objects.filter(
			currency__code__in=TRACKED_CURRENCIES,
			rate_date__gte=start_date,
			rate_date__lte=end_date,
			source=ExchangeRateHistory.Source.NBRB,
		).count()
		job.details = {
			'start_date': start_date.isoformat(),
			'end_date': end_date.isoformat(),
			'tracked_currencies': list(TRACKED_CURRENCIES),
			'nbrb_currency_ids': {code: descriptor.cur_id for code, descriptor in descriptors.items()},
		}
		job.finished_at = timezone.now()
		job.error_message = ''
		job.save(update_fields=['status', 'rows_detected', 'records_created', 'details', 'finished_at', 'error_message', 'updated_at'])

		recalculate_usd_valuations()

	return {
		'job_id': job.pk,
		'created': created,
		'records_created': records_created,
		'rows_detected': rows_detected,
		'stored_total': job.records_created,
	}