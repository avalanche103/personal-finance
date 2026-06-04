import os

from apps.imports.services.integrations.base import BaseApiClient
from apps.products.services.castle import (
	CASTLE_CALENDAR_URL,
	fetch_castle_token_terms,
)
from apps.products.services.token_terms import TokenTermsRow


class CastleBuySmartClient(BaseApiClient):
	"""Scrapes token terms from https://castle.by/calendar/ bond detail pages."""

	source_code = 'castle-buysmart'

	def fetch_rows(
		self,
		*,
		calendar_url: str | None = None,
		platform: str | None = 'finstore',
		limit: int | None = None,
	) -> tuple[list[TokenTermsRow], list[str]]:
		url = (calendar_url or os.getenv('CASTLE_CALENDAR_URL', '')).strip() or CASTLE_CALENDAR_URL
		rows, errors = fetch_castle_token_terms(
			calendar_url=url,
			platform=platform,
			limit=limit,
		)
		if errors and not rows:
			raise RuntimeError('; '.join(errors[:5]))
		return rows, errors
