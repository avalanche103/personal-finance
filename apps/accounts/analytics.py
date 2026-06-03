from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal


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
