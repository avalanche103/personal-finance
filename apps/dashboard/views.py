from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import render
from django.utils import timezone

from apps.accounts.models import Account, BalanceSnapshot, Transaction
from apps.accounts.analytics import build_dashboard_balance_rows
from apps.accounts.querysets import (
	is_portfolio_holding_account,
	visible_account_queryset,
)
from apps.accounts.services.balance import calculate_account_balance_as_of, transaction_affects_account_balance
from apps.common.dates import format_display_date
from apps.common.services.exchange_rates import get_usd_conversion_rate
from apps.common.models import ExchangeRateHistory
from apps.common.services.ledger import TRANSFER_LEG_METADATA_KEY, TRANSFER_PAIR_METADATA_KEY
from apps.imports.models import ImportJob
from apps.institutions.models import FinancialInstitution
from apps.products.analytics import (
    build_product_groups,
    build_product_transaction_map,
    is_deposit_group_key,
    product_group_key,
    product_group_label,
    reconstruct_insurance_product_value_native,
)
from apps.products.models import Product
from apps.products.operations_calendar import build_operations_calendar

PORTFOLIO_CHART_RANGES = {
    'week': {'label': 'Week', 'span_days': 6, 'step_days': 1, 'granularity': 'daily'},
    'month': {'label': 'Month', 'span_days': 29, 'step_days': 1, 'granularity': 'daily'},
    'year': {'label': 'Year', 'span_days': 364, 'step_days': 7, 'granularity': 'weekly'},
}
DEFAULT_PORTFOLIO_CHART_RANGE = 'month'

PORTFOLIO_CHART_MODES = {
    'value': {'label': 'Total USD'},
    'change': {'label': 'Change %'},
}
DEFAULT_PORTFOLIO_CHART_MODE = 'value'


def _resolve_portfolio_chart_range(range_key: str | None) -> str:
    if range_key in PORTFOLIO_CHART_RANGES:
        return range_key
    return DEFAULT_PORTFOLIO_CHART_RANGE


def _resolve_portfolio_chart_mode(mode_key: str | None) -> str:
    if mode_key in PORTFOLIO_CHART_MODES:
        return mode_key
    return DEFAULT_PORTFOLIO_CHART_MODE


@dataclass
class PortfolioHistoryCache:
    accounts: list[Account]
    products: list[Product]
    transaction_map: dict[int, list[Transaction]]
    account_snapshots: dict[int, list[BalanceSnapshot]]
    product_snapshots: dict[int, list[BalanceSnapshot]]
    exchange_rates_by_code: dict[str, list[ExchangeRateHistory]]

    @classmethod
    def build(cls) -> 'PortfolioHistoryCache':
        accounts = [
            account
            for account in Account.objects.select_related('institution', 'currency').all()
            if is_portfolio_holding_account(account)
        ]
        products = list(Product.objects.select_related('institution', 'currency').all())
        product_ids = [product.id for product in products]
        account_snapshots: dict[int, list[BalanceSnapshot]] = defaultdict(list)
        for snapshot in BalanceSnapshot.objects.filter(account_id__isnull=False).order_by(
            'account_id',
            '-captured_at',
            '-id',
        ):
            account_snapshots[snapshot.account_id].append(snapshot)

        product_snapshots: dict[int, list[BalanceSnapshot]] = defaultdict(list)
        for snapshot in BalanceSnapshot.objects.filter(product_id__isnull=False).order_by(
            'product_id',
            '-captured_at',
            '-id',
        ):
            product_snapshots[snapshot.product_id].append(snapshot)

        exchange_rates_by_code: dict[str, list[ExchangeRateHistory]] = defaultdict(list)
        for row in ExchangeRateHistory.objects.filter(
            source=ExchangeRateHistory.Source.NBRB,
        ).select_related('currency').order_by('currency__code', '-rate_date'):
            exchange_rates_by_code[row.currency.code].append(row)

        return cls(
            accounts=accounts,
            products=products,
            transaction_map=build_product_transaction_map(product_ids),
            account_snapshots=dict(account_snapshots),
            product_snapshots=dict(product_snapshots),
            exchange_rates_by_code=dict(exchange_rates_by_code),
        )


def _snapshot_local_date(snapshot: BalanceSnapshot) -> date:
    return timezone.localtime(snapshot.captured_at).date()


