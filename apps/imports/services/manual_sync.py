from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.accounts.services.binance import (
	BinanceSyncResult,
	sync_daily_account_snapshots,
	sync_earn_and_funding,
	sync_spot_balances,
)
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


def _run_binance_step(
	name: str,
	run,
) -> tuple[BinanceSyncResult | None, str | None]:
	try:
		return run(), None
	except Exception as exc:
		logger.exception('Manual Binance %s sync failed', name)
		return None, str(exc)


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

	step_failures: dict[str, str] = {}
	spot, spot_error = _run_binance_step('spot', lambda: sync_spot_balances(create_snapshots=True))
	if spot_error:
		step_failures['spot'] = spot_error

	earn, earn_error = _run_binance_step('earn', sync_earn_and_funding)
	if earn_error:
		step_failures['earn'] = earn_error

	daily, daily_error = _run_binance_step('daily', sync_daily_account_snapshots)
	if daily_error:
		step_failures['daily'] = daily_error

	if not any([spot, earn, daily]):
		job = record_manual_sync_job(
			source_code='binance-api',
			parser_name='binance-manual-sync',
			status=ImportJob.Status.FAILED,
			error_message='; '.join(f'{name}: {message}' for name, message in step_failures.items()),
			details={'step_failures': step_failures},
		)
		return ManualSyncResult(
			False,
			f'Binance sync failed: {"; ".join(step_failures.values())}',
			job_ids=[job.pk],
			details={'step_failures': step_failures},
		)

	usd: dict[str, int] = {}
	recalc_job: ImportJob | None = None
	usd_error = None
	try:
		usd = recalculate_usd_valuations()
		recalc_job = record_manual_sync_job(
			source_code='binance-api',
			parser_name='recalculate-usd-values',
			status=ImportJob.Status.SAVED,
			rows_detected=_usd_rows_updated(usd),
			records_created=_usd_rows_updated(usd),
			details={'trigger': 'manual-binance-sync', 'usd': usd},
		)
	except Exception as exc:
		logger.exception('Manual Binance USD recalculation failed')
		usd_error = str(exc)
		step_failures['recalculate'] = usd_error

	rows_detected = sum(result.rows_detected for result in (spot, earn, daily) if result)
	records_created = sum(
		(result.records_created + result.records_updated)
		for result in (spot, earn, daily)
		if result
	)
	summary_details: dict[str, Any] = {
		'spot_job_id': spot.job_id if spot else None,
		'earn_job_id': earn.job_id if earn else None,
		'daily_snapshots_job_id': daily.job_id if daily else None,
		'recalculate_job_id': recalc_job.pk if recalc_job else None,
		'usd': usd,
		'daily_snapshots': daily.details if daily else {},
	}
	if step_failures:
		summary_details['step_failures'] = step_failures
	if daily and daily.details.get('kline_errors'):
		summary_details['kline_errors'] = daily.details['kline_errors']

	summary = record_manual_sync_job(
		source_code='binance-api',
		parser_name='binance-manual-sync',
		status=ImportJob.Status.SAVED,
		rows_detected=rows_detected,
		records_created=records_created,
		details=summary_details,
		error_message='; '.join(f'{name}: {message}' for name, message in step_failures.items()),
	)
	job_ids = [
		job_id
		for job_id in (
			summary.pk,
			spot.job_id if spot else None,
			earn.job_id if earn else None,
			daily.job_id if daily else None,
			recalc_job.pk if recalc_job else None,
		)
		if job_id
	]
	mark_import_jobs_recent(job_ids, note='Manual Binance sync')

	step_labels = {
		'spot': f'Spot #{spot.job_id}' if spot and spot.job_id else None,
		'earn': f'Earn #{earn.job_id}' if earn and earn.job_id else None,
		'daily': f'Daily snapshots #{daily.job_id}' if daily and daily.job_id else None,
		'recalculate': f'Recalculate #{recalc_job.pk}' if recalc_job else None,
	}
	completed_parts = [label for label in step_labels.values() if label]
	failed_parts = [f'{name} ({step_failures[name]})' for name in ('spot', 'earn', 'daily', 'recalculate') if name in step_failures]

	if step_failures:
		message = (
			f'Binance sync partially completed. Summary job #{summary.pk}. '
			f'Completed: {", ".join(completed_parts) or "none"}. '
			f'Failed: {"; ".join(failed_parts)}.'
		)
	else:
		message = (
			f'Binance sync completed. Summary job #{summary.pk}, '
			f'Spot #{spot.job_id if spot else "-"}, Earn #{earn.job_id if earn else "-"}, '
			f'Daily snapshots #{daily.job_id if daily else "-"}, '
			f'Recalculate #{recalc_job.pk if recalc_job else "-"}.'
		)

	return ManualSyncResult(
		True,
		message,
		job_ids=job_ids,
		details={
			**summary_details,
			'summary_job_id': summary.pk,
			'partial': bool(step_failures),
		},
	)


def sync_priorlife_manual(updates: list[dict]) -> ManualSyncResult:
	from decimal import Decimal

	from apps.common.services.priorlife_insurance import apply_priorlife_manual_update

	job = record_manual_sync_job(
		source_code='priorlife-contributions',
		parser_name='priorlife-manual-update',
		status=ImportJob.Status.PARSING,
		details={'contracts': len(updates)},
	)
	results: list[dict] = []
	try:
		for update in updates:
			result = apply_priorlife_manual_update(
				account_number=update['account_number'],
				payment_date=update['payment_date'],
				premium_amount=Decimal(str(update['premium_amount'])),
				accumulated_amount=Decimal(str(update['accumulated_amount'])),
				import_job=job,
			)
			results.append(result)
	except Exception as exc:
		logger.exception('Manual Priorlife update failed')
		job.status = ImportJob.Status.FAILED
		job.error_message = str(exc)
		job.finished_at = timezone.now()
		job.details = {**(job.details or {}), 'results': results}
		job.save(update_fields=['status', 'error_message', 'finished_at', 'details', 'updated_at'])
		return ManualSyncResult(False, f'Приорлайф: {exc}', job_ids=[job.pk], details={'results': results})

	records_created = sum(1 + item.get('income_records', 0) for item in results)
	job.status = ImportJob.Status.SAVED
	job.records_created = records_created
	job.rows_detected = len(results)
	job.finished_at = timezone.now()
	job.details = {**(job.details or {}), 'results': results}
	job.save(update_fields=['status', 'records_created', 'rows_detected', 'finished_at', 'details', 'updated_at'])
	mark_import_jobs_recent([job.pk], note='Manual Priorlife update')

	accounts = ', '.join(item['account_number'] for item in results)
	message = f'Приорлайф обновлён: {accounts}. Job #{job.pk}.'
	return ManualSyncResult(True, message, job_ids=[job.pk], details={'results': results})
