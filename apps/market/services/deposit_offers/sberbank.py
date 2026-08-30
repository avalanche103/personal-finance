from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlparse

from django.utils.text import slugify

from apps.market.services.deposit_offers.base import (
	REQUEST_DELAY_SECONDS,
	ParsedDepositOffer,
	detect_irrevocable,
	fetch_text,
	parse_term_days,
	strip_tags,
)

SBER_BASE = 'https://www.sber-bank.by'
SBER_LISTING_URLS = [
	f'{SBER_BASE}/vklady',
	f'{SBER_BASE}/vklady/v-belorusskih-rublyah',
	f'{SBER_BASE}/vklady/v-inostrannoj-valyute',
]

PRODUCT_PATH_RE = re.compile(
	r'href=[\"\'](/deposit/([a-z0-9\-]+)/([A-Z]{3})/attributes)[\"\']',
	re.I,
)
# Next.js RSC embeds deposit calculator props with escaped quotes.
CALCULATOR_RE = re.compile(
	r'\\"depositCode\\":\\"([^\\"]+)\\"'
	r'.*?\\"termScaleOptions\\":\[(.*?)\]'
	r'.*?\\"minAmount\\":(\d+(?:\.\d+)?)'
	r'.*?\\"productName\\":\\"((?:\\\\.|[^\\\"])*)\\"',
	re.I | re.S,
)
TERM_OPTION_RE = re.compile(
	r'\\"rate\\":([0-9.]+).*?\\"periodType\\":\\"([^\\"]+)\\"'
	r'.*?\\"initialPeriodInDays\\":(\d+).*?\\"endPeriodInDays\\":(\d+)'
	r'.*?\\"label\\":\\"((?:\\\\.|[^\\\"])*)\\"',
	re.I | re.S,
)
H1_RE = re.compile(r'<h1[^>]*>([\s\S]*?)</h1>', re.I)
META_DESC_RE = re.compile(
	r'<meta[^>]+name=[\"\']description[\"\'][^>]+content=[\"\']([^\"\']+)[\"\']',
	re.I,
)

SKIP_DEPOSIT_CODES = (
	'precious',
	'metal',
	'guarantee',
	'premier',  # keep premier if real deposits; filter only non-deposit codes below
)


def _unescape_rsc(text: str) -> str:
	return (
		(text or '')
		.replace(r'\"', '"')
		.replace(r'\\n', ' ')
		.replace(r'\\u003c', '<')
		.replace(r'\\u003e', '>')
	)


def _decimal(value: str | float | int) -> Decimal | None:
	try:
		return Decimal(str(value))
	except (InvalidOperation, TypeError, ValueError):
		return None


def discover_sber_product_urls(listing_html: str, *, listing_url: str) -> list[str]:
	found: list[str] = []
	seen: set[str] = set()
	for path, code, currency in PRODUCT_PATH_RE.findall(listing_html):
		code_l = code.lower()
		if any(token in code_l for token in ('metal', 'guarantee', 'yachejk')):
			continue
		# Prefer online variants; skip offline twin to avoid duplicates.
		if code_l.endswith('-offline'):
			continue
		full = urljoin(listing_url, path)
		key = f'{code_l}:{currency.upper()}'
		if key in seen:
			continue
		seen.add(key)
		found.append(full)
	return found


def _term_text_from_option(label: str, days_min: int, days_max: int, period_type: str) -> str:
	"""One concrete term only — never «от X до Y»."""
	days = max(int(days_min or 0), int(days_max or 0))
	if days <= 0:
		label = _unescape_rsc(label).strip()
		return label
	if period_type.lower().startswith('month'):
		months = max(1, round(days / 30))
		if months == 1:
			return '1 месяц'
		if 2 <= months <= 4:
			return f'{months} месяца'
		return f'{months} месяцев'
	if days == 1:
		return '1 день'
	if 2 <= days <= 4:
		return f'{days} дня'
	return f'{days} дней'


