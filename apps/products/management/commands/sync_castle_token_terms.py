from django.core.management.base import BaseCommand, CommandError

from apps.products.models import Product
from apps.products.services.castle import fetch_castle_token_terms
from apps.products.services.token_terms import (
	apply_terms_to_product,
	find_product_for_terms,
	recompute_next_income_dates,
	resolve_finstore_institution,
)


class Command(BaseCommand):
	help = 'Sync token terms (rate, maturity, schedule) from castle.by calendar bond pages.'

	def add_arguments(self, parser):
		parser.add_argument('--institution', default='finstore', help='Institution slug (default: finstore).')
		parser.add_argument(
			'--platform',
			default='finstore',
			help='Filter castle bonds by platform (finstore, fainex, bynex). Use "" for all.',
		)
		parser.add_argument('--calendar-url', default=None, help='Override calendar URL.')
		parser.add_argument('--limit', type=int, default=None, help='Limit bond pages to fetch (debug).')
		parser.add_argument(
			'--delay',
			type=float,
			default=1.0,
			help='Seconds between bond page requests (default: 1.0).',
		)
		parser.add_argument(
			'--all-calendar',
			action='store_true',
			help='Fetch all calendar bonds, not only tokens already in the database.',
		)
		parser.add_argument('--dry-run', action='store_true', help='Match only, do not save.')
		parser.add_argument(
			'--overwrite-next-dates',
			action='store_true',
			help='Replace next_income_date after sync.',
		)

	def handle(self, *args, **options):
		try:
			institution = resolve_finstore_institution(options['institution'])
		except Exception as exc:
			raise CommandError(str(exc)) from exc

		platform = (options['platform'] or '').strip() or None
		target_ids = None
		if not options['all_calendar']:
			target_ids = {
				external_id
				for external_id in Product.objects.filter(institution=institution)
				.exclude(external_id='')
				.values_list('external_id', flat=True)
			}

		rows, errors = fetch_castle_token_terms(
			calendar_url=options['calendar_url'],
			platform=platform,
			target_external_ids=target_ids,
			limit=options['limit'],
			request_delay=options['delay'],
		)

		matched = 0
		updated = 0
		unmatched: list[str] = []

		for row in rows:
			product = find_product_for_terms(institution, row)
			if product is None:
				unmatched.append(row.external_id)
				continue
			matched += 1
			if options['dry_run']:
				continue
			if apply_terms_to_product(product, row):
				updated += 1

		if not options['dry_run']:
			recompute_next_income_dates(institution, overwrite=options['overwrite_next_dates'])

		mode = 'dry-run' if options['dry_run'] else 'sync'
		self.stdout.write(self.style.SUCCESS(
			f'{mode} completed. scraped={len(rows)} matched={matched} updated={updated} '
			f'unmatched={len(unmatched)} errors={len(errors)}'
		))
		if unmatched:
			self.stdout.write(self.style.WARNING(
				f'unmatched ({len(unmatched)}): {", ".join(unmatched[:20])}'
				+ (' ...' if len(unmatched) > 20 else '')
			))
		if errors:
			self.stdout.write(self.style.WARNING(
				f'errors ({len(errors)}): {"; ".join(errors[:10])}'
				+ (' ...' if len(errors) > 10 else '')
			))
