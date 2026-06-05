from django import template

from apps.common.dates import format_display_date, format_display_datetime

register = template.Library()


@register.filter
def get_item(mapping, key):
	if not mapping:
		return ''
	if isinstance(mapping, dict):
		value = mapping.get(key, '')
		return '' if value is None else value
	return ''


@register.filter
def display_date(value):
	return format_display_date(value)


@register.filter
def display_datetime(value):
	return format_display_datetime(value)
