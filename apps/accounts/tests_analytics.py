from decimal import Decimal

from django.test import TestCase

from apps.accounts.analytics import build_dashboard_balance_rows, is_binance_stable_account
from apps.accounts.models import Account
from apps.common.models import Currency
from apps.institutions.models import FinancialInstitution


class DashboardBalanceRowsTests(TestCase):
	def setUp(self):
		self.usd = Currency.objects.create(code='USD', name='US Dollar', symbol='$', usd_rate=Decimal('1'), is_base=True)
		self.binance = FinancialInstitution.objects.create(
			name='Binance',
			slug='binance',
			institution_type=FinancialInstitution.InstitutionType.CRYPTO_EXCHANGE,
		)
		self.bank = FinancialInstitution.objects.create(
			name='Test Bank',
			slug='test-bank',
			institution_type=FinancialInstitution.InstitutionType.BANK,
		)

	def test_is_binance_stable_account_detects_usdt_and_rwusd(self):
		usdt = Account(
			institution=self.binance,
			currency=self.usd,
			metadata={'asset': 'USDT'},
		)
		rwusd = Account(
			institution=self.binance,
			currency=self.usd,
			metadata={'asset': 'RWUSD'},
		)
		self.assertTrue(is_binance_stable_account(usdt))
		self.assertTrue(is_binance_stable_account(rwusd))

	def test_build_dashboard_balance_rows_merges_binance_stables(self):
		accounts = [
			Account.objects.create(
				institution=self.binance,
				name='Binance USDT',
				account_type=Account.AccountType.WALLET,
				currency=self.usd,
				current_balance=Decimal('70'),
				current_balance_usd=Decimal('70'),
				metadata={'asset': 'USDT'},
			),
			Account.objects.create(
				institution=self.binance,
				name='Binance RWUSD',
				account_type=Account.AccountType.WALLET,
				currency=self.usd,
				current_balance=Decimal('30'),
				current_balance_usd=Decimal('30'),
				metadata={'asset': 'RWUSD'},
			),
			Account.objects.create(
				institution=self.bank,
				name='Checking',
				account_type=Account.AccountType.BANK,
				currency=self.usd,
				current_balance=Decimal('200'),
				current_balance_usd=Decimal('200'),
			),
		]

		rows = build_dashboard_balance_rows(accounts)

		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0].name, 'Checking')
		self.assertEqual(rows[1].name, 'Binance Stable')
		self.assertEqual(rows[1].current_balance_usd, Decimal('100'))
