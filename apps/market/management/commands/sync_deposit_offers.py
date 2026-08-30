from django.core.management.base import BaseCommand, CommandError

from apps.market.services.deposit_offers.sync import sync_deposit_offers


class Command(BaseCommand):
	help = 'Fetch public Belarus deposit offers from bank adapters and upsert DepositOffer rows.'

	def add_arguments(self, parser):
		parser.add_argument(
			'--bank',
			action='append',
			dest='banks',
			help='DepositBank slug to sync (repeatable).',
		)
		parser.add_argument(
			'--parser',
			action='append',
			dest='parsers',
			help='Adapter parser_code to sync (repeatable).',
		)
		parser.add_argument(
			'--dry-run',
			action='store_true',
			help='Parse and count changes without writing offers.',
		)

	def handle(self, *args, **options):
		try:
			result = sync_deposit_offers(
				parser_codes=options.get('parsers'),
				bank_slugs=options.get('banks'),
				dry_run=bool(options.get('dry_run')),
			)
		except Exception as exc:
			raise CommandError(f'Deposit offers sync failed: {exc}') from exc

		for item in result.banks:
			if item.error:
				self.stdout.write(
					self.style.ERROR(
						f'{item.bank_name} [{item.parser_code}]: ERROR {item.error}'
					)
				)
			else:
				self.stdout.write(
					self.style.SUCCESS(
						f'{item.bank_name} [{item.parser_code}]: '
						f'offers={item.offers} created={item.created} '
						f'updated={item.updated} deactivated={item.deactivated}'
					)
				)

		summary = (
			f'Deposit offers sync finished. ok={result.ok} failed={result.failed} '
			f'total_offers={result.total_offers}'
		)
		if result.failed:
			self.stdout.write(self.style.WARNING(summary))
		else:
			self.stdout.write(self.style.SUCCESS(summary))
