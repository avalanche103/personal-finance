from django.core.management.base import BaseCommand, CommandError

from apps.common.services.bynex_trades import build_trade_row, load_bynex_trade_rows, record_bynex_trade


class Command(BaseCommand):
	help = 'Import manual BYNEX spot trades from CLI arguments or a CSV file.'

	def add_arguments(self, parser):
		parser.add_argument('--file', help='CSV file with BYNEX trades.')
		parser.add_argument('--occurred-at', help='Trade datetime, e.g. 2026-06-10 19:00:00.')
		parser.add_argument('--side', default='buy', choices=('buy', 'sell'), help='Trade side.')
		parser.add_argument('--base-asset', default='USDT', help='Bought/sold asset, default USDT.')
		parser.add_argument('--quote-currency', default='USD', help='Quote currency, default USD.')
		parser.add_argument('--quantity', help='Base asset quantity.')
		parser.add_argument('--price', help='Execution price in quote currency.')
		parser.add_argument('--fee', default='0', help='Fee amount in quote currency.')
		parser.add_argument('--total', help='Total quote amount including fee. If omitted, calculated from quantity, price, fee.')
		parser.add_argument('--external-id', default='', help='Optional exchange order/trade id for idempotency.')

	def handle(self, *args, **options):
		if options['file']:
			rows = load_bynex_trade_rows(options['file'])
		else:
			if not options['quantity'] or not options['price']:
				raise CommandError('Provide --quantity and --price, or use --file.')
			rows = [
				build_trade_row(
					occurred_at=options['occurred_at'],
					side=options['side'],
					base_asset=options['base_asset'],
					quote_currency=options['quote_currency'],
					quantity=options['quantity'],
					price=options['price'],
					fee=options['fee'],
					total=options['total'],
					external_id=options['external_id'],
				)
			]

		created = 0
		for row in rows:
			result = record_bynex_trade(row)
			if result.created:
				created += 1
			self.stdout.write(
				f'{row.occurred_at.isoformat()} {row.side} {row.quantity} {row.base_asset} '
				f'@ {row.price} {row.quote_currency}; total={result.exact_total}; '
				f'product_units={result.product.units}; cash_balance={result.account.current_balance}'
			)
		self.stdout.write(self.style.SUCCESS(f'BYNEX trades processed: {len(rows)}, created: {created}.'))
