from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.common.models import Currency
from apps.common.services.bnb_deposits import (
	_amount_usd,
	_extract_pdf_text,
	_fingerprint,
	_parse_date_from_text,
	_parse_decimal_from_text,
	_to_decimal,
)
from apps.imports.services.parsers.base import ParseResult
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product
from apps.products.services.deposit_schedule import estimate_deposit_next_income_date

ALFABANK_SLUG = 'alfabank'
ALFABANK_BYN_ACCOUNT_NAME = 'АльфаБанк BYN Account'

PRODUCT_NAMES_BY_CONTRACT = {
	'BY95ALFA341430LV871050270000': 'ALFA1',
	'BY13ALFA341430LV871040270000': 'ALFA2',
	'BY28ALFA341430LV871030270000': 'ALFA3',
}

TRANSACTION_PATTERN = re.compile(
	r'(\d{2}\.\d{2}\.\d{4})\s+([\d, ]+(?:\.\d+)?)\s+BYN\s+\2\s+BYN',
	re.I,
)


def is_alfabank_deposit_statement_text(text: str) -> bool:
	lowered = text.casefold()
	return (
		'альфа-банк' in lowered
		and 'номер депозитного договора' in lowered
		and 'alfa' in lowered
		and 'детальная информация по депозиту' in lowered
	)


def _parse_transactions(text: str) -> list[dict]:
	normalized = re.sub(r'\s+', ' ', text)
	rows: list[dict] = []
	for match in TRANSACTION_PATTERN.finditer(normalized):
		occurred_date = datetime.strptime(match.group(1), '%d.%m.%Y').date()
		amount = _to_decimal(match.group(2))
		rows.append(
			{
				'occurred_at': datetime.combine(occurred_date, datetime.min.time()).isoformat(sep=' '),
				'amount': str(amount),
				'operation_kind': 'top_up',
			}
		)
	return rows


def parse_alfabank_deposit_statement(file_path: Path) -> ParseResult:
	text = _extract_pdf_text(file_path)
	if not is_alfabank_deposit_statement_text(text):
		return ParseResult(warnings=['File does not look like an Alfabank deposit statement.'])

	contract_match = re.search(r'Номер депозитного договора\s+(BY[A-Z0-9]+)', text, re.I)
	if not contract_match:
		return ParseResult(warnings=['Could not parse Alfabank deposit contract number.'])

	contract_number = contract_match.group(1)
	return_account_match = re.search(r'Номер счета для возврата денег\s+(BY[A-Z0-9]+)', text, re.I)
	period_match = re.search(
		r'Выписка по депозиту за период\s+(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})',
		text,
		re.I,
	)
	transactions = _parse_transactions(text)
	opened_at = _parse_date_from_text(text, 'Дата открытия') or date.min
	initial_amount = _parse_decimal_from_text(text, 'Сумма на дату открытия') or Decimal('0')
	statement = {
		'contract_number': contract_number,
		'deposit_account': contract_number,
		'return_account': return_account_match.group(1) if return_account_match else '',
		'opened_at': opened_at.isoformat(),
		'maturity_date': (_parse_date_from_text(text, 'Дата возврата') or date.min).isoformat(),
		'initial_amount_byn': str(initial_amount),
		'annual_rate_pct': str(_parse_decimal_from_text(text, 'Процентная ставка, текущая') or Decimal('0')),
		'balance_byn': str(_parse_decimal_from_text(text, 'Текущий остаток') or Decimal('0')),
		'as_of_date': (
			datetime.strptime(period_match.group(2), '%d.%m.%Y').date().isoformat()
			if period_match
			else date.min.isoformat()
		),
		'interest_mode': 'payout',
		'income_schedule': Product.IncomeSchedule.TWICE_MONTHLY,
		'transactions': transactions,
	}

	return ParseResult(
		records=[statement],
		metadata={
			'parser_variant': 'alfabank-deposit-statement',
			'rows': len(transactions),
			'contract_number': contract_number,
			'as_of_date': statement['as_of_date'],
		},
		artifacts={'statement': statement},
	)


