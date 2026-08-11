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
        self.assertIn(date(2026, 6, 10), dates)

        forecast_events = [
            event
            for day in calendar
            for group in day['groups']
            for event in group['events']
            if event['kind'] == 'income_forecast'
        ]
        self.assertEqual(len(forecast_events), 2)
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

    def test_monthly_deposit_forecasts_use_opened_at_anchor_day(self):
        bank = FinancialInstitution.objects.create(
            name='BNB Calendar Bank',
            slug='bnb-calendar-bank',
            institution_type=FinancialInstitution.InstitutionType.BANK,
        )
        deposit = Product.objects.create(
            institution=bank,
            name='Monthly deposit',
            product_type=Product.ProductType.DEPOSIT,
            currency=self.usd,
            units=Decimal('1000'),
            current_price=Decimal('1'),
            current_value_usd=Decimal('1000'),
            annual_rate_pct=Decimal('12'),
            income_schedule=Product.IncomeSchedule.MONTHLY,
            maturity_date=date(2027, 1, 3),
            next_income_date=date(2026, 7, 3),
            metadata={'opened_at': '2025-12-03', 'interest_mode': 'capitalized'},
            is_active=True,
        )
        Transaction.objects.create(
            account=self.account,
            product=deposit,
            transaction_type=Transaction.TransactionType.INCOME,
            currency=self.usd,
            amount=Decimal('10'),
            amount_usd=Decimal('10'),
            quantity=Decimal('10'),
            occurred_at=timezone.make_aware(datetime(2026, 7, 3, 12, 0)),
            metadata={'operation_kind': 'capitalization', 'interest_mode': 'capitalized'},
        )

        calendar = build_operations_calendar([deposit], today=date(2026, 7, 25), future_days=60)
        dates = [day['date'] for day in calendar]
        self.assertEqual(dates, [date(2026, 8, 3), date(2026, 9, 3)])
        event = calendar[0]['groups'][0]['events'][0]
        self.assertEqual(event['kind'], 'income_forecast')
        self.assertEqual(event['product'], deposit)
        self.assertIsNotNone(event['amount'])

    def test_bnb_monthly_forecast_matches_actual_day_count_and_skips_paid_today(self):
        bank = FinancialInstitution.objects.create(
            name='BNB Forecast Bank',
            slug='bnb-forecast-bank',
            institution_type=FinancialInstitution.InstitutionType.BANK,
        )
        deposit = Product.objects.create(
            institution=bank,
            name='BNB2 forecast',
            product_type=Product.ProductType.DEPOSIT,
            currency=self.usd,
            units=Decimal('1203'),
            current_price=Decimal('1'),
            current_value_usd=Decimal('1203'),
            annual_rate_pct=Decimal('14.91'),
            income_schedule=Product.IncomeSchedule.MONTHLY,
            maturity_date=date(2029, 6, 29),
            next_income_date=date(2026, 6, 29),
            metadata={
                'opened_at': '2026-05-29',
                'interest_mode': 'capitalized',
                'income_day_count_basis': 365,
            },
            is_active=True,
        )
        for occurred_at, tx_type, amount, quantity, kind in (
            (datetime(2026, 5, 29, 12, 0), Transaction.TransactionType.DEPOSIT, Decimal('1115.04'), Decimal('1115.04'), 'opening'),
            (datetime(2026, 6, 11, 12, 0), Transaction.TransactionType.DEPOSIT, Decimal('11.07'), Decimal('11.07'), 'top_up'),
            (datetime(2026, 6, 12, 12, 0), Transaction.TransactionType.DEPOSIT, Decimal('3.49'), Decimal('3.49'), 'top_up'),
            (datetime(2026, 6, 25, 12, 0), Transaction.TransactionType.DEPOSIT, Decimal('11.07'), Decimal('11.07'), 'top_up'),
            (datetime(2026, 6, 28, 12, 0), Transaction.TransactionType.DEPOSIT, Decimal('3.27'), Decimal('3.27'), 'top_up'),
            (datetime(2026, 6, 29, 12, 0), Transaction.TransactionType.INCOME, Decimal('14.28'), Decimal('14.28'), 'capitalization'),
            (datetime(2026, 7, 14, 12, 0), Transaction.TransactionType.DEPOSIT, Decimal('14.78'), Decimal('14.78'), 'top_up'),
        ):
            Transaction.objects.create(
                account=self.account,
                product=deposit,
                transaction_type=tx_type,
                currency=self.usd,
                amount=amount,
                amount_usd=amount,
                quantity=quantity,
                occurred_at=timezone.make_aware(occurred_at),
                import_fingerprint=f'bnb-forecast-{occurred_at.date()}-{kind}',
                metadata={
                    'operation_kind': kind,
                    'interest_mode': 'capitalized',
                    'exclude_from_account_balance': True,
                },
            )

        morning = build_operations_calendar([deposit], today=date(2026, 7, 29), future_days=60)
        self.assertEqual(morning[0]['date'], date(2026, 7, 29))
        self.assertEqual(morning[0]['groups'][0]['events'][0]['amount'], Decimal('14.19'))

        Transaction.objects.create(
            account=self.account,
            product=deposit,
            transaction_type=Transaction.TransactionType.INCOME,
            currency=self.usd,
            amount=Decimal('14.19'),
            amount_usd=Decimal('14.19'),
            quantity=Decimal('14.19'),
            occurred_at=timezone.make_aware(datetime(2026, 7, 29, 9, 0)),
            import_fingerprint='bnb-forecast-2026-07-29-capitalization',
            metadata={
                'operation_kind': 'capitalization',
                'interest_mode': 'capitalized',
                'exclude_from_account_balance': True,
            },
        )
        Transaction.objects.create(
            account=self.account,
            product=deposit,
            transaction_type=Transaction.TransactionType.DEPOSIT,
            currency=self.usd,
            amount=Decimal('15.81'),
            amount_usd=Decimal('15.81'),
            quantity=Decimal('15.81'),
            occurred_at=timezone.make_aware(datetime(2026, 7, 29, 10, 0)),
            import_fingerprint='bnb-forecast-2026-07-29-top-up',
            metadata={
                'operation_kind': 'top_up',
                'interest_mode': 'capitalized',
                'exclude_from_account_balance': True,
            },
        )

        after_fact = build_operations_calendar([deposit], today=date(2026, 7, 29), future_days=60)
        self.assertEqual([day['date'] for day in after_fact], [date(2026, 8, 29)])
        self.assertNotEqual(after_fact[0]['groups'][0]['events'][0]['amount'], Decimal('14.95'))

    def test_alfabank_forecasts_use_actual_days_and_following_weekday(self):
        alfabank = FinancialInstitution.objects.create(
            name='Alfa Bank Calendar',
            slug='alfabank',
            institution_type=FinancialInstitution.InstitutionType.BANK,
        )
        specs = (
            ('ALFA1', Decimal('1086.02'), Decimal('16.00'), date(2026, 7, 10), date(2026, 7, 27), Decimal('8.09')),
            ('ALFA2', Decimal('616.75'), Decimal('15.50'), date(2026, 7, 10), date(2026, 7, 27), Decimal('4.45')),
            ('ALFA3', Decimal('530.95'), Decimal('15.00'), date(2026, 7, 13), date(2026, 7, 28), Decimal('3.27')),
        )
        products = []
        for name, balance, rate, last_payment, _, _ in specs:
            product = Product.objects.create(
                institution=alfabank,
                name=name,
                product_type=Product.ProductType.DEPOSIT,
                currency=self.usd,
                units=balance,
                current_price=Decimal('1'),
                current_value_usd=balance,
                annual_rate_pct=rate,
                income_schedule=Product.IncomeSchedule.TWICE_MONTHLY,
                maturity_date=date(2026, 10, 10),
                metadata={
                    'opened_at': '2025-04-10',
                    'interest_mode': 'payout',
                    'income_interval_days': 15,
                    'income_day_count_basis': 365,
                    'income_date_adjustment': 'following_weekday',
                },
                is_active=True,
            )
            Transaction.objects.create(
                account=self.account,
                product=product,
                transaction_type=Transaction.TransactionType.INCOME,
                currency=self.usd,
                amount=Decimal('1'),
                amount_usd=Decimal('1'),
                occurred_at=timezone.make_aware(datetime.combine(last_payment, datetime.min.time())),
                import_fingerprint=f'calendar-{name}-last-income',
            )
            products.append(product)

        calendar = build_operations_calendar(products, today=date(2026, 7, 25), future_days=10)
        actual = {
            event['product_name']: (day['date'], event['amount'])
            for day in calendar
            for group in day['groups']
            for event in group['events']
            if event['kind'] == 'income_forecast'
        }

        expected = {
            name: (payment_date, amount)
            for name, _, _, _, payment_date, amount in specs
        }
        self.assertEqual(actual, expected)

    def test_finstore_early_redemption_includes_accrued_income(self):
        finstore = FinancialInstitution.objects.create(
            name='Finstore Redemption',
            slug='finstore',
            institution_type=FinancialInstitution.InstitutionType.BROKER,
        )
        product = Product.objects.create(
            institution=finstore,
            name='LIGHTLEASING_(BYN_628)',
            product_type=Product.ProductType.TOKEN,
            currency=self.usd,
            units=Decimal('2'),
            current_price=Decimal('50'),
            current_value_usd=Decimal('100'),
            annual_rate_pct=Decimal('21'),
            income_schedule=Product.IncomeSchedule.MONTHLY,
            next_income_date=date(2026, 8, 15),
            maturity_date=date(2026, 8, 3),
            is_active=True,
        )
        Transaction.objects.create(
            account=self.account,
            product=product,
            transaction_type=Transaction.TransactionType.TRADE,
            currency=self.usd,
            amount=Decimal('-100'),
            quantity=Decimal('2'),
            occurred_at=timezone.make_aware(datetime(2024, 9, 17, 12, 0)),
            import_fingerprint='calendar-finstore-redemption-buy',
        )
        Transaction.objects.create(
            account=self.account,
            product=product,
            transaction_type=Transaction.TransactionType.INCOME,
            currency=self.usd,
            amount=Decimal('1.73'),
            occurred_at=timezone.make_aware(datetime(2026, 7, 15, 12, 0)),
            import_fingerprint='calendar-finstore-redemption-income',
        )

        calendar = build_operations_calendar([product], today=date(2026, 7, 25), future_days=30)

        self.assertEqual([day['date'] for day in calendar], [date(2026, 8, 3)])
        group = calendar[0]['groups'][0]
        self.assertEqual(
            [(event['kind'], event['amount']) for event in group['events']],
            [('maturity_forecast', Decimal('100.00')), ('income_forecast', Decimal('1.96'))],
        )
        self.assertEqual(group['total_amount'], Decimal('101.96'))

    def test_alfabank_maturity_includes_interest_after_last_forecast_payment(self):
        alfabank = FinancialInstitution.objects.create(
            name='Alfa Bank Maturity',
            slug='alfabank-maturity',
            institution_type=FinancialInstitution.InstitutionType.BANK,
        )
        product = Product.objects.create(
            institution=alfabank,
            name='ALFA3',
            product_type=Product.ProductType.DEPOSIT,
            currency=self.usd,
            units=Decimal('530.95'),
            current_price=Decimal('1'),
            current_value_usd=Decimal('530.95'),
            annual_rate_pct=Decimal('15'),
            income_schedule=Product.IncomeSchedule.TWICE_MONTHLY,
            maturity_date=date(2026, 8, 11),
            metadata={
                'opened_at': '2025-02-11',
                'interest_mode': 'payout',
                'income_interval_days': 15,
                'income_day_count_basis': 365,
                'income_date_adjustment': 'following_weekday',
            },
            is_active=True,
        )
        Transaction.objects.create(
            account=self.account,
            product=product,
            transaction_type=Transaction.TransactionType.INCOME,
            currency=self.usd,
            amount=Decimal('3.71'),
            occurred_at=timezone.make_aware(datetime(2026, 7, 13, 12, 0)),
            import_fingerprint='calendar-alfa3-maturity-last-income',
        )

        calendar = build_operations_calendar([product], today=date(2026, 7, 25), future_days=20)
        maturity_day = next(day for day in calendar if day['date'] == date(2026, 8, 11))
        group = maturity_day['groups'][0]

        self.assertEqual(
            [(event['kind'], event['amount']) for event in group['events']],
            [('maturity_forecast', Decimal('530.95')), ('income_forecast', Decimal('3.05'))],
        )
        self.assertEqual(group['total_amount'], Decimal('534.00'))
