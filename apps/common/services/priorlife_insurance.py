from __future__ import annotations

import calendar
import hashlib
import re
from collections import defaultdict
from datetime import date, datetime, time
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

PRIORLIFE_SLUG = 'priorlife'
PREMIUM_INSTITUTION_SLUG = 'income-sources'
PREMIUM_ACCOUNT_NAME = 'Страховые взносы'

DECIMAL_TOKEN_PATTERN = re.compile(r'[\d, ]+(?:\.\d+)?')
HEADER_PATTERN = re.compile(
	r'Информация о взносах по лицевому счету\s*№?\s*(\d+)\s+на\s+(\d{2}\.\d{2}\.\d{4})',
	re.I,
)
PERIOD_PATTERN = re.compile(
	r'Период:\s+с\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})(?:\s+\((\d+)\))?',
	re.I,
)
POLICYHOLDER_PATTERN = re.compile(r'Страхователь:\s*(.+)', re.I)
PAID_ROW_PATTERN = re.compile(
	r'([\d, ]+(?:\.\d+)?)\s+([\d, ]+(?:\.\d+)?)\s*\n'
	r'(\d{2}\.\d{2}\.\d{4})\s+(\d{2}\.\d{2}\.\d{4})\s+([\d, ]+(?:\.\d+)?)\s+USD\s+Уплачено',
	re.I,
)
TOTAL_CONTRACT_PATTERN = re.compile(
	r'([\d, ]+(?:\.\d+)?)\s*\nСумма взносов за весь период страхования',
	re.I,
)
PAID_TOTAL_PATTERN = re.compile(
	r'([\d, ]+(?:\.\d+)?)\s*\nСумма уплаченных взносов по договору',
	re.I,
)
FUTURE_PAYMENTS_PATTERN = re.compile(
	r'([\d, ]+(?:\.\d+)?)\s*\nСумма платежей будущих периодов',
	re.I,
)
OVERPAYMENT_PATTERN = re.compile(
	r'Сумма переплаты\s+([\d, ]+(?:\.\d+)?)\s+USD',
	re.I,
)
PREMIUM_AMOUNT_PATTERN = re.compile(
	r'([\d, ]+(?:\.\d+)?)\s+USD\s*\nзадолженность при ее наличии',
	re.I,
)


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


def _extract_pdf_text(file_path: Path) -> str:
	try:
		import pdfplumber
	except ImportError as exc:
		raise ImportError('pdfplumber is required for Priorlife PDF parsing.') from exc

	chunks: list[str] = []
	with pdfplumber.open(file_path) as pdf:
		for page in pdf.pages:
			chunks.append(page.extract_text() or '')
	return '\n'.join(chunks)


def is_priorlife_contributions_text(text: str) -> bool:
	lowered = text.casefold()
	return 'информация о взносах по лицевому счет' in lowered and 'страхователь:' in lowered


