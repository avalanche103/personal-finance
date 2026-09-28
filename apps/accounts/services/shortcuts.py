from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.accounts.services.balance import sync_account_balance
from apps.common.services.aigenis_bonds import get_aigenis_byn_account, get_alfabank_byn_account
from apps.common.services.bynex_trades import (
	build_transfer_row,
	record_bynex_transfer,
	record_bynex_usd_usdt_trade,
)
from apps.common.services.ledger import (
	TRANSFER_LEG_METADATA_KEY,
	TRANSFER_PAIR_METADATA_KEY,
	create_transaction,
)


@dataclass(frozen=True)
class AlfaAigenisTransferResult:
	transfer: Transaction
	fee_transaction: Transaction | None


def _aware_datetime(value):
	if timezone.is_naive(value):
		return timezone.make_aware(value, timezone.get_current_timezone())
	return value


def record_bynex_usdt_buy(*, usd_spent: Decimal, usdt_received: Decimal, occurred_at):
	return record_bynex_usd_usdt_trade(
		usd_spent=usd_spent,
		usdt_received=usdt_received,
		occurred_at=occurred_at,
	)


def _binance_usdt_account() -> Account:
	from apps.accounts.services.binance import _ensure_account, ensure_binance_reference_data

	institution, _ = ensure_binance_reference_data()
	return _ensure_account(institution, 'USDT', wallet='spot', update_balance=False)


def _transfer_leg_metadata(
	*,
	pair_id: str,
	leg: str,
	counterpart: Account,
	base_metadata: dict | None,
) -> dict:
	metadata = dict(base_metadata or {})
	metadata[TRANSFER_PAIR_METADATA_KEY] = pair_id
	metadata[TRANSFER_LEG_METADATA_KEY] = leg
	metadata['transfer_counterpart_account_id'] = counterpart.pk
	return metadata


def link_bynex_binance_transfer_pair(*, bynex_transfer: Transaction, incoming: Transaction) -> None:
	pair_id = str(
		(bynex_transfer.metadata or {}).get(TRANSFER_PAIR_METADATA_KEY)
		or (incoming.metadata or {}).get(TRANSFER_PAIR_METADATA_KEY)
		or uuid4()
	)
	out_metadata = _transfer_leg_metadata(
		pair_id=pair_id,
		leg='out',
		counterpart=incoming.account,
		base_metadata=bynex_transfer.metadata,
	)
	in_metadata = _transfer_leg_metadata(
		pair_id=pair_id,
		leg='in',
		counterpart=bynex_transfer.account,
		base_metadata=incoming.metadata,
	)
	in_metadata.setdefault('source', 'bynex')
	in_metadata.setdefault('operation_kind', 'incoming_from_bynex')
	bynex_transfer.related_account = incoming.account
	bynex_transfer.metadata = out_metadata
	bynex_transfer.save(update_fields=['related_account', 'metadata', 'updated_at'])
	incoming.related_account = bynex_transfer.account
	incoming.transaction_type = Transaction.TransactionType.TRANSFER
	incoming.metadata = in_metadata
	incoming.save(update_fields=['related_account', 'transaction_type', 'metadata', 'updated_at'])


def repair_bynex_binance_transfer_pairs() -> int:
	repaired = 0
	for incoming in Transaction.objects.filter(metadata__operation_kind='incoming_from_bynex'):
		fingerprint = incoming.import_fingerprint or ''
		suffix = ':binance-in'
		if not fingerprint.endswith(suffix):
			continue
		outbound = Transaction.objects.filter(import_fingerprint=fingerprint[: -len(suffix)]).first()
		if outbound is None:
			continue
		link_bynex_binance_transfer_pair(bynex_transfer=outbound, incoming=incoming)
		repaired += 1
	return repaired


