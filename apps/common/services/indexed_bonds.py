from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from apps.accounts.models import Transaction
from apps.common.models import ExchangeRateHistory
from apps.common.dates import format_display_date
from apps.common.services.aigenis_bonds import get_alfabank_byn_account
from apps.common.services.exchange_rates import get_usd_conversion_rate
from apps.products.models import Product
from apps.products.services.token_terms import _add_months, schedule_month_delta

OP47_ISIN = 'BCSE-00477-P01'

OP47_TERMS = {
	'annual_rate_pct': Decimal('7.0000'),
	'maturity_date': date(2029, 11, 6),
	'income_schedule': Product.IncomeSchedule.QUARTERLY,
	'next_income_date': date(2026, 7, 8),
}

OP47_PROSPECTUS_PAYMENTS = {
	'2026-04-08': '2.3209',
	'2026-07-08': '3.0609',
	'2026-10-08': '3.0945',
	'2027-01-08': '3.0945',
	'2027-04-08': '3.0273',
	'2027-07-08': '3.0609',
	'2027-10-08': '3.0945',
	'2028-01-08': '3.0938',
	'2028-04-08': '3.0525',
	'2028-07-08': '3.0525',
	'2028-10-08': '3.0861',
	'2029-01-08': '3.0868',
	'2029-04-08': '3.0273',
	'2029-07-08': '3.0609',
	'2029-11-06': '4.0700',
}

OP47_METADATA = {
	'face_value_byn': '500',
	'face_value_usd': '175.3894',
	'placement_fx_rate': '2.8508',
	'income_calendar': {
		'enabled': True,
		'coupon_day': 8,
		'schedule_start_date': '2026-04-08',
		'payments': OP47_PROSPECTUS_PAYMENTS,
	},
}

OP47_LAST_COUPON = {
	'payment_date': date(2026, 4, 8),
	'coupon_usd_per_unit': Decimal('2.3209'),
	'coupon_byn_per_unit': Decimal('6.7754'),
}

OP51_ISIN = 'BCSE-00487-P02'

OP51_TERMS = {
	'annual_rate_pct': Decimal('7.0000'),
	'maturity_date': date(2031, 12, 15),
	'income_schedule': Product.IncomeSchedule.QUARTERLY,
	'next_income_date': date(2026, 8, 16),
}

OP51_PROSPECTUS_PAYMENTS = {
	'2026-08-16': '2.3391',
	'2026-11-16': '1.8877',
	'2027-02-16': '1.8877',
	'2027-05-16': '1.8262',
	'2027-08-16': '1.8877',
	'2027-11-16': '1.8877',
	'2028-02-16': '1.8851',
	'2028-05-16': '1.8416',
	'2028-08-16': '1.8826',
	'2028-11-16': '1.8826',
	'2029-02-16': '1.8852',
	'2029-05-16': '1.8262',
	'2029-08-16': '1.8877',
	'2029-11-16': '1.8877',
	'2030-02-16': '1.8877',
	'2030-05-16': '1.8262',
	'2030-08-16': '1.8877',
	'2030-11-16': '1.8877',
	'2031-02-16': '1.8877',
	'2031-05-16': '1.8262',
	'2031-08-16': '1.8877',
	'2031-12-15': '2.4828',
}

OP51_METADATA = {
	'face_value_byn': '300',
	'face_value_usd': '106.9900',
	'placement_fx_rate': '2.8040',
	'income_calendar': {
		'enabled': True,
		'coupon_day': 16,
		'schedule_start_date': '2026-08-16',
		'payments': OP51_PROSPECTUS_PAYMENTS,
	},
}


def is_indexed_bond(product: Product) -> bool:
	return (
		product.product_type == Product.ProductType.BOND
		and isinstance(product.metadata, dict)
		and product.metadata.get('bond_kind') == 'indexed'
		and product.metadata.get('face_value_usd')
	)


def latest_usd_byn_rate(*, on_date: date | None = None) -> Decimal | None:
	reference = on_date or timezone.localdate()
	row = ExchangeRateHistory.objects.filter(
		currency__code='USD',
		rate_date__lte=reference,
		source=ExchangeRateHistory.Source.NBRB,
	).order_by('-rate_date').first()
	if row and row.rate_byn:
		return row.rate_byn
	return None


