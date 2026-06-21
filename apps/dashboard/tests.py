from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.common.management.commands.bootstrap_local_data import Command as BootstrapCommand
from apps.common.models import Currency
from apps.institutions.models import FinancialInstitution
from apps.dashboard.views import (
	_build_deposit_withdrawal_totals,
	_build_portfolio_period_comparisons,
	_account_balance_as_of,
	_account_value_as_of,
	_dashboard_metrics,
	_historical_portfolio_context,
	_portfolio_chart_payload,
	_portfolio_chart_points,
	_product_value_as_of,
	PortfolioHistoryCache,
)
from apps.common.services.ledger import create_transaction
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
		rows = list(response.context['balance_rows'])
		balance_row_names = [row.name for row in rows]

		self.assertContains(response, 'Binance Stable')
		self.assertNotIn('Binance Visible USD', balance_row_names)
		self.assertNotIn('Binance Empty BTC', balance_row_names)

	def test_dashboard_balances_sorted_by_usd_descending(self):
		usd = Currency.objects.get(code='USD')
		bank = FinancialInstitution.objects.create(
			name='Sort Test Bank',
			slug='sort-test-bank',
			institution_type=FinancialInstitution.InstitutionType.BANK,
			base_currency=usd,
		)
		Account.objects.create(
			institution=bank,
			name='Small balance',
			account_type=Account.AccountType.BANK,
			currency=usd,
			current_balance=Decimal('10'),
			current_balance_usd=Decimal('10'),
		)
		Account.objects.create(
			institution=bank,
			name='Large balance',
			account_type=Account.AccountType.BANK,
			currency=usd,
			current_balance=Decimal('5000'),
			current_balance_usd=Decimal('5000'),
		)
		Account.objects.create(
			institution=bank,
			name='Zero balance',
			account_type=Account.AccountType.BANK,
			currency=usd,
			current_balance=Decimal('0'),
			current_balance_usd=Decimal('0'),
		)

		response = self.client.get('/')
		rows = list(response.context['balance_rows'])
		usd_values = [row.current_balance_usd for row in rows]

		self.assertEqual(usd_values, sorted(usd_values, reverse=True))
		self.assertNotIn('Zero balance', [row.name for row in rows])
		sort_test_rows = [row for row in rows if row.institution.slug == 'sort-test-bank']
		self.assertEqual([row.name for row in sort_test_rows], ['Large balance', 'Small balance'])

	def test_dashboard_balances_aggregate_binance_stables(self):
		usd = Currency.objects.get(code='USD')
		binance = FinancialInstitution.objects.get(slug='binance')
		Account.objects.create(
			institution=binance,
			name='Binance USDT',
			account_type=Account.AccountType.WALLET,
			currency=usd,
			external_id='binance:spot:USDT',
			current_balance=Decimal('100'),
			current_balance_usd=Decimal('100'),
			metadata={'source': 'binance', 'asset': 'USDT', 'wallet': 'spot'},
		)
		Account.objects.create(
			institution=binance,
			name='Binance USDC',
			account_type=Account.AccountType.WALLET,
			currency=usd,
			external_id='binance:spot:USDC',
			current_balance=Decimal('50'),
			current_balance_usd=Decimal('50'),
			metadata={'source': 'binance', 'asset': 'USDC', 'wallet': 'spot'},
		)

		response = self.client.get('/')
		rows = list(response.context['balance_rows'])
		binance_rows = [row for row in rows if row.institution.slug == 'binance']

		self.assertEqual(len(binance_rows), 1)
		self.assertEqual(binance_rows[0].name, 'Binance Stable')
		self.assertEqual(binance_rows[0].current_balance_usd, Decimal('150'))
		self.assertNotIn('Binance USDT', [row.name for row in rows])
		self.assertNotIn('Binance USDC', [row.name for row in rows])

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
		self.assertContains(response, 'Portfolio groups')
		self.assertContains(response, 'period-comparison')
		self.assertContains(response, 'period-comparison-row-label')
		comparisons = response.context['historical_reporting']['period_comparisons']
		self.assertIn('breakdown_groups', comparisons[0])
		self.assertIn('breakdown_products', comparisons[0])
		self.assertIn('breakdown_accounts', comparisons[0])
		self.assertIn('product_groups', response.context)
		self.assertIn('historical_reporting', response.context)
		self.assertEqual(len(response.context['historical_reporting']['period_comparisons']), 3)
		self.assertContains(response, 'Previous day')

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
			balance=Decimal('1900'),
			balance_usd=Decimal('1900'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 6, 3, 12, 0)),
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			account=account,
			currency=usd,
			balance=Decimal('2000'),
			balance_usd=Decimal('2000'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 6, 4, 12, 0)),
		)

		response = self.client.get('/portfolio-report/?as_of=2026-06-04')
		self.assertEqual(response.status_code, 200)
		comparisons = response.context['period_comparisons']
		self.assertEqual(len(comparisons), 3)

		prev_day = next(item for item in comparisons if item['key'] == 'prev_day')
		prev_month = next(item for item in comparisons if item['key'] == 'prev_month')
		prev_year = next(item for item in comparisons if item['key'] == 'prev_year')
		self.assertEqual(prev_day['reference_date'], date(2026, 6, 3))
		self.assertEqual(prev_month['reference_date'], date(2026, 5, 31))
		self.assertEqual(prev_year['reference_date'], date(2025, 12, 31))
		prev_day_account = next(row for row in prev_day['breakdown_groups'] if row['label'] == 'Comparison Bank')
		self.assertEqual(prev_day_account['change']['baseline_usd'], Decimal('1900'))
		self.assertEqual(prev_day_account['current_usd'], Decimal('2000'))
		self.assertEqual(prev_day_account['change']['change_abs'], Decimal('100'))
		prev_month_account = next(row for row in prev_month['breakdown_groups'] if row['label'] == 'Comparison Bank')
		prev_year_account = next(row for row in prev_year['breakdown_groups'] if row['label'] == 'Comparison Bank')
		self.assertEqual(prev_month_account['change']['baseline_usd'], Decimal('1000'))
		self.assertEqual(prev_month_account['current_usd'], Decimal('2000'))
		self.assertEqual(prev_month_account['change']['change_abs'], Decimal('1000'))
		self.assertEqual(prev_year_account['change']['baseline_usd'], Decimal('1500'))
		self.assertEqual(prev_year_account['current_usd'], Decimal('2000'))
		self.assertEqual(prev_year_account['change']['change_abs'], Decimal('500'))
		self.assertContains(response, 'Previous day')
		self.assertContains(response, 'Last day of previous month')
		self.assertContains(response, 'Last day of previous year')
		self.assertContains(response, 'Portfolio groups')

	def test_period_comparison_breakdown_includes_product_groups(self):
		usd = Currency.objects.get(code='USD')
		institution = FinancialInstitution.objects.create(
			name='Breakdown Broker',
			slug='breakdown-broker',
			institution_type=FinancialInstitution.InstitutionType.BROKER,
			base_currency=usd,
		)
		product = Product.objects.create(
			institution=institution,
			name='Breakdown token',
			product_type=Product.ProductType.TOKEN,
			currency=usd,
			units=Decimal('10'),
			current_price=Decimal('100'),
			current_value_usd=Decimal('1000'),
			external_id='BREAKDOWN-TOKEN',
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			product=product,
			currency=usd,
			balance=Decimal('5'),
			balance_usd=Decimal('500'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 5, 31, 12, 0)),
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			product=product,
			currency=usd,
			balance=Decimal('10'),
			balance_usd=Decimal('1000'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 6, 4, 12, 0)),
		)

		current = _historical_portfolio_context(date(2026, 6, 4))
		comparisons = _build_portfolio_period_comparisons(date(2026, 6, 4), current)
		prev_month = next(item for item in comparisons if item['key'] == 'prev_month')
		product_group = next(
			row for row in prev_month['breakdown_products'] if row['label'] == 'Breakdown Broker_USD'
		)
		self.assertEqual(product_group['current_usd'], Decimal('1000'))
		self.assertEqual(product_group['change']['baseline_usd'], Decimal('500'))
		comparison_group = next(row for row in prev_month['breakdown_groups'] if row['label'] == 'Breakdown Broker')
		self.assertEqual(comparison_group['current_usd'], Decimal('1000'))
		self.assertEqual(comparison_group['change']['baseline_usd'], Decimal('500'))

	def test_period_comparison_does_not_backfill_today_product_purchase_into_yesterday(self):
		usd = Currency.objects.get(code='USD')
		finstore = FinancialInstitution.objects.get(slug='finstore')
		account = Account.objects.get(institution=finstore, currency=usd)
		product = Product.objects.create(
			institution=finstore,
			name='Today Finstore token',
			product_type=Product.ProductType.TOKEN,
			currency=usd,
			units=Decimal('10'),
			current_price=Decimal('5'),
			current_value_usd=Decimal('50'),
			external_id='TODAY-FINSTORE-TOKEN',
		)
		Transaction.objects.create(
			account=account,
			product=product,
			transaction_type=Transaction.TransactionType.TRADE,
			currency=usd,
			import_fingerprint='today-finstore-token-buy',
			amount=Decimal('0'),
			amount_usd=Decimal('0'),
			quantity=Decimal('10'),
			unit_price=Decimal('5'),
			occurred_at=timezone.make_aware(timezone.datetime(2026, 6, 10, 12, 0)),
		)

		current = _historical_portfolio_context(date(2026, 6, 10))
		comparisons = _build_portfolio_period_comparisons(date(2026, 6, 10), current)
		prev_day = next(item for item in comparisons if item['key'] == 'prev_day')
		finstore_group = next(row for row in prev_day['breakdown_groups'] if row['label'] == 'Finstore')

		self.assertEqual(finstore_group['current_usd'], Decimal('50'))
		self.assertEqual(finstore_group['change']['baseline_usd'], Decimal('0'))

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
		self.assertGreater(response.context['products_total_usd'], Decimal('1400'))
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

	def test_account_value_as_of_reconstructs_balance_from_transactions_without_snapshots(self):
		usd = Currency.objects.get(code='USD')
		institution = FinancialInstitution.objects.create(
			name='Ledger Bank',
			slug='ledger-bank',
			institution_type=FinancialInstitution.InstitutionType.BANK,
			base_currency=usd,
		)
		account = Account.objects.create(
			institution=institution,
			name='Ledger cash',
			account_type=Account.AccountType.BANK,
			currency=usd,
			current_balance=Decimal('12.50'),
			current_balance_usd=Decimal('12.50'),
		)
		Transaction.objects.create(
			account=account,
			transaction_type=Transaction.TransactionType.INCOME,
			currency=usd,
			import_fingerprint='ledger-income-yesterday',
			amount=Decimal('10.00'),
			amount_usd=Decimal('10.00'),
			occurred_at=timezone.make_aware(datetime(2026, 6, 6, 12, 0)),
		)
		Transaction.objects.create(
			account=account,
			transaction_type=Transaction.TransactionType.INCOME,
			currency=usd,
			import_fingerprint='ledger-income-today',
			amount=Decimal('2.50'),
			amount_usd=Decimal('2.50'),
			occurred_at=timezone.make_aware(datetime(2026, 6, 7, 12, 0)),
		)

		cache = PortfolioHistoryCache.build()
		rate_cache = {}
		today_value = _account_value_as_of(account, date(2026, 6, 7), rate_cache, portfolio_cache=cache)
		yesterday_value = _account_value_as_of(account, date(2026, 6, 6), rate_cache, portfolio_cache=cache)

		self.assertEqual(today_value, Decimal('12.50'))
		self.assertEqual(yesterday_value, Decimal('10.00'))

	def test_account_balance_as_of_zero_after_stale_normalization(self):
		usd = Currency.objects.get(code='USD')
		institution = FinancialInstitution.objects.create(
			name='Stale Binance Bank',
			slug='stale-binance-bank',
			institution_type=FinancialInstitution.InstitutionType.BROKER,
			base_currency=usd,
		)
		account = Account.objects.create(
			institution=institution,
			name='Stale RWUSD',
			account_type=Account.AccountType.BROKERAGE,
			currency=usd,
			current_balance=Decimal('0'),
			current_balance_usd=Decimal('0'),
			metadata={'source': 'binance', 'stale_after_normalization': True},
		)
		Account.objects.filter(pk=account.pk).update(
			updated_at=timezone.make_aware(datetime(2026, 6, 18, 8, 0)),
		)
		account.refresh_from_db()
		BalanceSnapshot.objects.create(
			institution=institution,
			account=account,
			currency=usd,
			balance=Decimal('535.47'),
			balance_usd=Decimal('535.47'),
			captured_at=timezone.make_aware(datetime(2026, 6, 17, 16, 0)),
			metadata={'source': 'binance'},
		)

		cache = PortfolioHistoryCache.build()
		self.assertEqual(
			_account_balance_as_of(account, date(2026, 6, 17), portfolio_cache=cache),
			Decimal('535.47'),
		)
		self.assertEqual(
			_account_balance_as_of(account, date(2026, 6, 18), portfolio_cache=cache),
			Decimal('0'),
		)
		self.assertEqual(
			_account_value_as_of(account, date(2026, 6, 21), {}, portfolio_cache=cache),
			Decimal('0'),
		)

	def test_account_balance_as_of_zero_balance_from_overrides_stale_updated_at(self):
		usd = Currency.objects.get(code='USD')
		institution = FinancialInstitution.objects.create(
			name='Backdated Stale Bank',
			slug='backdated-stale-bank',
			institution_type=FinancialInstitution.InstitutionType.BROKER,
			base_currency=usd,
		)
		account = Account.objects.create(
			institution=institution,
			name='Backdated RWUSD',
			account_type=Account.AccountType.BROKERAGE,
			currency=usd,
			current_balance=Decimal('0'),
			current_balance_usd=Decimal('0'),
			metadata={
				'source': 'binance',
				'stale_after_normalization': True,
				'zero_balance_from': '2026-06-17',
			},
		)
		Account.objects.filter(pk=account.pk).update(
			updated_at=timezone.make_aware(datetime(2026, 6, 21, 8, 0)),
		)
		account.refresh_from_db()
		BalanceSnapshot.objects.create(
			institution=institution,
			account=account,
			currency=usd,
			balance=Decimal('535.47'),
			balance_usd=Decimal('535.47'),
			captured_at=timezone.make_aware(datetime(2026, 6, 16, 12, 0)),
			metadata={'source': 'binance'},
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			account=account,
			currency=usd,
			balance=Decimal('535.47'),
			balance_usd=Decimal('535.47'),
			captured_at=timezone.make_aware(datetime(2026, 6, 17, 16, 0)),
			metadata={'source': 'binance'},
		)

		cache = PortfolioHistoryCache.build()
		self.assertEqual(
			_account_balance_as_of(account, date(2026, 6, 16), portfolio_cache=cache),
			Decimal('535.47'),
		)
		self.assertEqual(
			_account_balance_as_of(account, date(2026, 6, 17), portfolio_cache=cache),
			Decimal('0'),
		)
		self.assertEqual(
			_account_balance_as_of(account, date(2026, 6, 20), portfolio_cache=cache),
			Decimal('0'),
		)

	def test_account_value_as_of_uses_local_day_boundary_for_transactions(self):
		usd = Currency.objects.get(code='USD')
		institution = FinancialInstitution.objects.create(
			name='Local Day Bank',
			slug='local-day-bank',
			institution_type=FinancialInstitution.InstitutionType.BANK,
			base_currency=usd,
		)
		account = Account.objects.create(
			institution=institution,
			name='Local day cash',
			account_type=Account.AccountType.BANK,
			currency=usd,
			current_balance=Decimal('17.00'),
			current_balance_usd=Decimal('17.00'),
		)
		Transaction.objects.create(
			account=account,
			transaction_type=Transaction.TransactionType.DEPOSIT,
			currency=usd,
			import_fingerprint='local-day-before-midnight',
			amount=Decimal('10.00'),
			amount_usd=Decimal('10.00'),
			occurred_at=timezone.make_aware(datetime(2026, 6, 10, 20, 59, 59), timezone=dt_timezone.utc),
		)
		Transaction.objects.create(
			account=account,
			transaction_type=Transaction.TransactionType.DEPOSIT,
			currency=usd,
			import_fingerprint='local-day-at-midnight',
			amount=Decimal('7.00'),
			amount_usd=Decimal('7.00'),
			occurred_at=timezone.make_aware(datetime(2026, 6, 10, 21, 0, 0), timezone=dt_timezone.utc),
		)

		cache = PortfolioHistoryCache.build()
		value = _account_value_as_of(account, date(2026, 6, 10), {}, portfolio_cache=cache)

		self.assertEqual(value, Decimal('10.00'))

	def test_product_value_as_of_uses_local_day_boundary_for_transactions(self):
		usd = Currency.objects.get(code='USD')
		institution = FinancialInstitution.objects.create(
			name='Local Day Broker',
			slug='local-day-broker',
			institution_type=FinancialInstitution.InstitutionType.BROKER,
			base_currency=usd,
		)
		account = Account.objects.create(
			institution=institution,
			name='Local day brokerage',
			account_type=Account.AccountType.BROKERAGE,
			currency=usd,
		)
		product = Product.objects.create(
			institution=institution,
			name='Local day bond',
			product_type=Product.ProductType.BOND,
			currency=usd,
			units=Decimal('2'),
			current_price=Decimal('100'),
			current_value_usd=Decimal('200'),
			external_id='LOCAL-DAY-BOND',
		)
		Transaction.objects.create(
			account=account,
			product=product,
			transaction_type=Transaction.TransactionType.TRADE,
			currency=usd,
			import_fingerprint='local-day-product-before-midnight',
			amount=Decimal('-100.00'),
			amount_usd=Decimal('-100.00'),
			quantity=Decimal('1'),
			unit_price=Decimal('100'),
			occurred_at=timezone.make_aware(datetime(2026, 6, 10, 20, 59, 59), timezone=dt_timezone.utc),
		)
		Transaction.objects.create(
			account=account,
			product=product,
			transaction_type=Transaction.TransactionType.TRADE,
			currency=usd,
			import_fingerprint='local-day-product-at-midnight',
			amount=Decimal('-100.00'),
			amount_usd=Decimal('-100.00'),
			quantity=Decimal('1'),
			unit_price=Decimal('100'),
			occurred_at=timezone.make_aware(datetime(2026, 6, 10, 21, 0, 0), timezone=dt_timezone.utc),
		)

		cache = PortfolioHistoryCache.build()
		value = _product_value_as_of(product, date(2026, 6, 10), {}, portfolio_cache=cache)

		self.assertEqual(value, Decimal('100'))

	def test_balance_snapshot_as_of_uses_local_timezone_for_day_boundary(self):
		usd = Currency.objects.get(code='USD')
		institution = FinancialInstitution.objects.create(
			name='Timezone Bank',
			slug='timezone-bank',
			institution_type=FinancialInstitution.InstitutionType.BANK,
			base_currency=usd,
		)
		account = Account.objects.create(
			institution=institution,
			name='Timezone cash',
			account_type=Account.AccountType.BANK,
			currency=usd,
			current_balance=Decimal('178.84'),
			current_balance_usd=Decimal('178.84'),
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			account=account,
			currency=usd,
			balance=Decimal('79.38'),
			balance_usd=Decimal('79.38'),
			captured_at=timezone.make_aware(datetime(2026, 6, 6, 23, 59, 59), timezone=dt_timezone.utc),
			metadata={'snapshot_type': 'spot', 'source': 'binance'},
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			account=account,
			currency=usd,
			balance=Decimal('178.83'),
			balance_usd=Decimal('178.83'),
			captured_at=timezone.make_aware(datetime(2026, 6, 6, 15, 37, 13), timezone=dt_timezone.utc),
		)

		cache = PortfolioHistoryCache.build()
		rate_cache = {}
		yesterday_value = _account_value_as_of(account, date(2026, 6, 6), rate_cache, portfolio_cache=cache)

		self.assertEqual(yesterday_value, Decimal('178.83'))

	def test_income_sources_excluded_from_portfolio_history(self):
		from apps.accounts.querysets import is_portfolio_holding_account

		income_institution = FinancialInstitution.objects.get(slug='income-sources')
		payroll_account = Account.objects.filter(institution=income_institution).first()
		self.assertIsNotNone(payroll_account)
		self.assertFalse(is_portfolio_holding_account(payroll_account))

		cache = PortfolioHistoryCache.build()
		account_institution_slugs = {account.institution.slug for account in cache.accounts}
		self.assertNotIn('income-sources', account_institution_slugs)

		today = timezone.localdate()
		comparisons = _build_portfolio_period_comparisons(
			today,
			_historical_portfolio_context(today, portfolio_cache=cache),
			portfolio_cache=cache,
		)
		prev_day = next(item for item in comparisons if item['key'] == 'prev_day')
		breakdown_labels = {row['label'] for row in prev_day['breakdown_groups']}
		self.assertNotIn('Зарплата', breakdown_labels)

	def test_period_comparison_entities_do_not_duplicate_deposit_income_account(self):
		usd = Currency.objects.get(code='USD')
		institution = FinancialInstitution.objects.create(
			name='Deposit Entity Bank',
			slug='deposit-entity-bank',
			institution_type=FinancialInstitution.InstitutionType.BANK,
			base_currency=usd,
		)
		income_account = Account.objects.create(
			institution=institution,
			name='Deposit payout account',
			account_type=Account.AccountType.BANK,
			currency=usd,
			current_balance=Decimal('100'),
			current_balance_usd=Decimal('100'),
		)
		deposit = Product.objects.create(
			institution=institution,
			income_account=income_account,
			name='Deposit principal',
			product_type=Product.ProductType.DEPOSIT,
			currency=usd,
			units=Decimal('1000'),
			current_price=Decimal('1'),
			current_value_usd=Decimal('1000'),
			external_id='DEPOSIT-ENTITY-TEST',
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			account=income_account,
			currency=usd,
			balance=Decimal('80'),
			balance_usd=Decimal('80'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 5, 31, 12, 0)),
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			account=income_account,
			currency=usd,
			balance=Decimal('100'),
			balance_usd=Decimal('100'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 6, 4, 12, 0)),
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			product=deposit,
			currency=usd,
			balance=Decimal('900'),
			balance_usd=Decimal('900'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 5, 31, 12, 0)),
		)
		BalanceSnapshot.objects.create(
			institution=institution,
			product=deposit,
			currency=usd,
			balance=Decimal('1000'),
			balance_usd=Decimal('1000'),
			captured_at=timezone.make_aware(timezone.datetime(2026, 6, 4, 12, 0)),
		)

		current = _historical_portfolio_context(date(2026, 6, 4))
		comparisons = _build_portfolio_period_comparisons(date(2026, 6, 4), current)
		prev_month = next(item for item in comparisons if item['key'] == 'prev_month')
		group = next(row for row in prev_month['breakdown_groups'] if row['label'] == 'Deposits + bank accounts')

		self.assertEqual(group['current_usd'], Decimal('1100'))
		self.assertEqual(group['change']['baseline_usd'], Decimal('980'))


class DashboardCashFlowTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def setUp(self):
		self.usd = Currency.objects.get(code='USD')
		self.institution = FinancialInstitution.objects.create(
			name='Cash Flow Test Bank',
			slug='cash-flow-test-bank',
			institution_type=FinancialInstitution.InstitutionType.BANK,
			base_currency=self.usd,
		)
		self.source_account = Account.objects.create(
			institution=self.institution,
			name='Cash Flow Source',
			currency=self.usd,
			external_id='cash-flow-source',
		)
		self.destination_account = Account.objects.create(
			institution=self.institution,
			name='Cash Flow Destination',
			currency=self.usd,
			external_id='cash-flow-destination',
		)

	def test_deposit_withdrawal_totals_for_month_and_year(self):
		as_of = date(2026, 6, 17)
		before = _build_deposit_withdrawal_totals(as_of)
		Transaction.objects.create(
			account=self.source_account,
			transaction_type=Transaction.TransactionType.DEPOSIT,
			currency=self.usd,
			amount=Decimal('100'),
			amount_usd=Decimal('100'),
			import_fingerprint='cash-flow-deposit-month',
			occurred_at=timezone.make_aware(datetime(2026, 6, 10, 12, 0)),
		)
		Transaction.objects.create(
			account=self.source_account,
			transaction_type=Transaction.TransactionType.WITHDRAWAL,
			currency=self.usd,
			amount=Decimal('-40'),
			amount_usd=Decimal('-40'),
			import_fingerprint='cash-flow-withdrawal-month',
			occurred_at=timezone.make_aware(datetime(2026, 6, 12, 12, 0)),
		)
		Transaction.objects.create(
			account=self.source_account,
			transaction_type=Transaction.TransactionType.DEPOSIT,
			currency=self.usd,
			amount=Decimal('50'),
			amount_usd=Decimal('50'),
			import_fingerprint='cash-flow-deposit-year-only',
			occurred_at=timezone.make_aware(datetime(2026, 3, 15, 12, 0)),
		)
		Transaction.objects.create(
			account=self.source_account,
			transaction_type=Transaction.TransactionType.WITHDRAWAL,
			currency=self.usd,
			amount=Decimal('-20'),
			amount_usd=Decimal('-20'),
			import_fingerprint='cash-flow-withdrawal-prior-year',
			occurred_at=timezone.make_aware(datetime(2025, 12, 31, 12, 0)),
		)

		totals = _build_deposit_withdrawal_totals(as_of)

		self.assertEqual(totals.month_deposits_usd - before.month_deposits_usd, Decimal('100'))
		self.assertEqual(totals.month_withdrawals_usd - before.month_withdrawals_usd, Decimal('40'))
		self.assertEqual(totals.year_deposits_usd - before.year_deposits_usd, Decimal('150'))
		self.assertEqual(totals.year_withdrawals_usd - before.year_withdrawals_usd, Decimal('40'))

	def test_internal_transfer_pair_is_excluded_from_cash_flows(self):
		as_of = date(2026, 6, 17)
		before = _build_deposit_withdrawal_totals(as_of)
		create_transaction(
			account=self.source_account,
			related_account=self.destination_account,
			transaction_type=Transaction.TransactionType.TRANSFER,
			currency=self.usd,
			amount=Decimal('-75'),
			occurred_at=timezone.make_aware(datetime(2026, 6, 11, 12, 0)),
			import_fingerprint='cash-flow-internal-transfer-out',
			sync_balance=False,
		)

		totals = _build_deposit_withdrawal_totals(as_of)

		self.assertEqual(totals.month_deposits_usd - before.month_deposits_usd, Decimal('0'))
		self.assertEqual(totals.month_withdrawals_usd - before.month_withdrawals_usd, Decimal('0'))

	def test_excluded_from_account_balance_withdrawal_counts_in_cash_flows(self):
		as_of = date(2026, 6, 17)
		before = _build_deposit_withdrawal_totals(as_of)
		Transaction.objects.create(
			account=self.source_account,
			transaction_type=Transaction.TransactionType.WITHDRAWAL,
			currency=self.usd,
			amount=Decimal('-850.02'),
			amount_usd=Decimal('850.02'),
			import_fingerprint='cash-flow-excluded-withdrawal',
			occurred_at=timezone.make_aware(datetime(2026, 6, 17, 12, 0)),
			description='External withdrawal',
			metadata={'exclude_from_account_balance': True, 'operation_kind': 'external_withdrawal'},
		)

		totals = _build_deposit_withdrawal_totals(as_of)

		self.assertEqual(totals.month_withdrawals_usd - before.month_withdrawals_usd, Decimal('850.02'))
		self.assertEqual(totals.year_withdrawals_usd - before.year_withdrawals_usd, Decimal('850.02'))

	def test_dashboard_metrics_include_cash_flow_widget(self):
		metrics = _dashboard_metrics()

		self.assertIn('cash_flows', metrics)
		self.assertEqual(metrics['cash_flows'].as_of_date, timezone.localdate())
