from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings

from apps.imports.services.integrations.base import BaseApiClient


DEFAULT_BINANCE_API_BASE_URL = 'https://api.binance.com'


@dataclass(frozen=True)
class BinanceSymbol:
	symbol: str
	base_asset: str
	quote_asset: str


class BinanceApiError(RuntimeError):
	pass


class BinanceClient(BaseApiClient):
	source_code = 'binance-api'

	def __init__(
		self,
		api_key: str | None = None,
		api_secret: str | None = None,
		base_url: str | None = None,
		timeout: int = 20,
	):
		self.api_key = api_key if api_key is not None else getattr(settings, 'BINANCE_API_KEY', '')
		self.api_secret = api_secret if api_secret is not None else getattr(settings, 'BINANCE_API_SECRET', '')
		self.base_url = (base_url if base_url is not None else getattr(settings, 'BINANCE_API_BASE_URL', DEFAULT_BINANCE_API_BASE_URL)).rstrip('/')
		self.timeout = timeout

	def _headers(self, signed: bool) -> dict[str, str]:
		headers = {'Content-Type': 'application/json'}
		if signed:
			if not self.api_key or not self.api_secret:
				raise BinanceApiError('BINANCE_API_KEY and BINANCE_API_SECRET are required for signed Binance endpoints.')
			headers['X-MBX-APIKEY'] = self.api_key
		return headers

	def _signature(self, query_string: str) -> str:
		return hmac.new(
			self.api_secret.encode('utf-8'),
			query_string.encode('utf-8'),
			hashlib.sha256,
		).hexdigest()

	def _request(
		self,
		method: str,
		path: str,
		params: dict[str, Any] | None = None,
		*,
		signed: bool = False,
	) -> Any:
		params = {key: value for key, value in (params or {}).items() if value is not None}
		if signed:
			params.setdefault('timestamp', int(time.time() * 1000))
			query_string = urlencode(params, doseq=True)
			params['signature'] = self._signature(query_string)
		query_string = urlencode(params, doseq=True)
		url = f'{self.base_url}/{path.lstrip("/")}'
		data = None
		if method.upper() == 'GET':
			if query_string:
				url = f'{url}?{query_string}'
		else:
			data = query_string.encode('utf-8')

		request = Request(url, data=data, headers=self._headers(signed), method=method.upper())
		try:
			with urlopen(request, timeout=self.timeout) as response:
				raw_body = response.read().decode('utf-8')
		except Exception as exc:  # pragma: no cover - urllib exceptions vary by platform
			raise BinanceApiError(f'Binance request failed: {exc}') from exc

		return json.loads(raw_body) if raw_body else {}

	def fetch_exchange_info(self, symbols: list[str] | None = None) -> dict:
		params = {'symbols': json.dumps(symbols)} if symbols else None
		return self._request('GET', '/api/v3/exchangeInfo', params)

	def fetch_symbol_map(self) -> dict[str, BinanceSymbol]:
		payload = self.fetch_exchange_info()
		result: dict[str, BinanceSymbol] = {}
		for item in payload.get('symbols', []):
			if item.get('status') != 'TRADING':
				continue
			symbol = item.get('symbol')
			base_asset = item.get('baseAsset')
			quote_asset = item.get('quoteAsset')
			if symbol and base_asset and quote_asset:
				result[symbol] = BinanceSymbol(symbol=symbol, base_asset=base_asset, quote_asset=quote_asset)
		return result

	def fetch_account(self) -> dict:
		return self._request('GET', '/api/v3/account', signed=True)

	def fetch_ticker_prices(self) -> list[dict]:
		return self._request('GET', '/api/v3/ticker/price')

	def fetch_my_trades(self, symbol: str, start_time: int | None = None, end_time: int | None = None, from_id: int | None = None, limit: int = 1000) -> list[dict]:
		return self._request(
			'GET',
			'/api/v3/myTrades',
			{
				'symbol': symbol,
				'startTime': start_time,
				'endTime': end_time,
				'fromId': from_id,
				'limit': limit,
			},
			signed=True,
		)

	def fetch_deposits(self, start_time: int | None = None, end_time: int | None = None, limit: int = 1000) -> list[dict]:
		return self._request(
			'GET',
			'/sapi/v1/capital/deposit/hisrec',
			{'startTime': start_time, 'endTime': end_time, 'limit': limit},
			signed=True,
		)

	def fetch_withdrawals(self, start_time: int | None = None, end_time: int | None = None, limit: int = 1000) -> list[dict]:
		return self._request(
			'GET',
			'/sapi/v1/capital/withdraw/history',
			{'startTime': start_time, 'endTime': end_time, 'limit': limit},
			signed=True,
		)

	def fetch_funding_assets(self, asset: str | None = None) -> list[dict]:
		return self._request('POST', '/sapi/v1/asset/get-funding-asset', {'asset': asset}, signed=True)

	def fetch_simple_earn_flexible_positions(self, asset: str | None = None) -> dict:
		return self._request('GET', '/sapi/v1/simple-earn/flexible/position', {'asset': asset}, signed=True)

	def fetch_simple_earn_locked_positions(self, asset: str | None = None) -> dict:
		return self._request('GET', '/sapi/v1/simple-earn/locked/position', {'asset': asset}, signed=True)

	def fetch_simple_earn_rewards(self, start_time: int | None = None, end_time: int | None = None, reward_type: str | None = None) -> dict:
		return self._request(
			'GET',
			'/sapi/v1/simple-earn/history/rewardsRecord',
			{'startTime': start_time, 'endTime': end_time, 'type': reward_type},
			signed=True,
		)


def decimal_from_binance(value: Any) -> Decimal:
	if value in (None, ''):
		return Decimal('0')
	return Decimal(str(value))
