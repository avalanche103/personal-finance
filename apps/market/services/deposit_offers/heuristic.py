from __future__ import annotations

import re
import time
from decimal import Decimal
from urllib.parse import urljoin, urlparse

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

# Retail BYN deposits are currently ~5–15%; reject insurance "100%" and similar noise.
MAX_PLAUSIBLE_RATE = Decimal('20')
MIN_PLAUSIBLE_RATE = Decimal('0.01')

SKIP_PATH_PARTS = (
	'compare',
	'sravnen',
	'filter',
	'calculator',
	'faq',
	'garanti',
	'гарант',
	'press',
	'news',
	'blog',
	'literacy',
	'pdf',
	'ajax',
	'form_popup',
	'depozitar',
	'ячейк',
	'insurance',
	'strahov',
	'paket',
	'package',
	'service-pack',
	'cards-insurance',
	'ne-osushchestvlyaetsya',
	'zaklyuchenie-novykh',
	'priostanovleno',
)

JUNK_TITLE_PARTS = (
	'сравнен',
	'пакет',
	'сервис',
	'калькулятор',
	'гарант',
	'возмещен',
	'новост',
	'faq',
	'страхов',
	'ячейк',
	'депозитар',
	'не осуществляется',
	'валюта вклада',
	'тип вклада',
	'самые выгодные',
	'вопрос',
)

DEPOSIT_TITLE_HINTS = (
	'вклад',
	'депозит',
	'сберег',
	'сохраняй',
	'приумнож',
	'накоп',
	'online',
	'онлайн',
)

PRODUCT_HREF_RE = re.compile(r'href=\"([^\"]+)\"', re.I)
H1_RE = re.compile(r'<h1[^>]*>([\s\S]*?)</h1>', re.I)
TITLE_RE = re.compile(r'<title[^>]*>([\s\S]*?)</title>', re.I)
RATE_NEAR_LABEL_RE = re.compile(
	r'(?:ставк[аиуеы]|процент(?:н\w*)?\s*ставк|доходность|годовых)[^%]{0,60}?'
	r'((?:до\s*)?\d+[.,]\d+|\d+)\s*%',
	re.I,
)
ANY_RATE_RE = re.compile(r'((?:до\s*)?\d+[.,]\d+|\d+)\s*%', re.I)
TERM_RE = re.compile(
	r'(?:от\s+)?(\d+)\s*(?:до\s+(\d+)\s*)?(ден|дн|день|дня|дней|мес(?:яц(?:а|ев)?)?|год(?:а|ов)?|лет)',
	re.I,
)
CARD_BLOCK_RE = re.compile(
	r'(?:product-item|deposit(?:s)?-item|deposits-item|card-product|product-card|tariff-item)[^>]*>'
	r'([\s\S]{200,4000}?)</(?:div|article|section|li)>',
	re.I,
)


def _same_site(base: str, url: str) -> bool:
	b = urlparse(base)
	u = urlparse(url)
	if not u.netloc:
		return True
	return b.netloc.replace('www.', '') == u.netloc.replace('www.', '')


def _should_skip(url: str) -> bool:
	low = url.lower()
	return any(part in low for part in SKIP_PATH_PARTS)


def _is_depositish(url: str) -> bool:
	low = url.lower()
	return any(
		token in low
		for token in (
			'deposit',
			'vklad',
			'vklady',
			'депоз',
			'вклад',
			'sberezh',
			'sberezhen',
			'deposite',
		)
	)


def _is_plausible_rate(value: Decimal | None) -> bool:
	if value is None:
		return False
	try:
		return MIN_PLAUSIBLE_RATE <= value <= MAX_PLAUSIBLE_RATE
	except Exception:
		return False


def _normalize_rate_pair(
	rate_pct: Decimal | None,
	rate_pct_max: Decimal | None,
) -> tuple[Decimal | None, Decimal | None] | None:
	values = [v for v in (rate_pct, rate_pct_max) if v is not None]
	if not values:
		return None
	if not all(_is_plausible_rate(v) for v in values):
		return None
	if rate_pct is not None and rate_pct_max is not None and rate_pct == rate_pct_max:
		return rate_pct, rate_pct_max
	return rate_pct, rate_pct_max


def _is_deposit_title(name: str) -> bool:
	low = (name or '').lower()
	if len(low) < 4:
		return False
	if any(part in low for part in JUNK_TITLE_PARTS):
		return False
	# Prefer explicit deposit wording; otherwise keep short product-like titles from deposit URLs.
	return any(hint in low for hint in DEPOSIT_TITLE_HINTS)


