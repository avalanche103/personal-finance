from __future__ import annotations

import re
from collections import OrderedDict, defaultdict
from datetime import date
from decimal import Decimal
from math import isfinite

from apps.accounts.models import Transaction
from apps.common.services.indexed_bonds import resolve_product_market_value_usd
from apps.products.models import Product

TOKEN_ISSUER_PATTERN = re.compile(
    r'^(?P<issuer>.+?)_\((?P<currency>[A-Z]{3})_(?P<issue>\d+)\)$',
    re.I,
)
TOKEN_ISSUER_ALT_PATTERN = re.compile(
    r'^([A-Z][A-Z0-9_]+?)(?:USD|BYN|EUR|RUB)\.\d{4}\.\d+',
    re.I,
)

DEPOSIT_GROUP_PREFIX = '__deposits__'
DEPOSIT_GROUP_LABEL = 'Deposits'


def is_deposit_group_key(group_key: tuple[str, str]) -> bool:
    return group_key[0] == DEPOSIT_GROUP_PREFIX


def product_group_label(institution_name: str, currency_code: str) -> str:
    if institution_name == DEPOSIT_GROUP_PREFIX:
        return DEPOSIT_GROUP_LABEL
    return f'{institution_name}_{currency_code}'


def product_group_key(product) -> tuple[str, str]:
    if product.product_type == Product.ProductType.DEPOSIT:
        return DEPOSIT_GROUP_PREFIX, ''
    return product.institution.name, product.currency.code


def allocation_institution_label(product) -> str:
    if product.product_type == Product.ProductType.DEPOSIT:
        return DEPOSIT_GROUP_LABEL
    return product.institution.name


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


def _to_decimal(value) -> Decimal:
	if value in (None, ''):
		return Decimal('0')
	if isinstance(value, Decimal):
		return value
	return Decimal(str(value).strip().replace(' ', '').replace(',', '.'))


def reconstruct_insurance_product_value_native(
	transactions,
	as_of_date: date,
	*,
	product_type: str,
) -> Decimal:
	value = Decimal('0')
	for transaction in transactions:
		if transaction.occurred_at.date() > as_of_date:
			continue
		amount = transaction.amount or Decimal('0')
		if amount == 0:
			continue
		if transaction.transaction_type == Transaction.TransactionType.DEPOSIT:
			metadata = transaction.metadata if isinstance(transaction.metadata, dict) else {}
			if product_type == Product.ProductType.PENSION:
				employee_share = metadata.get('employee_share_byn')
				if employee_share not in (None, ''):
					value += _to_decimal(employee_share)
				else:
					value += abs(amount) / Decimal('2')
			elif product_type == Product.ProductType.LIFE_INSURANCE:
				net_amount = metadata.get('net_amount')
				value += _to_decimal(net_amount) if net_amount not in (None, '') else abs(amount)
		elif transaction.transaction_type == Transaction.TransactionType.INCOME:
			value += amount
	return value


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


def _is_capitalized_deposit_income(transaction: Transaction) -> bool:
    if transaction.transaction_type != Transaction.TransactionType.INCOME:
        return False
    metadata = transaction.metadata if isinstance(transaction.metadata, dict) else {}
    return str(metadata.get('interest_mode', '')).strip().casefold() == 'capitalized'


def _transaction_quantity_or_amount(transaction: Transaction) -> Decimal:
    quantity = transaction.quantity or Decimal('0')
    if quantity:
        return abs(quantity)
    return abs(transaction.amount or Decimal('0'))


