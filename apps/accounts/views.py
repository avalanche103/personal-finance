from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts.analytics import build_account_groups
from apps.accounts.forms import (
    AccountForm,
    AlfaAigenisTransferForm,
    BynexBinanceTransferForm,
    BynexTradeForm,
    TransactionForm,
)
from apps.accounts.models import Account, Transaction
from apps.accounts.querysets import visible_account_queryset
from apps.accounts.services.shortcuts import (
    record_alfa_to_aigenis_transfer,
    record_bynex_to_binance_transfer,
    record_bynex_usdt_buy,
)
from apps.common.services.ledger import delete_transaction
from apps.products.models import Product


TRANSACTIONS_PAGE_SIZE = 25


def _transaction_source(transaction):
    metadata = transaction.metadata if isinstance(transaction.metadata, dict) else {}
    if transaction.import_fingerprint.startswith('manual:'):
        return 'Manual', metadata.get('source', 'manual')
    if transaction.import_job_id:
        return 'Imported', metadata.get('imported_from') or transaction.import_job.source.name
    return 'API/CLI', metadata.get('source') or metadata.get('imported_from') or 'external'


def _decorate_transactions(transactions):
    for ledger_transaction in transactions:
        source_label, source_detail = _transaction_source(ledger_transaction)
        ledger_transaction.source_label = source_label
        ledger_transaction.source_detail = source_detail
        metadata = ledger_transaction.metadata if isinstance(ledger_transaction.metadata, dict) else {}
        if (
            not (ledger_transaction.description or '').strip()
            and metadata.get('operation_kind') == 'capitalization'
        ):
            ledger_transaction.description = 'Капитализация'
    return transactions


def _transaction_filter_querystring(request, *, page=None):
    params = {}
    for key in ('tx_q', 'tx_account', 'tx_type', 'tx_source', 'tx_date_from', 'tx_date_to'):
        value = request.GET.get(key, '').strip()
        if value:
            params[key] = value
    if page:
        params['page'] = page
    return urlencode(params)


def _transaction_queryset(request):
    transactions = Transaction.objects.select_related(
        'account',
        'account__institution',
        'currency',
        'product',
        'import_job',
        'import_job__source',
    )
    query = request.GET.get('tx_q', '').strip()
    account_id = request.GET.get('tx_account', '').strip()
    transaction_type = request.GET.get('tx_type', '').strip()
    source = request.GET.get('tx_source', '').strip()
    date_from = parse_date(request.GET.get('tx_date_from', '').strip())
    date_to = parse_date(request.GET.get('tx_date_to', '').strip())

    if query:
        transactions = transactions.filter(
            Q(description__icontains=query)
            | Q(external_id__icontains=query)
            | Q(account__name__icontains=query)
            | Q(account__institution__name__icontains=query)
            | Q(product__name__icontains=query)
        )
    if account_id:
        transactions = transactions.filter(account_id=account_id)
    if transaction_type:
        transactions = transactions.filter(transaction_type=transaction_type)
    if source == 'manual':
        transactions = transactions.filter(import_fingerprint__startswith='manual:')
    elif source == 'imported':
        transactions = transactions.filter(import_job__isnull=False)
    elif source == 'api_cli':
        transactions = transactions.filter(import_job__isnull=True).exclude(import_fingerprint__startswith='manual:')
    if date_from:
        transactions = transactions.filter(occurred_at__date__gte=date_from)
    if date_to:
        transactions = transactions.filter(occurred_at__date__lte=date_to)
    return transactions.order_by('-occurred_at', '-id')


