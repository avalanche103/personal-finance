from __future__ import annotations

import csv
import json
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from statistics import median

from django.utils import timezone

from apps.accounts.models import Transaction
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product

FINSTORE_INCOME_OPERATION = 'Получение дохода'

INCOME_SCHEDULE_ALIASES = {
	'twice_monthly': Product.IncomeSchedule.TWICE_MONTHLY,
	'2x_monthly': Product.IncomeSchedule.TWICE_MONTHLY,
	'semi_monthly': Product.IncomeSchedule.TWICE_MONTHLY,
	'2 times per month': Product.IncomeSchedule.TWICE_MONTHLY,
	'2 раза в месяц': Product.IncomeSchedule.TWICE_MONTHLY,
	'два раза в месяц': Product.IncomeSchedule.TWICE_MONTHLY,
	'monthly': Product.IncomeSchedule.MONTHLY,
	'ежемесячно': Product.IncomeSchedule.MONTHLY,
	'ежемесячная': Product.IncomeSchedule.MONTHLY,
	'quarterly': Product.IncomeSchedule.QUARTERLY,
	'ежеквартально': Product.IncomeSchedule.QUARTERLY,
	'ежеквартальная': Product.IncomeSchedule.QUARTERLY,
	'semi_annual': Product.IncomeSchedule.SEMI_ANNUAL,
	'semi-annual': Product.IncomeSchedule.SEMI_ANNUAL,
	'полугодовая': Product.IncomeSchedule.SEMI_ANNUAL,
	'annual': Product.IncomeSchedule.ANNUAL,
	'ежегодно': Product.IncomeSchedule.ANNUAL,
	'at_maturity': Product.IncomeSchedule.AT_MATURITY,
	'погашение': Product.IncomeSchedule.AT_MATURITY,
	'other': Product.IncomeSchedule.OTHER,
}

CSV_FIELD_ALIASES = {
	'token_id': {'token_id', 'id', 'tokenid'},
	'external_id': {'external_id', 'token_name', 'name', 'token', 'название_токена', 'токен'},
	'symbol': {'symbol', 'ticker'},
	'annual_rate_pct': {'annual_rate_pct', 'rate', 'rate_pct', 'ставка', 'доходность', 'ставка_годовых'},
	'maturity_date': {'maturity_date', 'maturity', 'погашение', 'дата_погашения', 'срок', 'срок_обращения'},
	'income_schedule': {'income_schedule', 'schedule', 'выплаты', 'график_выплат', 'периодичность_выплат'},
	'next_income_date': {'next_income_date', 'next_payment', 'следующая_выплата'},
}


@dataclass
class TokenTermsRow:
	token_id: str = ''
	external_id: str = ''
	symbol: str = ''
	annual_rate_pct: Decimal | None = None
	maturity_date: date | None = None
	income_schedule: str = ''
	next_income_date: date | None = None


@dataclass
class TokenTermsImportResult:
	rows_total: int = 0
	matched: int = 0
	updated: int = 0
	skipped: int = 0
	unmatched: list[str] | None = None

	def __post_init__(self):
		if self.unmatched is None:
			self.unmatched = []


def _normalize_header(value: str) -> str:
	return re.sub(r'[\s\-]+', '_', value.strip().lower())


def _map_csv_headers(fieldnames: list[str] | None) -> dict[str, str]:
	mapping: dict[str, str] = {}
	if not fieldnames:
		return mapping

	normalized = {_normalize_header(name): name for name in fieldnames}
	for canonical, aliases in CSV_FIELD_ALIASES.items():
		for alias in aliases:
			key = _normalize_header(alias)
			if key in normalized:
				mapping[canonical] = normalized[key]
				break
	return mapping


def _parse_decimal(value: str) -> Decimal | None:
	text = (value or '').strip().replace('%', '').replace(',', '.')
	if not text:
		return None
	try:
		return Decimal(text)
	except InvalidOperation:
		return None


def _parse_date(value: str) -> date | None:
	text = (value or '').strip()
	if not text:
		return None
	for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y'):
		try:
			return datetime.strptime(text, fmt).date()
		except ValueError:
			continue
	try:
		return date.fromisoformat(text[:10])
	except ValueError:
		return None


def _parse_income_schedule(value: str) -> str:
	text = (value or '').strip().lower()
	if not text:
		return ''
	return INCOME_SCHEDULE_ALIASES.get(text, '')


