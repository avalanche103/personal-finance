from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.accounts.services.binance import (
	normalize_binance_asset,
	sync_earn_and_funding,
	sync_spot_balances,
	sync_spot_history,
)
from apps.imports.services.integrations.binance import BinanceClient, BinanceSymbol
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


class BinanceClientTests(TestCase):
	def test_signature_uses_hmac_sha256(self):
		client = BinanceClient(api_key='key', api_secret='secret', base_url='https://example.test')

		signature = client._signature('symbol=BTCUSDT&timestamp=1')

		self.assertEqual(signature, 'ef9d3d77a34d9a13a21a4c2d7f3e8cb091888a74ca62b5b62f430e78eded95ba')

	def test_normalizes_binance_wrapper_assets(self):
		self.assertEqual(normalize_binance_asset('RWUSD'), 'RWUSD')
		self.assertEqual(normalize_binance_asset('LDBTC'), 'BTC')
		self.assertEqual(normalize_binance_asset('LDWBETH'), 'WBETH')


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
