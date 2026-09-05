from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from apps.accounts.models import Transaction
from apps.common.dates import following_weekday
from apps.products.models import Product


ALFABANK_PAYOUT_INTERVAL_DAYS = 15
ALFABANK_DAY_COUNT_BASIS = 365


def _parse_opened_at(product: Product) -> date | None:
	metadata = product.metadata if isinstance(product.metadata, dict) else {}
	raw_value = str(metadata.get('opened_at', '') or '').strip()
	if not raw_value:
		return None
	for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
		try:
			from datetime import datetime

			return datetime.strptime(raw_value, fmt).date()
		except ValueError:
			continue
	return None


def deposit_income_anchor_days(opened_at: date) -> tuple[int, int]:
	day1 = opened_at.day
	day2 = min(day1 + 15, monthrange(opened_at.year, opened_at.month)[1])
	return day1, day2


def deposit_income_dates_in_month(year: int, month: int, *, day1: int, day2: int) -> list[date]:
	last_day = monthrange(year, month)[1]
	return sorted({date(year, month, min(day, last_day)) for day in (day1, day2)})


def _monthly_anchor_day(product: Product) -> int | None:
	opened_at = _parse_opened_at(product)
	if opened_at is not None:
		return opened_at.day
	if product.next_income_date is not None:
		return product.next_income_date.day
	return None


def _date_on_anchor_day(year: int, month: int, anchor_day: int) -> date:
	return date(year, month, min(anchor_day, monthrange(year, month)[1]))


def _metadata_int(product: Product, key: str, default: int = 0) -> int:
	metadata = product.metadata if isinstance(product.metadata, dict) else {}
	try:
		return int(metadata.get(key, default) or default)
	except (TypeError, ValueError):
		return default


def _uses_rolling_day_count_forecast(product: Product) -> bool:
	return (
		product.product_type == Product.ProductType.DEPOSIT
		and product.income_schedule == Product.IncomeSchedule.TWICE_MONTHLY
		and _metadata_int(product, 'income_interval_days') > 0
	)


def _uses_actual_day_count_forecast(product: Product) -> bool:
	"""Monthly/capitalized deposits accrue by actual days between payments (BNB-style)."""
	if product.product_type != Product.ProductType.DEPOSIT:
		return False
	if product.income_schedule == Product.IncomeSchedule.MONTHLY:
		return True
	return _uses_rolling_day_count_forecast(product)


def _following_weekday(value: date) -> date:
	return following_weekday(value)


def _income_transaction_dates(
	product: Product,
	*,
	transactions: list[Transaction] | None = None,
) -> list[date]:
	source = transactions if transactions is not None else product.transactions.order_by('occurred_at', 'id')
	dates = []
	for ledger_transaction in source:
		if ledger_transaction.transaction_type != Transaction.TransactionType.INCOME:
			continue
		payment_date = timezone.localdate(ledger_transaction.occurred_at)
		if not dates or dates[-1] != payment_date:
			dates.append(payment_date)
	return dates


def latest_deposit_income_date(
	product: Product,
	*,
	transactions: list[Transaction] | None = None,
) -> date | None:
	dates = _income_transaction_dates(product, transactions=transactions)
	return dates[-1] if dates else None


def _rolling_income_dates(
	product: Product,
	*,
	reference: date,
	window_end: date,
	transactions: list[Transaction] | None = None,
) -> list[date]:
	interval_days = _metadata_int(product, 'income_interval_days')
	if interval_days <= 0:
		return []
	payment_dates = _income_transaction_dates(product, transactions=transactions)
	anchor = payment_dates[-1] if payment_dates else None
	if anchor is None:
		return []

	dates = []
	for _ in range(36):
		candidate = _following_weekday(anchor + timedelta(days=interval_days))
		anchor = candidate
		if candidate < reference:
			continue
		if product.maturity_date and candidate > product.maturity_date:
			break
		if candidate > window_end:
			break
		dates.append(candidate)
	return dates


def _unit_price(product: Product) -> Decimal:
	return product.current_price or Decimal('1')


def _quantity_deltas_between(
	transactions: list[Transaction],
	*,
	start_date: date,
	end_date: date,
) -> list[tuple[date, Decimal]]:
	merged: dict[date, Decimal] = {}
	for ledger_transaction in transactions:
		quantity = ledger_transaction.quantity or Decimal('0')
		if quantity == 0:
			continue
		occurred_date = timezone.localdate(ledger_transaction.occurred_at)
		if occurred_date <= start_date or occurred_date >= end_date:
			continue
		merged[occurred_date] = merged.get(occurred_date, Decimal('0')) + quantity
	return sorted(merged.items())


