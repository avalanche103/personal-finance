from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.common.models import Currency
from apps.institutions.models import FinancialInstitution
from apps.imports.models import ImportJob, ImportSource
from apps.products.models import Product


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

    def test_deposit_top_up_moves_cash_into_linked_deposit_product(self):
        bank = FinancialInstitution.objects.create(
            name='BNB Bank',
            slug='bnb-bank-test',
            institution_type=FinancialInstitution.InstitutionType.BANK,
        )
        income_account = Account.objects.create(
            institution=bank,
            name='BNB BYN',
            account_type=Account.AccountType.BANK,
            currency=self.byn,
            current_balance=Decimal('11.07'),
            current_balance_usd=Decimal('3.43'),
        )
        deposit = Product.objects.create(
            institution=bank,
            income_account=income_account,
            name='BNB2 test',
            product_type=Product.ProductType.DEPOSIT,
            currency=self.byn,
            units=Decimal('1115.04'),
            current_price=Decimal('1'),
            current_value_usd=Decimal('345.66'),
            external_id='test-bnb2',
            metadata={'interest_mode': 'capitalized'},
        )
        Transaction.objects.create(
            account=income_account,
            product=deposit,
            transaction_type=Transaction.TransactionType.DEPOSIT,
            currency=self.byn,
            import_fingerprint='manual:deposit-opening',
            amount=Decimal('1115.04'),
            amount_usd=Decimal('345.66'),
            quantity=Decimal('1115.04'),
            unit_price=Decimal('1'),
            occurred_at=timezone.make_aware(timezone.datetime(2026, 5, 1, 12, 0)),
            description='Opening deposit',
            metadata={'exclude_from_account_balance': True, 'operation_kind': 'opening'},
        )
        Transaction.objects.create(
            account=income_account,
            transaction_type=Transaction.TransactionType.TRANSFER,
            currency=self.byn,
            import_fingerprint='manual:deposit-top-up-transfer',
            amount=Decimal('11.07'),
            amount_usd=Decimal('3.43'),
            occurred_at=timezone.make_aware(timezone.datetime(2026, 6, 11, 16, 50)),
            description='Incoming transfer',
        )

        response = self.client.post(
            reverse('accounts:transaction_create'),
            {
                'account': income_account.pk,
                'related_account': '',
                'product': deposit.pk,
                'transaction_type': Transaction.TransactionType.DEPOSIT,
                'currency': self.byn.pk,
                'external_id': '',
                'amount': '11.07',
                'quantity': '0',
                'unit_price': '0',
                'occurred_at': '2026-06-11T17:00',
                'description': 'Top up BNB2',
                'metadata': '{}',
            },
        )

        self.assertRedirects(response, f'{reverse("accounts:list")}#transactions')
        ledger_transaction = Transaction.objects.get(description='Top up BNB2')
        self.assertEqual(ledger_transaction.amount, Decimal('-11.07'))
        self.assertEqual(ledger_transaction.quantity, Decimal('11.07'))
        self.assertTrue(ledger_transaction.metadata.get('operation_kind'), 'top_up')
        income_account.refresh_from_db()
        deposit.refresh_from_db()
        self.assertEqual(income_account.current_balance, Decimal('0'))
        self.assertEqual(deposit.units, Decimal('1126.11'))

    def test_transfer_create_posts_outgoing_and_incoming_legs(self):
        source = Account.objects.get(name='Checking')
        destination = Account.objects.create(
            institution=self.alfa,
            name='Savings BYN',
            account_type=Account.AccountType.BANK,
            currency=self.byn,
            current_balance=Decimal('0'),
            current_balance_usd=Decimal('0'),
        )
        Transaction.objects.create(
            account=source,
            transaction_type=Transaction.TransactionType.DEPOSIT,
            currency=self.byn,
            import_fingerprint='manual:transfer-source-funding',
            amount=Decimal('11.07'),
            amount_usd=Decimal('3.43'),
            occurred_at=timezone.make_aware(timezone.datetime(2026, 6, 11, 12, 0)),
            description='Opening balance',
        )
        source.current_balance = Decimal('11.07')
        source.save(update_fields=['current_balance'])

        response = self.client.post(
            reverse('accounts:transaction_create'),
            {
                'account': source.pk,
                'related_account': destination.pk,
                'product': '',
                'transaction_type': Transaction.TransactionType.TRANSFER,
                'currency': self.byn.pk,
                'external_id': '',
                'amount': '11.07',
                'quantity': '0',
                'unit_price': '0',
                'occurred_at': '2026-06-11T16:50',
                'description': 'Move to savings',
                'metadata': '{}',
            },
        )

        self.assertRedirects(response, f'{reverse("accounts:list")}#transactions')
        legs = list(Transaction.objects.filter(description='Move to savings').order_by('amount'))
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[0].amount, Decimal('-11.07'))
        self.assertEqual(legs[1].amount, Decimal('11.07'))
        self.assertEqual(legs[0].related_account_id, destination.pk)
        self.assertEqual(legs[1].related_account_id, source.pk)
        source.refresh_from_db()
        destination.refresh_from_db()
        self.assertEqual(source.current_balance, Decimal('0'))
        self.assertEqual(destination.current_balance, Decimal('11.07'))

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

        self.assertRedirects(response, f'{reverse("accounts:list")}#transactions')
        transaction = Transaction.objects.get(description='Manual interest')
        self.assertTrue(transaction.import_fingerprint.startswith('manual:'))
        self.assertEqual(transaction.amount_usd, Decimal('3.10'))
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal('10.00'))

    def test_account_list_embeds_transaction_panel_and_filters_transactions(self):
        checking = Account.objects.get(name='Checking')
        brokerage = Account.objects.get(name='Brokerage cash')
        Transaction.objects.create(
            account=checking,
            transaction_type=Transaction.TransactionType.INCOME,
            currency=self.byn,
            import_fingerprint='manual:interest',
            amount=Decimal('10.00'),
            amount_usd=Decimal('3.10'),
            occurred_at=timezone.make_aware(timezone.datetime(2026, 6, 6, 12, 0)),
            description='Manual interest',
        )
        Transaction.objects.create(
            account=brokerage,
            transaction_type=Transaction.TransactionType.FEE,
            currency=self.usd,
            import_fingerprint='manual:fee',
            amount=Decimal('-2.00'),
            amount_usd=Decimal('-2.00'),
            occurred_at=timezone.make_aware(timezone.datetime(2026, 6, 7, 12, 0)),
            description='Brokerage fee',
        )

        response = self.client.get(reverse('accounts:list'), {'tx_q': 'interest'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Transactions')
        self.assertContains(response, 'Manual interest')
        self.assertNotContains(response, 'Brokerage fee')

    def test_transaction_edit_recalculates_usd_and_syncs_balance(self):
        account = Account.objects.get(name='Checking')
        ledger_transaction = Transaction.objects.create(
            account=account,
            transaction_type=Transaction.TransactionType.INCOME,
            currency=self.byn,
            import_fingerprint='manual:edit',
            amount=Decimal('10.00'),
            amount_usd=Decimal('3.10'),
            occurred_at=timezone.make_aware(timezone.datetime(2026, 6, 6, 12, 0)),
            description='Manual interest',
        )
        account.current_balance = Decimal('10.00')
        account.save(update_fields=['current_balance'])

        response = self.client.post(
            reverse('accounts:transaction_edit', args=[ledger_transaction.pk]),
            {
                'account': account.pk,
                'related_account': '',
                'product': '',
                'transaction_type': Transaction.TransactionType.INCOME,
                'currency': self.byn.pk,
                'external_id': '',
                'amount': '20.00',
                'quantity': '0',
                'unit_price': '0',
                'occurred_at': '2026-06-06T12:00',
                'description': 'Updated interest',
                'metadata': '{}',
            },
        )

        self.assertRedirects(response, f'{reverse("accounts:list")}#transactions')
        ledger_transaction.refresh_from_db()
        self.assertEqual(ledger_transaction.amount, Decimal('20.00'))
        self.assertEqual(ledger_transaction.amount_usd, Decimal('6.20'))
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal('20.00'))

    def test_transaction_edit_syncs_old_and_new_accounts_when_account_changes(self):
        old_account = Account.objects.get(name='Checking')
        new_account = Account.objects.get(name='Brokerage BYN')
        ledger_transaction = Transaction.objects.create(
            account=old_account,
            transaction_type=Transaction.TransactionType.DEPOSIT,
            currency=self.byn,
            import_fingerprint='manual:move',
            amount=Decimal('10.00'),
            amount_usd=Decimal('3.10'),
            occurred_at=timezone.make_aware(timezone.datetime(2026, 6, 6, 12, 0)),
            description='Move account',
        )
        old_account.current_balance = Decimal('10.00')
        new_account.current_balance = Decimal('0.00')
        old_account.save(update_fields=['current_balance'])
        new_account.save(update_fields=['current_balance'])

        response = self.client.post(
            reverse('accounts:transaction_edit', args=[ledger_transaction.pk]),
            {
                'account': new_account.pk,
                'related_account': '',
                'product': '',
                'transaction_type': Transaction.TransactionType.DEPOSIT,
                'currency': self.byn.pk,
                'external_id': '',
                'amount': '10.00',
                'quantity': '0',
                'unit_price': '0',
                'occurred_at': '2026-06-06T12:00',
                'description': 'Move account',
                'metadata': '{}',
            },
        )

        self.assertRedirects(response, f'{reverse("accounts:list")}#transactions')
        old_account.refresh_from_db()
        new_account.refresh_from_db()
        self.assertEqual(old_account.current_balance, Decimal('0'))
        self.assertEqual(new_account.current_balance, Decimal('10.00'))

    def test_transaction_delete_syncs_balance(self):
        account = Account.objects.get(name='Checking')
        ledger_transaction = Transaction.objects.create(
            account=account,
            transaction_type=Transaction.TransactionType.INCOME,
            currency=self.byn,
            import_fingerprint='manual:delete',
            amount=Decimal('10.00'),
            amount_usd=Decimal('3.10'),
            occurred_at=timezone.make_aware(timezone.datetime(2026, 6, 6, 12, 0)),
            description='Delete me',
        )
        account.current_balance = Decimal('10.00')
        account.save(update_fields=['current_balance'])

        response = self.client.post(reverse('accounts:transaction_delete_confirm', args=[ledger_transaction.pk]))

        self.assertRedirects(response, f'{reverse("accounts:list")}#transactions')
        self.assertFalse(Transaction.objects.filter(pk=ledger_transaction.pk).exists())
        account.refresh_from_db()
        self.assertEqual(account.current_balance, Decimal('0'))

    def test_imported_transaction_edit_preserves_import_fields_and_marks_override(self):
        account = Account.objects.get(name='Checking')
        source = ImportSource.objects.create(
            name='Finstore XLS',
            code='finstore-xls',
            source_type=ImportSource.SourceType.XLS,
        )
        import_job = ImportJob.objects.create(source=source, idempotency_key='job-1', status=ImportJob.Status.SAVED)
        ledger_transaction = Transaction.objects.create(
            account=account,
            import_job=import_job,
            transaction_type=Transaction.TransactionType.INCOME,
            currency=self.byn,
            import_fingerprint='finstore:checksum:1',
            amount=Decimal('10.00'),
            amount_usd=Decimal('3.10'),
            occurred_at=timezone.make_aware(timezone.datetime(2026, 6, 6, 12, 0)),
            description='Imported interest',
            metadata={'imported_from': 'finstore-history'},
        )

        response = self.client.post(
            reverse('accounts:transaction_edit', args=[ledger_transaction.pk]),
            {
                'account': account.pk,
                'related_account': '',
                'product': '',
                'transaction_type': Transaction.TransactionType.INCOME,
                'currency': self.byn.pk,
                'external_id': '',
                'amount': '11.00',
                'quantity': '0',
                'unit_price': '0',
                'occurred_at': '2026-06-06T12:00',
                'description': 'Corrected imported interest',
                'metadata': '{"imported_from": "finstore-history"}',
            },
        )

        self.assertRedirects(response, f'{reverse("accounts:list")}#transactions')
        ledger_transaction.refresh_from_db()
        self.assertEqual(ledger_transaction.import_fingerprint, 'finstore:checksum:1')
        self.assertEqual(ledger_transaction.import_job, import_job)
        self.assertEqual(ledger_transaction.amount, Decimal('11.00'))
        self.assertTrue(ledger_transaction.metadata['manual_override'])
        self.assertIn('manual_override_at', ledger_transaction.metadata)

    def test_admin_add_is_disabled_for_accounts_and_transactions(self):
        user_model = get_user_model()
        user = user_model.objects.create_superuser('admin', 'admin@example.com', 'password')
        self.client.force_login(user)

        response = self.client.get('/admin/accounts/account/add/')
        self.assertEqual(response.status_code, 403)
        response = self.client.get('/admin/accounts/transaction/add/')
        self.assertEqual(response.status_code, 403)
