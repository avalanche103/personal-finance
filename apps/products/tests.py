from decimal import Decimal
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.common.models import Currency, ExchangeRateHistory
from apps.institutions.models import FinancialInstitution
from apps.products.analytics import (
	allocation_instrument_choices,
	build_portfolio_allocation,
	build_product_performance_summary,
	build_product_position_summary,
	extract_token_issuer,
)
from apps.products.models import Product


class ProductViewsTests(TestCase):
	def setUp(self):
		self.usd = Currency.objects.create(code='USD', name='US Dollar', symbol='$', usd_rate=Decimal('1'), is_base=True)
		self.byn = Currency.objects.create(code='BYN', name='Belarusian Ruble', symbol='Br', usd_rate=Decimal('0.31'))
		self.finstore = FinancialInstitution.objects.create(name='Finstore', institution_type=FinancialInstitution.InstitutionType.BROKER)
		self.other_institution = FinancialInstitution.objects.create(name='Alfa', institution_type=FinancialInstitution.InstitutionType.BANK)
		self.account = Account.objects.create(
			institution=self.finstore,
			name='Brokerage cash',
			account_type=Account.AccountType.BROKERAGE,
			currency=self.usd,
		)
		self.product_usd = Product.objects.create(
			institution=self.finstore,
			name='Bond USD',
			symbol='BONDUSD',
			product_type=Product.ProductType.BOND,
			currency=self.usd,
			units=Decimal('10'),
			current_price=Decimal('100'),
			current_value_usd=Decimal('1000'),
			metadata={'issuer': 'Finstore'},
		)
		self.product_byn = Product.objects.create(
			institution=self.finstore,
			name='Bond BYN',
			symbol='BONDBYN',
			product_type=Product.ProductType.BOND,
			currency=self.byn,
			units=Decimal('20'),
			current_price=Decimal('50'),
			current_value_usd=Decimal('310'),
		)
		self.other_product = Product.objects.create(
			institution=self.other_institution,
			name='Other asset',
			product_type=Product.ProductType.ETF,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('200'),
			current_value_usd=Decimal('200'),
		)
		self.closed_product = Product.objects.create(
			institution=self.finstore,
			name='Closed token',
			product_type=Product.ProductType.TOKEN,
			currency=self.usd,
			units=Decimal('0'),
			current_price=Decimal('100'),
			current_value_usd=Decimal('0'),
			is_active=False,
		)

	def test_product_list_groups_by_institution_and_currency(self):
		Transaction.objects.create(
			account=self.account,
			product=self.product_usd,
			transaction_type=Transaction.TransactionType.TRADE,
			currency=self.usd,
			import_fingerprint='products-test-list-usd-buy',
			amount=Decimal('-1000'),
			amount_usd=Decimal('-1000'),
			quantity=Decimal('10'),
			unit_price=Decimal('100'),
			occurred_at=timezone.now() - timedelta(days=5),
		)
		Transaction.objects.create(
			account=self.account,
			product=self.product_usd,
			transaction_type=Transaction.TransactionType.INCOME,
			currency=self.usd,
			import_fingerprint='products-test-list-usd-income',
			amount=Decimal('50'),
			amount_usd=Decimal('50'),
			quantity=Decimal('0'),
			unit_price=Decimal('0'),
			occurred_at=timezone.now() - timedelta(days=1),
		)
		response = self.client.get(reverse('products:list'))

		self.assertEqual(response.status_code, 200)
		groups = response.context['product_groups']
		labels = [group['label'] for group in groups]

		self.assertEqual(labels, ['Alfa_USD', 'Finstore_BYN', 'Finstore_USD'])
		self.assertEqual(groups[1]['total_value_native'], Decimal('1000'))
		self.assertEqual(groups[2]['total_value_native'], Decimal('1000'))
		self.assertEqual(groups[2]['total_return_value'], Decimal('50'))
		self.assertEqual(groups[2]['total_return_pct'], Decimal('5'))
		self.assertIsNotNone(groups[2]['xirr_pct'])
		self.assertContains(response, reverse('products:detail', args=[self.product_usd.pk]))
		self.assertContains(response, 'Finstore_USD')
		self.assertContains(response, 'Finstore_BYN')
		self.assertContains(response, 'Return')
		self.assertContains(response, 'XIRR')
		self.assertNotContains(response, 'Closed token')

	def test_product_list_sorts_products_within_group_by_value_usd(self):
		Product.objects.create(
			institution=self.finstore,
			name='Small bond',
			product_type=Product.ProductType.BOND,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('100'),
			current_value_usd=Decimal('100'),
		)
		Product.objects.create(
			institution=self.finstore,
			name='Large bond',
			product_type=Product.ProductType.BOND,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('500'),
			current_value_usd=Decimal('500'),
		)

		response = self.client.get(reverse('products:list'))
		group = next(item for item in response.context['product_groups'] if item['label'] == 'Finstore_USD')
		names = [product.name for product in group['products']]

		self.assertEqual(names, ['Bond USD', 'Large bond', 'Small bond'])

	def test_product_list_can_sort_by_maturity_date(self):
		from datetime import date

		early_bond = Product.objects.create(
			institution=self.finstore,
			name='Early bond',
			product_type=Product.ProductType.BOND,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('100'),
			current_value_usd=Decimal('100'),
			maturity_date=date(2026, 1, 15),
		)
		self.product_usd.maturity_date = date(2028, 6, 1)
		self.product_usd.save(update_fields=['maturity_date', 'updated_at'])

		response = self.client.get(
			reverse('products:list'),
			{'sort': 'maturity_date', 'dir': 'asc'},
		)
		self.assertEqual(response.status_code, 200)
		group = next(item for item in response.context['product_groups'] if item['label'] == 'Finstore_USD')
		names = [product.name for product in group['products']]
		self.assertEqual(names[0], early_bond.name)
		self.assertEqual(names[1], self.product_usd.name)

		response_desc = self.client.get(
			reverse('products:list'),
			{'sort': 'maturity_date', 'dir': 'desc'},
		)
		group_desc = next(item for item in response_desc.context['product_groups'] if item['label'] == 'Finstore_USD')
		names_desc = [product.name for product in group_desc['products']]
		self.assertEqual(names_desc[0], self.product_usd.name)
		self.assertEqual(names_desc[1], early_bond.name)

	def test_product_list_can_sort_by_name(self):
		Product.objects.create(
			institution=self.finstore,
			name='Alpha bond',
			product_type=Product.ProductType.BOND,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('1000'),
			current_value_usd=Decimal('1000'),
		)
		Product.objects.create(
			institution=self.finstore,
			name='Zeta bond',
			product_type=Product.ProductType.BOND,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('100'),
			current_value_usd=Decimal('100'),
		)

		response = self.client.get(reverse('products:list'), {'sort': 'name', 'dir': 'asc'})
		group = next(item for item in response.context['product_groups'] if item['label'] == 'Finstore_USD')
		names = [product.name for product in group['products']]

		self.assertEqual(names[0], 'Alpha bond')
		self.assertEqual(names[-1], 'Zeta bond')

	def test_product_list_shows_value_byn_for_byn_group(self):
		response = self.client.get(reverse('products:list'))

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Value BYN')
		group = next(item for item in response.context['product_groups'] if item['label'] == 'Finstore_BYN')
		product = group['products'][0]
		self.assertEqual(product.market_value, Decimal('1000'))
		self.assertContains(response, 'Value BYN')
		self.assertContains(response, '1000,00 BYN')

	def test_product_list_calculates_non_usd_return_in_usd(self):
		Transaction.objects.create(
			account=self.account,
			product=self.product_byn,
			transaction_type=Transaction.TransactionType.TRADE,
			currency=self.byn,
			import_fingerprint='products-test-list-byn-buy',
			amount=Decimal('-1000'),
			amount_usd=Decimal('-300'),
			quantity=Decimal('20'),
			unit_price=Decimal('50'),
			occurred_at=timezone.now() - timedelta(days=20),
		)
		Transaction.objects.create(
			account=self.account,
			product=self.product_byn,
			transaction_type=Transaction.TransactionType.INCOME,
			currency=self.byn,
			import_fingerprint='products-test-list-byn-income',
			amount=Decimal('100'),
			amount_usd=Decimal('35'),
			quantity=Decimal('0'),
			unit_price=Decimal('0'),
			occurred_at=timezone.now() - timedelta(days=5),
		)

		response = self.client.get(reverse('products:list'))

		self.assertEqual(response.status_code, 200)
		group = next(item for item in response.context['product_groups'] if item['label'] == 'Finstore_BYN')
		self.assertEqual(group['total_return_value'], Decimal('45'))
		self.assertEqual(group['total_return_pct'], Decimal('15'))
		self.assertIsNotNone(group['xirr_pct'])

		detail_response = self.client.get(reverse('products:detail', args=[self.product_byn.pk]))
		self.assertEqual(detail_response.context['performance_summary']['total_return_value'], Decimal('45'))
		self.assertEqual(detail_response.context['performance_summary']['total_return_pct'], Decimal('15'))

	def test_product_list_can_show_closed_products(self):
		response = self.client.get(reverse('products:list'), {'show_closed': '1'})

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Closed token')

	def test_product_search_includes_closed_products(self):
		response = self.client.get(reverse('products:list'), {'q': 'Closed token'})

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Closed token')
		self.assertTrue(response.context['search_includes_closed'])

	def test_extract_token_issuer_parses_finstore_token_name(self):
		product = Product(external_id='SMART_(BYN_868)', name='SMART_(BYN_868)', metadata={})
		self.assertEqual(extract_token_issuer(product), 'SMART')

	def test_extract_token_issuer_uses_metadata_when_present(self):
		product = Product(external_id='SMART_(BYN_868)', metadata={'issuer': 'Custom issuer'})
		self.assertEqual(extract_token_issuer(product), 'Custom issuer')

	def test_build_portfolio_allocation_groups_by_institution_group_and_issuer(self):
		token_a = Product.objects.create(
			institution=self.finstore,
			name='POLESIE_(USD_676)',
			external_id='POLESIE_(USD_676)',
			product_type=Product.ProductType.TOKEN,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('300'),
			current_value_usd=Decimal('300'),
		)
		token_b = Product.objects.create(
			institution=self.finstore,
			name='POLESIE_(USD_626)',
			external_id='POLESIE_(USD_626)',
			product_type=Product.ProductType.TOKEN,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('200'),
			current_value_usd=Decimal('200'),
		)

		allocation = build_portfolio_allocation(
			[self.product_usd, self.product_byn, self.other_product, token_a, token_b]
		)

		institutions = {row['label']: row for row in allocation['by_institution']}
		self.assertEqual(institutions['Finstore']['value_usd'], Decimal('1810'))
		self.assertEqual(institutions['Alfa']['value_usd'], Decimal('200'))

		groups = {row['label']: row for row in allocation['by_group']}
		self.assertEqual(groups['Finstore_USD']['value_usd'], Decimal('1500'))
		self.assertEqual(groups['Finstore_BYN']['value_usd'], Decimal('310'))

		issuers = {row['label']: row for row in allocation['by_issuer']}
		self.assertEqual(issuers['POLESIE']['value_usd'], Decimal('500'))

	def test_build_portfolio_allocation_limits_issuers_to_top_ten(self):
		tokens = [
			Product.objects.create(
				institution=self.finstore,
				name=f'ISSUER{i}_(USD_{i})',
				external_id=f'ISSUER{i}_(USD_{i})',
				product_type=Product.ProductType.TOKEN,
				currency=self.usd,
				units=Decimal('1'),
				current_price=Decimal(str(100 - i)),
				current_value_usd=Decimal(str(100 - i)),
			)
			for i in range(12)
		]

		allocation = build_portfolio_allocation(tokens)

		self.assertEqual(len(allocation['by_issuer']), 10)
		self.assertEqual(allocation['by_issuer'][0]['label'], 'ISSUER0')
		self.assertEqual(allocation['by_issuer'][-1]['label'], 'ISSUER9')

	def test_build_portfolio_allocation_filters_by_instrument_type(self):
		token_a = Product.objects.create(
			institution=self.finstore,
			name='POLESIE_(USD_676)',
			external_id='POLESIE_(USD_676)',
			product_type=Product.ProductType.TOKEN,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('300'),
			current_value_usd=Decimal('300'),
		)
		token_b = Product.objects.create(
			institution=self.finstore,
			name='POLESIE_(USD_626)',
			external_id='POLESIE_(USD_626)',
			product_type=Product.ProductType.TOKEN,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('200'),
			current_value_usd=Decimal('200'),
		)
		bond = Product.objects.create(
			institution=self.finstore,
			name='Айгенис Оп47',
			external_id='BCSE-00477-P01',
			product_type=Product.ProductType.BOND,
			currency=self.byn,
			units=Decimal('3'),
			current_price=Decimal('500'),
			current_value_usd=Decimal('526.17'),
			metadata={'issuer': 'Айгенис закрытое акционерное общество'},
		)
		products = [self.product_usd, self.product_byn, self.other_product, token_a, token_b, bond]

		bond_allocation = build_portfolio_allocation(products, instrument_type=Product.ProductType.BOND)
		token_allocation = build_portfolio_allocation(products, instrument_type=Product.ProductType.TOKEN)

		self.assertEqual(bond_allocation['total_usd'], Decimal('1836.17'))
		self.assertEqual(bond_allocation['product_count'], 3)
		self.assertEqual(bond_allocation['instrument_type'], 'bond')
		self.assertEqual(bond_allocation['instrument_label'], 'Bond')

		issuers = {row['label']: row for row in bond_allocation['by_issuer']}
		self.assertEqual(issuers['Aigenis']['value_usd'], Decimal('526.17'))
		self.assertAlmostEqual(float(issuers['Aigenis']['share_pct']), 526.17 / 1836.17 * 100, places=1)

		self.assertEqual(token_allocation['total_usd'], Decimal('500'))
		self.assertEqual(token_allocation['product_count'], 2)
		token_issuers = {row['label']: row for row in token_allocation['by_issuer']}
		self.assertEqual(token_issuers['POLESIE']['share_pct'], Decimal('100'))

	def test_build_portfolio_allocation_includes_bond_issuers(self):
		bond = Product.objects.create(
			institution=self.finstore,
			name='Айгенис Оп47',
			external_id='BCSE-00477-P01',
			product_type=Product.ProductType.BOND,
			currency=self.byn,
			units=Decimal('3'),
			current_price=Decimal('500'),
			current_value_usd=Decimal('526.17'),
			metadata={'issuer': 'Айгенис закрытое акционерное общество'},
		)

		allocation = build_portfolio_allocation([bond])

		issuers = {row['label']: row for row in allocation['by_issuer']}
		self.assertEqual(issuers['Aigenis']['value_usd'], Decimal('526.17'))

	def test_build_portfolio_allocation_includes_pension_issuer(self):
		stravita = FinancialInstitution.objects.create(
			name='Стравита',
			slug='stravita',
			institution_type=FinancialInstitution.InstitutionType.INSURANCE,
		)
		pension = Product.objects.create(
			institution=stravita,
			name='ДНПС EP-0004390',
			external_id='3040282A000PB5',
			product_type=Product.ProductType.PENSION,
			currency=self.byn,
			units=Decimal('1'),
			current_price=Decimal('4815.42'),
			current_value_usd=Decimal('1705.78'),
		)

		allocation = build_portfolio_allocation([pension])

		self.assertEqual(extract_token_issuer(pension), 'Стравита')
		issuers = {row['label']: row for row in allocation['by_issuer']}
		self.assertEqual(issuers['Стравита']['value_usd'], Decimal('1705.78'))

	def test_product_list_shows_assets_analysis(self):
		response = self.client.get(reverse('products:list'))

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Assets Analysis')
		self.assertContains(response, 'Institutions')
		self.assertContains(response, 'Product groups')
		self.assertContains(response, 'Issuers')
		self.assertContains(response, 'allocation-type-filter')
		self.assertIn('portfolio_allocation', response.context)

	def test_allocation_instrument_choices_lists_only_present_product_types(self):
		choices = allocation_instrument_choices(
			[self.product_usd, self.product_byn, self.other_product]
		)

		self.assertEqual(choices, [
			(Product.ProductType.BOND, 'Bond'),
			(Product.ProductType.ETF, 'ETF'),
		])

	def test_product_list_shows_only_present_allocation_instrument_types(self):
		response = self.client.get(reverse('products:list'))

		self.assertContains(response, 'value="bond"')
		self.assertContains(response, 'value="etf"')
		self.assertNotContains(response, 'value="token"')
		self.assertNotContains(response, 'value="crypto"')

	def test_product_list_filters_assets_analysis_by_instrument_type(self):
		response = self.client.get(reverse('products:list'), {'allocation_type': 'bond'})

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context['allocation_type'], 'bond')
		self.assertEqual(response.context['portfolio_allocation']['instrument_type'], 'bond')
		self.assertContains(response, 'within bonds')

	def test_product_detail_navigates_between_products(self):
		product_two = Product.objects.create(
			institution=self.finstore,
			name='Second token',
			symbol='SEC',
			product_type=Product.ProductType.TOKEN,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('5'),
			current_value_usd=Decimal('5'),
		)
		product_three = Product.objects.create(
			institution=self.finstore,
			name='Third token',
			symbol='THR',
			product_type=Product.ProductType.TOKEN,
			currency=self.usd,
			units=Decimal('1'),
			current_price=Decimal('50'),
			current_value_usd=Decimal('50'),
		)

		response = self.client.get(
			reverse('products:detail', args=[product_two.pk]),
			{'sort': 'name', 'dir': 'asc'},
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context['prev_product'], self.product_usd)
		self.assertEqual(response.context['next_product'], product_three)
		self.assertContains(response, f'href="/products/{self.product_usd.pk}/?sort=name&amp;dir=asc"')
		self.assertContains(response, f'href="/products/{product_three.pk}/?sort=name&amp;dir=asc"')
		self.assertContains(response, 'nav-arrow')
		self.assertContains(response, 'is-disabled', count=0)

	def test_product_detail_saves_token_terms(self):
		response = self.client.post(
			reverse('products:detail', args=[self.product_usd.pk]),
			{
				'action': 'save_terms',
				'annual_rate_pct': '11.5',
				'maturity_date': '2028-06-01',
				'income_schedule': Product.IncomeSchedule.QUARTERLY,
				'next_income_date': '2026-07-01',
			},
		)

		self.assertEqual(response.status_code, 302)
		self.product_usd.refresh_from_db()
		self.assertEqual(self.product_usd.annual_rate_pct, Decimal('11.5000'))
		self.assertEqual(str(self.product_usd.maturity_date), '2028-06-01')
		self.assertEqual(self.product_usd.income_schedule, Product.IncomeSchedule.QUARTERLY)
		self.assertEqual(str(self.product_usd.next_income_date), '2026-07-01')
		self.assertIsNotNone(self.product_usd.terms_updated_at)

		follow_up = self.client.get(reverse('products:detail', args=[self.product_usd.pk]))
		self.assertContains(follow_up, 'Token terms')
		self.assertContains(follow_up, 'name="annual_rate_pct"')
		self.assertContains(follow_up, '11.5')

	def test_product_detail_shows_transactions_snapshots_and_rates(self):
		occurred_at = timezone.now() - timedelta(days=1)
		Transaction.objects.create(
			account=self.account,
			product=self.product_usd,
			transaction_type=Transaction.TransactionType.TRADE,
			currency=self.usd,
			import_fingerprint='products-test-primary-placement',
			amount=Decimal('-1000'),
			amount_usd=Decimal('-1000'),
			quantity=Decimal('10'),
			unit_price=Decimal('100'),
			occurred_at=occurred_at,
			description='Primary placement',
		)
		BalanceSnapshot.objects.create(
			institution=self.finstore,
			account=self.account,
			product=self.product_usd,
			currency=self.usd,
			balance=Decimal('10'),
			balance_usd=Decimal('1000'),
			captured_at=timezone.now(),
		)
		ExchangeRateHistory.objects.create(
			currency=self.usd,
			rate_date=timezone.now().date(),
			rate_byn=Decimal('3.25'),
			usd_cross_rate=Decimal('1'),
			scale=1,
			source_currency_id=431,
		)

		response = self.client.get(reverse('products:detail', args=[self.product_usd.pk]))

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Primary placement')
		self.assertContains(response, 'Brokerage cash')
		self.assertContains(response, 'issuer')
		self.assertContains(response, '3.250000')
		self.assertContains(response, 'Total return %')
		self.assertContains(response, 'XIRR')

	def test_product_detail_shows_all_transactions_and_position_summary(self):
		old_trade = Transaction.objects.create(
			account=self.account,
			product=self.product_usd,
			transaction_type=Transaction.TransactionType.TRADE,
			currency=self.usd,
			import_fingerprint='products-test-old-trade',
			amount=Decimal('-800'),
			amount_usd=Decimal('-800'),
			quantity=Decimal('8'),
			unit_price=Decimal('100'),
			occurred_at=timezone.now() - timedelta(days=20),
			description='Older trade',
		)
		recent_redemption = Transaction.objects.create(
			account=self.account,
			product=self.product_usd,
			transaction_type=Transaction.TransactionType.INCOME,
			currency=self.usd,
			import_fingerprint='products-test-recent-redemption',
			amount=Decimal('220'),
			amount_usd=Decimal('220'),
			quantity=Decimal('-2'),
			unit_price=Decimal('100'),
			occurred_at=timezone.now() - timedelta(days=2),
			description='Recent redemption',
		)

		response = self.client.get(reverse('products:detail', args=[self.product_usd.pk]))

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, old_trade.description)
		self.assertContains(response, recent_redemption.description)
		self.assertContains(response, 'Average entry price')
		self.assertEqual(response.context['position_summary']['avg_entry_price'], Decimal('100'))
		self.assertEqual(response.context['position_summary']['redeemed_units'], Decimal('2'))
		self.assertIsNotNone(response.context['performance_summary']['total_return_pct'])


