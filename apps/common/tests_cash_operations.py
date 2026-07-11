from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Account, Transaction
from apps.common.management.commands.bootstrap_local_data import Command as BootstrapCommand
from apps.common.services.cash_operations import (
	CASH_OPERATION_LABELS,
	get_default_cash_account,
	record_cash_operation,
)
from apps.imports.models import ImportSource
from apps.institutions.models import FinancialInstitution


class CashOperationsTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()
		cls.cash_account = get_default_cash_account()
		cls.bank_account = Account.objects.get(institution__slug='alfabank', name='АльфаБанк BYN Account')

	def test_bootstrap_creates_cash_institution_account_and_source(self):
		institution = FinancialInstitution.objects.get(slug='cash')
		self.assertEqual(institution.name, 'Наличные')
		self.assertEqual(self.cash_account.account_type, Account.AccountType.CASH)
		self.assertEqual(self.cash_account.name, 'Наличные BYN')

		usd_cash = Account.objects.get(institution=institution, name='Наличные USD')
		self.assertEqual(usd_cash.currency.code, 'USD')

		source = ImportSource.objects.get(code='cash-manual')
		self.assertEqual(source.institution, institution)
		self.assertEqual(source.config['operations'], ['deposit', 'withdrawal', 'transfer_out', 'transfer_in'])

	def test_record_deposit_and_withdrawal(self):
		record_cash_operation(
			operation='deposit',
			amount=Decimal('120.50'),
			occurred_at=date(2026, 7, 1),
			description='Зарплата наличными',
		)
		record_cash_operation(
			operation='withdrawal',
			amount=Decimal('20.00'),
			occurred_at=date(2026, 7, 2),
			description='Продукты',
		)

		self.cash_account.refresh_from_db()
		self.assertEqual(self.cash_account.current_balance, Decimal('100.50'))

		deposit = Transaction.objects.get(metadata__operation_type=CASH_OPERATION_LABELS['deposit'])
		withdrawal = Transaction.objects.get(metadata__operation_type=CASH_OPERATION_LABELS['withdrawal'])
		self.assertEqual(deposit.amount, Decimal('120.50'))
		self.assertEqual(withdrawal.amount, Decimal('-20.00'))

	def test_record_transfer_out_and_in(self):
		record_cash_operation(
			operation='deposit',
			amount=Decimal('100.00'),
			occurred_at=date(2026, 7, 3),
		)
		record_cash_operation(
			operation='transfer_out',
			amount=Decimal('40.00'),
			occurred_at=date(2026, 7, 4),
			related_account=self.bank_account,
		)
		record_cash_operation(
			operation='transfer_in',
			amount=Decimal('15.00'),
			occurred_at=date(2026, 7, 5),
			related_account=self.bank_account,
		)

		self.cash_account.refresh_from_db()
		self.bank_account.refresh_from_db()
		self.assertEqual(self.cash_account.current_balance, Decimal('75.00'))
		self.assertEqual(self.bank_account.current_balance, Decimal('25.00'))
		cash_transfers = Transaction.objects.filter(
			account=self.cash_account,
			transaction_type=Transaction.TransactionType.TRANSFER,
			metadata__source='cash-manual',
		)
		self.assertEqual(cash_transfers.count(), 2)

	def test_import_upload_shows_cash_manual_form(self):
		response = self.client.get(reverse('imports:upload'))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Наличные')
		self.assertContains(response, 'Записать операцию')
		self.assertContains(response, 'Пополнение')
		self.assertContains(response, 'Кошелёк')

	def test_import_cash_operation_view(self):
		response = self.client.post(
			reverse('imports:cash_operation'),
			{
				'cash_account': self.cash_account.pk,
				'operation': 'deposit',
				'amount': '50.00',
				'occurred_at': '2026-07-10',
				'description': 'Тест',
			},
		)
		self.assertRedirects(response, reverse('imports:upload'))
		self.cash_account.refresh_from_db()
		self.assertEqual(self.cash_account.current_balance, Decimal('50.00'))

	def test_record_usd_cash_deposit(self):
		usd_cash = Account.objects.get(name='Наличные USD')
		record_cash_operation(
			operation='deposit',
			amount=Decimal('25.00'),
			occurred_at=date(2026, 7, 11),
			cash_account=usd_cash,
		)
		usd_cash.refresh_from_db()
		self.assertEqual(usd_cash.current_balance, Decimal('25.00'))
