from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.models import Account, Transaction
from apps.accounts.services.balance import sync_account_balance
from apps.common.services.aigenis_bonds import get_alfabank_byn_account
from apps.common.services.exchange_rates import get_usd_conversion_rate
from apps.common.services.ledger import TRANSFER_LEG_METADATA_KEY, TRANSFER_PAIR_METADATA_KEY
from apps.imports.models import ImportJob

PARITET_FUNDING_MARKERS = ('паритет',)


def is_paritet_funding_source(source: str | None) -> bool:
	normalized = str(source or '').casefold()
	return any(marker in normalized for marker in PARITET_FUNDING_MARKERS)


def _as_aware_datetime(value):
	if hasattr(value, 'tzinfo') and value.tzinfo is not None:
		return value
	if isinstance(value, str):
		parsed = parse_datetime(value)
		if parsed is None:
			raise ValueError(f'Invalid datetime: {value}')
		value = parsed
	if timezone.is_naive(value):
		return timezone.make_aware(value, timezone.get_current_timezone())
	return value


def _amount_to_usd(currency, amount: Decimal, as_of_date) -> Decimal:
	rate = get_usd_conversion_rate(currency, as_of_date)
	return ((amount or Decimal('0')) * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def replace_aigenis_paritet_funding_with_alfabank_transfer(
	*,
	deposit_transaction: Transaction | None = None,
	aigenis_account: Account | None = None,
	amount: Decimal | None = None,
	occurred_at=None,
	import_job: ImportJob | None = None,
	import_fingerprint: str = '',
	reported_source: str = 'Паритетбанк',
	bank_fee: Decimal | None = None,
	description: str = '',
	metadata: dict | None = None,
) -> list[Transaction]:
	"""
	Record Aigenis cash top-up as a transfer from Alfa-Bank.

	Broker reports often label the source as Паритетбанк (payment agent);
	the actual cash leaves the Alfa-Bank BYN account.
	"""
	if deposit_transaction is not None:
		aigenis_account = deposit_transaction.account
		amount = abs(deposit_transaction.amount or Decimal('0'))
		occurred_at = deposit_transaction.occurred_at
		import_job = deposit_transaction.import_job or import_job
		import_fingerprint = import_fingerprint or deposit_transaction.import_fingerprint
		meta = deposit_transaction.metadata if isinstance(deposit_transaction.metadata, dict) else {}
		reported_source = (
			reported_source
			or meta.get('deposit_source')
			or meta.get('security_type')
			or 'Паритетбанк'
		)
		metadata = {
			**meta,
			**(metadata or {}),
		}

	if aigenis_account is None:
		raise ValueError('Aigenis account is required.')
	magnitude = abs(amount or Decimal('0'))
	if magnitude <= 0:
		raise ValueError('Funding amount must be greater than zero.')
	occurred_at = _as_aware_datetime(occurred_at)
	alfa_account = get_alfabank_byn_account()
	if alfa_account is None:
		raise ValueError('Alfa-Bank BYN account was not found.')
	if alfa_account.currency_id != aigenis_account.currency_id:
		raise ValueError('Alfa-Bank and Aigenis accounts must use the same currency.')

	base_fingerprint = (import_fingerprint or f'aigenis-funding:{uuid4()}').removesuffix(':funding-in').removesuffix(':funding-out')
	out_fingerprint = f'{base_fingerprint}:funding-out'
	in_fingerprint = f'{base_fingerprint}:funding-in'
	fee_fingerprint = f'{base_fingerprint}:bank-fee'
	pair_id = str(uuid4())
	occurred_date = occurred_at.date()
	base_metadata = {
		**(metadata or {}),
		'imported_from': (metadata or {}).get('imported_from') or 'aigenis-report',
		'operation_type': (metadata or {}).get('operation_type') or 'Пополнение д.с.',
		'deposit_source_reported': reported_source,
		'deposit_source_actual': 'alfabank',
		'deposit_source_note': 'Паритетбанк — платёжный агент',
		TRANSFER_PAIR_METADATA_KEY: pair_id,
	}
	transfer_description = description or (
		f'Перевод на {aigenis_account.name} (через {reported_source})'
	)

	created: list[Transaction] = []
	accounts_to_sync = {aigenis_account, alfa_account}

	with transaction.atomic():
		if deposit_transaction is not None and deposit_transaction.pk:
			accounts_to_sync.add(deposit_transaction.account)
			deposit_transaction.delete()

		# Replace any prior single-leg deposit that used the raw report fingerprint.
		for stale in Transaction.objects.filter(import_fingerprint__in={base_fingerprint, out_fingerprint, in_fingerprint, fee_fingerprint}):
			accounts_to_sync.add(stale.account)
			if stale.related_account_id:
				accounts_to_sync.add(stale.related_account)
			stale.delete()

		out_tx = Transaction(
			account=alfa_account,
			related_account=aigenis_account,
			import_job=import_job,
			transaction_type=Transaction.TransactionType.TRANSFER,
			currency=alfa_account.currency,
			import_fingerprint=out_fingerprint,
			amount=-magnitude,
			amount_usd=_amount_to_usd(alfa_account.currency, -magnitude, occurred_date),
			occurred_at=occurred_at,
			description=transfer_description,
			metadata={
				**base_metadata,
				TRANSFER_LEG_METADATA_KEY: 'out',
			},
		)
		in_tx = Transaction(
			account=aigenis_account,
			related_account=alfa_account,
			import_job=import_job,
			transaction_type=Transaction.TransactionType.TRANSFER,
			currency=aigenis_account.currency,
			import_fingerprint=in_fingerprint,
			amount=magnitude,
			amount_usd=_amount_to_usd(aigenis_account.currency, magnitude, occurred_date),
			occurred_at=occurred_at,
			description=transfer_description,
			metadata={
				**base_metadata,
				TRANSFER_LEG_METADATA_KEY: 'in',
			},
		)
		out_tx.full_clean()
		in_tx.full_clean()
		out_tx.save()
		in_tx.save()
		created.extend([out_tx, in_tx])

		fee_amount = abs(bank_fee) if bank_fee is not None else Decimal('0')
		if fee_amount > 0:
			fee_tx = Transaction(
				account=alfa_account,
				import_job=import_job,
				transaction_type=Transaction.TransactionType.FEE,
				currency=alfa_account.currency,
				import_fingerprint=fee_fingerprint,
				amount=-fee_amount,
				amount_usd=_amount_to_usd(alfa_account.currency, -fee_amount, occurred_date),
				occurred_at=occurred_at,
				description=f'Комиссия банка за перевод на {aigenis_account.name}',
				metadata={
					**base_metadata,
					'fee_kind': 'bank_transfer',
					'fee_amount': str(fee_amount),
				},
			)
			fee_tx.full_clean()
			fee_tx.save()
			created.append(fee_tx)

		for account in accounts_to_sync:
			sync_account_balance(account)

	return created