def refresh_indexed_bond_valuation(product: Product, *, save: bool = True) -> bool:
	if not is_indexed_bond(product):
		return False

	metadata = product.metadata or {}
	face_value_usd = Decimal(str(metadata.get('face_value_usd')))
	units = product.units or Decimal('0')
	usd_byn_rate = latest_usd_byn_rate()
	if usd_byn_rate is None:
		placement_rate = Decimal(str(metadata.get('placement_fx_rate', '1')))
		usd_byn_rate = placement_rate

	current_price = (face_value_usd * usd_byn_rate).quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)
	current_value_usd = (units * face_value_usd).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
	product.current_price = current_price
	product.current_value_usd = current_value_usd
	if save:
		product.save(update_fields=['current_price', 'current_value_usd', 'updated_at'])
	return True


def refresh_indexed_bond_valuations(products: list[Product] | None = None) -> int:
	queryset = Product.objects.filter(product_type=Product.ProductType.BOND)
	if products is not None:
		queryset = queryset.filter(pk__in=[product.pk for product in products])
	updated = 0
	for product in queryset.select_related('currency'):
		if refresh_indexed_bond_valuation(product):
			updated += 1
	return updated


def get_income_calendar_config(product: Product) -> dict:
	if not isinstance(product.metadata, dict):
		return {}
	config = product.metadata.get('income_calendar')
	return config if isinstance(config, dict) else {}


def _parse_schedule_date(value) -> date | None:
	if value in (None, ''):
		return None
	if isinstance(value, date):
		return value
	try:
		return date.fromisoformat(str(value)[:10])
	except ValueError:
		return None


def _parse_decimal_value(value) -> Decimal | None:
	if value in (None, ''):
		return None
	try:
		return Decimal(str(value))
	except Exception:
		return None


def get_payment_schedule(product: Product) -> dict[str, str]:
	config = get_income_calendar_config(product)
	payments = config.get('payments')
	if not isinstance(payments, dict):
		return {}
	return {str(key): str(value) for key, value in payments.items() if value not in (None, '')}


def _date_on_day(year: int, month: int, day: int) -> date:
	return date(year, month, min(day, monthrange(year, month)[1]))


def resolve_schedule_start_date(product: Product) -> date | None:
	config = get_income_calendar_config(product)
	start_date = _parse_schedule_date(config.get('schedule_start_date'))
	if start_date is not None:
		return start_date
	if product.next_income_date is not None:
		return product.next_income_date
	payment_dates = get_payment_schedule(product)
	if payment_dates:
		return min(_parse_schedule_date(key) for key in payment_dates if _parse_schedule_date(key))
	return None


def generate_coupon_payment_dates(product: Product) -> list[date]:
	config = get_income_calendar_config(product)
	maturity = product.maturity_date
	start_date = resolve_schedule_start_date(product)
	if start_date is None or maturity is None:
		return []

	coupon_day = int(config.get('coupon_day') or (start_date.day if start_date else 8))
	month_delta = schedule_month_delta(product.income_schedule or Product.IncomeSchedule.QUARTERLY) or 3
	dates: list[date] = []
	candidate = _date_on_day(start_date.year, start_date.month, coupon_day)
	if candidate < start_date:
		candidate = _add_months(candidate, month_delta)
		candidate = _date_on_day(candidate.year, candidate.month, coupon_day)
	while candidate <= maturity:
		dates.append(candidate)
		candidate = _add_months(candidate, month_delta)
		candidate = _date_on_day(candidate.year, candidate.month, coupon_day)

	if dates and maturity not in dates:
		next_regular = _add_months(dates[-1], month_delta)
		next_regular = _date_on_day(next_regular.year, next_regular.month, coupon_day)
		if next_regular > maturity:
			dates.pop()
			dates.append(maturity)
	elif dates and dates[-1] != maturity and _add_months(dates[-1], month_delta) > maturity:
		dates.pop()
		dates.append(maturity)

	return dates


def planned_coupon_usd_per_unit(product: Product, payment_date: date | None = None) -> Decimal | None:
	schedule = get_payment_schedule(product)
	if payment_date is not None:
		return _parse_decimal_value(schedule.get(payment_date.isoformat()))

	reference = timezone.localdate()
	for payment_day in generate_coupon_payment_dates(product):
		if payment_day >= reference:
			amount = _parse_decimal_value(schedule.get(payment_day.isoformat()))
			if amount is not None:
				return amount

	legacy = get_income_calendar_config(product).get('planned_coupon_usd_per_unit')
	return _parse_decimal_value(legacy)


