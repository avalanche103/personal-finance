from datetime import date, timedelta
from decimal import Decimal

from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import render
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot
from apps.common.services.exchange_rates import get_usd_conversion_rate
from apps.common.models import ExchangeRateHistory
from apps.imports.models import ImportJob
from apps.institutions.models import FinancialInstitution
from apps.products.analytics import build_product_groups, build_product_transaction_map
from apps.products.models import Product


def _dashboard_metrics():
    account_total = Account.objects.aggregate(
        total=Coalesce(Sum('current_balance_usd'), Value(0), output_field=DecimalField(max_digits=20, decimal_places=2))
    )['total']
    product_total = Product.objects.aggregate(
        total=Coalesce(Sum('current_value_usd'), Value(0), output_field=DecimalField(max_digits=20, decimal_places=2))
    )['total']

    return {
        'institutions_count': FinancialInstitution.objects.count(),
        'accounts_count': Account.objects.count(),
        'products_count': Product.objects.filter(is_active=True).count(),
        'portfolio_usd': account_total + product_total,
    }


def _latest_rate_cards():
    cards = []
    for code in ['USD', 'EUR', 'RUB']:
        history = list(
            ExchangeRateHistory.objects.select_related('currency')
            .filter(currency__code=code)
            .order_by('-rate_date')[:2]
        )
        if not history:
            continue
        latest = history[0]
        previous = history[1] if len(history) > 1 else None
        change_byn = None
        change_pct = None
        display_rate_byn = latest.payload.get('Cur_OfficialRate', latest.rate_byn) if isinstance(latest.payload, dict) else latest.rate_byn
        display_change_byn = None
        if previous and previous.rate_byn:
            change_byn = latest.rate_byn - previous.rate_byn
            change_pct = (change_byn / previous.rate_byn) * Decimal('100')
            previous_display_rate_byn = previous.payload.get('Cur_OfficialRate', previous.rate_byn) if isinstance(previous.payload, dict) else previous.rate_byn
            display_change_byn = Decimal(str(display_rate_byn)) - Decimal(str(previous_display_rate_byn))
        cards.append(
            {
                'code': code,
                'latest': latest,
                'previous': previous,
                'change_byn': change_byn,
                'display_rate_byn': display_rate_byn,
                'display_change_byn': display_change_byn,
                'change_pct': change_pct,
            }
        )
    return cards


def _last_day_of_previous_month(reference_date: date) -> date:
    return reference_date.replace(day=1) - timedelta(days=1)


def _last_day_of_previous_year(reference_date: date) -> date:
    return date(reference_date.year - 1, 12, 31)


def _value_change(current: Decimal, baseline: Decimal) -> dict:
    change_abs = current - baseline
    change_pct = (change_abs / baseline * Decimal('100')) if baseline else None
    return {
        'baseline_usd': baseline,
        'change_abs': change_abs,
        'change_pct': change_pct,
    }


def _build_portfolio_period_comparisons(as_of_date: date, current: dict) -> list[dict]:
    comparisons = []
    for key, label, reference_date in (
        ('prev_month', 'Last day of previous month', _last_day_of_previous_month(as_of_date)),
        ('prev_year', 'Last day of previous year', _last_day_of_previous_year(as_of_date)),
    ):
        baseline = _historical_portfolio_context(reference_date)
        comparisons.append(
            {
                'key': key,
                'label': label,
                'reference_date': reference_date,
                'portfolio': _value_change(current['portfolio_usd'], baseline['portfolio_usd']),
                'accounts': _value_change(current['accounts_total_usd'], baseline['accounts_total_usd']),
                'products': _value_change(current['products_total_usd'], baseline['products_total_usd']),
            }
        )
    return comparisons


def _account_value_as_of(account: Account, as_of_date, rate_cache: dict) -> Decimal:
    snapshot = (
        account.balance_snapshots.filter(captured_at__date__lte=as_of_date)
        .order_by('-captured_at', '-id')
        .first()
    )
    balance = snapshot.balance if snapshot else account.current_balance
    rate = get_usd_conversion_rate(account.currency, as_of_date, rate_cache)
    return balance * rate


def _product_value_as_of(product: Product, as_of_date, rate_cache: dict) -> Decimal:
    snapshot = (
        product.balance_snapshots.filter(captured_at__date__lte=as_of_date)
        .order_by('-captured_at', '-id')
        .first()
    )
    units = snapshot.balance if snapshot else product.units
    rate = get_usd_conversion_rate(product.currency, as_of_date, rate_cache)
    return units * product.current_price * rate