def _row_from_mapping(mapping: dict[str, str], raw: dict[str, str]) -> TokenTermsRow:
	def cell(key: str) -> str:
		column = mapping.get(key)
		if not column:
			return ''
		return (raw.get(column) or '').strip()

	rate = _parse_decimal(cell('annual_rate_pct'))
	maturity = _parse_date(cell('maturity_date'))
	next_income = _parse_date(cell('next_income_date'))
	schedule = _parse_income_schedule(cell('income_schedule'))

	return TokenTermsRow(
		token_id=cell('token_id'),
		external_id=cell('external_id'),
		symbol=cell('symbol'),
		annual_rate_pct=rate,
		maturity_date=maturity,
		income_schedule=schedule,
		next_income_date=next_income,
	)


def _row_from_json_item(item: dict) -> TokenTermsRow:
	return TokenTermsRow(
		token_id=str(item.get('token_id', '') or '').strip(),
		external_id=str(item.get('external_id', item.get('token_name', '')) or '').strip(),
		symbol=str(item.get('symbol', '') or '').strip(),
		annual_rate_pct=_parse_decimal(str(item.get('annual_rate_pct', item.get('rate', '')) or '')),
		maturity_date=_parse_date(str(item.get('maturity_date', item.get('term', '')) or '')),
		income_schedule=_parse_income_schedule(str(item.get('income_schedule', '') or '')),
		next_income_date=_parse_date(str(item.get('next_income_date', '') or '')),
	)


def load_token_terms_from_json_payload(payload) -> list[TokenTermsRow]:
	if isinstance(payload, dict):
		payload = payload.get('tokens') or payload.get('items') or []
	rows = []
	for item in payload:
		if isinstance(item, dict):
			rows.append(_row_from_json_item(item))
	return rows


def load_token_terms_rows(path: Path) -> list[TokenTermsRow]:
	suffix = path.suffix.lower()
	if suffix == '.json':
		return load_token_terms_from_json_payload(json.loads(path.read_text(encoding='utf-8')))

	rows: list[TokenTermsRow] = []
	with path.open('r', encoding='utf-8-sig', newline='') as handle:
		reader = csv.DictReader(handle)
		mapping = _map_csv_headers(reader.fieldnames)
		for raw in reader:
			row = _row_from_mapping(mapping, raw)
			if any([row.token_id, row.external_id, row.symbol]):
				rows.append(row)
	return rows


def resolve_finstore_institution(slug: str = 'finstore') -> FinancialInstitution:
	return FinancialInstitution.objects.get(slug=slug)


def find_product_for_terms(institution: FinancialInstitution, row: TokenTermsRow) -> Product | None:
	if row.external_id:
		product = Product.objects.filter(institution=institution, external_id=row.external_id).first()
		if product:
			return product
		product = Product.objects.filter(institution=institution, name=row.external_id).first()
		if product:
			return product

	if row.token_id:
		product = Product.objects.filter(
			institution=institution,
			metadata__token_id=str(row.token_id),
		).first()
		if product:
			return product
		suffix = f'_({row.token_id})'
		product = Product.objects.filter(institution=institution, external_id__endswith=suffix).first()
		if product:
			return product

	if row.symbol:
		queryset = Product.objects.filter(institution=institution, symbol__iexact=row.symbol)
		if queryset.count() == 1:
			return queryset.first()

	return None


def apply_terms_to_product(product: Product, row: TokenTermsRow, *, update_timestamp: bool = True) -> list[str]:
	changed_fields: list[str] = []

	if row.annual_rate_pct is not None and product.annual_rate_pct != row.annual_rate_pct:
		product.annual_rate_pct = row.annual_rate_pct
		changed_fields.append('annual_rate_pct')

	if row.maturity_date is not None and product.maturity_date != row.maturity_date:
		product.maturity_date = row.maturity_date
		changed_fields.append('maturity_date')

	if row.income_schedule and product.income_schedule != row.income_schedule:
		product.income_schedule = row.income_schedule
		changed_fields.append('income_schedule')

	# next_income_date is always derived from income history, not imported files.

	if changed_fields:
		if update_timestamp:
			product.terms_updated_at = timezone.now()
			changed_fields.append('terms_updated_at')
		product.save(update_fields=changed_fields)

	return changed_fields


def _add_months(base: date, months: int) -> date:
	year = base.year + (base.month - 1 + months) // 12
	month = (base.month - 1 + months) % 12 + 1
	day = min(base.day, monthrange(year, month)[1])
	return date(year, month, day)


def schedule_month_delta(schedule: str) -> int | None:
	return {
		Product.IncomeSchedule.MONTHLY: 1,
		Product.IncomeSchedule.QUARTERLY: 3,
		Product.IncomeSchedule.SEMI_ANNUAL: 6,
		Product.IncomeSchedule.ANNUAL: 12,
	}.get(schedule)


def schedule_payments_per_year(schedule: str) -> int | None:
	return {
		Product.IncomeSchedule.TWICE_MONTHLY: 24,
		Product.IncomeSchedule.MONTHLY: 12,
		Product.IncomeSchedule.QUARTERLY: 4,
		Product.IncomeSchedule.SEMI_ANNUAL: 2,
		Product.IncomeSchedule.ANNUAL: 1,
	}.get(schedule)


