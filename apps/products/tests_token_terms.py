from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.common.models import Currency
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product
from apps.products.services.token_terms import (
	estimate_next_income_amount,
	estimate_next_income_date,
	import_token_terms_from_file,
	infer_schedule_from_payment_dates,
	income_payment_dates,
	load_token_terms_rows,
	recompute_next_income_dates,
)


class TokenTermsServiceTests(TestCase):
	def setUp(self):
		self.usd = Currency.objects.create(code='USD', name='US Dollar', symbol='$', usd_rate=Decimal('1'), is_base=True)
		self.finstore = FinancialInstitution.objects.create(
			name='Finstore',
			slug='finstore',
			institution_type=FinancialInstitution.InstitutionType.BROKER,
		)
		self.account = Account.objects.create(
			institution=self.finstore,
			name='Finstore USD',
			account_type=Account.AccountType.BROKERAGE,
			currency=self.usd,
		)
		self.product = Product.objects.create(
			institution=self.finstore,
			name='SMART_(BYN_868)',
			external_id='SMART_(BYN_868)',
			symbol='SMART',
			product_type=Product.ProductType.TOKEN,
			currency=self.usd,
			metadata={'imported_from': 'finstore-history', 'token_id': '868'},
		)

	def test_import_csv_updates_terms(self):
		csv_body = (
			'external_id,annual_rate_pct,maturity_date,income_schedule\n'
			'SMART_(BYN_868),19.00,2031-12-01,monthly\n'
		)
		with TemporaryDirectory() as tmp:
			path = Path(tmp) / 'terms.csv'
			path.write_text(csv_body, encoding='utf-8')

			result = import_token_terms_from_file(path, recompute_dates=False)

		self.product.refresh_from_db()
		self.assertEqual(result.matched, 1)
		self.assertEqual(result.updated, 1)
		self.assertEqual(self.product.annual_rate_pct, Decimal('19.0000'))
		self.assertEqual(self.product.maturity_date, date(2031, 12, 1))
		self.assertEqual(self.product.income_schedule, Product.IncomeSchedule.MONTHLY)
		self.assertIsNotNone(self.product.terms_updated_at)

	def test_recompute_next_income_date_from_history(self):
		self.product.income_schedule = Product.IncomeSchedule.MONTHLY
		self.product.save(update_fields=['income_schedule', 'updated_at'])

		Transaction.objects.create(
			account=self.account,
			product=self.product,
			currency=self.usd,
			transaction_type=Transaction.TransactionType.INCOME,
			amount=Decimal('1.00'),
			quantity=Decimal('0'),
			occurred_at=timezone.make_aware(datetime(2026, 4, 20, 3, 0, 0)),
			metadata={'operation_type': 'Получение дохода', 'token_name': self.product.external_id},
		)

		last_payment = date(2026, 4, 20)
		estimated = estimate_next_income_date(self.product, today=date(2026, 5, 1))
		self.assertEqual(estimated, date(2026, 5, 20))

		updated = recompute_next_income_dates(self.finstore, overwrite=True, today=date(2026, 5, 1))
		self.product.refresh_from_db()
		self.assertEqual(updated, 1)
		self.assertEqual(self.product.next_income_date, date(2026, 5, 20))
		transaction = Transaction.objects.get(product=self.product)
		self.assertEqual(timezone.localdate(transaction.occurred_at), last_payment)

	def test_estimate_next_income_amount_from_annual_rate(self):
		self.product.annual_rate_pct = Decimal('19.00')
		self.product.units = Decimal('10')
		self.product.current_price = Decimal('100')
		self.product.current_value_usd = Decimal('300')
		self.product.income_schedule = Product.IncomeSchedule.MONTHLY
		self.product.save()

		amount, amount_usd = estimate_next_income_amount(self.product, payment_dates=[])
		# 10 * 100 * 19% / 12 = 15.83
		self.assertEqual(amount, Decimal('15.83'))
		self.assertEqual(amount_usd, Decimal('4.75'))

	def test_infer_monthly_schedule_from_payment_gaps(self):
		dates = [date(2026, 2, 20), date(2026, 3, 20), date(2026, 4, 20)]
		self.assertEqual(infer_schedule_from_payment_dates(dates), Product.IncomeSchedule.MONTHLY)

	def test_infer_twice_monthly_schedule_from_payment_gaps(self):
		dates = [date(2026, 2, 1), date(2026, 2, 16), date(2026, 3, 1)]
		self.assertEqual(infer_schedule_from_payment_dates(dates), Product.IncomeSchedule.TWICE_MONTHLY)

	def test_twice_monthly_income_amount_uses_24_periods(self):
		from apps.products.services.token_terms import schedule_payments_per_year

		self.product.annual_rate_pct = Decimal('12')
		self.product.units = Decimal('1000')
		self.product.current_price = Decimal('1')
		self.product.current_value_usd = Decimal('1000')
		self.product.income_schedule = Product.IncomeSchedule.TWICE_MONTHLY
		self.product.save()

		self.assertEqual(schedule_payments_per_year(Product.IncomeSchedule.TWICE_MONTHLY), 24)
		amount, amount_usd = estimate_next_income_amount(self.product, payment_dates=[])
		self.assertEqual(amount, Decimal('5.00'))
		self.assertEqual(amount_usd, Decimal('5.00'))

	def test_forecast_without_preset_schedule(self):
		Transaction.objects.create(
			account=self.account,
			product=self.product,
			currency=self.usd,
			transaction_type=Transaction.TransactionType.INCOME,
			amount=Decimal('1.00'),
			quantity=Decimal('0'),
			occurred_at=timezone.make_aware(datetime(2026, 3, 20, 3, 0, 0)),
			import_fingerprint='token-terms-forecast-mar',
			metadata={'operation_type': 'Получение дохода'},
		)
		Transaction.objects.create(
			account=self.account,
			product=self.product,
			currency=self.usd,
			transaction_type=Transaction.TransactionType.INCOME,
			amount=Decimal('1.10'),
			quantity=Decimal('0'),
			occurred_at=timezone.make_aware(datetime(2026, 4, 20, 3, 0, 0)),
			import_fingerprint='token-terms-forecast-apr',
			metadata={'operation_type': 'Получение дохода'},
		)

		self.assertEqual(len(income_payment_dates(self.product)), 2)
		estimated = estimate_next_income_date(self.product, today=date(2026, 5, 1))
		self.assertEqual(estimated, date(2026, 5, 20))

		updated = recompute_next_income_dates(self.finstore, overwrite=True, today=date(2026, 5, 1))
		self.product.refresh_from_db()
		self.assertEqual(updated, 1)
		self.assertEqual(self.product.income_schedule, Product.IncomeSchedule.MONTHLY)
		self.assertEqual(self.product.next_income_date, date(2026, 5, 20))

	def test_csv_next_income_is_not_applied_from_file(self):
		csv_body = (
			'external_id,next_income_date\n'
			'SMART_(BYN_868),2099-01-01\n'
		)
		with TemporaryDirectory() as tmp:
			path = Path(tmp) / 'terms.csv'
			path.write_text(csv_body, encoding='utf-8')
			import_token_terms_from_file(path, recompute_dates=False)

		self.product.refresh_from_db()
		self.assertIsNone(self.product.next_income_date)

	def test_load_token_terms_rows_supports_russian_headers(self):
		csv_body = (
			'название токена,ставка,дата_погашения,график_выплат\n'
			'SMART_(BYN_868),8.5,2030-06-01,ежемесячно\n'
		)
		with TemporaryDirectory() as tmp:
			path = Path(tmp) / 'terms.csv'
			path.write_text(csv_body, encoding='utf-8')
			rows = load_token_terms_rows(path)

		self.assertEqual(rows[0].external_id, 'SMART_(BYN_868)')
		self.assertEqual(rows[0].annual_rate_pct, Decimal('8.5'))
		self.assertEqual(rows[0].income_schedule, Product.IncomeSchedule.MONTHLY)
