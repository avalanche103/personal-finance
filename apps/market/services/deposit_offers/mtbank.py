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
	parse_min_amount,
	parse_term_days,
	strip_tags,
)

MTBANK_BASE = 'https://www.mtbank.by'
MTBANK_LISTING_URL = f'{MTBANK_BASE}/deposits/'

PRODUCT_HREF_RE = re.compile(r'href=[\"\'](/deposits/([a-z0-9\-]+)/?)[\"\']', re.I)
TITLE_RE = re.compile(r'<title[^>]*>([\s\S]*?)</title>', re.I)
QUOTED_NAME_RE = re.compile(r'[«\"]([^»\"]{3,80})[»\"]')
AMOUNT_RE = re.compile(r'deposit-table__td--amount[^>]*>([\s\S]*?)</div>', re.I)
PERIOD_RE = re.compile(r'deposit-table__td--period[^>]*>([\s\S]*?)</div>', re.I)
PERCENT_RE = re.compile(r'deposit-table__td--percent[^>]*>([\s\S]*?)</div>', re.I)
TOKEN_RE = re.compile(
	r'(?:<h2 class=\"deposit-pricing__title\"[^>]*>([\s\S]*?)</h2>)|'
	r'(<div class=\"deposit-table__tr\">[\s\S]*?)'
	r'(?=<div class=\"deposit-table__tr\">|<div class=\"deposit-pricing__|<h2 class=\"deposit-pricing__title\")',
	re.I,
)

SKIP_SLUGS = {
	'warranty',
	'oferty-dlya-depozitov',
	'compare',
	'faq',
}

SLUG_NAMES = {
	'mtbelki': 'МТБелки',
	'mtbelki-online': 'МТБелки Онлайн',
	'aktualnyy-usd': 'Актуальный (USD)',
	'aktualnyy-online-usd': 'Актуальный online (USD)',
	'aktualnyy-eur': 'Актуальный (EUR)',
	'aktualnyy-online-eur': 'Актуальный online (EUR)',
	'aktualnyy-rub': 'Актуальный (RUB)',
	'aktualnyy-cny': 'Актуальный (CNY)',
}

CURRENCY_FROM_SLUG = {
	'usd': 'USD',
	'eur': 'EUR',
	'rub': 'RUB',
	'cny': 'CNY',
	'byn': 'BYN',
}


def _decimal(value: str) -> Decimal | None:
	try:
		return Decimal(value.replace(',', '.'))
	except (InvalidOperation, AttributeError):
		return None


def _clean(text: str) -> str:
	return re.sub(r'\s+', ' ', strip_tags(text or '')).strip()


def discover_mtbank_product_urls(listing_html: str, *, listing_url: str = MTBANK_LISTING_URL) -> list[str]:
	found: list[str] = []
	seen: set[str] = set()
	for path, slug in PRODUCT_HREF_RE.findall(listing_html):
		slug_l = slug.lower().strip('/')
		if not slug_l or slug_l in SKIP_SLUGS:
			continue
		full = urljoin(listing_url, path if path.endswith('/') else path + '/')
		if full in seen:
			continue
		seen.add(full)
		found.append(full)
	return found


def _decode_json_string(raw: str) -> str:
	if '\\u' not in raw:
		return raw
	try:
		return raw.encode('utf-8').decode('unicode_escape')
	except Exception:
		return raw


def _product_name(html: str, *, source_url: str) -> str:
	slug = urlparse(source_url).path.strip('/').split('/')[-1].lower()
	if slug in SLUG_NAMES:
		return SLUG_NAMES[slug]

	title_match = TITLE_RE.search(html)
	if title_match:
		title = _clean(title_match.group(1))
		quoted = QUOTED_NAME_RE.search(title)
		if quoted:
			return quoted.group(1).strip()
		title = re.sub(r'^[^A-Za-zА-Яа-яЁё0-9«\"]+', '', title)
		title = re.split(r'\s+[—\-–|]\s+', title)[0].strip()
		low = title.lower()
		if title and not any(token in low for token in ('белкам', 'инфляц', 'простой способ')):
			return title

	for m in re.finditer(r'"@type":"Offer","name":"((?:\\u[0-9a-fA-F]{4}|[^"])+)"', html):
		name = _clean(_decode_json_string(m.group(1)))
		if name and 'белкам' not in name.lower():
			return name

	return slug.replace('-', ' ').strip() or 'Вклад МТБанк'