def ensure_alfabank_bootstrap() -> dict:
	byn = Currency.objects.get(code='BYN')
	alfabank, _ = FinancialInstitution.objects.update_or_create(
		slug=ALFABANK_SLUG,
		defaults={
			'name': 'АльфаБанк',
			'institution_type': FinancialInstitution.InstitutionType.BANK,
			'country': 'BY',
			'base_currency': byn,
			'metadata': {'bootstrap': True},
		},
	)
	byn_account, _ = Account.objects.get_or_create(
		institution=alfabank,
		name=ALFABANK_BYN_ACCOUNT_NAME,
		defaults={
			'account_type': Account.AccountType.BANK,
			'currency': byn,
			'metadata': {'bootstrap': True},
		},
	)
	return {'alfabank': alfabank, 'byn_account': byn_account, 'byn': byn}


def _product_name_from_source(raw_import_file, contract_number: str) -> str:
	if raw_import_file.source and isinstance(raw_import_file.source.config, dict):
		config_name = str(raw_import_file.source.config.get('product_name', '')).strip()
		if config_name:
			return config_name

	filename = (raw_import_file.original_filename or '').strip()
	stem = Path(filename).stem.upper()
	if stem in {'ALFA1', 'ALFA2', 'ALFA3'}:
		return stem

	return PRODUCT_NAMES_BY_CONTRACT.get(contract_number, f'ALFA {contract_number[-4:]}')


def _build_product_metadata(statement: dict) -> dict:
	return {
		'contract_number': statement.get('contract_number', ''),
		'deposit_account': statement.get('deposit_account', ''),
		'return_account': statement.get('return_account', ''),
		'opened_at': statement.get('opened_at', ''),
		'interest_mode': statement.get('interest_mode', 'payout'),
		'as_of_date': statement.get('as_of_date', ''),
		'initial_amount_byn': statement.get('initial_amount_byn', ''),
		'balance_byn': statement.get('balance_byn', ''),
		'imported_from': 'alfabank-deposit-statement',
	}


