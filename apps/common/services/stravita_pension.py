from __future__ import annotations

import calendar
import hashlib
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.common.models import Currency
from apps.common.services.exchange_rates import get_usd_conversion_rate
from apps.imports.services.parsers.base import ParseResult
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product

STRAVITA_SLUG = 'stravita'
PAYROLL_INSTITUTION_SLUG = 'income-sources'
PAYROLL_ACCOUNT_NAME = 'Зарплата'

CONTRIBUTION_LINE_PATTERN = re.compile(
	r'^(\d{2}\.\d{2}\.\d{4})\s+(.+?)\s+([\d,]+(?:\.\d+)?)$',
)
DECIMAL_TOKEN_PATTERN = re.compile(r'[\d,]+(?:\.\d+)?')


def _to_decimal(value) -> Decimal:
	if value in (None, ''):
		return Decimal('0')
	if isinstance(value, Decimal):
		return value
	normalized = str(value).strip().replace(' ', '').replace(',', '.')
	return Decimal(normalized)


def _parse_decimal_from_text(text: str, label: str) -> Decimal | None:
	match = re.search(rf'{re.escape(label)}\s+({DECIMAL_TOKEN_PATTERN.pattern})', text, re.I)
	if not match:
		return None
	return _to_decimal(match.group(1))


def _parse_date_from_text(text: str, label: str) -> date | None:
	match = re.search(rf'{re.escape(label)}\s+(\d{{2}}\.\d{{2}}\.\d{{4}})', text, re.I)
	if not match:
		return None
	return datetime.strptime(match.group(1), '%d.%m.%Y').date()


def _extract_pdf_text(file_path: Path) -> str:
	try:
		import pdfplumber
	except ImportError as exc:
		raise ImportError('pdfplumber is required for Stravita PDF parsing.') from exc

	chunks: list[str] = []
	with pdfplumber.open(file_path) as pdf:
		for page in pdf.pages:
			chunks.append(page.extract_text() or '')
	return '\n'.join(chunks)


def is_stravita_extract_text(text: str) -> bool:
	lowered = text.casefold()
	return 'именной лицевой сч' in lowered and 'накопленная сумма' in lowered


def is_stravita_contributions_text(text: str) -> bool:
	lowered = text.casefold()
	return 'информация о взносах по лицевому сч' in lowered


