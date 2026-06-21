from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.common.models import Currency
from apps.common.services.finstore_income import (
	calculate_finstore_income_for_period,
	estimate_finstore_income_amount,
	finstore_accrual_period_for_payment,
)
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product
from apps.products.services.token_terms import estimate_next_income_amount


class FinstoreIncomeTests(TestCase):
	def setUp(self):
		self.byn = Currency.objects.create(code='BYN', name='Belarusian Ruble', symbol='Br', usd_rate=Decimal('0.31'))
		self.finstore = FinancialInstitution.objects.create(
			name='Finstore',
			slug='finstore',
			institution_type=FinancialInstitution.InstitutionType.BROKER,
		)
		self.account = Account.objects.create(
			institution=self.finstore,
			name='Finstore BYN',
			account_type=Account.AccountType.BROKERAGE,
			currency=self.byn,
		)
		self.product = Product.objects.create(
			institution=self.finstore,
			name='SMART_(BYN_868)',
			external_id='SMART_(BYN_868)',
			product_type=Product.ProductType.TOKEN,
			currency=self.byn,
			annual_rate_pct=Decimal('19.00'),
			units=Decimal('2'),
			current_price=Decimal('20'),
			income_schedule=Product.IncomeSchedule.MONTHLY,
		)

	def test_accrual_period_is_previous_calendar_month(self):
		period = finstore_accrual_period_for_payment(date(2026, 6, 15), first_holding_date=date(2026, 1, 1))
		self.assertEqual(period, (date(2026, 5, 1), date(2026, 5, 31)))

	def test_first_month_starts_on_purchase_date(self):
		period = finstore_accrual_period_for_payment(date(2026, 6, 15), first_holding_date=date(2026, 5, 15))
		self.assertEqual(period, (date(2026, 5, 15), date(2026, 5, 31)))

	def test_partial_first_month_matches_whitepaper_example(self):
		Transaction.objects.create(
			account=self.account,
			product=self.product,
			currency=self.byn,
			transaction_type=Transaction.TransactionType.TRADE,
			amount=Decimal('-40.00'),
			quantity=Decimal('2'),
			occurred_at=timezone.make_aware(datetime(2026, 5, 15, 12, 0, 0)),
			import_fingerprint='finstore-income-buy',
			metadata={'operation_type': 'Покупка'},
		)

		amount, _ = estimate_finstore_income_amount(self.product, date(2026, 6, 15))
		self.assertEqual(amount, Decimal('0.35'))

	def test_full_month_uses_actual_days_not_twelfth(self):
		self.product.annual_rate_pct = Decimal('17.00')
		self.product.units = Decimal('10')
		self.product.save(update_fields=['annual_rate_pct', 'units', 'updated_at'])
		Transaction.objects.create(
			account=self.account,
			product=self.product,
			currency=self.byn,
			transaction_type=Transaction.TransactionType.TRADE,
			amount=Decimal('-200.00'),
			quantity=Decimal('10'),
			occurred_at=timezone.make_aware(datetime(2026, 1, 1, 12, 0, 0)),
			import_fingerprint='finstore-income-full-month',
			metadata={'operation_type': 'Покупка'},
		)

		amount = calculate_finstore_income_for_period(
			self.product,
			date(2026, 5, 1),
			date(2026, 5, 31),
		)
		# 10 * 20 * 19% / 365 * 31 = 2.89
		self.assertEqual(amount, Decimal('2.89'))

	def test_token_terms_routes_finstore_products_to_whitepaper_formula(self):
		Transaction.objects.create(
			account=self.account,
			product=self.product,
			currency=self.byn,
			transaction_type=Transaction.TransactionType.TRADE,
			amount=Decimal('-40.00'),
			quantity=Decimal('2'),
			occurred_at=timezone.make_aware(datetime(2026, 5, 15, 12, 0, 0)),
			import_fingerprint='finstore-income-route-buy',
			metadata={'operation_type': 'Покупка'},
		)

		amount, _ = estimate_next_income_amount(self.product, payment_date=date(2026, 6, 15))
		self.assertEqual(amount, Decimal('0.35'))
