from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from apps.products.analytics import product_group_label, product_group_key
from apps.products.models import Product
from apps.products.services.deposit_schedule import upcoming_deposit_income_dates
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


def _metadata_date(product: Product, key: str) -> date | None:
    metadata = product.metadata if isinstance(product.metadata, dict) else {}
    raw_value = str(metadata.get(key, '') or '').strip()
    if not raw_value:
        return None
    for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(raw_value, fmt).date()
        except ValueError:
            continue
    return None


def _estimate_deposit_maturity_income_amount(product: Product) -> tuple[Decimal | None, Decimal | None]:
    if product.product_type != Product.ProductType.DEPOSIT:
        return None, None
    if product.annual_rate_pct is None or product.annual_rate_pct <= 0 or product.maturity_date is None:
        return None, None

    principal = product.market_value
    if principal <= 0:
        return None, None

    opened_at = _metadata_date(product, 'opened_at')
    term_days = max((product.maturity_date - opened_at).days, 1) if opened_at else 365
    amount = (
        principal
        * product.annual_rate_pct
        / Decimal('100')
        * Decimal(term_days)
        / Decimal('365')
    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    amount_usd = None
    if product.current_value_usd and product.current_value_usd > 0:
        amount_usd = (
            product.current_value_usd
            * product.annual_rate_pct
            / Decimal('100')
            * Decimal(term_days)
            / Decimal('365')
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    elif getattr(product, 'currency', None) is not None and product.currency.usd_rate:
        amount_usd = (amount * product.currency.usd_rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return amount, amount_usd


def _append_income_event(
    day_groups: dict[date, dict[str, list[dict]]],
    *,
    product: Product,
    label: str,
    forecast_date: date,
    amount: Decimal | None,
    amount_usd: Decimal | None,
    description: str,
) -> None:
    day_groups[forecast_date][label].append(
        {
            'kind': 'income_forecast',
            'is_forecast': True,
            'product': product,
            'product_name': product.name,
            'transaction_type': 'Income (forecast)',
            'operation_type': 'Прогноз выплаты',
            'description': description,
            'amount_usd': amount_usd,
            'currency_code': product.currency.code,
            'amount': amount,
            'annual_rate_pct': product.annual_rate_pct,
            'income_schedule': product.get_income_schedule_display() if product.income_schedule else '',
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
        if product.product_type not in (Product.ProductType.TOKEN, Product.ProductType.BOND, Product.ProductType.DEPOSIT):
            continue

        label = product_group_label(*product_group_key(product))
        maturity_in_window = _maturity_in_window(product, reference=reference, window_end=window_end)

        if maturity_in_window:
            _append_maturity_event(day_groups, product=product, label=label)

        if product.income_schedule == Product.IncomeSchedule.AT_MATURITY:
            if product.product_type == Product.ProductType.DEPOSIT and maturity_in_window:
                amount, amount_usd = _estimate_deposit_maturity_income_amount(product)
                _append_income_event(
                    day_groups,
                    product=product,
                    label=label,
                    forecast_date=product.maturity_date,
                    amount=amount,
                    amount_usd=amount_usd,
                    description='Deposit interest expected at maturity',
                )
            continue

        if (
            product.product_type == Product.ProductType.DEPOSIT
            and product.income_schedule == Product.IncomeSchedule.TWICE_MONTHLY
        ):
            forecast_dates = upcoming_deposit_income_dates(
                product,
                reference=reference,
                window_end=window_end,
            )
            for forecast_date in forecast_dates:
                amount, amount_usd = estimate_next_income_amount(product)
                _append_income_event(
                    day_groups,
                    product=product,
                    label=label,
                    forecast_date=forecast_date,
                    amount=amount,
                    amount_usd=amount_usd,
                    description=(
                        f'{product.annual_rate_pct}% p.a. · position × rate / period'
                        if product.annual_rate_pct
                        else 'Estimated from income payment history'
                    ),
                )
            continue

        forecast_date = estimate_next_income_date(product, today=reference)
        if forecast_date is None or forecast_date < reference or forecast_date > window_end:
            continue

        amount, amount_usd = estimate_next_income_amount(product)
        _append_income_event(
            day_groups,
            product=product,
            label=label,
            forecast_date=forecast_date,
            amount=amount,
            amount_usd=amount_usd,
            description=(
                f'{product.annual_rate_pct}% p.a. · position × rate / period'
                if product.annual_rate_pct
                else 'Estimated from income payment history'
            ),
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
