from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.accounts.services.balance import sync_account_balance
from apps.common.models import Currency
from apps.imports.models import ImportJob, ImportSource
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product

MONEY_QUANT = Decimal('0.01')
UNIT_QUANT = Decimal('0.000001')
PRICE_QUANT = Decimal('0.00000001')


@dataclass(frozen=True)
class BynexTradeRow:
	occurred_at: datetime
	side: str
	base_asset: str
	quote_currency: str
	quantity: Decimal
	price: Decimal
	fee: Decimal
	total: Decimal | None = None
	external_id: str = ''


@dataclass(frozen=True)
class BynexTradeResult:
	transaction: Transaction
	product: Product
	account: Account
	created: bool
	exact_gross: Decimal
	exact_total: Decimal


@dataclass(frozen=True)
class BynexTransferRow:
	occurred_at: datetime
	asset: str
	quantity: Decimal
	fee: Decimal
	destination: str = ''
	external_id: str = ''


@dataclass(frozen=True)
class BynexTransferResult:
	transfer: Transaction
	fee_transaction: Transaction | None
	product: Product
	account: Account
	created: int


def _money(value: Decimal) -> Decimal:
	return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _units(value: Decimal) -> Decimal:
	return value.quantize(UNIT_QUANT, rounding=ROUND_HALF_UP)


def _price(value: Decimal) -> Decimal:
	return value.quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)


def _to_decimal(value) -> Decimal:
	if value in (None, ''):
		return Decimal('0')
	return Decimal(str(value).strip().replace(' ', '').replace(',', '.'))


def _ensure_currency(code: str, *, name: str = '', symbol: str = '', usd_rate: Decimal = Decimal('0')) -> Currency:
	normalized_code = code.strip().upper()
	defaults = {
		'name': name or normalized_code,
		'symbol': symbol or normalized_code,
		'usd_rate': usd_rate,
		'metadata': {'source': 'bynex', 'auto_provisioned': True},
	}
	currency, _ = Currency.objects.get_or_create(code=normalized_code, defaults=defaults)
	return currency


def ensure_bynex_reference_data() -> tuple[FinancialInstitution, ImportSource, Account]:
	usd = _ensure_currency('USD', name='US Dollar', symbol='$', usd_rate=Decimal('1'))
	institution, _ = FinancialInstitution.objects.update_or_create(
		slug='bynex',
		defaults={
			'name': 'BYNEX',
			'institution_type': FinancialInstitution.InstitutionType.CRYPTO_EXCHANGE,
			'country': 'BY',
			'base_currency': usd,
			'metadata': {'bootstrap': True, 'manual_trades': True},
		},
	)
	source, _ = ImportSource.objects.update_or_create(
		code='bynex-manual-trades',
		defaults={
			'institution': institution,
			'name': 'BYNEX Manual Trades',
			'source_type': ImportSource.SourceType.MANUAL,
			'is_active': True,
			'config': {'parser': 'bynex-manual-trades', 'bootstrap': True},
		},
	)
	account = (
		Account.objects.filter(institution=institution, external_id='bynex:wallet:USD').first()
		or Account.objects.filter(institution=institution, name='BYNEX USD Account').first()
	)
	if account is None:
		account = Account.objects.create(
			institution=institution,
			name='BYNEX USD Account',
			account_type=Account.AccountType.WALLET,
			currency=usd,
			external_id='bynex:wallet:USD',
			metadata={'source': 'bynex', 'wallet': 'main', 'asset': 'USD'},
		)
	else:
		account.account_type = Account.AccountType.WALLET
		account.currency = usd
		account.external_id = account.external_id or 'bynex:wallet:USD'
		metadata = dict(account.metadata or {})
		metadata.update({'source': 'bynex', 'wallet': 'main', 'asset': 'USD'})
		account.metadata = metadata
		account.save(update_fields=['account_type', 'currency', 'external_id', 'metadata', 'updated_at'])
	return institution, source, account


