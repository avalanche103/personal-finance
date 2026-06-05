from django.db import models
from django.db.models import Q

from apps.common.models import TimeStampedModel


class Account(TimeStampedModel):
	class AccountType(models.TextChoices):
		CASH = 'cash', 'Cash'
		BANK = 'bank', 'Bank account'
		BROKERAGE = 'brokerage', 'Brokerage'
		DEPOSIT = 'deposit', 'Deposit'
		WALLET = 'wallet', 'Wallet'
		CARD = 'card', 'Card'
		OTHER = 'other', 'Other'

	institution = models.ForeignKey('institutions.FinancialInstitution', on_delete=models.CASCADE, related_name='accounts')
	name = models.CharField(max_length=255)
	account_type = models.CharField(max_length=32, choices=AccountType.choices, default=AccountType.OTHER)
	currency = models.ForeignKey('common.Currency', on_delete=models.PROTECT, related_name='accounts')
	external_id = models.CharField(max_length=128, blank=True)
	current_balance = models.DecimalField(max_digits=20, decimal_places=2, default=0)
	current_balance_usd = models.DecimalField(max_digits=20, decimal_places=2, default=0)
	metadata = models.JSONField(default=dict, blank=True)
	is_active = models.BooleanField(default=True)

	class Meta:
		ordering = ['name']
		constraints = [
			models.UniqueConstraint(
				fields=['institution', 'external_id'],
				condition=~Q(external_id=''),
				name='unique_account_external_id_per_institution',
			),
		]

	def __str__(self) -> str:
		return self.name


class Transaction(TimeStampedModel):
	class TransactionType(models.TextChoices):
		DEPOSIT = 'deposit', 'Deposit'
		WITHDRAWAL = 'withdrawal', 'Withdrawal'
		TRADE = 'trade', 'Trade'
		INCOME = 'income', 'Income'
		TRANSFER = 'transfer', 'Transfer'
		FEE = 'fee', 'Fee'
		OTHER = 'other', 'Other'

	account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='transactions')
	related_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.SET_NULL, related_name='linked_transactions')
	product = models.ForeignKey('products.Product', null=True, blank=True, on_delete=models.SET_NULL, related_name='transactions')
	import_job = models.ForeignKey('imports.ImportJob', null=True, blank=True, on_delete=models.SET_NULL, related_name='transactions')
	transaction_type = models.CharField(max_length=32, choices=TransactionType.choices, default=TransactionType.OTHER)
	currency = models.ForeignKey('common.Currency', on_delete=models.PROTECT, related_name='transactions')
	external_id = models.CharField(max_length=128, blank=True)
	import_fingerprint = models.CharField(max_length=128, blank=True, unique=True)
	amount = models.DecimalField(max_digits=20, decimal_places=2)
	amount_usd = models.DecimalField(max_digits=20, decimal_places=2, default=0)
	quantity = models.DecimalField(max_digits=20, decimal_places=8, default=0)
	unit_price = models.DecimalField(max_digits=20, decimal_places=8, default=0)
	occurred_at = models.DateTimeField()
	description = models.TextField(blank=True)
	metadata = models.JSONField(default=dict, blank=True)

	class Meta:
		ordering = ['-occurred_at', '-id']
		constraints = [
			models.UniqueConstraint(
				fields=['account', 'external_id'],
				condition=~Q(external_id=''),
				name='unique_transaction_external_id_per_account',
			),
		]

	def __str__(self) -> str:
		return f'{self.account} {self.transaction_type} {self.amount}'


class BalanceSnapshot(TimeStampedModel):
	institution = models.ForeignKey('institutions.FinancialInstitution', on_delete=models.CASCADE, related_name='balance_snapshots')
	account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.CASCADE, related_name='balance_snapshots')
	product = models.ForeignKey('products.Product', null=True, blank=True, on_delete=models.CASCADE, related_name='balance_snapshots')
	currency = models.ForeignKey('common.Currency', on_delete=models.PROTECT, related_name='balance_snapshots')
	balance = models.DecimalField(max_digits=20, decimal_places=8)
	balance_usd = models.DecimalField(max_digits=20, decimal_places=2, default=0)
	captured_at = models.DateTimeField()
	metadata = models.JSONField(default=dict, blank=True)

	class Meta:
		ordering = ['-captured_at', '-id']

	def __str__(self) -> str:
		from apps.common.dates import format_display_datetime

		return f'{self.institution} @ {format_display_datetime(self.captured_at)}'
