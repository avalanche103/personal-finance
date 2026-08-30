from __future__ import annotations

import re
from html import unescape

from django.utils.text import slugify

from apps.market.services.deposit_offers.base import (
	ParsedDepositOffer,
	detect_irrevocable,
	fetch_text,
	parse_rate_value,
	parse_term_days,
)

NEOBANK_DEPOSITS_URL = 'https://neobank.by/deposits/'

# Embedded CMS blob uses HTML entities: &quot;title&quot;:&quot;...&quot;
PRODUCT_RE = re.compile(
	r'&quot;title&quot;:&quot;([^&]+)&quot;'
	r'(?:(?!&quot;title&quot;).){0,800}?'
	r'&quot;title&quot;:&quot;((?:до\s*)?\d+[.,]\d+%[^&]*)&quot;',
	re.I | re.S,
)


def parse_neobank_deposits(html: str) -> list[ParsedDepositOffer]:
	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()
	for name_raw, rate_raw in PRODUCT_RE.findall(html):
		name = unescape(name_raw).strip()
		rate_text = unescape(rate_raw).strip()
		if 'вклад' not in name.lower() and 'нео' not in name.lower():
			continue
		rate_pct, rate_pct_max, _ = parse_rate_value(rate_text)
		if rate_pct is None and rate_pct_max is None:
			continue
		currency = 'BYN'
		up = name.upper()
		for code in ('USD', 'EUR', 'RUB', 'CNY', 'BYN'):
			if code in up:
				currency = code
				break
		term_text = ''
		term_min, term_max = parse_term_days(term_text)
		external_id = slugify(f'{name}-{currency}', allow_unicode=True)[:200]
		if not external_id or external_id in seen:
			continue
		seen.add(external_id)
		offers.append(
			ParsedDepositOffer(
				external_id=external_id,
				name=name,
				currency=currency,
				rate_pct=rate_pct,
				rate_pct_max=rate_pct_max,
				term_text=term_text,
				term_days_min=term_min,
				term_days_max=term_max,
				min_amount=None,
				is_irrevocable=detect_irrevocable(name),
				source_url=NEOBANK_DEPOSITS_URL,
				raw={'rate_text': rate_text, 'parser': 'neobank'},
			)
		)
	return offers


def fetch_neobank_offers() -> list[ParsedDepositOffer]:
	html = fetch_text(NEOBANK_DEPOSITS_URL, timeout=90, insecure_ssl=True)
	return parse_neobank_deposits(html)
