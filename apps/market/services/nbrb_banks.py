from __future__ import annotations

import re
from dataclasses import dataclass

from django.db import transaction
from django.utils.text import slugify

from apps.institutions.models import FinancialInstitution
from apps.market.models import DepositBank
from apps.market.services.deposit_offers.bank_sources import BANK_MATCHERS, WEBSITE_BY_PARSER
from apps.market.services.deposit_offers.base import fetch_text, strip_tags

NBRB_BANKS_URL = 'https://www.nbrb.by/system/banks/list'

ROW_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.I | re.S)
CELL_RE = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.I | re.S)
SHORT_NAME_RE = re.compile(r'\(([^)]+)\)\s*$')
REG_NUMBER_RE = re.compile(r'^\s*(\d+)\s*,')
SWIFT_RE = re.compile(r'\b([A-Z]{4}\s*BY\s*[A-Z0-9]{2}(?:\s*[A-Z0-9]{3})?)\b', re.I)
WEBSITE_RE = re.compile(r'https?://[^\s\"\'<>]+', re.I)


@dataclass(frozen=True)
class ParsedNbrbBank:
	name: str
	short_name: str
	reg_number: str
	swift: str
	address: str
	phone: str
	website: str
	entity_type: str
	external_key: str


def _extract_short_name(full_name: str) -> str:
	match = SHORT_NAME_RE.search(full_name)
	if match:
		return match.group(1).strip()
	return full_name.strip()


def _classify_entity(name: str) -> str:
	lower = name.lower()
	if 'нкфо' in lower or 'небанковск' in lower:
		return DepositBank.EntityType.NKFO
	if 'банк' in lower:
		return DepositBank.EntityType.BANK
	return DepositBank.EntityType.OTHER


def parse_nbrb_banks_html(html: str) -> list[ParsedNbrbBank]:
	banks: list[ParsedNbrbBank] = []
	seen: set[str] = set()
	for index, row_html in enumerate(ROW_RE.findall(html), start=1):
		cells = [strip_tags(cell) for cell in CELL_RE.findall(row_html)]
		if len(cells) < 4:
			continue
		name = cells[0]
		if not name or name.lower().startswith('полное наименован'):
			continue
		name = re.sub(r'^\d+\.\s*', '', name).strip()
		reg_cell = cells[1]
		reg_match = REG_NUMBER_RE.search(reg_cell)
		reg_number = reg_match.group(1) if reg_match else ''
		address = cells[3] if len(cells) > 3 else ''
		phone_cell = cells[4] if len(cells) > 4 else ''
		swift_match = SWIFT_RE.search(phone_cell) or SWIFT_RE.search(reg_cell)
		swift = ''
		if swift_match:
			swift = re.sub(r'\s+', '', swift_match.group(1).upper())
		short_name = _extract_short_name(name)
		website = ''
		website_match = WEBSITE_RE.search(row_html)
		if website_match:
			website = website_match.group(0).rstrip('.,);')
		if reg_number:
			external_key = f'nbrb-{reg_number}'
		else:
			slug = slugify(short_name, allow_unicode=True) or slugify(name, allow_unicode=True) or str(index)
			external_key = f'nbrb-{slug}'
		if external_key in seen:
			continue
		seen.add(external_key)
		banks.append(
			ParsedNbrbBank(
				name=name,
				short_name=short_name,
				reg_number=reg_number,
				swift=swift,
				address=address,
				phone=phone_cell,
				website=website,
				entity_type=_classify_entity(name),
				external_key=external_key,
			)
		)
	return banks


def _match_parser_and_institution(parsed: ParsedNbrbBank) -> tuple[str, FinancialInstitution | None]:
	haystack = f'{parsed.name} {parsed.short_name}'.lower()
	for needles, institution_slug, parser_code in BANK_MATCHERS:
		if any(needle in haystack for needle in needles):
			institution = None
			if institution_slug:
				institution = FinancialInstitution.objects.filter(slug=institution_slug).first()
			return parser_code, institution
	return '', None


def assign_parsers_to_existing_banks() -> int:
	"""Backfill parser_code/website for already synced DepositBank rows."""
	updated = 0
	for bank in DepositBank.objects.filter(is_active=True, entity_type=DepositBank.EntityType.BANK):
		haystack = f'{bank.name} {bank.short_name}'.lower()
		matched_code = ''
		for needles, _institution_slug, parser_code in BANK_MATCHERS:
			if any(needle in haystack for needle in needles):
				matched_code = parser_code
				break
		if not matched_code:
			continue
		changed = False
		if bank.parser_code != matched_code:
			bank.parser_code = matched_code
			changed = True
		website = WEBSITE_BY_PARSER.get(matched_code, '')
		if website and bank.website != website:
			bank.website = website
			changed = True
		if changed:
			bank.save(update_fields=['parser_code', 'website', 'updated_at'])
			updated += 1
	return updated


@transaction.atomic
def sync_nbrb_banks(*, html: str | None = None) -> dict:
	if html is None:
		html = fetch_text(NBRB_BANKS_URL, timeout=90)
	parsed_banks = parse_nbrb_banks_html(html)
	created = 0
	updated = 0
	seen_keys: set[str] = set()

	for parsed in parsed_banks:
		seen_keys.add(parsed.external_key)
		parser_code, institution = _match_parser_and_institution(parsed)

		bank = DepositBank.objects.filter(external_key=parsed.external_key).first()
		if bank is None and parser_code:
			bank = DepositBank.objects.filter(parser_code=parser_code).first()
		if bank is None and institution is not None:
			bank = DepositBank.objects.filter(institution=institution).first()

		defaults = {
			'name': parsed.name,
			'short_name': parsed.short_name,
			'reg_number': parsed.reg_number,
			'swift': parsed.swift,
			'address': parsed.address,
			'phone': parsed.phone,
			'entity_type': parsed.entity_type,
			'external_key': parsed.external_key,
			'nbrb_url': NBRB_BANKS_URL,
			'is_active': True,
			'metadata': {
				'source': 'nbrb-banks-list',
			},
		}
		if parser_code:
			defaults['parser_code'] = parser_code
			defaults['website'] = WEBSITE_BY_PARSER.get(parser_code, parsed.website)
		elif parsed.website:
			defaults['website'] = parsed.website
		if institution is not None:
			defaults['institution'] = institution

		if bank is None:
			DepositBank.objects.create(**defaults)
			created += 1
		else:
			for field, value in defaults.items():
				setattr(bank, field, value)
			bank.save()
			updated += 1

	deactivated = (
		DepositBank.objects.filter(metadata__source='nbrb-banks-list', is_active=True)
		.exclude(external_key__in=seen_keys)
		.update(is_active=False)
	)
	backfilled = assign_parsers_to_existing_banks()

	return {
		'fetched': len(parsed_banks),
		'created': created,
		'updated': updated,
		'deactivated': deactivated,
		'parsers_assigned': backfilled,
	}
