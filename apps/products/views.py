from decimal import Decimal
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
    allocation_instrument_choices,
    build_portfolio_allocation,
    build_product_groups,
    build_product_performance_summary,
    build_product_position_summary,
    build_product_transaction_map,
)
from apps.common.services.indexed_bonds import (
    build_income_calendar_rows,
    build_product_income_calendar,
    save_income_calendar_config,
)
from apps.products.forms import ProductDepositTermsForm, ProductForm, ProductIncomeCalendarForm, ProductTokenTermsForm
from apps.products.models import Product
from apps.products.services.token_terms import estimate_next_income_date, income_payment_dates


def _metadata_decimal(metadata: dict, key: str) -> Decimal:
    value = metadata.get(key)
    if value in (None, ''):
        return Decimal('0')
    return Decimal(str(value).strip().replace(' ', '').replace(',', '.'))


def _product_performance_as_of_date(product: Product):
    metadata = product.metadata if isinstance(product.metadata, dict) else {}
    raw_as_of = str(metadata.get('as_of_date', '') or '').strip()
    if raw_as_of:
        for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
            try:
                from datetime import datetime

                return datetime.strptime(raw_as_of, fmt).date()
            except ValueError:
                continue
    return timezone.localdate()


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


def _resolve_allocation_type(request, *, valid_types: set[str] | None = None) -> str:
    value = request.GET.get('allocation_type', '').strip().lower()
    if valid_types is None:
        valid_types = {choice[0] for choice in Product.ProductType.choices}
    return value if value in valid_types else ''


