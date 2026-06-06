from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.common.management.commands.bootstrap_local_data import Command as BootstrapCommand
from apps.common.models import Currency
from apps.institutions.models import FinancialInstitution
from apps.dashboard.views import (
	_dashboard_metrics,
	_historical_portfolio_context,
	_portfolio_chart_payload,
	_portfolio_chart_points,
	_product_value_as_of,
)
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

	def test_dashboard_recent_operations_show_native_and_usd_for_non_usd(self):
		byn = Currency.objects.get(code='BYN')
		account = Account.objects.get(name='Finstore BYN Account')
		Transaction.objects.create(
			account=account,
			transaction_type=Transaction.TransactionType.INCOME,
			currency=byn,
			amount=Decimal('14.00'),
			amount_usd=Decimal('5.00'),
			occurred_at=timezone.now(),
			description='Test BYN income',
		)

		response = self.client.get('/')
		self.assertContains(response, 'Test BYN income')
		self.assertContains(response, 'BYN</div>')
		self.assertContains(response, '$5,00')

	def test_dashboard_accounts_show_native_and_usd_for_non_usd(self):
		byn_account = Account.objects.get(name='Finstore BYN Account')
		byn_account.current_balance = Decimal('14.00')
		byn_account.current_balance_usd = Decimal('5.00')
		byn_account.save(update_fields=['current_balance', 'current_balance_usd', 'updated_at'])

		response = self.client.get('/')
		self.assertContains(response, 'BYN</div>')
		self.assertContains(response, '$5,00')

	def test_dashboard_balances_hide_zero_binance_accounts(self):
		usd = Currency.objects.get(code='USD')
		binance = FinancialInstitution.objects.get(slug='binance')
		Account.objects.create(
			institution=binance,
			name='Binance Visible USD',
			account_type=Account.AccountType.WALLET,
			currency=usd,
			current_balance=Decimal('10'),
			current_balance_usd=Decimal('10'),
		)
		Account.objects.create(
			institution=binance,
			name='Binance Empty BTC',
			account_type=Account.AccountType.WALLET,
			currency=usd,
			current_balance=Decimal('0'),
			current_balance_usd=Decimal('0'),
		)

		response = self.client.get('/')

		self.assertContains(response, 'Binance Visible USD')
		self.assertNotContains(response, 'Binance Empty BTC')

	def test_portfolio_chart_points_respect_range(self):
		as_of = date(2026, 6, 4)
		self.assertEqual(len(_portfolio_chart_points(as_of, 'week')), 7)
		self.assertEqual(len(_portfolio_chart_points(as_of, 'month')), 30)
		self.assertGreaterEqual(len(_portfolio_chart_points(as_of, 'year')), 52)

	def test_portfolio_chart_payload_includes_change_series(self):
		points = [
			{'date': date(2026, 6, 1), 'value': 1000.0},
			{'date': date(2026, 6, 4), 'value': 1012.5},
		]
		payload = _portfolio_chart_payload(points, 'week', 'change')
		self.assertEqual(payload['change_pct'][0], 0.0)
		self.assertEqual(payload['change_pct'][-1], 1.25)
		self.assertEqual(payload['period_change_usd'], 12.5)

	def test_dashboard_portfolio_chart_partial_supports_range_and_mode(self):
		response = self.client.get('/partials/portfolio-chart/?range=week&mode=value')
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'chart-range-btn is-active')
		self.assertContains(response, 'Total USD')

	@patch('apps.common.services.exchange_rates.ensure_nbrb_rates_current')
	def test_dashboard_does_not_fetch_nbrb_rates_on_load(self, ensure_rates):
		for url in ['/', '/exchange-rates/', '/partials/latest-rates/']:
			response = self.client.get(url)
			self.assertEqual(response.status_code, 200, url)
		ensure_rates.assert_not_called()

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
		self.assertContains(response, 'chart-range-btn')
		self.assertContains(response, 'Total USD')
		self.assertContains(response, 'Change %')
		self.assertContains(response, 'portfolio-chart-panel')
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

	def test_historical_portfolio_treats_pension_snapshot_as_total_value(self):
		byn = Currency.objects.get(code='BYN')
		institution = FinancialInstitution.objects.create(
			name='Pension Snapshot Test Insurer',
			slug='pension-snapshot-test-insurer',
			institution_type=FinancialInstitution.InstitutionType.INSURANCE,
			base_currency=byn,
		)
		product = Product.objects.create(
			institution=institution,
			name='DNPS',
			product_type=Product.ProductType.PENSION,
			currency=byn,
			units=Decimal('1'),
			current_price=Decimal('4815.42'),
			current_value_usd=Decimal('1705.78'),
			external_id='TEST-PENSION-SNAPSHOT',
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			product=product,
			currency=byn,
			balance=Decimal('4815.42'),
			balance_usd=Decimal('1705.78'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 5, 1, 12, 0)),
		)

		response = self.client.get('/portfolio-report/?as_of=2026-06-05')
		self.assertEqual(response.status_code, 200)
		self.assertGreater(response.context['products_total_usd'], Decimal('1500'))
		self.assertLess(response.context['portfolio_usd'], Decimal('10000'))

	def test_pension_value_after_snapshot_uses_statement_balance(self):
		byn = Currency.objects.get(code='BYN')
		institution = FinancialInstitution.objects.create(
			name='Pension Snapshot After Test',
			slug='pension-snapshot-after-test',
			institution_type=FinancialInstitution.InstitutionType.INSURANCE,
			base_currency=byn,
		)
		account = Account.objects.create(
			institution=institution,
			name='Premiums',
			currency=byn,
		)
		product = Product.objects.create(
			institution=institution,
			name='DNPS after snapshot',
			product_type=Product.ProductType.PENSION,
			currency=byn,
			units=Decimal('1'),
			current_price=Decimal('4815.42'),
			current_value_usd=Decimal('1705.78'),
			external_id='TEST-PENSION-SNAPSHOT-AFTER',
		)
		Transaction.objects.create(
			account=account,
			product=product,
			transaction_type=Transaction.TransactionType.DEPOSIT,
			currency=byn,
			amount=Decimal('100'),
			import_fingerprint='test-pension-snapshot-after-deposit',
			occurred_at=timezone.make_aware(timezone.datetime(2026, 4, 1, 12, 0)),
			metadata={'employee_share_byn': '50'},
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			product=product,
			currency=byn,
			balance=Decimal('4815.42'),
			balance_usd=Decimal('1705.78'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 5, 1, 12, 0)),
		)

		as_of = date(2026, 6, 1)
		if as_of == timezone.localdate():
			as_of = date(2026, 6, 2)
		value_usd = _product_value_as_of(product, as_of, {})
		from apps.common.services.exchange_rates import get_usd_conversion_rate

		expected_usd = Decimal('4815.42') * get_usd_conversion_rate(byn, as_of, {})
		self.assertEqual(value_usd, expected_usd)
		self.assertGreater(value_usd, Decimal('100'))

	def test_historical_portfolio_matches_hero_metrics_on_same_date(self):
		as_of = timezone.localdate()
		metrics = _dashboard_metrics()
		historical = _historical_portfolio_context(as_of)
		self.assertLess(abs(metrics['portfolio_usd'] - historical['portfolio_usd']), Decimal('1'))

	def test_historical_portfolio_reconstructs_life_insurance_from_transactions(self):
		usd = Currency.objects.get(code='USD')
		institution = FinancialInstitution.objects.create(
			name='Priorlife Chart Test',
			slug='priorlife-chart-test',
			institution_type=FinancialInstitution.InstitutionType.INSURANCE,
			base_currency=usd,
		)
		account = Account.objects.create(
			institution=institution,
			name='Priorlife premiums',
			currency=usd,
		)
		product = Product.objects.create(
			institution=institution,
			name='Priorlife contract',
			product_type=Product.ProductType.LIFE_INSURANCE,
			currency=usd,
			units=Decimal('1'),
			current_price=Decimal('3684.14'),
			current_value_usd=Decimal('3684.14'),
			external_id='TEST-PRIORLIFE-CHART',
		)
		Transaction.objects.create(
			account=account,
			product=product,
			transaction_type=Transaction.TransactionType.DEPOSIT,
			currency=usd,
			amount=Decimal('25'),
			import_fingerprint='test-priorlife-chart-deposit',
			occurred_at=timezone.make_aware(timezone.datetime(2016, 7, 27, 12, 0)),
			metadata={'net_amount': '23.00'},
		)
		Transaction.objects.create(
			account=account,
			product=product,
			transaction_type=Transaction.TransactionType.INCOME,
			currency=usd,
			amount=Decimal('5'),
			import_fingerprint='test-priorlife-chart-income',
			occurred_at=timezone.make_aware(timezone.datetime(2016, 7, 31, 12, 0)),
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			product=product,
			currency=usd,
			balance=Decimal('3684.14'),
			balance_usd=Decimal('3684.14'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 6, 5, 12, 0)),
		)

		before_import = _historical_portfolio_context(date(2026, 6, 4))
		on_import = _historical_portfolio_context(date(2026, 6, 5))
		early = _historical_portfolio_context(date(2016, 7, 31))

		product_row_before = next(
			row for row in before_import['product_rows'] if row['product'].id == product.id
		)
		product_row_on = next(
			row for row in on_import['product_rows'] if row['product'].id == product.id
		)
		product_row_early = next(
			row for row in early['product_rows'] if row['product'].id == product.id
		)
		self.assertEqual(product_row_early['value_usd'], Decimal('28'))
		self.assertEqual(product_row_before['value_usd'], Decimal('28'))
		self.assertNotEqual(product_row_before['value_usd'], Decimal('1'))
		self.assertEqual(product_row_on['value_usd'], Decimal('3684.14'))

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
