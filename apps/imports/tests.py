from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.common.management.commands.bootstrap_local_data import Command as BootstrapCommand
from apps.common.services.alfabank_deposits import parse_alfabank_deposit_statement
from apps.common.services.belarusbank_deposits import parse_belarusbank_deposit_statement
from apps.common.services.bnb_deposits import parse_bnb_deposit_statement
from apps.common.services.priorlife_insurance import (
	apply_priorlife_manual_update,
	compute_priorlife_balances,
	parse_priorlife_contributions,
	spread_yield_by_contribution_months,
)
from apps.common.services.stravita_pension import (
	parse_stravita_contributions,
	parse_stravita_extract,
	spread_income_by_cumulative_contributions,
)
from apps.imports.models import ImportJob, ImportSource
from apps.imports.services.pipeline import process_clipboard_import, process_uploaded_import
from apps.institutions.models import FinancialInstitution
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
		self.assertEqual(str(products[0].units), '2.000000')
		self.assertEqual(str(products[0].current_price), '20.00000000')
		self.assertGreater(products[0].current_value_usd, 0)
		self.assertEqual(products[1].currency.code, 'USD')
		self.assertEqual(str(products[1].units), '1.000000')
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
		self.assertEqual(str(product.units), '0.000000')
		self.assertFalse(product.is_active)
		self.assertEqual(str(product.current_value_usd), '0.00')

		transactions = list(Transaction.objects.filter(import_job=job).order_by('occurred_at', 'id'))
		self.assertEqual(len(transactions), 3)
		self.assertIsNone(transactions[0].product)
		self.assertEqual(transactions[1].product, product)
		self.assertEqual(transactions[1].transaction_type, Transaction.TransactionType.TRADE)
		self.assertEqual(str(transactions[1].quantity), '2.000000')
		self.assertEqual(transactions[2].product, product)
		self.assertEqual(transactions[2].transaction_type, Transaction.TransactionType.INCOME)
		self.assertEqual(str(transactions[2].quantity), '-2.000000')

		usd_account = Account.objects.get(institution=source.institution, currency__code='USD')
		self.assertEqual(str(usd_account.current_balance), '110.00')

	def test_finstore_early_redemption_closes_token_position(self):
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
			'Finstore_early_redemption.xlsx',
			workbook.getvalue(),
			content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
		)

		job, created = process_uploaded_import(source, upload)
		self.assertTrue(created)
		self.assertEqual(job.status, ImportJob.Status.SAVED)

		product = Product.objects.get(institution=source.institution, external_id='BLESAVARIS_(BYN_442)')
		self.assertEqual(str(product.units), '0.000000')
		self.assertFalse(product.is_active)
		self.assertEqual(str(product.current_value_usd), '0.00')

		redemption = Transaction.objects.get(import_job=job, metadata__operation_type='Досрочное погашение токенов')
		self.assertEqual(redemption.product, product)
		self.assertEqual(redemption.transaction_type, Transaction.TransactionType.INCOME)
		self.assertEqual(str(redemption.amount), '50.00')
		self.assertEqual(str(redemption.quantity), '-1.000000')

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
		self.assertTrue(all(str(product.units) == '0.000000' for product in products))

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
		self.assertEqual(str(product.units), '2.000000')
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
		self.assertEqual(str(product.units), '7.000000')
		self.assertTrue(product.is_active)
		self.assertEqual(str(product.current_price), '10.00000000')
		self.assertGreater(product.current_value_usd, 0)
		self.assertEqual(Transaction.objects.filter(import_job=job, product=product).count(), 1)

class StravitaPensionImportTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def _pdf_path(self, filename: str) -> Path:
		path = Path(settings.BASE_DIR) / filename
		self.assertTrue(path.exists(), f'Missing fixture PDF: {path}')
		return path

	def test_parse_stravita_extract_pdf(self):
		result = parse_stravita_extract(self._pdf_path('policy_pension_extract.pdf'))
		self.assertEqual(result.metadata['parser_variant'], 'stravita-extract')
		self.assertEqual(result.metadata['account_number'], '3040282A000PB5')
		statement = result.artifacts['statement']
		self.assertEqual(statement['certificate_series'], 'EP')
		self.assertEqual(statement['certificate_number'], '0004390')
		self.assertEqual(statement['as_of_date'], '2026-05-01')
		self.assertEqual(statement['contributions_total_byn'], '4668.78')
		self.assertEqual(statement['accumulated_amount_byn'], '4815.42')
		self.assertEqual(statement['insurance_bonus_byn'], '46.03')
		self.assertEqual(statement['refinancing_yield_byn'], '100.61')

	def test_parse_stravita_contributions_pdf(self):
		result = parse_stravita_contributions(self._pdf_path('policy_pension_contributions.pdf'))
		self.assertEqual(result.metadata['parser_variant'], 'stravita-contributions')
		self.assertEqual(result.metadata['rows'], 52)
		self.assertEqual(result.metadata['account_number'], '3040282A000PB5')

	def test_spread_stravita_income_by_cumulative_contributions(self):
		contributions = {
			(2024, 7): Decimal('75.36'),
			(2024, 8): Decimal('150.72'),
		}
		rows = spread_income_by_cumulative_contributions(Decimal('30'), contributions)
		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0][0], date(2024, 7, 31))
		self.assertEqual(rows[1][0], date(2024, 8, 31))
		self.assertEqual(rows[0][1], Decimal('7.50'))
		self.assertEqual(rows[1][1], Decimal('22.50'))
		self.assertEqual(sum(amount for _, amount in rows), Decimal('30'))
		self.assertGreater(rows[1][1], rows[0][1])

	def test_import_stravita_pension_pipeline(self):
		institution = FinancialInstitution.objects.get(slug='stravita')
		extract_source = ImportSource.objects.get(code='stravita-extract')
		contributions_source = ImportSource.objects.get(code='stravita-contributions')

		extract_upload = SimpleUploadedFile(
			'policy_pension_extract.pdf',
			self._pdf_path('policy_pension_extract.pdf').read_bytes(),
			content_type='application/pdf',
		)
		contributions_upload = SimpleUploadedFile(
			'policy_pension_contributions.pdf',
			self._pdf_path('policy_pension_contributions.pdf').read_bytes(),
			content_type='application/pdf',
		)

		extract_job, extract_created = process_uploaded_import(extract_source, extract_upload)
		contributions_job, contributions_created = process_uploaded_import(contributions_source, contributions_upload)

		self.assertTrue(extract_created)
		self.assertTrue(contributions_created)
		self.assertEqual(extract_job.status, ImportJob.Status.SAVED)
		self.assertEqual(contributions_job.status, ImportJob.Status.SAVED)
		self.assertEqual(extract_job.details['metadata']['parser_variant'], 'stravita-extract')
		self.assertEqual(contributions_job.details['metadata']['parser_variant'], 'stravita-contributions')

		product = Product.objects.get(institution=institution, external_id='3040282A000PB5')
		self.assertEqual(product.product_type, Product.ProductType.PENSION)
		self.assertEqual(str(product.current_price), '4815.42000000')
		self.assertEqual(product.metadata['program'], 'dnps_state')
		self.assertEqual(product.metadata['management_expense_pct'], '5.7')

		contributions = Transaction.objects.filter(product=product, transaction_type=Transaction.TransactionType.DEPOSIT)
		self.assertEqual(contributions.count(), 52)
		income = Transaction.objects.filter(product=product, transaction_type=Transaction.TransactionType.INCOME)
		months_with_contributions = {
			tx.occurred_at.date().replace(day=1)
			for tx in contributions.filter(occurred_at__date__lte='2026-05-01')
		}
		self.assertEqual(income.count(), len(months_with_contributions) * 2)
		self.assertEqual(
			sum(tx.amount for tx in income.filter(metadata__income_kind='insurance_bonus')),
			Decimal('46.03'),
		)
		self.assertEqual(
			sum(tx.amount for tx in income.filter(metadata__income_kind='refinancing_yield')),
			Decimal('100.61'),
		)
		self.assertFalse(income.filter(occurred_at__date=date(2026, 5, 1)).exists())
		self.assertTrue(all(tx.metadata.get('spread_accrual') for tx in income))

		payroll_account = Account.objects.get(institution__slug='income-sources', name='Зарплата')
		self.assertEqual(contributions.filter(account=payroll_account).count(), 52)

		snapshot = BalanceSnapshot.objects.get(product=product)
		self.assertEqual(snapshot.balance, Decimal('4815.42'))

		july_contribution = contributions.filter(occurred_at__date='2024-07-04', amount=Decimal('75.36')).first()
		self.assertIsNotNone(july_contribution)
		self.assertEqual(str(july_contribution.metadata['employee_share_byn']), '37.68')
		self.assertEqual(str(july_contribution.metadata['employer_share_byn']), '37.68')

		contributions_through_may = contributions.filter(occurred_at__date__lte='2026-05-01')
		total_through_may = sum((tx.amount for tx in contributions_through_may), Decimal('0'))
		self.assertEqual(str(total_through_may), '4668.78')


class PriorlifeInsuranceImportTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def _pdf_path(self, filename: str) -> Path:
		path = Path(settings.BASE_DIR) / filename
		self.assertTrue(path.exists(), f'Missing fixture PDF: {path}')
		return path

	def test_parse_priorlife_contributions_pdf(self):
		result = parse_priorlife_contributions(self._pdf_path('Priorlife_1.pdf'))
		self.assertEqual(result.metadata['parser_variant'], 'priorlife-contributions')
		self.assertEqual(result.metadata['account_number'], '210004070')
		self.assertEqual(result.metadata['rows'], 119)
		statement = result.artifacts['statement']
		self.assertEqual(statement['as_of_date'], '2026-06-05')
		self.assertEqual(statement['paid_contributions_total'], '2975.00')
		self.assertEqual(statement['total_contract_premium'], '4500.00')
		self.assertEqual(statement['future_payments_total'], '1525.00')

	def test_parse_priorlife_contributions_pdf_contract_210004069(self):
		result = parse_priorlife_contributions(self._pdf_path('Priorlife_2.pdf'))
		statement = result.artifacts['statement']
		self.assertEqual(result.metadata['account_number'], '210004069')
		self.assertEqual(statement['contract_end'], '27.07.2029')
		self.assertEqual(result.metadata['rows'], 119)

	def test_compute_priorlife_balances_with_contract_load(self):
		balances = compute_priorlife_balances(
			gross_paid=Decimal('2975'),
			load_pct=Decimal('8'),
			accumulated_amount=Decimal('3684.14'),
			accrued_yield_reported=Decimal('1867.14'),
		)
		self.assertEqual(balances['paid_contributions_gross'], '2975')
		self.assertEqual(balances['net_contributions_total'], '2737.00')
		self.assertEqual(balances['contract_load_deducted_total'], '238.00')
		self.assertEqual(balances['accumulated_amount'], '3684.14')
		self.assertEqual(balances['accrued_yield_in_account'], '947.14')
		self.assertEqual(balances['accrued_yield_reported'], '1867.14')

	def test_spread_yield_by_contribution_months(self):
		contributions = [
			{'payment_date': '2016-07-27', 'amount': '25'},
			{'payment_date': '2016-08-14', 'amount': '25'},
			{'payment_date': '2016-08-20', 'amount': '25'},
		]
		rows = spread_yield_by_contribution_months(
			Decimal('30'),
			contributions,
			load_pct=Decimal('8'),
		)
		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0][0], date(2016, 7, 20))
		self.assertEqual(rows[1][0], date(2016, 8, 20))
		self.assertEqual(rows[0][1], Decimal('7.50'))
		self.assertEqual(rows[1][1], Decimal('22.50'))
		self.assertEqual(sum(amount for _, amount in rows), Decimal('30'))
		self.assertGreater(rows[1][1], rows[0][1])

	def test_import_priorlife_pipeline(self):
		institution = FinancialInstitution.objects.get(slug='priorlife')
		source = ImportSource.objects.get(code='priorlife-contributions')
		source.config = {
			'parser': 'priorlife-contributions',
			'contract_date': '27.07.2016',
			'contract_load_pct': '8',
			'guaranteed_yield_pct': '6',
			'accumulated_amount': '3684.14',
			'accrued_yield': '1867.14',
			'additional_accrued_yield': '0',
			'premium_amount': '25',
			'premium_schedule': 'monthly',
			'insurance_type': 'life',
		}
		source.save(update_fields=['config', 'updated_at'])
		upload = SimpleUploadedFile(
			'Priorlife_1.pdf',
			self._pdf_path('Priorlife_1.pdf').read_bytes(),
			content_type='application/pdf',
		)
		job, created = process_uploaded_import(source, upload)
		self.assertTrue(created)
		self.assertEqual(job.status, ImportJob.Status.SAVED)
		self.assertEqual(job.details['metadata']['parser_variant'], 'priorlife-contributions')

		product = Product.objects.get(institution=institution, external_id='210004070')
		self.assertEqual(product.product_type, Product.ProductType.LIFE_INSURANCE)
		self.assertEqual(str(product.current_price), '3684.14000000')
		self.assertEqual(product.metadata['guaranteed_yield_pct'], '6')
		self.assertEqual(product.metadata['accrued_yield_reported'], '1867.14')
		self.assertEqual(product.metadata['accrued_yield_in_account'], '947.14')
		self.assertEqual(product.metadata['net_contributions_total'], '2737.00')

		contributions = Transaction.objects.filter(product=product, transaction_type=Transaction.TransactionType.DEPOSIT)
		self.assertEqual(contributions.count(), 119)
		income = Transaction.objects.filter(product=product, transaction_type=Transaction.TransactionType.INCOME)
		self.assertEqual(income.count(), 98)
		self.assertEqual(sum(tx.amount for tx in income), Decimal('947.14'))
		self.assertFalse(income.filter(occurred_at__date=date(2026, 6, 5)).exists())
		last_income = income.order_by('-occurred_at').first()
		self.assertEqual(last_income.occurred_at.date(), date(2026, 5, 20))
		self.assertTrue(last_income.metadata.get('spread_accrual'))

		premium_account = Account.objects.get(institution__slug='income-sources', name='Страховые взносы')
		self.assertEqual(contributions.filter(account=premium_account).count(), 119)
		first_deposit = contributions.order_by('occurred_at').first()
		self.assertEqual(str(first_deposit.metadata['net_amount']), '23.00')
		self.assertEqual(str(first_deposit.metadata['load_amount']), '2.00')

		snapshot = BalanceSnapshot.objects.get(product=product)
		self.assertEqual(snapshot.balance, Decimal('3684.14'))

	def test_apply_priorlife_manual_update(self):
		institution = FinancialInstitution.objects.get(slug='priorlife')
		source = ImportSource.objects.get(code='priorlife-contributions')
		source.config = {
			'parser': 'priorlife-contributions',
			'contract_load_pct': '8',
			'accumulated_amount': '3684.14',
			'accrued_yield': '1867.14',
		}
		source.save(update_fields=['config', 'updated_at'])
		upload = SimpleUploadedFile(
			'Priorlife_1.pdf',
			self._pdf_path('Priorlife_1.pdf').read_bytes(),
			content_type='application/pdf',
		)
		process_uploaded_import(source, upload)
		product = Product.objects.get(institution=institution, external_id='210004070')

		result = apply_priorlife_manual_update(
			account_number='210004070',
			payment_date=date(2026, 6, 10),
			premium_amount=Decimal('25'),
			accumulated_amount=Decimal('3725'),
		)
		self.assertTrue(result['deposit_created'])

		product.refresh_from_db()
		self.assertEqual(str(product.current_price), '3725.00000000')
		self.assertEqual(product.metadata['accumulated_amount'], '3725')
		self.assertEqual(product.metadata['paid_contributions_total'], '3000.00')
		self.assertEqual(product.metadata['accrued_yield_in_account'], '965.00')

		deposits = Transaction.objects.filter(product=product, transaction_type=Transaction.TransactionType.DEPOSIT)
		self.assertEqual(deposits.count(), 120)
		june_deposit = deposits.filter(metadata__payment_date='2026-06-10').get()
		self.assertEqual(june_deposit.amount, Decimal('25'))

		income = Transaction.objects.filter(product=product, transaction_type=Transaction.TransactionType.INCOME)
		self.assertEqual(income.count(), 99)
		self.assertEqual(sum(tx.amount for tx in income), Decimal('965.00'))
		self.assertTrue(income.filter(occurred_at__date=date(2026, 6, 20)).exists())


class BnbDepositImportTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def _pdf_path(self, filename: str) -> Path:
		path = Path(settings.BASE_DIR) / filename
		self.assertTrue(path.exists(), f'Missing fixture PDF: {path}')
		return path

	def test_parse_bnb1_deposit_statement_pdf(self):
		result = parse_bnb_deposit_statement(self._pdf_path('BNB1.pdf'))
		self.assertEqual(result.metadata['parser_variant'], 'bnb-deposit-statement')
		self.assertEqual(result.metadata['contract_number'], '1112109330009211')
		statement = result.artifacts['statement']
		self.assertEqual(statement['as_of_date'], '2026-06-06')
		self.assertEqual(statement['initial_amount_byn'], '1671.50')
		self.assertEqual(statement['balance_byn'], '1877.00')
		self.assertEqual(statement['annual_rate_pct'], '15.64')
		self.assertEqual(statement['maturity_date'], '2027-01-03')
		self.assertEqual(result.metadata['rows'], 13)

	def test_parse_bnb2_deposit_statement_pdf(self):
		result = parse_bnb_deposit_statement(self._pdf_path('BNB2.pdf'))
		statement = result.artifacts['statement']
		self.assertEqual(result.metadata['contract_number'], '1112449330000404')
		self.assertEqual(statement['balance_byn'], '1115.04')
		self.assertEqual(statement['annual_rate_pct'], '14.91')
		self.assertEqual(result.metadata['rows'], 1)

	def test_import_bnb_deposit_pipeline(self):
		institution = FinancialInstitution.objects.get(slug='bnb-bank')
		source = ImportSource.objects.get(code='bnb-deposit-statement')
		bank_account = Account.objects.get(institution=institution, name='БНБ-Банк BYN Account')

		for filename, product_name, contract_number, balance in [
			('BNB1.pdf', 'BNB1', '1112109330009211', Decimal('1877.00')),
			('BNB2.pdf', 'BNB2', '1112449330000404', Decimal('1115.04')),
		]:
			upload = SimpleUploadedFile(
				filename,
				self._pdf_path(filename).read_bytes(),
				content_type='application/pdf',
			)
			job, created = process_uploaded_import(source, upload)
			self.assertTrue(created)
			self.assertEqual(job.status, ImportJob.Status.SAVED)
			self.assertEqual(job.details['metadata']['parser_variant'], 'bnb-deposit-statement')

			product = Product.objects.get(institution=institution, external_id=contract_number)
			self.assertEqual(product.name, product_name)
			self.assertEqual(product.product_type, Product.ProductType.DEPOSIT)
			self.assertEqual(product.income_account, bank_account)
			self.assertEqual(product.units, balance)
			self.assertEqual(product.metadata['interest_mode'], 'capitalized')
			self.assertEqual(str(product.current_price), '1.00000000')

		bnb1 = Product.objects.get(external_id='1112109330009211')
		self.assertEqual(
			Transaction.objects.filter(
				product=bnb1,
				transaction_type=Transaction.TransactionType.DEPOSIT,
			).count(),
			1,
		)
		capitalized = Transaction.objects.filter(
			product=bnb1,
			transaction_type=Transaction.TransactionType.INCOME,
			metadata__interest_mode='capitalized',
		)
		self.assertEqual(capitalized.count(), 12)
		self.assertEqual(sum(tx.amount for tx in capitalized), Decimal('205.50'))

		bnb2 = Product.objects.get(external_id='1112449330000404')
		self.assertEqual(
			Transaction.objects.filter(product=bnb2).count(),
			1,
		)
		self.assertEqual(BalanceSnapshot.objects.filter(product=bnb1).count(), 1)
		self.assertEqual(BalanceSnapshot.objects.filter(product=bnb2).count(), 1)


class BelarusbankDepositImportTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def _pdf_path(self, filename: str) -> Path:
		path = Path(settings.BASE_DIR) / filename
		self.assertTrue(path.exists(), f'Missing fixture PDF: {path}')
		return path

	def test_parse_belarusbank_deposit_statement_pdf(self):
		result = parse_belarusbank_deposit_statement(self._pdf_path('Belarusbank.pdf'))
		self.assertEqual(result.metadata['parser_variant'], 'belarusbank-deposit-statement')
		self.assertEqual(result.metadata['iban'], 'BY74AKBB34140038751750070000')
		statement = result.artifacts['statement']
		self.assertEqual(statement['as_of_date'], '2026-06-06')
		self.assertEqual(statement['initial_amount_byn'], '1868.71')
		self.assertEqual(statement['balance_byn'], '2045.48')
		self.assertEqual(statement['maturity_date'], '2029-04-05')
		self.assertEqual(result.metadata['rows'], 7)
		self.assertIn('Правильный выбор онлайн', statement['deposit_name'])

	def test_import_belarusbank_deposit_pipeline(self):
		institution = FinancialInstitution.objects.get(slug='belarusbank')
		source = ImportSource.objects.get(code='belarusbank-deposit-statement')
		bank_account = Account.objects.get(institution=institution, name='Беларусбанк BYN Account')

		upload = SimpleUploadedFile(
			'Belarusbank.pdf',
			self._pdf_path('Belarusbank.pdf').read_bytes(),
			content_type='application/pdf',
		)
		job, created = process_uploaded_import(source, upload)
		self.assertTrue(created)
		self.assertEqual(job.status, ImportJob.Status.SAVED)
		self.assertEqual(job.details['metadata']['parser_variant'], 'belarusbank-deposit-statement')

		product = Product.objects.get(
			institution=institution,
			external_id='BY74AKBB34140038751750070000',
		)
		self.assertEqual(product.name, 'Belarusbank')
		self.assertEqual(product.product_type, Product.ProductType.DEPOSIT)
		self.assertEqual(product.income_account, bank_account)
		self.assertEqual(product.units, Decimal('2045.48'))
		self.assertEqual(product.metadata['interest_mode'], 'capitalized')
		self.assertGreater(product.annual_rate_pct, Decimal('14'))

		deposits = Transaction.objects.filter(
			product=product,
			transaction_type=Transaction.TransactionType.DEPOSIT,
		)
		self.assertEqual(deposits.count(), 4)
		self.assertEqual(sum(tx.amount for tx in deposits), Decimal('1971.94'))

		capitalized = Transaction.objects.filter(
			product=product,
			transaction_type=Transaction.TransactionType.INCOME,
			metadata__interest_mode='capitalized',
		)
		self.assertEqual(capitalized.count(), 3)
		self.assertEqual(sum(tx.amount for tx in capitalized), Decimal('73.54'))


class AlfabankDepositImportTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def _pdf_path(self, filename: str) -> Path:
		path = Path(settings.BASE_DIR) / filename
		self.assertTrue(path.exists(), f'Missing fixture PDF: {path}')
		return path

	def test_parse_alfa1_deposit_statement_pdf(self):
		result = parse_alfabank_deposit_statement(self._pdf_path('ALFA1.pdf'))
		self.assertEqual(result.metadata['parser_variant'], 'alfabank-deposit-statement')
		statement = result.artifacts['statement']
		self.assertEqual(result.metadata['contract_number'], 'BY95ALFA341430LV871050270000')
		self.assertEqual(statement['balance_byn'], '1086.02')
		self.assertEqual(statement['initial_amount_byn'], '476.25')
		self.assertEqual(statement['annual_rate_pct'], '16.0000000')
		self.assertEqual(result.metadata['rows'], 26)

	def test_import_alfabank_deposit_pipeline(self):
		from apps.products.operations_calendar import build_operations_calendar

		institution = FinancialInstitution.objects.get(slug='alfabank')
		source = ImportSource.objects.get(code='alfabank-deposit-statement')
		bank_account = Account.objects.get(institution=institution, name='АльфаБанк BYN Account')
		before_balance = bank_account.current_balance

		for filename, product_name, contract_number, balance, next_income in [
			('ALFA1.pdf', 'ALFA1', 'BY95ALFA341430LV871050270000', Decimal('1086.02'), date(2026, 6, 10)),
			('ALFA2.pdf', 'ALFA2', 'BY13ALFA341430LV871040270000', Decimal('616.75'), date(2026, 6, 10)),
			('ALFA3.pdf', 'ALFA3', 'BY28ALFA341430LV871030270000', Decimal('530.95'), date(2026, 6, 11)),
		]:
			upload = SimpleUploadedFile(
				filename,
				self._pdf_path(filename).read_bytes(),
				content_type='application/pdf',
			)
			job, created = process_uploaded_import(source, upload)
			self.assertTrue(created)
			self.assertEqual(job.status, ImportJob.Status.SAVED)

			product = Product.objects.get(institution=institution, external_id=contract_number)
			self.assertEqual(product.name, product_name)
			self.assertEqual(product.units, balance)
			self.assertEqual(product.metadata['interest_mode'], 'payout')
			self.assertEqual(product.income_schedule, Product.IncomeSchedule.TWICE_MONTHLY)
			self.assertEqual(product.next_income_date, next_income)

		bank_account.refresh_from_db()
		self.assertEqual(bank_account.current_balance, before_balance)

		alfa1 = Product.objects.get(external_id='BY95ALFA341430LV871050270000')
		self.assertEqual(
			Transaction.objects.filter(product=alfa1, transaction_type=Transaction.TransactionType.DEPOSIT).count(),
			27,
		)
		self.assertFalse(
			Transaction.objects.filter(product=alfa1, transaction_type=Transaction.TransactionType.INCOME).exists()
		)

		calendar = build_operations_calendar(
			list(Product.objects.filter(external_id__startswith='BY').order_by('name')),
			today=date(2026, 6, 6),
			future_days=60,
		)
		forecast_dates = [day['date'] for day in calendar]
		self.assertIn(date(2026, 6, 10), forecast_dates)
		self.assertIn(date(2026, 6, 25), forecast_dates)
		self.assertIn(date(2026, 6, 11), forecast_dates)
		self.assertIn(date(2026, 6, 26), forecast_dates)


