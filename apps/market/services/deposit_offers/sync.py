from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.market.models import DepositBank, DepositOffer
from apps.market.services.deposit_offers.base import ParsedDepositOffer, expand_atomic_offers
from apps.market.services.deposit_offers.registry import get_adapter

logger = logging.getLogger(__name__)

MAX_STORED_RATE = Decimal('20')
MIN_STORED_RATE = Decimal('0.01')


@dataclass
class BankSyncResult:
	bank_id: int
	bank_name: str
	parser_code: str
	offers: int
	created: int
	updated: int
	deactivated: int
	error: str = ''


@dataclass
class OfferSyncResult:
	banks: list[BankSyncResult]
	ok: int
	failed: int

	@property
	def total_offers(self) -> int:
		return sum(item.offers for item in self.banks if not item.error)


def _is_quality_offer(offer: ParsedDepositOffer) -> bool:
	"""Reject obvious scrape noise before persistence."""
	name = (offer.name or '').lower()
	if not name or len(name) < 4:
		return False
	junk = (
		'сравнен',
		'пакет сервис',
		'калькулятор',
		'гарант',
		'возмещен',
		'faq',
		'не осуществляется',
		'валюта вклада',
		'тип вклада',
		'самые выгодные',
	)
	if any(token in name for token in junk):
		return False
	term = (offer.term_text or '').lower()
	if re.search(r'\bот\s+\d+', term) and re.search(r'\bдо\s+\d+', term):
		return False
	if re.search(r'\d+\s*[–—-]\s*\d+\s*(ден|дн|мес|год|лет)', term):
		return False
	if offer.term_days_min is not None and offer.term_days_max is not None:
		if offer.term_days_min != offer.term_days_max:
			return False
	values = [v for v in (offer.rate_pct, offer.rate_pct_max) if v is not None]
	if not values:
		return False
	if any(v < MIN_STORED_RATE or v > MAX_STORED_RATE for v in values):
		return False
	return True


def _canonical_bank_for_parser(parser_code: str, banks: list[DepositBank]) -> DepositBank:
	"""Prefer portfolio-linked / oldest row when NBRB sync created duplicates."""
	matching = [b for b in banks if b.parser_code == parser_code]
	if not matching:
		raise ValueError(parser_code)
	matching.sort(key=lambda b: (0 if b.institution_id else 1, b.pk))
	return matching[0]


def _persist_offers(
	bank: DepositBank,
	parsed_offers: list[ParsedDepositOffer],
	*,
	dry_run: bool = False,
) -> tuple[int, int, int, int]:
	now = timezone.now()
	seen_ids: set[str] = set()
	created = 0
	updated = 0
	filtered = [offer for offer in parsed_offers if _is_quality_offer(offer)]
	sibling_ids = list(
		DepositBank.objects.filter(parser_code=bank.parser_code).values_list('pk', flat=True)
	)

	for offer in filtered:
		seen_ids.add(offer.external_id)
		defaults = {
			'name': offer.name,
			'currency': offer.currency,
			'rate_pct': offer.rate_pct,
			'rate_pct_max': offer.rate_pct_max,
			'term_text': offer.term_text,
			'term_days_min': offer.term_days_min,
			'term_days_max': offer.term_days_max,
			'min_amount': offer.min_amount,
			'is_irrevocable': offer.is_irrevocable,
			'source_url': offer.source_url,
			'scraped_at': now,
			'raw': offer.raw,
			'is_active': True,
		}
		if dry_run:
			exists = DepositOffer.objects.filter(bank=bank, external_id=offer.external_id).exists()
			if exists:
				updated += 1
			else:
				created += 1
			continue

		_, was_created = DepositOffer.objects.update_or_create(
			bank=bank,
			external_id=offer.external_id,
			defaults=defaults,
		)
		if was_created:
			created += 1
		else:
			updated += 1

	if dry_run:
		deactivated = (
			DepositOffer.objects.filter(bank_id__in=sibling_ids, is_active=True)
			.exclude(bank=bank, external_id__in=seen_ids)
			.count()
		)
	else:
		# Keep only current external_ids on the canonical bank; drop siblings' leftovers.
		deactivated = (
			DepositOffer.objects.filter(bank_id__in=sibling_ids, is_active=True)
			.exclude(bank=bank, external_id__in=seen_ids)
			.update(is_active=False)
		)
		bank.last_synced_at = now
		bank.save(update_fields=['last_synced_at', 'updated_at'])
		# Hide duplicate NBRB rows for the same parser so the UI/bank filter stays clean.
		DepositBank.objects.filter(parser_code=bank.parser_code, is_active=True).exclude(
			pk=bank.pk
		).update(is_active=False)

	return len(filtered), created, updated, deactivated


