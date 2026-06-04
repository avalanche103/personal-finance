from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.imports.services.integrations.finstore import FinstoreTokenCatalogClient
from apps.institutions.models import FinancialInstitution
from apps.products.services.token_terms import (
	apply_terms_to_product,
	find_product_for_terms,
	import_token_terms_from_file,
	recompute_next_income_dates,
	resolve_finstore_institution,
)


class Command(BaseCommand):
	help = (
		'Sync Finstore token terms from FINSTORE_TERMS_FILE, FINSTORE_TERMS_URL, or --file. '
		'Falls back to the same CSV/JSON import as import_finstore_token_terms.'
	)

	def add_arguments(self, parser):
		parser.add_argument('--file', default=None, help='Optional CSV/JSON path (overrides env).')
		parser.add_argument('--url', default=None, help='Optional JSON URL (overrides FINSTORE_TERMS_URL).')
		parser.add_argument('--institution', default='finstore', help='Institution slug (default: finstore).')
		parser.add_argument('--dry-run', action='store_true', help='Match rows without saving changes.')
		parser.add_argument(
			'--recompute-dates-only',
			action='store_true',
			help='Only estimate next_income_date from transaction history.',
		)
		parser.add_argument(
			'--overwrite-next-dates',
			action='store_true',
			help='Replace next_income_date even when already set.',
		)

	def handle(self, *args, **options):
		try:
			institution = resolve_finstore_institution(options['institution'])
		except FinancialInstitution.DoesNotExist as exc:
			raise CommandError(str(exc)) from exc

		if options['recompute_dates_only']:
			updated = recompute_next_income_dates(
				institution,
				overwrite=options['overwrite_next_dates'],
			)
			self.stdout.write(self.style.SUCCESS(f'Recomputed next_income_date for {updated} products.'))
			return

		if options['file']:
			result = import_token_terms_from_file(
				Path(options['file']),
				institution_slug=options['institution'],
				dry_run=options['dry_run'],
				recompute_dates=True,
				overwrite_next_dates=options['overwrite_next_dates'],
			)
			self._report_import_result(result, options['dry_run'])
			return

		client = FinstoreTokenCatalogClient()
		try:
			rows = client.fetch_rows(file_path=options['file'], url=options['url'])
		except FileNotFoundError as exc:
			raise CommandError(str(exc)) from exc

		matched = 0
		updated = 0
		unmatched: list[str] = []

		for row in rows:
			product = find_product_for_terms(institution, row)
			if product is None:
				unmatched.append(row.external_id or row.token_id or row.symbol or 'unknown')
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
			f'{mode} completed. rows={len(rows)} matched={matched} updated={updated} unmatched={len(unmatched)}'
		))
		if unmatched:
			self.stdout.write(self.style.WARNING(
				f'unmatched: {", ".join(unmatched[:20])}' + (' ...' if len(unmatched) > 20 else '')
			))

	def _report_import_result(self, result, dry_run: bool) -> None:
		mode = 'dry-run' if dry_run else 'sync'
		self.stdout.write(self.style.SUCCESS(
			f'{mode} completed. rows={result.rows_total} matched={result.matched} '
			f'updated={result.updated} skipped={result.skipped}'
		))
		if result.unmatched:
			self.stdout.write(self.style.WARNING(
				f'unmatched ({len(result.unmatched)}): {", ".join(result.unmatched[:20])}'
				+ (' ...' if len(result.unmatched) > 20 else '')
			))
