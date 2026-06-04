from apps.imports.models import ImportJob

TERMINAL_STATUSES = frozenset({ImportJob.Status.SAVED, ImportJob.Status.FAILED})

STATUS_PROGRESS = {
    ImportJob.Status.PENDING: (12, 'Queued'),
    ImportJob.Status.PARSING: (48, 'Parsing file'),
    ImportJob.Status.VALIDATED: (78, 'Validated'),
    ImportJob.Status.SAVED: (100, 'Complete'),
    ImportJob.Status.FAILED: (100, 'Failed'),
}


def job_progress(job: ImportJob) -> dict:
    percent, label = STATUS_PROGRESS.get(job.status, (0, job.get_status_display()))
    return {
        'percent': percent,
        'label': label,
        'status': job.status,
        'status_display': job.get_status_display(),
        'is_terminal': job.status in TERMINAL_STATUSES,
        'error_message': job.error_message,
        'rows_detected': job.rows_detected,
        'records_created': job.records_created,
    }
