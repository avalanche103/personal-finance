from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from apps.products.models import Product
from apps.products.services.token_terms import TokenTermsRow

CASTLE_CALENDAR_URL = 'https://castle.by/calendar/'
CASTLE_BASE_URL = 'https://castle.by'
DEFAULT_USER_AGENT = 'PersonalFinanceDashboard/1.0 (+local sync)'
REQUEST_DELAY_SECONDS = 1.0
MAX_FETCH_RETRIES = 4

BOND_LINK_PATTERN = re.compile(r'href="(/bond/(\d+)/)"', re.I)
CALENDAR_BOND_LINK_PATTERN = re.compile(
	r'href="/bond/(\d+)/"[^>]*>\s*([^<]+?)\s*</a>',
	re.I | re.S,
)
TOKEN_PATTERN = re.compile(
	r'([A-Z][A-Z0-9_]*_\([A-Z]{3}_\d+\)'
	r'|[A-Z][A-Z0-9.]+(?:USD|BYN|EUR|RUB)\.\d{4}\.\d+'
	r'|[A-Z]+\d+[A-Z]{3}\d+)',
	re.I,
)
PARAM_PATTERN = re.compile(
	r'bond-param-label">([^<]+)</td>\s*<td[^>]*>([^<]+)',
	re.I,
)
PLATFORM_PATTERN = re.compile(r'\b(Finstore|Fainex|BynEX|Bynex)\b', re.I)

SCHEDULE_MAP = {
	'ежемесяч': Product.IncomeSchedule.MONTHLY,
	'ежеквартал': Product.IncomeSchedule.QUARTERLY,
	'полугод': Product.IncomeSchedule.SEMI_ANNUAL,
	'ежегод': Product.IncomeSchedule.ANNUAL,
	'погашен': Product.IncomeSchedule.AT_MATURITY,
}


@dataclass
class CastleBondDetails:
	bond_id: str
	external_id: str
	platform: str
	annual_rate_pct: Decimal | None
	maturity_date: date | None
	income_schedule: str
	source_url: str


def _fetch_html(url: str, *, timeout: int = 60) -> str:
	last_error: Exception | None = None
	for attempt in range(MAX_FETCH_RETRIES):
		try:
			request = Request(url, headers={'User-Agent': DEFAULT_USER_AGENT})
			with urlopen(request, timeout=timeout) as response:
				return response.read().decode('utf-8', errors='replace')
		except HTTPError as exc:
			last_error = exc
			if exc.code == 429 and attempt < MAX_FETCH_RETRIES - 1:
				time.sleep(2 ** attempt)
				continue
			raise
		except URLError as exc:
			last_error = exc
			raise
	if last_error:
		raise last_error
	raise RuntimeError(f'Failed to fetch {url}')


def _parse_decimal_rate(value: str) -> Decimal | None:
	text = (value or '').strip().replace('%', '').replace(',', '.')
	if not text:
		return None
	try:
		return Decimal(text)
	except InvalidOperation:
		return None


def _parse_date(value: str) -> date | None:
	text = re.sub(r'\s+', ' ', (value or '').strip())
	if not text:
		return None
	for fmt in ('%d/%m/%Y', '%d.%m.%Y', '%Y-%m-%d'):
		try:
			return datetime.strptime(text[:10], fmt).date()
		except ValueError:
			continue
	return None


def _parse_schedule(value: str) -> str:
	text = (value or '').strip().lower()
	if not text:
		return ''
	for needle, schedule in SCHEDULE_MAP.items():
		if needle in text:
			return schedule
	return ''


def _normalize_label(label: str) -> str:
	return re.sub(r'\s+', ' ', label.strip().lower())


def _is_rate_label(label: str) -> bool:
	text = _normalize_label(label)
	return text == 'ставка' or (text.startswith('ставка') and 'оферт' not in text)


def _is_maturity_label(label: str) -> bool:
	text = _normalize_label(label)
	return text == 'погашение'


def _is_schedule_label(label: str) -> bool:
	return 'периодичность' in _normalize_label(label)


def _extract_external_id(text: str) -> str:
	match = TOKEN_PATTERN.search(text)
	return match.group(1) if match else ''


def _extract_token_id(html: str) -> str:
	for pattern in (
		r'информация по токену\s+([A-Z0-9_().]+)',
		r'токен[уа]?\s+([A-Z][A-Z0-9_().]+)',
	):
		match = re.search(pattern, html, re.I)
		if match:
			candidate = match.group(1).strip()
			if TOKEN_PATTERN.fullmatch(candidate) or '_' in candidate or '.' in candidate:
				return candidate
	return _extract_external_id(html)


