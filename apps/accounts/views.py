from django.db.models import Q
from django.shortcuts import render

from apps.accounts.models import Account


def account_list(request):
    query = request.GET.get('q', '').strip()
    accounts = Account.objects.select_related('institution', 'currency')
    if query:
        accounts = accounts.filter(
            Q(name__icontains=query)
            | Q(account_type__icontains=query)
            | Q(institution__name__icontains=query)
            | Q(currency__code__icontains=query)
        )

    context = {
        'accounts': accounts.order_by('name'),
        'query': query,
    }
    template_name = 'accounts/partials/table.html' if request.headers.get('HX-Request') == 'true' else 'accounts/list.html'
    return render(request, template_name, context)
