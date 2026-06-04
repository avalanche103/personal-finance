from decimal import Decimal
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.common.models import Currency, ExchangeRateHistory
from apps.institutions.models import FinancialInstitution
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

	def test_product_detail_filters_transactions_by_date_and_shows_position_summary(self):
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

		response = self.client.get(
			reverse('products:detail', args=[self.product_usd.pk]),
			{'from': (timezone.localdate() - timedelta(days=5)).isoformat()},
		)

		self.assertEqual(response.status_code, 200)
		self.assertNotContains(response, old_trade.description)
		self.assertContains(response, recent_redemption.description)
		self.assertContains(response, 'Average entry price')
		self.assertEqual(response.context['position_summary']['avg_entry_price'], Decimal('100'))
		self.assertEqual(response.context['position_summary']['redeemed_units'], Decimal('2'))
		self.assertIsNotNone(response.context['performance_summary']['total_return_pct'])
