from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.accounts.services.balance import calculate_account_balance
from apps.common.models import Currency, ExchangeRateHistory
from apps.common.services.indexed_bonds import (
	build_income_calendar_rows,
	configure_op47_bond,
	configure_op51_bond,
	generate_coupon_payment_dates,
	refresh_indexed_bond_valuation,
)
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product


class IndexedBondTests(TestCase):
	def setUp(self):
		self.byn = Currency.objects.create(code='BYN', name='Belarusian Ruble', symbol='Br', usd_rate=Decimal('0.31'))
		self.usd = Currency.objects.create(code='USD', name='US Dollar', symbol='$', usd_rate=Decimal('1'), is_base=True)
		ExchangeRateHistory.objects.create(
			currency=self.usd,
			rate_date=date(2026, 6, 1),
			rate_byn=Decimal('2.9200'),
			usd_cross_rate=Decimal('1'),
			source=ExchangeRateHistory.Source.NBRB,
			source_currency_id=145,
		)
		self.aigenis = FinancialInstitution.objects.create(name='Aigenis', slug='aigenis', institution_type='broker')
		self.alfabank = FinancialInstitution.objects.create(name='АльфаБанк', slug='alfabank', institution_type='bank')
		self.broker_account = Account.objects.create(
			institution=self.aigenis,
			name='Aigenis BYN',
			account_type=Account.AccountType.BROKERAGE,
			currency=self.byn,
		)
		self.income_account = Account.objects.create(
			institution=self.alfabank,
			name='АльфаБанк BYN Account',
			account_type=Account.AccountType.BANK,
			currency=self.byn,
			current_balance=Decimal('0.00'),
		)
		self.product = Product.objects.create(
			institution=self.aigenis,
			name='Айгенис Оп47',
			external_id='BCSE-00477-P01',
			isin='BCSE-00477-P01',
			product_type=Product.ProductType.BOND,
			currency=self.byn,
			units=Decimal('3'),
			current_price=Decimal('500'),
			income_account=self.income_account,
			metadata={'bond_kind': 'indexed'},
		)

	def test_configure_op47_sets_terms_calendar_and_coupon_history(self):
		for index, payment_day in enumerate((date(2026, 3, 4), date(2026, 3, 13)), start=1):
			Transaction.objects.create(
				account=self.broker_account,
				product=self.product,
				currency=self.byn,
				transaction_type=Transaction.TransactionType.TRADE,
				amount=Decimal('-500.00'),
				quantity=Decimal('1'),
				occurred_at=timezone.make_aware(datetime.combine(payment_day, datetime.min.time())),
				import_fingerprint=f'op47-purchase-{index}',
			)

		configure_op47_bond(self.product)
		self.product.refresh_from_db()

		self.assertEqual(self.product.annual_rate_pct, Decimal('7.0000'))
		self.assertEqual(self.product.maturity_date, date(2029, 11, 6))
		self.assertEqual(self.product.income_schedule, Product.IncomeSchedule.QUARTERLY)
		self.assertEqual(self.product.next_income_date, date(2026, 7, 8))
		self.assertEqual(self.product.metadata['face_value_usd'], '175.3894')

		coupon_tx = Transaction.objects.get(import_fingerprint='aigenis:op47:coupon:2026-04-08')
		self.assertEqual(coupon_tx.amount, Decimal('13.55'))  # 2 × 6.7754
		self.assertEqual(coupon_tx.metadata.get('units_at_payment'), '2')
		self.assertEqual(coupon_tx.product, self.product)
		self.assertTrue(coupon_tx.metadata.get('exclude_from_account_balance'))
		self.assertEqual(calculate_account_balance(self.income_account), Decimal('0.00'))

	def test_indexed_bond_valuation_uses_usd_face_value(self):
		configure_op47_bond(self.product)
		refresh_indexed_bond_valuation(self.product)
		self.product.refresh_from_db()

		self.assertEqual(self.product.current_value_usd, Decimal('526.17'))
		self.assertEqual(self.product.current_price, Decimal('512.13704800'))

	def test_income_calendar_preview_uses_planned_usd_coupon(self):
		configure_op47_bond(self.product)
		from apps.common.services.indexed_bonds import build_income_calendar_rows, save_income_calendar_config

		save_income_calendar_config(
			self.product,
			enabled=True,
			coupon_day=8,
			schedule_start_date=date(2026, 4, 8),
			payment_amounts={
				'2026-04-08': '2.3209',
				'2026-07-08': '2.5000',
			},
		)
		rows = build_income_calendar_rows(self.product, today=date(2026, 6, 1))

		self.assertTrue(rows)
		self.assertEqual(rows[0]['date'], date(2026, 4, 8))
		self.assertEqual(rows[0]['coupon_usd_per_unit'], Decimal('2.3209'))
		self.assertEqual(rows[1]['date'], date(2026, 7, 8))
		self.assertEqual(rows[1]['coupon_usd_per_unit'], Decimal('2.5000'))
		self.assertEqual(rows[1]['amount_usd'], Decimal('7.5000'))

	def test_configure_op47_preserves_user_payment_overrides(self):
		from apps.common.services.indexed_bonds import configure_op47_bond, get_payment_schedule, save_income_calendar_config

		save_income_calendar_config(
			self.product,
			enabled=True,
			coupon_day=8,
			schedule_start_date=date(2026, 4, 8),
			payment_amounts={'2026-07-08': '9.9999'},
		)
		configure_op47_bond(self.product)
		schedule = get_payment_schedule(self.product)
		self.assertEqual(schedule['2026-07-08'], '9.9999')
		self.assertEqual(schedule['2026-04-08'], '2.3209')

	def test_final_coupon_falls_on_maturity_not_last_quarterly(self):
		self.product.maturity_date = date(2029, 11, 6)
		self.product.income_schedule = Product.IncomeSchedule.QUARTERLY
		self.product.save()
		from apps.common.services.indexed_bonds import generate_coupon_payment_dates, save_income_calendar_config

		save_income_calendar_config(
			self.product,
			enabled=True,
			coupon_day=8,
			schedule_start_date=date(2026, 4, 8),
			payment_amounts={'2029-11-06': '4.0700'},
		)
		dates = generate_coupon_payment_dates(self.product)
		self.assertNotIn(date(2029, 10, 8), dates)
		self.assertEqual(dates[-1], date(2029, 11, 6))

	def test_generate_full_coupon_schedule_until_maturity(self):
		self.product.maturity_date = date(2027, 4, 8)
		self.product.income_schedule = Product.IncomeSchedule.QUARTERLY
		self.product.save()
		from apps.common.services.indexed_bonds import generate_coupon_payment_dates, save_income_calendar_config

		save_income_calendar_config(
			self.product,
			enabled=True,
			coupon_day=8,
			schedule_start_date=date(2026, 4, 8),
			payment_amounts={},
		)
		dates = generate_coupon_payment_dates(self.product)
		self.assertEqual(dates, [date(2026, 4, 8), date(2026, 7, 8), date(2026, 10, 8), date(2027, 1, 8), date(2027, 4, 8)])


