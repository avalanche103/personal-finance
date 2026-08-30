from __future__ import annotations

# Re-export helpers for convenience.
from apps.market.services.deposit_offers.base import (  # noqa: F401
	ParsedDepositOffer,
	fetch_text,
	parse_min_amount,
	parse_rate_value,
	parse_term_days,
	strip_tags,
)
