from __future__ import annotations

import re
import time
from datetime import datetime
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

BELAPB_BASE = 'https://www.belapb.by'
BELAPB_LISTING_URL = f'{BELAPB_BASE}/chastnomu-klientu/sberezheniya/vklady-i-scheta/'

PRODUCT_HREF_RE = re.compile(
	r'href=[\"\'](/chastnomu-klientu/sberezheniya/vklady-i-scheta/([^\"\'?#]+)/?)[\"\']',
	re.I,
)
H1_RE = re.compile(r'<h1[^>]*>([\s\S]*?)</h1>', re.I)
DATE_BLOCK_RE = re.compile(
	r'с\s+(\d{2}\.\d{2}\.\d{4})([\s\S]{0,2500}?)(?=с\s+\d{2}\.\d{2}\.\d{4}|$)',
	re.I,
)
TERM_RATE_RE = re.compile(
	r'(\d{1,4})\s*дн(?:ей|я|ь)\s+(\d+[.,]\d+|\d+)\s*%',
	re.I,
)
MIN_AMOUNT_RE = re.compile(
	r'(?:сумма\s+от|от)\s*([\d\s]+)\s*(BYN|USD|EUR|RUB|CNY|бел|долл|евро|рос)',
	re.I,
)

SKIP_SLUG_PARTS = (
	'zaklyuchenie-novykh',
	'ne-osushchestvlyaetsya',
	'priostanovleno',
	'filter',
	'payment',
	'garanti',
	'blagotvoritel',
	'tekushchie',
	'clear',
	'apply',
	'pagen',
)


def _decimal(raw: str) -> Decimal | None:
	try:
		return Decimal(raw.replace(',', '.').replace(' ', ''))
	except (InvalidOperation, AttributeError):
		return None


def _clean(text: str) -> str:
	return re.sub(r'\s+', ' ', strip_tags(text or '')).strip()


def _currency_from_url(url: str, name: str = '') -> str:
	blob = f'{url} {name}'.lower()
	if any(token in blob for token in ('dollar', 'usd', 'сша')):
		return 'USD'
	if any(token in blob for token in ('evro', 'eur', 'евро')):
		return 'EUR'
	if any(token in blob for token in ('cny', 'yuan', 'юан')):
		return 'CNY'
	if any(
		token in blob
		for token in (
			'rossij',
			'rossiy',
			'россий',
			'рос-руб',
			'ros-rub',
		)
	):
		return 'RUB'
	# «belorusskih-rublyah» is BYN, not RUB.
	if any(
		token in blob
		for token in (
			'beloruss',
			'белорус',
			'бел. руб',
			'byn',
		)
	):
		return 'BYN'
	return 'BYN'


def discover_belapb_product_urls(listing_html: str, *, listing_url: str = BELAPB_LISTING_URL) -> list[str]:
	found: list[str] = []
	seen: set[str] = set()
	for path, slug in PRODUCT_HREF_RE.findall(listing_html):
		slug_l = slug.lower().strip('/')
		if not slug_l or slug_l == 'vklady-i-scheta':
			continue
		if any(part in slug_l for part in SKIP_SLUG_PARTS):
			continue
		if 'vklad' not in slug_l and 'depozit' not in slug_l and 'plyus' not in slug_l:
			continue
		full = urljoin(listing_url, path if path.endswith('/') else path + '/')
		if full in seen:
			continue
		seen.add(full)
		found.append(full)
	return found


def _parse_date(raw: str) -> datetime | None:
	try:
		return datetime.strptime(raw, '%d.%m.%Y')
	except ValueError:
		return None


def _current_rate_schedule(html: str) -> list[tuple[str, Decimal]]:
	"""Pick the newest dated schedule on the page and return (term_text, rate) rows."""
	text = _clean(
		re.sub(r'<script\b[^>]*>[\s\S]*?</script>|<style\b[^>]*>[\s\S]*?</style>', ' ', html, flags=re.I)
	)
	blocks: list[tuple[datetime, str]] = []
	for date_raw, body in DATE_BLOCK_RE.findall(text):
		dt = _parse_date(date_raw)
		if dt:
			blocks.append((dt, body))
	if not blocks:
		# Fallback: whole page pairs (may mix archives).
		body = text
	else:
		blocks.sort(key=lambda item: item[0], reverse=True)
		body = blocks[0][1]

	pairs: list[tuple[str, Decimal]] = []
	seen: set[str] = set()
	for days_raw, rate_raw in TERM_RATE_RE.findall(body):
		rate = _decimal(rate_raw)
		if rate is None or rate <= 0 or rate > 20:
			continue
		days = int(days_raw)
		if days <= 0:
			continue
		term_text = f'{days} дней' if days != 1 else '1 день'
		if term_text in seen:
			continue
		seen.add(term_text)
		pairs.append((term_text, rate))
	return pairs


def parse_belapb_product_page(html: str, *, source_url: str) -> list[ParsedDepositOffer]:
	path = urlparse(source_url).path.lower()
	if any(part in path for part in SKIP_SLUG_PARTS):
		return []

	h1 = H1_RE.search(html)
	name = _clean(h1.group(1)) if h1 else ''
	if not name or 'не осуществляется' in name.lower():
		return []
	if not any(token in name.lower() for token in ('вклад', 'депозит', 'плюс')):
		return []

	currency = _currency_from_url(source_url, name)
	schedule = _current_rate_schedule(html)
	if not schedule:
		return []

	min_amount = None
	min_match = MIN_AMOUNT_RE.search(_clean(html))
	if min_match:
		min_amount = parse_min_amount(min_match.group(1))

	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()
	for term_text, rate in schedule:
		term_min, term_max = parse_term_days(term_text)
		external_id = slugify(
			f'belapb-{name}-{currency}-{term_text}-{rate}',
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
				term_days_min=term_min,
				term_days_max=term_max,
				min_amount=min_amount,
				is_irrevocable=detect_irrevocable(name),
				source_url=source_url,
				raw={'parser': 'belapb'},
			)
		)
	return offers


def fetch_belapb_offers() -> list[ParsedDepositOffer]:
	listing_html = fetch_text(BELAPB_LISTING_URL, timeout=90, insecure_ssl=True)
	urls = discover_belapb_product_urls(listing_html)
	# Listing is paginated; pull a couple of extra pages when present.
	for page in (2, 3, 4):
		page_url = f'{BELAPB_LISTING_URL}?PAGEN_1={page}'
		try:
			page_html = fetch_text(page_url, timeout=90, insecure_ssl=True)
		except Exception:
			break
		for url in discover_belapb_product_urls(page_html, listing_url=BELAPB_LISTING_URL):
			if url not in urls:
				urls.append(url)
		time.sleep(REQUEST_DELAY_SECONDS)

	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()
	for url in urls:
		time.sleep(REQUEST_DELAY_SECONDS)
		try:
			html = fetch_text(url, timeout=90, insecure_ssl=True)
		except Exception:
			continue
		for offer in parse_belapb_product_page(html, source_url=url):
			if offer.external_id in seen:
				continue
			seen.add(offer.external_id)
			offers.append(offer)
	return offers
