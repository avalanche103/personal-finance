from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.common.services.exchange_rates import sync_nbrb_rate_history


class Command(BaseCommand):
	help = 'Fetch and persist NBRB exchange rate history for tracked currencies.'

	def add_arguments(self, parser):
		parser.add_argument('--start-date', default='2024-01-01', help='Start date in YYYY-MM-DD format.')
		parser.add_argument('--end-date', default=None, help='Optional end date in YYYY-MM-DD format.')

	def handle(self, *args, **options):
		try:
			start_date = date.fromisoformat(options['start_date'])
			end_date = date.fromisoformat(options['end_date']) if options['end_date'] else None
		except ValueError as exc:
			raise CommandError(f'Invalid date format: {exc}') from exc

		result = sync_nbrb_rate_history(start_date=start_date, end_date=end_date)
		self.stdout.write(self.style.SUCCESS(
			f"NBRB sync completed. job_id={result['job_id']} rows_detected={result['rows_detected']} stored_total={result['stored_total']} new_rows={result['records_created']}"
		))