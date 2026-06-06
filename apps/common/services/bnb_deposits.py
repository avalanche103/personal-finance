from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.accounts.services.balance import sync_account_balance
from apps.common.models import Currency
from apps.common.services.exchange_rates import get_usd_conversion_rate
from apps.imports.services.parsers.base import ParseResult
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product
from apps.products.services.token_terms import estimate_next_income_date

BNB_BANK_SLUG = 'bnb-bank'
BNB_BYN_ACCOUNT_NAME = 'БНБ-Банк BYN Account'

PRODUCT_NAMES_BY_CONTRACT = {
	'1112109330009211': 'BNB1',
	'1112449330000404': 'BNB2',
}

DECIMAL_TOKEN_PATTERN = re.compile(r'[\d, ]+(?:\.\d+)?')
TRANSACTION_LINE_PATTERN = re.compile(
	r'^(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})\s+(.+?)\s+'
	r'([+-][\d, ]+(?:\.\d+)?)\s+BYN\s+([\d, ]+(?:\.\d+)?)\s+BYN\s*$',
)


def _to_decimal(value) -> Decimal:
	if value in (None, ''):
		return Decimal('0')
	if isinstance(value, Decimal):
		return value
	normalized = str(value).strip().replace(' ', '').replace(',', '.')
	return Decimal(normalized)


def _parse_decimal_from_text(text: str, label: str) -> Decimal | None:
	match = re.search(rf'{re.escape(label)}\s*:?\s*({DECIMAL_TOKEN_PATTERN.pattern})', text, re.I)
	if not match:
		return None
	return _to_decimal(match.group(1))


def _parse_date_from_text(text: str, label: str) -> date | None:
	match = re.search(rf'{re.escape(label)}\s*:?\s*(\d{{2}}\.\d{{2}}\.\d{{4}})', text, re.I)
	if not match:
		return None
	return datetime.strptime(match.group(1), '%d.%m.%Y').date()


def _extract_pdf_text(file_path: Path) -> str:
	try:
		import pdfplumber
	except ImportError as exc:
		raise ImportError('pdfplumber is required for BNB deposit PDF parsing.') from exc

	chunks: list[str] = []
	with pdfplumber.open(file_path) as pdf:
		for page in pdf.pages:
			chunks.append(page.extract_text() or '')
	return '\n'.join(chunks)


def is_bnb_deposit_statement_text(text: str) -> bool:
	lowered = text.casefold()
	return (
		'бнб-банк' in lowered
		and 'выписка по вкладу' in lowered
		and 'номер договора' in lowered
	)


def _classify_operation(description: str) -> str:
	lowered = description.casefold()
	if 'открыт' in lowered:
		return 'opening'
	if 'капитализац' in lowered:
		return 'capitalization'
	if 'пополнен' in lowered:
		return 'interest_credit'
	return 'other'


def _parse_transactions(text: str) -> list[dict]:
	rows: list[dict] = []
	for line in text.splitlines():
		match = TRANSACTION_LINE_PATTERN.match(line.strip())
		if not match:
			continue
		occurred_date = datetime.strptime(match.group(1), '%d.%m.%Y').date()
		occurred_time = datetime.strptime(match.group(2), '%H:%M:%S').time()
		description = re.sub(r'\s+', ' ', match.group(3).strip())
		amount = _to_decimal(match.group(4))
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


def _infer_income_schedule(transactions: list[dict]) -> str:
	capitalization_days: list[int] = []
	for row in transactions:
		if row.get('operation_kind') != 'capitalization':
			continue
		occurred_at = datetime.fromisoformat(row['occurred_at'])
		capitalization_days.append(occurred_at.day)
	if capitalization_days:
		return Product.IncomeSchedule.MONTHLY
	return Product.IncomeSchedule.MONTHLY


def parse_bnb_deposit_statement(file_path: Path) -> ParseResult:
	text = _extract_pdf_text(file_path)
	if not is_bnb_deposit_statement_text(text):
		return ParseResult(warnings=['File does not look like a BNB Bank deposit statement.'])

	contract_match = re.search(r'Номер договора\s+(\d+)', text, re.I)
	if not contract_match:
		return ParseResult(warnings=['Could not parse BNB deposit contract number.'])

	contract_number = contract_match.group(1)
	iban_match = re.search(r'Номер сч[ёе]та\s+(BY\d+)', text, re.I)
	deposit_name_match = re.search(r'Наименование вклада\s+(.+)', text, re.I)
	transactions = _parse_transactions(text)
	statement = {
		'contract_number': contract_number,
		'iban': iban_match.group(1) if iban_match else '',
		'deposit_name': deposit_name_match.group(1).strip() if deposit_name_match else '',
		'opened_at': (_parse_date_from_text(text, 'Дата открытия') or date.min).isoformat(),
		'maturity_date': (_parse_date_from_text(text, 'Дата возврата') or date.min).isoformat(),
		'initial_amount_byn': str(_parse_decimal_from_text(text, 'Сумма первоначального взноса') or Decimal('0')),
		'annual_rate_pct': str(_parse_decimal_from_text(text, 'Текущая процентная ставка') or Decimal('0')),
		'balance_byn': str(_parse_decimal_from_text(text, 'Остаток на момент формирования выписки') or Decimal('0')),
		'as_of_date': (_parse_date_from_text(text, 'Дата формирования выписки') or date.min).isoformat(),
		'interest_mode': 'capitalized',
		'income_schedule': _infer_income_schedule(transactions),
		'transactions': transactions,
	}

	return ParseResult(
		records=[statement],
		metadata={
			'parser_variant': 'bnb-deposit-statement',
			'rows': len(transactions),
			'contract_number': contract_number,
			'as_of_date': statement['as_of_date'],
		},
		artifacts={'statement': statement},
	)