def parse_stravita_extract(file_path: Path) -> ParseResult:
	text = _extract_pdf_text(file_path)
	if not is_stravita_extract_text(text):
		return ParseResult(warnings=['File does not look like a Stravita account statement.'])

	account_match = re.search(r'№\s*(\d{2,}[A-Z0-9]+)', text)
	account_number = account_match.group(1) if account_match else ''
	certificate_match = re.search(
		r'серия:\s*([A-Z]{2})\s+номер:\s*(\d+)\s+от:\s*(\d{2}\.\d{2}\.\d{4})',
		text,
		re.I,
	)
	contract_period_match = re.search(
		r'с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})',
		text,
		re.I,
	)
	as_of_match = re.search(r'по состоянию на\s+(\d{2}\.\d{2}\.\d{4})', text, re.I)

	employee_tariff = _parse_decimal_from_text(text, 'Тариф Страхователя')
	employer_tariff = _parse_decimal_from_text(text, 'Тариф Работодателя')
	total_tariff = _parse_decimal_from_text(text, 'Тариф по договору')
	contributions_total = _parse_decimal_from_text(text, 'Итого')
	contributions_employee = None
	contributions_employer = None
	employee_employer_match = re.search(
		r'Итого\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)',
		text,
		re.I,
	)
	if employee_employer_match:
		contributions_total = _to_decimal(employee_employer_match.group(1))
		contributions_employee = _to_decimal(employee_employer_match.group(2))
		contributions_employer = _to_decimal(employee_employer_match.group(3))

	accumulated_amount = _parse_decimal_from_text(text, 'Накопленная сумма, BYN')
	if accumulated_amount is None:
		accumulated_amount = _parse_decimal_from_text(text, 'Накопленная сумма')
	insurance_bonus = _parse_decimal_from_text(text, 'страховой бонус')
	insurance_sum = _parse_decimal_from_text(text, 'Страховая сумма, BYN')
	if insurance_sum is None:
		insurance_sum = _parse_decimal_from_text(text, 'Страховая сумма')

	employers: list[dict] = []
	seen_indexes: set[int] = set()
	for row_match in re.finditer(
		r'(\d+)\s+(.+?)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s*(?:[—\-]|$)',
		text,
		re.M,
	):
		name = re.sub(r'\s+', ' ', row_match.group(2).strip())
		if name.casefold().startswith('итого'):
			continue
		index = int(row_match.group(1))
		seen_indexes.add(index)
		employers.append(
			{
				'index': index,
				'name': name,
				'total_byn': str(_to_decimal(row_match.group(3))),
				'employee_byn': str(_to_decimal(row_match.group(4))),
				'employer_byn': str(_to_decimal(row_match.group(5))),
			}
		)

	for row_match in re.finditer(
		r'(?m)^(\d)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s*(?:[—\-]|$)',
		text,
	):
		index = int(row_match.group(1))
		if index in seen_indexes:
			continue
		name = 'ФСЦ ДиМ Центрального района г. Минска'
		if 'ФСЦ ДиМ Центрального' not in text:
			name = f'Работодатель {index}'
		seen_indexes.add(index)
		employers.append(
			{
				'index': index,
				'name': name,
				'total_byn': str(_to_decimal(row_match.group(2))),
				'employee_byn': str(_to_decimal(row_match.group(3))),
				'employer_byn': str(_to_decimal(row_match.group(4))),
			}
		)
	employers.sort(key=lambda item: item['index'])

	refinancing_yield = Decimal('0')
	if accumulated_amount is not None and contributions_total is not None:
		refinancing_yield = accumulated_amount - contributions_total - (insurance_bonus or Decimal('0'))

	as_of_date = None
	if as_of_match:
		as_of_date = datetime.strptime(as_of_match.group(1), '%d.%m.%Y').date()

	record = {
		'account_number': account_number,
		'as_of_date': as_of_date.isoformat() if as_of_date else '',
		'certificate_series': certificate_match.group(1) if certificate_match else '',
		'certificate_number': certificate_match.group(2) if certificate_match else '',
		'certificate_date': certificate_match.group(3) if certificate_match else '',
		'contract_start': contract_period_match.group(1) if contract_period_match else '',
		'contract_end': contract_period_match.group(2) if contract_period_match else '',
		'employee_tariff_pct': str(employee_tariff or Decimal('0')),
		'employer_tariff_pct': str(employer_tariff or Decimal('0')),
		'total_tariff_pct': str(total_tariff or Decimal('0')),
		'contributions_total_byn': str(contributions_total or Decimal('0')),
		'contributions_employee_byn': str(contributions_employee or Decimal('0')),
		'contributions_employer_byn': str(contributions_employer or Decimal('0')),
		'accumulated_amount_byn': str(accumulated_amount or Decimal('0')),
		'insurance_bonus_byn': str(insurance_bonus or Decimal('0')),
		'refinancing_yield_byn': str(refinancing_yield),
		'insurance_sum_byn': str(insurance_sum or Decimal('0')),
		'employers': employers,
	}

	return ParseResult(
		records=[record],
		metadata={
			'parser_variant': 'stravita-extract',
			'rows': 1,
			'account_number': account_number,
			'as_of_date': record['as_of_date'],
		},
		artifacts={'statement': record},
	)


def parse_stravita_contributions(file_path: Path) -> ParseResult:
	text = _extract_pdf_text(file_path)
	if not is_stravita_contributions_text(text):
		return ParseResult(warnings=['File does not look like a Stravita contributions report.'])

	account_match = re.search(r'№\s*(\d{2,}[A-Z0-9]+)', text)
	account_number = account_match.group(1) if account_match else ''

	rows: list[dict] = []
	for line in text.splitlines():
		match = CONTRIBUTION_LINE_PATTERN.match(line.strip())
		if not match:
			continue
		payment_date = datetime.strptime(match.group(1), '%d.%m.%Y').date()
		employer_name = match.group(2).strip()
		amount = _to_decimal(match.group(3))
		employee_share = amount / Decimal('2')
		employer_share = amount / Decimal('2')
		rows.append(
			{
				'account_number': account_number,
				'payment_date': payment_date.isoformat(),
				'employer_name': employer_name,
				'amount_byn': str(amount),
				'employee_share_byn': str(employee_share),
				'employer_share_byn': str(employer_share),
			}
		)

	return ParseResult(
		records=rows,
		metadata={
			'parser_variant': 'stravita-contributions',
			'rows': len(rows),
			'account_number': account_number,
		},
		artifacts={'contributions': rows},
	)


def _fingerprint(*parts: str) -> str:
	payload = ':'.join(parts)
	return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _month_end(year: int, month: int) -> date:
	return date(year, month, calendar.monthrange(year, month)[1])


