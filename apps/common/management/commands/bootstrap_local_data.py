from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import Account, Transaction
from apps.accounts.services.balance import sync_account_balance
from apps.common.models import Currency
from apps.imports.models import ImportSource
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product


class Command(BaseCommand):
	help = 'Create local bootstrap data for currencies, institutions, import sources, and demo financial records.'

	def handle(self, *args, **options):
		usd, _ = Currency.objects.update_or_create(
			code='USD',
			defaults={
				'name': 'US Dollar',
				'symbol': '$',
				'usd_rate': Decimal('1'),
				'metadata': {'bootstrap': True},
			},
		)
		eur, _ = Currency.objects.update_or_create(
			code='EUR',
			defaults={
				'name': 'Euro',
				'symbol': 'EUR',
				'usd_rate': Decimal('1.08'),
				'metadata': {'bootstrap': True},
			},
		)
		rub, _ = Currency.objects.update_or_create(
			code='RUB',
			defaults={
				'name': 'Russian Ruble',
				'symbol': 'RUB',
				'usd_rate': Decimal('0.011'),
				'metadata': {'bootstrap': True},
			},
		)
		byn, _ = Currency.objects.update_or_create(
			code='BYN',
			defaults={
				'name': 'Belarusian Ruble',
				'symbol': 'Br',
				'usd_rate': Decimal('0.31'),
				'metadata': {'bootstrap': True},
			},
		)

		finstore, _ = FinancialInstitution.objects.update_or_create(
			slug='finstore',
			defaults={
				'name': 'Finstore',
				'institution_type': FinancialInstitution.InstitutionType.BROKER,
				'country': 'BY',
				'base_currency': byn,
				'metadata': {'bootstrap': True},
			},
		)
		aigenis, _ = FinancialInstitution.objects.update_or_create(
			slug='aigenis',
			defaults={
				'name': 'Aigenis',
				'institution_type': FinancialInstitution.InstitutionType.BROKER,
				'country': 'BY',
				'base_currency': byn,
				'metadata': {'bootstrap': True},
			},
		)
		alfabank, _ = FinancialInstitution.objects.update_or_create(
			slug='alfabank',
			defaults={
				'name': 'АльфаБанк',
				'institution_type': FinancialInstitution.InstitutionType.BANK,
				'country': 'BY',
				'base_currency': byn,
				'metadata': {'bootstrap': True},
			},
		)
		bnb_bank, _ = FinancialInstitution.objects.update_or_create(
			slug='bnb-bank',
			defaults={
				'name': 'БНБ-Банк',
				'institution_type': FinancialInstitution.InstitutionType.BANK,
				'country': 'BY',
				'website': 'https://bnb.by/',
				'base_currency': byn,
				'metadata': {'bootstrap': True},
			},
		)
		bynex, _ = FinancialInstitution.objects.update_or_create(
			slug='bynex',
			defaults={
				'name': 'BYNEX',
				'institution_type': FinancialInstitution.InstitutionType.CRYPTO_EXCHANGE,
				'country': 'BY',
				'base_currency': usd,
				'metadata': {'bootstrap': True},
			},
		)
		stravita, _ = FinancialInstitution.objects.update_or_create(
			slug='stravita',
			defaults={
				'name': 'Стравита',
				'institution_type': FinancialInstitution.InstitutionType.INSURANCE,
				'country': 'BY',
				'website': 'https://stravita.by/',
				'base_currency': byn,
				'metadata': {'bootstrap': True},
			},
		)
		priorlife, _ = FinancialInstitution.objects.update_or_create(
			slug='priorlife',
			defaults={
				'name': 'Приорлайф',
				'institution_type': FinancialInstitution.InstitutionType.INSURANCE,
				'country': 'BY',
				'website': 'https://priorlife.by/',
				'base_currency': usd,
				'metadata': {'bootstrap': True},
			},
		)
		income_sources, _ = FinancialInstitution.objects.update_or_create(
			slug='income-sources',
			defaults={
				'name': 'Доходы',
				'institution_type': FinancialInstitution.InstitutionType.OTHER,
				'country': 'BY',
				'base_currency': byn,
				'metadata': {'bootstrap': True, 'purpose': 'payroll_source'},
			},
		)

		ImportSource.objects.update_or_create(
			code='nbrb-exrates-api',
			defaults={
				'name': 'NBRB Exchange Rates API',
				'source_type': ImportSource.SourceType.API,
				'is_active': True,
				'config': {'tracked_currencies': ['USD', 'EUR', 'RUB'], 'bootstrap': True},
			},
		)

		ImportSource.objects.update_or_create(
			code='finstore-history',
			defaults={
				'institution': finstore,
				'name': 'Finstore Token History',
				'source_type': ImportSource.SourceType.XLS,
				'is_active': True,
				'config': {'parser': 'finstore-history', 'bootstrap': True},
			},
		)

		ImportSource.objects.update_or_create(
			code='aigenis-report',
			defaults={
				'institution': aigenis,
				'name': 'Aigenis Broker Report',
				'source_type': ImportSource.SourceType.XLS,
				'is_active': True,
				'config': {'parser': 'aigenis-report', 'bootstrap': True},
			},
		)
		ImportSource.objects.update_or_create(
			code='stravita-extract',
			defaults={
				'institution': stravita,
				'name': 'Stravita Pension Statement',
				'source_type': ImportSource.SourceType.PDF,
				'is_active': True,
				'config': {'parser': 'stravita-extract', 'management_expense_pct': '5.7', 'bootstrap': True},
			},
		)
		ImportSource.objects.update_or_create(
			code='stravita-contributions',
			defaults={
				'institution': stravita,
				'name': 'Stravita Pension Contributions',
				'source_type': ImportSource.SourceType.PDF,
				'is_active': True,
				'config': {'parser': 'stravita-contributions', 'bootstrap': True},
			},
		)
		ImportSource.objects.update_or_create(
			code='priorlife-contributions',
			defaults={
				'institution': priorlife,
				'name': 'Priorlife Insurance Contributions',
				'source_type': ImportSource.SourceType.PDF,
				'is_active': True,
				'config': {
					'parser': 'priorlife-contributions',
					'bootstrap': True,
				},
			},
		)

		Account.objects.get_or_create(
			institution=aigenis,
			name='Aigenis BYN Account',
			defaults={
				'account_type': Account.AccountType.BROKERAGE,
				'currency': byn,
				'current_balance': Decimal('0.00'),
				'current_balance_usd': Decimal('0.00'),
				'metadata': {'bootstrap': True},
			},
		)
		Account.objects.get_or_create(
			institution=income_sources,
			name='Зарплата',
			defaults={
				'account_type': Account.AccountType.OTHER,
				'currency': byn,
				'current_balance': Decimal('0.00'),
				'current_balance_usd': Decimal('0.00'),
				'metadata': {'bootstrap': True, 'purpose': 'payroll'},
			},
		)
		Account.objects.get_or_create(
			institution=alfabank,
			name='АльфаБанк BYN Account',
			defaults={
				'account_type': Account.AccountType.BANK,
				'currency': byn,
				'current_balance': Decimal('0.00'),
				'current_balance_usd': Decimal('0.00'),
				'metadata': {'bootstrap': True, 'purpose': 'coupon_income'},
			},
		)
		for account_name, currency in [
			('БНБ-Банк BYN Account', byn),
			('БНБ-Банк USD Account', usd),
		]:
			account, created = Account.objects.get_or_create(
				institution=bnb_bank,
				name=account_name,
				defaults={
					'account_type': Account.AccountType.BANK,
					'currency': currency,
					'current_balance': Decimal('0.00'),
					'current_balance_usd': Decimal('0.00'),
					'metadata': {'bootstrap': True},
				},
			)
			if not created:
				account.account_type = Account.AccountType.BANK
				account.currency = currency
				account.save(update_fields=['account_type', 'currency', 'updated_at'])

		Account.objects.get_or_create(
			institution=bynex,
			name='BYNEX USD Account',
			defaults={
				'account_type': Account.AccountType.WALLET,
				'currency': usd,
				'current_balance': Decimal('0.00'),
				'current_balance_usd': Decimal('0.00'),
				'metadata': {'bootstrap': True},
			},
		)
		bynex_usd_account = Account.objects.get(institution=bynex, name='BYNEX USD Account')
		bnb_usd_account = Account.objects.get(institution=bnb_bank, name='БНБ-Банк USD Account')
		for amount, occurred_at, date_key in [
			(Decimal('50.46'), timezone.datetime(2026, 4, 25, 12, 0), '2026-04-25'),
			(Decimal('75.63'), timezone.datetime(2026, 5, 8, 12, 0), '2026-05-08'),
			(Decimal('54.66'), timezone.datetime(2026, 5, 25, 12, 0), '2026-05-25'),
		]:
			Transaction.objects.update_or_create(
				import_fingerprint=f'bootstrap:bynex-usd-deposit:{date_key}',
				defaults={
					'account': bynex_usd_account,
					'related_account': bnb_usd_account,
					'transaction_type': Transaction.TransactionType.TRANSFER,
					'currency': usd,
					'amount': amount,
					'amount_usd': amount,
					'occurred_at': timezone.make_aware(occurred_at),
					'description': 'Пополнение с БНБ-Банк USD Account',
					'metadata': {'bootstrap': True, 'source_institution': 'bnb-bank'},
				},
			)
		sync_account_balance(bynex_usd_account)
		bynex_usd_account.refresh_from_db()
		bynex_usd_account.current_balance_usd = bynex_usd_account.current_balance
		bynex_usd_account.save(update_fields=['current_balance_usd', 'updated_at'])

		for account_name, currency in [
			('Finstore BYN Account', byn),
			('Finstore USD Account', usd),
			('Finstore EUR Account', eur),
			('Finstore RUB Account', rub),
		]:
			account, created = Account.objects.get_or_create(
				institution=finstore,
				name=account_name,
				defaults={
					'account_type': Account.AccountType.BROKERAGE,
					'currency': currency,
					'current_balance': Decimal('0.00'),
					'current_balance_usd': Decimal('0.00'),
					'metadata': {'bootstrap': True},
				},
			)
			if not created:
				account.account_type = Account.AccountType.BROKERAGE
				account.currency = currency
				account.save(update_fields=['account_type', 'currency', 'updated_at'])

		from apps.common.services.aigenis_bonds import AIGENIS_INDEXED_BOND_ISINS, configure_aigenis_indexed_bonds
		from apps.common.services.indexed_bonds import configure_aigenis_indexed_bond
		from apps.products.models import Product

		updated_bonds = configure_aigenis_indexed_bonds(institution=aigenis)
		for isin in AIGENIS_INDEXED_BOND_ISINS:
			product = Product.objects.filter(institution=aigenis, external_id=isin).first()
			if product and not (
				isinstance(product.metadata, dict)
				and product.metadata.get('income_calendar', {}).get('payments')
			):
				configure_aigenis_indexed_bond(product)
		if updated_bonds:
			self.stdout.write(f'Configured {updated_bonds} Aigenis indexed bond(s).')

		self.stdout.write(self.style.SUCCESS('Local bootstrap data created or updated.'))