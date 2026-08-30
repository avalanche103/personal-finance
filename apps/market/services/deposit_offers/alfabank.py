from __future__ import annotations

import re
import time
from urllib.parse import urljoin

from django.utils.text import slugify

from apps.market.services.deposit_offers.base import (
	REQUEST_DELAY_SECONDS,
	ParsedDepositOffer,
	detect_irrevocable,
	fetch_text,
	parse_rate_value,
	parse_term_days,
	strip_tags,
)

ALFA_BASE = 'https://www.alfabank.by'
ALFA_BYN_URL = f'{ALFA_BASE}/deposits/byn/'
ALFA_CURRENCY_URL = f'{ALFA_BASE}/deposits/currency/'

PRODUCT_RE = re.compile(
	r'product-item__title[^>]*>([^<]+)'
	r'[\s\S]{0,1600}?'
	r'item-top\">([^<]+)'
	r'[\s\S]{0,600}?'
	r'item-top\">([^<]+)',
	re.I,
)
HREF_NEAR_TITLE_RE = re.compile(
	r'href=\"(/deposits/(?:byn|currency)/[^\"]+)\"'
	r'[\s\S]{0,2000}?'
	r'product-item__title[^>]*>([^<]+)',
	re.I,
)


def _currency_for_page(url: str, name: str) -> str:
	text = f'{url} {name}'.lower()
	if 'usd' in text or 'доллар' in text:
		return 'USD'
	if 'eur' in text or 'евро' in text:
		return 'EUR'
	if 'rub' in text or 'российск' in text:
		return 'RUB'
	if 'cny' in text or 'юан' in text:
		return 'CNY'
	return 'BYN'


def parse_alfabank_listing(html: str, *, page_url: str) -> list[ParsedDepositOffer]:
	href_by_title: dict[str, str] = {}
	for href, title in HREF_NEAR_TITLE_RE.findall(html):
		key = strip_tags(title).lower()
		href_by_title.setdefault(key, urljoin(ALFA_BASE, href))

	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()
	for title_raw, rate_raw, term_raw in PRODUCT_RE.findall(html):
		name = strip_tags(title_raw)
		if not name:
			continue
		rate_pct, rate_pct_max, _ = parse_rate_value(rate_raw)
		term_text = strip_tags(term_raw)
		term_min, term_max = parse_term_days(term_text)
		source_url = href_by_title.get(name.lower(), page_url)
		currency = _currency_for_page(source_url, name)
		external_id = slugify(f'{name}-{currency}-{term_text}')[:200] or slugify(name)
		if external_id in seen:
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
					'rate_text': strip_tags(rate_raw),
					'term_text': term_text,
					'page_url': page_url,
				},
			)
		)
	return offers


def fetch_alfabank_offers() -> list[ParsedDepositOffer]:
	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()
	for url in (ALFA_BYN_URL, ALFA_CURRENCY_URL):
		html = fetch_text(url, timeout=90, insecure_ssl=True)
		for offer in parse_alfabank_listing(html, page_url=url):
			if offer.external_id in seen:
				continue
			seen.add(offer.external_id)
			offers.append(offer)
		time.sleep(REQUEST_DELAY_SECONDS)
	return offers
