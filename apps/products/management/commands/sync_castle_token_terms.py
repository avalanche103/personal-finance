import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from urllib.error import HTTPError, URLError

from apps.products.models import Product
from apps.products.services.castle import (
	CASTLE_CALENDAR_URL,
	DEFAULT_BOND_INDEX_CACHE,
	fetch_bond_details,
	fetch_calendar_bond_index,
)
from apps.products.services.castle import _details_to_terms_row
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
		parser.add_argument(
			'--missing-terms',
			action='store_true',
			help='Only tokens missing annual_rate_pct or maturity_date.',
		)
		parser.add_argument(
			'--bond-index-cache',
			default=str(DEFAULT_BOND_INDEX_CACHE),
			help='Path to cache calendar token->bond index (default: data/cache/castle_bond_index.json).',
		)
		parser.add_argument(
			'--use-cache-only',
			action='store_true',
			help='Do not request calendar page; use bond index cache file only.',
		)
		parser.add_argument(
			'--calendar-html',
			default=None,
			help='Load calendar bond index from a saved HTML file (offline / rate-limit workaround).',
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

		platform_filter = (options['platform'] or '').strip().lower() or None
		cache_path = Path(options['bond_index_cache'])

		target_ids = None
		if not options['all_calendar']:
			product_qs = Product.objects.filter(institution=institution).exclude(external_id='')
			if options['missing_terms']:
				product_qs = product_qs.filter(
					Q(annual_rate_pct__isnull=True) | Q(maturity_date__isnull=True),
				)
			target_ids = set(product_qs.values_list('external_id', flat=True))

		try:
			calendar_html = Path(options['calendar_html']) if options['calendar_html'] else None
			calendar_url = (options['calendar_url'] or CASTLE_CALENDAR_URL).strip()
			calendar_index = fetch_calendar_bond_index(
				calendar_url=calendar_url,
				bond_index_cache=cache_path,
				use_cache_only=options['use_cache_only'],
				calendar_html_file=calendar_html,
			)
		except (HTTPError, URLError, RuntimeError) as exc:
			raise CommandError(
				f'Failed to load castle calendar index: {exc}. '
				f'Wait for rate limit to clear or pass --use-cache-only with a populated {cache_path}.'
			) from exc

		if target_ids:
			bond_jobs = [
				(calendar_index[external_id], external_id)
				for external_id in sorted(target_ids)
				if external_id in calendar_index
			]
			missing_from_calendar = sorted(target_ids - set(calendar_index))
		else:
			bond_jobs = [
				(bond_id, external_id)
				for external_id, bond_id in sorted(calendar_index.items())
			]
			missing_from_calendar = []

		if options['limit'] is not None:
			bond_jobs = bond_jobs[: options['limit']]

		matched = 0
		updated = 0
		scraped = 0
		unmatched: list[str] = []
		errors: list[str] = []
		request_delay = options['delay']

		for index, (bond_id, expected_external_id) in enumerate(bond_jobs):
			if index and request_delay:
				time.sleep(request_delay)
			try:
				details = fetch_bond_details(bond_id)
			except (HTTPError, URLError) as exc:
				errors.append(f'bond/{bond_id}: {exc}')
				continue

			if details is None:
				errors.append(f'bond/{bond_id}: token not detected')
				continue

			if platform_filter and details.platform != platform_filter:
				continue

			if expected_external_id and details.external_id != expected_external_id:
				errors.append(
					f'bond/{bond_id}: expected {expected_external_id}, got {details.external_id}'
				)
				continue

			scraped += 1
			row = _details_to_terms_row(details)
			product = find_product_for_terms(institution, row)
			if product is None:
				unmatched.append(row.external_id)
				continue
			matched += 1
			if options['dry_run']:
				self.stdout.write(
					f'{row.external_id}: rate={row.annual_rate_pct} maturity={row.maturity_date}'
				)
				continue
			if apply_terms_to_product(product, row):
				updated += 1

		if not options['dry_run']:
			recompute_next_income_dates(institution, overwrite=options['overwrite_next_dates'])

		mode = 'dry-run' if options['dry_run'] else 'sync'
		self.stdout.write(self.style.SUCCESS(
			f'{mode} completed. calendar_tokens={len(calendar_index)} jobs={len(bond_jobs)} '
			f'scraped={scraped} matched={matched} updated={updated} '
			f'unmatched={len(unmatched)} errors={len(errors)}'
		))
		if missing_from_calendar:
			self.stdout.write(self.style.WARNING(
				f'not in calendar ({len(missing_from_calendar)}): '
				f'{", ".join(missing_from_calendar[:15])}'
				+ (' ...' if len(missing_from_calendar) > 15 else '')
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
