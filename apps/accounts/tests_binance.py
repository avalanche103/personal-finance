from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from django.test import TestCase

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.accounts.services.binance import (
	_daily_kline_close_index,
	normalize_binance_asset,
	sync_daily_account_snapshots,
	sync_earn_and_funding,
	sync_spot_balances,
	sync_spot_history,
)
from apps.imports.services.integrations.binance import BinanceApiError, BinanceClient, BinanceSymbol
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product


class FakeBinanceClient:
	def fetch_account(self):
		return {
			'balances': [
				{'asset': 'BTC', 'free': '0.50000000', 'locked': '0.00000000'},
				{'asset': 'LDBTC', 'free': '0.10000000', 'locked': '0.00000000'},
				{'asset': 'USDT', 'free': '100.12345678', 'locked': '0.00000000'},
				{'asset': 'RWUSD', 'free': '7.77000000', 'locked': '0.00000000'},
				{'asset': 'ZERO', 'free': '0.00000000', 'locked': '0.00000000'},
			],
		}

	def fetch_ticker_prices(self):
		return [{'symbol': 'BTCUSDT', 'price': '70000.00000000'}]

	def fetch_klines(self, symbol, interval='1d', start_time=None, end_time=None, limit=1000):
		return [
			[1764547200000, '69000.00000000', '70000.00000000', '68000.00000000', '69500.00000000', '0', 1764633599999, '0', 0, '0', '0', 0],
			[1767225600000, '70000.00000000', '71000.00000000', '69000.00000000', '70500.00000000', '0', 1767311999999, '0', 0, '0', '0', 0],
		]

	def fetch_symbol_map(self):
		return {'BTCUSDT': BinanceSymbol(symbol='BTCUSDT', base_asset='BTC', quote_asset='USDT')}

	def fetch_my_trades(self, symbol, start_time=None, end_time=None):
		return [
			{
				'id': 123,
				'price': '70000.00000000',
				'qty': '0.10000000',
				'quoteQty': '7000.00000000',
				'commission': '0.00010000',
				'commissionAsset': 'BNB',
				'time': 1767225600000,
				'isBuyer': True,
			},
		]

	def fetch_funding_assets(self):
		return [{'asset': 'USDT', 'free': '12.34000000', 'locked': '0', 'freeze': '0'}]

	def fetch_simple_earn_flexible_positions(self):
		return {'rows': [{'asset': 'LDBTC', 'totalAmount': '0.20000000'}, {'asset': 'USDC', 'totalAmount': '10.00000000'}]}

	def fetch_simple_earn_locked_positions(self):
		return {'rows': [{'asset': 'ETH', 'amount': '1.50000000'}]}

	def fetch_account_snapshots(self, snapshot_type, *, start_time=None, end_time=None, limit=30):
		return [
			{
				'updateTime': 1764547200000,
				'type': 'spot',
				'data': {
					'balances': [
						{'asset': 'BTC', 'free': '0.40000000', 'locked': '0.00000000'},
						{'asset': 'LDBTC', 'free': '0.10000000', 'locked': '0.00000000'},
					],
					'totalAssetOfBtc': '0.50000000',
				},
			},
			{
				'updateTime': 1767225600000,
				'type': 'spot',
				'data': {
					'balances': [
						{'asset': 'BTC', 'free': '0.50000000', 'locked': '0.00000000'},
						{'asset': 'LDBTC', 'free': '0.10000000', 'locked': '0.00000000'},
						{'asset': 'USDT', 'free': '100.00000000', 'locked': '0.00000000'},
					],
					'totalAssetOfBtc': '0.51000000',
				},
			},
		]


