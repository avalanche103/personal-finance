from django.db.models import Q
from django.shortcuts import render

from apps.accounts.analytics import build_account_groups
from apps.accounts.querysets import visible_account_queryset


def account_list(request):
    query = request.GET.get('q', '').strip()
    accounts = visible_account_queryset()
    if query:
        accounts = accounts.filter(
            Q(name__icontains=query)
            | Q(account_type__icontains=query)
            | Q(institution__name__icontains=query)
            | Q(currency__code__icontains=query)
        )

    ordered_accounts = accounts.order_by('institution__name', 'currency__code', 'name')
    context = {
        'account_groups': build_account_groups(ordered_accounts),
        'query': query,
    }
    template_name = 'accounts/partials/table.html' if request.headers.get('HX-Request') == 'true' else 'accounts/list.html'
    return render(request, template_name, context)
