from decimal import Decimal
from io import BytesIO

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.accounts.models import Transaction
from apps.common.management.commands.bootstrap_local_data import Command as BootstrapCommand
from apps.common.services.bynex_trades import build_trade_row, build_transfer_row, record_bynex_trade, record_bynex_transfer
from apps.common.services.finstore_reconciliation import reconcile_finstore_products
from apps.imports.models import ImportSource
from apps.imports.services.pipeline import process_uploaded_import
from apps.products.models import Product


class FinstoreReconciliationTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def test_reconciliation_links_legacy_transactions_and_closes_redeemed_product(self):
		source = ImportSource.objects.get(code='finstore-history')
		workbook = BytesIO()
		pd.DataFrame(
			[
				['История операций', '', '', '', ''],
				['Вид операции', 'Название токена', 'Количество токенов', 'Сумма валюты', 'Дата'],
				['Пополнение кошелька', '', '', '100 USD.sc', '46157.300000000000'],
				['Покупка токенов', 'LEGACY_(USD_1001)', '2', '100 USD.sc', '46157.429363425923'],
				['Возврат инвестиций', 'LEGACY_(USD_1001)', '2', '110 USD.sc', '46187.429363425923'],
			]
		).to_excel(workbook, index=False, header=False)
		workbook.seek(0)

		upload = SimpleUploadedFile(
			'Finstore_legacy.xlsx',
			workbook.getvalue(),
			content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
		)
		job, _ = process_uploaded_import(source, upload)

		Transaction.objects.filter(
			import_job=job,
			metadata__token_name='LEGACY_(USD_1001)',
			metadata__operation_type='Возврат инвестиций',
		).update(product=None, quantity=Decimal('2'))
		Transaction.objects.filter(
			import_job=job,
			metadata__token_name='LEGACY_(USD_1001)',
			metadata__operation_type='Покупка токенов',
		).update(product=None)
		product = Product.objects.get(external_id='LEGACY_(USD_1001)')
		product.units = Decimal('2')
		product.is_active = True
		product.save(update_fields=['units', 'is_active', 'updated_at'])

		result = reconcile_finstore_products()

		product.refresh_from_db()
		redemption = Transaction.objects.get(import_job=job, metadata__operation_type='Возврат инвестиций')
		linked_transactions = Transaction.objects.filter(metadata__token_name='LEGACY_(USD_1001)', product=product).count()
		self.assertEqual(linked_transactions, 2)
		self.assertEqual(str(redemption.quantity), '-2.000000')
		self.assertEqual(str(product.units), '0.000000')
		self.assertFalse(product.is_active)
		self.assertEqual(str(product.current_value_usd), '0.00')
		self.assertGreaterEqual(result['transactions_linked'], 2)
		self.assertGreaterEqual(result['normalized_transactions'], 1)

	def test_reconciliation_closes_early_redeemed_product(self):
		source = ImportSource.objects.get(code='finstore-history')
		workbook = BytesIO()
		pd.DataFrame(
			[
				['История операций', '', '', '', ''],
				['Вид операции', 'Название токена', 'Количество токенов', 'Сумма валюты', 'Дата'],
				['Пополнение кошелька', '', '', '50 BYN.sc', '10.06.2026 10:00:00'],
				['Покупка токенов', 'BLESAVARIS_(BYN_442)', '1', '50 BYN.sc', '01.05.2026 10:00:00'],
				['Досрочное погашение токенов', 'BLESAVARIS_(BYN_442)', '1', '50 BYN.sc', '10.06.2026 10:00:00'],
			]
		).to_excel(workbook, index=False, header=False)
		workbook.seek(0)

		upload = SimpleUploadedFile(
			'Finstore_early_legacy.xlsx',
			workbook.getvalue(),
			content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
		)
		job, _ = process_uploaded_import(source, upload)

		Transaction.objects.filter(
			import_job=job,
			metadata__token_name='BLESAVARIS_(BYN_442)',
			metadata__operation_type='Досрочное погашение токенов',
		).update(product=None, quantity=Decimal('1'), transaction_type=Transaction.TransactionType.OTHER)
		product = Product.objects.get(external_id='BLESAVARIS_(BYN_442)')
		product.units = Decimal('2')
		product.is_active = True
		product.save(update_fields=['units', 'is_active', 'updated_at'])

		result = reconcile_finstore_products()

		product.refresh_from_db()
		redemption = Transaction.objects.get(import_job=job, metadata__operation_type='Досрочное погашение токенов')
		self.assertEqual(redemption.product, product)
		self.assertEqual(redemption.transaction_type, Transaction.TransactionType.INCOME)
		self.assertEqual(str(redemption.quantity), '-1.000000')
		self.assertEqual(str(product.units), '0.000000')
		self.assertFalse(product.is_active)
		self.assertGreaterEqual(result['normalized_transactions'], 1)


class BynexTradeTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def test_record_bynex_usdt_buy_updates_cash_and_position(self):
		row = build_trade_row(
			occurred_at='2026-06-10 19:00:00',
			side='buy',
			base_asset='USDT',
			quote_currency='USD',
			quantity='268.371',
			price='1.00411',
			fee='0.06736',
			total='269.54137',
			external_id='bynex-usdt-buy-2026-06-10',
		)

		result = record_bynex_trade(row)

		self.assertTrue(result.created)
		self.assertEqual(str(result.transaction.quantity), '268.371000')
		self.assertEqual(str(result.transaction.amount), '-269.54')
		self.assertEqual(str(result.transaction.unit_price), '1.00411000')
		self.assertEqual(result.transaction.metadata['gross_amount_exact'], '269.47400481')
		self.assertEqual(result.transaction.metadata['fee_amount_exact'], '0.06736')
		self.assertEqual(result.transaction.metadata['total_amount_exact'], '269.54137')
		self.assertEqual(str(result.product.units), '268.371000')
		self.assertEqual(str(result.product.current_price), '1.00411000')
		self.assertEqual(str(result.product.current_value_usd), '269.47')
		self.assertEqual(str(result.account.current_balance), '-88.79')

		repeated = record_bynex_trade(row)
		self.assertFalse(repeated.created)
		self.assertEqual(Transaction.objects.filter(import_fingerprint='bynex:trade:bynex-usdt-buy-2026-06-10').count(), 1)

	def test_record_bynex_transfer_and_fee_closes_usdt_position(self):
		trade_row = build_trade_row(
			occurred_at='2026-06-10 19:00:00',
			side='buy',
			base_asset='USDT',
			quote_currency='USD',
			quantity='268.371',
			price='1.00411',
			fee='0.06736',
			total='269.54137',
			external_id='bynex-usdt-buy-2026-06-10',
		)
		record_bynex_trade(trade_row)
		transfer_row = build_transfer_row(
			occurred_at='2026-06-10 19:05:00',
			asset='USDT',
			quantity='263.371',
			fee='5',
			destination='Binance',
			external_id='bynex-usdt-to-binance-2026-06-10',
		)

		result = record_bynex_transfer(transfer_row)

		self.assertEqual(result.created, 2)
		self.assertEqual(str(result.transfer.quantity), '-263.371000')
		self.assertEqual(result.transfer.transaction_type, Transaction.TransactionType.TRANSFER)
		self.assertIsNotNone(result.fee_transaction)
		self.assertEqual(str(result.fee_transaction.quantity), '-5.000000')
		self.assertEqual(result.fee_transaction.transaction_type, Transaction.TransactionType.FEE)
		self.assertEqual(str(result.product.units), '0.000000')
		self.assertEqual(str(result.product.current_value_usd), '0.00')
		self.assertFalse(result.product.is_active)
		self.assertEqual(str(result.account.current_balance), '-88.79')

		repeated = record_bynex_transfer(transfer_row)
		self.assertEqual(repeated.created, 0)
