from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.accounts.services.balance import sync_account_balance
from apps.common.services.exchange_rates import get_usd_conversion_rate
from apps.products.models import Product

TRANSFER_PAIR_METADATA_KEY = 'transfer_pair_id'
TRANSFER_LEG_METADATA_KEY = 'transfer_leg'


def _amount_to_usd(currency, amount: Decimal, as_of_date) -> Decimal:
	rate = get_usd_conversion_rate(currency, as_of_date)
	return ((amount or Decimal('0')) * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _is_capitalized_deposit_income(product: Product, metadata: dict) -> bool:
	interest_mode = str(metadata.get('interest_mode', '') or '').strip().casefold()
	if interest_mode == 'capitalized':
		return True
	product_metadata = product.metadata if isinstance(product.metadata, dict) else {}
	return str(product_metadata.get('interest_mode', '') or '').strip().casefold() == 'capitalized'


def _normalize_deposit_product_transaction(
	*,
	account: Account,
	product: Product | None,
	transaction_type: str,
	amount: Decimal,
	quantity: Decimal,
	metadata: dict | None,
) -> tuple[Decimal, Decimal, dict]:
	metadata = dict(metadata or {})
	if product is None or product.product_type != Product.ProductType.DEPOSIT:
		return amount, quantity, metadata

	if transaction_type == Transaction.TransactionType.DEPOSIT:
		magnitude = abs(amount or Decimal('0'))
		if magnitude <= 0:
			raise ValueError('Deposit amount must be non-zero.')
		if product.income_account_id and account.pk != product.income_account_id:
			raise ValueError('Deposit must be recorded on the linked income account for this product.')
		if metadata.get('exclude_from_account_balance'):
			quantity = magnitude if not quantity else abs(quantity)
			metadata.setdefault('operation_kind', 'opening')
			return magnitude, quantity, metadata

		quantity = magnitude
		metadata.setdefault('operation_kind', 'top_up')
		product_metadata = product.metadata if isinstance(product.metadata, dict) else {}
		if product_metadata.get('interest_mode'):
			metadata.setdefault('interest_mode', product_metadata['interest_mode'])
		return -magnitude, quantity, metadata

	if transaction_type == Transaction.TransactionType.INCOME and _is_capitalized_deposit_income(product, metadata):
		magnitude = abs(amount or Decimal('0'))
		if magnitude <= 0:
			raise ValueError('Capitalized deposit income must be non-zero.')
		if product.income_account_id and account.pk != product.income_account_id:
			raise ValueError('Capitalized deposit income must be recorded on the linked income account.')
		quantity = magnitude if not quantity else abs(quantity)
		metadata.setdefault('operation_kind', 'capitalization')
		metadata['exclude_from_account_balance'] = True
		metadata.setdefault('interest_mode', 'capitalized')
		return magnitude, quantity, metadata

	return amount, quantity, metadata


def _deposit_units_from_transactions(product: Product) -> Decimal:
	units = Decimal('0')
	for ledger_transaction in Transaction.objects.filter(product=product).order_by('occurred_at', 'id'):
		quantity = ledger_transaction.quantity or Decimal('0')
		if quantity:
			units += quantity
			continue
		amount = ledger_transaction.amount or Decimal('0')
		if ledger_transaction.transaction_type == Transaction.TransactionType.DEPOSIT:
			units += abs(amount)
		elif ledger_transaction.transaction_type == Transaction.TransactionType.INCOME:
			metadata = ledger_transaction.metadata if isinstance(ledger_transaction.metadata, dict) else {}
			if _is_capitalized_deposit_income(product, metadata):
				units += abs(amount)
	return max(units, Decimal('0'))


def refresh_deposit_product_from_transactions(product: Product, *, save: bool = True) -> bool:
	if product.product_type != Product.ProductType.DEPOSIT:
		return False

	units = _deposit_units_from_transactions(product)
	changed = False
	update_fields = ['updated_at']
	if product.units != units:
		product.units = units
		update_fields.append('units')
	if product.current_price != Decimal('1'):
		product.current_price = Decimal('1')
		update_fields.append('current_price')
	current_value_usd = _amount_to_usd(product.currency, units, timezone.localdate())
	if product.current_value_usd != current_value_usd:
		product.current_value_usd = current_value_usd
		update_fields.append('current_value_usd')
	if len(update_fields) == 1:
		return False
	if save:
		product.save(update_fields=update_fields)
	return True


def repair_manual_deposit_top_up(ledger_transaction: Transaction, *, sync_balance: bool = True) -> Transaction:
	product = ledger_transaction.product
	if (
		product is None
		or product.product_type != Product.ProductType.DEPOSIT
		or ledger_transaction.transaction_type != Transaction.TransactionType.DEPOSIT
	):
		return ledger_transaction
	if ledger_transaction.metadata.get('exclude_from_account_balance'):
		return ledger_transaction

	amount, quantity, metadata = _normalize_deposit_product_transaction(
		account=ledger_transaction.account,
		product=product,
		transaction_type=ledger_transaction.transaction_type,
		amount=ledger_transaction.amount,
		quantity=ledger_transaction.quantity,
		metadata=ledger_transaction.metadata if isinstance(ledger_transaction.metadata, dict) else {},
	)
	ledger_transaction.amount = amount
	ledger_transaction.amount_usd = _amount_to_usd(ledger_transaction.currency, amount, ledger_transaction.occurred_at.date())
	ledger_transaction.quantity = quantity
	ledger_transaction.unit_price = Decimal('1')
	ledger_transaction.metadata = metadata
	ledger_transaction.save(
		update_fields=['amount', 'amount_usd', 'quantity', 'unit_price', 'metadata', 'updated_at'],
	)
	refresh_deposit_product_from_transactions(product)
	if sync_balance:
		sync_account_balance(ledger_transaction.account)
	return ledger_transaction


def _transfer_magnitude(amount: Decimal) -> Decimal:
	return abs(amount or Decimal('0'))


def _transfer_pair_id(metadata: dict | None) -> str:
	if not isinstance(metadata, dict):
		return ''
	return str(metadata.get(TRANSFER_PAIR_METADATA_KEY, '') or '').strip()


def _transfer_leg(metadata: dict | None) -> str:
	if not isinstance(metadata, dict):
		return ''
	return str(metadata.get(TRANSFER_LEG_METADATA_KEY, '') or '').strip()


def _find_transfer_counterpart(ledger_transaction: Transaction) -> Transaction | None:
	pair_id = _transfer_pair_id(ledger_transaction.metadata)
	if not pair_id:
		return None
	leg = _transfer_leg(ledger_transaction.metadata)
	other_leg = 'in' if leg == 'out' else 'out'
	return (
		Transaction.objects.filter(
			metadata__contains={TRANSFER_PAIR_METADATA_KEY: pair_id, TRANSFER_LEG_METADATA_KEY: other_leg},
		)
		.exclude(pk=ledger_transaction.pk)
		.first()
	)


def _build_transfer_leg_metadata(
	*,
	pair_id: str,
	leg: str,
	counterpart_account: Account,
	base_metadata: dict | None = None,
) -> dict:
	metadata = dict(base_metadata or {})
	metadata[TRANSFER_PAIR_METADATA_KEY] = pair_id
	metadata[TRANSFER_LEG_METADATA_KEY] = leg
	metadata['transfer_counterpart_account_id'] = counterpart_account.pk
	return metadata


def _validate_transfer_accounts(source_account: Account, destination_account: Account, currency) -> None:
	if source_account.pk == destination_account.pk:
		raise ValueError('Transfer source and destination accounts must be different.')
	if source_account.currency_id != destination_account.currency_id:
		raise ValueError('Transfer accounts must use the same currency.')
	if currency and currency.pk not in {source_account.currency_id, destination_account.currency_id}:
		raise ValueError('Transfer currency must match both accounts.')


def _create_transfer_pair(
	*,
	source_account: Account,
	destination_account: Account,
	currency,
	amount: Decimal,
	occurred_at,
	product: Product | None = None,
	external_id: str = '',
	import_fingerprint: str = '',
	quantity: Decimal = Decimal('0'),
	unit_price: Decimal = Decimal('0'),
	description: str = '',
	metadata: dict | None = None,
	sync_balance: bool = True,
) -> Transaction:
	_validate_transfer_accounts(source_account, destination_account, currency)
	magnitude = _transfer_magnitude(amount)
	if magnitude <= 0:
		raise ValueError('Transfer amount must be non-zero.')

	pair_id = str(uuid4())
	base_metadata = dict(metadata or {})
	out_fingerprint = import_fingerprint or f'manual:{pair_id}:out'
	in_fingerprint = f'manual:{pair_id}:in'
	occurred_date = occurred_at.date() if hasattr(occurred_at, 'date') else occurred_at
	out_description = description or f'Transfer to {destination_account.name}'
	in_description = description or f'Transfer from {source_account.name}'

	out_transaction = Transaction(
		account=source_account,
		related_account=destination_account,
		product=product,
		transaction_type=Transaction.TransactionType.TRANSFER,
		currency=currency,
		external_id=external_id or '',
		import_fingerprint=out_fingerprint,
		amount=-magnitude,
		amount_usd=_amount_to_usd(currency, -magnitude, occurred_date),
		quantity=quantity or Decimal('0'),
		unit_price=unit_price or Decimal('0'),
		occurred_at=occurred_at,
		description=out_description,
		metadata=_build_transfer_leg_metadata(
			pair_id=pair_id,
			leg='out',
			counterpart_account=destination_account,
			base_metadata=base_metadata,
		),
	)
	in_transaction = Transaction(
		account=destination_account,
		related_account=source_account,
		product=product,
		transaction_type=Transaction.TransactionType.TRANSFER,
		currency=currency,
		external_id=external_id or '',
		import_fingerprint=in_fingerprint,
		amount=magnitude,
		amount_usd=_amount_to_usd(currency, magnitude, occurred_date),
		quantity=quantity or Decimal('0'),
		unit_price=unit_price or Decimal('0'),
		occurred_at=occurred_at,
		description=in_description,
		metadata=_build_transfer_leg_metadata(
			pair_id=pair_id,
			leg='in',
			counterpart_account=source_account,
			base_metadata=base_metadata,
		),
	)
	out_transaction.full_clean()
	in_transaction.full_clean()
	out_transaction.save()
	in_transaction.save()
	if sync_balance:
		sync_account_balance(source_account)
		sync_account_balance(destination_account)
	return out_transaction


def _update_transfer_pair(
	ledger_transaction: Transaction,
	*,
	source_account: Account,
	destination_account: Account,
	currency,
	amount: Decimal,
	occurred_at,
	product: Product | None = None,
	external_id: str = '',
	quantity: Decimal = Decimal('0'),
	unit_price: Decimal = Decimal('0'),
	description: str = '',
	metadata: dict | None = None,
	sync_balance: bool = True,
) -> Transaction:
	_validate_transfer_accounts(source_account, destination_account, currency)
	counterpart = _find_transfer_counterpart(ledger_transaction)
	magnitude = _transfer_magnitude(amount)
	if magnitude <= 0:
		raise ValueError('Transfer amount must be non-zero.')

	occurred_date = occurred_at.date() if hasattr(occurred_at, 'date') else occurred_at
	pair_id = _transfer_pair_id(ledger_transaction.metadata) or str(uuid4())
	base_metadata = dict(metadata or {})
	out_description = description or f'Transfer to {destination_account.name}'
	in_description = description or f'Transfer from {source_account.name}'

	old_accounts = {ledger_transaction.account_id}
	if counterpart is not None:
		old_accounts.add(counterpart.account_id)

	if counterpart is None:
		out_transaction = ledger_transaction
		out_transaction.account = source_account
		out_transaction.related_account = destination_account
		out_transaction.product = product
		out_transaction.transaction_type = Transaction.TransactionType.TRANSFER
		out_transaction.currency = currency
		out_transaction.external_id = external_id or ''
		out_transaction.amount = -magnitude
		out_transaction.amount_usd = _amount_to_usd(currency, -magnitude, occurred_date)
		out_transaction.quantity = quantity or Decimal('0')
		out_transaction.unit_price = unit_price or Decimal('0')
		out_transaction.occurred_at = occurred_at
		out_transaction.description = out_description
		out_transaction.metadata = _build_transfer_leg_metadata(
			pair_id=pair_id,
			leg='out',
			counterpart_account=destination_account,
			base_metadata=base_metadata,
		)
		if not out_transaction.import_fingerprint:
			out_transaction.import_fingerprint = f'manual:{pair_id}:out'
		out_transaction.full_clean()
		out_transaction.save()

		in_transaction = Transaction(
			account=destination_account,
			related_account=source_account,
			product=product,
			transaction_type=Transaction.TransactionType.TRANSFER,
			currency=currency,
			external_id=external_id or '',
			import_fingerprint=f'manual:{pair_id}:in',
			amount=magnitude,
			amount_usd=_amount_to_usd(currency, magnitude, occurred_date),
			quantity=quantity or Decimal('0'),
			unit_price=unit_price or Decimal('0'),
			occurred_at=occurred_at,
			description=in_description,
			metadata=_build_transfer_leg_metadata(
				pair_id=pair_id,
				leg='in',
				counterpart_account=source_account,
				base_metadata=base_metadata,
			),
		)
		in_transaction.full_clean()
		in_transaction.save()
		if sync_balance:
			sync_account_balance(source_account)
			sync_account_balance(destination_account)
		return out_transaction

	out_transaction = ledger_transaction if _transfer_leg(ledger_transaction.metadata) == 'out' else counterpart
	in_transaction = counterpart if out_transaction is ledger_transaction else ledger_transaction

	for transaction_row, account, related_account, signed_amount, leg, leg_description in (
		(out_transaction, source_account, destination_account, -magnitude, 'out', out_description),
		(in_transaction, destination_account, source_account, magnitude, 'in', in_description),
	):
		transaction_row.account = account
		transaction_row.related_account = related_account
		transaction_row.product = product
		transaction_row.transaction_type = Transaction.TransactionType.TRANSFER
		transaction_row.currency = currency
		transaction_row.external_id = external_id or ''
		transaction_row.amount = signed_amount
		transaction_row.amount_usd = _amount_to_usd(currency, signed_amount, occurred_date)
		transaction_row.quantity = quantity or Decimal('0')
		transaction_row.unit_price = unit_price or Decimal('0')
		transaction_row.occurred_at = occurred_at
		transaction_row.description = leg_description
		transaction_row.metadata = _build_transfer_leg_metadata(
			pair_id=pair_id,
			leg=leg,
			counterpart_account=related_account,
			base_metadata=base_metadata,
		)
		transaction_row.full_clean()
		transaction_row.save()

	if sync_balance:
		accounts_to_sync = {source_account, destination_account}
		for account_id in old_accounts:
			if account_id:
				accounts_to_sync.add(Account.objects.get(pk=account_id))
		for account in accounts_to_sync:
			sync_account_balance(account)
	return out_transaction


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
	amount, quantity, metadata = _normalize_deposit_product_transaction(
		account=account,
		product=product,
		transaction_type=transaction_type,
		amount=amount,
		quantity=quantity,
		metadata=metadata,
	)
	if transaction_type == Transaction.TransactionType.TRANSFER and related_account is not None:
		return _create_transfer_pair(
			source_account=account,
			destination_account=related_account,
			currency=currency,
			amount=amount,
			occurred_at=occurred_at,
			product=product,
			external_id=external_id,
			import_fingerprint=import_fingerprint,
			quantity=quantity,
			unit_price=unit_price,
			description=description,
			metadata=metadata,
			sync_balance=sync_balance,
		)
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
		if product is not None and product.product_type == Product.ProductType.DEPOSIT:
			refresh_deposit_product_from_transactions(product)
		if sync_balance:
			sync_account_balance(account)
	return ledger_transaction


def update_transaction(
	ledger_transaction: Transaction,
	*,
	account: Account,
	transaction_type: str,
	currency,
	amount: Decimal,
	occurred_at,
	related_account: Account | None = None,
	product: Product | None = None,
	external_id: str = '',
	quantity: Decimal = Decimal('0'),
	unit_price: Decimal = Decimal('0'),
	description: str = '',
	metadata: dict | None = None,
	sync_balance: bool = True,
) -> Transaction:
	if timezone.is_naive(occurred_at):
		occurred_at = timezone.make_aware(occurred_at, timezone.get_current_timezone())
	amount = amount or Decimal('0')
	old_product = ledger_transaction.product
	amount, quantity, metadata = _normalize_deposit_product_transaction(
		account=account,
		product=product,
		transaction_type=transaction_type,
		amount=amount,
		quantity=quantity,
		metadata=metadata,
	)
	old_account = Transaction.objects.select_related('account').get(pk=ledger_transaction.pk).account
	counterpart = _find_transfer_counterpart(ledger_transaction)
	if transaction_type == Transaction.TransactionType.TRANSFER and related_account is not None:
		return _update_transfer_pair(
			ledger_transaction,
			source_account=account,
			destination_account=related_account,
			currency=currency,
			amount=amount,
			occurred_at=occurred_at,
			product=product,
			external_id=external_id,
			quantity=quantity,
			unit_price=unit_price,
			description=description,
			metadata=metadata,
			sync_balance=sync_balance,
		)
	if counterpart is not None:
		with transaction.atomic():
			counterpart_account = counterpart.account
			counterpart.delete()
			if sync_balance:
				sync_account_balance(counterpart_account)
	with transaction.atomic():
		ledger_transaction.account = account
		ledger_transaction.related_account = related_account
		ledger_transaction.product = product
		ledger_transaction.transaction_type = transaction_type
		ledger_transaction.currency = currency
		ledger_transaction.external_id = external_id or ''
		ledger_transaction.amount = amount
		ledger_transaction.amount_usd = _amount_to_usd(currency, amount, occurred_at.date())
		ledger_transaction.quantity = quantity or Decimal('0')
		ledger_transaction.unit_price = unit_price or Decimal('0')
		ledger_transaction.occurred_at = occurred_at
		ledger_transaction.description = description or ''
		ledger_transaction.metadata = metadata or {}
		ledger_transaction.full_clean()
		ledger_transaction.save()
		products_to_refresh = {
			product.pk
			for product in (product, old_product)
			if product is not None and product.product_type == Product.ProductType.DEPOSIT
		}
		for product_id in products_to_refresh:
			refresh_deposit_product_from_transactions(Product.objects.get(pk=product_id))
		if sync_balance:
			sync_account_balance(old_account)
			if old_account.pk != account.pk:
				sync_account_balance(account)
	return ledger_transaction


def delete_transaction(ledger_transaction: Transaction, *, sync_balance: bool = True) -> None:
	counterpart = _find_transfer_counterpart(ledger_transaction)
	product = ledger_transaction.product
	accounts_to_sync = {ledger_transaction.account}
	if counterpart is not None:
		accounts_to_sync.add(counterpart.account)
	with transaction.atomic():
		if counterpart is not None:
			counterpart.delete()
		ledger_transaction.delete()
		if product is not None and product.product_type == Product.ProductType.DEPOSIT:
			refresh_deposit_product_from_transactions(product)
		if sync_balance:
			for account in accounts_to_sync:
				sync_account_balance(account)


def repair_legacy_transfer(ledger_transaction: Transaction, *, sync_balance: bool = True) -> Transaction:
	if ledger_transaction.transaction_type != Transaction.TransactionType.TRANSFER:
		return ledger_transaction
	if _find_transfer_counterpart(ledger_transaction) is not None:
		return ledger_transaction
	if ledger_transaction.related_account_id is None:
		return ledger_transaction

	source_account = ledger_transaction.account
	destination_account = ledger_transaction.related_account
	magnitude = _transfer_magnitude(ledger_transaction.amount)
	if magnitude <= 0:
		return ledger_transaction

	with transaction.atomic():
		repaired = _update_transfer_pair(
			ledger_transaction,
			source_account=source_account,
			destination_account=destination_account,
			currency=ledger_transaction.currency,
			amount=magnitude,
			occurred_at=ledger_transaction.occurred_at,
			product=ledger_transaction.product,
			external_id=ledger_transaction.external_id,
			quantity=ledger_transaction.quantity,
			unit_price=ledger_transaction.unit_price,
			description=ledger_transaction.description,
			metadata=ledger_transaction.metadata if isinstance(ledger_transaction.metadata, dict) else {},
			sync_balance=sync_balance,
		)
	return repaired
