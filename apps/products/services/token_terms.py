from __future__ import annotations

import csv
import json
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.utils import timezone

from apps.accounts.models import Transaction
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product

FINSTORE_INCOME_OPERATION = 'Получение дохода'

INCOME_SCHEDULE_ALIASES = {
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
	for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y'):
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

	if row.next_income_date is not None and product.next_income_date != row.next_income_date:
		product.next_income_date = row.next_income_date
		changed_fields.append('next_income_date')

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


def last_income_payment_date(product: Product) -> date | None:
	transactions = Transaction.objects.filter(product=product).order_by('-occurred_at', '-id')
	for tx in transactions:
		operation_type = ''
		if isinstance(tx.metadata, dict):
			operation_type = tx.metadata.get('operation_type', '')
		if operation_type == FINSTORE_INCOME_OPERATION or (
			tx.transaction_type == Transaction.TransactionType.INCOME and not (tx.quantity or 0)
		):
			return timezone.localdate(tx.occurred_at)
	return None


def estimate_next_income_date(product: Product, *, today: date | None = None) -> date | None:
	if not product.income_schedule or product.income_schedule == Product.IncomeSchedule.AT_MATURITY:
		return None

	month_delta = schedule_month_delta(product.income_schedule)
	if month_delta is None:
		return None

	last_payment = last_income_payment_date(product)
	reference = today or timezone.localdate()
	if last_payment is None:
		return None

	candidate = _add_months(last_payment, month_delta)
	while candidate < reference:
		candidate = _add_months(candidate, month_delta)
	return candidate


def recompute_next_income_dates(
	institution: FinancialInstitution | None = None,
	*,
	overwrite: bool = False,
	product_ids: list[int] | None = None,
	today: date | None = None,
) -> int:
	queryset = Product.objects.filter(is_active=True).exclude(income_schedule='')
	if institution is not None:
		queryset = queryset.filter(institution=institution)
	if product_ids:
		queryset = queryset.filter(pk__in=product_ids)

	updated = 0
	for product in queryset:
		if product.next_income_date and not overwrite:
			continue
		estimated = estimate_next_income_date(product, today=today)
		if estimated is None:
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
	overwrite_next_dates: bool = False,
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