def spread_income_by_cumulative_contributions(
	total_amount: Decimal,
	monthly_contributions: dict[tuple[int, int], Decimal],
) -> list[tuple[date, Decimal]]:
	"""Allocate income across month-ends, weighted by cumulative contributions at each month-end."""
	if total_amount <= 0 or not monthly_contributions:
		return []

	month_weights: list[tuple[tuple[int, int], Decimal]] = []
	running_balance = Decimal('0')
	for month_key, month_amount in sorted(monthly_contributions.items()):
		running_balance += month_amount
		month_weights.append((month_key, running_balance))

	total_weight = sum(weight for _, weight in month_weights)
	if total_weight <= 0:
		return []

	rows: list[tuple[date, Decimal]] = []
	allocated = Decimal('0')
	for index, ((year, month), weight) in enumerate(month_weights):
		if index == len(month_weights) - 1:
			amount = total_amount - allocated
		else:
			amount = (total_amount * weight / total_weight).quantize(Decimal('0.01'))
			allocated += amount
		if amount > 0:
			rows.append((_month_end(year, month), amount))
	return rows


def _monthly_contribution_totals(product: Product, as_of_date: date) -> dict[tuple[int, int], Decimal]:
	monthly: dict[tuple[int, int], Decimal] = defaultdict(lambda: Decimal('0'))
	for tx in Transaction.objects.filter(
		product=product,
		transaction_type=Transaction.TransactionType.DEPOSIT,
	):
		tx_date = tx.occurred_at.date()
		if tx_date > as_of_date:
			continue
		amount = tx.amount or Decimal('0')
		if amount > 0:
			monthly[(tx_date.year, tx_date.month)] += amount
	return dict(monthly)


def _sync_stravita_spread_income(
	*,
	product: Product,
	account_number: str,
	statement: dict,
	payroll_account: Account,
	byn: Currency,
	as_of_date: date,
	import_job,
) -> int:
	monthly_totals = _monthly_contribution_totals(product, as_of_date)
	if not monthly_totals:
		return 0

	as_of_raw = statement.get('as_of_date', '') or as_of_date.isoformat()
	Transaction.objects.filter(
		product=product,
		transaction_type=Transaction.TransactionType.INCOME,
		metadata__imported_from='stravita-extract',
	).delete()

	records_created = 0
	income_rows = [
		('insurance_bonus', _to_decimal(statement.get('insurance_bonus_byn')), 'Страховой бонус'),
		(
			'refinancing_yield',
			_to_decimal(statement.get('refinancing_yield_byn')),
			'Доходность (ставка рефинансирования)',
		),
	]
	for income_kind, total_amount, description in income_rows:
		if total_amount <= 0:
			continue
		for month_end, amount in spread_income_by_cumulative_contributions(total_amount, monthly_totals):
			if amount <= 0:
				continue
			fingerprint = _fingerprint(
				'stravita',
				account_number,
				'income',
				income_kind,
				month_end.isoformat(),
			)
			occurred_at = timezone.make_aware(datetime.combine(month_end, datetime.min.time()))
			Transaction.objects.update_or_create(
				import_fingerprint=fingerprint,
				defaults={
					'account': payroll_account,
					'product': product,
					'import_job': import_job,
					'transaction_type': Transaction.TransactionType.INCOME,
					'currency': byn,
					'amount': amount,
					'amount_usd': _amount_usd(amount, byn, rate_date=month_end),
					'occurred_at': occurred_at,
					'description': f'{description} — {month_end:%m.%Y}',
					'metadata': {
						'imported_from': 'stravita-extract',
						'income_kind': income_kind,
						'as_of_date': as_of_raw,
						'accrual_month': month_end.strftime('%Y-%m'),
						'spread_accrual': True,
					},
				},
			)
			records_created += 1
	return records_created


def ensure_stravita_bootstrap(*, management_expense_pct: Decimal | None = None) -> dict:
	byn = Currency.objects.get(code='BYN')
	stravita, _ = FinancialInstitution.objects.update_or_create(
		slug=STRAVITA_SLUG,
		defaults={
			'name': 'Стравита',
			'institution_type': FinancialInstitution.InstitutionType.INSURANCE,
			'country': 'BY',
			'website': 'https://stravita.by/',
			'base_currency': byn,
			'metadata': {'bootstrap': True},
		},
	)
	income_institution, _ = FinancialInstitution.objects.update_or_create(
		slug=PAYROLL_INSTITUTION_SLUG,
		defaults={
			'name': 'Доходы',
			'institution_type': FinancialInstitution.InstitutionType.OTHER,
			'country': 'BY',
			'base_currency': byn,
			'metadata': {'bootstrap': True, 'purpose': 'payroll_source'},
		},
	)
	payroll_account, _ = Account.objects.get_or_create(
		institution=income_institution,
		name=PAYROLL_ACCOUNT_NAME,
		defaults={
			'account_type': Account.AccountType.OTHER,
			'currency': byn,
			'metadata': {'bootstrap': True, 'purpose': 'payroll'},
		},
	)
	return {
		'stravita': stravita,
		'payroll_account': payroll_account,
		'byn': byn,
		'management_expense_pct': management_expense_pct,
	}