def discover_product_urls(listing_html: str, listing_url: str, *, limit: int = 25) -> list[str]:
	found: list[str] = []
	seen: set[str] = set()
	for href in PRODUCT_HREF_RE.findall(listing_html):
		full = urljoin(listing_url, href.split('#')[0].split('?')[0])
		low = full.lower()
		if not _same_site(listing_url, full):
			continue
		if any(
			token in low
			for token in (
				'/_next/',
				'.js',
				'.css',
				'.png',
				'.jpg',
				'.svg',
				'/page/',
				'guarantee',
				'yachejk',
				'precious-metal',
			)
		):
			continue
		if not _is_depositish(full):
			continue
		if _should_skip(full):
			continue
		path = urlparse(full).path.strip('/')
		if not path or path.count('/') < 1:
			continue
		if full.rstrip('/') == listing_url.rstrip('/'):
			continue
		if full in seen:
			continue
		seen.add(full)
		found.append(full)
		if len(found) >= limit:
			break
	return found

def _extract_title(html: str) -> str:
	match = H1_RE.search(html)
	if match:
		title = strip_tags(match.group(1))
		if title:
			return re.sub(r'\s+', ' ', title).strip()
	match = TITLE_RE.search(html)
	if match:
		title = strip_tags(match.group(1))
		title = re.split(r'[|\-–—]', title)[0].strip()
		return re.sub(r'\s+', ' ', title).strip()
	return ''


def _visible_text(html: str) -> str:
	"""Strip scripts/styles before text extraction so CSS % values are ignored."""
	cleaned = re.sub(r'<script\b[^>]*>[\s\S]*?</script>', ' ', html, flags=re.I)
	cleaned = re.sub(r'<style\b[^>]*>[\s\S]*?</style>', ' ', cleaned, flags=re.I)
	return strip_tags(cleaned)


def _extract_best_rate(html: str) -> tuple[Decimal | None, Decimal | None] | None:
	"""Pick one plausible deposit rate from the page (never insurance 100%)."""
	candidates: list[tuple[Decimal | None, Decimal | None]] = []
	text = _visible_text(html)
	for raw in RATE_NEAR_LABEL_RE.findall(text)[:40]:
		pair = _normalize_rate_pair(*parse_rate_value(raw + '%')[:2])
		if pair:
			candidates.append(pair)
	if not candidates:
		for raw in ANY_RATE_RE.findall(text)[:40]:
			pair = _normalize_rate_pair(*parse_rate_value(raw + '%')[:2])
			if pair:
				candidates.append(pair)
	# Meta descriptions are often stale marketing («до 15,2%») — last resort only.
	if not candidates:
		meta = re.search(
			r'<meta[^>]+name=[\"\']description[\"\'][^>]+content=[\"\']([^\"\']+)[\"\']',
			html,
			re.I,
		)
		if meta:
			for raw in RATE_NEAR_LABEL_RE.findall(meta.group(1))[:5]:
				pair = _normalize_rate_pair(*parse_rate_value(raw + '%')[:2])
				if pair:
					candidates.append(pair)
			if not candidates:
				for raw in ANY_RATE_RE.findall(meta.group(1))[:5]:
					pair = _normalize_rate_pair(*parse_rate_value(raw + '%')[:2])
					if pair:
						candidates.append(pair)
	if not candidates:
		return None

	def sort_key(item: tuple[Decimal | None, Decimal | None]):
		low, high = item
		return high or low or Decimal('0')

	return max(candidates, key=sort_key)


def _extract_term(html_or_text: str) -> str:
	text = _visible_text(html_or_text) if '<' in html_or_text else strip_tags(html_or_text)
	from apps.market.services.deposit_offers.base import concretize_term, format_term_days

	for match in TERM_RE.finditer(text):
		low = match.group(1)
		high = match.group(2)
		if low.startswith('0') and low != '0':
			continue
		if int(low) <= 0:
			continue
		if high and (high.startswith('0') or int(high) <= 0):
			continue
		unit = match.group(3).lower()
		# Always emit a single bound (upper if range).
		amount = int(high or low)
		if unit.startswith('ден') or unit.startswith('дн'):
			return format_term_days(amount)
		if unit.startswith('мес'):
			return format_term_days(amount * 30)
		return format_term_days(amount * 365)
	concrete = concretize_term(text)
	return concrete[0] if concrete else ''

