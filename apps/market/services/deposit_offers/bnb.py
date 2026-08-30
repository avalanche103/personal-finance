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

BNB_BASE = 'https://bnb.by'
BNB_LISTING_URL = f'{BNB_BASE}/o-lichnom/sberezhenie/'

LISTING_CARD_RE = re.compile(
	r'<div class=\"deposits-item__sm deposits-item[^\"]*\"([\s\S]*?)'
	r'(?=<div class=\"deposits-item__sm deposits-item|$)',
	re.I,
)
HREF_RE = re.compile(r'href=\"(/o-lichnom/sberezhenie/[^\"]+)\"', re.I)
TITLE_RE = re.compile(r'deposits-item__title[^>]*>([\s\S]*?)</div>', re.I)
ROW_RE = re.compile(r'<tr[^>]*>([\s\S]*?)</tr>', re.I)
CELL_RE = re.compile(r'<t[dh][^>]*>([\s\S]*?)</t[dh]>', re.I)
RATE_CELL_RE = re.compile(
	r'(?:до\s*)?(\d+[.,]\d+|\d+)\s*%|(\d+[.,]\d+|\d+)\s*\)',
	re.I,
)


def _parse_rate_cell(text: str) -> tuple:
	cleaned = strip_tags(text)
	if cleaned in {'', '-', '—', '–'}:
		return None, None
	# Prefer explicit «(13.5%)» used with РВСР formulas.
	paren = re.search(r'\((\d+[.,]\d+|\d+)\s*%\)', cleaned)
	if paren:
		rate_pct, rate_pct_max, _ = parse_rate_value(paren.group(1) + '%')
		return rate_pct, rate_pct_max
	rate_pct, rate_pct_max, is_up_to = parse_rate_value(cleaned)
	if rate_pct is None and rate_pct_max is None:
		match = RATE_CELL_RE.search(cleaned)
		if match:
			raw = match.group(1) or match.group(2)
			rate_pct, rate_pct_max, is_up_to = parse_rate_value(raw + '%')
	if is_up_to and rate_pct is not None and rate_pct_max is None:
		return None, rate_pct
	# Avoid «РВСР-1,12 … 13.5%» becoming a 1.12–13.5 range.
	if (
		rate_pct is not None
		and rate_pct_max is not None
		and rate_pct != rate_pct_max
		and 'рвср' in cleaned.lower()
	):
		return rate_pct_max, rate_pct_max
	return rate_pct, rate_pct_max


def parse_bnb_listing(html: str) -> list[tuple[str, str, bool | None]]:
	"""Return list of (title, path, is_irrevocable)."""
	products: list[tuple[str, str, bool | None]] = []
	seen: set[str] = set()
	for card in LISTING_CARD_RE.findall(html):
		href_match = HREF_RE.search(card)
		title_match = TITLE_RE.search(card)
		if not href_match or not title_match:
			continue
		path = href_match.group(1)
		if 'filter/' in path:
			continue
		title = strip_tags(title_match.group(1))
		if path in seen:
			continue
		seen.add(path)
		products.append((title, path, detect_irrevocable(title + ' ' + path)))
	return products


def parse_bnb_rate_table(
	html: str,
	*,
	product_name: str,
	source_url: str,
	is_irrevocable: bool | None,
) -> list[ParsedDepositOffer]:
	rows = ROW_RE.findall(html)
	if not rows:
		return []

	header_cells: list[str] = []
	offers: list[ParsedDepositOffer] = []
	for row_html in rows:
		cells = [strip_tags(cell) for cell in CELL_RE.findall(row_html)]
		if not cells:
			continue
		joined = ' '.join(cells).lower()
		if 'срок вклада' in joined or (cells[0].lower().startswith('срок') and 'byn' in joined):
			header_cells = [c.upper() for c in cells[1:]]
			continue
		if not header_cells:
			continue
		term_text = cells[0]
		if not term_text or term_text.lower().startswith('валюта'):
			# Stop once descriptive key/value rows begin.
			if ':' in term_text or term_text.lower() in {'валюта:', 'срок:'}:
				break
			continue
		term_min, term_max = parse_term_days(term_text)
		for idx, currency in enumerate(header_cells):
			if currency not in {'BYN', 'USD', 'EUR', 'RUB', 'CNY'}:
				continue
			if idx + 1 >= len(cells):
				continue
			rate_pct, rate_pct_max = _parse_rate_cell(cells[idx + 1])
			if rate_pct is None and rate_pct_max is None:
				continue
			external_id = slugify(f'{product_name}-{currency}-{term_text}')[:200]
			offers.append(
				ParsedDepositOffer(
					external_id=external_id,
					name=f'{product_name} ({term_text})',
					currency=currency,
					rate_pct=rate_pct,
					rate_pct_max=rate_pct_max,
					term_text=term_text,
					term_days_min=term_min,
					term_days_max=term_max,
					min_amount=None,
					is_irrevocable=is_irrevocable,
					source_url=source_url,
					raw={
						'product_name': product_name,
						'term_text': term_text,
						'rate_cell': cells[idx + 1],
					},
				)
			)
	return offers


def fetch_bnb_offers() -> list[ParsedDepositOffer]:
	listing_html = fetch_text(BNB_LISTING_URL, timeout=90)
	products = parse_bnb_listing(listing_html)
	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()
	for title, path, is_irrevocable in products:
		# Prefer online variants; still include offline pages if present.
		url = urljoin(BNB_BASE, path)
		time.sleep(REQUEST_DELAY_SECONDS)
		detail_html = fetch_text(url, timeout=90)
		for offer in parse_bnb_rate_table(
			detail_html,
			product_name=title,
			source_url=url,
			is_irrevocable=is_irrevocable,
		):
			if offer.external_id in seen:
				continue
			seen.add(offer.external_id)
			offers.append(offer)
	# Fallback: if detail tables failed, keep listing summary cards.
	if not offers:
		for title, path, is_irrevocable in products:
			card_match = None
			for card in LISTING_CARD_RE.findall(listing_html):
				if path in card:
					card_match = card
					break
			rate_text = ''
			if card_match:
				info = re.search(r'deposits-item__info[^>]*>([\s\S]*?)</div>', card_match, re.I)
				rate_text = strip_tags(info.group(1)) if info else ''
			rate_pct, rate_pct_max, _ = parse_rate_value(rate_text)
			external_id = slugify(title)[:200]
			offers.append(
				ParsedDepositOffer(
					external_id=external_id,
					name=title,
					currency='BYN',
					rate_pct=rate_pct,
					rate_pct_max=rate_pct_max,
					term_text='',
					term_days_min=None,
					term_days_max=None,
					min_amount=None,
					is_irrevocable=is_irrevocable,
					source_url=urljoin(BNB_BASE, path),
					raw={'rate_text': rate_text},
				)
			)
	return offers
