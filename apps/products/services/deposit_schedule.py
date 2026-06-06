from __future__ import annotations

from calendar import monthrange
from datetime import date

from apps.products.models import Product


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


def upcoming_deposit_income_dates(
	product: Product,
	*,
	reference: date,
	window_end: date,
) -> list[date]:
	if product.income_schedule != Product.IncomeSchedule.TWICE_MONTHLY:
		return []

	opened_at = _parse_opened_at(product)
	if opened_at is None:
		return []

	day1, day2 = deposit_income_anchor_days(opened_at)
	dates: list[date] = []
	year, month = reference.year, reference.month
	for _ in range(36):
		for candidate in deposit_income_dates_in_month(year, month, day1=day1, day2=day2):
			if candidate < opened_at or candidate < reference:
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


def estimate_deposit_next_income_date(product: Product, *, today: date) -> date | None:
	upcoming = upcoming_deposit_income_dates(product, reference=today, window_end=today.replace(year=today.year + 2))
	return upcoming[0] if upcoming else None
