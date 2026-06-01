import hashlib

from django.db import transaction
from django.utils import timezone

from apps.imports.models import ImportJob, RawImportFile
from apps.imports.services.parsers.base import ParseResult
from apps.imports.services.parsers.pdf import PDFImportParser
from apps.imports.services.parsers.xls import XLSImportParser
from apps.imports.services.storage import calculate_upload_checksum, persist_uploaded_file


PARSER_REGISTRY = {
    'xls': XLSImportParser(),
    'xlsx': XLSImportParser(),
    'pdf': PDFImportParser(),
}


def build_idempotency_key(source_id: int, checksum: str, filename: str) -> str:
    payload = f'{source_id}:{checksum}'.encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def process_uploaded_import(source, uploaded_file):
    checksum = calculate_upload_checksum(uploaded_file)
    existing_raw_file = RawImportFile.objects.select_related('job').filter(source=source, checksum=checksum).first()
    if existing_raw_file:
        return existing_raw_file.job, False

    idempotency_key = build_idempotency_key(source.pk, checksum, uploaded_file.name)
    file_type = uploaded_file.name.rsplit('.', 1)[-1].lower() if '.' in uploaded_file.name else ''

    with transaction.atomic():
        job, created = ImportJob.objects.get_or_create(
            source=source,
            idempotency_key=idempotency_key,
            defaults={
                'institution': source.institution,
                'status': ImportJob.Status.PENDING,
                'file_type': file_type,
                'original_filename': uploaded_file.name,
                'started_at': timezone.now(),
            },
        )

        if not created:
            return job, False

        storage_info = persist_uploaded_file(uploaded_file, checksum)
        raw_import_file = RawImportFile.objects.create(
            job=job,
            source=source,
            original_filename=uploaded_file.name,
            stored_path=storage_info['stored_path'],
            file_type=storage_info['file_type'],
            mime_type=storage_info['mime_type'],
            checksum=storage_info['checksum'],
            size_bytes=storage_info['size_bytes'],
            metadata={'skeleton': True},
        )

        parser = PARSER_REGISTRY.get(storage_info['file_type'])
        job.status = ImportJob.Status.PARSING
        job.parser_name = parser.parser_name if parser else ''
        job.save(update_fields=['status', 'parser_name', 'updated_at'])

        try:
            result = parser.parse(raw_import_file) if parser else ParseResult(warnings=['No parser registered for this file type.'])
            records_created = parser.persist(raw_import_file, result) if parser else 0
            job.status = ImportJob.Status.VALIDATED
            job.rows_detected = int(result.metadata.get('rows', len(result.records)))
            job.details = result.to_dict()
            job.records_created = records_created
            job.status = ImportJob.Status.SAVED
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'rows_detected', 'details', 'records_created', 'finished_at', 'updated_at'])
            return job, True
        except Exception as exc:
            job.status = ImportJob.Status.FAILED
            job.error_message = str(exc)
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'error_message', 'finished_at', 'updated_at'])
            raise