class ImportManualSyncTests(TestCase):
	@classmethod
	def setUpTestData(cls):
		BootstrapCommand().handle()

	def setUp(self):
		self.client = Client()

	@patch('apps.imports.views.sync_nbrb_rates_manual')
	def test_manual_nbrb_sync_redirects_with_success_message(self, sync_nbrb):
		from apps.imports.services.manual_sync import ManualSyncResult

		source = ImportSource.objects.get(code='nbrb-exrates-api')
		job = ImportJob.objects.create(
			source=source,
			idempotency_key='nbrb-rates:highlight',
			status=ImportJob.Status.SAVED,
			file_type='api',
			parser_name='nbrb-exrates-api',
			rows_detected=21,
			records_created=21,
		)
		sync_nbrb.return_value = ManualSyncResult(
			True,
			f'NBRB sync completed. Job #{job.pk}, 3 new rows, 21 stored in range.',
			job_ids=[job.pk],
		)

		response = self.client.post(reverse('imports:sync_nbrb'))

		self.assertEqual(response.status_code, 302)
		self.assertEqual(response['Location'], reverse('imports:upload'))
		sync_nbrb.assert_called_once()

		follow_up = self.client.get(reverse('imports:upload'))
		self.assertContains(follow_up, 'NBRB sync completed')
		self.assertContains(follow_up, 'is-recent-sync')

	@patch('apps.imports.views.sync_binance_manual')
	def test_manual_binance_sync_redirects_with_success_message(self, sync_binance):
		from apps.imports.services.manual_sync import ManualSyncResult

		source = ImportSource.objects.get(code='binance-api')
		spot_job = ImportJob.objects.create(
			source=source,
			idempotency_key='binance:spot:highlight',
			status=ImportJob.Status.SAVED,
			file_type='api',
			parser_name='binance-spot-balances',
		)
		earn_job = ImportJob.objects.create(
			source=source,
			idempotency_key='binance:earn:highlight',
			status=ImportJob.Status.SAVED,
			file_type='api',
			parser_name='binance-earn-funding',
		)
		sync_binance.return_value = ManualSyncResult(
			True,
			f'Binance sync completed. Spot job #{spot_job.pk}, Earn job #{earn_job.pk}.',
			job_ids=[spot_job.pk, earn_job.pk],
		)

		response = self.client.post(reverse('imports:sync_binance'))

		self.assertEqual(response.status_code, 302)
		self.assertEqual(response['Location'], reverse('imports:upload'))
		sync_binance.assert_called_once()

		follow_up = self.client.get(reverse('imports:upload'))
		self.assertContains(follow_up, 'Binance sync completed')
		self.assertContains(follow_up, 'is-recent-sync')

	@patch('apps.imports.views.sync_binance_manual')
	def test_manual_binance_sync_without_credentials_shows_warning(self, sync_binance):
		from apps.imports.services.manual_sync import ManualSyncResult

		source = ImportSource.objects.get(code='binance-api')
		job = ImportJob.objects.create(
			source=source,
			idempotency_key='binance:manual:skipped',
			status=ImportJob.Status.FAILED,
			file_type='api',
			parser_name='binance-manual-sync',
			original_filename='Manual sync',
			error_message='BINANCE_API_KEY and BINANCE_API_SECRET are not configured.',
		)
		sync_binance.return_value = ManualSyncResult(
			False,
			'BINANCE_API_KEY and BINANCE_API_SECRET are not configured.',
			job_ids=[job.pk],
			details={'skipped': True},
		)

		response = self.client.post(reverse('imports:sync_binance'))
		self.assertEqual(response.status_code, 302)

		follow_up = self.client.get(reverse('imports:upload'))
		self.assertContains(follow_up, 'BINANCE_API_KEY')
		self.assertContains(follow_up, 'is-recent-sync')

	@patch('apps.imports.views.sync_priorlife_manual')
	def test_manual_priorlife_update_redirects_with_success_message(self, sync_priorlife):
		from apps.common.services.priorlife_insurance import ensure_priorlife_bootstrap
		from apps.imports.services.manual_sync import ManualSyncResult

		bootstrap = ensure_priorlife_bootstrap()
		for account_number in ('210004069', '210004070'):
			Product.objects.get_or_create(
				institution=bootstrap['priorlife'],
				external_id=account_number,
				defaults={
					'name': f'Приорлайф №{account_number}',
					'product_type': Product.ProductType.LIFE_INSURANCE,
					'currency': bootstrap['usd'],
					'metadata': {'premium_amount': '25'},
				},
			)

		sync_priorlife.return_value = ManualSyncResult(
			True,
			'Приорлайф обновлён: 210004069, 210004070. Job #42.',
			job_ids=[42],
		)

		response = self.client.post(
			reverse('imports:priorlife_update'),
			data={
				'210004069_payment_date': '2026-06-10',
				'210004069_accumulated_amount': '3725',
				'210004069_premium_amount': '25',
				'210004070_payment_date': '2026-06-10',
				'210004070_accumulated_amount': '3725',
				'210004070_premium_amount': '25',
			},
		)

		self.assertEqual(response.status_code, 302)
		self.assertEqual(response['Location'], reverse('imports:upload'))
		sync_priorlife.assert_called_once()

		follow_up = self.client.get(reverse('imports:upload'))
		self.assertContains(follow_up, 'Приорлайф обновлён')
		self.assertContains(follow_up, 'Ежемесячное обновление')

	@patch('apps.imports.services.manual_sync.recalculate_usd_valuations')
	@patch('apps.imports.services.manual_sync.sync_daily_account_snapshots')
	@patch('apps.imports.services.manual_sync.sync_earn_and_funding')
	@patch('apps.imports.services.manual_sync.sync_spot_balances')
	def test_manual_binance_sync_creates_summary_and_recalculate_jobs(self, sync_spot, sync_earn, sync_daily, recalc_usd):
		from apps.accounts.services.binance import BinanceSyncResult
		from apps.imports.services.manual_sync import sync_binance_manual

		source = ImportSource.objects.get(code='binance-api')
		spot_job = ImportJob.objects.create(
			source=source,
			idempotency_key='binance:spot:multi',
			status=ImportJob.Status.SAVED,
			file_type='api',
			parser_name='binance-spot-balances',
			rows_detected=5,
		)
		earn_job = ImportJob.objects.create(
			source=source,
			idempotency_key='binance:earn:multi',
			status=ImportJob.Status.SAVED,
			file_type='api',
			parser_name='binance-earn-funding',
			rows_detected=3,
		)
		sync_spot.return_value = BinanceSyncResult(scope='spot-balances', job_id=spot_job.pk, rows_detected=5, records_updated=5)
		sync_earn.return_value = BinanceSyncResult(scope='earn-funding', job_id=earn_job.pk, rows_detected=3, records_updated=2)
		daily_job = ImportJob.objects.create(
			source=source,
			idempotency_key='binance:daily:multi',
			status=ImportJob.Status.SAVED,
			file_type='api',
			parser_name='binance-daily-snapshots',
			rows_detected=30,
		)
		sync_daily.return_value = BinanceSyncResult(scope='daily-snapshots', job_id=daily_job.pk, rows_detected=30, records_created=120)
		recalc_usd.return_value = {'accounts': 2, 'transactions': 4, 'balance_snapshots': 1, 'products': 3}

		before = ImportJob.objects.count()
		result = sync_binance_manual()

		self.assertTrue(result.success)
		self.assertEqual(ImportJob.objects.count(), before + 2)
		self.assertEqual(len(result.job_ids), 5)
		parsers = set(
			ImportJob.objects.filter(pk__in=result.job_ids).values_list('parser_name', flat=True)
		)
		self.assertEqual(
			parsers,
			{'binance-manual-sync', 'binance-spot-balances', 'binance-earn-funding', 'binance-daily-snapshots', 'recalculate-usd-values'},
		)

	@patch('apps.imports.services.manual_sync.recalculate_usd_valuations')
	@patch('apps.imports.services.manual_sync.sync_daily_account_snapshots')
	@patch('apps.imports.services.manual_sync.sync_earn_and_funding')
	@patch('apps.imports.services.manual_sync.sync_spot_balances')
	def test_manual_binance_sync_continues_when_daily_step_fails(self, sync_spot, sync_earn, sync_daily, recalc_usd):
		from apps.accounts.services.binance import BinanceSyncResult
		from apps.imports.services.manual_sync import sync_binance_manual

		source = ImportSource.objects.get(code='binance-api')
		spot_job = ImportJob.objects.create(
			source=source,
			idempotency_key='binance:spot:partial',
			status=ImportJob.Status.SAVED,
			file_type='api',
			parser_name='binance-spot-balances',
			rows_detected=5,
		)
		earn_job = ImportJob.objects.create(
			source=source,
			idempotency_key='binance:earn:partial',
			status=ImportJob.Status.SAVED,
			file_type='api',
			parser_name='binance-earn-funding',
			rows_detected=3,
		)
		sync_spot.return_value = BinanceSyncResult(scope='spot-balances', job_id=spot_job.pk, rows_detected=5, records_updated=5)
		sync_earn.return_value = BinanceSyncResult(scope='earn-funding', job_id=earn_job.pk, rows_detected=3, records_updated=2)
		sync_daily.side_effect = RuntimeError('Binance API error -1121: Invalid symbol.')
		recalc_usd.return_value = {'accounts': 2, 'transactions': 0, 'balance_snapshots': 0, 'products': 3}

		result = sync_binance_manual()

		self.assertTrue(result.success)
		self.assertTrue(result.details.get('partial'))
		self.assertIn('partially completed', result.message)
		self.assertIn('daily', result.details['step_failures'])
		self.assertIn(spot_job.pk, result.job_ids)
		self.assertIn(earn_job.pk, result.job_ids)

	@patch('apps.imports.services.manual_sync.sync_nbrb_rate_history')
	def test_manual_nbrb_sync_creates_summary_job_in_recent_jobs(self, sync_history):
		from apps.imports.services.recent_jobs import recent_import_jobs

		source = ImportSource.objects.get(code='nbrb-exrates-api')
		job = ImportJob.objects.create(
			source=source,
			idempotency_key='nbrb-rates:test',
			status=ImportJob.Status.SAVED,
			file_type='api',
			parser_name='nbrb-exrates-api',
			rows_detected=21,
			records_created=21,
		)
		sync_history.return_value = {
			'job_id': job.pk,
			'records_created': 0,
			'stored_total': 21,
			'rows_detected': 21,
		}

		response = self.client.post(reverse('imports:sync_nbrb'))
		self.assertEqual(response.status_code, 302)
		recent = recent_import_jobs()
		self.assertEqual(recent[0].parser_name, 'nbrb-manual-sync')
		self.assertIn(job.pk, [item.pk for item in recent[:3]])

	def test_upload_page_shows_manual_sync_buttons(self):
		response = self.client.get(reverse('imports:upload'))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Sync NBRB rates')
		self.assertContains(response, 'Sync Binance')
