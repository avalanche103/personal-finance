from django.core.management.base import BaseCommand, CommandError

from apps.common.services.bynex_trades import build_transfer_row, load_bynex_transfer_rows, record_bynex_transfer


class Command(BaseCommand):
	help = 'Import manual BYNEX outgoing crypto transfers from CLI arguments or a CSV file.'

	def add_arguments(self, parser):
		parser.add_argument('--file', help='CSV file with BYNEX transfers.')
		parser.add_argument('--occurred-at', help='Transfer datetime, e.g. 2026-06-10 19:05:00.')
		parser.add_argument('--asset', default='USDT', help='Transferred asset, default USDT.')
		parser.add_argument('--quantity', help='Quantity received by destination, excluding fee.')
		parser.add_argument('--fee', default='0', help='Network/exchange fee in the same asset.')
		parser.add_argument('--destination', default='', help='Destination exchange or wallet.')
		parser.add_argument('--external-id', default='', help='Optional exchange transfer id for idempotency.')

	def handle(self, *args, **options):
		if options['file']:
			rows = load_bynex_transfer_rows(options['file'])
		else:
			if not options['quantity']:
				raise CommandError('Provide --quantity, or use --file.')
			rows = [
				build_transfer_row(
					occurred_at=options['occurred_at'],
					asset=options['asset'],
					quantity=options['quantity'],
					fee=options['fee'],
					destination=options['destination'],
					external_id=options['external_id'],
				)
			]

		created = 0
		for row in rows:
			result = record_bynex_transfer(row)
			created += result.created
			self.stdout.write(
				f'{row.occurred_at.isoformat()} transfer {row.quantity} {row.asset} '
				f'to {row.destination or "external wallet"}; fee={row.fee}; '
				f'product_units={result.product.units}; cash_balance={result.account.current_balance}'
			)
		self.stdout.write(self.style.SUCCESS(f'BYNEX transfers processed: {len(rows)}, records created: {created}.'))