def _principal_day_weight(
	product: Product,
	payment_date: date,
	*,
	previous_payment_date: date,
	principal: Decimal,
	transactions: list[Transaction] | None = None,
	as_of: date | None = None,
) -> Decimal | None:
	"""Sum of principal × days over actual balance segments.

	Bank day-count: accrual starts the day after the previous payment and includes
	the payment day itself (BNB / Belarusbank / Alfa). Top-ups count from the day
	credited; capitalization quantity on the payment day is excluded from this
	period via end_date=payment_date in quantity deltas.
	"""
	if payment_date <= previous_payment_date:
		return None

	accrual_start = previous_payment_date + timedelta(days=1)
	accrual_end = payment_date
	if accrual_end < accrual_start:
		return None

	opening = principal
	events: list[tuple[date, Decimal]] = []
	reference = as_of or timezone.localdate()
	source = transactions
	if source is None:
		source = list(product.transactions.order_by('occurred_at', 'id'))
	if source is not None and previous_payment_date <= reference:
		from apps.common.services.indexed_bonds import units_held_on_date

		held = units_held_on_date(product, previous_payment_date, transactions=source)
		if held > 0:
			opening = held * _unit_price(product)
		events = _quantity_deltas_between(
			source,
			start_date=previous_payment_date,
			end_date=payment_date,
		)

	if opening <= 0:
		return None

	weighted = Decimal('0')
	cursor = accrual_start
	balance = opening
	price = _unit_price(product)
	for event_date, quantity in events:
		if event_date >= payment_date:
			continue
		effective_start = max(event_date, accrual_start)
		if effective_start > accrual_end:
			balance += quantity * price
			continue
		if effective_start > cursor:
			days = (effective_start - cursor).days
			if days > 0:
				weighted += balance * Decimal(days)
			cursor = effective_start
		balance += quantity * price
	if cursor <= accrual_end:
		days = (accrual_end - cursor).days + 1
		if days > 0:
			weighted += balance * Decimal(days)
	return weighted if weighted > 0 else None


def _interest_from_weighted_principal(
	product: Product,
	weighted_principal_days: Decimal,
) -> tuple[Decimal, Decimal | None]:
	basis = _metadata_int(product, 'income_day_count_basis', ALFABANK_DAY_COUNT_BASIS)
	if basis <= 0:
		basis = ALFABANK_DAY_COUNT_BASIS
	amount = (
		weighted_principal_days
		* product.annual_rate_pct
		/ Decimal('100')
		/ Decimal(basis)
	).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
	amount_usd = None
	if product.current_value_usd and product.units and product.units > 0:
		usd_per_unit = product.current_value_usd / product.units
		price = _unit_price(product)
		weighted_usd_days = (
			weighted_principal_days * usd_per_unit / price if price else weighted_principal_days * usd_per_unit
		)
		amount_usd = (
			weighted_usd_days
			* product.annual_rate_pct
			/ Decimal('100')
			/ Decimal(basis)
		).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
	elif getattr(product, 'currency', None) is not None and product.currency.usd_rate:
		amount_usd = (amount * product.currency.usd_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
	return amount, amount_usd


def estimate_rolling_deposit_income_amount(
	product: Product,
	payment_date: date,
	*,
	previous_payment_date: date,
	principal: Decimal | None = None,
	transactions: list[Transaction] | None = None,
	as_of: date | None = None,
) -> tuple[Decimal | None, Decimal | None]:
	if not _uses_actual_day_count_forecast(product):
		return None, None
	if product.annual_rate_pct is None or product.annual_rate_pct <= 0:
		return None, None
	if principal is None:
		principal = product.market_value
	weighted = _principal_day_weight(
		product,
		payment_date,
		previous_payment_date=previous_payment_date,
		principal=principal or Decimal('0'),
		transactions=transactions,
		as_of=as_of,
	)
	if weighted is None:
		return None, None
	return _interest_from_weighted_principal(product, weighted)


def upcoming_deposit_income_dates(
	product: Product,
	*,
	reference: date,
	window_end: date,
	transactions: list[Transaction] | None = None,
) -> list[date]:
	opened_at = _parse_opened_at(product)
	paid_dates = set(_income_transaction_dates(product, transactions=transactions))

	if product.income_schedule == Product.IncomeSchedule.TWICE_MONTHLY:
		if _uses_rolling_day_count_forecast(product):
			rolling_dates = _rolling_income_dates(
				product,
				reference=reference,
				window_end=window_end,
				transactions=transactions,
			)
			if rolling_dates:
				return [candidate for candidate in rolling_dates if candidate not in paid_dates]
		if opened_at is None:
			return []
		day1, day2 = deposit_income_anchor_days(opened_at)
		dates: list[date] = []
		year, month = reference.year, reference.month
		for _ in range(36):
			for candidate in deposit_income_dates_in_month(year, month, day1=day1, day2=day2):
				if candidate < opened_at or candidate < reference:
					continue
				if candidate in paid_dates:
					continue
				if product.maturity_date and candidate > product.maturity_date:
					continue
				if candidate > window_end:
					return dates
				dates.append(candidate)
			month += 1
			if month > 12:
				month = 1
				year += 1
		return dates

	if product.income_schedule == Product.IncomeSchedule.MONTHLY:
		anchor_day = _monthly_anchor_day(product)
		if anchor_day is None:
			return []
		dates = []
		year, month = reference.year, reference.month
		for _ in range(36):
			candidate = _date_on_anchor_day(year, month, anchor_day)
			if opened_at is not None and candidate < opened_at:
				pass
			elif candidate >= reference and candidate not in paid_dates:
				if product.maturity_date and candidate > product.maturity_date:
					break
				if candidate > window_end:
					break
				dates.append(candidate)
			month += 1
			if month > 12:
				month = 1
				year += 1
		return dates

	return []


def estimate_deposit_next_income_date(
	product: Product,
	*,
	today: date,
	transactions: list[Transaction] | None = None,
) -> date | None:
	upcoming = upcoming_deposit_income_dates(
		product,
		reference=today,
		window_end=today.replace(year=today.year + 2),
		transactions=transactions,
	)
	return upcoming[0] if upcoming else None
