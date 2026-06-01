from django.conf import settings


def project_settings(request):
    return {
        'project_name': 'Personal Finance Dashboard',
        'reporting_base_currency': settings.REPORTING_BASE_CURRENCY,
    }