def _ensure_product(institution: FinancialInstitution, asset: str) -> Product:
	usd = _ensure_currency('USD', name='US Dollar', symbol='$', usd_rate=Decimal('1'))
	normalized_asset = asset.strip().upper()
	product, _ = Product.objects.update_or_create(
		institution=institution,
		external_id=f'bynex:spot:{normalized_asset}',
		defaults={
			'name': f'BYNEX {normalized_asset} Spot',
			'symbol': normalized_asset,
			'product_type': Product.ProductType.CRYPTO,
			'currency': usd,
			'metadata': {'source': 'bynex', 'asset': normalized_asset, 'product_area': 'spot'},
			'is_active': True,
		},
	)
	return product


def _trade_fingerprint(row: BynexTradeRow, exact_total: Decimal) -> str:
	if row.external_id:
		return f'bynex:trade:{row.external_id}'
	payload = ':'.join(
		[
			row.occurred_at.isoformat(),
			row.side,
			row.base_asset,
			row.quote_currency,
			str(row.quantity),
			str(row.price),
			str(row.fee),
			str(exact_total),
		]
	)
	return f'bynex:trade:{hashlib.sha256(payload.encode("utf-8")).hexdigest()}'


def _transfer_fingerprint(row: BynexTransferRow, suffix: str = '') -> str:
	if row.external_id:
		base = f'bynex:transfer:{row.external_id}'
	else:
		payload = ':'.join(
			[
				row.occurred_at.isoformat(),
				row.asset,
				str(row.quantity),
				str(row.fee),
				row.destination,
			]
		)
		base = f'bynex:transfer:{hashlib.sha256(payload.encode("utf-8")).hexdigest()}'
	return f'{base}:{suffix}' if suffix else base


def _parse_occurred_at(raw_value: str) -> datetime:
	value = (raw_value or '').strip()
	if not value:
		return timezone.now()
	parsed_date = None
	for date_format in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d.%m.%Y %H:%M:%S', '%d.%m.%Y'):
		try:
			parsed_date = datetime.strptime(value, date_format)
			break
		except ValueError:
			continue
	if parsed_date is None:
		parsed_date = datetime.fromisoformat(value)
	if parsed_date.tzinfo is None:
		parsed_date = timezone.make_aware(parsed_date, timezone.get_current_timezone())
	if parsed_date.time() == time.min and len(value) <= 10:
		parsed_date = parsed_date.replace(hour=12)
	return parsed_date


def build_trade_row(
	*,
	occurred_at: str | datetime | None = None,
	side: str = 'buy',
	base_asset: str = 'USDT',
	quote_currency: str = 'USD',
	quantity,
	price,
	fee=Decimal('0'),
	total=None,
	external_id: str = '',
) -> BynexTradeRow:
	parsed_at = occurred_at if isinstance(occurred_at, datetime) else _parse_occurred_at(occurred_at or '')
	return BynexTradeRow(
		occurred_at=parsed_at,
		side=(side or 'buy').strip().lower(),
		base_asset=(base_asset or 'USDT').strip().upper(),
		quote_currency=(quote_currency or 'USD').strip().upper(),
		quantity=_to_decimal(quantity),
		price=_to_decimal(price),
		fee=_to_decimal(fee),
		total=_to_decimal(total) if total not in (None, '') else None,
		external_id=(external_id or '').strip(),
	)


def load_bynex_trade_rows(path: str | Path) -> list[BynexTradeRow]:
	rows: list[BynexTradeRow] = []
	with Path(path).open('r', encoding='utf-8-sig', newline='') as handle:
		for raw_row in csv.DictReader(handle):
			if not any((value or '').strip() for value in raw_row.values()):
				continue
			rows.append(
				build_trade_row(
					occurred_at=raw_row.get('occurred_at'),
					side=raw_row.get('side') or 'buy',
					base_asset=raw_row.get('base_asset') or 'USDT',
					quote_currency=raw_row.get('quote_currency') or 'USD',
					quantity=raw_row.get('quantity'),
					price=raw_row.get('price'),
					fee=raw_row.get('fee') or '0',
					total=raw_row.get('total'),
					external_id=raw_row.get('external_id') or '',
				)
			)
	return rows


