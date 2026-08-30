from __future__ import annotations

from apps.institutions.models import FinancialInstitution
from apps.market.models import DepositBank

PORTFOLIO_BANKS = (
	{
		'external_key': 'portfolio-belarusbank',
		'name': 'Открытое акционерное общество "Сберегательный банк "Беларусбанк"',
		'short_name': 'ОАО "АСБ Беларусбанк"',
		'slug': 'belarusbank',
		'parser_code': 'belarusbank',
		'website': 'https://belarusbank.by/',
		'institution_slug': 'belarusbank',
	},
	{
		'external_key': 'portfolio-alfabank',
		'name': 'Закрытое акционерное общество "Альфа-Банк"',
		'short_name': 'ЗАО "Альфа-Банк"',
		'slug': 'alfabank',
		'parser_code': 'alfabank',
		'website': 'https://www.alfabank.by/',
		'institution_slug': 'alfabank',
	},
	{
		'external_key': 'portfolio-bnb',
		'name': 'Открытое акционерное общество "БНБ-Банк"',
		'short_name': 'ОАО "БНБ-Банк"',
		'slug': 'bnb-bank',
		'parser_code': 'bnb',
		'website': 'https://bnb.by/',
		'institution_slug': 'bnb-bank',
	},
)


def ensure_portfolio_deposit_banks() -> int:
	"""Ensure the three portfolio banks exist with parser codes even before NBRB sync."""
	created = 0
	for item in PORTFOLIO_BANKS:
		institution = FinancialInstitution.objects.filter(slug=item['institution_slug']).first()
		defaults = {
			'name': item['name'],
			'short_name': item['short_name'],
			'slug': item['slug'],
			'parser_code': item['parser_code'],
			'website': item['website'],
			'entity_type': DepositBank.EntityType.BANK,
			'is_active': True,
			'institution': institution,
			'metadata': {'source': 'portfolio-bootstrap'},
		}
		bank, was_created = DepositBank.objects.get_or_create(
			external_key=item['external_key'],
			defaults=defaults,
		)
		if was_created:
			created += 1
			continue
		changed = False
		if not bank.parser_code:
			bank.parser_code = item['parser_code']
			changed = True
		if institution and bank.institution_id != institution.pk:
			bank.institution = institution
			changed = True
		if not bank.website:
			bank.website = item['website']
			changed = True
		if changed:
			bank.save()
	return created
