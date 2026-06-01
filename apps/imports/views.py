from django.contrib import messages
from django.shortcuts import redirect, render

from apps.imports.forms import ImportUploadForm
from apps.imports.models import ImportJob
from apps.imports.services.pipeline import process_uploaded_import


def import_upload(request):
    form = ImportUploadForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        job, created = process_uploaded_import(form.cleaned_data['source'], form.cleaned_data['file'])
        if created:
            messages.success(request, f'Import job #{job.pk} created with status {job.status}.')
        else:
            messages.info(request, f'Import job #{job.pk} already exists for this file.')
        return redirect('imports:upload')

    context = {
        'form': form,
        'recent_jobs': ImportJob.objects.select_related('source').order_by('-created_at')[:10],
    }
    return render(request, 'imports/upload.html', context)


def import_history(request):
    jobs = ImportJob.objects.select_related('source', 'institution').order_by('-created_at')
    context = {'jobs': jobs}
    template_name = 'imports/partials/history_table.html' if request.headers.get('HX-Request') == 'true' else 'imports/history.html'
    return render(request, template_name, context)