def build_product_position_summary(
    transactions,
    market_value: Decimal,
    market_value_usd: Decimal | None = None,
    currency=None,
    *,
    product_type: str | None = None,
):
    include_trade_fees = product_type == Product.ProductType.BOND
    bought_units = Decimal('0')
    redeemed_units = Decimal('0')
    purchase_cost = Decimal('0')
    returned_cash = Decimal('0')
    passive_income = Decimal('0')
    capitalized_income = Decimal('0')
    purchase_cost_usd = Decimal('0')
    returned_cash_usd = Decimal('0')
    passive_income_usd = Decimal('0')
    capitalized_income_usd = Decimal('0')

    for transaction in transactions:
        quantity = transaction.quantity or Decimal('0')
        amount = transaction.amount or Decimal('0')
        amount_usd = _resolve_amount_usd(transaction)
        if product_type == Product.ProductType.DEPOSIT:
            if transaction.transaction_type == Transaction.TransactionType.DEPOSIT:
                principal = _transaction_quantity_or_amount(transaction)
                bought_units += principal
                purchase_cost += abs(amount)
                purchase_cost_usd += abs(amount_usd)
                continue
            if transaction.transaction_type == Transaction.TransactionType.INCOME:
                income = abs(amount)
                income_usd = abs(amount_usd)
                passive_income += income
                passive_income_usd += income_usd
                if _is_capitalized_deposit_income(transaction):
                    capitalized_income += income
                    capitalized_income_usd += income_usd
                continue
            if transaction.transaction_type in (
                Transaction.TransactionType.WITHDRAWAL,
                Transaction.TransactionType.TRANSFER,
            ) or quantity < 0:
                redeemed_units += abs(quantity) if quantity else abs(amount)
                returned_cash += abs(amount)
                returned_cash_usd += abs(amount_usd)
                continue
        if (
            product_type == Product.ProductType.PENSION
            and transaction.transaction_type == Transaction.TransactionType.DEPOSIT
        ):
            metadata = transaction.metadata if isinstance(transaction.metadata, dict) else {}
            employee_share = metadata.get('employee_share_byn')
            employer_share = metadata.get('employer_share_byn')
            employee_amount = _to_decimal(employee_share) if employee_share not in (None, '') else abs(amount) / Decimal('2')
            employee_amount_usd = employee_amount * (abs(amount_usd) / abs(amount)) if amount else Decimal('0')
            bought_units += abs(amount)
            purchase_cost += employee_amount
            purchase_cost_usd += employee_amount_usd
            continue
        if (
            product_type == Product.ProductType.LIFE_INSURANCE
            and transaction.transaction_type == Transaction.TransactionType.DEPOSIT
        ):
            bought_units += abs(amount)
            purchase_cost += abs(amount)
            purchase_cost_usd += abs(amount_usd)
            continue
        if quantity > 0:
            bought_units += quantity
            purchase_cost += abs(amount)
            purchase_cost_usd += abs(amount_usd)
        elif quantity < 0:
            redeemed_units += abs(quantity)
            returned_cash += amount
            returned_cash_usd += amount_usd
        elif include_trade_fees and transaction.transaction_type == Transaction.TransactionType.FEE:
            purchase_cost += abs(amount)
            purchase_cost_usd += abs(amount_usd)
        elif transaction.transaction_type == Transaction.TransactionType.INCOME:
            if product_type != Product.ProductType.LIFE_INSURANCE:
                passive_income += amount
                passive_income_usd += amount_usd

    avg_entry_price = purchase_cost / bought_units if bought_units else Decimal('0')
    open_units = max(sum((transaction.quantity or Decimal('0')) for transaction in transactions), Decimal('0'))
    open_cost_basis = avg_entry_price * open_units
    open_cost_basis_usd = purchase_cost_usd / bought_units * open_units if bought_units else Decimal('0')
    if product_type == Product.ProductType.DEPOSIT:
        open_cost_basis = max(purchase_cost - returned_cash, Decimal('0'))
        open_cost_basis_usd = max(purchase_cost_usd - returned_cash_usd, Decimal('0'))
    market_value_usd = market_value_usd if market_value_usd is not None else Decimal('0')

    employer_subsidy = Decimal('0')
    employer_subsidy_usd = Decimal('0')
    if product_type == Product.ProductType.PENSION:
        for tx in transactions:
            if tx.transaction_type != Transaction.TransactionType.DEPOSIT:
                continue
            metadata = tx.metadata if isinstance(tx.metadata, dict) else {}
            employer_share = metadata.get('employer_share_byn')
            employer_amount = _to_decimal(employer_share) if employer_share not in (None, '') else (tx.amount or Decimal('0')) / Decimal('2')
            employer_subsidy += employer_amount
            amount = tx.amount or Decimal('0')
            amount_usd = _resolve_amount_usd(tx)
            employer_subsidy_usd += employer_amount * (abs(amount_usd) / abs(amount)) if amount else Decimal('0')

    return {
        'bought_units': bought_units,
        'redeemed_units': redeemed_units,
        'open_units': open_units,
        'purchase_cost': purchase_cost,
        'purchase_cost_usd': purchase_cost_usd,
        'employer_subsidy': employer_subsidy,
        'employer_subsidy_usd': employer_subsidy_usd,
        'returned_cash': returned_cash,
        'returned_cash_usd': returned_cash_usd,
        'passive_income': passive_income,
        'passive_income_usd': passive_income_usd,
        'capitalized_income': capitalized_income,
        'capitalized_income_usd': capitalized_income_usd,
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


def _employee_contribution_usd(transaction: Transaction) -> Decimal:
    amount = transaction.amount or Decimal('0')
    amount_usd = _resolve_amount_usd(transaction)
    metadata = transaction.metadata if isinstance(transaction.metadata, dict) else {}
    employee_share = metadata.get('employee_share_byn')
    employee_amount = _to_decimal(employee_share) if employee_share not in (None, '') else abs(amount) / Decimal('2')
    if not amount:
        return Decimal('0')
    return employee_amount * (abs(amount_usd) / abs(amount))


def build_performance_cash_flows(
    transactions,
    *,
    product_type: str | None = None,
    market_value_usd: Decimal,
    as_of_date: date | None = None,
) -> list[tuple[date, Decimal]]:
    if product_type == Product.ProductType.PENSION:
        cash_flows = [
            (tx.occurred_at.date(), -_employee_contribution_usd(tx))
            for tx in transactions
            if tx.transaction_type == Transaction.TransactionType.DEPOSIT and (tx.amount or tx.amount_usd)
        ]
    elif product_type == Product.ProductType.LIFE_INSURANCE:
        cash_flows = [
            (tx.occurred_at.date(), -abs(_resolve_amount_usd(tx)))
            for tx in transactions
            if tx.transaction_type == Transaction.TransactionType.DEPOSIT and (tx.amount or tx.amount_usd)
        ]
    elif product_type == Product.ProductType.DEPOSIT:
        cash_flows = []
        for tx in transactions:
            amount_usd = _resolve_amount_usd(tx)
            if not amount_usd:
                continue
            if tx.transaction_type == Transaction.TransactionType.DEPOSIT:
                cash_flows.append((tx.occurred_at.date(), -abs(amount_usd)))
            elif tx.transaction_type == Transaction.TransactionType.INCOME:
                if not _is_capitalized_deposit_income(tx):
                    cash_flows.append((tx.occurred_at.date(), abs(amount_usd)))
            elif tx.transaction_type in (
                Transaction.TransactionType.WITHDRAWAL,
                Transaction.TransactionType.TRANSFER,
            ):
                cash_flows.append((tx.occurred_at.date(), abs(amount_usd)))
    else:
        cash_flows = [
            (tx.occurred_at.date(), _resolve_amount_usd(tx))
            for tx in transactions
            if tx.amount or tx.amount_usd
        ]

    if market_value_usd > 0 and as_of_date is not None:
        cash_flows.append((as_of_date, market_value_usd))
    return cash_flows


def build_product_performance_summary(
    transactions,
    position_summary,
    as_of_date: date | None = None,
    *,
    product_type: str | None = None,
):
    purchase_cost_usd = position_summary['purchase_cost_usd']
    market_value_usd = _resolve_market_value_usd(position_summary)
    passive_income_usd = position_summary['passive_income_usd']
    if product_type == Product.ProductType.DEPOSIT:
        passive_income_usd -= position_summary.get('capitalized_income_usd', Decimal('0'))
    total_return_value = (
        position_summary['returned_cash_usd']
        + passive_income_usd
        + market_value_usd
        - purchase_cost_usd
    )
    total_return_pct = (total_return_value / purchase_cost_usd * Decimal('100')) if purchase_cost_usd else None
    cash_flows = build_performance_cash_flows(
        transactions,
        product_type=product_type,
        market_value_usd=market_value_usd,
        as_of_date=as_of_date,
    )
    xirr = calculate_xirr(cash_flows)
    return {
        'total_return_value': total_return_value,
        'total_return_pct': total_return_pct,
        'xirr': xirr,
        'xirr_pct': xirr * Decimal('100') if xirr is not None else None,
    }


ISSUER_ALLOCATION_LIMIT = 10

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
    if sort_field == 'maturity_date':
        with_dates = sorted(
            [product for product in products if product.maturity_date],
            key=lambda product: product.maturity_date,
            reverse=(sort_dir == 'desc'),
        )
        without_dates = sorted(
            [product for product in products if not product.maturity_date],
            key=lambda product: product.name.casefold(),
        )
        return with_dates + without_dates

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
        group_key = product_group_key(product)
        market_value = product.market_value or Decimal('0')
        market_value_usd = resolve_product_market_value_usd(product)
        product_transactions = transaction_map.get(product.id, [])
        position_summary = build_product_position_summary(
            product_transactions,
            market_value,
            market_value_usd=market_value_usd,
            currency=product.currency,
            product_type=product.product_type,
        )
        performance_summary = build_product_performance_summary(
            product_transactions,
            position_summary,
            as_of_date=as_of_date,
            product_type=product.product_type,
        )

        if group_key not in grouped:
            is_deposit_group = is_deposit_group_key(group_key)
            grouped[group_key] = {
                'label': product_group_label(*group_key),
                'institution': None if is_deposit_group else product.institution,
                'currency': product.currency,
                'is_deposit_group': is_deposit_group,
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
            build_performance_cash_flows(
                product_transactions,
                product_type=product.product_type,
                market_value_usd=Decimal('0'),
                as_of_date=None,
            )
        )

    for group in grouped.values():
        currencies = {product.currency.code for product in group['products']}
        group['has_single_currency'] = len(currencies) <= 1
        group['show_byn_column'] = 'BYN' in currencies
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


def normalize_issuer_label(issuer: str) -> str:
    normalized = str(issuer or '').strip()
    if not normalized:
        return normalized
    lowered = normalized.casefold()
    if 'aigenis' in lowered or 'айгенис' in lowered:
        return 'Aigenis'
    if 'stravita' in lowered or 'стравита' in lowered:
        return 'Стравита'
    if 'priorlife' in lowered or 'приорлайф' in lowered:
        return 'Приорлайф'
    return normalized


def extract_product_issuer(product) -> str:
    metadata = product.metadata if isinstance(product.metadata, dict) else {}
    issuer = str(metadata.get('issuer', '') or '').strip()
    if issuer:
        return normalize_issuer_label(issuer)

    if product.product_type == Product.ProductType.PENSION:
        institution_slug = getattr(product.institution, 'slug', '')
        if institution_slug == 'stravita':
            return 'Стравита'
        institution_name = str(getattr(product.institution, 'name', '') or '').strip()
        if institution_name:
            return normalize_issuer_label(institution_name)

    if product.product_type == Product.ProductType.LIFE_INSURANCE:
        institution_slug = getattr(product.institution, 'slug', '')
        if institution_slug == 'priorlife':
            return 'Приорлайф'
        institution_name = str(getattr(product.institution, 'name', '') or '').strip()
        if institution_name:
            return normalize_issuer_label(institution_name)

    if product.product_type == Product.ProductType.DEPOSIT:
        institution_name = str(getattr(product.institution, 'name', '') or '').strip()
        if institution_name:
            return normalize_issuer_label(institution_name)

    if product.product_type == Product.ProductType.CRYPTO:
        asset = str(metadata.get('asset', '') or product.symbol or product.name or '').strip()
        if asset:
            return asset.upper()

    if product.product_type == Product.ProductType.BOND:
        institution_slug = getattr(product.institution, 'slug', '')
        if institution_slug == 'aigenis':
            return 'Aigenis'
        bond_name = str(product.name or '').strip()
        if bond_name.casefold().startswith('айгенис'):
            return 'Aigenis'

    for source in (product.external_id, product.name, product.symbol):
        candidate = str(source or '').strip()
        if not candidate:
            continue
        match = TOKEN_ISSUER_PATTERN.match(candidate)
        if match:
            return match.group('issuer')
        alt_match = TOKEN_ISSUER_ALT_PATTERN.match(candidate)
        if alt_match:
            return alt_match.group(1).rstrip('._')
        return candidate

    return 'Unknown'


def extract_token_issuer(product) -> str:
    return extract_product_issuer(product)


def _allocation_rows(bucket: dict[str, Decimal], total_usd: Decimal) -> list[dict]:
    rows = []
    for label, value_usd in sorted(bucket.items(), key=lambda item: item[1], reverse=True):
        share_pct = value_usd / total_usd * Decimal('100') if total_usd else Decimal('0')
        rows.append(
            {
                'label': label,
                'value_usd': value_usd,
                'share_pct': share_pct,
            }
        )
    return rows


def allocation_instrument_choices(products) -> list[tuple[str, str]]:
    type_labels = dict(Product.ProductType.choices)
    present_types = {product.product_type for product in products}
    return [
        (value, type_labels[value])
        for value, _label in Product.ProductType.choices
        if value in present_types
    ]


def products_for_allocation(products, instrument_type: str | None = None) -> list:
    if not instrument_type:
        return list(products)
    return [product for product in products if product.product_type == instrument_type]


def build_portfolio_allocation(products, *, instrument_type: str | None = None) -> dict:
    scoped_products = products_for_allocation(products, instrument_type)
    by_institution: dict[str, Decimal] = defaultdict(lambda: Decimal('0'))
    by_group: dict[str, Decimal] = defaultdict(lambda: Decimal('0'))
    by_issuer: dict[str, Decimal] = defaultdict(lambda: Decimal('0'))
    total_usd = Decimal('0')

    for product in scoped_products:
        value_usd = product.current_value_usd or Decimal('0')
        total_usd += value_usd
        by_institution[allocation_institution_label(product)] += value_usd
        by_group[product_group_label(*product_group_key(product))] += value_usd
        if product.product_type in (
            Product.ProductType.TOKEN,
            Product.ProductType.BOND,
            Product.ProductType.CRYPTO,
            Product.ProductType.PENSION,
            Product.ProductType.LIFE_INSURANCE,
            Product.ProductType.DEPOSIT,
        ):
            by_issuer[extract_product_issuer(product)] += value_usd

    instrument_label = ''
    if instrument_type:
        instrument_label = dict(Product.ProductType.choices).get(instrument_type, instrument_type)

    return {
        'instrument_type': instrument_type or '',
        'instrument_label': instrument_label,
        'product_count': len(scoped_products),
        'total_usd': total_usd,
        'by_institution': _allocation_rows(by_institution, total_usd),
        'by_group': _allocation_rows(by_group, total_usd),
        'by_issuer': _allocation_rows(by_issuer, total_usd)[:ISSUER_ALLOCATION_LIMIT],
    }