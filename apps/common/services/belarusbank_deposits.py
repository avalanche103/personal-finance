from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.accounts.services.balance import sync_account_balance
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
from apps.products.services.token_terms import estimate_next_income_date

BELARUSBANK_SLUG = 'belarusbank'
BELARUSBANK_BYN_ACCOUNT_NAME = 'Беларусбанк BYN Account'

DECIMAL_TOKEN_PATTERN = re.compile(r'[\d, ]+(?:\.\d+)?')
TRANSACTION_PATTERN = re.compile(
	r'(\d{2}\.\d{2}\.\d{4})\s+\d{2}\.\d{2}\.\d{4}\s+'
	r'(.+?)\s+BYN\s+Приход\s+'
	r'([\d, ]+(?:\.\d+)?)\s+([\d, ]+(?:\.\d+)?)\s+([\d, ]+(?:\.\d+)?)'
	r'.*?\s(\d{2}:\d{2})\s+\d{2}:\d{2}',
	re.I,
)


def is_belarusbank_deposit_statement_text(text: str) -> bool:
	lowered = text.casefold()
	return (
		'выписка по вкладу' in lowered
		and 'iban:' in lowered
		and 'akbb' in lowered
		and 'регистрационный номер приложения' in lowered
	)


def _classify_operation(description: str) -> str:
	lowered = description.casefold()
	if 'открыт' in lowered:
		return 'opening'
	if 'капитализац' in lowered:
		return 'capitalization'
	if 'пополнен' in lowered:
		return 'top_up'
	return 'other'


def _parse_transactions(text: str) -> list[dict]:
	section_match = re.search(r'по вкладу(.*)', text, re.I | re.S)
	if not section_match:
		return []
	normalized = re.sub(r'\s+', ' ', section_match.group(1))
	rows: list[dict] = []
	for match in TRANSACTION_PATTERN.finditer(normalized):
		occurred_date = datetime.strptime(match.group(1), '%d.%m.%Y').date()
		occurred_time = datetime.strptime(match.group(6), '%H:%M').time()
		description = re.sub(r'\s+', ' ', match.group(2).strip())
		amount = _to_decimal(match.group(3))
		balance_after = _to_decimal(match.group(5))
		rows.append(
			{
				'occurred_at': datetime.combine(occurred_date, occurred_time).isoformat(sep=' '),
				'description': description,
				'amount': str(amount),
				'balance_after': str(balance_after),
				'operation_kind': _classify_operation(description),
			}
		)
	return rows


def _estimate_annual_rate_pct(transactions: list[dict]) -> Decimal:
	rates: list[Decimal] = []
	for row in transactions:
		if row.get('operation_kind') != 'capitalization':
			continue
		amount = _to_decimal(row.get('amount'))
		balance_after = _to_decimal(row.get('balance_after'))
		prior_balance = balance_after - amount
		if prior_balance <= 0:
			continue
		rates.append(amount / prior_balance * Decimal('12') * Decimal('100'))
	if not rates:
		return Decimal('0')
	return sum(rates, Decimal('0')) / Decimal(len(rates))


def _infer_income_schedule(transactions: list[dict]) -> str:
	capitalization_days = [
		datetime.fromisoformat(row['occurred_at']).day
		for row in transactions
		if row.get('operation_kind') == 'capitalization'
	]
	if capitalization_days and len(set(capitalization_days)) == 1:
		return Product.IncomeSchedule.MONTHLY
	return Product.IncomeSchedule.MONTHLY


