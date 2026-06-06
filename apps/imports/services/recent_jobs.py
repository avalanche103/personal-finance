from __future__ import annotations

from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.imports.models import ImportJob

RECENT_JOBS_LIMIT = 20


def recent_import_jobs_queryset():
	return (
		ImportJob.objects.select_related('source', 'institution')
		.annotate(last_activity_at=Coalesce('finished_at', 'updated_at', 'created_at'))
		.order_by('-last_activity_at', '-pk')
	)


def recent_import_jobs(limit: int = RECENT_JOBS_LIMIT):
	return list(recent_import_jobs_queryset()[:limit])


def mark_import_jobs_recent(job_ids: list[int], *, note: str | None = None) -> None:
	if not job_ids:
		return

	now = timezone.now()
	jobs = list(ImportJob.objects.filter(pk__in=job_ids))
	for job in jobs:
		details = dict(job.details or {})
		details['last_manual_run_at'] = now.isoformat()
		if note:
			details['last_manual_run_note'] = note
		job.details = details
		job.finished_at = now
	ImportJob.objects.bulk_update(jobs, ['details', 'finished_at', 'updated_at'])


def record_manual_sync_job(
	*,
	source_code: str,
	parser_name: str,
	status: str,
	error_message: str = '',
	details: dict | None = None,
	rows_detected: int = 0,
	records_created: int = 0,
) -> ImportJob:
	from apps.imports.models import ImportSource

	source = ImportSource.objects.filter(code=source_code).first()
	if source is None:
		source = ImportSource.objects.create(
			name=source_code,
			code=source_code,
			source_type=ImportSource.SourceType.API,
			is_active=True,
		)

	now = timezone.now()
	return ImportJob.objects.create(
		source=source,
		idempotency_key=f'manual:{parser_name}:{now.isoformat()}',
		status=status,
		file_type='api',
		parser_name=parser_name,
		original_filename='Manual sync',
		started_at=now,
		finished_at=now,
		rows_detected=rows_detected,
		records_created=records_created,
		details={**(details or {}), 'trigger': 'manual'},
		error_message=error_message,
	)