def _historical_portfolio_context(as_of_date):
    rate_cache: dict[tuple[str, str], Decimal] = {}
    institution_rows = []
    account_rows = []
    product_rows = []
    total_accounts_usd = Decimal('0')
    total_products_usd = Decimal('0')

    accounts = list(Account.objects.select_related('institution', 'currency').all())
    products = list(Product.objects.select_related('institution', 'currency').all())

    institution_map: dict[int, dict] = {}
    for account in accounts:
        value_usd = _account_value_as_of(account, as_of_date, rate_cache)
        total_accounts_usd += value_usd
        account_rows.append({'account': account, 'value_usd': value_usd})
        bucket = institution_map.setdefault(
            account.institution_id,
            {'institution': account.institution, 'accounts_usd': Decimal('0'), 'products_usd': Decimal('0')},
        )
        bucket['accounts_usd'] += value_usd

    for product in products:
        value_usd = _product_value_as_of(product, as_of_date, rate_cache)
        total_products_usd += value_usd
        product_rows.append({'product': product, 'value_usd': value_usd})
        bucket = institution_map.setdefault(
            product.institution_id,
            {'institution': product.institution, 'accounts_usd': Decimal('0'), 'products_usd': Decimal('0')},
        )
        bucket['products_usd'] += value_usd

    for bucket in institution_map.values():
        bucket['total_usd'] = bucket['accounts_usd'] + bucket['products_usd']
        institution_rows.append(bucket)

    institution_rows.sort(key=lambda row: row['total_usd'], reverse=True)
    account_rows.sort(key=lambda row: row['value_usd'], reverse=True)
    product_rows.sort(key=lambda row: row['value_usd'], reverse=True)

    latest_snapshot = BalanceSnapshot.objects.filter(captured_at__date__lte=as_of_date).order_by('-captured_at').first()
    return {
        'as_of_date': as_of_date,
        'institution_rows': institution_rows,
        'account_rows': account_rows[:20],
        'product_rows': product_rows[:20],
        'portfolio_usd': total_accounts_usd + total_products_usd,
        'accounts_total_usd': total_accounts_usd,
        'products_total_usd': total_products_usd,
        'latest_snapshot': latest_snapshot,
    }


def dashboard_home(request):
    as_of_date = timezone.localdate()
    historical_report = _historical_portfolio_context(as_of_date)
    products = list(Product.objects.select_related('institution', 'currency').filter(is_active=True).order_by('institution__name', 'currency__code', 'name'))
    product_transaction_map = build_product_transaction_map([product.id for product in products])
    context = {
        'metrics': _dashboard_metrics(),
        'institutions': FinancialInstitution.objects.order_by('name')[:5],
        'accounts': Account.objects.select_related('institution', 'currency').order_by('name')[:8],
        'product_groups': build_product_groups(products, transaction_map=product_transaction_map, as_of_date=as_of_date),
        'recent_imports': ImportJob.objects.select_related('source').order_by('-created_at')[:5],
        'latest_rate_cards': _latest_rate_cards(),
        'historical_reporting': {
            **historical_report,
            'period_comparisons': _build_portfolio_period_comparisons(as_of_date, historical_report),
        },
    }
    return render(request, 'dashboard/index.html', context)


def dashboard_summary(request):
    return render(request, 'dashboard/partials/summary_cards.html', {'metrics': _dashboard_metrics()})


def dashboard_recent_imports(request):
    return render(
        request,
        'dashboard/partials/recent_imports.html',
        {'recent_imports': ImportJob.objects.select_related('source').order_by('-created_at')[:5]},
    )


def dashboard_latest_rates(request):
    return render(
        request,
        'dashboard/partials/latest_rates.html',
        {'latest_rate_cards': _latest_rate_cards()},
    )


def exchange_rate_history(request):
    period = request.GET.get('period', '90d')
    period_map = {
        '30d': 30,
        '90d': 90,
        '365d': 365,
        'all': None,
    }
    days = period_map.get(period, 90)
    rate_history = ExchangeRateHistory.objects.select_related('currency').filter(currency__code__in=['USD', 'EUR', 'RUB'])
    if days is not None:
        start_date = timezone.localdate() - timedelta(days=days)
        rate_history = rate_history.filter(rate_date__gte=start_date)

    rate_history = rate_history.order_by('rate_date', 'currency__code')
    chart_series = {'USD': [], 'EUR': [], 'RUB': []}
    for row in rate_history:
        chart_series[row.currency.code].append({
            'x': row.rate_date.isoformat(),
            'y': float(row.usd_cross_rate),
            'byn': float(row.rate_byn),
        })

    latest_rows = {
        row.currency.code: row
        for row in ExchangeRateHistory.objects.select_related('currency').filter(currency__code__in=['USD', 'EUR', 'RUB']).order_by('currency__code', '-rate_date')
    }
    latest_display_rows = []
    for code in ['USD', 'EUR', 'RUB']:
        if code not in latest_rows:
            continue
        row = latest_rows[code]
        latest_display_rows.append(
            {
                'row': row,
                'display_rate_byn': row.payload.get('Cur_OfficialRate', row.rate_byn) if isinstance(row.payload, dict) else row.rate_byn,
                'display_scale': row.scale,
            }
        )

    context = {
        'period': period,
        'period_options': [('30d', '30 days'), ('90d', '90 days'), ('365d', '365 days'), ('all', 'All')],
        'chart_series': chart_series,
        'rate_rows': rate_history.order_by('-rate_date', 'currency__code')[:180],
        'latest_rows': latest_display_rows,
    }
    template_name = 'dashboard/partials/exchange_rates_content.html' if request.headers.get('HX-Request') == 'true' else 'dashboard/exchange_rates.html'
    return render(request, template_name, context)


def portfolio_report(request):
    raw_date = request.GET.get('as_of')
    try:
        as_of_date = timezone.datetime.fromisoformat(raw_date).date() if raw_date else timezone.localdate()
    except ValueError:
        as_of_date = timezone.localdate()

    context = _historical_portfolio_context(as_of_date)
    context['period_comparisons'] = _build_portfolio_period_comparisons(as_of_date, context)
    return render(request, 'dashboard/portfolio_report.html', context)
