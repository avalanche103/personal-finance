from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.accounts.services.balance import sync_account_balance
from apps.common.services.exchange_rates import get_usd_conversion_rate
from apps.products.models import Product


def _amount_to_usd(currency, amount: Decimal, as_of_date) -> Decimal:
	rate = get_usd_conversion_rate(currency, as_of_date)
	return ((amount or Decimal('0')) * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def create_account(
	*,
	institution,
	name: str,
	account_type: str,
	currency,
	external_id: str = '',
	current_balance: Decimal = Decimal('0'),
	metadata: dict | None = None,
	is_active: bool = True,
) -> Account:
	current_balance = current_balance or Decimal('0')
	account = Account(
		institution=institution,
		name=name,
		account_type=account_type,
		currency=currency,
		external_id=external_id or '',
		current_balance=current_balance,
		current_balance_usd=_amount_to_usd(currency, current_balance, timezone.localdate()),
		metadata=metadata or {},
		is_active=is_active,
	)
	account.full_clean()
	account.save()
	return account


def create_product(
	*,
	institution,
	name: str,
	product_type: str,
	currency,
	income_account=None,
	symbol: str = '',
	isin: str = '',
	units: Decimal = Decimal('0'),
	current_price: Decimal = Decimal('0'),
	external_id: str = '',
	metadata: dict | None = None,
	is_active: bool = True,
	annual_rate_pct: Decimal | None = None,
	maturity_date=None,
	income_schedule: str = '',
	next_income_date=None,
) -> Product:
	units = units or Decimal('0')
	current_price = current_price or Decimal('0')
	market_value = units * current_price
	product = Product(
		institution=institution,
		income_account=income_account,
		name=name,
		symbol=symbol or '',
		isin=isin or '',
		product_type=product_type,
		currency=currency,
		units=units,
		current_price=current_price,
		current_value_usd=_amount_to_usd(currency, market_value, timezone.localdate()),
		external_id=external_id or '',
		metadata=metadata or {},
		is_active=is_active,
		annual_rate_pct=annual_rate_pct,
		maturity_date=maturity_date,
		income_schedule=income_schedule or '',
		next_income_date=next_income_date,
		terms_updated_at=timezone.now() if any([annual_rate_pct, maturity_date, income_schedule, next_income_date]) else None,
	)
	product.full_clean()
	product.save()
	return product


def create_transaction(
	*,
	account: Account,
	transaction_type: str,
	currency,
	amount: Decimal,
	occurred_at,
	related_account: Account | None = None,
	product: Product | None = None,
	external_id: str = '',
	import_fingerprint: str = '',
	quantity: Decimal = Decimal('0'),
	unit_price: Decimal = Decimal('0'),
	description: str = '',
	metadata: dict | None = None,
	sync_balance: bool = True,
) -> Transaction:
	if timezone.is_naive(occurred_at):
		occurred_at = timezone.make_aware(occurred_at, timezone.get_current_timezone())
	amount = amount or Decimal('0')
	with transaction.atomic():
		ledger_transaction = Transaction(
			account=account,
			related_account=related_account,
			product=product,
			transaction_type=transaction_type,
			currency=currency,
			external_id=external_id or '',
			import_fingerprint=import_fingerprint or f'manual:{uuid4()}',
			amount=amount,
			amount_usd=_amount_to_usd(currency, amount, occurred_at.date()),
			quantity=quantity or Decimal('0'),
			unit_price=unit_price or Decimal('0'),
			occurred_at=occurred_at,
			description=description or '',
			metadata=metadata or {},
		)
		ledger_transaction.full_clean()
		ledger_transaction.save()
		if sync_balance:
			sync_account_balance(account)
	return ledger_transaction
