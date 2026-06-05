from urllib.parse import urlencode

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.models import Transaction
from apps.common.models import ExchangeRateHistory
from apps.products.analytics import (
    build_portfolio_allocation,
    build_product_groups,
    build_product_performance_summary,
    build_product_position_summary,
    build_product_transaction_map,
)
from apps.products.forms import ProductTokenTermsForm
from apps.products.models import Product
from apps.products.services.token_terms import estimate_next_income_date, income_payment_dates


PRODUCT_SORT_FIELDS = ('name', 'institution', 'type', 'currency', 'units', 'value_usd', 'value_byn', 'maturity_date')
PRODUCT_NUMERIC_SORT_FIELDS = {'units', 'value_usd', 'value_byn'}


def _resolve_product_sort(request):
    sort_field = request.GET.get('sort', 'value_usd')
    if sort_field not in PRODUCT_SORT_FIELDS:
        sort_field = 'value_usd'

    sort_dir = request.GET.get('dir', 'desc')
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'desc'

    return sort_field, sort_dir


def _next_product_sort_dir(field: str, current_sort: str, current_dir: str) -> str:
    if field == current_sort:
        return 'desc' if current_dir == 'asc' else 'asc'
    return 'desc' if field in PRODUCT_NUMERIC_SORT_FIELDS else 'asc'


def _product_list_nav_params(request) -> dict[str, str]:
    params = {}
    for key in ('q', 'show_closed', 'sort', 'dir'):
        value = request.GET.get(key, '').strip()
        if value:
            params[key] = value
    return params


def _product_list_queryset(request, *, ensure_product: Product | None = None):
    query = request.GET.get('q', '').strip()
    show_closed = request.GET.get('show_closed') == '1'
    products = Product.objects.select_related('institution', 'currency')
    if query:
        products = products.filter(
            Q(name__icontains=query)
            | Q(symbol__icontains=query)
            | Q(isin__icontains=query)
            | Q(product_type__icontains=query)
            | Q(institution__name__icontains=query)
        )
    elif not show_closed:
        products = products.filter(is_active=True)

    product_ids = list(products.order_by('institution__name', 'currency__code', 'name').values_list('pk', flat=True))
    if ensure_product and ensure_product.pk not in product_ids:
        product_ids.insert(0, ensure_product.pk)

    return Product.objects.filter(pk__in=product_ids).select_related('institution', 'currency').order_by(
        'institution__name',
        'currency__code',
        'name',
    )


def _ordered_products_for_navigation(request, *, ensure_product: Product | None = None) -> list[Product]:
    sort_field, sort_dir = _resolve_product_sort(request)
    products = list(_product_list_queryset(request, ensure_product=ensure_product))
    transaction_map = build_product_transaction_map([product.id for product in products])
    product_groups = build_product_groups(
        products,
        transaction_map=transaction_map,
        as_of_date=timezone.localdate(),
        sort_field=sort_field,
        sort_dir=sort_dir,
    )
    ordered: list[Product] = []
    for group in product_groups:
        ordered.extend(group['products'])
    return ordered


def _product_navigation(request, product: Product) -> dict:
    ordered = _ordered_products_for_navigation(request, ensure_product=product)
    product_ids = [item.pk for item in ordered]
    try:
        index = product_ids.index(product.pk)
    except ValueError:
        return {
            'prev_product': None,
            'next_product': None,
            'nav_index': 0,
            'nav_total': 0,
        }

    nav_params = _product_list_nav_params(request)
    return {
        'prev_product': ordered[index - 1] if index > 0 else None,
        'next_product': ordered[index + 1] if index < len(ordered) - 1 else None,
        'nav_index': index + 1,
        'nav_total': len(ordered),
        'nav_query': urlencode(nav_params),
    }