def _product_list_nav_params(request) -> dict[str, str]:
    params = {}
    for key in ('q', 'show_closed', 'sort', 'dir', 'allocation_type'):
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
    allocation_type_choices = allocation_instrument_choices(ordered_products)
    available_allocation_types = {value for value, _label in allocation_type_choices}
    allocation_type = _resolve_allocation_type(request, valid_types=available_allocation_types)
    transaction_map = build_product_transaction_map([product.id for product in ordered_products])
    context = {
        'product_groups': build_product_groups(
            ordered_products,
            transaction_map=transaction_map,
            as_of_date=timezone.localdate(),
            sort_field=sort_field,
            sort_dir=sort_dir,
        ),
        'portfolio_allocation': build_portfolio_allocation(
            ordered_products,
            instrument_type=allocation_type,
        ),
        'allocation_type': allocation_type,
        'allocation_type_choices': allocation_type_choices,
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


def product_create(request):
    form = ProductForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        product = form.save()
        messages.success(request, f'Product "{product.name}" created.')
        return redirect('products:detail', pk=product.pk)

    return render(
        request,
        'products/form.html',
        {
            'form': form,
            'title': 'Add product',
            'eyebrow': 'Assets',
            'submit_label': 'Create product',
            'back_url': 'products:list',
        },
    )


TOKEN_TERMS_UPDATE_FIELDS = (
    'annual_rate_pct',
    'maturity_date',
    'income_schedule',
    'next_income_date',
    'terms_updated_at',
    'updated_at',
)

DEPOSIT_TERMS_UPDATE_FIELDS = TOKEN_TERMS_UPDATE_FIELDS + (
    'income_account',
    'metadata',
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
        product_type=product.product_type,
    )
    performance_summary = build_product_performance_summary(
        all_transactions,
        position_summary,
        as_of_date=_product_performance_as_of_date(product),
        product_type=product.product_type,
    )

    pension_summary = None
    life_insurance_summary = None
    deposit_summary = None
    if product.product_type == Product.ProductType.PENSION:
        metadata = product.metadata if isinstance(product.metadata, dict) else {}
        pension_summary = {
            'own_contributions_byn': position_summary['purchase_cost'],
            'employer_subsidy_byn': position_summary.get('employer_subsidy', Decimal('0')),
            'total_contributions_byn': position_summary['purchase_cost'] + position_summary.get('employer_subsidy', Decimal('0')),
            'management_expense_pct': metadata.get('management_expense_pct'),
            'insurance_sum_byn': metadata.get('insurance_sum_byn'),
        }
    elif product.product_type == Product.ProductType.LIFE_INSURANCE:
        metadata = product.metadata if isinstance(product.metadata, dict) else {}
        life_insurance_summary = {
            'paid_contributions_gross': _metadata_decimal(metadata, 'paid_contributions_gross') or _metadata_decimal(metadata, 'paid_contributions_total'),
            'net_contributions': _metadata_decimal(metadata, 'net_contributions_total'),
            'contract_load_deducted': _metadata_decimal(metadata, 'contract_load_deducted_total'),
            'accrued_yield_reported': _metadata_decimal(metadata, 'accrued_yield_reported'),
            'accrued_yield_in_account': _metadata_decimal(metadata, 'accrued_yield_in_account'),
            'additional_accrued_yield_in_account': _metadata_decimal(metadata, 'additional_accrued_yield_in_account'),
            'accumulated_amount': _metadata_decimal(metadata, 'accumulated_amount') or product.current_price,
            'contract_load_pct': metadata.get('contract_load_pct'),
            'guaranteed_yield_pct': metadata.get('guaranteed_yield_pct'),
            'future_payments_total': metadata.get('future_payments_total'),
        }
    elif product.product_type == Product.ProductType.DEPOSIT:
        metadata = product.metadata if isinstance(product.metadata, dict) else {}
        deposit_summary = {
            'principal': product.market_value,
            'principal_usd': product.current_value_usd or Decimal('0'),
            'interest_income': position_summary['passive_income'],
            'interest_income_usd': position_summary['passive_income_usd'],
            'capitalized_interest': position_summary.get('capitalized_income', Decimal('0')),
            'capitalized_interest_usd': position_summary.get('capitalized_income_usd', Decimal('0')),
            'interest_mode': metadata.get('interest_mode', ''),
            'contract_number': metadata.get('contract_number', ''),
            'opened_at': metadata.get('opened_at', ''),
            'auto_renewal': metadata.get('auto_renewal', ''),
        }

    return {
        'product': product,
        'transactions': transactions,
        'transaction_count': len(transactions),
        'latest_rate': currency_rates[0] if currency_rates else None,
        'product_metadata': product.metadata.items() if isinstance(product.metadata, dict) else [],
        'position_summary': position_summary,
        'performance_summary': performance_summary,
        'pension_summary': pension_summary,
        'life_insurance_summary': life_insurance_summary,
        'deposit_summary': deposit_summary,
    }


@require_http_methods(['GET', 'POST'])
def product_detail(request, pk):
    product = get_object_or_404(
        Product.objects.select_related('institution', 'currency', 'income_account', 'income_account__institution'),
        pk=pk,
    )
    terms_form = (
        ProductDepositTermsForm(instance=product)
        if product.product_type == Product.ProductType.DEPOSIT
        else ProductTokenTermsForm(instance=product)
    )
    income_calendar_form = ProductIncomeCalendarForm(product=product)

    if request.method == 'POST':
        action = request.POST.get('action', 'save_terms')

        if action == 'save_income_calendar':
            income_calendar_form = ProductIncomeCalendarForm(request.POST, product=product)
            if income_calendar_form.is_valid():
                payment_amounts = {
                    key.removeprefix('payment_usd_'): value
                    for key, value in request.POST.items()
                    if key.startswith('payment_usd_')
                }
                save_income_calendar_config(
                    product,
                    enabled=income_calendar_form.cleaned_data['enabled'],
                    coupon_day=income_calendar_form.cleaned_data.get('coupon_day'),
                    schedule_start_date=income_calendar_form.cleaned_data.get('schedule_start_date'),
                    payment_amounts=payment_amounts,
                )
                product.refresh_from_db()
                messages.success(request, 'Payment calendar saved.')
            else:
                messages.error(request, 'Could not save payment calendar settings.')
            query = _product_list_nav_params(request)
            url = reverse('products:detail', args=[product.pk])
            return redirect(f'{url}?{urlencode(query)}' if query else url)

        if action == 'recompute_next_income':
            estimated = estimate_next_income_date(product)
            if estimated is None:
                messages.warning(request, 'Could not estimate next income date. Set income schedule and import income history first.')
            else:
                product.next_income_date = estimated
                product.save(update_fields=['next_income_date', 'updated_at'])
                from apps.common.dates import format_display_date

                messages.success(request, f'Next income date set to {format_display_date(estimated)}.')
            query = _product_list_nav_params(request)
            url = reverse('products:detail', args=[product.pk])
            return redirect(f'{url}?{urlencode(query)}' if query else url)

        terms_form = (
            ProductDepositTermsForm(request.POST, instance=product)
            if product.product_type == Product.ProductType.DEPOSIT
            else ProductTokenTermsForm(request.POST, instance=product)
        )
        if terms_form.is_valid():
            if product.product_type == Product.ProductType.DEPOSIT:
                updated_product = terms_form.save(commit=False)
                updated_product.terms_updated_at = timezone.now()
                updated_product.save(update_fields=list(DEPOSIT_TERMS_UPDATE_FIELDS) + ['terms_updated_at'])
            else:
                updated_product = terms_form.save(commit=False)
                updated_product.terms_updated_at = timezone.now()
                updated_product.save(update_fields=list(TOKEN_TERMS_UPDATE_FIELDS))
            messages.success(request, 'Income terms saved.')
            query = _product_list_nav_params(request)
            url = reverse('products:detail', args=[product.pk])
            return redirect(f'{url}?{urlencode(query)}' if query else url)

        messages.error(request, 'Could not save income terms. Please fix the errors below.')

    context = _build_product_detail_context(product)
    context['terms_form'] = terms_form
    context['income_calendar_form'] = income_calendar_form
    context['income_calendar_rows'] = build_income_calendar_rows(product)
    context['income_calendar_events'] = build_product_income_calendar(product)
    context['estimated_next_income_date'] = estimate_next_income_date(product)
    context['income_payment_count'] = len(income_payment_dates(product))
    context.update(_product_navigation(request, product))
    return render(request, 'products/detail.html', context)
