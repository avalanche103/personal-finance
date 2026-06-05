from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Account
from apps.common.models import Currency
from apps.institutions.models import FinancialInstitution


class AccountViewsTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(code='USD', name='US Dollar', symbol='$', usd_rate=Decimal('1'), is_base=True)
        self.byn = Currency.objects.create(code='BYN', name='Belarusian Ruble', symbol='Br', usd_rate=Decimal('0.31'))
        self.finstore = FinancialInstitution.objects.create(name='Finstore', institution_type=FinancialInstitution.InstitutionType.BROKER)
        self.alfa = FinancialInstitution.objects.create(name='Alfa', institution_type=FinancialInstitution.InstitutionType.BANK)
        self.binance = FinancialInstitution.objects.create(name='Binance', slug='binance', institution_type=FinancialInstitution.InstitutionType.CRYPTO_EXCHANGE)
        Account.objects.create(
            institution=self.finstore,
            name='Brokerage cash',
            account_type=Account.AccountType.BROKERAGE,
            currency=self.usd,
            current_balance=Decimal('1000'),
            current_balance_usd=Decimal('1000'),
        )
        Account.objects.create(
            institution=self.finstore,
            name='Brokerage BYN',
            account_type=Account.AccountType.BROKERAGE,
            currency=self.byn,
            current_balance=Decimal('500'),
            current_balance_usd=Decimal('155'),
        )
        Account.objects.create(
            institution=self.alfa,
            name='Checking',
            account_type=Account.AccountType.BANK,
            currency=self.byn,
            current_balance=Decimal('200'),
            current_balance_usd=Decimal('62'),
        )
        Account.objects.create(
            institution=self.binance,
            name='Binance USD',
            account_type=Account.AccountType.WALLET,
            currency=self.usd,
            current_balance=Decimal('12.34'),
            current_balance_usd=Decimal('12.34'),
        )
        Account.objects.create(
            institution=self.binance,
            name='Binance BTC',
            account_type=Account.AccountType.WALLET,
            currency=self.usd,
            current_balance=Decimal('0'),
            current_balance_usd=Decimal('0'),
        )

    def test_account_list_groups_by_institution(self):
        response = self.client.get(reverse('accounts:list'))
        self.assertEqual(response.status_code, 200)
        groups = response.context['account_groups']
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0]['label'], 'Alfa')
        self.assertEqual(len(groups[0]['accounts']), 1)
        self.assertEqual(groups[1]['label'], 'Binance')
        self.assertEqual(len(groups[1]['accounts']), 1)
        self.assertEqual(groups[1]['accounts'][0].name, 'Binance USD')
        self.assertEqual(groups[2]['label'], 'Finstore')
        self.assertEqual(len(groups[2]['accounts']), 2)
        self.assertEqual(groups[2]['total_balance_usd'], Decimal('1155'))

    def test_account_list_search_filters_groups(self):
        response = self.client.get(reverse('accounts:list'), {'q': 'Brokerage'})
        self.assertEqual(response.status_code, 200)
        groups = response.context['account_groups']
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['label'], 'Finstore')
        self.assertEqual(len(groups[0]['accounts']), 2)