def _entity_metadata(entity) -> dict:
    metadata = entity.metadata
    return metadata if isinstance(metadata, dict) else {}


def _stale_zero_local_date(entity) -> date | None:
    metadata = _entity_metadata(entity)
    if not metadata.get('stale_after_normalization'):
        return None
    balance = getattr(entity, 'current_balance', None)
    if balance is None:
        balance = getattr(entity, 'units', None)
    if balance not in (None, Decimal('0')):
        return None
    explicit = metadata.get('zero_balance_from')
    if explicit:
        return date.fromisoformat(explicit) if isinstance(explicit, str) else explicit
    return timezone.localtime(entity.updated_at).date()


def _latest_balance_snapshot_as_of(
    snapshots: list[BalanceSnapshot],
    as_of_date: date,
) -> BalanceSnapshot | None:
    for snapshot in snapshots:
        if _snapshot_local_date(snapshot) <= as_of_date:
            return snapshot
    return None


def _end_of_local_day(as_of_date: date):
    return timezone.make_aware(
        datetime.combine(as_of_date + timedelta(days=1), time.min),
        timezone.get_current_timezone(),
    )


def _account_transaction_delta_after_snapshot(
    account: Account,
    snapshot: BalanceSnapshot,
    as_of_date: date,
) -> Decimal:
    delta = Decimal('0')
    for transaction in Transaction.objects.filter(
        account=account,
        occurred_at__gt=snapshot.captured_at,
        occurred_at__lt=_end_of_local_day(as_of_date),
    ).only('amount', 'metadata'):
        if transaction_affects_account_balance(transaction):
            delta += transaction.amount or Decimal('0')
    return delta


def _account_balance_from_snapshot_as_of(
    account: Account,
    snapshot: BalanceSnapshot,
    as_of_date: date,
) -> Decimal:
    return snapshot.balance + _account_transaction_delta_after_snapshot(account, snapshot, as_of_date)


def _balance_snapshot_as_of(snapshots: list[BalanceSnapshot], as_of_date: date, fallback: Decimal) -> Decimal:
    snapshot = _latest_balance_snapshot_as_of(snapshots, as_of_date)
    return snapshot.balance if snapshot else fallback


def _account_balance_as_of(
    account: Account,
    as_of_date: date,
    *,
    portfolio_cache: PortfolioHistoryCache | None = None,
) -> Decimal:
    if not is_portfolio_holding_account(account):
        return Decimal('0')

    today = timezone.localdate()
    if as_of_date >= today:
        return account.current_balance or Decimal('0')

    stale_zero_date = _stale_zero_local_date(account)
    if stale_zero_date is not None and as_of_date >= stale_zero_date:
        return Decimal('0')

    if portfolio_cache is not None:
        snapshot = _latest_balance_snapshot_as_of(
            portfolio_cache.account_snapshots.get(account.id, []),
            as_of_date,
        )
        if snapshot:
            return _account_balance_from_snapshot_as_of(account, snapshot, as_of_date)
    else:
        snapshot = (
            account.balance_snapshots.filter(captured_at__date__lte=as_of_date)
            .order_by('-captured_at', '-id')
            .first()
        )
        if snapshot:
            return _account_balance_from_snapshot_as_of(account, snapshot, as_of_date)
    return calculate_account_balance_as_of(account, as_of_date)


def _usd_rate_from_cache(
    currency,
    target_date: date,
    rate_cache: dict[tuple[str, str], Decimal],
    exchange_rates_by_code: dict[str, list[ExchangeRateHistory]],
) -> Decimal:
    cache_key = (currency.code, target_date.isoformat())
    if cache_key in rate_cache:
        return rate_cache[cache_key]

    if currency.code == 'USD':
        rate_cache[cache_key] = Decimal('1')
        return rate_cache[cache_key]

    if currency.code == 'BYN':
        for row in exchange_rates_by_code.get('USD', []):
            if row.rate_date <= target_date and row.rate_byn:
                rate_cache[cache_key] = Decimal('1') / row.rate_byn
                return rate_cache[cache_key]

    for row in exchange_rates_by_code.get(currency.code, []):
        if row.rate_date <= target_date:
            rate_cache[cache_key] = row.usd_cross_rate
            return rate_cache[cache_key]

    default_rate = currency.usd_rate if currency.usd_rate else Decimal('0')
    rate_cache[cache_key] = default_rate
    return default_rate


