from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.accounts.services.binance import sync_earn_and_funding, sync_spot_balances
from apps.common.services.exchange_rates import recalculate_usd_valuations, sync_nbrb_rate_history
from apps.imports.models import ImportJob
from apps.imports.services.recent_jobs import mark_import_jobs_recent, record_manual_sync_job

logger = logging.getLogger(__name__)


@dataclass
class ManualSyncResult:
	success: bool
	message: str
	job_ids: list[int] = field(default_factory=list)
	details: dict[str, Any] = field(default_factory=dict)


def _usd_rows_updated(usd: dict[str, int]) -> int:
	return sum(usd.values())


def sync_nbrb_rates_manual() -> ManualSyncResult:
	start_date = timezone.localdate() - timedelta(days=7)
	try:
		result = sync_nbrb_rate_history(start_date=start_date)
	except Exception as exc:
		logger.exception('Manual NBRB sync failed')
		job = record_manual_sync_job(
			source_code='nbrb-exrates-api',
			parser_name='nbrb-manual-sync',
			status=ImportJob.Status.FAILED,
			error_message=str(exc),
			details={'start_date': start_date.isoformat()},
		)
		return ManualSyncResult(False, f'NBRB sync failed: {exc}', job_ids=[job.pk])

	job_id = result['job_id']
	summary = record_manual_sync_job(
		source_code='nbrb-exrates-api',
		parser_name='nbrb-manual-sync',
		status=ImportJob.Status.SAVED,
		rows_detected=result.get('rows_detected', 0),
		records_created=result.get('records_created', 0),
		details={
			'nbrb_job_id': job_id,
			'start_date': start_date.isoformat(),
			'stored_total': result.get('stored_total', 0),
		},
	)
	job_ids = [summary.pk, job_id]
	mark_import_jobs_recent(job_ids, note='Manual NBRB sync')

	if result.get('records_created', 0) == 0:
		message = f'NBRB rates are up to date. Summary job #{summary.pk}, data job #{job_id}.'
	else:
		message = (
			f'NBRB sync completed. Summary job #{summary.pk}, data job #{job_id}, '
			f'{result["records_created"]} new rows, {result["stored_total"]} stored in range.'
		)
	return ManualSyncResult(True, message, job_ids=job_ids, details=result)


def sync_binance_manual() -> ManualSyncResult:
	if not settings.BINANCE_API_KEY or not settings.BINANCE_API_SECRET:
		job = record_manual_sync_job(
			source_code='binance-api',
			parser_name='binance-manual-sync',
			status=ImportJob.Status.FAILED,
			error_message='BINANCE_API_KEY and BINANCE_API_SECRET are not configured.',
			details={'skipped': True},
		)
		return ManualSyncResult(
			False,
			'BINANCE_API_KEY and BINANCE_API_SECRET are not configured.',
			job_ids=[job.pk],
			details={'skipped': True},
		)

	try:
		spot = sync_spot_balances(create_snapshots=True)
		earn = sync_earn_and_funding()
		usd = recalculate_usd_valuations()
	except Exception as exc:
		logger.exception('Manual Binance sync failed')
		job = record_manual_sync_job(
			source_code='binance-api',
			parser_name='binance-manual-sync',
			status=ImportJob.Status.FAILED,
			error_message=str(exc),
		)
		return ManualSyncResult(False, f'Binance sync failed: {exc}', job_ids=[job.pk])

	recalc_job = record_manual_sync_job(
		source_code='binance-api',
		parser_name='recalculate-usd-values',
		status=ImportJob.Status.SAVED,
		rows_detected=_usd_rows_updated(usd),
		records_created=_usd_rows_updated(usd),
		details={'trigger': 'manual-binance-sync', 'usd': usd},
	)
	summary = record_manual_sync_job(
		source_code='binance-api',
		parser_name='binance-manual-sync',
		status=ImportJob.Status.SAVED,
		rows_detected=spot.rows_detected + earn.rows_detected,
		records_created=spot.records_created + spot.records_updated + earn.records_updated,
		details={
			'spot_job_id': spot.job_id,
			'earn_job_id': earn.job_id,
			'recalculate_job_id': recalc_job.pk,
			'usd': usd,
		},
	)
	job_ids = [
		job_id
		for job_id in (summary.pk, spot.job_id, earn.job_id, recalc_job.pk)
		if job_id
	]
	mark_import_jobs_recent(job_ids, note='Manual Binance sync')

	message = (
		f'Binance sync completed. Summary job #{summary.pk}, '
		f'Spot #{spot.job_id or "-"}, Earn #{earn.job_id or "-"}, '
		f'Recalculate #{recalc_job.pk}.'
	)
	return ManualSyncResult(
		True,
		message,
		job_ids=job_ids,
		details={
			'summary_job_id': summary.pk,
			'spot_job_id': spot.job_id,
			'earn_job_id': earn.job_id,
			'recalculate_job_id': recalc_job.pk,
			'usd': usd,
		},
	)