def _credit_binance_usdt(*, quantity: Decimal, occurred_at, fingerprint: str) -> Transaction:
	from apps.common.services.exchange_rates import get_usd_conversion_rate

	account = _binance_usdt_account()
	occurred_at = _aware_datetime(occurred_at)
	incoming_fingerprint = f'{fingerprint}:binance-in'
	is_api_snapshot = (account.metadata or {}).get('current_balance_source') == 'api_snapshot'
	amount = quantity.quantize(Decimal('0.01'))
	incoming, created = Transaction.objects.update_or_create(
		import_fingerprint=incoming_fingerprint,
		defaults={
			'account': account,
			'transaction_type': Transaction.TransactionType.TRANSFER,
			'currency': account.currency,
			'amount': amount,
			'amount_usd': (amount * get_usd_conversion_rate(account.currency, occurred_at.date())).quantize(
				Decimal('0.01')
			),
			'quantity': quantity.quantize(Decimal('0.000001')),
			'occurred_at': occurred_at,
			'description': 'USDT received from BYNEX',
			'metadata': {
				'source': 'bynex',
				'operation_kind': 'incoming_from_bynex',
				'exclude_from_account_balance': is_api_snapshot,
			},
		},
	)
	if is_api_snapshot:
		if created:
			account.current_balance = (account.current_balance or Decimal('0')) + amount
			account.current_balance_usd = (account.current_balance_usd or Decimal('0')) + incoming.amount_usd
			account.save(update_fields=['current_balance', 'current_balance_usd', 'updated_at'])
	else:
		sync_account_balance(account)
	return incoming


def record_bynex_to_binance_transfer(*, quantity: Decimal, fee: Decimal, occurred_at):
	row = build_transfer_row(
		occurred_at=occurred_at,
		asset='USDT',
		quantity=quantity,
		fee=fee or Decimal('0'),
		destination='Binance',
	)
	result = record_bynex_transfer(row)
	incoming = _credit_binance_usdt(
		quantity=quantity,
		occurred_at=occurred_at,
		fingerprint=result.transfer.import_fingerprint,
	)
	link_bynex_binance_transfer_pair(bynex_transfer=result.transfer, incoming=incoming)
	return result


def record_alfa_to_aigenis_transfer(
	*,
	amount: Decimal,
	fee: Decimal,
	occurred_at,
	description: str = '',
) -> AlfaAigenisTransferResult:
	alfa_account = get_alfabank_byn_account()
	aigenis_account = get_aigenis_byn_account()
	if alfa_account is None:
		raise ValueError('Не найден счёт АльфаБанк BYN.')
	if aigenis_account is None:
		raise ValueError('Не найден счёт Aigenis BYN.')
	if alfa_account.currency_id != aigenis_account.currency_id:
		raise ValueError('Счета АльфаБанк и Aigenis должны быть в одной валюте.')

	magnitude = abs(amount or Decimal('0'))
	if magnitude <= 0:
		raise ValueError('Сумма перевода должна быть больше нуля.')

	occurred_at = _aware_datetime(occurred_at)
	transfer = create_transaction(
		account=alfa_account,
		related_account=aigenis_account,
		transaction_type=Transaction.TransactionType.TRANSFER,
		currency=alfa_account.currency,
		amount=magnitude,
		occurred_at=occurred_at,
		description=description or f'Перевод на {aigenis_account.name}',
		metadata={'source': 'manual', 'operation_kind': 'alfa_to_aigenis'},
	)
	fee_transaction = None
	fee_amount = abs(fee or Decimal('0'))
	if fee_amount > 0:
		fee_transaction = create_transaction(
			account=alfa_account,
			transaction_type=Transaction.TransactionType.FEE,
			currency=alfa_account.currency,
			amount=-fee_amount,
			occurred_at=occurred_at,
			description=f'Комиссия банка за перевод на {aigenis_account.name}',
			metadata={
				'source': 'manual',
				'operation_kind': 'alfa_to_aigenis_fee',
				'fee_kind': 'bank_transfer',
			},
		)
	return AlfaAigenisTransferResult(transfer=transfer, fee_transaction=fee_transaction)
