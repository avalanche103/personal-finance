from __future__ import annotations

import re
import ssl
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_USER_AGENT = 'Mozilla/5.0 (compatible; PersonalFinance/1.0; +local sync)'
REQUEST_DELAY_SECONDS = 1.0
MAX_FETCH_RETRIES = 5
RATE_LIMIT_BACKOFF_SECONDS = (15, 30, 60, 90, 120)

_INSECURE_SSL_CONTEXT = ssl.create_default_context()
_INSECURE_SSL_CONTEXT.check_hostname = False
_INSECURE_SSL_CONTEXT.verify_mode = ssl.CERT_NONE


def fetch_text(
	url: str,
	*,
	timeout: int = 60,
	encoding: str | None = None,
	insecure_ssl: bool = False,
) -> str:
	last_error: Exception | None = None
	context = _INSECURE_SSL_CONTEXT if insecure_ssl else None
	for attempt in range(MAX_FETCH_RETRIES):
		try:
			request = Request(url, headers={'User-Agent': DEFAULT_USER_AGENT})
			with urlopen(request, timeout=timeout, context=context) as response:
				raw = response.read()
				charset = encoding
				if charset is None:
					content_type = response.headers.get_content_charset()
					charset = content_type or 'utf-8'
				return raw.decode(charset, errors='replace')
		except HTTPError as exc:
			last_error = exc
			if exc.code == 429 and attempt < MAX_FETCH_RETRIES - 1:
				backoff = RATE_LIMIT_BACKOFF_SECONDS[
					min(attempt, len(RATE_LIMIT_BACKOFF_SECONDS) - 1)
				]
				time.sleep(backoff)
				continue
			raise
		except URLError as exc:
			# Retry once with insecure SSL when certificate chain is incomplete.
			if not insecure_ssl and 'CERTIFICATE_VERIFY_FAILED' in str(exc):
				return fetch_text(
					url,
					timeout=timeout,
					encoding=encoding,
					insecure_ssl=True,
				)
			last_error = exc
			raise
	if last_error:
		raise last_error
	raise RuntimeError(f'Failed to fetch {url}')


def strip_tags(html: str) -> str:
	text = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
	text = re.sub(r'<[^>]+>', ' ', text)
	text = unescape(text)
	return re.sub(r'\s+', ' ', text).strip()


def parse_rate_value(value: str) -> tuple[Decimal | None, Decimal | None, bool]:
	"""Return (rate_pct, rate_pct_max, is_up_to)."""
	text = unescape(value or '').strip().lower().replace('\xa0', ' ')
	text = text.replace('%', ' ').replace(',', '.')
	is_up_to = 'до' in text or 'up to' in text
	numbers = re.findall(r'\d+(?:\.\d+)?', text)
	if not numbers:
		return None, None, is_up_to
	values: list[Decimal] = []
	for item in numbers:
		try:
			values.append(Decimal(item))
		except InvalidOperation:
			continue
	if not values:
		return None, None, is_up_to
	if is_up_to:
		return None, max(values), True
	if len(values) == 1:
		return values[0], values[0], False
	return min(values), max(values), False


def parse_min_amount(value) -> Decimal | None:
	if value is None or value == '':
		return None
	if isinstance(value, (int, float, Decimal)):
		try:
			amount = Decimal(str(value))
		except InvalidOperation:
			return None
		return None if amount == 0 else amount
	text = str(value).replace('\xa0', ' ').replace(' ', '').replace(',', '.')
	match = re.search(r'\d+(?:\.\d+)?', text)
	if not match:
		return None
	try:
		amount = Decimal(match.group(0))
	except InvalidOperation:
		return None
	return None if amount == 0 else amount


_TERM_DAY_RE = re.compile(
	r'(?:от\s+)?(\d+)\s*(?:до\s+(\d+)\s*)?(ден|дн|день|дня|дней|мес|месяц|год|лет|года)',
	re.I,
)


def parse_term_days(term_text: str) -> tuple[int | None, int | None]:
	text = (term_text or '').lower().replace('\xa0', ' ')
	matches = list(_TERM_DAY_RE.finditer(text))
	if not matches:
		return None, None

	def to_days(amount: int, unit: str) -> int:
		unit = unit.lower()
		if unit.startswith('д'):
			return amount
		if unit.startswith('мес'):
			return amount * 30
		return amount * 365

	mins: list[int] = []
	maxs: list[int] = []
	for match in matches:
		low = int(match.group(1))
		high = int(match.group(2)) if match.group(2) else low
		unit = match.group(3)
		mins.append(to_days(low, unit))
		maxs.append(to_days(high, unit))
	return min(mins), max(maxs)


def detect_irrevocable(name: str) -> bool | None:
	text = (name or '').lower()
	if 'безотзыв' in text:
		return True
	if 'отзыв' in text:
		return False
	return None


