from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import date
from decimal import Decimal
from math import isfinite

from apps.accounts.models import Transaction


def build_product_transaction_map(product_ids: list[int]) -> dict[int, list[Transaction]]:
    transaction_map: dict[int, list[Transaction]] = defaultdict(list)
    if not product_ids:
        return transaction_map

    for transaction in (
        Transaction.objects.filter(product_id__in=product_ids)
        .select_related('account', 'currency')
        .order_by('occurred_at', 'id')
    ):
        transaction_map[transaction.product_id].append(transaction)
    return transaction_map


def _resolve_amount_usd(transaction: Transaction) -> Decimal:
    amount = transaction.amount or Decimal('0')
    amount_usd = transaction.amount_usd or Decimal('0')
    currency = getattr(transaction, 'currency', None)

    if amount_usd or not amount:
        return amount_usd
    if getattr(currency, 'code', '') == 'USD':
        return amount
    if currency is not None and getattr(currency, 'usd_rate', None):
        return amount * currency.usd_rate
    return amount_usd


def _resolve_market_value_usd(position_summary: dict) -> Decimal:
    market_value = position_summary['market_value']
    market_value_usd = position_summary['market_value_usd']

    if market_value_usd or not market_value:
        return market_value_usd
    currency = position_summary.get('currency')
    if getattr(currency, 'code', '') == 'USD':
        return market_value
    if currency is not None and getattr(currency, 'usd_rate', None):
        return market_value * currency.usd_rate
    return market_value_usd


def build_product_position_summary(transactions, market_value: Decimal, market_value_usd: Decimal | None = None, currency=None):
    bought_units = Decimal('0')
    redeemed_units = Decimal('0')
    purchase_cost = Decimal('0')
    returned_cash = Decimal('0')
    passive_income = Decimal('0')
    purchase_cost_usd = Decimal('0')
    returned_cash_usd = Decimal('0')
    passive_income_usd = Decimal('0')

    for transaction in transactions:
        quantity = transaction.quantity or Decimal('0')
        amount = transaction.amount or Decimal('0')
        amount_usd = _resolve_amount_usd(transaction)
        if quantity > 0:
            bought_units += quantity
            purchase_cost += abs(amount)
            purchase_cost_usd += abs(amount_usd)
        elif quantity < 0:
            redeemed_units += abs(quantity)
            returned_cash += amount
            returned_cash_usd += amount_usd
        elif transaction.transaction_type == Transaction.TransactionType.INCOME:
            passive_income += amount
            passive_income_usd += amount_usd

    avg_entry_price = purchase_cost / bought_units if bought_units else Decimal('0')
    open_units = max(sum((transaction.quantity or Decimal('0')) for transaction in transactions), Decimal('0'))
    open_cost_basis = avg_entry_price * open_units
    open_cost_basis_usd = purchase_cost_usd / bought_units * open_units if bought_units else Decimal('0')
    market_value_usd = market_value_usd if market_value_usd is not None else Decimal('0')

    return {
        'bought_units': bought_units,
        'redeemed_units': redeemed_units,
        'open_units': open_units,
        'purchase_cost': purchase_cost,
        'purchase_cost_usd': purchase_cost_usd,
        'returned_cash': returned_cash,
        'returned_cash_usd': returned_cash_usd,
        'passive_income': passive_income,
        'passive_income_usd': passive_income_usd,
        'avg_entry_price': avg_entry_price,
        'open_cost_basis': open_cost_basis,
        'open_cost_basis_usd': open_cost_basis_usd,
        'market_value': market_value,
        'market_value_usd': market_value_usd,
        'currency': currency,
        'unrealized_pnl': market_value - open_cost_basis,
        'unrealized_pnl_usd': market_value_usd - open_cost_basis_usd,
    }


def _xnpv(rate: float, cash_flows: list[tuple[date, Decimal]]) -> float:
    first_date = cash_flows[0][0]
    return sum(
        float(amount) / ((1 + rate) ** (((cash_date - first_date).days) / 365.0))
        for cash_date, amount in cash_flows
    )


def calculate_xirr(cash_flows: list[tuple[date, Decimal]]) -> Decimal | None:
    if len(cash_flows) < 2:
        return None
    positive = any(amount > 0 for _, amount in cash_flows)
    negative = any(amount < 0 for _, amount in cash_flows)
    if not positive or not negative:
        return None

    low = -0.9999
    high = 1.0
    low_value = _xnpv(low, cash_flows)
    high_value = _xnpv(high, cash_flows)

    expansions = 0
    while low_value * high_value > 0 and expansions < 20:
        high *= 2
        high_value = _xnpv(high, cash_flows)
        expansions += 1

    if low_value * high_value > 0:
        return None

    for _ in range(100):
        mid = (low + high) / 2
        mid_value = _xnpv(mid, cash_flows)
        if abs(mid_value) < 1e-7:
            return Decimal(str(mid))
        if low_value * mid_value <= 0:
            high = mid
            high_value = mid_value
        else:
            low = mid
            low_value = mid_value

    result = Decimal(str((low + high) / 2))
    return result if isfinite(float(result)) else None


