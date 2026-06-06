from django.contrib import messages
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render

from apps.imports.forms import ImportUploadForm
from apps.imports.models import ImportJob
from apps.imports.services.details import get_editable_records, infer_record_fields, update_editable_record
from apps.imports.services.manual_sync import sync_binance_manual, sync_nbrb_rates_manual
from apps.imports.services.pipeline import process_clipboard_import, process_uploaded_import
from apps.imports.services.progress import job_progress
from apps.imports.services.recent_jobs import recent_import_jobs, recent_import_jobs_queryset


def import_upload(request):
    form = ImportUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        if form.cleaned_data['clipboard_text']:
            job, created = process_clipboard_import(form.cleaned_data['source'], form.cleaned_data['clipboard_text'])
        else:
            job, created = process_uploaded_import(form.cleaned_data['source'], form.cleaned_data['file'])
        if created:
            messages.success(request, f'Import job #{job.pk} created with status {job.status}.')
        else:
            messages.info(request, f'Import job #{job.pk} already exists for this file.')
        return redirect('imports:detail', pk=job.pk)

    active_job = (
        ImportJob.objects.exclude(status__in=[ImportJob.Status.SAVED, ImportJob.Status.FAILED])
        .select_related('source')
        .order_by('-created_at')
        .first()
    )
    highlight_job_ids = request.session.pop('recent_job_ids', [])
    context = {
        'form': form,
        'recent_jobs': recent_import_jobs(),
        'highlight_job_ids': highlight_job_ids,
        'active_job': active_job,
        'active_progress': job_progress(active_job) if active_job else None,
    }
    return render(request, 'imports/upload.html', context)


def import_recent_jobs(request):
    return render(
        request,
        'imports/partials/history_table.html',
        {'jobs': recent_import_jobs()},
    )


def import_history(request):
    jobs = recent_import_jobs_queryset()
    context = {'jobs': jobs}
    template_name = 'imports/partials/history_table.html' if request.headers.get('HX-Request') == 'true' else 'imports/history.html'
    return render(request, template_name, context)


def import_job_detail(request, pk):
    job = get_object_or_404(ImportJob.objects.select_related('source', 'institution'), pk=pk)
    editable_records = get_editable_records(job)
    context = {
        'job': job,
        'progress': job_progress(job),
        'editable_records': editable_records,
        'record_fields': (job.details or {}).get('record_fields') or infer_record_fields(editable_records),
        'warnings': (job.details or {}).get('warnings', []),
        'metadata': (job.details or {}).get('metadata', {}),
    }
    return render(request, 'imports/detail.html', context)


def import_job_progress(request, pk):
    job = get_object_or_404(ImportJob, pk=pk)
    context = {'job': job, 'progress': job_progress(job)}
    return render(request, 'imports/partials/job_progress.html', context)


def import_sync_nbrb(request):
	if request.method != 'POST':
		return HttpResponseBadRequest('POST required')

	result = sync_nbrb_rates_manual()
	if result.success:
		messages.success(request, result.message)
	else:
		messages.error(request, result.message)
	if result.job_ids:
		request.session['recent_job_ids'] = result.job_ids
	return redirect('imports:upload')


def import_sync_binance(request):
	if request.method != 'POST':
		return HttpResponseBadRequest('POST required')

	result = sync_binance_manual()
	if result.success:
		messages.success(request, result.message)
	else:
		level = messages.warning if result.details.get('skipped') else messages.error
		level(request, result.message)
	if result.job_ids:
		request.session['recent_job_ids'] = result.job_ids
	return redirect('imports:upload')


def import_record_update(request, pk, row_index):
    if request.method != 'POST':
        return HttpResponseBadRequest('POST required')

    job = get_object_or_404(ImportJob, pk=pk)
    field = request.POST.get('field', '').strip()
    value = request.POST.get('value', '')
    if not field:
        return HttpResponseBadRequest('Field required')

    ok, error = update_editable_record(job, int(row_index), field, value)
    if not ok:
        return HttpResponseBadRequest(error)

    editable_records = get_editable_records(job)
    record_fields = (job.details or {}).get('record_fields') or infer_record_fields(editable_records)
    context = {
        'job': job,
        'row_index': row_index,
        'record': editable_records[int(row_index)],
        'record_fields': record_fields,
    }
    return render(request, 'imports/partials/parse_record_row.html', context)