def parse_priorlife_contributions(file_path: Path) -> ParseResult:
	text = _extract_pdf_text(file_path)
	if not is_priorlife_contributions_text(text):
		return ParseResult(warnings=['File does not look like a Priorlife contributions statement.'])

	header_match = HEADER_PATTERN.search(text)
	if not header_match:
		return ParseResult(warnings=['Could not parse Priorlife account header.'])

	account_number = header_match.group(1)
	as_of_date = datetime.strptime(header_match.group(2), '%d.%m.%Y').date()
	period_match = PERIOD_PATTERN.search(text)
	policyholder_match = POLICYHOLDER_PATTERN.search(text)
	policyholder = policyholder_match.group(1).strip() if policyholder_match else ''

	rows: list[dict] = []
	for row_match in PAID_ROW_PATTERN.finditer(text):
		due_date = datetime.strptime(row_match.group(3), '%d.%m.%Y').date()
		payment_date = datetime.strptime(row_match.group(4), '%d.%m.%Y').date()
		amount = _to_decimal(row_match.group(2))
		rows.append(
			{
				'account_number': account_number,
				'due_date': due_date.isoformat(),
				'payment_date': payment_date.isoformat(),
				'amount': str(amount),
				'currency_code': 'USD',
				'status': 'paid',
			}
		)

	footer = {
		'total_contract_premium': str(_to_decimal(TOTAL_CONTRACT_PATTERN.search(text).group(1)))
		if TOTAL_CONTRACT_PATTERN.search(text)
		else '',
		'paid_contributions_total': str(_to_decimal(PAID_TOTAL_PATTERN.search(text).group(1)))
		if PAID_TOTAL_PATTERN.search(text)
		else '',
		'future_payments_total': str(_to_decimal(FUTURE_PAYMENTS_PATTERN.search(text).group(1)))
		if FUTURE_PAYMENTS_PATTERN.search(text)
		else '',
		'overpayment_total': str(_to_decimal(OVERPAYMENT_PATTERN.search(text).group(1)))
		if OVERPAYMENT_PATTERN.search(text)
		else '',
		'scheduled_premium_amount': str(_to_decimal(PREMIUM_AMOUNT_PATTERN.search(text).group(1)))
		if PREMIUM_AMOUNT_PATTERN.search(text)
		else '',
	}

	statement = {
		'account_number': account_number,
		'as_of_date': as_of_date.isoformat(),
		'policyholder': policyholder,
		'contract_start': period_match.group(1) if period_match else '',
		'contract_end': period_match.group(2) if period_match else '',
		'contract_years': period_match.group(3) if period_match and period_match.group(3) else '',
		'currency_code': 'USD',
		'contributions': rows,
		**footer,
	}

	return ParseResult(
		records=rows,
		metadata={
			'parser_variant': 'priorlife-contributions',
			'rows': len(rows),
			'account_number': account_number,
			'as_of_date': statement['as_of_date'],
		},
		artifacts={'statement': statement},
	)


def _fingerprint(*parts: str) -> str:
	payload = ':'.join(parts)
	return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def ensure_priorlife_bootstrap() -> dict:
	usd = Currency.objects.get(code='USD')
	priorlife, _ = FinancialInstitution.objects.update_or_create(
		slug=PRIORLIFE_SLUG,
		defaults={
			'name': 'Приорлайф',
			'institution_type': FinancialInstitution.InstitutionType.INSURANCE,
			'country': 'BY',
			'website': 'https://priorlife.by/',
			'base_currency': usd,
			'metadata': {'bootstrap': True},
		},
	)
	premium_institution, _ = FinancialInstitution.objects.update_or_create(
		slug=PREMIUM_INSTITUTION_SLUG,
		defaults={
			'name': 'Доходы',
			'institution_type': FinancialInstitution.InstitutionType.OTHER,
			'country': 'BY',
			'base_currency': usd,
			'metadata': {'bootstrap': True, 'purpose': 'payroll_source'},
		},
	)
	premium_account, _ = Account.objects.get_or_create(
		institution=premium_institution,
		name=PREMIUM_ACCOUNT_NAME,
		defaults={
			'account_type': Account.AccountType.OTHER,
			'currency': usd,
			'metadata': {'bootstrap': True, 'purpose': 'insurance_premiums'},
		},
	)
	if premium_account.currency_id != usd.id:
		premium_account.currency = usd
		premium_account.save(update_fields=['currency', 'updated_at'])
	return {
		'priorlife': priorlife,
		'premium_account': premium_account,
		'usd': usd,
	}


def _product_name(account_number: str) -> str:
	return f'Приорлайф №{account_number}'


def _parse_contract_date(raw: str) -> date | None:
	value = str(raw or '').strip()
	if not value:
		return None
	for fmt in ('%d.%m.%Y', '%Y-%m-%d'):
		try:
			return datetime.strptime(value, fmt).date()
		except ValueError:
			continue
	return None