def parse_belarusbank_deposit_statement(file_path: Path) -> ParseResult:
	text = _extract_pdf_text(file_path)
	if not is_belarusbank_deposit_statement_text(text):
		return ParseResult(warnings=['File does not look like a Belarusbank deposit statement.'])

	iban_match = re.search(r'IBAN:\s+(BY[A-Z0-9]+)', text, re.I)
	if not iban_match:
		return ParseResult(warnings=['Could not parse Belarusbank deposit IBAN.'])

	iban = iban_match.group(1)
	registration_match = re.search(r'Регистрационный номер приложения:\s+(\d+)', text, re.I)
	deposit_name_match = re.search(r'Название вклада:\s*(.+?)\s*IBAN:', text, re.I | re.S)
	transactions = _parse_transactions(text)
	if not transactions:
		return ParseResult(warnings=['Could not parse Belarusbank deposit transactions.'])

	opening_rows = [row for row in transactions if row.get('operation_kind') == 'opening']
	initial_amount = _to_decimal(opening_rows[0]['amount']) if opening_rows else Decimal('0')
	as_of_match = re.search(r'Сформирована:\s+(\d{2}\.\d{2}\.\d{4})', text, re.I)
	as_of_date = (
		datetime.strptime(as_of_match.group(1), '%d.%m.%Y').date()
		if as_of_match
		else (_parse_date_from_text(text, 'Дата последней операции по вкладу') or date.min)
	)
	statement = {
		'iban': iban,
		'registration_number': registration_match.group(1) if registration_match else '',
		'deposit_name': re.sub(r'\s+', ' ', deposit_name_match.group(1).strip()) if deposit_name_match else '',
		'opened_at': (_parse_date_from_text(text, 'Дата открытия/пролонгации') or date.min).isoformat(),
		'maturity_date': (_parse_date_from_text(text, 'Дата окончания') or date.min).isoformat(),
		'initial_amount_byn': str(initial_amount),
		'annual_rate_pct': str(_estimate_annual_rate_pct(transactions).quantize(Decimal('0.01'))),
		'balance_byn': str(_parse_decimal_from_text(text, 'Остаток на конец периода') or Decimal('0')),
		'as_of_date': as_of_date.isoformat(),
		'interest_mode': 'capitalized',
		'income_schedule': _infer_income_schedule(transactions),
		'transactions': transactions,
	}

	return ParseResult(
		records=[statement],
		metadata={
			'parser_variant': 'belarusbank-deposit-statement',
			'rows': len(transactions),
			'iban': iban,
			'as_of_date': statement['as_of_date'],
		},
		artifacts={'statement': statement},
	)


def ensure_belarusbank_bootstrap() -> dict:
	byn = Currency.objects.get(code='BYN')
	belarusbank, _ = FinancialInstitution.objects.update_or_create(
		slug=BELARUSBANK_SLUG,
		defaults={
			'name': 'Беларусбанк',
			'institution_type': FinancialInstitution.InstitutionType.BANK,
			'country': 'BY',
			'website': 'https://belarusbank.by/',
			'base_currency': byn,
			'metadata': {'bootstrap': True},
		},
	)
	byn_account, _ = Account.objects.get_or_create(
		institution=belarusbank,
		name=BELARUSBANK_BYN_ACCOUNT_NAME,
		defaults={
			'account_type': Account.AccountType.BANK,
			'currency': byn,
			'metadata': {'bootstrap': True},
		},
	)
	return {'belarusbank': belarusbank, 'byn_account': byn_account, 'byn': byn}


def _product_name_from_source(raw_import_file) -> str:
	if raw_import_file.source and isinstance(raw_import_file.source.config, dict):
		config_name = str(raw_import_file.source.config.get('product_name', '')).strip()
		if config_name:
			return config_name

	filename = (raw_import_file.original_filename or '').strip()
	stem = Path(filename).stem
	if stem:
		return stem
	return 'Belarusbank'


def _build_product_metadata(statement: dict) -> dict:
	return {
		'iban': statement.get('iban', ''),
		'registration_number': statement.get('registration_number', ''),
		'deposit_name': statement.get('deposit_name', ''),
		'opened_at': statement.get('opened_at', ''),
		'interest_mode': statement.get('interest_mode', 'capitalized'),
		'as_of_date': statement.get('as_of_date', ''),
		'initial_amount_byn': statement.get('initial_amount_byn', ''),
		'balance_byn': statement.get('balance_byn', ''),
		'imported_from': 'belarusbank-deposit-statement',
	}