@dataclass(frozen=True)
class ParsedDepositOffer:
	external_id: str
	name: str
	currency: str
	rate_pct: Decimal | None
	rate_pct_max: Decimal | None
	term_text: str
	term_days_min: int | None
	term_days_max: int | None
	min_amount: Decimal | None
	is_irrevocable: bool | None
	source_url: str
	raw: dict


_TERM_UNIT_TOKEN_RE = re.compile(
	r'(\d+)\s*(ден(?:ь|я|ей)?|дн\.?|мес(?:яц(?:а|ев)?)?|год(?:а|ов)?|лет)?',
	re.I,
)


def _normalize_term_unit(amount: int, unit: str) -> str:
	unit = (unit or '').lower()
	if unit.startswith('д'):
		if amount == 1:
			return 'день'
		if 2 <= amount <= 4:
			return 'дня'
		return 'дней'
	if unit.startswith('мес'):
		if amount == 1:
			return 'месяц'
		if 2 <= amount <= 4:
			return 'месяца'
		return 'месяцев'
	if amount == 1:
		return 'год'
	if 2 <= amount <= 4:
		return 'года'
	return 'лет'


def split_discrete_terms(term_text: str) -> list[str]:
	"""
	Split listing copy like «13 и 37 месяцев» / «60 или 100 дней, 5, 7, 13 месяцев»
	into one concrete term per item.
	"""
	text = re.sub(r'\s+', ' ', (term_text or '').strip())
	if not text:
		return ['']

	low = text.lower()
	# Continuous «от X до Y» is handled later by concretize_term (single bound).
	if re.search(r'\bот\s+\d+', low) and re.search(r'\bдо\s+\d+', low):
		if not re.search(r'\b(и|или)\b', low):
			return [text]

	# Need an explicit list separator to expand.
	if not re.search(r'\b(и|или)\b|,', low):
		return [text]

	tokens = _TERM_UNIT_TOKEN_RE.findall(text)
	if len(tokens) <= 1:
		return [text]

	pending: list[int] = []
	pairs: list[tuple[int, str]] = []
	for raw_amount, raw_unit in tokens:
		amount = int(raw_amount)
		if raw_unit:
			pending.append(amount)
			for value in pending:
				pairs.append((value, raw_unit))
			pending = []
		else:
			pending.append(amount)

	if not pairs:
		return [text]

	seen: set[str] = set()
	result: list[str] = []
	for amount, unit in pairs:
		label = f'{amount} {_normalize_term_unit(amount, unit)}'
		if label not in seen:
			seen.add(label)
			result.append(label)
	return result or [text]


def format_term_days(days: int) -> str:
	"""Single concrete term label from a day count (always in days)."""
	if days <= 0:
		return ''
	return f'{days} {_normalize_term_unit(days, "день")}'


def format_term_months(months: int) -> str:
	if months <= 0:
		return ''
	return f'{months} {_normalize_term_unit(months, "мес")}'


_RANGE_TERM_RE = re.compile(
	r'(?:от\s+)?(\d+)\s*(?:ден|дн|день|дня|дней|мес|месяц|год|лет|года)?'
	r'.{0,20}?(?:до|–|—|-)\s*(\d+)\s*(ден|дн|день|дня|дней|мес|месяц|год|лет|года)',
	re.I,
)
_LAST_TERM_TOKEN_RE = re.compile(
	r'(\d+)\s*(ден(?:ь|я|ей)?|дн\.?|мес(?:яц(?:а|ев)?)?|год(?:а|ов)?|лет)',
	re.I,
)


def concretize_term(
	term_text: str,
	term_days_min: int | None = None,
	term_days_max: int | None = None,
) -> tuple[str, int, int] | None:
	"""
	Force one concrete term (no «от…до», no «35–1100»).
	For a published tariff band use the upper bound as the single term.
	"""
	text = re.sub(r'\s+', ' ', (term_text or '').strip())
	tokens = list(_LAST_TERM_TOKEN_RE.finditer(text))
	if tokens:
		amount = int(tokens[-1].group(1))
		unit = tokens[-1].group(2).lower()
		if amount <= 0:
			return None
		if unit.startswith('мес'):
			days = amount * 30
			return format_term_months(amount), days, days
		if unit.startswith('д'):
			return format_term_days(amount), amount, amount
		days = amount * 365
		label = f'{amount} {_normalize_term_unit(amount, "год")}'
		return label, days, days

	low = term_days_min
	high = term_days_max
	parsed_low, parsed_high = parse_term_days(text) if text else (None, None)
	if low is None:
		low = parsed_low
	if high is None:
		high = parsed_high
	if low is None and high is None:
		return None
	if low is None:
		low = high
	if high is None:
		high = low
	assert low is not None and high is not None
	days = max(int(low), int(high))
	label = format_term_days(days)
	if not label:
		return None
	return label, days, days


_VAGUE_PRODUCT_NAMES = (
	'вклады онлайн',
	'безотзывные вклады',
	'отзывные вклады',
	'вклады в белорус',
	'вклады в россий',
	'краткосрочные вклады',
	'accounts and deposits',
	'рахункі і ўклады',
	'вклады и счета',
)


