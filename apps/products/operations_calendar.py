from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from apps.products.analytics import product_group_label, product_group_key
from apps.products.models import Product
from apps.products.services.token_terms import estimate_next_income_amount, estimate_next_income_date


def _event_sort_key(event: dict) -> tuple:
    kind = event.get('kind', '')
    return (0 if kind == 'maturity_forecast' else 1, event.get('product_name', ''), kind)


def _maturity_in_window(product: Product, *, reference: date, window_end: date) -> bool:
    maturity_date = product.maturity_date
    if maturity_date is None:
        return False
    if maturity_date < reference or maturity_date > window_end:
        return False
    return (product.units or Decimal('0')) > 0


def _estimate_maturity_redemption_amount(product: Product) -> tuple[Decimal | None, Decimal | None]:
    principal = product.market_value
    if principal <= 0:
        return None, None

    amount = principal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    amount_usd = None
    if product.current_value_usd and product.current_value_usd > 0:
        amount_usd = product.current_value_usd.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    elif getattr(product, 'currency', None) is not None and product.currency.usd_rate:
        amount_usd = (amount * product.currency.usd_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return amount, amount_usd


def _append_maturity_event(
    day_groups: dict[date, dict[str, list[dict]]],
    *,
    product: Product,
    label: str,
) -> None:
    amount, amount_usd = _estimate_maturity_redemption_amount(product)
    day_groups[product.maturity_date][label].append(
        {
            'kind': 'maturity_forecast',
            'is_forecast': True,
            'product': product,
            'product_name': product.name,
            'transaction_type': 'Redemption (planned)',
            'operation_type': 'Плановое погашение',
            'description': 'Principal return at maturity',
            'amount_usd': amount_usd,
            'currency_code': product.currency.code,
            'amount': amount,
            'annual_rate_pct': product.annual_rate_pct,
            'income_schedule': '',
        }
    )


def _sum_group_forecast_amounts(events: list[dict]) -> dict:
    native_amounts = [event['amount'] for event in events if event.get('amount') is not None]
    usd_amounts = [event['amount_usd'] for event in events if event.get('amount_usd') is not None]
    return {
        'total_amount': sum(native_amounts, Decimal('0')) if native_amounts else None,
        'total_amount_usd': sum(usd_amounts, Decimal('0')) if usd_amounts else None,
        'currency_code': events[0]['currency_code'] if events else '',
    }


def build_operations_calendar(
    products: list[Product],
    *,
    today: date | None = None,
    future_days: int = 60,
) -> list[dict]:
    """Upcoming operations only; nearest dates first."""
    reference = today or timezone.localdate()
    window_end = reference + timedelta(days=future_days)

    day_groups: dict[date, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))

    for product in products:
        if not product.is_active:
            continue
        if product.product_type not in (Product.ProductType.TOKEN, Product.ProductType.BOND):
            continue

        label = product_group_label(*product_group_key(product))

        if _maturity_in_window(product, reference=reference, window_end=window_end):
            _append_maturity_event(day_groups, product=product, label=label)

        if product.income_schedule == Product.IncomeSchedule.AT_MATURITY:
            continue

        forecast_date = estimate_next_income_date(product, today=reference)
        if forecast_date is None or forecast_date < reference or forecast_date > window_end:
            continue

        amount, amount_usd = estimate_next_income_amount(product)
        day_groups[forecast_date][label].append(
            {
                'kind': 'income_forecast',
                'is_forecast': True,
                'product': product,
                'product_name': product.name,
                'transaction_type': 'Income (forecast)',
                'operation_type': 'Прогноз выплаты',
                'description': (
                    f'{product.annual_rate_pct}% p.a. · position × rate / period'
                    if product.annual_rate_pct
                    else 'Estimated from income payment history'
                ),
                'amount_usd': amount_usd,
                'currency_code': product.currency.code,
                'amount': amount,
                'annual_rate_pct': product.annual_rate_pct,
                'income_schedule': product.get_income_schedule_display() if product.income_schedule else '',
            }
        )

    calendar_days = []
    for day in sorted(day_groups.keys()):
        groups = []
        for label in sorted(day_groups[day].keys()):
            events = sorted(day_groups[day][label], key=_event_sort_key)
            totals = _sum_group_forecast_amounts(events)
            groups.append({'label': label, 'events': events, **totals})
        calendar_days.append(
            {
                'date': day,
                'days_until': (day - reference).days,
                'is_today': day == reference,
                'groups': groups,
            }
        )

    return calendar_days
