from __future__ import annotations

import base64
import json
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

TECHNOBANK_DEPOSITS_URL = 'https://tb.by/individuals/deposits/'
ENCODED_RE = re.compile(r'&quot;encoded&quot;:&quot;([A-Za-z0-9+/=]+)&quot;')


def _decode_blob(encoded: str) -> dict | list | None:
	try:
		raw = base64.b64decode(encoded)
		return json.loads(raw.decode('utf-8'))
	except Exception:
		return None


def _walk(node, acc: list[dict]):
	if isinstance(node, dict):
		if node.get('type') == 'deposit' and node.get('title'):
			acc.append(node)
		for value in node.values():
			_walk(value, acc)
	elif isinstance(node, list):
		for item in node:
			_walk(item, acc)


def parse_technobank_deposits(html: str) -> list[ParsedDepositOffer]:
	products: list[dict] = []
	for encoded in ENCODED_RE.findall(html):
		payload = _decode_blob(encoded)
		if payload is None:
			continue
		_walk(payload, products)

	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()
	for product in products:
		name = unescape(str(product.get('title') or '')).strip()
		if not name:
			continue
		features = product.get('features') or []
		rate_text = ''
		term_text = ''
		min_amount_text = ''
		for feature in features:
			title = str(feature.get('title') or '')
			subtitle = str(feature.get('subtitle') or '').lower()
			if '%' in title or 'ставк' in subtitle:
				rate_text = title
			elif 'срок' in subtitle or any(u in title.lower() for u in ('дн', 'мес', 'год')):
				term_text = title
			elif 'сумм' in subtitle:
				min_amount_text = title
		rate_pct, rate_pct_max, _ = parse_rate_value(rate_text)
		if rate_pct is None and rate_pct_max is None:
			continue
		link = ((product.get('link') or {}) if isinstance(product.get('link'), dict) else {}).get('url') or ''
		source_url = 'https://tb.by' + link if link.startswith('/') else (link or TECHNOBANK_DEPOSITS_URL)
		currency = 'BYN'
		blob = f'{name} {source_url}'.upper()
		for code in ('USD', 'EUR', 'RUB', 'CNY', 'BYN'):
			if code in blob:
				currency = code
				break
		term_min, term_max = parse_term_days(term_text)
		external_id = slugify(f'{name}-{currency}-{term_text}', allow_unicode=True)[:200]
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
				source_url=source_url,
				raw={
					'parser': 'technobank',
					'rate_text': rate_text,
					'min_amount_text': min_amount_text,
				},
			)
		)
	return offers


def fetch_technobank_offers() -> list[ParsedDepositOffer]:
	html = fetch_text(TECHNOBANK_DEPOSITS_URL, timeout=90, insecure_ssl=True)
	return parse_technobank_deposits(html)