def is_vague_listing_offer(offer: ParsedDepositOffer) -> bool:
	"""Drop category/listing blurbs that are not a single product tariff."""
	name = (offer.name or '').strip().lower()
	if any(token in name for token in _VAGUE_PRODUCT_NAMES):
		return True
	term = (offer.term_text or '').lower()
	# Discrete «13 и 37 месяцев» lists are expanded — not vague.
	if re.search(r'\b(и|или)\b|,', term):
		parts = split_discrete_terms(offer.term_text)
		if len(parts) > 1:
			return False
	low, high = offer.term_days_min, offer.term_days_max
	if low is None or high is None:
		low2, high2 = parse_term_days(offer.term_text)
		low = low if low is not None else low2
		high = high if high is not None else high2
	# Generic category titles with a huge window and only «до X%» are listings, not products.
	has_product_mark = ('«' in (offer.name or '')) or ('"' in (offer.name or '')) or ('„' in (offer.name or ''))
	if (
		low is not None
		and high is not None
		and high - low >= 365
		and offer.rate_pct is None
		and offer.rate_pct_max is not None
		and not has_product_mark
	):
		return True
	return False


def expand_atomic_offers(offers: list[ParsedDepositOffer]) -> list[ParsedDepositOffer]:
	"""
	One stored row = one rate + one concrete term + one condition set.
	No «от…до» / «35–1100» term windows remain after this step.
	"""
	from django.utils.text import slugify

	expanded: list[ParsedDepositOffer] = []
	seen_content: set[tuple] = set()
	seen_ids: set[str] = set()

	for offer in offers:
		if is_vague_listing_offer(offer):
			continue
		terms = split_discrete_terms(offer.term_text)
		rate_pct = offer.rate_pct
		rate_pct_max = offer.rate_pct_max
		if (
			rate_pct is not None
			and rate_pct_max is not None
			and rate_pct != rate_pct_max
			and rate_pct < Decimal('3')
			and rate_pct_max >= Decimal('3')
		):
			rate_pct = rate_pct_max
		elif rate_pct is not None and rate_pct_max is None:
			rate_pct_max = rate_pct

		for term_text in terms:
			# Don't leak original multi-term day bounds into a split concrete term.
			if len(terms) > 1 or (term_text and term_text != (offer.term_text or '').strip()):
				concrete = concretize_term(term_text)
			else:
				concrete = concretize_term(
					term_text or offer.term_text,
					offer.term_days_min,
					offer.term_days_max,
				)
			if concrete is None:
				continue
			term_text, term_min, term_max = concrete

			content_key = (
				offer.name.strip().lower(),
				offer.currency.upper(),
				str(rate_pct),
				str(rate_pct_max),
				term_text.strip().lower(),
				offer.is_irrevocable,
				str(offer.min_amount),
			)
			if content_key in seen_content:
				continue
			seen_content.add(content_key)

			external_id = offer.external_id
			if len(terms) > 1 or term_text != (offer.term_text or '').strip():
				external_id = slugify(
					f'{offer.external_id}-{term_text}',
					allow_unicode=True,
				)[:200] or offer.external_id
			if external_id in seen_ids:
				external_id = slugify(
					f'{external_id}-{offer.currency}-{rate_pct or rate_pct_max}',
					allow_unicode=True,
				)[:200]
			if external_id in seen_ids:
				continue
			seen_ids.add(external_id)

			raw = dict(offer.raw or {})
			if term_text != (offer.term_text or '').strip():
				raw = {**raw, 'expanded_from_term': offer.term_text}

			expanded.append(
				ParsedDepositOffer(
					external_id=external_id,
					name=offer.name,
					currency=offer.currency,
					rate_pct=rate_pct,
					rate_pct_max=rate_pct_max,
					term_text=term_text,
					term_days_min=term_min,
					term_days_max=term_max,
					min_amount=offer.min_amount,
					is_irrevocable=offer.is_irrevocable,
					source_url=offer.source_url,
					raw=raw,
				)
			)
	return expanded


# Term filter buckets for the market UI (days).
TERM_FILTER_BUCKETS: dict[str, tuple[int | None, int | None]] = {
	'le90': (None, 90),
	'91-180': (91, 180),
	'181-365': (181, 365),
	'366-730': (366, 730),
	'gt730': (731, None),
}


def offer_matches_term_bucket(
	*,
	term_days_min: int | None,
	term_days_max: int | None,
	bucket: str,
) -> bool:
	bounds = TERM_FILTER_BUCKETS.get(bucket)
	if not bounds:
		return True
	bucket_min, bucket_max = bounds
	if term_days_min is None and term_days_max is None:
		return False
	low = term_days_min if term_days_min is not None else term_days_max
	high = term_days_max if term_days_max is not None else term_days_min
	assert low is not None and high is not None
	if bucket_min is not None and high < bucket_min:
		return False
	if bucket_max is not None and low > bucket_max:
		return False
	return True