def _currency_from_amount(amount_text: str, source_url: str, product_name: str) -> str:
	blob = f'{amount_text} {source_url} {product_name}'.upper()
	for code in ('BYN', 'USD', 'EUR', 'RUB', 'CNY'):
		if re.search(rf'\b{code}\b', blob):
			return code
	slug = urlparse(source_url).path.lower()
	for token, code in CURRENCY_FROM_SLUG.items():
		if token in slug:
			return code
	return 'BYN'


def _rate_from_percent_cell(text: str) -> tuple[Decimal | None, Decimal | None]:
	cleaned = _clean(text)
	match = re.match(r'(?:до\s*)?(\d+[.,]\d+|\d+)\s*%', cleaned, re.I)
	if not match:
		return None, None
	rate = _decimal(match.group(1))
	if rate is None or rate <= 0 or rate > 20:
		return None, None
	return rate, rate


def parse_mtbank_product_page(html: str, *, source_url: str) -> list[ParsedDepositOffer]:
	product_name = _product_name(html, source_url=source_url)
	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()
	current_section = ''

	for token in TOKEN_RE.finditer(html):
		if token.group(1) is not None:
			title = _clean(token.group(1))
			if title:
				current_section = title
			continue

		row_html = token.group(2) or ''
		amount_m = AMOUNT_RE.search(row_html)
		period_m = PERIOD_RE.search(row_html)
		percent_m = PERCENT_RE.search(row_html)
		if not period_m or not percent_m:
			continue

		amount_text = _clean(amount_m.group(1) if amount_m else '')
		term_text = _clean(period_m.group(1))
		term_match = re.search(
			r'(\d+\s*(?:ден|дн|день|дня|дней|мес(?:яц(?:а|ев)?)?|год(?:а|ов)?|лет)\w*)',
			term_text,
			re.I,
		)
		if term_match:
			term_text = term_match.group(1)

		rate_pct, rate_pct_max = _rate_from_percent_cell(percent_m.group(1))
		if rate_pct is None and rate_pct_max is None:
			continue

		currency = _currency_from_amount(amount_text, source_url, product_name)
		min_amount = parse_min_amount(amount_text)
		term_min, term_max = parse_term_days(term_text)
		section_label = current_section.strip()
		is_irrevocable = detect_irrevocable(f'{section_label} {product_name}')
		name = product_name
		section_low = section_label.lower()
		if section_low in {'безотзывный', 'отзывный'}:
			name = f'{product_name} ({section_label})'
		elif 'безотзыв' in section_low:
			name = f'{product_name} (Безотзывный)'
			is_irrevocable = True
		elif re.search(r'(?<!безо)отзыв', section_low):
			name = f'{product_name} (Отзывный)'
			is_irrevocable = False

		external_id = slugify(
			f'mtbank-{product_name}-{currency}-{term_text}-{rate_pct or rate_pct_max}-{section_label}',
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
				rate_pct=rate_pct,
				rate_pct_max=rate_pct_max,
				term_text=term_text,
				term_days_min=term_min,
				term_days_max=term_max,
				min_amount=min_amount,
				is_irrevocable=is_irrevocable,
				source_url=source_url,
				raw={
					'parser': 'mtbank',
					'section': section_label,
					'amount_text': amount_text,
				},
			)
		)
	return offers


def fetch_mtbank_offers() -> list[ParsedDepositOffer]:
	listing_html = fetch_text(MTBANK_LISTING_URL, timeout=90, insecure_ssl=True)
	product_urls = discover_mtbank_product_urls(listing_html)
	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()
	for url in product_urls:
		time.sleep(REQUEST_DELAY_SECONDS)
		try:
			html = fetch_text(url, timeout=90, insecure_ssl=True)
		except Exception:
			continue
		for offer in parse_mtbank_product_page(html, source_url=url):
			if offer.external_id in seen:
				continue
			seen.add(offer.external_id)
			offers.append(offer)
	return offers