def product_list(request):
    query = request.GET.get('q', '').strip()
    show_closed = request.GET.get('show_closed') == '1'
    sort_field, sort_dir = _resolve_product_sort(request)
    ordered_products = _product_list_queryset(request)
    transaction_map = build_product_transaction_map([product.id for product in ordered_products])
    context = {
        'product_groups': build_product_groups(
            ordered_products,
            transaction_map=transaction_map,
            as_of_date=timezone.localdate(),
            sort_field=sort_field,
            sort_dir=sort_dir,
        ),
        'portfolio_allocation': build_portfolio_allocation(ordered_products),
        'query': query,
        'show_closed': show_closed,
        'search_includes_closed': bool(query and not show_closed),
        'sort': sort_field,
        'sort_dir': sort_dir,
        'sort_next_dirs': {
            field: _next_product_sort_dir(field, sort_field, sort_dir)
            for field in PRODUCT_SORT_FIELDS
        },
        'product_list_query': urlencode(_product_list_nav_params(request)),
    }
    template_name = 'products/partials/table.html' if request.headers.get('HX-Request') == 'true' else 'products/list.html'
    return render(request, template_name, context)


TOKEN_TERMS_UPDATE_FIELDS = (
    'annual_rate_pct',
    'maturity_date',
    'income_schedule',
    'next_income_date',
    'terms_updated_at',
    'updated_at',
)


def _build_product_detail_context(product: Product) -> dict:
    all_transactions = list(
        Transaction.objects.filter(product=product)
        .select_related('account', 'currency')
        .order_by('occurred_at', 'id')
    )
    transactions = list(
        Transaction.objects.filter(product=product)
        .select_related('account', 'currency')
        .order_by('-occurred_at', '-id')
    )
    currency_rates = list(
        ExchangeRateHistory.objects.filter(currency=product.currency)
        .select_related('currency')
        .order_by('-rate_date')[:20]
    )
    position_summary = build_product_position_summary(
        all_transactions,
        product.market_value,
        market_value_usd=product.current_value_usd,
        currency=product.currency,
    )
    performance_summary = build_product_performance_summary(
        all_transactions,
        position_summary,
        as_of_date=timezone.localdate(),
    )

    return {
        'product': product,
        'transactions': transactions,
        'transaction_count': len(transactions),
        'latest_rate': currency_rates[0] if currency_rates else None,
        'product_metadata': product.metadata.items() if isinstance(product.metadata, dict) else [],
        'position_summary': position_summary,
        'performance_summary': performance_summary,
    }


@require_http_methods(['GET', 'POST'])
def product_detail(request, pk):
    product = get_object_or_404(
        Product.objects.select_related('institution', 'currency'),
        pk=pk,
    )
    terms_form = ProductTokenTermsForm(instance=product)

    if request.method == 'POST':
        action = request.POST.get('action', 'save_terms')

        if action == 'recompute_next_income':
            estimated = estimate_next_income_date(product)
            if estimated is None:
                messages.warning(request, 'Could not estimate next income date. Set income schedule and import income history first.')
            else:
                product.next_income_date = estimated
                product.save(update_fields=['next_income_date', 'updated_at'])
                messages.success(request, f'Next income date set to {estimated.isoformat()}.')
            query = _product_list_nav_params(request)
            url = reverse('products:detail', args=[product.pk])
            return redirect(f'{url}?{urlencode(query)}' if query else url)

        terms_form = ProductTokenTermsForm(request.POST, instance=product)
        if terms_form.is_valid():
            updated_product = terms_form.save(commit=False)
            updated_product.terms_updated_at = timezone.now()
            updated_product.save(update_fields=list(TOKEN_TERMS_UPDATE_FIELDS))
            messages.success(request, 'Token terms saved.')
            query = _product_list_nav_params(request)
            url = reverse('products:detail', args=[product.pk])
            return redirect(f'{url}?{urlencode(query)}' if query else url)

        messages.error(request, 'Could not save token terms. Please fix the errors below.')

    context = _build_product_detail_context(product)
    context['terms_form'] = terms_form
    context['estimated_next_income_date'] = estimate_next_income_date(product)
    context['income_payment_count'] = len(income_payment_dates(product))
    context.update(_product_navigation(request, product))
    return render(request, 'products/detail.html', context)
