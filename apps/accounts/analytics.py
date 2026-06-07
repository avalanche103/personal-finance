from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal

from apps.accounts.models import Account
from apps.accounts.services.binance import USD_LIKE_ASSETS
from apps.institutions.models import FinancialInstitution


@dataclass(frozen=True)
class DashboardBalanceRow:
	name: str
	institution: FinancialInstitution
	currency_code: str
	current_balance: Decimal
	current_balance_usd: Decimal


def is_binance_stable_account(account: Account) -> bool:
	if account.institution.slug != 'binance':
		return False
	metadata = account.metadata if isinstance(account.metadata, dict) else {}
	asset = (metadata.get('asset') or account.currency.code or '').upper()
	return asset in USD_LIKE_ASSETS


def build_dashboard_balance_rows(accounts, *, limit: int = 8) -> list[DashboardBalanceRow]:
	rows: list[DashboardBalanceRow] = []
	binance_stable_accounts: list[Account] = []
	binance_institution: FinancialInstitution | None = None

	for account in accounts:
		if is_binance_stable_account(account):
			binance_stable_accounts.append(account)
			binance_institution = account.institution
			continue
		rows.append(
			DashboardBalanceRow(
				name=account.name,
				institution=account.institution,
				currency_code=account.currency.code,
				current_balance=account.current_balance or Decimal('0'),
				current_balance_usd=account.current_balance_usd or Decimal('0'),
			)
		)

	if binance_stable_accounts and binance_institution is not None:
		total_usd = sum((account.current_balance_usd or Decimal('0') for account in binance_stable_accounts), Decimal('0'))
		if total_usd > 0:
			rows.append(
				DashboardBalanceRow(
					name='Binance Stable',
					institution=binance_institution,
					currency_code='USD',
					current_balance=total_usd,
					current_balance_usd=total_usd,
				)
			)

	rows.sort(key=lambda row: (row.current_balance_usd, row.name), reverse=True)
	return rows[:limit]


def build_account_groups(accounts):
    grouped = OrderedDict()
    for account in accounts:
        institution = account.institution
        if institution.pk not in grouped:
            grouped[institution.pk] = {
                'label': institution.name,
                'institution': institution,
                'accounts': [],
                'total_balance_usd': Decimal('0'),
                'balances_by_currency': OrderedDict(),
            }

        group = grouped[institution.pk]
        group['accounts'].append(account)
        balance = account.current_balance or Decimal('0')
        balance_usd = account.current_balance_usd or Decimal('0')
        group['total_balance_usd'] += balance_usd
        currency_code = account.currency.code
        group['balances_by_currency'][currency_code] = (
            group['balances_by_currency'].get(currency_code, Decimal('0')) + balance
        )

    result = []
    for group in grouped.values():
        group['balances_by_currency'] = [
            {'currency_code': code, 'balance': balance}
            for code, balance in group['balances_by_currency'].items()
        ]
        result.append(group)
    return result
