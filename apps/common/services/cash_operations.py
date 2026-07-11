from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from uuid import uuid4

from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.common.services.ledger import create_transaction
from apps.imports.models import ImportJob
from apps.institutions.models import FinancialInstitution

CASH_INSTITUTION_SLUG = 'cash'
CASH_IMPORT_SOURCE_CODE = 'cash-manual'

CASH_OPERATION_CHOICES = (
	('deposit', 'Пополнение'),
	('withdrawal', 'Расход'),
	('transfer_out', 'Перевод на счёт'),
	('transfer_in', 'Перевод со счёта'),
)

CASH_OPERATION_LABELS = dict(CASH_OPERATION_CHOICES)


@dataclass(frozen=True)
class CashOperationResult:
	operation: str
	transaction_ids: list[int]
	created: bool


def get_cash_institution() -> FinancialInstitution:
	return FinancialInstitution.objects.get(slug=CASH_INSTITUTION_SLUG)


def get_default_cash_account(*, currency_code: str = 'BYN') -> Account:
	account = Account.objects.filter(
		institution__slug=CASH_INSTITUTION_SLUG,
		account_type=Account.AccountType.CASH,
		currency__code=currency_code,
	).first()
	if account is not None:
		return account
	return Account.objects.filter(
		institution__slug=CASH_INSTITUTION_SLUG,
		account_type=Account.AccountType.CASH,
	).order_by('currency__code', 'name').first()


def _occurred_at_from_date(value: date) -> datetime:
	return timezone.make_aware(datetime.combine(value, time(12, 0)))


def record_cash_operation(
	*,
	operation: str,
	amount: Decimal,
	occurred_at: date | datetime,
	cash_account: Account | None = None,
	related_account: Account | None = None,
	description: str = '',
	import_job: ImportJob | None = None,
) -> CashOperationResult:
	if operation not in CASH_OPERATION_LABELS:
		raise ValueError(f'Unsupported cash operation: {operation}')

	magnitude = abs(amount or Decimal('0'))
	if magnitude <= 0:
		raise ValueError('Amount must be greater than zero.')

	cash_account = cash_account or get_default_cash_account()
	if isinstance(occurred_at, date) and not isinstance(occurred_at, datetime):
		occurred_at = _occurred_at_from_date(occurred_at)

	fingerprint_base = f'cash-manual:{uuid4()}'
	metadata = {
		'operation_type': CASH_OPERATION_LABELS[operation],
		'source': 'cash-manual',
	}
	if import_job is not None:
		metadata['import_job_id'] = import_job.pk

	if operation == 'deposit':
		ledger_transaction = create_transaction(
			account=cash_account,
			transaction_type=Transaction.TransactionType.DEPOSIT,
			currency=cash_account.currency,
			amount=magnitude,
			occurred_at=occurred_at,
			description=description or CASH_OPERATION_LABELS[operation],
			import_fingerprint=f'{fingerprint_base}:deposit',
			metadata=metadata,
		)
		if import_job is not None:
			ledger_transaction.import_job = import_job
			ledger_transaction.save(update_fields=['import_job', 'updated_at'])
		return CashOperationResult(operation=operation, transaction_ids=[ledger_transaction.pk], created=True)

	if operation == 'withdrawal':
		ledger_transaction = create_transaction(
			account=cash_account,
			transaction_type=Transaction.TransactionType.WITHDRAWAL,
			currency=cash_account.currency,
			amount=-magnitude,
			occurred_at=occurred_at,
			description=description or CASH_OPERATION_LABELS[operation],
			import_fingerprint=f'{fingerprint_base}:withdrawal',
			metadata=metadata,
		)
		if import_job is not None:
			ledger_transaction.import_job = import_job
			ledger_transaction.save(update_fields=['import_job', 'updated_at'])
		return CashOperationResult(operation=operation, transaction_ids=[ledger_transaction.pk], created=True)

	if related_account is None:
		raise ValueError('Select a counterparty account for a transfer.')

	if operation == 'transfer_out':
		ledger_transaction = create_transaction(
			account=cash_account,
			related_account=related_account,
			transaction_type=Transaction.TransactionType.TRANSFER,
			currency=cash_account.currency,
			amount=magnitude,
			occurred_at=occurred_at,
			description=description or f'{CASH_OPERATION_LABELS[operation]} · {related_account.name}',
			import_fingerprint=f'{fingerprint_base}:out',
			metadata=metadata,
		)
	elif operation == 'transfer_in':
		if related_account.currency_id != cash_account.currency_id:
			raise ValueError('Transfer accounts must use the same currency.')
		ledger_transaction = create_transaction(
			account=related_account,
			related_account=cash_account,
			transaction_type=Transaction.TransactionType.TRANSFER,
			currency=related_account.currency,
			amount=magnitude,
			occurred_at=occurred_at,
			description=description or f'{CASH_OPERATION_LABELS[operation]} · {cash_account.name}',
			import_fingerprint=f'{fingerprint_base}:out',
			metadata=metadata,
		)
	else:
		raise ValueError(f'Unsupported cash operation: {operation}')

	transaction_ids = [ledger_transaction.pk]
	pair_id = (ledger_transaction.metadata or {}).get('transfer_pair_id')
	if pair_id:
		transaction_ids = list(
			Transaction.objects.filter(metadata__transfer_pair_id=pair_id)
			.order_by('id')
			.values_list('id', flat=True)
		)
	if import_job is not None:
		Transaction.objects.filter(pk__in=transaction_ids).update(import_job=import_job)
	return CashOperationResult(operation=operation, transaction_ids=transaction_ids, created=True)
