"""Build product operation deep-links into the ledger transaction form."""

from __future__ import annotations

from urllib.parse import urlencode

from django.urls import reverse

from apps.accounts.models import Transaction
from apps.products.models import Product


def product_detail_path(product: Product) -> str:
	return reverse('products:detail', args=[product.pk])


def transaction_create_url(
	product: Product,
	*,
	transaction_type: str | None = None,
	account_id: int | None = None,
	next_url: str | None = None,
) -> str:
	params: dict[str, str] = {
		'product': str(product.pk),
		'next': next_url or product_detail_path(product),
	}
	if transaction_type:
		params['type'] = transaction_type
	resolved_account_id = account_id
	if resolved_account_id is None and product.product_type == Product.ProductType.DEPOSIT:
		resolved_account_id = product.income_account_id
	if resolved_account_id:
		params['account'] = str(resolved_account_id)
	return f"{reverse('accounts:transaction_create')}?{urlencode(params)}"


def _action(label: str, url: str, *, style: str = 'secondary', kind: str = '') -> dict:
	return {
		'label': label,
		'url': url,
		'style': style,
		'kind': kind,
	}


def build_product_actions(product: Product) -> list[dict]:
	"""Return action buttons for the product detail page."""
	next_url = product_detail_path(product)
	add_tx = _action(
		'Add transaction',
		transaction_create_url(product, next_url=next_url),
		style='secondary',
		kind='add_transaction',
	)
	imports_url = reverse('imports:upload')

	if product.product_type == Product.ProductType.DEPOSIT:
		actions: list[dict] = []
		if product.is_active and product.income_account_id:
			actions.extend(
				[
					_action(
						'Deposit',
						transaction_create_url(
							product,
							transaction_type=Transaction.TransactionType.DEPOSIT,
							next_url=next_url,
						),
						style='primary',
						kind='top_up',
					),
					_action(
						'Withdrawal',
						transaction_create_url(
							product,
							transaction_type=Transaction.TransactionType.WITHDRAWAL,
							next_url=next_url,
						),
						style='secondary',
						kind='redeem',
					),
					_action(
						'Income',
						transaction_create_url(
							product,
							transaction_type=Transaction.TransactionType.INCOME,
							next_url=next_url,
						),
						style='secondary',
						kind='interest',
					),
				]
			)
		actions.append(add_tx)
		return actions

	if product.product_type == Product.ProductType.BOND:
		actions = []
		if product.is_active:
			actions.extend(
				[
					_action(
						'Coupon / income',
						transaction_create_url(
							product,
							transaction_type=Transaction.TransactionType.INCOME,
							account_id=product.income_account_id,
							next_url=next_url,
						),
						style='primary',
						kind='income',
					),
					_action(
						'Buy / sell',
						transaction_create_url(
							product,
							transaction_type=Transaction.TransactionType.TRADE,
							next_url=next_url,
						),
						style='secondary',
						kind='trade',
					),
				]
			)
		actions.append(add_tx)
		return actions

	if product.product_type == Product.ProductType.LIFE_INSURANCE:
		return [
			_action('Update contribution', imports_url, style='primary', kind='priorlife'),
			add_tx,
		]

	if product.product_type == Product.ProductType.PENSION:
		return [
			_action('Import', imports_url, style='primary', kind='import'),
			add_tx,
		]

	if product.product_type in {
		Product.ProductType.TOKEN,
		Product.ProductType.STOCK,
		Product.ProductType.ETF,
		Product.ProductType.CFD,
		Product.ProductType.CRYPTO,
	}:
		actions = []
		if product.is_active:
			actions.extend(
				[
					_action(
						'Buy',
						transaction_create_url(
							product,
							transaction_type=Transaction.TransactionType.TRADE,
							next_url=next_url,
						),
						style='primary',
						kind='buy',
					),
					_action(
						'Sell',
						transaction_create_url(
							product,
							transaction_type=Transaction.TransactionType.TRADE,
							next_url=next_url,
						),
						style='secondary',
						kind='sell',
					),
					_action(
						'Income',
						transaction_create_url(
							product,
							transaction_type=Transaction.TransactionType.INCOME,
							account_id=product.income_account_id,
							next_url=next_url,
						),
						style='secondary',
						kind='income',
					),
				]
			)
		actions.append(add_tx)
		return actions

	return [add_tx]
