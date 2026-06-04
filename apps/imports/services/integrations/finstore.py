import json
import os
from pathlib import Path
from urllib.request import urlopen

from apps.imports.services.integrations.base import BaseApiClient
from apps.products.services.token_terms import TokenTermsRow, load_token_terms_from_json_payload, load_token_terms_rows


class FinstoreTokenCatalogClient(BaseApiClient):
	"""Loads Finstore token terms from a local file or configurable URL.

	Finstore does not publish a stable public catalog API for retail investors.
	Set FINSTORE_TERMS_FILE or FINSTORE_TERMS_URL to sync terms automatically.
	"""

	source_code = 'finstore-token-catalog'

	def resolve_source_path(self, explicit_path: str | None = None) -> Path | None:
		candidate = explicit_path or os.getenv('FINSTORE_TERMS_FILE', '').strip()
		if not candidate:
			return None
		path = Path(candidate)
		return path if path.exists() else None

	def resolve_source_url(self, explicit_url: str | None = None) -> str:
		return (explicit_url or os.getenv('FINSTORE_TERMS_URL', '')).strip()

	def fetch_rows(self, *, file_path: str | None = None, url: str | None = None) -> list[TokenTermsRow]:
		path = self.resolve_source_path(file_path)
		if path is not None:
			return load_token_terms_rows(path)

		source_url = self.resolve_source_url(url)
		if source_url:
			with urlopen(source_url, timeout=30) as response:
				payload = json.loads(response.read().decode('utf-8'))
			return load_token_terms_from_json_payload(payload)

		raise FileNotFoundError(
			'Finstore token terms source is not configured. '
			'Provide --file, set FINSTORE_TERMS_FILE, or set FINSTORE_TERMS_URL.'
		)