class Op51IndexedBondTests(TestCase):
	def setUp(self):
		self.byn = Currency.objects.create(code='BYN', name='Belarusian Ruble', symbol='Br', usd_rate=Decimal('0.31'))
		self.usd = Currency.objects.create(code='USD', name='US Dollar', symbol='$', usd_rate=Decimal('1'), is_base=True)
		ExchangeRateHistory.objects.create(
			currency=self.usd,
			rate_date=date(2026, 6, 1),
			rate_byn=Decimal('2.9200'),
			usd_cross_rate=Decimal('1'),
			source=ExchangeRateHistory.Source.NBRB,
			source_currency_id=145,
		)
		self.aigenis = FinancialInstitution.objects.create(name='Aigenis', slug='aigenis-op51', institution_type='broker')
		self.alfabank = FinancialInstitution.objects.create(name='АльфаБанк', slug='alfabank-op51', institution_type='bank')
		self.broker_account = Account.objects.create(
			institution=self.aigenis,
			name='Aigenis BYN',
			account_type=Account.AccountType.BROKERAGE,
			currency=self.byn,
		)
		self.income_account = Account.objects.create(
			institution=self.alfabank,
			name='АльфаБанк BYN Account',
			account_type=Account.AccountType.BANK,
			currency=self.byn,
			current_balance=Decimal('0.00'),
		)
		self.product = Product.objects.create(
			institution=self.aigenis,
			name='Айгенис Оп51',
			external_id='BCSE-00487-P02',
			isin='BCSE-00487-P02',
			product_type=Product.ProductType.BOND,
			currency=self.byn,
			units=Decimal('4'),
			current_price=Decimal('300'),
			income_account=self.income_account,
			metadata={'bond_kind': 'indexed'},
		)

	def test_configure_op51_sets_terms_and_full_calendar(self):
		for index, payment_day in enumerate(
			(date(2026, 4, 25), date(2026, 5, 8), date(2026, 5, 25)),
			start=1,
		):
			Transaction.objects.create(
				account=self.broker_account,
				product=self.product,
				currency=self.byn,
				transaction_type=Transaction.TransactionType.TRADE,
				amount=Decimal('-300.00'),
				quantity=Decimal('1') if index != 2 else Decimal('2'),
				occurred_at=timezone.make_aware(datetime.combine(payment_day, datetime.min.time())),
				import_fingerprint=f'op51-purchase-{index}',
			)

		configure_op51_bond(self.product)
		self.product.refresh_from_db()

		self.assertEqual(self.product.annual_rate_pct, Decimal('7.0000'))
		self.assertEqual(self.product.maturity_date, date(2031, 12, 15))
		self.assertEqual(self.product.income_schedule, Product.IncomeSchedule.QUARTERLY)
		self.assertEqual(self.product.next_income_date, date(2026, 8, 16))
		self.assertEqual(self.product.metadata['face_value_usd'], '106.9900')
		self.assertEqual(self.product.metadata['placement_fx_rate'], '2.8040')
		self.assertEqual(self.product.metadata['income_calendar']['coupon_day'], 16)

		dates = generate_coupon_payment_dates(self.product)
		self.assertEqual(dates[0], date(2026, 8, 16))
		self.assertNotIn(date(2031, 11, 16), dates)
		self.assertEqual(dates[-1], date(2031, 12, 15))
		self.assertEqual(len(dates), 22)

		rows = build_income_calendar_rows(self.product, today=date(2026, 6, 1))
		self.assertEqual(rows[0]['coupon_usd_per_unit'], Decimal('2.3391'))
		self.assertEqual(rows[0]['units'], Decimal('4'))
		self.assertEqual(rows[-1]['coupon_usd_per_unit'], Decimal('2.4828'))
		self.assertTrue(rows[-1]['is_maturity_coupon'])

	def test_indexed_bond_valuation_uses_op51_usd_face_value(self):
		configure_op51_bond(self.product)
		refresh_indexed_bond_valuation(self.product)
		self.product.refresh_from_db()

		self.assertEqual(self.product.current_value_usd, Decimal('427.96'))
		self.assertEqual(self.product.current_price, Decimal('312.41080000'))
