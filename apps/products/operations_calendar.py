from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from django.utils import timezone

from apps.products.analytics import product_group_label, product_group_key
from apps.products.models import Product
from apps.products.services.token_terms import estimate_next_income_amount, estimate_next_income_date


def _event_sort_key(event: dict) -> tuple:
    return (event.get('product_name', ''), event.get('kind', ''))


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
        if product.product_type != Product.ProductType.TOKEN or not product.is_active:
            continue

        forecast_date = estimate_next_income_date(product, today=reference)
        if forecast_date is None or forecast_date < reference or forecast_date > window_end:
            continue

        amount, amount_usd = estimate_next_income_amount(product)
        label = product_group_label(*product_group_key(product))
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
            groups.append({'label': label, 'events': events})
        calendar_days.append(
            {
                'date': day,
                'days_until': (day - reference).days,
                'is_today': day == reference,
                'groups': groups,
            }
        )

    return calendar_days
