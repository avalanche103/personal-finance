from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from apps.accounts.models import Transaction
from apps.common.models import Currency
from apps.common.services.exchange_rates import recalculate_usd_valuations
from apps.common.services.finstore_operations import is_finstore_redemption_operation
from apps.products.models import Product


def _resolve_currency(currency_code: str) -> Currency:
    currency_names = {
        'BYN': ('Belarusian Ruble', 'Br', Decimal('0.31')),
        'USD': ('US Dollar', '$', Decimal('1')),
        'EUR': ('Euro', 'EUR', Decimal('1.08')),
        'RUB': ('Russian Ruble', 'RUB', Decimal('0.011')),
    }
    normalized_code = (currency_code or 'USD').upper()
    name, symbol, usd_rate = currency_names.get(normalized_code, (normalized_code, normalized_code, Decimal('1')))
    currency, _ = Currency.objects.get_or_create(
        code=normalized_code,
        defaults={
            'name': name,
            'symbol': symbol,
            'usd_rate': usd_rate,
            'metadata': {'imported_from': 'finstore-history'},
        },
    )
    return currency


def reconcile_finstore_products(institution_id: int | None = None, token_names: list[str] | None = None) -> dict:
    transactions_qs = Transaction.objects.filter(metadata__imported_from='finstore-history')
    if institution_id is not None:
        transactions_qs = transactions_qs.filter(account__institution_id=institution_id)
    if token_names:
        transactions_qs = transactions_qs.filter(metadata__token_name__in=token_names)

    transactions = list(
        transactions_qs
        .select_related('account__institution', 'product', 'currency')
        .order_by('occurred_at', 'id')
    )
    token_transactions = [tx for tx in transactions if isinstance(tx.metadata, dict) and tx.metadata.get('token_name')]
    if not token_transactions:
        return {'products_updated': 0, 'transactions_linked': 0}

    linked_transactions = 0
    normalized_transactions = 0
    updated_products = 0
    products_by_key: dict[tuple[int, str], Product] = {}

    with transaction.atomic():
        for tx in token_transactions:
            token_name = tx.metadata.get('token_name', '')
            key = (tx.account.institution_id, token_name)
            product = products_by_key.get(key)
            if product is None:
                product = (
                    Product.objects.filter(institution_id=tx.account.institution_id, external_id=token_name).first()
                    or Product.objects.filter(institution_id=tx.account.institution_id, name=token_name).first()
                )
                if product is None:
                    product = Product.objects.create(
                        institution=tx.account.institution,
                        name=token_name,
                        external_id=token_name,
                        symbol=token_name[:32],
                        product_type=Product.ProductType.TOKEN,
                        currency=_resolve_currency(tx.metadata.get('amount_currency', tx.currency.code if tx.currency_id else 'USD')),
                        metadata={'imported_from': 'finstore-history', 'token_id': tx.metadata.get('token_id', '')},
                    )
                products_by_key[key] = product

            if tx.product_id != product.id:
                tx.product = product
                tx.save(update_fields=['product', 'updated_at'])
                linked_transactions += 1

            operation_type = tx.metadata.get('operation_type', '') if isinstance(tx.metadata, dict) else ''
            if is_finstore_redemption_operation(operation_type):
                update_fields = ['updated_at']
                if (tx.quantity or Decimal('0')) > 0:
                    tx.quantity = -abs(tx.quantity)
                    update_fields.append('quantity')
                if tx.transaction_type != Transaction.TransactionType.INCOME:
                    tx.transaction_type = Transaction.TransactionType.INCOME
                    update_fields.append('transaction_type')
                if len(update_fields) > 1:
                    tx.save(update_fields=update_fields)
                    normalized_transactions += 1

        grouped_transactions: dict[int, list[Transaction]] = {}
        for tx in token_transactions:
            if tx.product_id:
                grouped_transactions.setdefault(tx.product_id, []).append(tx)

        finstore_products = Product.objects.filter(metadata__imported_from='finstore-history')
        if institution_id is not None:
            finstore_products = finstore_products.filter(institution_id=institution_id)
        if token_names:
            finstore_products = finstore_products.filter(external_id__in=token_names)
        finstore_products = finstore_products.select_related('currency', 'institution')
        for product in finstore_products:
            product_transactions = grouped_transactions.get(product.id, [])
            units = sum((tx.quantity or Decimal('0')) for tx in product_transactions)
            latest_price = product.current_price or Decimal('0')
            operation_types = set()
            first_operation_at = ''
            last_operation_at = ''
            latest_price_at = ''

            for tx in product_transactions:
                operation_type = tx.metadata.get('operation_type', '') if isinstance(tx.metadata, dict) else ''
                if operation_type:
                    operation_types.add(operation_type)
                occurred_at = tx.occurred_at.isoformat() if tx.occurred_at else ''
                if occurred_at and (not first_operation_at or occurred_at < first_operation_at):
                    first_operation_at = occurred_at
                if occurred_at and (not last_operation_at or occurred_at > last_operation_at):
                    last_operation_at = occurred_at
                if (tx.quantity or Decimal('0')) > 0 and occurred_at and (not latest_price_at or occurred_at >= latest_price_at):
                    latest_price = tx.unit_price or latest_price
                    latest_price_at = occurred_at

            normalized_units = units if abs(units) > Decimal('0.00000001') else Decimal('0')
            metadata = dict(product.metadata or {})
            metadata.update(
                {
                    'imported_from': 'finstore-history',
                    'history_rows': len(product_transactions),
                    'operation_types': sorted(operation_types),
                    'first_operation_at': first_operation_at,
                    'last_operation_at': last_operation_at,
                    'latest_price_at': latest_price_at,
                }
            )
            product.units = normalized_units
            product.current_price = latest_price
            product.is_active = normalized_units > 0
            product.metadata = metadata
            product.save(update_fields=['units', 'current_price', 'is_active', 'metadata', 'updated_at'])
            updated_products += 1

    recalculate_usd_valuations()
    return {
        'products_updated': updated_products,
        'transactions_linked': linked_transactions,
        'normalized_transactions': normalized_transactions,
    }