from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from apps.accounts.models import Account, Transaction
from apps.common.services.exchange_rates import get_usd_conversion_rate
from django.utils import timezone


def transaction_affects_account_balance(transaction: Transaction) -> bool:
	metadata = transaction.metadata if isinstance(transaction.metadata, dict) else {}
	return not metadata.get('exclude_from_account_balance')


def calculate_account_balance(account: Account) -> Decimal:
	return calculate_account_balance_as_of(account, timezone.localdate())


def calculate_account_balance_as_of(account: Account, as_of_date: date) -> Decimal:
	total = Decimal('0')
	end_of_day = timezone.make_aware(
		datetime.combine(as_of_date + timedelta(days=1), time.min),
		timezone.get_current_timezone(),
	)
	for transaction in Transaction.objects.filter(account=account, occurred_at__lt=end_of_day).only(
		'amount',
		'metadata',
	):
		if transaction_affects_account_balance(transaction):
			total += transaction.amount or Decimal('0')
	return total


def sync_account_balance(account: Account) -> bool:
	current_balance = calculate_account_balance(account)
	changed = False
	update_fields = ['updated_at']
	if account.current_balance != current_balance:
		account.current_balance = current_balance
		update_fields.append('current_balance')
		changed = True
	current_balance_usd = (current_balance or Decimal('0')) * get_usd_conversion_rate(
		account.currency,
		timezone.localdate(),
	)
	if account.current_balance_usd != current_balance_usd:
		account.current_balance_usd = current_balance_usd
		update_fields.append('current_balance_usd')
		changed = True
	if not changed:
		return False
	account.save(update_fields=update_fields)
	return True
