from django.contrib import messages
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render

from apps.imports.forms import CashManualOperationForm, ImportUploadForm, PriorlifeManualUpdateForm
from apps.imports.models import ImportJob, ImportSource
from apps.imports.services.details import get_editable_records, infer_record_fields, update_editable_record
from apps.imports.services.manual_sync import (
	sync_binance_manual,
	sync_cash_manual,
	sync_nbrb_rates_manual,
	sync_priorlife_manual,
)
from apps.imports.services.pipeline import process_clipboard_import, process_uploaded_import
from apps.imports.services.progress import job_progress
from apps.imports.services.recent_jobs import recent_import_jobs, recent_import_jobs_queryset
from apps.common.services.priorlife_insurance import list_priorlife_products
from apps.accounts.models import Account
from apps.common.services.cash_operations import CASH_INSTITUTION_SLUG


SYSTEM_IMPORT_GROUP = {
    'key': 'system-rates',
    'name': 'Курсы и справочники',
    'description': 'Системные данные без отдельного финансового института.',
    'type_label': 'Сервис',
    'logo_slug': 'nbrb',
}

FILE_ACTION_META = {
    ImportSource.SourceType.PDF: {
        'title': 'Загрузить PDF',
        'description': 'Выписки, отчеты и страховые документы в PDF.',
        'accept': '.pdf,application/pdf',
        'hint': 'PDF statement',
    },
    ImportSource.SourceType.XLS: {
        'title': 'Загрузить Excel',
        'description': 'История операций или брокерский отчет XLS/XLSX.',
        'accept': '.xls,.xlsx,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'hint': 'XLS/XLSX report',
    },
}

API_ACTIONS = {
    'binance-api': {
        'title': 'Синхронизировать Binance',
        'description': 'Обновляет Spot, Earn и Funding через read-only API.',
        'hint': 'API sync',
        'url_name': 'imports:sync_binance',
    },
    'nbrb-exrates-api': {
        'title': 'Обновить курсы НБРБ',
        'description': 'Загружает последние курсы по отслеживаемым валютам.',
        'hint': 'API sync',
        'url_name': 'imports:sync_nbrb',
    },
}


def _group_for_source(source):
    institution = source.institution
    if institution:
        return {
            'key': f'institution-{institution.pk}',
            'institution': institution,
            'name': institution.name,
            'description': institution.website or institution.get_institution_type_display(),
            'type_label': institution.get_institution_type_display(),
            'actions': [],
        }
    return {**SYSTEM_IMPORT_GROUP, 'actions': []}


def _posted_source_pk(request):
    try:
        return int(request.POST.get('source') or 0)
    except (TypeError, ValueError):
        return None


def _build_import_groups(priorlife_products, posted_source_pk=None):
    groups = {}
    ordered_groups = []
    sources = (
        ImportSource.objects.filter(is_active=True)
        .select_related('institution')
        .order_by('institution__name', 'source_type', 'name')
    )

    for source in sources:
        group = _group_for_source(source)
        if group['key'] not in groups:
            groups[group['key']] = group
            ordered_groups.append(group)
        else:
            group = groups[group['key']]

        if source.source_type in FILE_ACTION_META:
            meta = FILE_ACTION_META[source.source_type]
            group['actions'].append(
                {
                    'kind': 'file',
                    'source': source,
                    'title': meta['title'],
                    'description': meta['description'],
                    'accept': meta['accept'],
                    'hint': meta['hint'],
                    'show_errors': posted_source_pk == source.pk,
                }
            )

        if source.code == 'finstore-history':
            group['actions'].append(
                {
                    'kind': 'clipboard',
                    'source': source,
                    'title': 'Вставить операции',
                    'description': 'Быстрый импорт строк Finstore из буфера обмена.',
                    'hint': 'TSV clipboard',
                    'show_errors': posted_source_pk == source.pk,
                }
            )

        if source.code == 'cash-manual':
            group['actions'].append(
                {
                    'kind': 'cash_manual',
                    'source': source,
                    'title': 'Записать операцию',
                    'description': 'Пополнение, расход или перевод между наличными и банковским счётом.',
                    'hint': 'Manual input',
                }
            )

        api_action = API_ACTIONS.get(source.code)
        if api_action:
            group['actions'].append({'kind': 'api', 'source': source, **api_action})

    if priorlife_products:
        for group in ordered_groups:
            institution = group.get('institution')
            if institution and institution.slug == 'priorlife':
                group['actions'].append(
                    {
                        'kind': 'priorlife_manual',
                        'title': 'Ручное обновление договора',
                        'description': 'Дата взноса и текущая сумма продукта из личного кабинета.',
                        'hint': 'Manual input',
                    }
                )
                break

    ordered_groups = [group for group in ordered_groups if group['actions']]
    selected_group_key = ''
    if posted_source_pk:
        for group in ordered_groups:
            if any(action.get('source') and action['source'].pk == posted_source_pk for action in group['actions']):
                selected_group_key = group['key']
                break

    return ordered_groups, selected_group_key


