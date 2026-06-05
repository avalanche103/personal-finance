from __future__ import annotations

from datetime import datetime, time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.services.binance import (
	BinanceSyncResult,
	sync_deposits_withdrawals,
	sync_earn_and_funding,
	sync_spot_balances,
	sync_spot_history,
)


def _date_to_ms(value: str | None, *, end_of_day: bool = False) -> int | None:
	if not value:
		return None
	try:
		day = datetime.fromisoformat(value).date()
	except ValueError as exc:
		raise CommandError(f'Invalid date format for Binance sync: {value}') from exc
	dt = datetime.combine(day, time.max if end_of_day else time.min)
	return int(timezone.make_aware(dt).timestamp() * 1000)


class Command(BaseCommand):
	help = 'Sync Binance Spot, history, transfers, Earn, and Funding data.'

	def add_arguments(self, parser):
		parser.add_argument('--spot', action='store_true', help='Sync Spot balances and current valuation.')
		parser.add_argument('--history', action='store_true', help='Sync Spot trade history. Requires --symbols.')
		parser.add_argument('--transfers', action='store_true', help='Sync deposits and withdrawals.')
		parser.add_argument('--earn', action='store_true', help='Sync Simple Earn and Funding wallet positions.')
		parser.add_argument('--funding', action='store_true', help='Alias for --earn; kept for command readability.')
		parser.add_argument('--snapshots', action='store_true', help='Create BalanceSnapshot rows during Spot balance sync.')
		parser.add_argument('--symbols', default='', help='Comma-separated Binance symbols for --history, e.g. BTCUSDT,ETHUSDT.')
		parser.add_argument('--start-date', default=None, help='Optional start date in YYYY-MM-DD format for history/transfers.')
		parser.add_argument('--end-date', default=None, help='Optional end date in YYYY-MM-DD format for history/transfers.')
		parser.add_argument('--dry-run', action='store_true', help='Fetch and summarize Binance data without writing to the database.')
		parser.add_argument('--skip-missing-credentials', action='store_true', help='Skip sync instead of failing when Binance API credentials are not configured.')

	def _write_result(self, result: BinanceSyncResult):
		self.stdout.write(self.style.SUCCESS(
			f'Binance {result.scope} sync completed. '
			f'job_id={result.job_id or "-"} rows_detected={result.rows_detected} '
			f'created={result.records_created} updated={result.records_updated} skipped={result.skipped}'
		))
		if result.details:
			self.stdout.write(f'details={result.details}')

	def handle(self, *args, **options):
		run_spot = options['spot']
		run_history = options['history']
		run_transfers = options['transfers']
		run_earn = options['earn'] or options['funding']
		if not any([run_spot, run_history, run_transfers, run_earn]):
			run_spot = True

		start_time = _date_to_ms(options['start_date'])
		end_time = _date_to_ms(options['end_date'], end_of_day=True)
		dry_run = options['dry_run']
		if not settings.BINANCE_API_KEY or not settings.BINANCE_API_SECRET:
			message = 'BINANCE_API_KEY and BINANCE_API_SECRET are not configured.'
			if options['skip_missing_credentials']:
				self.stdout.write(self.style.WARNING(f'{message} Skipping Binance sync.'))
				return
			raise CommandError(message)

		if run_spot:
			self._write_result(sync_spot_balances(create_snapshots=options['snapshots'], dry_run=dry_run))

		if run_history:
			symbols = [symbol.strip().upper() for symbol in options['symbols'].split(',') if symbol.strip()]
			if not symbols:
				raise CommandError('--history requires --symbols BTCUSDT,ETHUSDT,...')
			self._write_result(sync_spot_history(symbols=symbols, start_time=start_time, end_time=end_time, dry_run=dry_run))

		if run_transfers:
			self._write_result(sync_deposits_withdrawals(start_time=start_time, end_time=end_time, dry_run=dry_run))

		if run_earn:
			self._write_result(sync_earn_and_funding(dry_run=dry_run))