def _portfolio_chart_points(
    as_of_date: date,
    range_key: str = DEFAULT_PORTFOLIO_CHART_RANGE,
    *,
    portfolio_cache: PortfolioHistoryCache | None = None,
) -> list[dict]:
    config = PORTFOLIO_CHART_RANGES[_resolve_portfolio_chart_range(range_key)]
    span_days = config['span_days']
    step_days = config['step_days']
    point_dates = [as_of_date - timedelta(days=offset) for offset in range(span_days, -1, -step_days)]
    if point_dates[-1] != as_of_date:
        point_dates.append(as_of_date)

    portfolio_cache = portfolio_cache or PortfolioHistoryCache.build()
    points = []
    for point_date in point_dates:
        snapshot = _historical_portfolio_context(point_date, portfolio_cache=portfolio_cache)
        points.append({'date': point_date, 'value': float(snapshot['portfolio_usd'])})
    return points


def _portfolio_chart_payload(points: list[dict], range_key: str, mode_key: str) -> dict:
    config = PORTFOLIO_CHART_RANGES[_resolve_portfolio_chart_range(range_key)]
    values = [point['value'] for point in points]
    baseline = values[0] if values else 0.0
    change_pct = []
    change_usd = []
    for value in values:
        if baseline:
            change_pct.append(round((value / baseline - 1) * 100, 4))
            change_usd.append(round(value - baseline, 2))
        else:
            change_pct.append(0.0)
            change_usd.append(0.0)

    return {
        'range': range_key,
        'mode': _resolve_portfolio_chart_mode(mode_key),
        'granularity': config['granularity'],
        'dates': [format_display_date(point['date']) for point in points],
        'values': values,
        'change_pct': change_pct,
        'change_usd': change_usd,
        'baseline_usd': baseline,
        'period_change_pct': change_pct[-1] if change_pct else 0.0,
        'period_change_usd': change_usd[-1] if change_usd else 0.0,
    }


def _portfolio_chart_context(
    range_key: str | None = None,
    mode_key: str | None = None,
    as_of_date: date | None = None,
    *,
    portfolio_cache: PortfolioHistoryCache | None = None,
) -> dict:
    as_of_date = as_of_date or timezone.localdate()
    chart_range = _resolve_portfolio_chart_range(range_key)
    chart_mode = _resolve_portfolio_chart_mode(mode_key)
    chart_points = _portfolio_chart_points(as_of_date, chart_range, portfolio_cache=portfolio_cache)
    config = PORTFOLIO_CHART_RANGES[chart_range]
    return {
        'chart_range': chart_range,
        'chart_mode': chart_mode,
        'chart_range_options': [(key, value['label']) for key, value in PORTFOLIO_CHART_RANGES.items()],
        'chart_mode_options': [(key, value['label']) for key, value in PORTFOLIO_CHART_MODES.items()],
        'chart_granularity_label': config['granularity'],
        'portfolio_chart_points': chart_points,
        'portfolio_chart_json': _portfolio_chart_payload(chart_points, chart_range, chart_mode),
    }


@dataclass(frozen=True)
class CashFlowTotals:
	month_deposits_usd: Decimal
	month_withdrawals_usd: Decimal
	year_deposits_usd: Decimal
	year_withdrawals_usd: Decimal
	as_of_date: date


def _is_internal_transfer_leg(transaction: Transaction) -> bool:
	metadata = transaction.metadata if isinstance(transaction.metadata, dict) else {}
	return bool(metadata.get(TRANSFER_PAIR_METADATA_KEY) and metadata.get(TRANSFER_LEG_METADATA_KEY))


def _transaction_flow_usd(transaction: Transaction, rate_cache: dict) -> Decimal:
	amount_usd = transaction.amount_usd or Decimal('0')
	if amount_usd:
		return amount_usd
	amount = transaction.amount or Decimal('0')
	if not amount:
		return Decimal('0')
	tx_date = timezone.localtime(transaction.occurred_at).date()
	rate = get_usd_conversion_rate(transaction.currency, tx_date, rate_cache)
	return amount * rate


