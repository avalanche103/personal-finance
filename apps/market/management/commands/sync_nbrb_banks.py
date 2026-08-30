from django.core.management.base import BaseCommand, CommandError

from apps.market.services.nbrb_banks import sync_nbrb_banks


class Command(BaseCommand):
	help = 'Fetch the NBRB bank registry and upsert DepositBank rows.'

	def handle(self, *args, **options):
		try:
			result = sync_nbrb_banks()
		except Exception as exc:
			raise CommandError(f'NBRB banks sync failed: {exc}') from exc
		self.stdout.write(
			self.style.SUCCESS(
				'NBRB banks sync completed. '
				f"fetched={result['fetched']} created={result['created']} "
				f"updated={result['updated']} deactivated={result['deactivated']} "
				f"parsers_assigned={result.get('parsers_assigned', 0)}"
			)
		)