def build_transfer_row(
	*,
	occurred_at: str | datetime | None = None,
	asset: str = 'USDT',
	quantity,
	fee=Decimal('0'),
	destination: str = '',
	external_id: str = '',
) -> BynexTransferRow:
	parsed_at = occurred_at if isinstance(occurred_at, datetime) else _parse_occurred_at(occurred_at or '')
	return BynexTransferRow(
		occurred_at=parsed_at,
		asset=(asset or 'USDT').strip().upper(),
		quantity=_to_decimal(quantity),
		fee=_to_decimal(fee),
		destination=(destination or '').strip(),
		external_id=(external_id or '').strip(),
	)


def load_bynex_transfer_rows(path: str | Path) -> list[BynexTransferRow]:
	rows: list[BynexTransferRow] = []
	with Path(path).open('r', encoding='utf-8-sig', newline='') as handle:
		for raw_row in csv.DictReader(handle):
			if not any((value or '').strip() for value in raw_row.values()):
				continue
			rows.append(
				build_transfer_row(
					occurred_at=raw_row.get('occurred_at'),
					asset=raw_row.get('asset') or 'USDT',
					quantity=raw_row.get('quantity'),
					fee=raw_row.get('fee') or '0',
					destination=raw_row.get('destination') or '',
					external_id=raw_row.get('external_id') or '',
				)
			)
	return rows


def _refresh_product_from_transactions(product: Product, *, current_price: Decimal | None = None) -> None:
	product.units = _units(
		sum(
			(tx.quantity or Decimal('0'))
			for tx in Transaction.objects.filter(product=product)
		)
	)
	if current_price is not None:
		product.current_price = _price(current_price)
	product.current_value_usd = _money(product.units * (product.current_price or Decimal('0')))
	product.is_active = product.units > 0
	product.save(update_fields=['units', 'current_price', 'current_value_usd', 'is_active', 'updated_at'])


def record_bynex_trade(row: BynexTradeRow) -> BynexTradeResult:
	if row.side not in {'buy', 'sell'}:
		raise ValueError('BYNEX trade side must be "buy" or "sell".')
	if row.quote_currency != 'USD':
		raise ValueError('BYNEX trade importer currently supports USD-quoted trades only.')
	if row.quantity <= 0:
		raise ValueError('BYNEX trade quantity must be positive.')
	if row.price <= 0:
		raise ValueError('BYNEX trade price must be positive.')
	if row.fee < 0:
		raise ValueError('BYNEX trade fee cannot be negative.')

	institution, source, account = ensure_bynex_reference_data()
	product = _ensure_product(institution, row.base_asset)
	exact_gross = row.quantity * row.price
	exact_total = row.total if row.total is not None else exact_gross + row.fee
	if row.side == 'sell' and row.total is None:
		exact_total = exact_gross - row.fee
	quantity = _units(row.quantity if row.side == 'buy' else -row.quantity)
	ledger_amount = _money(-exact_total if row.side == 'buy' else exact_total)
	fingerprint = _trade_fingerprint(row, exact_total)

	with transaction.atomic():
		job, _ = ImportJob.objects.get_or_create(
			source=source,
			idempotency_key=fingerprint,
			defaults={
				'institution': institution,
				'status': ImportJob.Status.SAVED,
				'file_type': 'manual',
				'parser_name': 'bynex-manual-trades',
				'original_filename': 'manual-bynex-trade',
				'rows_detected': 1,
				'records_created': 1,
				'started_at': timezone.now(),
				'finished_at': timezone.now(),
				'details': {
					'scope': 'bynex-manual-trade',
					'base_asset': row.base_asset,
					'side': row.side,
					'exact_total': str(exact_total),
				},
			},
		)
		trade, created = Transaction.objects.update_or_create(
			import_fingerprint=fingerprint,
			defaults={
				'account': account,
				'product': product,
				'import_job': job,
				'transaction_type': Transaction.TransactionType.TRADE,
				'currency': account.currency,
				'external_id': row.external_id,
				'amount': ledger_amount,
				'amount_usd': ledger_amount,
				'quantity': quantity,
				'unit_price': _price(row.price),
				'occurred_at': row.occurred_at,
				'description': f'BYNEX {row.base_asset}{row.quote_currency} {row.side}',
				'metadata': {
					'source': 'bynex',
					'symbol': f'{row.base_asset}{row.quote_currency}',
					'side': row.side,
					'gross_amount_exact': str(exact_gross),
					'fee_amount_exact': str(row.fee),
					'total_amount_exact': str(exact_total),
					'ledger_amount_rounded': str(ledger_amount),
				},
			},
		)
		_refresh_product_from_transactions(product, current_price=row.price)
		sync_account_balance(account)
		job.records_created = 1 if created else 0
		job.finished_at = timezone.now()
		job.save(update_fields=['records_created', 'finished_at', 'updated_at'])
		account.refresh_from_db()
		product.refresh_from_db()
	return BynexTradeResult(
		transaction=trade,
		product=product,
		account=account,
		created=created,
		exact_gross=exact_gross,
		exact_total=exact_total,
	)


