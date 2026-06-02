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

	def test_product_list_can_show_closed_products(self):
		response = self.client.get(reverse('products:list'), {'show_closed': '1'})

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Closed token')

	def test_product_search_includes_closed_products(self):
		response = self.client.get(reverse('products:list'), {'q': 'Closed token'})

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Closed token')
		self.assertTrue(response.context['search_includes_closed'])

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
