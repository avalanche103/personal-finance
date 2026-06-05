from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from apps.accounts.models import Transaction
from apps.common.models import Currency
from apps.common.services.aigenis_bonds import apply_aigenis_indexed_bond_defaults
from apps.common.services.exchange_rates import recalculate_usd_valuations
from apps.products.models import Product


def canonical_aigenis_security_name(*names: str) -> str:
	candidates = [name.strip() for name in names if name and str(name).strip()]
	if not candidates:
		return ''
	preferred = [name for name in candidates if not name.startswith('Размещение -')]
	pool = preferred or candidates
	return min(pool, key=lambda name: (len(name), name))


def _resolve_currency(currency_code: str) -> Currency:
	currency_names = {
		'BYN': ('Belarusian Ruble', 'Br', Decimal('0.31')),
		'USD': ('US Dollar', '$', Decimal('1')),
		'EUR': ('Euro', 'EUR', Decimal('1.08')),
		'RUB': ('Russian Ruble', 'RUB', Decimal('0.011')),
	}
	normalized_code = (currency_code or 'BYN').upper()
	name, symbol, usd_rate = currency_names.get(normalized_code, (normalized_code, normalized_code, Decimal('1')))
	currency, _ = Currency.objects.get_or_create(
		code=normalized_code,
		defaults={
			'name': name,
			'symbol': symbol,
			'usd_rate': usd_rate,
			'metadata': {'imported_from': 'aigenis-report'},
		},
	)
	return currency


def reconcile_aigenis_products(institution_id: int | None = None, isins: list[str] | None = None) -> dict:
	transactions_qs = Transaction.objects.filter(metadata__imported_from='aigenis-report')
	if institution_id is not None:
		transactions_qs = transactions_qs.filter(account__institution_id=institution_id)
	if isins:
		transactions_qs = transactions_qs.filter(metadata__isin__in=isins)

	transactions = list(
		transactions_qs
		.select_related('account__institution', 'product', 'currency')
		.order_by('occurred_at', 'id')
	)
	security_transactions = [tx for tx in transactions if isinstance(tx.metadata, dict) and tx.metadata.get('isin')]
	if not security_transactions:
		return {'products_updated': 0, 'transactions_linked': 0}

	linked_transactions = 0
	updated_products = 0
	products_by_key: dict[tuple[int, str], Product] = {}

	with transaction.atomic():
		for tx in security_transactions:
			isin = tx.metadata.get('isin', '')
			key = (tx.account.institution_id, isin)
			product = products_by_key.get(key)
			if product is None:
				product = (
					Product.objects.filter(institution_id=tx.account.institution_id, external_id=isin).first()
					or Product.objects.filter(institution_id=tx.account.institution_id, isin=isin).first()
				)
				if product is None:
					security_name = tx.metadata.get('security_name', isin)
					product = Product.objects.create(
						institution=tx.account.institution,
						name=security_name,
						external_id=isin,
						isin=isin,
						symbol=isin[:32],
						product_type=Product.ProductType.BOND,
						currency=_resolve_currency(tx.metadata.get('amount_currency', tx.currency.code if tx.currency_id else 'BYN')),
						metadata={
							'imported_from': 'aigenis-report',
							'issuer': tx.metadata.get('issuer', ''),
							'security_type': tx.metadata.get('security_type', ''),
						},
					)
				products_by_key[key] = product

			if tx.product_id != product.id:
				tx.product = product
				tx.save(update_fields=['product', 'updated_at'])
				linked_transactions += 1

		grouped_transactions: dict[int, list[Transaction]] = {}
		for tx in security_transactions:
			if tx.product_id:
				grouped_transactions.setdefault(tx.product_id, []).append(tx)

		aigenis_products = Product.objects.filter(metadata__imported_from='aigenis-report')
		if institution_id is not None:
			aigenis_products = aigenis_products.filter(institution_id=institution_id)
		if isins:
			aigenis_products = aigenis_products.filter(external_id__in=isins)
		aigenis_products = aigenis_products.select_related('currency', 'institution')
		for product in aigenis_products:
			product_transactions = grouped_transactions.get(product.id, [])
			units = sum((tx.quantity or Decimal('0')) for tx in product_transactions)
			latest_price = product.current_price or Decimal('0')
			operation_types = set()
			first_operation_at = ''
			last_operation_at = ''
			latest_price_at = ''
			security_names: list[str] = []

			for tx in product_transactions:
				operation_type = tx.metadata.get('operation_type', '') if isinstance(tx.metadata, dict) else ''
				if operation_type:
					operation_types.add(operation_type)
				security_name = tx.metadata.get('security_name', '') if isinstance(tx.metadata, dict) else ''
				if security_name:
					security_names.append(security_name)
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
					'imported_from': 'aigenis-report',
					'history_rows': len(product_transactions),
					'operation_types': sorted(operation_types),
					'first_operation_at': first_operation_at,
					'last_operation_at': last_operation_at,
					'latest_price_at': latest_price_at,
				}
			)
			product.name = canonical_aigenis_security_name(*security_names) or product.name
			product.units = normalized_units
			product.current_price = latest_price
			product.is_active = normalized_units > 0
			product.metadata = metadata
			product.save(update_fields=['name', 'units', 'current_price', 'is_active', 'metadata', 'updated_at'])
			apply_aigenis_indexed_bond_defaults(product)
			updated_products += 1

	recalculate_usd_valuations()
	return {
		'products_updated': updated_products,
		'transactions_linked': linked_transactions,
	}