def record_bynex_transfer(row: BynexTransferRow) -> BynexTransferResult:
	if row.quantity <= 0:
		raise ValueError('BYNEX transfer quantity must be positive.')
	if row.fee < 0:
		raise ValueError('BYNEX transfer fee cannot be negative.')

	institution, source, account = ensure_bynex_reference_data()
	product = _ensure_product(institution, row.asset)
	fingerprint = _transfer_fingerprint(row)
	fee_fingerprint = _transfer_fingerprint(row, 'fee')

	with transaction.atomic():
		job, _ = ImportJob.objects.get_or_create(
			source=source,
			idempotency_key=fingerprint,
			defaults={
				'institution': institution,
				'status': ImportJob.Status.SAVED,
				'file_type': 'manual',
				'parser_name': 'bynex-manual-transfers',
				'original_filename': 'manual-bynex-transfer',
				'rows_detected': 1,
				'records_created': 1,
				'started_at': timezone.now(),
				'finished_at': timezone.now(),
				'details': {
					'scope': 'bynex-manual-transfer',
					'asset': row.asset,
					'quantity': str(row.quantity),
					'fee': str(row.fee),
					'destination': row.destination,
				},
			},
		)
		transfer, transfer_created = Transaction.objects.update_or_create(
			import_fingerprint=fingerprint,
			defaults={
				'account': account,
				'product': product,
				'import_job': job,
				'transaction_type': Transaction.TransactionType.TRANSFER,
				'currency': account.currency,
				'external_id': row.external_id,
				'amount': Decimal('0'),
				'amount_usd': Decimal('0'),
				'quantity': _units(-row.quantity),
				'unit_price': product.current_price or Decimal('0'),
				'occurred_at': row.occurred_at,
				'description': f'BYNEX {row.asset} transfer to {row.destination or "external wallet"}',
				'metadata': {
					'source': 'bynex',
					'asset': row.asset,
					'destination': row.destination,
					'exclude_from_account_balance': True,
				},
			},
		)
		fee_transaction = None
		fee_created = False
		if row.fee:
			fee_transaction, fee_created = Transaction.objects.update_or_create(
				import_fingerprint=fee_fingerprint,
				defaults={
					'account': account,
					'product': product,
					'import_job': job,
					'transaction_type': Transaction.TransactionType.FEE,
					'currency': account.currency,
					'external_id': f'{row.external_id}:fee' if row.external_id else '',
					'amount': Decimal('0'),
					'amount_usd': Decimal('0'),
					'quantity': _units(-row.fee),
					'unit_price': product.current_price or Decimal('0'),
					'occurred_at': row.occurred_at,
					'description': f'BYNEX {row.asset} transfer fee',
					'metadata': {
						'source': 'bynex',
						'asset': row.asset,
						'destination': row.destination,
						'fee_for': fingerprint,
						'exclude_from_account_balance': True,
					},
				},
			)
		_refresh_product_from_transactions(product)
		sync_account_balance(account)
		created = int(transfer_created) + int(fee_created)
		job.records_created = created
		job.finished_at = timezone.now()
		job.save(update_fields=['records_created', 'finished_at', 'updated_at'])
		account.refresh_from_db()
		product.refresh_from_db()
	return BynexTransferResult(
		transfer=transfer,
		fee_transaction=fee_transaction,
		product=product,
		account=account,
		created=created,
	)
