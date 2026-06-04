from datetime import date, timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.common.management.commands.bootstrap_local_data import Command as BootstrapCommand
from apps.common.models import Currency
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product


class DashboardSmokeTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def setUp(self):
		self.client = Client()

	def test_dashboard_and_reports_render(self):
		for url in ['/', '/exchange-rates/', '/portfolio-report/', '/settings/']:
			response = self.client.get(url)
			self.assertEqual(response.status_code, 200, url)

	def test_dashboard_contains_bootstrap_cards(self):
		response = self.client.get('/')
		self.assertContains(response, 'NBRB rates')
		self.assertContains(response, 'USD')
		self.assertContains(response, 'Finstore')
		self.assertContains(response, 'Groups')
		self.assertContains(response, 'All products')
		self.assertContains(response, 'Period comparison')
		self.assertContains(response, 'Recent operations')
		self.assertContains(response, 'Operations calendar')
		self.assertContains(response, 'portfolio-chart-data')
		self.assertContains(response, 'plotly')
		self.assertContains(response, 'Last day of previous month')
		self.assertIn('product_groups', response.context)
		self.assertIn('historical_reporting', response.context)
		self.assertEqual(len(response.context['historical_reporting']['period_comparisons']), 2)

	def test_portfolio_report_contains_bootstrap_institution(self):
		response = self.client.get('/portfolio-report/?as_of=2026-05-31')
		self.assertContains(response, 'Finstore')

	def test_portfolio_report_period_comparison_uses_reference_dates(self):
		usd = Currency.objects.get(code='USD')
		institution = FinancialInstitution.objects.create(
			name='Comparison Bank',
			institution_type=FinancialInstitution.InstitutionType.BANK,
		)
		account = Account.objects.create(
			institution=institution,
			name='Comparison cash',
			account_type=Account.AccountType.BANK,
			currency=usd,
			current_balance=Decimal('2000'),
			current_balance_usd=Decimal('2000'),
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			account=account,
			currency=usd,
			balance=Decimal('1000'),
			balance_usd=Decimal('1000'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 5, 31, 12, 0)),
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			account=account,
			currency=usd,
			balance=Decimal('1500'),
			balance_usd=Decimal('1500'),
			captured_at=timezone.make_aware(timezone.datetime(2025, 12, 31, 12, 0)),
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			account=account,
			currency=usd,
			balance=Decimal('2000'),
			balance_usd=Decimal('2000'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 6, 3, 12, 0)),
		)

		response = self.client.get('/portfolio-report/?as_of=2026-06-04')
		self.assertEqual(response.status_code, 200)
		comparisons = response.context['period_comparisons']
		self.assertEqual(len(comparisons), 2)

		prev_month = next(item for item in comparisons if item['key'] == 'prev_month')
		prev_year = next(item for item in comparisons if item['key'] == 'prev_year')
		self.assertEqual(prev_month['reference_date'], date(2026, 5, 31))
		self.assertEqual(prev_year['reference_date'], date(2025, 12, 31))
		self.assertEqual(prev_month['portfolio']['baseline_usd'], Decimal('1000'))
		self.assertEqual(prev_year['portfolio']['baseline_usd'], Decimal('1500'))
		self.assertEqual(prev_month['portfolio']['change_abs'], Decimal('1000'))
		self.assertEqual(prev_month['portfolio']['change_pct'], Decimal('100'))
		self.assertEqual(prev_year['portfolio']['change_abs'], Decimal('500'))
		self.assertContains(response, 'Last day of previous month')
		self.assertContains(response, 'Last day of previous year')

	def test_dashboard_group_shows_xirr_for_custom_product_group(self):
		usd = Currency.objects.get(code='USD')
		institution = FinancialInstitution.objects.create(
			name='Yield House',
			institution_type=FinancialInstitution.InstitutionType.BROKER,
		)
		account = Account.objects.create(
			institution=institution,
			name='Yield cash',
			account_type=Account.AccountType.BROKERAGE,
			currency=usd,
		)
		product = Product.objects.create(
			institution=institution,
			name='Yield Note',
			product_type=Product.ProductType.BOND,
			currency=usd,
			units=Decimal('10'),
			current_price=Decimal('110'),
			current_value_usd=Decimal('1100'),
		)
		Transaction.objects.create(
			account=account,
			product=product,
			transaction_type=Transaction.TransactionType.TRADE,
			currency=usd,
			import_fingerprint='dashboard-xirr-buy',
			amount=Decimal('-1000'),
			amount_usd=Decimal('-1000'),
			quantity=Decimal('10'),
			unit_price=Decimal('100'),
			occurred_at=timezone.now() - timedelta(days=30),
		)
		Transaction.objects.create(
			account=account,
			product=product,
			transaction_type=Transaction.TransactionType.INCOME,
			currency=usd,
			import_fingerprint='dashboard-xirr-income',
			amount=Decimal('25'),
			amount_usd=Decimal('25'),
			quantity=Decimal('0'),
			unit_price=Decimal('0'),
			occurred_at=timezone.now() - timedelta(days=10),
		)

		response = self.client.get('/')

		self.assertEqual(response.status_code, 200)
		group = next(item for item in response.context['product_groups'] if item['label'] == 'Yield House_USD')
		self.assertIsNotNone(group['xirr_pct'])
		self.assertContains(response, 'Yield House_USD')
		self.assertContains(response, 'XIRR')

# Create your tests here.
