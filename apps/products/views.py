from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.accounts.models import Transaction
from apps.common.models import ExchangeRateHistory
from apps.products.analytics import (
    build_product_groups,
    build_product_performance_summary,
    build_product_position_summary,
    build_product_transaction_map,
)
from apps.products.models import Product


def product_list(request):
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

    ordered_products = products.order_by('institution__name', 'currency__code', 'name')
    transaction_map = build_product_transaction_map([product.id for product in ordered_products])
    context = {
        'product_groups': build_product_groups(ordered_products, transaction_map=transaction_map, as_of_date=timezone.localdate()),
        'query': query,
        'show_closed': show_closed,
        'search_includes_closed': bool(query and not show_closed),
    }
    template_name = 'products/partials/table.html' if request.headers.get('HX-Request') == 'true' else 'products/list.html'
    return render(request, template_name, context)


def product_detail(request, pk):
    product = get_object_or_404(
        Product.objects.select_related('institution', 'currency'),
        pk=pk,
    )
    filter_from = parse_date(request.GET.get('from', '').strip()) if request.GET.get('from') else None
    filter_to = parse_date(request.GET.get('to', '').strip()) if request.GET.get('to') else None

    all_transactions = list(
        Transaction.objects.filter(product=product)
        .select_related('account', 'currency')
        .order_by('occurred_at', 'id')
    )
    filtered_transactions_qs = (
        Transaction.objects.filter(product=product)
        .select_related('account', 'currency')
        .order_by('-occurred_at', '-id')
    )
    currency_rates_qs = (
        ExchangeRateHistory.objects.filter(currency=product.currency)
        .select_related('currency')
        .order_by('-rate_date')
    )

    if filter_from:
        filtered_transactions_qs = filtered_transactions_qs.filter(occurred_at__date__gte=filter_from)
        currency_rates_qs = currency_rates_qs.filter(rate_date__gte=filter_from)
    if filter_to:
        filtered_transactions_qs = filtered_transactions_qs.filter(occurred_at__date__lte=filter_to)
        currency_rates_qs = currency_rates_qs.filter(rate_date__lte=filter_to)

    transactions = list(filtered_transactions_qs)
    currency_rates = list(currency_rates_qs[:20])
    position_summary = build_product_position_summary(all_transactions, product.market_value)
    performance_summary = build_product_performance_summary(all_transactions, position_summary, as_of_date=timezone.localdate())

    context = {
        'product': product,
        'transactions': transactions,
        'transaction_count': len(transactions),
        'latest_rate': currency_rates[0] if currency_rates else None,
        'product_metadata': product.metadata.items() if isinstance(product.metadata, dict) else [],
        'position_summary': position_summary,
        'performance_summary': performance_summary,
        'filter_from': filter_from.isoformat() if filter_from else '',
        'filter_to': filter_to.isoformat() if filter_to else '',
        'all_transaction_count': len(all_transactions),
    }
    return render(request, 'products/detail.html', context)