def _product_name_from_statement(statement: dict) -> str:
	series = statement.get('certificate_series', '')
	number = statement.get('certificate_number', '')
	if series and number:
		return f'ДНПС {series}-{number}'
	return f'ДНПС {statement.get("account_number", "")}'


def _build_product_metadata(statement: dict, *, management_expense_pct: Decimal | None = None) -> dict:
	metadata = {
		'program': 'dnps_state',
		'certificate_series': statement.get('certificate_series', ''),
		'certificate_number': statement.get('certificate_number', ''),
		'certificate_date': statement.get('certificate_date', ''),
		'contract_start': statement.get('contract_start', ''),
		'contract_end': statement.get('contract_end', ''),
		'employee_tariff_pct': statement.get('employee_tariff_pct', ''),
		'employer_tariff_pct': statement.get('employer_tariff_pct', ''),
		'total_tariff_pct': statement.get('total_tariff_pct', ''),
		'liquidity': 'locked_until_retirement',
		'status': 'active',
		'guaranteed_yield_type': 'refinancing_rate',
		'insurance_sum_byn': statement.get('insurance_sum_byn', ''),
		'as_of_date': statement.get('as_of_date', ''),
		'contributions_total_byn': statement.get('contributions_total_byn', ''),
		'contributions_employee_byn': statement.get('contributions_employee_byn', ''),
		'contributions_employer_byn': statement.get('contributions_employer_byn', ''),
		'insurance_bonus_byn': statement.get('insurance_bonus_byn', ''),
		'refinancing_yield_byn': statement.get('refinancing_yield_byn', ''),
		'accumulated_amount_byn': statement.get('accumulated_amount_byn', ''),
		'employers': statement.get('employers', []),
		'imported_from': 'stravita-extract',
	}
	if management_expense_pct is not None:
		metadata['management_expense_pct'] = str(management_expense_pct)
	return metadata


def _amount_usd(amount: Decimal, currency: Currency, rate_date: date | None = None) -> Decimal:
	target_date = rate_date or timezone.localdate()
	rate = get_usd_conversion_rate(currency, target_date)
	return amount * rate


@transaction.atomic
def persist_stravita_extract(
	raw_import_file,
	result: ParseResult,
	*,
	management_expense_pct: Decimal | None = None,
) -> int:
	statement = result.artifacts.get('statement')
	if not statement:
		return 0

	bootstrap = ensure_stravita_bootstrap(management_expense_pct=management_expense_pct)
	stravita = bootstrap['stravita']
	byn = bootstrap['byn']
	payroll_account = bootstrap['payroll_account']
	if management_expense_pct is None and bootstrap.get('management_expense_pct') is not None:
		management_expense_pct = bootstrap['management_expense_pct']

	accumulated_amount = _to_decimal(statement.get('accumulated_amount_byn'))
	as_of_raw = statement.get('as_of_date', '')
	as_of_date = datetime.strptime(as_of_raw, '%Y-%m-%d').date() if as_of_raw else timezone.localdate()
	as_of_dt = timezone.make_aware(datetime.combine(as_of_date, datetime.min.time()))

	product, _ = Product.objects.update_or_create(
		institution=stravita,
		external_id=statement['account_number'],
		defaults={
			'name': _product_name_from_statement(statement),
			'product_type': Product.ProductType.PENSION,
			'currency': byn,
			'units': Decimal('1'),
		},
	)
	product.name = _product_name_from_statement(statement)
	product.product_type = Product.ProductType.PENSION
	product.currency = byn
	product.units = Decimal('1')
	product.current_price = accumulated_amount
	product.is_active = True
	product.metadata = _build_product_metadata(statement, management_expense_pct=management_expense_pct)
	product.save(
		update_fields=[
			'name',
			'product_type',
			'currency',
			'units',
			'current_price',
			'is_active',
			'metadata',
			'updated_at',
		]
	)

	product.current_value_usd = accumulated_amount * get_usd_conversion_rate(byn, as_of_date)
	product.save(update_fields=['current_value_usd', 'updated_at'])

	BalanceSnapshot.objects.update_or_create(
		institution=stravita,
		product=product,
		captured_at=as_of_dt,
		defaults={
			'currency': byn,
			'balance': accumulated_amount,
			'balance_usd': product.current_value_usd,
			'metadata': {
				'imported_from': 'stravita-extract',
				'contributions_total_byn': statement.get('contributions_total_byn', ''),
				'insurance_bonus_byn': statement.get('insurance_bonus_byn', ''),
				'refinancing_yield_byn': statement.get('refinancing_yield_byn', ''),
			},
		},
	)

	records_created = 1 + _sync_stravita_spread_income(
		product=product,
		account_number=statement['account_number'],
		statement=statement,
		payroll_account=payroll_account,
		byn=byn,
		as_of_date=as_of_date,
		import_job=raw_import_file.job,
	)

	return records_created