class BinanceClientTests(TestCase):
	def test_signature_uses_hmac_sha256(self):
		client = BinanceClient(api_key='key', api_secret='secret', base_url='https://example.test')

		signature = client._signature('symbol=BTCUSDT&timestamp=1')

		self.assertEqual(signature, 'ef9d3d77a34d9a13a21a4c2d7f3e8cb091888a74ca62b5b62f430e78eded95ba')

	def test_formats_binance_http_error_body(self):
		client = BinanceClient(api_key='key', api_secret='secret', base_url='https://example.test')
		body = BytesIO(b'{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}')
		error = HTTPError('https://example.test/api/v3/account', 400, 'Bad Request', {}, body)

		message = client._format_request_error(error)

		self.assertEqual(message, 'Binance API error -1021: Timestamp for this request is outside of the recvWindow.')

	def test_signed_request_includes_recv_window(self):
		client = BinanceClient(api_key='key', api_secret='secret', base_url='https://example.test')

		with patch('apps.imports.services.integrations.binance.urlopen') as urlopen:
			urlopen.return_value.__enter__.return_value.read.return_value = b'{"makerCommission":0}'
			client.fetch_account()

		request = urlopen.call_args.args[0]
		self.assertIn('recvWindow=5000', request.full_url)

	def test_normalizes_binance_wrapper_assets(self):
		self.assertEqual(normalize_binance_asset('RWUSD'), 'RWUSD')
		self.assertEqual(normalize_binance_asset('LDBTC'), 'BTC')
		self.assertEqual(normalize_binance_asset('LDWBETH'), 'WBETH')


class FailingKlineBinanceClient(FakeBinanceClient):
	def fetch_klines(self, symbol, interval='1d', start_time=None, end_time=None, limit=1000):
		if symbol == 'BTCUSDT':
			raise BinanceApiError('Binance API error -1121: Invalid symbol.')
		return super().fetch_klines(symbol, interval=interval, start_time=start_time, end_time=end_time, limit=limit)


