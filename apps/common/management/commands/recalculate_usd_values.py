from django.core.management.base import BaseCommand

from apps.common.services.exchange_rates import recalculate_usd_valuations


class Command(BaseCommand):
	help = 'Recalculate USD values for accounts, transactions, balance snapshots, and products.'

	def handle(self, *args, **options):
		result = recalculate_usd_valuations()
		self.stdout.write(self.style.SUCCESS(
			'Recalculation completed. '
			f"accounts={result['accounts']} transactions={result['transactions']} "
			f"balance_snapshots={result['balance_snapshots']} products={result['products']}"
		))