@transaction.atomic
def persist_stravita_contributions(raw_import_file, result: ParseResult) -> int:
	rows = result.artifacts.get('contributions', [])
	if not rows:
		return 0

	bootstrap = ensure_stravita_bootstrap()
	stravita = bootstrap['stravita']
	byn = bootstrap['byn']
	payroll_account = bootstrap['payroll_account']
	account_number = result.metadata.get('account_number') or rows[0].get('account_number', '')

	product = Product.objects.filter(institution=stravita, external_id=account_number).first()
	if product is None:
		product, _ = Product.objects.get_or_create(
			institution=stravita,
			external_id=account_number,
			defaults={
				'name': f'ДНПС {account_number}',
				'product_type': Product.ProductType.PENSION,
				'currency': byn,
				'units': Decimal('1'),
				'metadata': {'program': 'dnps_state', 'imported_from': 'stravita-contributions'},
			},
		)

	records_created = 0
	for row in rows:
		amount = _to_decimal(row.get('amount_byn'))
		if amount <= 0:
			continue
		payment_date = datetime.strptime(row['payment_date'], '%Y-%m-%d').date()
		occurred_at = timezone.make_aware(datetime.combine(payment_date, datetime.min.time()))
		employer_name = row.get('employer_name', '')
		fingerprint = _fingerprint(
			'stravita',
			account_number,
			row['payment_date'],
			employer_name,
			row.get('amount_byn', ''),
		)
		employee_share = _to_decimal(row.get('employee_share_byn'))
		employer_share = _to_decimal(row.get('employer_share_byn'))
		_, created = Transaction.objects.update_or_create(
			import_fingerprint=fingerprint,
			defaults={
				'account': payroll_account,
				'product': product,
				'import_job': raw_import_file.job,
				'transaction_type': Transaction.TransactionType.DEPOSIT,
				'currency': byn,
				'amount': amount,
				'amount_usd': _amount_usd(amount, byn, rate_date=payment_date),
				'occurred_at': occurred_at,
				'description': f'ДНПС взнос — {employer_name}',
				'metadata': {
					'imported_from': 'stravita-contributions',
					'contribution_source': 'payroll',
					'employer_name': employer_name,
					'employee_share_byn': str(employee_share),
					'employer_share_byn': str(employer_share),
				},
			},
		)
		if created:
			records_created += 1

	metadata = product.metadata if isinstance(product.metadata, dict) else {}
	as_of_raw = metadata.get('as_of_date', '')
	if as_of_raw and metadata.get('imported_from') == 'stravita-extract':
		as_of_date = datetime.strptime(as_of_raw, '%Y-%m-%d').date()
		records_created += _sync_stravita_spread_income(
			product=product,
			account_number=account_number,
			statement=metadata,
			payroll_account=payroll_account,
			byn=byn,
			as_of_date=as_of_date,
			import_job=raw_import_file.job,
		)

	return records_created


def import_stravita_pension_files(
	*,
	extract_path: Path | None = None,
	contributions_path: Path | None = None,
	management_expense_pct: Decimal | None = None,
) -> dict:
	summary = {
		'extract_records': 0,
		'contribution_records': 0,
		'account_number': '',
	}
	if extract_path is not None:
		result = parse_stravita_extract(extract_path)
		class _RawFile:
			job = None

		summary['extract_records'] = persist_stravita_extract(
			_RawFile(),
			result,
			management_expense_pct=management_expense_pct,
		)
		summary['account_number'] = result.metadata.get('account_number', '')

	if contributions_path is not None:
		result = parse_stravita_contributions(contributions_path)
		class _RawFile:
			job = None

		summary['contribution_records'] = persist_stravita_contributions(_RawFile(), result)
		if not summary['account_number']:
			summary['account_number'] = result.metadata.get('account_number', '')

	return summary
