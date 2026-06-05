from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone

from apps.common.dates import format_display_date, format_display_datetime, parse_display_date


class DisplayDateTests(TestCase):
	def test_format_display_date(self):
		self.assertEqual(format_display_date(date(2026, 8, 16)), '16.08.2026')

	def test_format_display_datetime(self):
		value = datetime(2026, 8, 16, 14, 30, tzinfo=ZoneInfo('Europe/Minsk'))
		self.assertEqual(format_display_datetime(value), '16.08.2026 14:30')

	def test_parse_display_date_accepts_dotted_format(self):
		self.assertEqual(parse_display_date('16.08.2026'), date(2026, 8, 16))

	def test_parse_display_date_accepts_iso_fallback(self):
		self.assertEqual(parse_display_date('2026-08-16'), date(2026, 8, 16))