def parse_sber_product_page(html: str, *, source_url: str) -> list[ParsedDepositOffer]:
	path = urlparse(source_url).path.strip('/')
	parts = path.split('/')
	# /deposit/{code}/{CCY}/attributes
	currency = 'BYN'
	deposit_code = ''
	if len(parts) >= 3 and parts[0] == 'deposit':
		deposit_code = parts[1]
		currency = parts[2].upper()

	h1 = ''
	h1_match = H1_RE.search(html)
	if h1_match:
		h1 = re.sub(r'\s+', ' ', strip_tags(h1_match.group(1))).strip()

	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()

	for match in CALCULATOR_RE.finditer(html):
		code, options_blob, min_raw, product_name = match.groups()
		if deposit_code and code != deposit_code and not deposit_code.startswith(code):
			# Keep options tied to this product URL when possible.
			continue
		name = _unescape_rsc(product_name).strip() or h1
		if not name:
			continue
		min_amount = _decimal(min_raw)
		option_matches = list(TERM_OPTION_RE.finditer(options_blob))
		if not option_matches:
			# Fallback: any rate in the options blob.
			rates = re.findall(r'\\"rate\\":([0-9.]+)', options_blob)
			labels = re.findall(r'\\"label\\":\\"((?:\\\\.|[^\\\"])*)\\"', options_blob)
			days = re.findall(r'\\"initialPeriodInDays\\":(\d+)', options_blob)
			if not rates:
				continue
			rate = _decimal(rates[0])
			if rate is None or rate <= 0 or rate > 20:
				continue
			term_text = _unescape_rsc(labels[0]) if labels else ''
			term_min = int(days[0]) if days else None
			term_max = term_min
			external_id = slugify(f'sber-{code}-{currency}-{term_text}-{rate}', allow_unicode=True)[:200]
			if external_id in seen:
				continue
			seen.add(external_id)
			offers.append(
				ParsedDepositOffer(
					external_id=external_id,
					name=name,
					currency=currency,
					rate_pct=rate,
					rate_pct_max=rate,
					term_text=term_text,
					term_days_min=term_min,
					term_days_max=term_max,
					min_amount=min_amount,
					is_irrevocable=detect_irrevocable(name + ' ' + code),
					source_url=source_url,
					raw={'parser': 'sberbank', 'deposit_code': code},
				)
			)
			continue

		for opt in option_matches:
			rate = _decimal(opt.group(1))
			period_type = opt.group(2)
			days_min = int(opt.group(3))
			days_max = int(opt.group(4))
			label = opt.group(5)
			if rate is None or rate <= 0 or rate > 20:
				continue
			term_text = _term_text_from_option(label, days_min, days_max, period_type)
			term_parsed_min, term_parsed_max = parse_term_days(term_text)
			external_id = slugify(
				f'sber-{code}-{currency}-{term_text}-{rate}',
				allow_unicode=True,
			)[:200]
			if not external_id or external_id in seen:
				continue
			seen.add(external_id)
			offers.append(
				ParsedDepositOffer(
					external_id=external_id,
					name=name,
					currency=currency,
					rate_pct=rate,
					rate_pct_max=rate,
					term_text=term_text,
					term_days_min=term_parsed_min or days_min,
					term_days_max=term_parsed_max or days_max,
					min_amount=min_amount,
					is_irrevocable=detect_irrevocable(name + ' ' + code),
					source_url=source_url,
					raw={'parser': 'sberbank', 'deposit_code': code},
				)
			)

	if offers:
		return offers

	# Last resort: meta description for this product page.
	meta = META_DESC_RE.search(html)
	if not meta or not h1:
		return []
	desc = meta.group(1)
	rate_match = re.search(r'(?:ставке|до)\s*(\d+[.,]\d+|\d+)\s*%', desc, re.I)
	if not rate_match:
		return []
	rate = _decimal(rate_match.group(1).replace(',', '.'))
	if rate is None or rate <= 0 or rate > 20:
		return []
	term_match = re.search(
		r'срок\s+(\d+)\s*(ден|дн|день|дня|дней|мес(?:яц(?:а|ев)?)?)',
		desc,
		re.I,
	)
	term_text = ''
	if term_match:
		n, unit = term_match.group(1), term_match.group(2).lower()
		term_text = f'{n} мес.' if unit.startswith('мес') else f'{n} дней'
	term_min, term_max = parse_term_days(term_text)
	ccy_match = re.search(r'\b(BYN|USD|EUR|RUB|CNY)\b', desc)
	if ccy_match:
		currency = ccy_match.group(1)
	external_id = slugify(f'sber-{deposit_code or h1}-{currency}-{term_text}-{rate}', allow_unicode=True)[:200]
	return [
		ParsedDepositOffer(
			external_id=external_id,
			name=h1,
			currency=currency,
			rate_pct=rate,
			rate_pct_max=rate,
			term_text=term_text,
			term_days_min=term_min,
			term_days_max=term_max,
			min_amount=None,
			is_irrevocable=detect_irrevocable(h1 + ' ' + deposit_code),
			source_url=source_url,
			raw={'parser': 'sberbank_meta', 'deposit_code': deposit_code},
		)
	]


def fetch_sberbank_offers() -> list[ParsedDepositOffer]:
	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()
	product_urls: list[str] = []

	for listing_url in SBER_LISTING_URLS:
		html = fetch_text(listing_url, timeout=90, insecure_ssl=True)
		for url in discover_sber_product_urls(html, listing_url=listing_url):
			if url not in product_urls:
				product_urls.append(url)
		time.sleep(REQUEST_DELAY_SECONDS)

	for url in product_urls:
		try:
			html = fetch_text(url, timeout=90, insecure_ssl=True)
		except Exception:
			continue
		for offer in parse_sber_product_page(html, source_url=url):
			if offer.external_id in seen:
				continue
			seen.add(offer.external_id)
			offers.append(offer)
		time.sleep(REQUEST_DELAY_SECONDS)

	return offers
