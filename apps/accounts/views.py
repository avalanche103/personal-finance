from django.db.models import Q
from django.contrib import messages
from django.shortcuts import redirect, render

from apps.accounts.analytics import build_account_groups
from apps.accounts.forms import AccountForm, TransactionForm
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


def account_create(request):
    form = AccountForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        account = form.save()
        messages.success(request, f'Account "{account.name}" created.')
        return redirect('accounts:list')

    return render(
        request,
        'accounts/form.html',
        {
            'form': form,
            'title': 'Add account',
            'eyebrow': 'Ledger',
            'submit_label': 'Create account',
            'back_url': 'accounts:list',
        },
    )


def transaction_create(request):
    form = TransactionForm(request.POST or None)
    account_id = request.GET.get('account')
    if request.method == 'GET' and account_id:
        form.initial['account'] = account_id

    if request.method == 'POST' and form.is_valid():
        ledger_transaction = form.save()
        messages.success(request, f'Transaction #{ledger_transaction.pk} created.')
        return redirect('accounts:list')

    return render(
        request,
        'accounts/form.html',
        {
            'form': form,
            'title': 'Add transaction',
            'eyebrow': 'Ledger entry',
            'submit_label': 'Create transaction',
            'back_url': 'accounts:list',
        },
    )
