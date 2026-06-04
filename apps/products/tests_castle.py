from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.products.services.castle import (
	fetch_calendar_bond_index,
	fetch_calendar_bond_ids,
	parse_bond_page,
)

BOND_HTML_FIXTURE = """
<html><body>
<p>Информация по токену SMART_(BYN_868) компании ООО «СМАРТ Партнер».</p>
<table class="bond-params-table">
<tr><td class="bond-param-label">Ставка</td><td class="yield-cell fw-semibold">19,0%</td></tr>
<tr><td class="bond-param-label">Срок</td><td>66 мес.</td></tr>
<tr><td class="bond-param-label">Окончание продаж</td><td>14/05/2027</td></tr>
<tr><td class="bond-param-label">Погашение</td><td class="fw-semibold">15/11/2031</td></tr>
<tr><td class="bond-param-label">Периодичность выплат</td><td>Ежемесячно</td></tr>
</table>
<p>Размещение на платформе Finstore.</p>
</body></html>
"""

CALENDAR_HTML_FIXTURE = """
<a href="/bond/973/">SMART_(BYN_868)</a>
<a href="/bond/864/">YOWHEELS_(USD_864)</a>
<a href="/bond/973/">SMART_(BYN_868)</a>
"""


class CastleParserTests(TestCase):
	def test_parse_bond_page_extracts_rate_maturity_and_schedule(self):
		details = parse_bond_page(BOND_HTML_FIXTURE, bond_id='973', source_url='https://castle.by/bond/973/')
		self.assertIsNotNone(details)
		assert details is not None
		self.assertEqual(details.external_id, 'SMART_(BYN_868)')
		self.assertEqual(details.annual_rate_pct, Decimal('19.0'))
		self.assertEqual(details.maturity_date, date(2031, 11, 15))
		self.assertEqual(details.income_schedule, 'monthly')
		self.assertEqual(details.platform, 'finstore')

	def test_fetch_calendar_bond_index_maps_token_to_latest_bond(self):
		with patch('apps.products.services.castle._fetch_html', return_value=CALENDAR_HTML_FIXTURE):
			index = fetch_calendar_bond_index()
		self.assertEqual(index['SMART_(BYN_868)'], '973')
		self.assertEqual(index['YOWHEELS_(USD_864)'], '864')
		self.assertEqual(sorted(index.values()), ['864', '973'])