class ProductPositionSummaryTests(TestCase):
	def _sample_transactions_with_fee(self):
		return [
			Transaction(
				transaction_type=Transaction.TransactionType.TRADE,
				amount=Decimal('-1000'),
				amount_usd=Decimal('-1000'),
				quantity=Decimal('10'),
				occurred_at=timezone.now(),
			),
			Transaction(
				transaction_type=Transaction.TransactionType.FEE,
				amount=Decimal('-5'),
				amount_usd=Decimal('-5'),
				quantity=Decimal('0'),
				occurred_at=timezone.now(),
			),
		]

	def test_bond_purchase_fees_increase_cost_basis_and_reduce_total_return(self):
		transactions = self._sample_transactions_with_fee()
		summary = build_product_position_summary(
			transactions,
			market_value=Decimal('1100'),
			market_value_usd=Decimal('1100'),
			product_type=Product.ProductType.BOND,
		)
		performance = build_product_performance_summary(transactions, summary)

		self.assertEqual(summary['purchase_cost'], Decimal('1005'))
		self.assertEqual(summary['purchase_cost_usd'], Decimal('1005'))
		self.assertEqual(summary['avg_entry_price'], Decimal('100.5'))
		self.assertEqual(summary['open_cost_basis_usd'], Decimal('1005'))
		self.assertEqual(summary['unrealized_pnl_usd'], Decimal('95'))
		self.assertEqual(performance['total_return_value'], Decimal('95'))

	def test_token_purchase_fees_are_excluded_from_cost_basis(self):
		transactions = self._sample_transactions_with_fee()
		summary = build_product_position_summary(
			transactions,
			market_value=Decimal('1100'),
			market_value_usd=Decimal('1100'),
			product_type=Product.ProductType.TOKEN,
		)

		self.assertEqual(summary['purchase_cost'], Decimal('1000'))
		self.assertEqual(summary['purchase_cost_usd'], Decimal('1000'))
		self.assertEqual(summary['avg_entry_price'], Decimal('100'))

	def test_pension_xirr_uses_negative_employee_contributions(self):
		account = Account.objects.create(
			institution=FinancialInstitution.objects.create(name='Income', institution_type=FinancialInstitution.InstitutionType.OTHER),
			name='Payroll',
			currency=Currency.objects.create(code='BYN', name='Belarusian Ruble', symbol='Br', usd_rate=Decimal('0.31')),
		)
		product = Product.objects.create(
			institution=FinancialInstitution.objects.create(name='Stravita', institution_type=FinancialInstitution.InstitutionType.INSURANCE),
			name='DNPS',
			product_type=Product.ProductType.PENSION,
			currency=account.currency,
			units=Decimal('1'),
			current_price=Decimal('300'),
			current_value_usd=Decimal('93'),
		)
		transactions = [
			Transaction(
				account=account,
				product=product,
				transaction_type=Transaction.TransactionType.DEPOSIT,
				currency=account.currency,
				amount=Decimal('200'),
				amount_usd=Decimal('62'),
				occurred_at=timezone.now() - timedelta(days=180),
				metadata={'employee_share_byn': '100', 'employer_share_byn': '100'},
			),
			Transaction(
				account=account,
				product=product,
				transaction_type=Transaction.TransactionType.INCOME,
				currency=account.currency,
				amount=Decimal('50'),
				amount_usd=Decimal('15.5'),
				occurred_at=timezone.now() - timedelta(days=1),
				metadata={'income_kind': 'insurance_bonus'},
			),
		]
		summary = build_product_position_summary(
			transactions,
			market_value=Decimal('300'),
			market_value_usd=Decimal('93'),
			product_type=Product.ProductType.PENSION,
		)
		performance = build_product_performance_summary(
			transactions,
			summary,
			as_of_date=timezone.localdate(),
			product_type=Product.ProductType.PENSION,
		)

		self.assertEqual(summary['purchase_cost'], Decimal('100'))
		self.assertIsNotNone(performance['xirr'])
		self.assertIsNotNone(performance['xirr_pct'])