def _transaction_list_context(request):
    paginator = Paginator(_transaction_queryset(request), TRANSACTIONS_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page'))
    transactions = _decorate_transactions(list(page_obj.object_list))
    page_obj.object_list = transactions
    return {
        'transactions': transactions,
        'transactions_page': page_obj,
        'transaction_query': request.GET.get('tx_q', '').strip(),
        'transaction_account': request.GET.get('tx_account', '').strip(),
        'transaction_type': request.GET.get('tx_type', '').strip(),
        'transaction_source': request.GET.get('tx_source', '').strip(),
        'transaction_date_from': request.GET.get('tx_date_from', '').strip(),
        'transaction_date_to': request.GET.get('tx_date_to', '').strip(),
        'transaction_accounts': visible_account_queryset().order_by('institution__name', 'name'),
        'transaction_types': Transaction.TransactionType.choices,
        'transaction_filters_query': _transaction_filter_querystring(request),
        'transaction_previous_query': _transaction_filter_querystring(
            request,
            page=page_obj.previous_page_number() if page_obj.has_previous() else None,
        ),
        'transaction_next_query': _transaction_filter_querystring(
            request,
            page=page_obj.next_page_number() if page_obj.has_next() else None,
        ),
    }


def _accounts_url_with_transactions(request):
    query = _transaction_filter_querystring(request)
    url = reverse('accounts:list')
    if query:
        url = f'{url}?{query}'
    return f'{url}#transactions'


def _safe_next_url(request, candidate: str | None) -> str | None:
    value = (candidate or '').strip()
    if not value:
        return None
    if url_has_allowed_host_and_scheme(
        value,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return value
    if value.startswith('/') and not value.startswith('//'):
        return value
    return None


def _transaction_create_redirect(request):
    next_url = _safe_next_url(request, request.POST.get('next') or request.GET.get('next'))
    if next_url:
        return next_url
    return _accounts_url_with_transactions(request)


def _apply_transaction_create_prefill(form, request):
    account_id = request.GET.get('account', '').strip()
    product_id = request.GET.get('product', '').strip()
    transaction_type = (
        request.GET.get('type', '').strip()
        or request.GET.get('transaction_type', '').strip()
    )

    product = None
    if product_id.isdigit():
        product = (
            Product.objects.select_related('currency', 'income_account')
            .filter(pk=int(product_id))
            .first()
        )
        if product is not None:
            form.initial['product'] = product.pk
            form.initial['currency'] = product.currency_id
            if (
                not account_id
                and product.product_type == Product.ProductType.DEPOSIT
                and product.income_account_id
            ):
                account_id = str(product.income_account_id)

    if account_id.isdigit():
        form.initial['account'] = int(account_id)

    valid_types = {choice[0] for choice in Transaction.TransactionType.choices}
    if transaction_type in valid_types:
        form.initial['transaction_type'] = transaction_type

    return product


def _institution_shortcut_actions(institution):
    slug = getattr(institution, 'slug', '') or ''
    if slug == 'bynex':
        return [
            {
                'label': 'Add trade',
                'url': reverse('accounts:bynex_trade_create'),
                'style': 'primary',
            },
            {
                'label': 'Add transfer to Binance',
                'url': reverse('accounts:bynex_binance_transfer_create'),
                'style': 'secondary',
            },
        ]
    if slug == 'alfabank':
        return [
            {
                'label': 'Add transfer to Aigenis',
                'url': reverse('accounts:alfa_aigenis_transfer_create'),
                'style': 'primary',
            },
        ]
    return []


def _with_shortcut_actions(account_groups):
    for group in account_groups:
        group['shortcut_actions'] = _institution_shortcut_actions(group.get('institution'))
    return account_groups


def _shortcut_datetime_initial():
    return timezone.localtime().replace(second=0, microsecond=0)


def _shortcut_form_view(request, *, form_class, title, submit_label, handler, success_message):
    initial = {}
    if request.method != 'POST':
        initial['occurred_at'] = _shortcut_datetime_initial()
    form = form_class(request.POST or None, initial=initial)
    back_href = _accounts_url_with_transactions(request)
    if request.method == 'POST' and form.is_valid():
        try:
            handler(form.cleaned_data)
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, success_message)
            return redirect(back_href)
    return render(
        request,
        'accounts/form.html',
        {
            'form': form,
            'title': title,
            'eyebrow': 'Ledger entry',
            'submit_label': submit_label,
            'back_url': 'accounts:list',
            'back_label': 'Back to accounts',
            'back_href': back_href,
        },
    )


def account_list(request):
    query = request.GET.get('q', '').strip()
    accounts = visible_account_queryset()
    shortcut_accounts = Account.objects.select_related('institution', 'currency').filter(
        institution__slug__in=['bynex', 'alfabank']
    )
    if query:
        search_filter = (
            Q(name__icontains=query)
            | Q(account_type__icontains=query)
            | Q(institution__name__icontains=query)
            | Q(currency__code__icontains=query)
        )
        accounts = accounts.filter(search_filter)
        shortcut_accounts = shortcut_accounts.filter(search_filter)

    account_ids = set(accounts.values_list('pk', flat=True)) | set(shortcut_accounts.values_list('pk', flat=True))
    ordered_accounts = (
        Account.objects.filter(pk__in=account_ids)
        .select_related('institution', 'currency')
        .order_by('institution__name', 'currency__code', 'name')
    )
    context = {
        'account_groups': _with_shortcut_actions(build_account_groups(ordered_accounts)),
        'query': query,
    }
    template_name = 'accounts/partials/table.html' if request.headers.get('HX-Request') == 'true' else 'accounts/list.html'
    if template_name == 'accounts/list.html':
        context.update(_transaction_list_context(request))
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


def bynex_trade_create(request):
    return _shortcut_form_view(
        request,
        form_class=BynexTradeForm,
        title='Add BYNEX trade',
        submit_label='Create trade',
        handler=lambda data: record_bynex_usdt_buy(
            usd_spent=data['usd_spent'],
            usdt_received=data['usdt_received'],
            occurred_at=data['occurred_at'],
        ),
        success_message='BYNEX USD → USDT trade created.',
    )


def bynex_binance_transfer_create(request):
    return _shortcut_form_view(
        request,
        form_class=BynexBinanceTransferForm,
        title='Add transfer to Binance',
        submit_label='Create transfer',
        handler=lambda data: record_bynex_to_binance_transfer(
            quantity=data['quantity'],
            fee=data['fee'] or 0,
            occurred_at=data['occurred_at'],
        ),
        success_message='USDT transfer from BYNEX to Binance created.',
    )


def alfa_aigenis_transfer_create(request):
    return _shortcut_form_view(
        request,
        form_class=AlfaAigenisTransferForm,
        title='Add transfer to Aigenis',
        submit_label='Create transfer',
        handler=lambda data: record_alfa_to_aigenis_transfer(
            amount=data['amount'],
            fee=data['fee'] or 0,
            occurred_at=data['occurred_at'],
        ),
        success_message='BYN transfer from АльфаБанк to Aigenis created.',
    )


def transaction_create(request):
    form = TransactionForm(request.POST or None)
    if request.method == 'GET':
        _apply_transaction_create_prefill(form, request)

    next_url = _safe_next_url(request, request.POST.get('next') or request.GET.get('next'))
    back_href = next_url or _accounts_url_with_transactions(request)
    back_label = 'Back to product' if next_url else 'Back to accounts'

    if request.method == 'POST' and form.is_valid():
        ledger_transaction = form.save()
        messages.success(request, f'Transaction #{ledger_transaction.pk} created.')
        return redirect(_transaction_create_redirect(request))

    return render(
        request,
        'accounts/form.html',
        {
            'form': form,
            'title': 'Add transaction',
            'eyebrow': 'Ledger entry',
            'submit_label': 'Create transaction',
            'back_url': 'accounts:list',
            'back_label': back_label,
            'back_href': back_href,
            'next_url': next_url or '',
        },
    )


def transaction_list(request):
    return render(request, 'accounts/partials/transactions_table.html', _transaction_list_context(request))


def transaction_edit(request, pk):
    ledger_transaction = get_object_or_404(
        Transaction.objects.select_related('import_job', 'import_job__source'),
        pk=pk,
    )
    form = TransactionForm(request.POST or None, instance=ledger_transaction)
    if request.method == 'POST' and form.is_valid():
        updated_transaction = form.save()
        messages.success(request, f'Transaction #{updated_transaction.pk} updated.')
        return redirect(_accounts_url_with_transactions(request))

    is_imported = bool(ledger_transaction.import_job_id) or not ledger_transaction.import_fingerprint.startswith('manual:')
    return render(
        request,
        'accounts/form.html',
        {
            'form': form,
            'title': f'Edit transaction #{ledger_transaction.pk}',
            'eyebrow': 'Ledger entry',
            'submit_label': 'Save transaction',
            'back_url': 'accounts:list',
            'back_label': 'Back to accounts',
            'back_href': _accounts_url_with_transactions(request),
            'is_imported_transaction': is_imported,
        },
    )


def transaction_delete(request, pk):
    ledger_transaction = get_object_or_404(
        Transaction.objects.select_related('account', 'currency', 'import_job', 'import_job__source'),
        pk=pk,
    )
    source_label, source_detail = _transaction_source(ledger_transaction)
    ledger_transaction.source_label = source_label
    ledger_transaction.source_detail = source_detail
    return render(
        request,
        'accounts/transaction_confirm_delete.html',
        {
            'transaction': ledger_transaction,
            'back_url': _accounts_url_with_transactions(request),
        },
    )


@require_POST
def transaction_delete_confirm(request, pk):
    ledger_transaction = get_object_or_404(Transaction, pk=pk)
    transaction_pk = ledger_transaction.pk
    delete_transaction(ledger_transaction)
    messages.success(request, f'Transaction #{transaction_pk} deleted.')
    return redirect(_accounts_url_with_transactions(request))
