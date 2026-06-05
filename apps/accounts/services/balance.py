from __future__ import annotations

from decimal import Decimal

from apps.accounts.models import Account, Transaction


def transaction_affects_account_balance(transaction: Transaction) -> bool:
	metadata = transaction.metadata if isinstance(transaction.metadata, dict) else {}
	return not metadata.get('exclude_from_account_balance')


def calculate_account_balance(account: Account) -> Decimal:
	total = Decimal('0')
	for transaction in Transaction.objects.filter(account=account).only('amount', 'metadata'):
		if transaction_affects_account_balance(transaction):
			total += transaction.amount or Decimal('0')
	return total


def sync_account_balance(account: Account) -> bool:
	current_balance = calculate_account_balance(account)
	if account.current_balance == current_balance:
		return False
	account.current_balance = current_balance
	account.save(update_fields=['current_balance', 'updated_at'])
	return True
