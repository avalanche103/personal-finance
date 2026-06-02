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


def build_product_position_summary(transactions, market_value: Decimal):
    bought_units = Decimal('0')
    redeemed_units = Decimal('0')
    purchase_cost = Decimal('0')
    returned_cash = Decimal('0')
    passive_income = Decimal('0')

    for transaction in transactions:
        quantity = transaction.quantity or Decimal('0')
        amount = transaction.amount or Decimal('0')
        if quantity > 0:
            bought_units += quantity
            purchase_cost += abs(amount)
        elif quantity < 0:
            redeemed_units += abs(quantity)
            returned_cash += amount
        elif transaction.transaction_type == Transaction.TransactionType.INCOME:
            passive_income += amount

    avg_entry_price = purchase_cost / bought_units if bought_units else Decimal('0')
    open_units = max(sum((transaction.quantity or Decimal('0')) for transaction in transactions), Decimal('0'))
    open_cost_basis = avg_entry_price * open_units

    return {
        'bought_units': bought_units,
        'redeemed_units': redeemed_units,
        'open_units': open_units,
        'purchase_cost': purchase_cost,
        'returned_cash': returned_cash,
        'passive_income': passive_income,
        'avg_entry_price': avg_entry_price,
        'open_cost_basis': open_cost_basis,
        'market_value': market_value,
        'unrealized_pnl': market_value - open_cost_basis,
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
    purchase_cost = position_summary['purchase_cost']
    total_return_value = position_summary['returned_cash'] + position_summary['passive_income'] + position_summary['market_value'] - purchase_cost
    total_return_pct = (total_return_value / purchase_cost * Decimal('100')) if purchase_cost else None
    cash_flows = [(tx.occurred_at.date(), tx.amount or Decimal('0')) for tx in transactions if tx.amount]
    if position_summary['market_value'] > 0 and as_of_date is not None:
        cash_flows.append((as_of_date, position_summary['market_value']))
    xirr = calculate_xirr(cash_flows)
    return {
        'total_return_value': total_return_value,
        'total_return_pct': total_return_pct,
        'xirr': xirr,
        'xirr_pct': xirr * Decimal('100') if xirr is not None else None,
    }


def build_product_groups(products, transaction_map: dict[int, list[Transaction]] | None = None, as_of_date: date | None = None):
    transaction_map = transaction_map or {}
    grouped = OrderedDict()
    for product in products:
        group_key = (product.institution.name, product.currency.code)
        market_value = product.market_value or Decimal('0')
        product_transactions = transaction_map.get(product.id, [])
        position_summary = build_product_position_summary(product_transactions, market_value)
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
                'returned_cash': Decimal('0'),
                'passive_income': Decimal('0'),
                'total_return_value': Decimal('0'),
                'cash_flows': [],
            }

        grouped[group_key]['products'].append(product)
        grouped[group_key]['total_value_native'] += market_value
        grouped[group_key]['total_value_usd'] += product.current_value_usd or Decimal('0')
        grouped[group_key]['purchase_cost'] += position_summary['purchase_cost']
        grouped[group_key]['returned_cash'] += position_summary['returned_cash']
        grouped[group_key]['passive_income'] += position_summary['passive_income']
        grouped[group_key]['total_return_value'] += performance_summary['total_return_value']
        grouped[group_key]['cash_flows'].extend(
            (transaction.occurred_at.date(), transaction.amount)
            for transaction in product_transactions
            if transaction.amount
        )

    for group in grouped.values():
        group['total_return_pct'] = (
            group['total_return_value'] / group['purchase_cost'] * Decimal('100')
            if group['purchase_cost']
            else None
        )
        group_cash_flows = sorted(group['cash_flows'], key=lambda item: item[0])
        if group['total_value_native'] > 0 and as_of_date is not None:
            group_cash_flows.append((as_of_date, group['total_value_native']))
        group['xirr'] = calculate_xirr(group_cash_flows)
        group['xirr_pct'] = group['xirr'] * Decimal('100') if group['xirr'] is not None else None
        del group['cash_flows']

    return list(grouped.values())