def build_product_performance_summary(transactions, position_summary, as_of_date: date | None = None):
    purchase_cost_usd = position_summary['purchase_cost_usd']
    market_value_usd = _resolve_market_value_usd(position_summary)
    total_return_value = (
        position_summary['returned_cash_usd']
        + position_summary['passive_income_usd']
        + market_value_usd
        - purchase_cost_usd
    )
    total_return_pct = (total_return_value / purchase_cost_usd * Decimal('100')) if purchase_cost_usd else None
    cash_flows = [
        (tx.occurred_at.date(), _resolve_amount_usd(tx))
        for tx in transactions
        if tx.amount or tx.amount_usd
    ]
    if market_value_usd > 0 and as_of_date is not None:
        cash_flows.append((as_of_date, market_value_usd))
    xirr = calculate_xirr(cash_flows)
    return {
        'total_return_value': total_return_value,
        'total_return_pct': total_return_pct,
        'xirr': xirr,
        'xirr_pct': xirr * Decimal('100') if xirr is not None else None,
    }


PRODUCT_GROUP_SORT_FIELDS = {
    'name': lambda product: (product.name.casefold(), product.pk),
    'institution': lambda product: (product.institution.name.casefold(), product.pk),
    'type': lambda product: (product.product_type, product.name.casefold()),
    'currency': lambda product: (product.currency.code, product.name.casefold()),
    'units': lambda product: (product.units or Decimal('0'), product.name.casefold()),
    'value_usd': lambda product: (product.current_value_usd or Decimal('0'), product.name.casefold()),
    'value_byn': lambda product: (product.market_value or Decimal('0'), product.name.casefold()),
}


def sort_group_products(products, sort_field='value_usd', sort_dir='desc'):
    key_fn = PRODUCT_GROUP_SORT_FIELDS.get(sort_field, PRODUCT_GROUP_SORT_FIELDS['value_usd'])
    return sorted(products, key=key_fn, reverse=(sort_dir == 'desc'))


def build_product_groups(
    products,
    transaction_map: dict[int, list[Transaction]] | None = None,
    as_of_date: date | None = None,
    sort_field: str = 'value_usd',
    sort_dir: str = 'desc',
):
    transaction_map = transaction_map or {}
    grouped = OrderedDict()
    for product in products:
        group_key = (product.institution.name, product.currency.code)
        market_value = product.market_value or Decimal('0')
        market_value_usd = product.current_value_usd or Decimal('0')
        product_transactions = transaction_map.get(product.id, [])
        position_summary = build_product_position_summary(
            product_transactions,
            market_value,
            market_value_usd=market_value_usd,
            currency=product.currency,
        )
        performance_summary = build_product_performance_summary(product_transactions, position_summary, as_of_date=as_of_date)

        if group_key not in grouped:
            grouped[group_key] = {
                'label': f'{product.institution.name}_{product.currency.code}',
                'institution': product.institution,
                'currency': product.currency,
                'products': [],
                'total_value_native': Decimal('0'),
                'total_value_usd': Decimal('0'),
                'purchase_cost': Decimal('0'),
                'purchase_cost_usd': Decimal('0'),
                'returned_cash': Decimal('0'),
                'passive_income': Decimal('0'),
                'total_return_value': Decimal('0'),
                'cash_flows': [],
            }

        grouped[group_key]['products'].append(product)
        grouped[group_key]['total_value_native'] += market_value
        grouped[group_key]['total_value_usd'] += market_value_usd
        grouped[group_key]['purchase_cost'] += position_summary['purchase_cost']
        grouped[group_key]['purchase_cost_usd'] += position_summary['purchase_cost_usd']
        grouped[group_key]['returned_cash'] += position_summary['returned_cash']
        grouped[group_key]['passive_income'] += position_summary['passive_income']
        grouped[group_key]['total_return_value'] += performance_summary['total_return_value']
        grouped[group_key]['cash_flows'].extend(
            (transaction.occurred_at.date(), _resolve_amount_usd(transaction))
            for transaction in product_transactions
            if transaction.amount or transaction.amount_usd
        )

    for group in grouped.values():
        group['total_return_pct'] = (
            group['total_return_value'] / group['purchase_cost_usd'] * Decimal('100')
            if group['purchase_cost_usd']
            else None
        )
        group_cash_flows = sorted(group['cash_flows'], key=lambda item: item[0])
        if group['total_value_usd'] > 0 and as_of_date is not None:
            group_cash_flows.append((as_of_date, group['total_value_usd']))
        group['xirr'] = calculate_xirr(group_cash_flows)
        group['xirr_pct'] = group['xirr'] * Decimal('100') if group['xirr'] is not None else None
        del group['cash_flows']
        group['products'] = sort_group_products(group['products'], sort_field=sort_field, sort_dir=sort_dir)

    return list(grouped.values())