def _transaction_cash_flow_usd(transaction: Transaction, rate_cache: dict) -> tuple[Decimal, Decimal]:
	"""Return (deposits_usd, withdrawals_usd) as positive magnitudes."""
	if _is_internal_transfer_leg(transaction):
		return Decimal('0'), Decimal('0')

	amount = transaction.amount or Decimal('0')
	usd = _transaction_flow_usd(transaction, rate_cache)
	tx_type = transaction.transaction_type

	if tx_type == Transaction.TransactionType.DEPOSIT and amount > 0:
		return abs(usd), Decimal('0')
	if tx_type == Transaction.TransactionType.WITHDRAWAL and amount < 0:
		return Decimal('0'), abs(usd)
	if tx_type == Transaction.TransactionType.TRANSFER:
		if amount > 0:
			return abs(usd), Decimal('0')
		if amount < 0:
			return Decimal('0'), abs(usd)
	return Decimal('0'), Decimal('0')


def _build_deposit_withdrawal_totals(as_of_date: date | None = None) -> CashFlowTotals:
	as_of_date = as_of_date or timezone.localdate()
	month_start = timezone.make_aware(datetime.combine(as_of_date.replace(day=1), time.min))
	year_start = timezone.make_aware(datetime.combine(date(as_of_date.year, 1, 1), time.min))
	end_of_day = timezone.make_aware(datetime.combine(as_of_date + timedelta(days=1), time.min))

	month_deposits = Decimal('0')
	month_withdrawals = Decimal('0')
	year_deposits = Decimal('0')
	year_withdrawals = Decimal('0')
	rate_cache: dict = {}

	transactions = Transaction.objects.filter(
		occurred_at__gte=year_start,
		occurred_at__lt=end_of_day,
		transaction_type__in=[
			Transaction.TransactionType.DEPOSIT,
			Transaction.TransactionType.WITHDRAWAL,
			Transaction.TransactionType.TRANSFER,
		],
	).select_related('currency')

	for ledger_transaction in transactions:
		deposits_usd, withdrawals_usd = _transaction_cash_flow_usd(ledger_transaction, rate_cache)
		year_deposits += deposits_usd
		year_withdrawals += withdrawals_usd
		if ledger_transaction.occurred_at >= month_start:
			month_deposits += deposits_usd
			month_withdrawals += withdrawals_usd

	return CashFlowTotals(
		month_deposits_usd=month_deposits,
		month_withdrawals_usd=month_withdrawals,
		year_deposits_usd=year_deposits,
		year_withdrawals_usd=year_withdrawals,
		as_of_date=as_of_date,
	)