def _split_premium_load(gross: Decimal, load_pct: Decimal) -> tuple[Decimal, Decimal]:
	if gross <= 0:
		return Decimal('0'), Decimal('0')
	if load_pct <= 0:
		return gross, Decimal('0')
	load_amount = (gross * load_pct / Decimal('100')).quantize(Decimal('0.01'))
	return gross - load_amount, load_amount


def compute_priorlife_balances(
	*,
	gross_paid: Decimal,
	load_pct: Decimal,
	accumulated_amount: Decimal | None = None,
	accrued_yield_reported: Decimal | None = None,
	additional_accrued_yield_reported: Decimal | None = None,
) -> dict[str, str]:
	net_paid, load_deducted = _split_premium_load(gross_paid, load_pct)
	reported_yield = accrued_yield_reported or Decimal('0')
	additional_yield = additional_accrued_yield_reported or Decimal('0')

	if accumulated_amount is not None and accumulated_amount > 0:
		yield_in_account = accumulated_amount - net_paid - additional_yield
		total_accumulated = accumulated_amount
	else:
		yield_in_account = reported_yield + additional_yield
		total_accumulated = net_paid + yield_in_account

	return {
		'paid_contributions_gross': str(gross_paid),
		'paid_contributions_total': str(gross_paid),
		'net_contributions_total': str(net_paid),
		'contract_load_deducted_total': str(load_deducted),
		'accrued_yield_reported': str(reported_yield),
		'accrued_yield_in_account': str(yield_in_account),
		'additional_accrued_yield_reported': str(additional_yield),
		'additional_accrued_yield_in_account': str(additional_yield),
		'accumulated_amount': str(total_accumulated),
	}


def _month_end(year: int, month: int) -> date:
	return date(year, month, calendar.monthrange(year, month)[1])


def spread_yield_by_contribution_months(
	total_yield: Decimal,
	contributions: list[dict],
	*,
	load_pct: Decimal,
) -> list[tuple[date, Decimal]]:
	"""Allocate accrued yield across month-ends, weighted by cumulative net balance."""
	if total_yield <= 0 or not contributions:
		return []

	monthly_net: dict[tuple[int, int], Decimal] = defaultdict(lambda: Decimal('0'))
	for row in contributions:
		payment_date = date.fromisoformat(row['payment_date'])
		month_key = (payment_date.year, payment_date.month)
		gross = _to_decimal(row.get('amount'))
		net_amount, _ = _split_premium_load(gross, load_pct)
		monthly_net[month_key] += net_amount

	if not monthly_net:
		return []

	month_weights: list[tuple[tuple[int, int], Decimal]] = []
	running_balance = Decimal('0')
	for month_key, net_amount in sorted(monthly_net.items()):
		running_balance += net_amount
		month_weights.append((month_key, running_balance))

	total_weight = sum(weight for _, weight in month_weights)
	if total_weight <= 0:
		return []

	rows: list[tuple[date, Decimal]] = []
	allocated = Decimal('0')
	for index, ((year, month), weight) in enumerate(month_weights):
		if index == len(month_weights) - 1:
			amount = total_yield - allocated
		else:
			amount = (total_yield * weight / total_weight).quantize(Decimal('0.01'))
			allocated += amount
		rows.append((_month_end(year, month), amount))
	return rows