class BinanceSyncTests(TestCase):
	def test_spot_balances_create_account_product_and_snapshots(self):
		result = sync_spot_balances(client=FakeBinanceClient(), create_snapshots=True)

		self.assertEqual(result.rows_detected, 3)
		institution = FinancialInstitution.objects.get(slug='binance')
		usdt_account = Account.objects.get(institution=institution, currency__code='USDT')
		self.assertEqual(str(usdt_account.current_balance), '100.12')
		self.assertEqual(str(usdt_account.current_balance_usd), '100.12')
		rwusd_account = Account.objects.get(institution=institution, currency__code='RWUSD')
		self.assertEqual(str(rwusd_account.current_balance), '7.77')
		self.assertEqual(str(rwusd_account.current_balance_usd), '7.77')

		btc = Product.objects.get(institution=institution, external_id='binance:spot:BTC')
		self.assertEqual(btc.product_type, Product.ProductType.CRYPTO)
		self.assertEqual(str(btc.units), '0.600000')
		self.assertEqual(str(btc.current_price), '70000.00000000')
		self.assertEqual(str(btc.current_value_usd), '42000.00')
		self.assertEqual(btc.metadata['raw_assets'], ['BTC', 'LDBTC'])
		self.assertEqual(BalanceSnapshot.objects.filter(institution=institution).count(), 3)

	def test_spot_history_is_idempotent_and_excluded_from_snapshot_balances(self):
		result = sync_spot_history(client=FakeBinanceClient(), symbols=['BTCUSDT'])

		self.assertEqual(result.records_created, 2)
		trade = Transaction.objects.get(transaction_type=Transaction.TransactionType.TRADE)
		self.assertEqual(str(trade.amount), '-7000.00')
		self.assertEqual(str(trade.quantity), '0.100000')
		self.assertTrue(trade.metadata['exclude_from_account_balance'])
		fee = Transaction.objects.get(transaction_type=Transaction.TransactionType.FEE)
		self.assertEqual(fee.product.symbol, 'BNB')

		second = sync_spot_history(client=FakeBinanceClient(), symbols=['BTCUSDT'])
		self.assertEqual(second.skipped, 1)
		self.assertEqual(Transaction.objects.count(), 2)

	def test_earn_and_funding_create_separate_products(self):
		sync_spot_balances(client=FakeBinanceClient(), create_snapshots=True)
		result = sync_earn_and_funding(client=FakeBinanceClient())

		self.assertEqual(result.rows_detected, 4)
		institution = FinancialInstitution.objects.get(slug='binance')
		self.assertTrue(Account.objects.filter(institution=institution, external_id='binance:funding:USDT').exists())
		self.assertFalse(Product.objects.filter(institution=institution, external_id='binance:spot:USDC').exists())
		self.assertFalse(Product.objects.filter(institution=institution, external_id='binance:earn_flexible:BTC').exists())
		self.assertEqual(str(Product.objects.get(institution=institution, external_id='binance:spot:BTC').units), '0.600000')
		self.assertTrue(Product.objects.filter(institution=institution, external_id='binance:earn_locked:ETH').exists())

	def test_earn_locked_products_deactivated_when_missing_from_api(self):
		from apps.common.models import Currency

		sync_spot_balances(client=FakeBinanceClient())
		institution = FinancialInstitution.objects.get(slug='binance')
		usd = Currency.objects.get(code='USD')
		ton = Product.objects.create(
			institution=institution,
			name='Binance TON Earn Locked',
			symbol='TON',
			external_id='binance:earn_locked:TON',
			product_type=Product.ProductType.CRYPTO,
			currency=usd,
			units=Decimal('18.367029'),
			current_price=Decimal('1.50'),
			current_value_usd=Decimal('27.55'),
			metadata={'source': 'binance', 'asset': 'TON', 'product_area': 'earn_locked'},
			is_active=True,
		)

		sync_earn_and_funding(client=FakeBinanceClient())

		ton.refresh_from_db()
		self.assertFalse(ton.is_active)
		self.assertEqual(ton.units, Decimal('0'))
		self.assertEqual(ton.current_value_usd, Decimal('0'))
		self.assertTrue(ton.metadata.get('stale_after_normalization'))

	def test_daily_account_snapshots_import_spot_history_and_skip_duplicates(self):
		first = sync_daily_account_snapshots(client=FakeBinanceClient(), days=30)
		self.assertEqual(first.rows_detected, 2)
		self.assertGreater(first.records_created, 0)

		institution = FinancialInstitution.objects.get(slug='binance')
		btc = Product.objects.get(institution=institution, external_id='binance:spot:BTC')
		historical = BalanceSnapshot.objects.filter(product=btc, metadata__snapshot_type='spot').order_by('captured_at')
		self.assertEqual(historical.count(), 2)
		self.assertEqual(str(historical.first().balance), '0.500000')
		self.assertEqual(str(historical.last().balance), '0.600000')
		self.assertEqual(str(historical.first().balance_usd), '34750.00')
		self.assertEqual(historical.first().metadata.get('price_usd'), '69500.00000000')
		self.assertTrue(any(item.metadata.get('flexible_earn_included') for item in historical))

		second = sync_daily_account_snapshots(client=FakeBinanceClient(), days=30)
		self.assertEqual(second.records_created, 1)
		self.assertEqual(BalanceSnapshot.objects.filter(product=btc, metadata__snapshot_type='spot').count(), 2)
		self.assertEqual(BalanceSnapshot.objects.filter(institution=institution, metadata__snapshot_type='spot').count(), 3)
		self.assertTrue(
			BalanceSnapshot.objects.filter(institution=institution, metadata__snapshot_type='earn_locked').exists()
		)

	def test_daily_kline_errors_do_not_abort_snapshot_import(self):
		client = FailingKlineBinanceClient()
		index, kline_errors = _daily_kline_close_index(client, {'BTCUSDT'}, 1764547200000, 1767225600000)

		self.assertEqual(kline_errors, {'BTCUSDT': 'Binance API error -1121: Invalid symbol.'})
		self.assertEqual(index, {})

		result = sync_daily_account_snapshots(client=client, days=30)

		self.assertGreater(result.records_created, 0)
		self.assertEqual(result.details['kline_errors'], {'BTCUSDT': 'Binance API error -1121: Invalid symbol.'})