def _extract_platform(html: str) -> str:
	match = PLATFORM_PATTERN.search(html)
	if not match:
		return ''
	return match.group(1).lower().replace('bynex', 'bynex')


def parse_bond_page(html: str, *, bond_id: str, source_url: str) -> CastleBondDetails | None:
	external_id = _extract_token_id(html)
	if not external_id:
		return None

	annual_rate_pct = None
	maturity_date = None
	income_schedule = ''

	for raw_label, raw_value in PARAM_PATTERN.findall(html):
		label = raw_label.strip()
		value = re.sub(r'\s+', ' ', raw_value.strip())
		if _is_rate_label(label):
			annual_rate_pct = _parse_decimal_rate(value)
		elif _is_maturity_label(label):
			maturity_date = _parse_date(value)
		elif _is_schedule_label(label):
			income_schedule = _parse_schedule(value)

	return CastleBondDetails(
		bond_id=bond_id,
		external_id=external_id,
		platform=_extract_platform(html),
		annual_rate_pct=annual_rate_pct,
		maturity_date=maturity_date,
		income_schedule=income_schedule,
		source_url=source_url,
	)


def fetch_calendar_bond_index(*, calendar_url: str = CASTLE_CALENDAR_URL) -> dict[str, str]:
	"""Map token external_id -> latest bond id from calendar hyperlinks."""
	html = _fetch_html(calendar_url)
	index: dict[str, str] = {}

	for bond_id, link_text in CALENDAR_BOND_LINK_PATTERN.findall(html):
		external_id = _extract_external_id(link_text)
		if not external_id:
			continue
		current = index.get(external_id)
		if current is None or int(bond_id) > int(current):
			index[external_id] = bond_id

	if not index:
		for _href, bond_id in BOND_LINK_PATTERN.findall(html):
			index.setdefault(bond_id, bond_id)

	return index


def fetch_calendar_bond_ids(*, calendar_url: str = CASTLE_CALENDAR_URL) -> list[str]:
	return list(fetch_calendar_bond_index(calendar_url=calendar_url).values())


def fetch_bond_details(
	bond_id: str,
	*,
	base_url: str = CASTLE_BASE_URL,
) -> CastleBondDetails | None:
	source_url = f'{base_url.rstrip("/")}/bond/{bond_id}/'
	html = _fetch_html(source_url)
	return parse_bond_page(html, bond_id=bond_id, source_url=source_url)


def _details_to_terms_row(details: CastleBondDetails) -> TokenTermsRow:
	token_id = ''
	match = re.search(r'_(\d+)\)?$', details.external_id)
	if match:
		token_id = match.group(1)
	elif details.external_id.rsplit('_', 1)[-1].isdigit():
		token_id = details.external_id.rsplit('_', 1)[-1]

	return TokenTermsRow(
		token_id=token_id,
		external_id=details.external_id,
		annual_rate_pct=details.annual_rate_pct,
		maturity_date=details.maturity_date,
		income_schedule=details.income_schedule,
	)


def fetch_castle_token_terms(
	*,
	calendar_url: str | None = None,
	platform: str | None = 'finstore',
	target_external_ids: set[str] | None = None,
	limit: int | None = None,
	request_delay: float = REQUEST_DELAY_SECONDS,
) -> tuple[list[TokenTermsRow], list[str]]:
	"""Scrape castle.by: one calendar request, then bond pages for matched tokens only."""
	platform_filter = (platform or '').strip().lower()
	errors: list[str] = []
	by_external_id: dict[str, CastleBondDetails] = {}

	url = (calendar_url or CASTLE_CALENDAR_URL).strip()
	calendar_index = fetch_calendar_bond_index(calendar_url=url)

	if target_external_ids:
		bond_jobs = [
			(calendar_index[external_id], external_id)
			for external_id in sorted(target_external_ids)
			if external_id in calendar_index
		]
	else:
		bond_jobs = [(bond_id, external_id) for external_id, bond_id in sorted(calendar_index.items())]

	if limit is not None:
		bond_jobs = bond_jobs[:limit]

	for index, (bond_id, expected_external_id) in enumerate(bond_jobs):
		if index and request_delay:
			time.sleep(request_delay)
		try:
			details = fetch_bond_details(bond_id)
		except (HTTPError, URLError) as exc:
			errors.append(f'bond/{bond_id}: {exc}')
			continue

		if details is None:
			errors.append(f'bond/{bond_id}: token not detected')
			continue

		if platform_filter and details.platform != platform_filter:
			continue

		if expected_external_id and details.external_id != expected_external_id:
			errors.append(
				f'bond/{bond_id}: expected {expected_external_id}, got {details.external_id}'
			)

		by_external_id[details.external_id] = details

	rows = [_details_to_terms_row(item) for item in by_external_id.values()]
	return rows, errors