def _build_product_metadata(
	statement: dict,
	*,
	contract_details: dict | None = None,
	balance_fields: dict | None = None,
) -> dict:
	details = contract_details or {}
	balances = balance_fields or {}
	metadata = {
		'program': details.get('program', ''),
		'insurance_type': details.get('insurance_type', 'life'),
		'contract_status': details.get('contract_status', 'active'),
		'contract_number': statement.get('account_number', ''),
		'account_number': statement.get('account_number', ''),
		'policyholder': statement.get('policyholder', ''),
		'contract_date': details.get('contract_date', ''),
		'contract_start': statement.get('contract_start', '') or details.get('contract_start', ''),
		'contract_end': statement.get('contract_end', '') or details.get('contract_end', ''),
		'contract_years': statement.get('contract_years', '') or details.get('contract_years', ''),
		'premium_amount': details.get('premium_amount', statement.get('scheduled_premium_amount', '')),
		'premium_currency': statement.get('currency_code', 'USD'),
		'premium_schedule': details.get('premium_schedule', 'monthly'),
		'contract_load_pct': details.get('contract_load_pct', ''),
		'guaranteed_yield_pct': details.get('guaranteed_yield_pct', ''),
		'accrued_yield_reported': balances.get('accrued_yield_reported', details.get('accrued_yield', '')),
		'accrued_yield_in_account': balances.get('accrued_yield_in_account', ''),
		'additional_accrued_yield_reported': balances.get(
			'additional_accrued_yield_reported',
			details.get('additional_accrued_yield', ''),
		),
		'additional_accrued_yield_in_account': balances.get('additional_accrued_yield_in_account', ''),
		'total_contract_premium': statement.get('total_contract_premium', ''),
		'paid_contributions_gross': balances.get('paid_contributions_gross', statement.get('paid_contributions_total', '')),
		'paid_contributions_total': balances.get('paid_contributions_total', statement.get('paid_contributions_total', '')),
		'net_contributions_total': balances.get('net_contributions_total', ''),
		'contract_load_deducted_total': balances.get('contract_load_deducted_total', ''),
		'future_payments_total': statement.get('future_payments_total', ''),
		'overpayment_total': statement.get('overpayment_total', ''),
		'accumulated_amount': balances.get('accumulated_amount', details.get('accumulated_amount', '')),
		'as_of_date': statement.get('as_of_date', ''),
		'liquidity': 'locked_until_maturity',
		'imported_from': 'priorlife-contributions',
	}
	return metadata


def _accumulated_amount_from_metadata(metadata: dict) -> Decimal:
	accumulated = _to_decimal(metadata.get('accumulated_amount'))
	if accumulated > 0:
		return accumulated
	net_paid = _to_decimal(metadata.get('net_contributions_total'))
	yield_in_account = _to_decimal(metadata.get('accrued_yield_in_account'))
	additional_yield = _to_decimal(metadata.get('additional_accrued_yield_in_account'))
	total = net_paid + yield_in_account + additional_yield
	return total if total > 0 else net_paid


def _amount_usd(amount: Decimal, currency: Currency, rate_date: date | None = None) -> Decimal:
	target_date = rate_date or timezone.localdate()
	rate = get_usd_conversion_rate(currency, target_date)
	return amount * rate


def _schedule_to_income_schedule(schedule: str) -> str:
	mapping = {
		'monthly': Product.IncomeSchedule.MONTHLY,
		'quarterly': Product.IncomeSchedule.QUARTERLY,
		'annual': Product.IncomeSchedule.ANNUAL,
		'semi_annual': Product.IncomeSchedule.SEMI_ANNUAL,
		'ежемесячно': Product.IncomeSchedule.MONTHLY,
		'ежеквартально': Product.IncomeSchedule.QUARTERLY,
		'ежегодно': Product.IncomeSchedule.ANNUAL,
	}
	return mapping.get(str(schedule or '').strip().casefold(), '')


def _contract_details_from_source(raw_import_file) -> dict | None:
	if not raw_import_file.source or not isinstance(raw_import_file.source.config, dict):
		return None
	config = raw_import_file.source.config
	details = {
		key: str(config.get(key, '') or '').strip()
		for key in (
			'contract_date',
			'contract_load_pct',
			'guaranteed_yield_pct',
			'accrued_yield',
			'additional_accrued_yield',
			'accumulated_amount',
			'premium_amount',
			'premium_schedule',
			'insurance_type',
			'program',
			'contract_status',
		)
		if str(config.get(key, '') or '').strip()
	}
	return details or None