def deactivate_implausible_offers() -> int:
	"""One-shot cleanup for already stored scrape noise."""
	from django.db.models import Q

	qs = DepositOffer.objects.filter(is_active=True).filter(
		Q(rate_pct__gt=MAX_STORED_RATE)
		| Q(rate_pct_max__gt=MAX_STORED_RATE)
		| Q(rate_pct__lt=MIN_STORED_RATE, rate_pct_max__isnull=True)
		| Q(name__icontains='сравнен')
		| Q(name__icontains='пакет')
		| Q(name__icontains='калькулятор')
	)
	return qs.update(is_active=False)


def sync_deposit_offers(
	*,
	parser_codes: list[str] | None = None,
	bank_slugs: list[str] | None = None,
	dry_run: bool = False,
) -> OfferSyncResult:
	from apps.market.services.bootstrap import ensure_portfolio_deposit_banks
	from apps.market.services.nbrb_banks import assign_parsers_to_existing_banks

	ensure_portfolio_deposit_banks()
	assign_parsers_to_existing_banks()
	qs = DepositBank.objects.filter(is_active=True).exclude(parser_code='')
	if parser_codes:
		qs = qs.filter(parser_code__in=parser_codes)
	if bank_slugs:
		qs = qs.filter(slug__in=bank_slugs)

	banks_by_parser: dict[str, DepositBank] = {}
	all_banks = list(qs.order_by('name'))
	for bank in all_banks:
		banks_by_parser.setdefault(bank.parser_code, bank)
	# Prefer institution-linked / oldest bank as the canonical sync target.
	for parser_code in list(banks_by_parser):
		banks_by_parser[parser_code] = _canonical_bank_for_parser(
			parser_code,
			[b for b in all_banks if b.parser_code == parser_code]
			or [banks_by_parser[parser_code]],
		)
	banks = list(banks_by_parser.values())

	results: list[BankSyncResult] = []
	ok = 0
	failed = 0

	for bank in sorted(banks, key=lambda item: item.name):
		adapter = get_adapter(bank.parser_code)
		if adapter is None:
			results.append(
				BankSyncResult(
					bank_id=bank.pk,
					bank_name=str(bank),
					parser_code=bank.parser_code,
					offers=0,
					created=0,
					updated=0,
					deactivated=0,
					error=f'No adapter registered for parser_code={bank.parser_code}',
				)
			)
			failed += 1
			continue

		try:
			parsed = expand_atomic_offers(adapter())
			with transaction.atomic():
				kept, created, updated, deactivated = _persist_offers(
					bank, parsed, dry_run=dry_run
				)
			results.append(
				BankSyncResult(
					bank_id=bank.pk,
					bank_name=str(bank),
					parser_code=bank.parser_code,
					offers=kept,
					created=created,
					updated=updated,
					deactivated=deactivated,
				)
			)
			ok += 1
		except Exception as exc:
			logger.exception('Deposit offers sync failed for %s', bank)
			results.append(
				BankSyncResult(
					bank_id=bank.pk,
					bank_name=str(bank),
					parser_code=bank.parser_code,
					offers=0,
					created=0,
					updated=0,
					deactivated=0,
					error=str(exc),
				)
			)
			failed += 1

	return OfferSyncResult(banks=results, ok=ok, failed=failed)


def sync_result_as_dict(result: OfferSyncResult) -> dict:
	return {
		'ok': result.ok,
		'failed': result.failed,
		'total_offers': result.total_offers,
		'banks': [asdict(item) for item in result.banks],
	}
