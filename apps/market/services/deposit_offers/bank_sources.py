from __future__ import annotations

from collections.abc import Callable

from apps.market.services.deposit_offers.base import ParsedDepositOffer
from apps.market.services.deposit_offers.heuristic import fetch_bank_offers

# parser_code -> listing URLs on the bank's own site
BANK_LISTING_URLS: dict[str, list[str]] = {
	'priorbank': [
		'https://www.priorbank.by/offers/savings/deposits',
	],
	'belinvestbank': [
		'https://www.belinvestbank.by/individual/deposits',
	],
	'belgazprombank': [
		'https://belgazprombank.by/personal_banking/vkladi_depoziti/depoziti/',
	],
	'vtb': [
		'https://www.vtb.by/deposits',
		'https://www.vtb.by/deposits/vklady-v-belorusskih-rublyah',
	],
	'belapb': [
		'https://www.belapb.by/chastnomu-klientu/sberezheniya/vklady-i-scheta/',
	],
	'belveb': [
		'https://www.belveb.by/deposits/',
	],
	'mtbank': [
		'https://www.mtbank.by/deposits/',
	],
	'sberbank': [
		'https://www.sber-bank.by/vklady',
		'https://www.sber-bank.by/vklady/v-belorusskih-rublyah',
	],
	'dabrabyt': [
		'https://bankdabrabyt.by/personal/deposite/',
	],
	'bsb': [
		'https://www.bsb.by/personal/deposits/',
	],
	'paritetbank': [
		'https://www.paritetbank.by/private/deposit/',
	],
	'rrb': [
		'https://www.rrb.by/vkladi',
	],
	'tcbank': [
		'https://www.tcbank.by/personal/deposits/',
	],
	'neobank': [
		'https://neobank.by/deposits/',
	],
	'zepterbank': [
		'https://www.zepterbank.by/personal/deposits/',
		'https://www.zepterbank.by/personal/deposits/vklady-v-belorusskikh-rublyakh/',
	],
	'statusbank': [
		'https://www.stbank.by/private-client/deposits/',
	],
	'technobank': [
		'https://tb.by/individuals/deposits/',
	],
	'reshenie': [
		'https://rbank.by/life/deposits/',
	],
}

WEBSITE_BY_PARSER: dict[str, str] = {
	'belarusbank': 'https://belarusbank.by/',
	'alfabank': 'https://www.alfabank.by/',
	'bnb': 'https://bnb.by/',
	'priorbank': 'https://www.priorbank.by/',
	'belinvestbank': 'https://www.belinvestbank.by/',
	'belgazprombank': 'https://belgazprombank.by/',
	'vtb': 'https://www.vtb.by/',
	'belapb': 'https://www.belapb.by/',
	'belveb': 'https://www.belveb.by/',
	'mtbank': 'https://www.mtbank.by/',
	'sberbank': 'https://www.sber-bank.by/',
	'dabrabyt': 'https://bankdabrabyt.by/',
	'bsb': 'https://www.bsb.by/',
	'paritetbank': 'https://www.paritetbank.by/',
	'rrb': 'https://www.rrb.by/',
	'tcbank': 'https://www.tcbank.by/',
	'neobank': 'https://neobank.by/',
	'zepterbank': 'https://www.zepterbank.by/',
	'statusbank': 'https://www.stbank.by/',
	'technobank': 'https://tb.by/',
	'reshenie': 'https://rbank.by/',
}

# NBRB name needles -> (optional institution_slug, parser_code)
BANK_MATCHERS: list[tuple[tuple[str, ...], str, str]] = [
	(('беларусбанк', 'belarusbank'), 'belarusbank', 'belarusbank'),
	(('альфа-банк', 'альфа банк', 'alfabank'), 'alfabank', 'alfabank'),
	(('бнб-банк', 'бнб банк', 'bnb-банк'), 'bnb-bank', 'bnb'),
	(('приорбанк', 'priorbank'), '', 'priorbank'),
	(('белинвестбанк', 'belinvestbank'), '', 'belinvestbank'),
	(('белгазпромбанк', 'belgazprombank'), '', 'belgazprombank'),
	(('втб', 'vtb'), '', 'vtb'),
	(('белагропромбанк', 'belapb'), '', 'belapb'),
	(('белвэб', 'belveb'), '', 'belveb'),
	(('мтбанк', 'mtbank'), '', 'mtbank'),
	(('сбер банк', 'сбербанк', 'sber-bank', 'sber bank'), '', 'sberbank'),
	(('дабрабыт', 'dabrabyt'), '', 'dabrabyt'),
	(('бсб банк', 'bsb'), '', 'bsb'),
	(('паритетбанк', 'paritetbank'), '', 'paritetbank'),
	(('банк ррб', 'ррб', 'rrb'), '', 'rrb'),
	(('тк банк', 'tcbank', 'тк-банк'), '', 'tcbank'),
	(('нео банк', 'нео банк азия', 'neobank', 'btabank'), '', 'neobank'),
	(('цептер банк', 'zepter'), '', 'zepterbank'),
	(('статусбанк', 'statusbank', 'stbank'), '', 'statusbank'),
	(('технобанк', 'technobank', 'tb.by'), '', 'technobank'),
	(('банк "решение"', 'банк решение', 'решение', 'rbank'), '', 'reshenie'),
]


def _make_fetcher(parser_code: str) -> Callable[[], list[ParsedDepositOffer]]:
	urls = BANK_LISTING_URLS[parser_code]

	def _fetch() -> list[ParsedDepositOffer]:
		return fetch_bank_offers(
			listing_urls=urls,
			max_products=12,
			crawl_details=True,
			insecure_ssl=True,
		)

	_fetch.__name__ = f'fetch_{parser_code}_offers'
	_fetch.__qualname__ = _fetch.__name__
	return _fetch


HEURISTIC_ADAPTERS: dict[str, Callable[[], list[ParsedDepositOffer]]] = {
	code: _make_fetcher(code)
	for code in BANK_LISTING_URLS
	if code not in {'neobank', 'technobank', 'sberbank', 'mtbank', 'belapb'}
}