def estimate_next_income_amount(
	product: Product,
	*,
	payment_dates: list[date] | None = None,
) -> tuple[Decimal | None, Decimal | None]:
	"""Coupon per period from annual_rate_pct, units, and price. Returns (native, usd)."""
	from apps.common.services.indexed_bonds import latest_usd_byn_rate, planned_coupon_usd_per_unit

	payment_dates = payment_dates if payment_dates is not None else income_payment_dates(product)
	next_payment_date = product.next_income_date
	if next_payment_date is None:
		next_payment_date = estimate_next_income_date(product)
	planned_usd = planned_coupon_usd_per_unit(product, next_payment_date)
	if planned_usd is not None and (product.units or Decimal('0')) > 0:
		amount_usd = (planned_usd * product.units).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
		usd_byn_rate = latest_usd_byn_rate()
		if usd_byn_rate is None and isinstance(product.metadata, dict):
			usd_byn_rate = _parse_decimal(str(product.metadata.get('placement_fx_rate', '')))
		amount_native = None
		if usd_byn_rate:
			amount_native = (amount_usd * usd_byn_rate).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
		return amount_native, amount_usd

	if product.annual_rate_pct is None or product.annual_rate_pct <= 0:
		return None, None

	principal = product.market_value
	if principal <= 0:
		return None, None

	schedule = resolve_income_schedule(product, payment_dates)
	if schedule == Product.IncomeSchedule.AT_MATURITY:
		return None, None

	periods_per_year = schedule_payments_per_year(schedule)
	if periods_per_year is None:
		return None, None

	period_income = (
		principal * product.annual_rate_pct / Decimal('100') / Decimal(periods_per_year)
	).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

	amount_usd = None
	if product.current_value_usd and product.current_value_usd > 0:
		amount_usd = (
			product.current_value_usd * product.annual_rate_pct / Decimal('100') / Decimal(periods_per_year)
		).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
	elif getattr(product, 'currency', None) is not None and product.currency.usd_rate:
		amount_usd = (period_income * product.currency.usd_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

	return period_income, amount_usd


def is_income_transaction(transaction: Transaction) -> bool:
	operation_type = ''
	if isinstance(transaction.metadata, dict):
		operation_type = transaction.metadata.get('operation_type', '')
	if operation_type == FINSTORE_INCOME_OPERATION:
		return True
	return (
		transaction.transaction_type == Transaction.TransactionType.INCOME
		and not (transaction.quantity or 0)
	)


def income_payment_dates(product: Product) -> list[date]:
	dates: list[date] = []
	seen: set[date] = set()
	for tx in Transaction.objects.filter(product=product).order_by('occurred_at', 'id'):
		if not is_income_transaction(tx):
			continue
		payment_date = timezone.localdate(tx.occurred_at)
		if payment_date not in seen:
			seen.add(payment_date)
			dates.append(payment_date)
	return dates


def last_income_payment_date(product: Product) -> date | None:
	dates = income_payment_dates(product)
	return dates[-1] if dates else None


def infer_schedule_from_payment_dates(dates: list[date]) -> str:
	if len(dates) < 2:
		return ''

	gaps = [(later - earlier).days for earlier, later in zip(dates, dates[1:]) if (later - earlier).days > 0]
	if not gaps:
		return ''

	median_gap = int(median(gaps))
	if 12 <= median_gap <= 18:
		return Product.IncomeSchedule.TWICE_MONTHLY
	if 25 <= median_gap <= 38:
		return Product.IncomeSchedule.MONTHLY
	if 80 <= median_gap <= 100:
		return Product.IncomeSchedule.QUARTERLY
	if 170 <= median_gap <= 200:
		return Product.IncomeSchedule.SEMI_ANNUAL
	if 350 <= median_gap <= 380:
		return Product.IncomeSchedule.ANNUAL
	return ''


def typical_income_day_of_month(dates: list[date]) -> int | None:
	if not dates:
		return None
	return int(median([payment.day for payment in dates]))


def _date_on_day(year: int, month: int, day: int) -> date:
	return date(year, month, min(day, monthrange(year, month)[1]))


def _advance_by_gap(last_payment: date, gap_days: int, typical_day: int | None, reference: date) -> date:
	candidate = last_payment + timedelta(days=gap_days)
	if typical_day is not None:
		candidate = _date_on_day(candidate.year, candidate.month, typical_day)
	while candidate < reference:
		candidate = candidate + timedelta(days=gap_days)
		if typical_day is not None:
			candidate = _date_on_day(candidate.year, candidate.month, typical_day)
	return candidate


def resolve_income_schedule(product: Product, payment_dates: list[date]) -> str:
	if product.income_schedule and product.income_schedule != Product.IncomeSchedule.OTHER:
		return product.income_schedule
	inferred = infer_schedule_from_payment_dates(payment_dates)
	return inferred or product.income_schedule


def maybe_update_income_schedule_from_history(product: Product, payment_dates: list[date]) -> bool:
	if product.income_schedule and product.income_schedule != Product.IncomeSchedule.OTHER:
		return False
	inferred = infer_schedule_from_payment_dates(payment_dates)
	if not inferred:
		return False
	product.income_schedule = inferred
	product.save(update_fields=['income_schedule', 'updated_at'])
	return True


def estimate_next_income_date(product: Product, *, today: date | None = None) -> date | None:
	reference = today or timezone.localdate()
	payment_dates = income_payment_dates(product)

	if product.income_schedule == Product.IncomeSchedule.AT_MATURITY:
		if product.maturity_date and product.maturity_date >= reference:
			return product.maturity_date
		return None

	if product.product_type == Product.ProductType.DEPOSIT and product.income_schedule == Product.IncomeSchedule.TWICE_MONTHLY:
		from apps.products.services.deposit_schedule import estimate_deposit_next_income_date

		scheduled = estimate_deposit_next_income_date(product, today=reference)
		if scheduled is not None:
			return scheduled

	if not payment_dates:
		if product.next_income_date and product.next_income_date >= reference:
			return product.next_income_date
		return None

	schedule = resolve_income_schedule(product, payment_dates)
	typical_day = typical_income_day_of_month(payment_dates)
	last_payment = payment_dates[-1]

	if schedule == Product.IncomeSchedule.AT_MATURITY:
		if product.maturity_date and product.maturity_date >= reference:
			return product.maturity_date
		return None

	if schedule == Product.IncomeSchedule.TWICE_MONTHLY:
		return _advance_by_gap(last_payment, 15, typical_day, reference)

	month_delta = schedule_month_delta(schedule)
	if month_delta is not None:
		candidate = _add_months(last_payment, month_delta)
		if typical_day is not None:
			candidate = _date_on_day(candidate.year, candidate.month, typical_day)
		while candidate < reference:
			candidate = _add_months(candidate, month_delta)
			if typical_day is not None:
				candidate = _date_on_day(candidate.year, candidate.month, typical_day)
		return candidate

	if len(payment_dates) >= 2:
		gaps = [(later - earlier).days for earlier, later in zip(payment_dates, payment_dates[1:]) if (later - earlier).days > 0]
		if gaps:
			gap_days = max(int(median(gaps)), 1)
			return _advance_by_gap(last_payment, gap_days, typical_day, reference)

	return None


def recompute_next_income_dates(
	institution: FinancialInstitution | None = None,
	*,
	overwrite: bool = True,
	product_ids: list[int] | None = None,
	today: date | None = None,
) -> int:
	queryset = Product.objects.filter(is_active=True, product_type=Product.ProductType.TOKEN)
	if institution is not None:
		queryset = queryset.filter(institution=institution)
	if product_ids:
		queryset = queryset.filter(pk__in=product_ids)

	updated = 0
	for product in queryset:
		payment_dates = income_payment_dates(product)
		if not payment_dates and not product.income_schedule:
			continue

		maybe_update_income_schedule_from_history(product, payment_dates)

		if product.next_income_date and not overwrite:
			continue

		estimated = estimate_next_income_date(product, today=today)
		if estimated is None:
			if overwrite and product.next_income_date is not None:
				product.next_income_date = None
				product.save(update_fields=['next_income_date', 'updated_at'])
				updated += 1
			continue

		if product.next_income_date == estimated:
			continue

		product.next_income_date = estimated
		product.save(update_fields=['next_income_date', 'updated_at'])
		updated += 1
	return updated


def import_token_terms_from_file(
	path: Path,
	*,
	institution_slug: str = 'finstore',
	dry_run: bool = False,
	recompute_dates: bool = True,
	overwrite_next_dates: bool = True,
) -> TokenTermsImportResult:
	institution = resolve_finstore_institution(institution_slug)
	rows = load_token_terms_rows(path)
	result = TokenTermsImportResult(rows_total=len(rows))

	for row in rows:
		product = find_product_for_terms(institution, row)
		if product is None:
			label = row.external_id or row.token_id or row.symbol or 'unknown'
			result.unmatched.append(label)
			result.skipped += 1
			continue

		result.matched += 1
		if dry_run:
			continue

		changed = apply_terms_to_product(product, row)
		if changed:
			result.updated += 1

	if recompute_dates and not dry_run:
		recompute_next_income_dates(institution, overwrite=overwrite_next_dates)

	return result
