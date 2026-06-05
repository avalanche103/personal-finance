from io import BytesIO

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Account, Transaction
from apps.common.management.commands.bootstrap_local_data import Command as BootstrapCommand
from apps.imports.models import ImportJob, ImportSource
from apps.imports.services.pipeline import process_clipboard_import, process_uploaded_import
from apps.products.models import Product


class ImportPipelineSmokeTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def test_manual_upload_creates_single_idempotent_job(self):
		source = ImportSource.objects.create(
			name='Manual Test Source',
			code='manual-test-source',
			source_type=ImportSource.SourceType.MANUAL,
			is_active=True,
		)
		upload_one = SimpleUploadedFile(
			'portfolio.csv',
			b'date,amount\n2026-05-31,100\n',
			content_type='text/csv',
		)
		job_one, created_one = process_uploaded_import(source, upload_one)
		self.assertTrue(created_one)
		self.assertEqual(job_one.status, ImportJob.Status.SAVED)

		upload_two = SimpleUploadedFile(
			'portfolio-copy.csv',
			b'date,amount\n2026-05-31,100\n',
			content_type='text/csv',
		)
		job_two, created_two = process_uploaded_import(source, upload_two)
		self.assertFalse(created_two)
		self.assertEqual(job_one.pk, job_two.pk)

	def test_finstore_history_creates_products_per_token(self):
		source = ImportSource.objects.get(code='finstore-history')
		workbook = BytesIO()
		pd.DataFrame(
			[
				['История операций', '', '', '', ''],
				['Вид операции', 'Название токена', 'Количество токенов', 'Сумма валюты', 'Дата'],
				['Пополнение кошелька', '', '', '20 USD.sc', '46157.300000000000'],
				['Пополнение кошелька', '', '', '100 BYN.sc', '46157.310000000000'],
				['Покупка токенов', 'YOWHEELS_(USD_864)', '1', '10 USD.sc', '46157.429363425923'],
				['Получение дохода', 'YOWHEELS_(USD_864)', '', '0.43 USD.sc', '46158.131145833337'],
				['Покупка токенов', 'SMART_(BYN_868)', '2', '40 BYN.sc', '46157.428969907407'],
			]
		).to_excel(workbook, index=False, header=False)
		workbook.seek(0)

		upload = SimpleUploadedFile(
			'Finstore_history.xlsx',
			workbook.getvalue(),
			content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
		)

		job, created = process_uploaded_import(source, upload)
		self.assertTrue(created)
		self.assertEqual(job.status, ImportJob.Status.SAVED)
		self.assertEqual(job.records_created, 7)
		self.assertIn('editable_records', job.details)
		self.assertGreaterEqual(len(job.details['editable_records']), 5)

		products = Product.objects.filter(institution=source.institution, external_id__in=['YOWHEELS_(USD_864)', 'SMART_(BYN_868)']).order_by('external_id')
		self.assertEqual(products.count(), 2)
		self.assertEqual(products[0].product_type, Product.ProductType.TOKEN)
		self.assertEqual(products[0].currency.code, 'BYN')
		self.assertEqual(str(products[0].units), '2.00000000')
		self.assertEqual(str(products[0].current_price), '20.00000000')
		self.assertGreater(products[0].current_value_usd, 0)
		self.assertEqual(products[1].currency.code, 'USD')
		self.assertEqual(str(products[1].units), '1.00000000')
		self.assertEqual(str(products[1].current_price), '10.00000000')
		self.assertEqual(str(products[1].current_value_usd), '10.00')
		self.assertEqual(job.details['metadata']['parser_variant'], 'finstore-history')
		self.assertEqual(job.details['metadata']['products_created'], 2)
		self.assertEqual(job.details['metadata']['transactions_created'], 5)
		self.assertEqual(job.details['metadata']['accounts_synced'], 4)

		transactions = Transaction.objects.filter(import_job=job).order_by('occurred_at', 'id')
		self.assertEqual(transactions.count(), 5)
		self.assertEqual(transactions[0].transaction_type, Transaction.TransactionType.DEPOSIT)
		self.assertEqual(transactions[0].currency.code, 'USD')
		self.assertEqual(str(transactions[0].amount), '20.00')
		self.assertEqual(transactions[1].transaction_type, Transaction.TransactionType.DEPOSIT)
		self.assertEqual(transactions[1].currency.code, 'BYN')
		self.assertEqual(str(transactions[1].amount), '100.00')
		self.assertEqual(transactions[2].currency.code, 'BYN')
		self.assertEqual(transactions[2].transaction_type, Transaction.TransactionType.TRADE)
		self.assertEqual(str(transactions[2].amount), '-40.00')
		self.assertEqual(transactions[3].currency.code, 'USD')
		self.assertEqual(transactions[3].product, products[1])
		self.assertEqual(str(transactions[3].amount), '-10.00')
		self.assertEqual(transactions[4].transaction_type, Transaction.TransactionType.INCOME)
		self.assertEqual(transactions[4].product, products[1])
		self.assertEqual(str(transactions[4].amount), '0.43')

		byn_account = Account.objects.get(institution=source.institution, currency__code='BYN')
		usd_account = Account.objects.get(institution=source.institution, currency__code='USD')
		eur_account = Account.objects.get(institution=source.institution, currency__code='EUR')
		self.assertEqual(str(byn_account.current_balance), '60.00')
		self.assertGreater(byn_account.current_balance_usd, 0)
		self.assertEqual(str(usd_account.current_balance), '10.43')
		self.assertEqual(str(usd_account.current_balance_usd), '10.43')
		self.assertEqual(str(eur_account.current_balance), '0.00')

	def test_finstore_redemption_closes_token_position(self):
		source = ImportSource.objects.get(code='finstore-history')
		workbook = BytesIO()
		pd.DataFrame(
			[
				['История операций', '', '', '', ''],
				['Вид операции', 'Название токена', 'Количество токенов', 'Сумма валюты', 'Дата'],
				['Пополнение кошелька', '', '', '100 USD.sc', '46157.300000000000'],
				['Покупка токенов', 'EXIT_(USD_999)', '2', '100 USD.sc', '46157.429363425923'],
				['Возврат инвестиций', 'EXIT_(USD_999)', '2', '110 USD.sc', '46187.429363425923'],
			]
		).to_excel(workbook, index=False, header=False)
		workbook.seek(0)

		upload = SimpleUploadedFile(
			'Finstore_redemption.xlsx',
			workbook.getvalue(),
			content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
		)

		job, created = process_uploaded_import(source, upload)
		self.assertTrue(created)
		self.assertEqual(job.status, ImportJob.Status.SAVED)

		product = Product.objects.get(institution=source.institution, external_id='EXIT_(USD_999)')
		self.assertEqual(str(product.units), '0E-8')
		self.assertFalse(product.is_active)
		self.assertEqual(str(product.current_value_usd), '0.00')

		transactions = list(Transaction.objects.filter(import_job=job).order_by('occurred_at', 'id'))
		self.assertEqual(len(transactions), 3)
		self.assertIsNone(transactions[0].product)
		self.assertEqual(transactions[1].product, product)
		self.assertEqual(transactions[1].transaction_type, Transaction.TransactionType.TRADE)
		self.assertEqual(str(transactions[1].quantity), '2.00000000')
		self.assertEqual(transactions[2].product, product)
		self.assertEqual(transactions[2].transaction_type, Transaction.TransactionType.INCOME)
		self.assertEqual(str(transactions[2].quantity), '-2.00000000')

		usd_account = Account.objects.get(institution=source.institution, currency__code='USD')
		self.assertEqual(str(usd_account.current_balance), '110.00')

	def test_finstore_clipboard_import_creates_income_transactions(self):
		source = ImportSource.objects.get(code='finstore-history')
		clipboard_text = (
			'Вид операции\n'
			'Название токена\n'
			'Количество токенов\n'
			'Сумма валюты\n'
			'Получение дохода\tPOLESIE_(USD_676)\t\t0.63 USD.sc\t20.05.2026 03:01:41\t\n'
			'Получение дохода\tSMART_(BYN_804)\t\t1.27 BYN.sc\t20.05.2026 03:01:41\t\n'
			'Получение дохода\tPOLESIE_(USD_626)\t\t0.53 USD.sc\t20.05.2026 03:01:41\t\n'
		)

		job, created = process_clipboard_import(source, clipboard_text)

		self.assertTrue(created)
		self.assertEqual(job.status, ImportJob.Status.SAVED)
		self.assertEqual(job.details['metadata']['import_channel'], 'clipboard')
		self.assertEqual(job.rows_detected, 3)

		transactions = list(Transaction.objects.filter(import_job=job).order_by('description'))
		self.assertEqual(len(transactions), 3)
		self.assertTrue(all(transaction.transaction_type == Transaction.TransactionType.INCOME for transaction in transactions))
		self.assertEqual(str(transactions[0].amount), '0.53')
		self.assertEqual(str(transactions[1].amount), '0.63')
		self.assertEqual(str(transactions[2].amount), '1.27')

		products = Product.objects.filter(institution=source.institution, external_id__in=['POLESIE_(USD_676)', 'POLESIE_(USD_626)', 'SMART_(BYN_804)']).order_by('external_id')
		self.assertEqual(products.count(), 3)
		self.assertTrue(all(str(product.units) == '0E-8' for product in products))

	def test_import_upload_view_accepts_finstore_clipboard_text(self):
		source = ImportSource.objects.get(code='finstore-history')
		response = self.client.post(
			reverse('imports:upload'),
			{
				'source': source.pk,
				'clipboard_text': 'Получение дохода\tPOLESIE_(USD_676)\t\t0.63 USD.sc\t20.05.2026 03:01:41\t',
			},
		)

		job = ImportJob.objects.filter(source=source).order_by('-created_at').first()
		self.assertRedirects(response, reverse('imports:detail', args=[job.pk]))
		self.assertEqual(ImportJob.objects.filter(source=source).count(), 1)

	def test_aigenis_report_creates_bond_products(self):
		from apps.institutions.models import FinancialInstitution

		institution = FinancialInstitution.objects.create(
			name='Aigenis Test',
			slug='aigenis-test',
			institution_type=FinancialInstitution.InstitutionType.BROKER,
		)
		byn = Account.objects.filter(currency__code='BYN').first().currency
		Account.objects.create(
			institution=institution,
			name='Aigenis BYN Account',
			account_type=Account.AccountType.BROKERAGE,
			currency=byn,
		)
		source = ImportSource.objects.create(
			name='Aigenis Broker Report',
			code='aigenis-report-test',
			source_type=ImportSource.SourceType.XLS,
			institution=institution,
			is_active=True,
		)

		workbook = BytesIO()
		pd.DataFrame(
			[
				['', 'Клиент', 'ИЗОТОВ АНТОН ВАДИМОВИЧ', '', '', '', '', '', 'ОТЧЕТ БРОКЕРА'],
				['', 'Договор №', 'A0906122022', 'от 06.12.2022'],
				['', 'Период с', '01.01.2026', 'по 04.06.2026'],
				['Дата совершения операции', 'Тип операции', 'Срок сделки (дней)', 'Вид ценной бумаги (источник пополнения)', 'Режим торгов', 'Эмитент', 'Наименование ценной бумаги', '№ гос.регистрации выпуска', 'Валюта операции', 'Цена покупки/продажи за единицу', 'Кол-во ценных бумаг (штук)', 'Сумма операции'],
				['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12'],
				['2026-01-01', 'Входящий', '', '', '', '', '', '', 'BYN', '', '', ''],
				['2026-02-23', 'Пополнение д.с.', '', 'Паритетбанк', '', '', '', '', '', '', '', '100.00'],
				[
					'2026-03-04', 'Покупка', '', 'Облигация', 'NDA', 'Айгенис закрытое акционерное общество',
					'Айгенис Оп47', 'BCSE-00477-P01', 'BYN', '516.97', '1', '516.97', '1.00', '0', '', '0.05', '0.01',
				],
				[
					'2026-03-13', 'Покупка', '', 'Облигация', 'NDA', 'Айгенис закрытое акционерное общество',
					'Айгенис Оп47', 'BCSE-00477-P01', 'BYN', '525.70', '1', '525.70', '1.00', '0', '', '0.05', '0.01',
				],
				[
					'2026-04-25', 'Покупка', '', 'Облигация', 'NDA', 'Айгенис закрытое акционерное общество',
					'Размещение - Айгенис Оп51_НДА', 'BCSE-00487-P02', 'BYN', '301.62', '1', '301.62', '1.00', '0', '', '0.03', '0.01',
				],
				[
					'2026-05-08', 'Покупка', '', 'Облигация', 'NDA', 'Айгенис закрытое акционерное общество',
					'Айгенис Оп51', 'BCSE-00487-P02', 'BYN', '302.35', '2', '604.70', '1.00', '0', '', '0.06', '0.01',
				],
			]
		).to_excel(workbook, index=False, header=False)
		workbook.seek(0)

		upload = SimpleUploadedFile(
			'Aigenis_report.xlsx',
			workbook.getvalue(),
			content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
		)

		job, created = process_uploaded_import(source, upload)
		self.assertTrue(created)
		self.assertEqual(job.status, ImportJob.Status.SAVED)
		self.assertEqual(job.details['metadata']['parser_variant'], 'aigenis-report')
		self.assertEqual(job.details['metadata']['products_created'], 2)
		self.assertEqual(job.details['metadata']['transactions_created'], 9)

		product = Product.objects.get(institution=institution, external_id='BCSE-00477-P01')
		op51 = Product.objects.get(institution=institution, external_id='BCSE-00487-P02')
		self.assertEqual(product.product_type, Product.ProductType.BOND)
		self.assertEqual(product.name, 'Айгенис Оп47')
		self.assertEqual(op51.name, 'Айгенис Оп51')
		self.assertEqual(str(product.units), '2.00000000')
		self.assertEqual(str(product.current_price), '525.70000000')

		transactions = Transaction.objects.filter(import_job=job).order_by('occurred_at', 'id')
		self.assertEqual(transactions.count(), 9)
		self.assertEqual(transactions[0].transaction_type, Transaction.TransactionType.DEPOSIT)
		self.assertEqual(str(transactions[0].amount), '100.00')
		self.assertEqual(transactions[1].transaction_type, Transaction.TransactionType.TRADE)
		self.assertEqual(str(transactions[1].amount), '-516.97')
		self.assertEqual(transactions[1].product, product)
		self.assertEqual(transactions[2].transaction_type, Transaction.TransactionType.FEE)
		self.assertEqual(str(transactions[2].amount), '-1.06')
		self.assertEqual(transactions[2].product, product)

		account = Account.objects.get(institution=institution, currency__code='BYN')
		self.assertEqual(str(account.current_balance), '-1853.22')

	def test_finstore_clipboard_income_does_not_close_existing_position(self):
		source = ImportSource.objects.get(code='finstore-history')
		purchase_workbook = BytesIO()
		pd.DataFrame(
			[
				['История операций', '', '', '', ''],
				['Вид операции', 'Название токена', 'Количество токенов', 'Сумма валюты', 'Дата'],
				['Пополнение кошелька', '', '', '100 BYN.sc', '46157.300000000000'],
				['Покупка токенов', 'SMART_(BYN_804)', '7', '70 BYN.sc', '46157.428969907407'],
			]
		).to_excel(purchase_workbook, index=False, header=False)
		purchase_workbook.seek(0)

		purchase_upload = SimpleUploadedFile(
			'Finstore_smart_seed.xlsx',
			purchase_workbook.getvalue(),
			content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
		)
		process_uploaded_import(source, purchase_upload)

		clipboard_text = 'Получение дохода\tSMART_(BYN_804)\t\t1.27 BYN.sc\t20.05.2026 03:01:41\t'
		job, created = process_clipboard_import(source, clipboard_text)

		self.assertTrue(created)
		product = Product.objects.get(institution=source.institution, external_id='SMART_(BYN_804)')
		self.assertEqual(str(product.units), '7.00000000')
		self.assertTrue(product.is_active)
		self.assertEqual(str(product.current_price), '10.00000000')
		self.assertGreater(product.current_value_usd, 0)
		self.assertEqual(Transaction.objects.filter(import_job=job, product=product).count(), 1)

# Create your tests here.
