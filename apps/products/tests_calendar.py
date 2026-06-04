from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.common.models import Currency
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product
from apps.products.operations_calendar import build_operations_calendar


class OperationsCalendarTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(code='USD', name='US Dollar', symbol='$', usd_rate=Decimal('1'), is_base=True)
        self.finstore = FinancialInstitution.objects.create(
            name='Finstore',
            slug='finstore-cal',
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
            name='TOKEN_(USD_100)',
            external_id='TOKEN_(USD_100)',
            product_type=Product.ProductType.TOKEN,
            currency=self.usd,
            income_schedule=Product.IncomeSchedule.MONTHLY,
        )

    def test_calendar_shows_only_future_forecasts_nearest_first(self):
        Transaction.objects.create(
            account=self.account,
            product=self.product,
            currency=self.usd,
            transaction_type=Transaction.TransactionType.INCOME,
            amount=Decimal('2.00'),
            quantity=Decimal('0'),
            occurred_at=timezone.make_aware(datetime(2026, 4, 10, 12, 0, 0)),
            import_fingerprint='calendar-test-apr',
            metadata={'operation_type': 'Получение дохода'},
        )
        Transaction.objects.create(
            account=self.account,
            product=self.product,
            currency=self.usd,
            transaction_type=Transaction.TransactionType.INCOME,
            amount=Decimal('1.00'),
            quantity=Decimal('0'),
            occurred_at=timezone.make_aware(datetime(2026, 3, 10, 12, 0, 0)),
            import_fingerprint='calendar-test-mar',
            metadata={'operation_type': 'Получение дохода'},
        )

        reference = date(2026, 4, 15)
        calendar = build_operations_calendar([self.product], today=reference, future_days=60)
        dates = [day['date'] for day in calendar]

        self.assertNotIn(date(2026, 4, 10), dates)
        self.assertNotIn(date(2026, 3, 10), dates)
        self.assertTrue(dates)
        self.assertTrue(all(day_date >= reference for day_date in dates))
        self.assertEqual(dates[0], date(2026, 5, 10))

        forecast_events = [
            event
            for day in calendar
            for group in day['groups']
            for event in group['events']
        ]
        self.assertEqual(len(forecast_events), 1)
        self.assertTrue(all(event['is_forecast'] for event in forecast_events))

    def test_calendar_includes_forecast_amount_when_rate_set(self):
        self.product.annual_rate_pct = Decimal('12.00')
        self.product.units = Decimal('5')
        self.product.current_price = Decimal('20')
        self.product.current_value_usd = Decimal('50')
        self.product.save()

        Transaction.objects.create(
            account=self.account,
            product=self.product,
            currency=self.usd,
            transaction_type=Transaction.TransactionType.INCOME,
            amount=Decimal('1.00'),
            quantity=Decimal('0'),
            occurred_at=timezone.make_aware(datetime(2026, 4, 10, 12, 0, 0)),
            import_fingerprint='calendar-rate-apr',
            metadata={'operation_type': 'Получение дохода'},
        )

        calendar = build_operations_calendar([self.product], today=date(2026, 4, 15), future_days=60)
        event = calendar[0]['groups'][0]['events'][0]
        # 5 * 20 * 12% / 12 = 1.00
        self.assertEqual(event['amount'], Decimal('1.00'))

    def test_calendar_group_summary_totals_expected_payments(self):
        product_b = Product.objects.create(
            institution=self.finstore,
            name='TOKEN_B_(USD_200)',
            external_id='TOKEN_B_(USD_200)',
            product_type=Product.ProductType.TOKEN,
            currency=self.usd,
            income_schedule=Product.IncomeSchedule.MONTHLY,
            annual_rate_pct=Decimal('12.00'),
            units=Decimal('10'),
            current_price=Decimal('20'),
            current_value_usd=Decimal('200'),
        )
        self.product.annual_rate_pct = Decimal('12.00')
        self.product.units = Decimal('5')
        self.product.current_price = Decimal('20')
        self.product.current_value_usd = Decimal('100')
        self.product.save()

        for product, fingerprint in ((self.product, 'calendar-group-a'), (product_b, 'calendar-group-b')):
            Transaction.objects.create(
                account=self.account,
                product=product,
                currency=self.usd,
                transaction_type=Transaction.TransactionType.INCOME,
                amount=Decimal('1.00'),
                quantity=Decimal('0'),
                occurred_at=timezone.make_aware(datetime(2026, 4, 10, 12, 0, 0)),
                import_fingerprint=fingerprint,
                metadata={'operation_type': 'Получение дохода'},
            )

        calendar = build_operations_calendar([self.product, product_b], today=date(2026, 4, 15), future_days=60)
        group = calendar[0]['groups'][0]
        self.assertEqual(len(group['events']), 2)
        self.assertEqual(group['total_amount'], Decimal('3.00'))
        self.assertEqual(group['total_amount_usd'], Decimal('3.00'))

    def test_calendar_includes_planned_maturity_redemption(self):
        self.product.maturity_date = date(2026, 5, 20)
        self.product.units = Decimal('10')
        self.product.current_price = Decimal('20')
        self.product.current_value_usd = Decimal('200')
        self.product.save()

        Transaction.objects.create(
            account=self.account,
            product=self.product,
            currency=self.usd,
            transaction_type=Transaction.TransactionType.INCOME,
            amount=Decimal('1.00'),
            quantity=Decimal('0'),
            occurred_at=timezone.make_aware(datetime(2026, 4, 10, 12, 0, 0)),
            import_fingerprint='calendar-maturity-income',
            metadata={'operation_type': 'Получение дохода'},
        )

        calendar = build_operations_calendar([self.product], today=date(2026, 4, 15), future_days=60)
        events_by_kind = {
            event['kind']: event
            for day in calendar
            for group in day['groups']
            for event in group['events']
        }
        self.assertIn('maturity_forecast', events_by_kind)
        maturity = events_by_kind['maturity_forecast']
        self.assertEqual(maturity['operation_type'], 'Плановое погашение')
        self.assertEqual(maturity['amount'], Decimal('200.00'))
        self.assertIn('income_forecast', events_by_kind)

    def test_at_maturity_schedule_shows_redemption_only(self):
        self.product.income_schedule = Product.IncomeSchedule.AT_MATURITY
        self.product.maturity_date = date(2026, 5, 20)
        self.product.units = Decimal('5')
        self.product.current_price = Decimal('20')
        self.product.current_value_usd = Decimal('100')
        self.product.save()

        calendar = build_operations_calendar([self.product], today=date(2026, 4, 15), future_days=60)
        kinds = [
            event['kind']
            for day in calendar
            for group in day['groups']
            for event in group['events']
        ]
        self.assertEqual(kinds, ['maturity_forecast'])

    def test_maturity_outside_window_or_closed_position_excluded(self):
        self.product.maturity_date = date(2026, 8, 1)
        self.product.units = Decimal('0')
        self.product.save()

        calendar = build_operations_calendar([self.product], today=date(2026, 4, 15), future_days=60)
        kinds = [
            event['kind']
            for day in calendar
            for group in day['groups']
            for event in group['events']
        ]
        self.assertNotIn('maturity_forecast', kinds)