@transaction.atomic
def persist_priorlife_contributions(
	raw_import_file,
	result: ParseResult,
	*,
	contract_details: dict | None = None,
) -> int:
	statement = result.artifacts.get('statement')
	if not statement:
		return 0

	if contract_details is None:
		contract_details = _contract_details_from_source(raw_import_file)

	bootstrap = ensure_priorlife_bootstrap()
	priorlife = bootstrap['priorlife']
	usd = bootstrap['usd']
	premium_account = bootstrap['premium_account']
	account_number = statement['account_number']
	details = dict(contract_details or {})
	gross_paid = _to_decimal(statement.get('paid_contributions_total'))
	load_pct = _to_decimal(details.get('contract_load_pct'))
	balance_fields = compute_priorlife_balances(
		gross_paid=gross_paid,
		load_pct=load_pct,
		accumulated_amount=_to_decimal(details.get('accumulated_amount')) or None,
		accrued_yield_reported=_to_decimal(details.get('accrued_yield')) or None,
		additional_accrued_yield_reported=_to_decimal(details.get('additional_accrued_yield')) or None,
	)

	as_of_raw = statement.get('as_of_date', '')
	as_of_date = datetime.strptime(as_of_raw, '%Y-%m-%d').date() if as_of_raw else timezone.localdate()
	as_of_dt = timezone.make_aware(datetime.combine(as_of_date, datetime.min.time()))
	metadata = _build_product_metadata(statement, contract_details=details, balance_fields=balance_fields)
	accumulated_amount = _accumulated_amount_from_metadata(metadata)
	maturity_date = _parse_contract_date(metadata.get('contract_end', ''))
	guaranteed_yield = metadata.get('guaranteed_yield_pct')
	premium_schedule = _schedule_to_income_schedule(metadata.get('premium_schedule', ''))

	product, _ = Product.objects.update_or_create(
		institution=priorlife,
		external_id=account_number,
		defaults={
			'name': _product_name(account_number),
			'product_type': Product.ProductType.LIFE_INSURANCE,
			'currency': usd,
			'units': Decimal('1'),
		},
	)
	product.name = _product_name(account_number)
	product.product_type = Product.ProductType.LIFE_INSURANCE
	product.currency = usd
	product.units = Decimal('1')
	product.current_price = accumulated_amount
	product.is_active = metadata.get('contract_status', 'active') != 'closed'
	product.maturity_date = maturity_date
	if guaranteed_yield not in (None, ''):
		product.annual_rate_pct = _to_decimal(guaranteed_yield)
	if premium_schedule:
		product.income_schedule = premium_schedule
	metadata['accumulated_amount'] = str(accumulated_amount)
	product.metadata = metadata
	product.save(
		update_fields=[
			'name',
			'product_type',
			'currency',
			'units',
			'current_price',
			'is_active',
			'maturity_date',
			'annual_rate_pct',
			'income_schedule',
			'metadata',
			'updated_at',
		]
	)

	product.current_value_usd = _amount_usd(accumulated_amount, usd, rate_date=as_of_date)
	product.save(update_fields=['current_value_usd', 'updated_at'])

	BalanceSnapshot.objects.update_or_create(
		institution=priorlife,
		product=product,
		captured_at=as_of_dt,
		defaults={
			'currency': usd,
			'balance': accumulated_amount,
			'balance_usd': product.current_value_usd,
			'metadata': {
				'imported_from': 'priorlife-contributions',
				'paid_contributions_gross': balance_fields.get('paid_contributions_gross', ''),
				'net_contributions_total': balance_fields.get('net_contributions_total', ''),
				'contract_load_deducted_total': balance_fields.get('contract_load_deducted_total', ''),
				'accrued_yield_in_account': balance_fields.get('accrued_yield_in_account', ''),
				'additional_accrued_yield_in_account': balance_fields.get('additional_accrued_yield_in_account', ''),
			},
		},
	)

	records_created = 1
	load_pct = _to_decimal(metadata.get('contract_load_pct'))
	for row in statement.get('contributions', []):
		amount = _to_decimal(row.get('amount'))
		if amount <= 0:
			continue
		net_amount, load_amount = _split_premium_load(amount, load_pct)
		payment_date = datetime.strptime(row['payment_date'], '%Y-%m-%d').date()
		occurred_at = timezone.make_aware(datetime.combine(payment_date, datetime.min.time()))
		fingerprint = _fingerprint(
			'priorlife',
			account_number,
			row['payment_date'],
			row.get('due_date', ''),
			row.get('amount', ''),
		)
		_, created = Transaction.objects.update_or_create(
			import_fingerprint=fingerprint,
			defaults={
				'account': premium_account,
				'product': product,
				'import_job': raw_import_file.job,
				'transaction_type': Transaction.TransactionType.DEPOSIT,
				'currency': usd,
				'amount': amount,
				'amount_usd': _amount_usd(amount, usd, rate_date=payment_date),
				'occurred_at': occurred_at,
				'description': f'Приорлайф взнос — {payment_date:%d.%m.%Y}',
				'metadata': {
					'imported_from': 'priorlife-contributions',
					'contribution_source': 'premium',
					'due_date': row.get('due_date', ''),
					'payment_date': row.get('payment_date', ''),
					'gross_amount': str(amount),
					'net_amount': str(net_amount),
					'load_amount': str(load_amount),
					'load_pct': str(load_pct),
				},
			},
		)
		if created:
			records_created += 1

	income_specs = [
		(
			'accrued_yield_in_account',
			_to_decimal(metadata.get('accrued_yield_in_account')),
			'Начисленная доходность (в счёте)',
		),
		(
			'additional_accrued_yield_in_account',
			_to_decimal(metadata.get('additional_accrued_yield_in_account')),
			'Начисленная дополнительная доходность (в счёте)',
		),
	]
	Transaction.objects.filter(
		product=product,
		transaction_type=Transaction.TransactionType.INCOME,
		metadata__imported_from='priorlife-contributions',
	).delete()
	for income_kind, total_amount, description_prefix in income_specs:
		if total_amount <= 0:
			continue
		for month_end, amount in spread_yield_by_contribution_months(
			total_amount,
			statement.get('contributions', []),
			load_pct=load_pct,
		):
			if amount <= 0:
				continue
			occurred_at = timezone.make_aware(datetime.combine(month_end, time(12, 0)))
			fingerprint = _fingerprint(
				'priorlife',
				account_number,
				'income',
				income_kind,
				month_end.isoformat(),
			)
			Transaction.objects.update_or_create(
				import_fingerprint=fingerprint,
				defaults={
					'account': premium_account,
					'product': product,
					'import_job': raw_import_file.job,
					'transaction_type': Transaction.TransactionType.INCOME,
					'currency': usd,
					'amount': amount,
					'amount_usd': _amount_usd(amount, usd, rate_date=month_end),
					'occurred_at': occurred_at,
					'description': f'{description_prefix} — {month_end:%m.%Y}',
					'metadata': {
						'imported_from': 'priorlife-contributions',
						'income_kind': income_kind,
						'accrual_month': month_end.strftime('%Y-%m'),
						'spread_accrual': True,
					},
				},
			)
			records_created += 1

	return records_created


def import_priorlife_files(
	*,
	contributions_path: Path | None = None,
	contract_details: dict | None = None,
) -> dict:
	summary = {
		'contribution_records': 0,
		'account_number': '',
	}
	if contributions_path is None:
		return summary

	result = parse_priorlife_contributions(contributions_path)

	class _RawFile:
		job = None

	summary['contribution_records'] = persist_priorlife_contributions(
		_RawFile(),
		result,
		contract_details=contract_details,
	)
	summary['account_number'] = result.metadata.get('account_number', '')
	return summary
