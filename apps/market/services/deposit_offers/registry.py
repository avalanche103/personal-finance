from __future__ import annotations

from collections.abc import Callable

from apps.market.services.deposit_offers.alfabank import fetch_alfabank_offers
from apps.market.services.deposit_offers.bank_sources import HEURISTIC_ADAPTERS
from apps.market.services.deposit_offers.base import ParsedDepositOffer
from apps.market.services.deposit_offers.belapb import fetch_belapb_offers
from apps.market.services.deposit_offers.belarusbank import fetch_belarusbank_offers
from apps.market.services.deposit_offers.bnb import fetch_bnb_offers
from apps.market.services.deposit_offers.mtbank import fetch_mtbank_offers
from apps.market.services.deposit_offers.neobank import fetch_neobank_offers
from apps.market.services.deposit_offers.sberbank import fetch_sberbank_offers
from apps.market.services.deposit_offers.technobank import fetch_technobank_offers

AdapterFn = Callable[[], list[ParsedDepositOffer]]

ADAPTERS: dict[str, AdapterFn] = {
	'belarusbank': fetch_belarusbank_offers,
	'alfabank': fetch_alfabank_offers,
	'bnb': fetch_bnb_offers,
	'neobank': fetch_neobank_offers,
	'technobank': fetch_technobank_offers,
	'sberbank': fetch_sberbank_offers,
	'mtbank': fetch_mtbank_offers,
	'belapb': fetch_belapb_offers,
	**HEURISTIC_ADAPTERS,
}

# Dedicated parsers override heuristic entries with the same code.
ADAPTERS['neobank'] = fetch_neobank_offers
ADAPTERS['technobank'] = fetch_technobank_offers
ADAPTERS['sberbank'] = fetch_sberbank_offers
ADAPTERS['mtbank'] = fetch_mtbank_offers
ADAPTERS['belapb'] = fetch_belapb_offers



def get_adapter(parser_code: str) -> AdapterFn | None:
	return ADAPTERS.get((parser_code or '').strip().lower())
