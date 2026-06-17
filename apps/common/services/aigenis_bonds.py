from __future__ import annotations

from django.db.models import Q

from apps.accounts.models import Account
from apps.institutions.models import FinancialInstitution
from apps.products.models import Product

AIGENIS_INDEXED_BOND_ISINS = frozenset({
	'BCSE-00477-P01',
	'BCSE-00487-P02',
})


def get_alfabank_byn_account() -> Account | None:
	return (
		Account.objects.select_related('institution', 'currency')
		.filter(
			institution__slug='alfabank',
			currency__code='BYN',
			account_type=Account.AccountType.BANK,
		)
		.order_by('id')
		.first()
	)


def apply_aigenis_indexed_bond_defaults(product: Product, *, save: bool = True) -> bool:
	product_key = product.external_id or product.isin
	if product_key not in AIGENIS_INDEXED_BOND_ISINS:
		return False

	income_account = get_alfabank_byn_account()
	if income_account is not None:
		product.income_account = income_account

	from apps.common.services.indexed_bonds import configure_aigenis_indexed_bond

	if not save:
		return configure_aigenis_indexed_bond(product, preserve_user_payments=True)
	if income_account is not None and product.income_account_id != income_account.id:
		product.save(update_fields=['income_account', 'updated_at'])
	return configure_aigenis_indexed_bond(product, preserve_user_payments=True)


def configure_aigenis_indexed_bonds(institution: FinancialInstitution | None = None) -> int:
	products = Product.objects.filter(
		product_type=Product.ProductType.BOND,
	).filter(Q(external_id__in=AIGENIS_INDEXED_BOND_ISINS) | Q(isin__in=AIGENIS_INDEXED_BOND_ISINS))
	if institution is not None:
		products = products.filter(institution=institution)

	updated = 0
	for product in products:
		if apply_aigenis_indexed_bond_defaults(product):
			updated += 1
	return updated