def _dashboard_metrics():
    account_total = Account.objects.aggregate(
        total=Coalesce(Sum('current_balance_usd'), Value(0), output_field=DecimalField(max_digits=20, decimal_places=2))
    )['total']
    product_total = Product.objects.aggregate(
        total=Coalesce(Sum('current_value_usd'), Value(0), output_field=DecimalField(max_digits=20, decimal_places=2))
    )['total']
    cash_flows = _build_deposit_withdrawal_totals()

    return {
        'institutions_count': FinancialInstitution.objects.count(),
        'accounts_count': Account.objects.count(),
        'products_count': Product.objects.filter(is_active=True).count(),
        'portfolio_usd': account_total + product_total,
        'cash_flows': cash_flows,
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


def _previous_day(reference_date: date) -> date:
    return reference_date - timedelta(days=1)


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


def _portfolio_account_group_values(
    as_of_date,
    *,
    portfolio_cache: PortfolioHistoryCache,
    rate_cache: dict | None = None,
) -> dict[int, dict]:
    rate_cache = rate_cache if rate_cache is not None else {}
    grouped: OrderedDict[int, dict] = OrderedDict()
    for account in portfolio_cache.accounts:
        institution = account.institution
        value_usd = _account_value_as_of(account, as_of_date, rate_cache, portfolio_cache=portfolio_cache)
        if institution.id not in grouped:
            grouped[institution.id] = {
                'label': institution.name,
                'institution': institution,
                'value_usd': Decimal('0'),
            }
        grouped[institution.id]['value_usd'] += value_usd
    return grouped


def _portfolio_product_group_values(
    as_of_date,
    *,
    portfolio_cache: PortfolioHistoryCache,
    rate_cache: dict | None = None,
) -> dict[tuple[str, str], dict]:
    rate_cache = rate_cache if rate_cache is not None else {}
    grouped: OrderedDict[tuple[str, str], dict] = OrderedDict()
    for product in portfolio_cache.products:
        group_key = product_group_key(product)
        value_usd = _product_value_as_of(
            product,
            as_of_date,
            rate_cache,
            transaction_map=portfolio_cache.transaction_map,
            portfolio_cache=portfolio_cache,
        )
        if group_key not in grouped:
            is_deposit_group = is_deposit_group_key(group_key)
            grouped[group_key] = {
                'label': product_group_label(*group_key),
                'institution': None if is_deposit_group else product.institution,
                'currency': product.currency,
                'is_deposit_group': is_deposit_group,
                'value_usd': Decimal('0'),
            }
        grouped[group_key]['value_usd'] += value_usd
    return grouped


def _deposit_linked_account_ids(products: list[Product]) -> set[int]:
    return {
        product.income_account_id
        for product in products
        if product.product_type == Product.ProductType.DEPOSIT and product.income_account_id
    }


def _comparison_group_key_for_institution(institution) -> tuple[str, str]:
    slug = institution.slug
    if slug == 'finstore':
        return 'finstore', 'Finstore'
    if slug in {'binance', 'bynex'}:
        return 'crypto', 'Binance + BYNEX'
    if slug == 'aigenis':
        return 'aigenis', 'Aigenis'
    if slug == 'priorlife':
        return 'priorlife', 'Priorlife'
    if slug == 'stravita':
        return 'stravita', 'Stravita'
    return f'institution:{institution.id}', institution.name


def _comparison_group_row_defaults(group_key: str, label: str, institution=None) -> dict:
    custom_groups = {
        'deposits': {'initials': 'DP', 'accent': '#0D9488'},
        'crypto': {'initials': 'CR', 'accent': '#F59E0B'},
    }
    custom = custom_groups.get(group_key, {})
    return {
        'label': label,
        'institution': institution,
        'currency': None,
        'group_initials': custom.get('initials', ''),
        'group_accent': custom.get('accent', '#64748B'),
        'is_deposit_group': group_key == 'deposits',
        'value_usd': Decimal('0'),
    }


def _portfolio_comparison_group_values(
    as_of_date,
    *,
    portfolio_cache: PortfolioHistoryCache,
    rate_cache: dict | None = None,
) -> OrderedDict[str, dict]:
    rate_cache = rate_cache if rate_cache is not None else {}
    grouped: OrderedDict[str, dict] = OrderedDict()
    deposit_account_ids = _deposit_linked_account_ids(portfolio_cache.products)

    for account in portfolio_cache.accounts:
        if account.id in deposit_account_ids:
            group_key, label = 'deposits', 'Deposits + bank accounts'
            institution = None
        else:
            group_key, label = _comparison_group_key_for_institution(account.institution)
            institution = None if group_key == 'crypto' else account.institution
        if group_key not in grouped:
            grouped[group_key] = _comparison_group_row_defaults(group_key, label, institution=institution)
        grouped[group_key]['value_usd'] += _account_value_as_of(
            account,
            as_of_date,
            rate_cache,
            portfolio_cache=portfolio_cache,
        )

    for product in portfolio_cache.products:
        if product.product_type == Product.ProductType.DEPOSIT:
            group_key, label = 'deposits', 'Deposits + bank accounts'
            institution = None
        else:
            group_key, label = _comparison_group_key_for_institution(product.institution)
            institution = None if group_key == 'crypto' else product.institution
        if group_key not in grouped:
            grouped[group_key] = _comparison_group_row_defaults(group_key, label, institution=institution)
        grouped[group_key]['value_usd'] += _product_value_as_of(
            product,
            as_of_date,
            rate_cache,
            transaction_map=portfolio_cache.transaction_map,
            portfolio_cache=portfolio_cache,
        )

    return grouped


def _comparison_breakdown_rows(
    current_groups: dict,
    baseline_groups: dict,
) -> list[dict]:
    keys = set(current_groups) | set(baseline_groups)
    rows = []
    for key in keys:
        current_row = current_groups.get(key, {})
        baseline_row = baseline_groups.get(key, {})
        current_usd = current_row.get('value_usd', Decimal('0'))
        baseline_usd = baseline_row.get('value_usd', Decimal('0'))
        if current_usd == 0 and baseline_usd == 0:
            continue
        meta = current_row or baseline_row
        rows.append(
            {
                **meta,
                'current_usd': current_usd,
                'change': _value_change(current_usd, baseline_usd),
            }
        )
    rows.sort(
        key=lambda row: (abs(row['change']['change_abs']), row['current_usd']),
        reverse=True,
    )
    return rows


def _build_portfolio_period_comparisons(
    as_of_date: date,
    current: dict,
    *,
    portfolio_cache: PortfolioHistoryCache | None = None,
) -> list[dict]:
    portfolio_cache = portfolio_cache or PortfolioHistoryCache.build()
    current_rate_cache: dict[tuple[str, str], Decimal] = {}
    current_account_groups = _portfolio_account_group_values(
        as_of_date,
        portfolio_cache=portfolio_cache,
        rate_cache=current_rate_cache,
    )
    current_product_groups = _portfolio_product_group_values(
        as_of_date,
        portfolio_cache=portfolio_cache,
        rate_cache=current_rate_cache,
    )
    current_comparison_groups = _portfolio_comparison_group_values(
        as_of_date,
        portfolio_cache=portfolio_cache,
        rate_cache=current_rate_cache,
    )
    comparisons = []
    for key, label, reference_date in (
        ('prev_day', 'Previous calendar day', _previous_day(as_of_date)),
        ('prev_month', 'Previous month end', _last_day_of_previous_month(as_of_date)),
        ('prev_year', 'Previous year end', _last_day_of_previous_year(as_of_date)),
    ):
        baseline = _historical_portfolio_context(reference_date, portfolio_cache=portfolio_cache)
        baseline_rate_cache: dict[tuple[str, str], Decimal] = {}
        baseline_account_groups = _portfolio_account_group_values(
            reference_date,
            portfolio_cache=portfolio_cache,
            rate_cache=baseline_rate_cache,
        )
        baseline_product_groups = _portfolio_product_group_values(
            reference_date,
            portfolio_cache=portfolio_cache,
            rate_cache=baseline_rate_cache,
        )
        baseline_comparison_groups = _portfolio_comparison_group_values(
            reference_date,
            portfolio_cache=portfolio_cache,
            rate_cache=baseline_rate_cache,
        )
        comparisons.append(
            {
                'key': key,
                'label': label,
                'reference_date': reference_date,
                'portfolio': _value_change(current['portfolio_usd'], baseline['portfolio_usd']),
                'accounts': _value_change(current['accounts_total_usd'], baseline['accounts_total_usd']),
                'products': _value_change(current['products_total_usd'], baseline['products_total_usd']),
                'breakdown_groups': _comparison_breakdown_rows(current_comparison_groups, baseline_comparison_groups),
                'breakdown_products': _comparison_breakdown_rows(current_product_groups, baseline_product_groups),
                'breakdown_accounts': _comparison_breakdown_rows(current_account_groups, baseline_account_groups),
            }
        )
    return comparisons



def _account_value_as_of(
    account: Account,
    as_of_date,
    rate_cache: dict,
    *,
    portfolio_cache: PortfolioHistoryCache | None = None,
) -> Decimal:
    if not is_portfolio_holding_account(account):
        return Decimal('0')
    balance = _account_balance_as_of(account, as_of_date, portfolio_cache=portfolio_cache)
    if portfolio_cache is not None:
        rate = _usd_rate_from_cache(
            account.currency,
            as_of_date,
            rate_cache,
            portfolio_cache.exchange_rates_by_code,
        )
    else:
        rate = get_usd_conversion_rate(account.currency, as_of_date, rate_cache)
    return balance * rate


def _product_market_value_native(product: Product, *, units: Decimal) -> Decimal:
    if product.product_type in (Product.ProductType.PENSION, Product.ProductType.LIFE_INSURANCE):
        return units
    return units * (product.current_price or Decimal('0'))


def _latest_product_snapshot_as_of(
    product: Product,
    as_of_date: date,
    *,
    portfolio_cache: PortfolioHistoryCache | None = None,
) -> BalanceSnapshot | None:
    if portfolio_cache is not None:
        return _latest_balance_snapshot_as_of(
            portfolio_cache.product_snapshots.get(product.id, []),
            as_of_date,
        )
    return (
        product.balance_snapshots.filter(captured_at__date__lte=as_of_date)
        .order_by('-captured_at', '-id')
        .first()
    )


def _stored_market_snapshot_value_usd(snapshot: BalanceSnapshot | None) -> Decimal | None:
    if snapshot is None:
        return None
    metadata = snapshot.metadata if isinstance(snapshot.metadata, dict) else {}
    if metadata.get('source') == 'binance':
        return snapshot.balance_usd or Decimal('0')
    return None


def _product_units_from_transactions_as_of(
    product: Product,
    as_of_date,
    transaction_map: dict[int, list[Transaction]] | None,
) -> Decimal | None:
    if transaction_map is None or product.id not in transaction_map:
        return None
    return sum(
        (transaction.quantity or Decimal('0'))
        for transaction in transaction_map.get(product.id, [])
        if timezone.localtime(transaction.occurred_at).date() <= as_of_date
    )


def _product_native_units_as_of(
    product: Product,
    as_of_date: date,
    *,
    transaction_map: dict[int, list[Transaction]] | None = None,
    portfolio_cache: PortfolioHistoryCache | None = None,
) -> Decimal:
    today = timezone.localdate()
    if as_of_date >= today:
        return product.units or Decimal('0')

    stale_zero_date = _stale_zero_local_date(product)
    if stale_zero_date is not None and as_of_date >= stale_zero_date:
        return Decimal('0')

    effective_transaction_map = transaction_map or (portfolio_cache.transaction_map if portfolio_cache else None)
    snapshot = _latest_product_snapshot_as_of(product, as_of_date, portfolio_cache=portfolio_cache)
    if snapshot:
        return snapshot.balance
    units_from_transactions = _product_units_from_transactions_as_of(product, as_of_date, effective_transaction_map)
    if units_from_transactions is not None:
        return units_from_transactions
    if timezone.localtime(product.created_at).date() > as_of_date:
        return Decimal('0')
    return product.units or Decimal('0')


def _product_value_as_of(
    product: Product,
    as_of_date,
    rate_cache: dict,
    *,
    transaction_map: dict[int, list[Transaction]] | None = None,
    portfolio_cache: PortfolioHistoryCache | None = None,
) -> Decimal:
    if portfolio_cache is not None:
        rate = _usd_rate_from_cache(
            product.currency,
            as_of_date,
            rate_cache,
            portfolio_cache.exchange_rates_by_code,
        )
    else:
        rate = get_usd_conversion_rate(product.currency, as_of_date, rate_cache)
    today = timezone.localdate()
    if as_of_date < today:
        snapshot = _latest_product_snapshot_as_of(product, as_of_date, portfolio_cache=portfolio_cache)
        stored_market_value_usd = _stored_market_snapshot_value_usd(snapshot)
        if stored_market_value_usd is not None:
            return stored_market_value_usd
    if product.product_type in (Product.ProductType.PENSION, Product.ProductType.LIFE_INSURANCE):
        if as_of_date >= today:
            return product.current_value_usd or Decimal('0')
        stale_zero_date = _stale_zero_local_date(product)
        if stale_zero_date is not None and as_of_date >= stale_zero_date:
            native_value = Decimal('0')
        elif portfolio_cache is not None:
            native_value = _balance_snapshot_as_of(
                portfolio_cache.product_snapshots.get(product.id, []),
                as_of_date,
                reconstruct_insurance_product_value_native(
                    (transaction_map or portfolio_cache.transaction_map).get(product.id, []),
                    as_of_date,
                    product_type=product.product_type,
                ),
            )
        else:
            snapshot = (
                product.balance_snapshots.filter(captured_at__date__lte=as_of_date)
                .order_by('-captured_at', '-id')
                .first()
            )
            transactions = (transaction_map or {}).get(product.id, [])
            if snapshot:
                native_value = snapshot.balance
            else:
                native_value = reconstruct_insurance_product_value_native(
                    transactions,
                    as_of_date,
                    product_type=product.product_type,
                )
        return native_value * rate

    units = _product_native_units_as_of(
        product,
        as_of_date,
        transaction_map=transaction_map,
        portfolio_cache=portfolio_cache,
    )
    return _product_market_value_native(product, units=units) * rate


def _historical_portfolio_context(
    as_of_date,
    *,
    portfolio_cache: PortfolioHistoryCache | None = None,
):
    portfolio_cache = portfolio_cache or PortfolioHistoryCache.build()
    rate_cache: dict[tuple[str, str], Decimal] = {}
    institution_rows = []
    account_rows = []
    product_rows = []
    total_accounts_usd = Decimal('0')
    total_products_usd = Decimal('0')

    institution_map: dict[int, dict] = {}
    for account in portfolio_cache.accounts:
        value_usd = _account_value_as_of(account, as_of_date, rate_cache, portfolio_cache=portfolio_cache)
        total_accounts_usd += value_usd
        account_rows.append({'account': account, 'value_usd': value_usd})
        bucket = institution_map.setdefault(
            account.institution_id,
            {'institution': account.institution, 'accounts_usd': Decimal('0'), 'products_usd': Decimal('0')},
        )
        bucket['accounts_usd'] += value_usd

    for product in portfolio_cache.products:
        value_usd = _product_value_as_of(
            product,
            as_of_date,
            rate_cache,
            transaction_map=portfolio_cache.transaction_map,
            portfolio_cache=portfolio_cache,
        )
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

    latest_snapshot = None
    for snapshots in portfolio_cache.account_snapshots.values():
        for snapshot in snapshots:
            if _snapshot_local_date(snapshot) <= as_of_date and (
                latest_snapshot is None or snapshot.captured_at > latest_snapshot.captured_at
            ):
                latest_snapshot = snapshot
    for snapshots in portfolio_cache.product_snapshots.values():
        for snapshot in snapshots:
            if _snapshot_local_date(snapshot) <= as_of_date and (
                latest_snapshot is None or snapshot.captured_at > latest_snapshot.captured_at
            ):
                latest_snapshot = snapshot
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
    portfolio_cache = PortfolioHistoryCache.build()
    historical_report = _historical_portfolio_context(as_of_date, portfolio_cache=portfolio_cache)
    products = list(Product.objects.select_related('institution', 'currency').filter(is_active=True).order_by('institution__name', 'currency__code', 'name'))
    product_transaction_map = build_product_transaction_map([product.id for product in products])
    metrics = _dashboard_metrics()
    context = {
        'metrics': metrics,
        **_portfolio_chart_context(
            request.GET.get('range'),
            request.GET.get('mode'),
            as_of_date,
            portfolio_cache=portfolio_cache,
        ),
        'institutions': FinancialInstitution.objects.order_by('name')[:5],
        'balance_rows': build_dashboard_balance_rows(
            visible_account_queryset().order_by('-current_balance_usd', 'name'),
        ),
        'product_groups': build_product_groups(products, transaction_map=product_transaction_map, as_of_date=as_of_date),
        'recent_imports': ImportJob.objects.select_related('source').order_by('-created_at')[:5],
        'recent_transactions': Transaction.objects.select_related('account', 'currency', 'product').order_by('-occurred_at')[:12],
        'operations_calendar': build_operations_calendar(products, today=as_of_date),
        'latest_rate_cards': _latest_rate_cards(),
        'historical_reporting': {
            **historical_report,
            'period_comparisons': _build_portfolio_period_comparisons(
                as_of_date,
                historical_report,
                portfolio_cache=portfolio_cache,
            ),
        },
    }
    return render(request, 'dashboard/index.html', context)


def dashboard_portfolio_chart(request):
    return render(
        request,
        'dashboard/partials/portfolio_chart.html',
        _portfolio_chart_context(request.GET.get('range'), request.GET.get('mode')),
    )


def dashboard_summary(request):
    return render(
        request,
        'dashboard/partials/summary_row.html',
        {'metrics': _dashboard_metrics()},
    )


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
            'x': format_display_date(row.rate_date),
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
        from apps.common.dates import parse_display_date

        as_of_date = parse_display_date(raw_date) if raw_date else timezone.localdate()
        if raw_date and as_of_date is None:
            as_of_date = timezone.localdate()
    except ValueError:
        as_of_date = timezone.localdate()

    portfolio_cache = PortfolioHistoryCache.build()
    context = _historical_portfolio_context(as_of_date, portfolio_cache=portfolio_cache)
    context['period_comparisons'] = _build_portfolio_period_comparisons(
        as_of_date,
        context,
        portfolio_cache=portfolio_cache,
    )
    return render(request, 'dashboard/portfolio_report.html', context)
