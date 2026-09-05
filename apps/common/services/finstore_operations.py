from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.utils import dateparse, timezone

FINSTORE_REDEMPTION_OPERATIONS = {
	'Возврат инвестиций',
	'Досрочное погашение токенов',
}

FINSTORE_INCOME_OPERATIONS = {
	'Получение дохода',
	'Начисление дохода',
}

FINSTORE_TRADE_OPERATIONS = {
	'Покупка токенов',
	'Покупка ICO токенов на Вторичном рынке',
}


def is_finstore_redemption_operation(operation_type: str) -> bool:
	normalized_operation = (operation_type or '').strip()
	return normalized_operation in FINSTORE_REDEMPTION_OPERATIONS


def is_finstore_income_operation(operation_type: str) -> bool:
	normalized_operation = (operation_type or '').strip()
	return normalized_operation in FINSTORE_INCOME_OPERATIONS


def is_finstore_position_operation(operation_type: str) -> bool:
	"""Operations that change token units (buys, sells, redemptions)."""
	normalized_operation = (operation_type or '').strip()
	return (
		normalized_operation in FINSTORE_TRADE_OPERATIONS
		or is_finstore_redemption_operation(normalized_operation)
	)


def canonical_finstore_decimal(value: Decimal | int | float | str | None) -> str:
	if value in (None, ''):
		return '0'
	try:
		number = Decimal(str(value))
	except (InvalidOperation, ValueError):
		return '0'
	text = format(number.normalize(), 'f')
	if '.' in text:
		text = text.rstrip('0').rstrip('.')
	return text or '0'


def canonical_finstore_occurred_at(value) -> str:
	if value in (None, ''):
		return ''
	if isinstance(value, datetime):
		dt = value
	else:
		dt = dateparse.parse_datetime(str(value))
		if dt is None:
			return str(value)
	if timezone.is_naive(dt):
		dt = timezone.make_aware(dt, timezone.get_current_timezone())
	return dt.isoformat()


def build_finstore_transaction_fingerprint(
	*,
	operation_type: str,
	token_name: str,
	occurred_at,
	amount: Decimal | int | float | str,
	quantity: Decimal | int | float | str,
	amount_currency: str = '',
) -> str:
	"""Stable across re-imports of the same Finstore operation from different files/clipboards."""
	payload = ':'.join(
		[
			(operation_type or '').strip(),
			(token_name or '').strip(),
			canonical_finstore_occurred_at(occurred_at),
			canonical_finstore_decimal(amount),
			canonical_finstore_decimal(quantity),
			(amount_currency or '').strip().upper(),
		]
	)
	digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
	return f'finstore:{digest}'