@transaction.atomic
def persist_belarusbank_deposit_statement(raw_import_file, result: ParseResult) -> int:
	statement = result.artifacts.get('statement')
	if not statement:
		return 0

	bootstrap = ensure_belarusbank_bootstrap()
	belarusbank = bootstrap['belarusbank']
	byn_account = bootstrap['byn_account']
	byn = bootstrap['byn']

	iban = statement['iban']
	product_name = _product_name_from_source(raw_import_file)
	balance = _to_decimal(statement.get('balance_byn'))
	annual_rate = _to_decimal(statement.get('annual_rate_pct'))
	maturity_raw = statement.get('maturity_date', '')
	maturity_date = datetime.strptime(maturity_raw, '%Y-%m-%d').date() if maturity_raw else None
	as_of_raw = statement.get('as_of_date', '')
	as_of_date = datetime.strptime(as_of_raw, '%Y-%m-%d').date() if as_of_raw else timezone.localdate()
	as_of_dt = timezone.make_aware(datetime.combine(as_of_date, datetime.min.time()))
	income_schedule = statement.get('income_schedule') or Product.IncomeSchedule.MONTHLY

	product, _ = Product.objects.update_or_create(
		institution=belarusbank,
		external_id=iban,
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
	if annual_rate > 0:
		product.annual_rate_pct = annual_rate
	product.maturity_date = maturity_date
	product.income_schedule = income_schedule
	product.metadata = _build_product_metadata(statement)
	product.current_value_usd = _amount_usd(balance, byn, rate_date=as_of_date)
	product.terms_updated_at = timezone.now()
	update_fields = [
		'name',
		'product_type',
		'currency',
		'income_account',
		'units',
		'current_price',
		'is_active',
		'maturity_date',
		'income_schedule',
		'metadata',
		'current_value_usd',
		'terms_updated_at',
		'updated_at',
	]
	if annual_rate > 0:
		update_fields.insert(7, 'annual_rate_pct')
	product.save(update_fields=update_fields)

	next_income_date = estimate_next_income_date(product, today=as_of_date)
	if next_income_date:
		product.next_income_date = next_income_date
		product.save(update_fields=['next_income_date', 'updated_at'])

	BalanceSnapshot.objects.update_or_create(
		institution=belarusbank,
		product=product,
		captured_at=as_of_dt,
		defaults={
			'currency': byn,
			'balance': balance,
			'balance_usd': product.current_value_usd,
			'metadata': {
				'imported_from': 'belarusbank-deposit-statement',
				'iban': iban,
				'annual_rate_pct': statement.get('annual_rate_pct', ''),
			},
		},
	)

	records_created = 1
	for row in statement.get('transactions', []):
		amount = _to_decimal(row.get('amount'))
		if amount <= 0:
			continue
		occurred_at = timezone.make_aware(datetime.fromisoformat(row['occurred_at']))
		kind = row.get('operation_kind', 'other')
		description = row.get('description', '')
		fingerprint = _fingerprint(
			'belarusbank-deposit',
			iban,
			row.get('occurred_at', ''),
			row.get('amount', ''),
			kind,
		)

		if kind in {'opening', 'top_up'}:
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
					'description': description or f'Пополнение вклада {product_name}',
					'metadata': {
						'imported_from': 'belarusbank-deposit-statement',
						'operation_kind': kind,
						'balance_after': row.get('balance_after', ''),
						'exclude_from_account_balance': True,
					},
				},
			)
			if created:
				records_created += 1
			continue

		if kind == 'capitalization':
			_, created = Transaction.objects.update_or_create(
				import_fingerprint=fingerprint,
				defaults={
					'account': byn_account,
					'product': product,
					'import_job': raw_import_file.job,
					'transaction_type': Transaction.TransactionType.INCOME,
					'currency': byn,
					'amount': amount,
					'amount_usd': _amount_usd(amount, byn, rate_date=occurred_at.date()),
					'quantity': amount,
					'unit_price': Decimal('1'),
					'occurred_at': occurred_at,
					'description': description or f'Капитализация процентов {product_name}',
					'metadata': {
						'imported_from': 'belarusbank-deposit-statement',
						'operation_kind': kind,
						'interest_mode': 'capitalized',
						'balance_after': row.get('balance_after', ''),
						'exclude_from_account_balance': True,
					},
				},
			)
			if created:
				records_created += 1

	sync_account_balance(byn_account)
	return records_created
