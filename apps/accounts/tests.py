from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Account, Transaction
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

    def test_account_create_uses_common_ui_not_admin(self):
        response = self.client.post(
            reverse('accounts:create'),
            {
                'institution': self.alfa.pk,
                'name': 'Savings account',
                'account_type': Account.AccountType.BANK,
                'currency': self.usd.pk,
                'external_id': 'SAV-1',
                'current_balance': '125.50',
                'metadata': '{}',
                'is_active': 'on',
            },
        )

        self.assertRedirects(response, reverse('accounts:list'))
        account = Account.objects.get(external_id='SAV-1')
        self.assertEqual(account.current_balance, Decimal('125.50'))
        self.assertEqual(account.current_balance_usd, Decimal('125.500000'))

        list_response = self.client.get(reverse('accounts:list'))
        self.assertContains(list_response, reverse('accounts:create'))
        self.assertContains(list_response, reverse('accounts:transaction_create'))
        self.assertNotContains(list_response, '/admin/accounts/account/add/')

    def test_transaction_create_generates_fingerprint_and_syncs_balance(self):
        account = Account.objects.get(name='Checking')
        response = self.client.post(
            reverse('accounts:transaction_create'),
            {
                'account': account.pk,
                'related_account': '',
                'product': '',
                'transaction_type': Transaction.TransactionType.INCOME,
                'currency': self.byn.pk,
                'external_id': '',
                'amount': '10.00',
                'quantity': '0',
                'unit_price': '0',
                'occurred_at': '2026-06-06T12:00',
                'description': 'Manual interest',
                'metadata': '{}',
            },
        )

        self.assertRedirects(response, reverse('accounts:list'))
        transaction = Transaction.objects.get(description='Manual interest')
        self.assertTrue(transaction.import_fingerprint.startswith('manual:'))
        self.assertEqual(transaction.amount_usd, Decimal('3.10'))
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal('10.00'))

    def test_admin_add_is_disabled_for_accounts_and_transactions(self):
        user_model = get_user_model()
        user = user_model.objects.create_superuser('admin', 'admin@example.com', 'password')
        self.client.force_login(user)

        response = self.client.get('/admin/accounts/account/add/')
        self.assertEqual(response.status_code, 403)
        response = self.client.get('/admin/accounts/transaction/add/')
        self.assertEqual(response.status_code, 403)
