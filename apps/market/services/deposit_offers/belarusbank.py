from __future__ import annotations

import json
from html import unescape

from apps.market.services.deposit_offers.base import (
	ParsedDepositOffer,
	detect_irrevocable,
	fetch_text,
	parse_min_amount,
	parse_rate_value,
	parse_term_days,
)

BELARUSBANK_DEPOSITS_URL = 'https://belarusbank.by/api/deposits_info'
BELARUSBANK_SOURCE_URL = 'https://belarusbank.by/deposits/'


def _normalize_currency(raw: str) -> list[str]:
	text = unescape(raw or '').upper()
	found: list[str] = []
	for code in ('BYN', 'USD', 'EUR', 'RUB', 'CNY'):
		if code in text:
			found.append(code)
	return found or ['BYN']


def parse_belarusbank_deposits(payload: dict | list) -> list[ParsedDepositOffer]:
	items = payload.values() if isinstance(payload, dict) else payload
	offers: list[ParsedDepositOffer] = []
	for item in items:
		if not isinstance(item, dict):
			continue
		external_id = str(item.get('vklad_id') or '').strip()
		name = unescape(str(item.get('vklad_name') or '')).strip()
		if not external_id or not name:
			continue
		rate_raw = str(item.get('vklad_procent') or '')
		rate_pct, rate_pct_max, _ = parse_rate_value(rate_raw)
		term_text = unescape(str(item.get('vklad_srok_text') or item.get('vklad_srok') or '')).strip()
		term_min, term_max = parse_term_days(term_text)
		min_amount = parse_min_amount(item.get('vklad_minimal'))
		is_irrevocable = detect_irrevocable(name)
		currencies = _normalize_currency(str(item.get('vklad_val') or ''))
		for currency in currencies:
			offers.append(
				ParsedDepositOffer(
					external_id=f'{external_id}:{currency}',
					name=name,
					currency=currency,
					rate_pct=rate_pct,
					rate_pct_max=rate_pct_max,
					term_text=term_text,
					term_days_min=term_min,
					term_days_max=term_max,
					min_amount=min_amount,
					is_irrevocable=is_irrevocable,
					source_url=BELARUSBANK_SOURCE_URL,
					raw=dict(item),
				)
			)
	return offers


def fetch_belarusbank_offers() -> list[ParsedDepositOffer]:
	text = fetch_text(BELARUSBANK_DEPOSITS_URL, timeout=60, insecure_ssl=True)
	payload = json.loads(text)
	return parse_belarusbank_deposits(payload)
