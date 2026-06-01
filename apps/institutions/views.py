from django.db.models import Q
from django.shortcuts import render

from apps.institutions.models import FinancialInstitution


def institution_list(request):
    query = request.GET.get('q', '').strip()
    institutions = FinancialInstitution.objects.select_related('base_currency')
    if query:
        institutions = institutions.filter(
            Q(name__icontains=query) | Q(country__icontains=query) | Q(institution_type__icontains=query)
        )

    context = {
        'institutions': institutions.order_by('name'),
        'query': query,
    }
    template_name = 'institutions/partials/table.html' if request.headers.get('HX-Request') == 'true' else 'institutions/list.html'
    return render(request, template_name, context)
