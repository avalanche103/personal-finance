import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import urlopen

from apps.imports.services.integrations.base import BaseApiClient


@dataclass(frozen=True)
class NBRBCurrencyDescriptor:
	cur_id: int
	code: str
	name: str
	scale: int
	date_start: str
	date_end: str


class NBRBExchangeRatesClient(BaseApiClient):
	source_code = 'nbrb-exrates-api'
	base_url = 'https://api.nbrb.by/exrates'

	def _get_json(self, path: str, query: dict | None = None):
		url = f'{self.base_url}/{path.lstrip("/")}'
		if query:
			url = f'{url}?{urlencode(query)}'

		with urlopen(url) as response:
			return json.loads(response.read().decode('utf-8'))

	def fetch_currencies(self, codes: list[str]) -> dict[str, NBRBCurrencyDescriptor]:
		payload = self._get_json('currencies')
		selected: dict[str, NBRBCurrencyDescriptor] = {}
		for item in payload:
			code = item.get('Cur_Abbreviation')
			if code not in codes:
				continue
			selected[code] = NBRBCurrencyDescriptor(
				cur_id=item['Cur_ID'],
				code=code,
				name=item.get('Cur_Name_Eng') or item.get('Cur_Name') or code,
				scale=item['Cur_Scale'],
				date_start=item.get('Cur_DateStart', ''),
				date_end=item.get('Cur_DateEnd', ''),
			)

		missing = set(codes) - set(selected)
		if missing:
			raise ValueError(f'Missing currencies in NBRB API: {", ".join(sorted(missing))}')

		return selected

	def fetch_rate_dynamics(self, cur_id: int, start_date: date, end_date: date) -> list[dict]:
		return self._get_json(
			f'rates/dynamics/{cur_id}',
			query={
				'startDate': start_date.isoformat(),
				'endDate': end_date.isoformat(),
			},
		)

	def fetch_rate_dynamics_in_chunks(self, cur_id: int, start_date: date, end_date: date, chunk_days: int = 365) -> list[dict]:
		all_rows: list[dict] = []
		cursor = start_date
		while cursor <= end_date:
			chunk_end = min(cursor + timedelta(days=chunk_days - 1), end_date)
			all_rows.extend(self.fetch_rate_dynamics(cur_id, cursor, chunk_end))
			cursor = chunk_end + timedelta(days=1)
		return all_rows

	@staticmethod
	def official_rate_per_unit(row: dict, scale: int) -> Decimal:
		return Decimal(str(row['Cur_OfficialRate'])) / Decimal(scale)