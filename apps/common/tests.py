from decimal import Decimal
from io import BytesIO

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.accounts.models import Transaction
from apps.common.management.commands.bootstrap_local_data import Command as BootstrapCommand
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
