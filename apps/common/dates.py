from __future__ import annotations

from datetime import date, datetime, timedelta

from django.utils import timezone

DATE_DISPLAY_FORMAT = '%d.%m.%Y'
DATETIME_DISPLAY_FORMAT = '%d.%m.%Y %H:%M'
DATE_INPUT_FORMATS = ('%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y')


def format_display_date(value: date | datetime | None) -> str:
	if value is None:
		return ''
	if isinstance(value, datetime):
		if timezone.is_aware(value):
			value = timezone.localtime(value)
		return value.strftime(DATE_DISPLAY_FORMAT)
	return value.strftime(DATE_DISPLAY_FORMAT)


def format_display_datetime(value: date | datetime | None) -> str:
	if value is None:
		return ''
	if isinstance(value, datetime):
		if timezone.is_aware(value):
			value = timezone.localtime(value)
		return value.strftime(DATETIME_DISPLAY_FORMAT)
	if isinstance(value, date):
		return value.strftime(DATE_DISPLAY_FORMAT)
	return str(value)


def following_weekday(value: date) -> date:
	"""Move Saturday/Sunday to the following Monday. Weekday dates stay as-is."""
	if value.weekday() == 5:
		return value + timedelta(days=2)
	if value.weekday() == 6:
		return value + timedelta(days=1)
	return value


def parse_display_date(text: str | None) -> date | None:
	if text in (None, ''):
		return None
	normalized = str(text).strip()
	for fmt in DATE_INPUT_FORMATS:
		try:
			return datetime.strptime(normalized, fmt).date()
		except ValueError:
			continue
	try:
		return date.fromisoformat(normalized[:10])
	except ValueError:
		return None
