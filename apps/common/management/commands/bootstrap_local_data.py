from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.accounts.models import Account
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
				'institution_type': FinancialInstitution.InstitutionType.BANK,
				'country': 'BY',
				'base_currency': byn,
				'metadata': {'bootstrap': True},
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

		for account_name, currency, current_balance, current_balance_usd in [
			('Finstore BYN Account', byn, Decimal('0.00'), Decimal('0.00')),
			('Finstore USD Account', usd, Decimal('0.00'), Decimal('0.00')),
			('Finstore EUR Account', eur, Decimal('0.00'), Decimal('0.00')),
			('Finstore RUB Account', rub, Decimal('0.00'), Decimal('0.00')),
		]:
			Account.objects.get_or_create(
				institution=finstore,
				name=account_name,
				defaults={
					'account_type': Account.AccountType.BANK,
					'currency': currency,
					'current_balance': current_balance,
					'current_balance_usd': current_balance_usd,
					'metadata': {'bootstrap': True},
				},
			)

		self.stdout.write(self.style.SUCCESS('Local bootstrap data created or updated.'))