def _cash_manual_context():
    cash_accounts = list(
        Account.objects.filter(
            institution__slug=CASH_INSTITUTION_SLUG,
            account_type=Account.AccountType.CASH,
            is_active=True,
        )
        .select_related('currency', 'institution')
        .order_by('name')
    )
    transfer_accounts = Account.objects.exclude(
        institution__slug=CASH_INSTITUTION_SLUG,
    ).select_related('currency', 'institution').order_by('institution__name', 'name')
    return {
        'cash_form': CashManualOperationForm(cash_accounts, transfer_accounts),
        'cash_accounts': cash_accounts,
    }


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
    priorlife_products = list_priorlife_products()
    import_groups, selected_group_key = _build_import_groups(priorlife_products, _posted_source_pk(request))
    context = {
        'form': form,
        'priorlife_form': PriorlifeManualUpdateForm(priorlife_products),
        'priorlife_products': priorlife_products,
        'import_groups': import_groups,
        'selected_group_key': selected_group_key,
        'recent_jobs': recent_import_jobs(),
        'highlight_job_ids': highlight_job_ids,
        'active_job': active_job,
        'active_progress': job_progress(active_job) if active_job else None,
        **_cash_manual_context(),
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
		if result.details.get('partial'):
			messages.warning(request, result.message)
		else:
			messages.success(request, result.message)
	else:
		level = messages.warning if result.details.get('skipped') else messages.error
		level(request, result.message)
	if result.job_ids:
		request.session['recent_job_ids'] = result.job_ids
	return redirect('imports:upload')


def import_priorlife_update(request):
	if request.method != 'POST':
		return HttpResponseBadRequest('POST required')

	priorlife_products = list_priorlife_products()
	form = PriorlifeManualUpdateForm(priorlife_products, request.POST)
	if not form.is_valid():
		for error in form.non_field_errors():
			messages.error(request, error)
		for field_errors in form.errors.values():
			for error in field_errors:
				messages.error(request, error)
		return redirect('imports:upload')

	result = sync_priorlife_manual(form.cleaned_contract_updates())
	if result.success:
		messages.success(request, result.message)
	else:
		messages.error(request, result.message)
	if result.job_ids:
		request.session['recent_job_ids'] = result.job_ids
	return redirect('imports:upload')


def import_cash_operation(request):
	if request.method != 'POST':
		return HttpResponseBadRequest('POST required')

	cash_accounts = list(
		Account.objects.filter(
			institution__slug=CASH_INSTITUTION_SLUG,
			account_type=Account.AccountType.CASH,
			is_active=True,
		).select_related('currency', 'institution')
	)
	transfer_accounts = Account.objects.exclude(
		institution__slug=CASH_INSTITUTION_SLUG,
	).select_related('currency', 'institution')
	form = CashManualOperationForm(cash_accounts, transfer_accounts, request.POST)
	if not form.is_valid():
		for error in form.non_field_errors():
			messages.error(request, error)
		for field_errors in form.errors.values():
			for error in field_errors:
				messages.error(request, error)
		return redirect('imports:upload')

	result = sync_cash_manual(form.cleaned_payload())
	if result.success:
		messages.success(request, result.message)
	else:
		messages.error(request, result.message)
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
