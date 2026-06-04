from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.institutions.models import FinancialInstitution
from apps.products.services.token_terms import import_token_terms_from_file


class Command(BaseCommand):
	help = 'Import Finstore token terms (rate, maturity, income schedule) from CSV or JSON.'

	def add_arguments(self, parser):
		parser.add_argument('--file', required=True, help='Path to CSV or JSON file with token terms.')
		parser.add_argument('--institution', default='finstore', help='Institution slug (default: finstore).')
		parser.add_argument('--dry-run', action='store_true', help='Match rows without saving changes.')
		parser.add_argument(
			'--no-recompute-dates',
			action='store_true',
			help='Skip estimating next_income_date from imported income history.',
		)
		parser.add_argument(
			'--overwrite-next-dates',
			action='store_true',
			help='Replace next_income_date even when already set.',
		)

	def handle(self, *args, **options):
		path = Path(options['file'])
		if not path.exists():
			raise CommandError(f'File not found: {path}')

		try:
			result = import_token_terms_from_file(
				path,
				institution_slug=options['institution'],
				dry_run=options['dry_run'],
				recompute_dates=not options['no_recompute_dates'],
				overwrite_next_dates=options['overwrite_next_dates'],
			)
		except FinancialInstitution.DoesNotExist as exc:
			raise CommandError(str(exc)) from exc

		mode = 'dry-run' if options['dry_run'] else 'import'
		self.stdout.write(self.style.SUCCESS(
			f'{mode} completed. rows={result.rows_total} matched={result.matched} '
			f'updated={result.updated} skipped={result.skipped}'
		))
		if result.unmatched:
			self.stdout.write(self.style.WARNING(
				f'unmatched ({len(result.unmatched)}): {", ".join(result.unmatched[:20])}'
				+ (' ...' if len(result.unmatched) > 20 else '')
			))