def _fingerprint(*parts: str) -> str:
	payload = ':'.join(parts)
	return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _amount_usd(amount: Decimal, currency: Currency, rate_date: date | None = None) -> Decimal:
	target_date = rate_date or timezone.localdate()
	rate = get_usd_conversion_rate(currency, target_date)
	return amount * rate


def ensure_bnb_bootstrap() -> dict:
	byn = Currency.objects.get(code='BYN')
	bnb_bank, _ = FinancialInstitution.objects.update_or_create(
		slug=BNB_BANK_SLUG,
		defaults={
			'name': 'БНБ-Банк',
			'institution_type': FinancialInstitution.InstitutionType.BANK,
			'country': 'BY',
			'website': 'https://bnb.by/',
			'base_currency': byn,
			'metadata': {'bootstrap': True},
		},
	)
	byn_account, _ = Account.objects.get_or_create(
		institution=bnb_bank,
		name=BNB_BYN_ACCOUNT_NAME,
		defaults={
			'account_type': Account.AccountType.BANK,
			'currency': byn,
			'metadata': {'bootstrap': True},
		},
	)
	return {'bnb_bank': bnb_bank, 'byn_account': byn_account, 'byn': byn}


def _product_name_from_source(raw_import_file, contract_number: str) -> str:
	if raw_import_file.source and isinstance(raw_import_file.source.config, dict):
		config_name = str(raw_import_file.source.config.get('product_name', '')).strip()
		if config_name:
			return config_name

	filename = (raw_import_file.original_filename or '').strip()
	stem = Path(filename).stem.upper()
	if stem in {'BNB1', 'BNB2'}:
		return stem

	return PRODUCT_NAMES_BY_CONTRACT.get(contract_number, f'BNB {contract_number[-4:]}')


def _build_product_metadata(statement: dict) -> dict:
	return {
		'contract_number': statement.get('contract_number', ''),
		'iban': statement.get('iban', ''),
		'deposit_name': statement.get('deposit_name', ''),
		'opened_at': statement.get('opened_at', ''),
		'interest_mode': statement.get('interest_mode', 'capitalized'),
		'as_of_date': statement.get('as_of_date', ''),
		'initial_amount_byn': statement.get('initial_amount_byn', ''),
		'balance_byn': statement.get('balance_byn', ''),
		'imported_from': 'bnb-deposit-statement',
	}


@transaction.atomic
def persist_bnb_deposit_statement(raw_import_file, result: ParseResult) -> int:
	statement = result.artifacts.get('statement')
	if not statement:
		return 0

	bootstrap = ensure_bnb_bootstrap()
	bnb_bank = bootstrap['bnb_bank']
	byn_account = bootstrap['byn_account']
	byn = bootstrap['byn']

	contract_number = statement['contract_number']
	product_name = _product_name_from_source(raw_import_file, contract_number)
	balance = _to_decimal(statement.get('balance_byn'))
	annual_rate = _to_decimal(statement.get('annual_rate_pct'))
	maturity_raw = statement.get('maturity_date', '')
	maturity_date = datetime.strptime(maturity_raw, '%Y-%m-%d').date() if maturity_raw else None
	as_of_raw = statement.get('as_of_date', '')
	as_of_date = datetime.strptime(as_of_raw, '%Y-%m-%d').date() if as_of_raw else timezone.localdate()
	as_of_dt = timezone.make_aware(datetime.combine(as_of_date, datetime.min.time()))
	income_schedule = statement.get('income_schedule') or Product.IncomeSchedule.MONTHLY

	product, _ = Product.objects.update_or_create(
		institution=bnb_bank,
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
	product.income_schedule = income_schedule
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

	next_income_date = estimate_next_income_date(product, today=as_of_date)
	if next_income_date:
		product.next_income_date = next_income_date
		product.save(update_fields=['next_income_date', 'updated_at'])

	BalanceSnapshot.objects.update_or_create(
		institution=bnb_bank,
		product=product,
		captured_at=as_of_dt,
		defaults={
			'currency': byn,
			'balance': balance,
			'balance_usd': product.current_value_usd,
			'metadata': {
				'imported_from': 'bnb-deposit-statement',
				'contract_number': contract_number,
				'annual_rate_pct': statement.get('annual_rate_pct', ''),
			},
		},
	)

	records_created = 1
	contract_number = statement['contract_number']
	for row in statement.get('transactions', []):
		amount = _to_decimal(row.get('amount'))
		if amount <= 0:
			continue
		occurred_at = timezone.make_aware(datetime.fromisoformat(row['occurred_at']))
		kind = row.get('operation_kind', 'other')
		description = row.get('description', '')
		fingerprint = _fingerprint(
			'bnb-deposit',
			contract_number,
			row.get('occurred_at', ''),
			row.get('amount', ''),
			kind,
		)

		if kind == 'opening':
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
					'description': description or f'Открытие вклада {product_name}',
					'metadata': {
						'imported_from': 'bnb-deposit-statement',
						'operation_kind': kind,
						'balance_after': row.get('balance_after', ''),
						'exclude_from_account_balance': True,
					},
				},
			)
			if created:
				records_created += 1
			continue

		if kind in {'capitalization', 'interest_credit'}:
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
						'imported_from': 'bnb-deposit-statement',
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