def coupon_totals_for_date(
	product: Product,
	payment_date: date,
	*,
	units: Decimal | None = None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
	per_unit_usd = planned_coupon_usd_per_unit(product, payment_date)
	if per_unit_usd is None:
		return None, None, None

	held_units = units if units is not None else units_held_on_date(product, payment_date)
	if held_units <= 0:
		held_units = product.units or Decimal('0')
	if held_units <= 0:
		return per_unit_usd, None, None

	total_usd = (per_unit_usd * held_units).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
	usd_byn_rate = latest_usd_byn_rate(on_date=payment_date) or _parse_decimal_value((product.metadata or {}).get('placement_fx_rate'))
	total_byn = None
	if usd_byn_rate:
		total_byn = (total_usd * usd_byn_rate).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
	return per_unit_usd, total_usd, total_byn


def build_income_calendar_rows(product: Product, *, today: date | None = None) -> list[dict]:
	config = get_income_calendar_config(product)
	if not config.get('enabled'):
		return []

	reference = today or timezone.localdate()
	schedule = get_payment_schedule(product)
	rows: list[dict] = []
	maturity = product.maturity_date
	for payment_date in generate_coupon_payment_dates(product):
		per_unit_usd = _parse_decimal_value(schedule.get(payment_date.isoformat()))
		held_units = units_held_on_date(product, payment_date)
		_, total_usd, total_byn = coupon_totals_for_date(product, payment_date, units=held_units)
		is_maturity_coupon = maturity is not None and payment_date == maturity
		rows.append(
			{
				'date': payment_date,
				'date_iso': payment_date.isoformat(),
				'days_until': (payment_date - reference).days,
				'is_past': payment_date < reference,
				'is_forecast': payment_date >= reference,
				'is_maturity_coupon': is_maturity_coupon,
				'coupon_usd_per_unit': per_unit_usd,
				'amount_usd': total_usd,
				'amount_byn': total_byn,
				'units': held_units if held_units > 0 else product.units,
			}
		)
	return rows


def build_product_income_calendar(product: Product, *, today: date | None = None, future_days: int | None = None) -> list[dict]:
	rows = build_income_calendar_rows(product, today=today)
	if future_days is None:
		return rows
	reference = today or timezone.localdate()
	window_end = reference + timedelta(days=future_days)
	return [row for row in rows if row['date'] <= window_end]


def save_income_calendar_config(
	product: Product,
	*,
	enabled: bool,
	coupon_day: int | None = None,
	schedule_start_date: date | None = None,
	payment_amounts: dict[str, str] | None = None,
) -> None:
	metadata = dict(product.metadata or {})
	config = dict(get_income_calendar_config(product))
	config['enabled'] = enabled
	if coupon_day is not None:
		config['coupon_day'] = coupon_day
	if schedule_start_date is not None:
		config['schedule_start_date'] = schedule_start_date.isoformat()
	payments = dict(get_payment_schedule(product))
	if payment_amounts is not None:
		for payment_date, amount in payment_amounts.items():
			normalized_amount = (amount or '').strip()
			if normalized_amount:
				payments[payment_date] = normalized_amount
			else:
				payments.pop(payment_date, None)
	config['payments'] = payments
	config.pop('planned_coupon_usd_per_unit', None)
	metadata['income_calendar'] = config
	product.metadata = metadata
	product.save(update_fields=['metadata', 'updated_at'])


def units_held_on_date(product: Product, on_date: date) -> Decimal:
	end_of_day = timezone.make_aware(datetime.combine(on_date, datetime.max.time()))
	units = Decimal('0')
	for transaction in (
		Transaction.objects.filter(product=product, occurred_at__lte=end_of_day)
		.order_by('occurred_at', 'id')
	):
		quantity = transaction.quantity or Decimal('0')
		if quantity != 0:
			units += quantity
	return max(units, Decimal('0'))


def ensure_op47_coupon_history(product: Product) -> Transaction | None:
	income_account = product.income_account or get_alfabank_byn_account()
	if income_account is None:
		return None

	coupon = OP47_LAST_COUPON
	units = units_held_on_date(product, coupon['payment_date'])
	if units <= 0:
		return None

	total_byn = (coupon['coupon_byn_per_unit'] * units).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
	total_usd = (coupon['coupon_usd_per_unit'] * units).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
	occurred_at = timezone.make_aware(datetime.combine(coupon['payment_date'], datetime.min.time()))

	transaction, _ = Transaction.objects.update_or_create(
		import_fingerprint='aigenis:op47:coupon:2026-04-08',
		defaults={
			'account': income_account,
			'product': product,
			'transaction_type': Transaction.TransactionType.INCOME,
			'currency': income_account.currency,
			'amount': total_byn,
			'amount_usd': total_usd,
			'quantity': Decimal('0'),
			'unit_price': Decimal('0'),
			'occurred_at': occurred_at,
			'description': (
				f'Выплата купона: {product.name} ({format_display_date(coupon["payment_date"])}, '
				f'{units.normalize()} шт.)'
			),
			'metadata': {
				'imported_from': 'manual-coupon-history',
				'operation_type': 'Выплата купона',
				'exclude_from_account_balance': True,
				'coupon_usd_per_unit': str(coupon['coupon_usd_per_unit']),
				'coupon_byn_per_unit': str(coupon['coupon_byn_per_unit']),
				'payment_date': coupon['payment_date'].isoformat(),
				'units_at_payment': str(units.quantize(Decimal('1'))),
			},
		},
	)
	return transaction


def merge_op47_metadata(metadata: dict) -> dict:
	merged = dict(metadata or {})
	for key, value in OP47_METADATA.items():
		if key == 'income_calendar':
			continue
		merged[key] = value

	existing_calendar = merged.get('income_calendar')
	if not isinstance(existing_calendar, dict):
		existing_calendar = {}
	default_calendar = dict(OP47_METADATA['income_calendar'])
	existing_payments = existing_calendar.get('payments')
	if not isinstance(existing_payments, dict):
		existing_payments = {}
	merged['income_calendar'] = {
		**default_calendar,
		**existing_calendar,
		'payments': {
			**OP47_PROSPECTUS_PAYMENTS,
			**existing_payments,
		},
	}
	merged['bond_kind'] = 'indexed'
	return merged


def merge_op51_metadata(metadata: dict) -> dict:
	merged = dict(metadata or {})
	for key, value in OP51_METADATA.items():
		if key == 'income_calendar':
			continue
		merged[key] = value

	existing_calendar = merged.get('income_calendar')
	if not isinstance(existing_calendar, dict):
		existing_calendar = {}
	default_calendar = dict(OP51_METADATA['income_calendar'])
	existing_payments = existing_calendar.get('payments')
	if not isinstance(existing_payments, dict):
		existing_payments = {}
	merged['income_calendar'] = {
		**default_calendar,
		**existing_calendar,
		'payments': {
			**OP51_PROSPECTUS_PAYMENTS,
			**existing_payments,
		},
	}
	merged['bond_kind'] = 'indexed'
	return merged


def configure_op51_bond(product: Product, *, preserve_user_payments: bool = True) -> bool:
	product_key = product.external_id or product.isin
	if product_key != OP51_ISIN:
		return False

	metadata = dict(product.metadata or {})
	if preserve_user_payments:
		metadata = merge_op51_metadata(metadata)
	else:
		metadata.update(OP51_METADATA)
		metadata['bond_kind'] = 'indexed'

	product.annual_rate_pct = OP51_TERMS['annual_rate_pct']
	product.maturity_date = OP51_TERMS['maturity_date']
	product.income_schedule = OP51_TERMS['income_schedule']
	product.next_income_date = OP51_TERMS['next_income_date']
	product.metadata = metadata
	product.terms_updated_at = timezone.now()
	product.save(
		update_fields=[
			'annual_rate_pct',
			'maturity_date',
			'income_schedule',
			'next_income_date',
			'metadata',
			'terms_updated_at',
			'updated_at',
		]
	)
	refresh_indexed_bond_valuation(product)
	return True


def configure_aigenis_indexed_bond(product: Product, *, preserve_user_payments: bool = True) -> bool:
	product_key = product.external_id or product.isin
	if product_key == OP47_ISIN:
		return configure_op47_bond(product, preserve_user_payments=preserve_user_payments)
	if product_key == OP51_ISIN:
		return configure_op51_bond(product, preserve_user_payments=preserve_user_payments)
	return False


def configure_op47_bond(product: Product, *, preserve_user_payments: bool = True) -> bool:
	product_key = product.external_id or product.isin
	if product_key != OP47_ISIN:
		return False

	metadata = dict(product.metadata or {})
	if preserve_user_payments:
		metadata = merge_op47_metadata(metadata)
	else:
		metadata.update(OP47_METADATA)
		metadata['bond_kind'] = 'indexed'

	product.annual_rate_pct = OP47_TERMS['annual_rate_pct']
	product.maturity_date = OP47_TERMS['maturity_date']
	product.income_schedule = OP47_TERMS['income_schedule']
	product.next_income_date = OP47_TERMS['next_income_date']
	product.metadata = metadata
	product.terms_updated_at = timezone.now()
	product.save(
		update_fields=[
			'annual_rate_pct',
			'maturity_date',
			'income_schedule',
			'next_income_date',
			'metadata',
			'terms_updated_at',
			'updated_at',
		]
	)
	refresh_indexed_bond_valuation(product)
	ensure_op47_coupon_history(product)
	return True
