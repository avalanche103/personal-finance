from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from apps.accounts.models import Transaction
from apps.products.models import Product

FINSTORE_INSTITUTION_SLUG = 'finstore'


def is_finstore_token(product: Product) -> bool:
	return (
		product.product_type == Product.ProductType.TOKEN
		and getattr(getattr(product, 'institution', None), 'slug', '') == FINSTORE_INSTITUTION_SLUG
	)


def days_in_year(year: int) -> int:
	if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0):
		return 366
	return 365


def first_holding_date(product: Product) -> date | None:
	for tx in Transaction.objects.filter(product=product).order_by('occurred_at', 'id'):
		if (tx.quantity or Decimal('0')) > 0:
			return timezone.localdate(tx.occurred_at)
	return None


def finstore_accrual_period_for_payment(
	payment_date: date,
	*,
	first_holding_date: date | None,
) -> tuple[date, date] | None:
	"""Calendar month before payment_date; trimmed to first day of ownership."""
	if payment_date.month == 1:
		year, month = payment_date.year - 1, 12
	else:
		year, month = payment_date.year, payment_date.month - 1

	period_start = date(year, month, 1)
	period_end = date(year, month, monthrange(year, month)[1])

	if first_holding_date is not None:
		if first_holding_date > period_end:
			return None
		if first_holding_date > period_start:
			period_start = first_holding_date

	return period_start, period_end


def finstore_nominal_per_unit(product: Product) -> Decimal:
	return product.current_price or Decimal('0')


def _units_for_accrual_day(product: Product, day: date, *, projection_as_of: date) -> Decimal:
	from apps.common.services.indexed_bonds import units_held_on_date

	if day <= projection_as_of:
		return units_held_on_date(product, day)
	return units_held_on_date(product, projection_as_of)


def calculate_finstore_income_for_period(
	product: Product,
	period_start: date,
	period_end: date,
	*,
	projection_as_of: date | None = None,
) -> Decimal:
	"""WhitePaper formula: sum(units_day * nominal * rate / days_in_year)."""
	if product.annual_rate_pct is None or product.annual_rate_pct <= 0:
		return Decimal('0')

	nominal = finstore_nominal_per_unit(product)
	if nominal <= 0:
		return Decimal('0')

	reference = projection_as_of or timezone.localdate()
	rate = product.annual_rate_pct / Decimal('100')
	total = Decimal('0')
	day = period_start
	while day <= period_end:
		units = _units_for_accrual_day(product, day, projection_as_of=reference)
		if units > 0:
			total += units * nominal * rate / Decimal(days_in_year(day.year))
		day += timedelta(days=1)

	return total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _native_to_usd(product: Product, amount_native: Decimal) -> Decimal | None:
	if amount_native <= 0:
		return None
	if getattr(product, 'currency', None) is not None and product.currency.code == 'USD':
		return amount_native
	if product.current_value_usd and product.units and product.units > 0 and product.market_value > 0:
		return (amount_native * product.current_value_usd / product.market_value).quantize(
			Decimal('0.01'),
			rounding=ROUND_HALF_UP,
		)
	if getattr(product, 'currency', None) is not None and product.currency.usd_rate:
		return (amount_native * product.currency.usd_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
	return None


def estimate_finstore_income_amount(
	product: Product,
	payment_date: date,
	*,
	today: date | None = None,
) -> tuple[Decimal | None, Decimal | None]:
	"""Forecast payout on payment_date for the previous calendar month."""
	reference = today or timezone.localdate()
	first_hold = first_holding_date(product)
	period = finstore_accrual_period_for_payment(payment_date, first_holding_date=first_hold)
	if period is None:
		return None, None

	amount_native = calculate_finstore_income_for_period(
		product,
		period[0],
		period[1],
		projection_as_of=reference,
	)
	if amount_native <= 0:
		return None, None

	return amount_native, _native_to_usd(product, amount_native)