def _currency_from_context(name: str, url: str = '', snippet: str = '') -> str:
	blob = f'{name} {url} {snippet}'.lower()
	if any(token in blob for token in ('usd', 'доллар', 'долл', 'сша', 'dollar')):
		return 'USD'
	if any(token in blob for token in ('eur', 'евро', 'evro')):
		return 'EUR'
	if any(token in blob for token in ('cny', 'юан', 'yuan')):
		return 'CNY'
	# BYN before RUB: «belorusskih-rublyah» contains the «rub» substring.
	if any(token in blob for token in ('byn', 'бел. руб', 'белорус', 'beloruss', 'бел руб')):
		return 'BYN'
	if any(
		token in blob
		for token in ('рос. руб', 'российск', 'рос руб', 'rossij', 'rossiy', '/rub/', '-rub-')
	):
		return 'RUB'
	if re.search(r'(?<![a-zа-я])rub(?![a-zа-я])', blob):
		return 'RUB'
	return 'BYN'


def parse_product_page(html: str, *, source_url: str) -> list[ParsedDepositOffer]:
	name = _extract_title(html)
	if not name or not _is_deposit_title(name):
		return []
	rate_pair = _extract_best_rate(html)
	if not rate_pair:
		return []
	rate_pct, rate_pct_max = rate_pair
	term_text = _extract_term(html)
	term_min, term_max = parse_term_days(term_text)
	currency = _currency_from_context(name, source_url)
	external_id = slugify(f'{name}-{currency}-{term_text}', allow_unicode=True)[:200]
	if not external_id:
		external_id = slugify(f'{urlparse(source_url).path}-{currency}')[:200]
	return [
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
			is_irrevocable=detect_irrevocable(name + ' ' + source_url),
			source_url=source_url,
			raw={'parser': 'heuristic_spider'},
		)
	]


def parse_listing_cards(html: str, *, listing_url: str) -> list[ParsedDepositOffer]:
	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()

	blocks = CARD_BLOCK_RE.findall(html)
	if not blocks:
		blocks = re.findall(
			r'(<(?:h[1-3]|div)[^>]*(?:title|name|heading|product|deposit)[^>]*>'
			r'[\s\S]{0,1200}?((?:до\s*)?\d+[.,]\d+|\d+)\s*%)',
			html,
			re.I,
		)
		blocks = [item[0] if isinstance(item, tuple) else item for item in blocks]

	for block in blocks:
		text = strip_tags(block)
		rate_match = ANY_RATE_RE.search(text)
		if not rate_match:
			continue
		pair = _normalize_rate_pair(*parse_rate_value(rate_match.group(1) + '%')[:2])
		if not pair:
			continue
		rate_pct, rate_pct_max = pair
		title_match = re.search(r'<h[1-3][^>]*>([\s\S]*?)</h[1-3]>', block, re.I)
		if not title_match:
			title_match = re.search(r'(?:title|name|heading)[^>]*>([\s\S]*?)</', block, re.I)
		name = strip_tags(title_match.group(1)) if title_match else ''
		name = re.sub(r'\s+', ' ', name).strip()
		if not _is_deposit_title(name):
			continue
		href_match = PRODUCT_HREF_RE.search(block)
		source_url = urljoin(listing_url, href_match.group(1)) if href_match else listing_url
		if _should_skip(source_url):
			continue
		term_text = _extract_term(block)
		term_min, term_max = parse_term_days(term_text)
		currency = _currency_from_context(name, source_url, text[:200])
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
				raw={'parser': 'listing_cards'},
			)
		)
	return offers


def fetch_bank_offers(
	*,
	listing_urls: list[str],
	max_products: int = 20,
	crawl_details: bool = True,
	insecure_ssl: bool = False,
) -> list[ParsedDepositOffer]:
	offers: list[ParsedDepositOffer] = []
	seen: set[str] = set()
	product_urls: list[str] = []

	for listing_url in listing_urls:
		html = fetch_text(listing_url, timeout=90, insecure_ssl=insecure_ssl)
		for offer in parse_listing_cards(html, listing_url=listing_url):
			if offer.external_id in seen:
				continue
			seen.add(offer.external_id)
			offers.append(offer)
		if crawl_details:
			for url in discover_product_urls(html, listing_url, limit=max_products):
				if url not in product_urls:
					product_urls.append(url)
		time.sleep(REQUEST_DELAY_SECONDS)

	if crawl_details:
		for url in product_urls[:max_products]:
			try:
				html = fetch_text(url, timeout=90, insecure_ssl=insecure_ssl)
			except Exception:
				continue
			for offer in parse_product_page(html, source_url=url):
				if offer.external_id in seen:
					continue
				seen.add(offer.external_id)
				offers.append(offer)
			time.sleep(REQUEST_DELAY_SECONDS)

	return offers