@transaction.atomic
def persist_alfabank_deposit_statement(raw_import_file, result: ParseResult) -> int:
	statement = result.artifacts.get('statement')
	if not statement:
		return 0

	bootstrap = ensure_alfabank_bootstrap()
	alfabank = bootstrap['alfabank']
	byn_account = bootstrap['byn_account']
	byn = bootstrap['byn']

	contract_number = statement['contract_number']
	product_name = _product_name_from_source(raw_import_file, contract_number)
	balance = _to_decimal(statement.get('balance_byn'))
	annual_rate = _to_decimal(statement.get('annual_rate_pct'))
	opened_raw = statement.get('opened_at', '')
	opened_at = datetime.strptime(opened_raw, '%Y-%m-%d').date() if opened_raw else None
	maturity_raw = statement.get('maturity_date', '')
	maturity_date = datetime.strptime(maturity_raw, '%Y-%m-%d').date() if maturity_raw else None
	as_of_raw = statement.get('as_of_date', '')
	as_of_date = datetime.strptime(as_of_raw, '%Y-%m-%d').date() if as_of_raw else timezone.localdate()
	as_of_dt = timezone.make_aware(datetime.combine(as_of_date, datetime.min.time()))
	initial_amount = _to_decimal(statement.get('initial_amount_byn'))

	product, _ = Product.objects.update_or_create(
		institution=alfabank,
		external_id=contract_number,
		defaults={
			'name': product_name,
			'product_type': Product.ProductType.DEPOSIT,
			'currency': byn,
			'units': balance,
			'current_price': Decimal('1'),
		},
	)
	product.name = product_name
	product.product_type = Product.ProductType.DEPOSIT
	product.currency = byn
	product.income_account = byn_account
	product.units = balance
	product.current_price = Decimal('1')
	product.is_active = True
	product.annual_rate_pct = annual_rate
	product.maturity_date = maturity_date
	product.income_schedule = Product.IncomeSchedule.TWICE_MONTHLY
	product.metadata = _build_product_metadata(statement)
	product.current_value_usd = _amount_usd(balance, byn, rate_date=as_of_date)
	product.terms_updated_at = timezone.now()
	product.save(
		update_fields=[
			'name',
			'product_type',
			'currency',
			'income_account',
			'units',
			'current_price',
			'is_active',
			'annual_rate_pct',
			'maturity_date',
			'income_schedule',
			'metadata',
			'current_value_usd',
			'terms_updated_at',
			'updated_at',
		]
	)

	next_income_date = estimate_deposit_next_income_date(product, today=as_of_date)
	if next_income_date:
		product.next_income_date = next_income_date
		product.save(update_fields=['next_income_date', 'updated_at'])

	BalanceSnapshot.objects.update_or_create(
		institution=alfabank,
		product=product,
		captured_at=as_of_dt,
		defaults={
			'currency': byn,
			'balance': balance,
			'balance_usd': product.current_value_usd,
			'metadata': {
				'imported_from': 'alfabank-deposit-statement',
				'contract_number': contract_number,
				'annual_rate_pct': statement.get('annual_rate_pct', ''),
			},
		},
	)

	records_created = 1
	if opened_at and initial_amount > 0:
		opening_at = timezone.make_aware(datetime.combine(opened_at, datetime.min.time()))
		opening_fingerprint = _fingerprint(
			'alfabank-deposit',
			contract_number,
			'opening',
			opened_at.isoformat(),
			str(initial_amount),
		)
		_, created = Transaction.objects.update_or_create(
			import_fingerprint=opening_fingerprint,
			defaults={
				'account': byn_account,
				'product': product,
				'import_job': raw_import_file.job,
				'transaction_type': Transaction.TransactionType.DEPOSIT,
				'currency': byn,
				'amount': initial_amount,
				'amount_usd': _amount_usd(initial_amount, byn, rate_date=opened_at),
				'quantity': initial_amount,
				'unit_price': Decimal('1'),
				'occurred_at': opening_at,
				'description': f'Открытие депозита {product_name}',
				'metadata': {
					'imported_from': 'alfabank-deposit-statement',
					'operation_kind': 'opening',
					'exclude_from_account_balance': True,
				},
			},
		)
		if created:
			records_created += 1

	for row in statement.get('transactions', []):
		amount = _to_decimal(row.get('amount'))
		if amount <= 0:
			continue
		occurred_at = timezone.make_aware(datetime.fromisoformat(row['occurred_at']))
		if (
			opened_at
			and occurred_at.date() == opened_at
			and initial_amount > 0
			and amount == initial_amount
		):
			continue
		fingerprint = _fingerprint(
			'alfabank-deposit',
			contract_number,
			row.get('occurred_at', ''),
			row.get('amount', ''),
			row.get('operation_kind', 'top_up'),
		)
		_, created = Transaction.objects.update_or_create(
			import_fingerprint=fingerprint,
			defaults={
				'account': byn_account,
				'product': product,
				'import_job': raw_import_file.job,
				'transaction_type': Transaction.TransactionType.DEPOSIT,
				'currency': byn,
				'amount': amount,
				'amount_usd': _amount_usd(amount, byn, rate_date=occurred_at.date()),
				'quantity': amount,
				'unit_price': Decimal('1'),
				'occurred_at': occurred_at,
				'description': f'Пополнение депозита {product_name}',
				'metadata': {
					'imported_from': 'alfabank-deposit-statement',
					'operation_kind': 'top_up',
					'exclude_from_account_balance': True,
				},
			},
		)
		if created:
			records_created += 1

	return records_created
