from django.db.models import QuerySet

from apps.accounts.models import Account

PORTFOLIO_EXCLUDED_INSTITUTION_SLUGS = frozenset({'income-sources'})
NON_HOLDING_ACCOUNT_PURPOSES = frozenset({'payroll', 'insurance_premiums'})


def is_portfolio_holding_account(account: Account) -> bool:
	if account.institution.slug in PORTFOLIO_EXCLUDED_INSTITUTION_SLUGS:
		return False
	institution_metadata = account.institution.metadata if isinstance(account.institution.metadata, dict) else {}
	if institution_metadata.get('purpose') == 'payroll_source':
		return False
	account_metadata = account.metadata if isinstance(account.metadata, dict) else {}
	if account_metadata.get('purpose') in NON_HOLDING_ACCOUNT_PURPOSES:
		return False
	return True


def visible_account_queryset() -> QuerySet[Account]:
	return (
		Account.objects.select_related('institution', 'currency')
		.filter(current_balance_usd__gt=0)
	)


def portfolio_holding_account_queryset() -> QuerySet[Account]:
	return visible_account_queryset().exclude(institution__slug__in=PORTFOLIO_EXCLUDED_INSTITUTION_SLUGS)
