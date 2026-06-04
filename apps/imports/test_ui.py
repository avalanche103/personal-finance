from io import BytesIO

import pandas as pd
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.common.management.commands.bootstrap_local_data import Command as BootstrapCommand
from apps.imports.models import ImportJob, ImportSource
from apps.imports.services.details import get_editable_records
from apps.imports.services.pipeline import process_uploaded_import


class ImportUiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        BootstrapCommand().handle()

    def setUp(self):
        self.client = Client()

    def _create_finstore_job(self) -> ImportJob:
        source = ImportSource.objects.get(code='finstore-history')
        workbook = BytesIO()
        pd.DataFrame(
            [
                ['История операций', '', '', '', ''],
                ['Вид операции', 'Название токена', 'Количество токенов', 'Сумма валюты', 'Дата'],
                ['Пополнение кошелька', '', '', '20 USD.sc', '46157.300000000000'],
                ['Покупка токенов', 'YOWHEELS_(USD_864)', '1', '10 USD.sc', '46157.429363425923'],
            ]
        ).to_excel(workbook, index=False, header=False)
        workbook.seek(0)
        upload = SimpleUploadedFile(
            'Finstore_ui.xlsx',
            workbook.getvalue(),
            content_type='application/vnd.openxmlformats.officedocument/spreadsheetml.sheet',
        )
        job, _ = process_uploaded_import(source, upload)
        return job

    def test_upload_redirects_to_job_detail(self):
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

    def test_job_detail_and_progress_partial_render(self):
        job = self._create_finstore_job()
        detail = self.client.get(reverse('imports:detail', args=[job.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'Editable rows')
        self.assertContains(detail, 'job-progress-')

        progress = self.client.get(reverse('imports:progress', args=[job.pk]), HTTP_HX_REQUEST='true')
        self.assertEqual(progress.status_code, 200)
        self.assertContains(progress, 'Complete')

    def test_record_update_persists_correction(self):
        job = self._create_finstore_job()
        records = get_editable_records(job)
        self.assertTrue(records)

        response = self.client.post(
            reverse('imports:record_update', args=[job.pk, 0]),
            {'field': 'token_name', 'value': 'EDITED_(USD_999)'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'EDITED_(USD_999)')

        job.refresh_from_db()
        updated = get_editable_records(job)[0]
        self.assertEqual(updated['token_name'], 'EDITED_(USD_999)')
