"""Probe deposit listing pages for Belarus banks without parsers."""
from __future__ import annotations

import re
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = {'User-Agent': 'Mozilla/5.0 (compatible; PersonalFinance/1.0)'}
CACHE = Path('data/cache/bank_deposits_probe')
CACHE.mkdir(parents=True, exist_ok=True)

CANDIDATES = {
	'priorbank': [
		'https://www.priorbank.by/offers/savings/deposits',
		'https://www.priorbank.by/individual/deposits',
	],
	'belinvestbank': [
		'https://www.belinvestbank.by/individual/deposit',
		'https://belinvestbank.by/individual/deposit',
	],
	'belgazprombank': [
		'https://belgazprombank.by/personal/deposits/',
		'https://www.belgazprombank.by/personal/deposits/',
		'https://belgazprombank.by/individuals/deposits/',
	],
	'vtb': [
		'https://www.vtb.by/deposits',
		'https://www.vtb.by/deposits/vklady-v-belorusskih-rublyah',
	],
	'belapb': [
		'https://www.belapb.by/fizicheskim-licam/vklady/',
		'https://belapb.by/ru/private/deposits/',
		'https://www.belapb.by/ru/personal/deposits/',
	],
	'belveb': [
		'https://www.belveb.by/personal/deposits/',
		'https://belveb.by/individuals/deposits/',
		'https://www.belveb.by/chastnym-klientam/vklady/',
	],
	'mtbank': [
		'https://www.mtbank.by/personal/deposits/',
		'https://mtbank.by/chastnym-klientam/vklady/',
	],
	'sberbank': [
		'https://www.sber-bank.by/page/deposits',
		'https://www.sber-bank.by/individual/deposits',
		'https://www.sber-bank.by/ru/personal/deposits/',
	],
	'dabrabyt': [
		'https://bankdabrabyt.by/individuals/deposits/',
		'https://bankdabrabyt.by/chastnym-klientam/vklady/',
	],
	'bsb': [
		'https://www.bsb.by/personal/deposits/',
		'https://bsb.by/individuals/deposits/',
	],
	'paritetbank': [
		'https://www.paritetbank.by/personal/deposits/',
		'https://paritetbank.by/individuals/deposits/',
	],
	'rrb': [
		'https://www.rrb.by/personal/deposits/',
		'https://rrb.by/individuals/deposits/',
		'https://www.rrb.by/chastnym-klientam/vklady/',
	],
	'tcbank': [
		'https://www.tcbank.by/personal/deposits/',
		'https://tcbank.by/individuals/deposits/',
	],
	'btabank': [
		'https://www.btabank.by/personal/deposits/',
		'https://btabank.by/individuals/deposits/',
	],
	'zepterbank': [
		'https://www.zepterbank.by/personal/deposits/',
		'https://zepterbank.by/individuals/deposits/',
	],
	'statusbank': [
		'https://www.stbank.by/personal/deposits/',
		'https://stbank.by/individuals/deposits/',
		'https://www.stbank.by/chastnym-klientam/vklady/',
	],
	'technobank': [
		'https://www.tb.by/personal/deposits/',
		'https://tb.by/individuals/deposits/',
		'https://www.tb.by/chastnym-klientam/vklady/',
	],
	'reshenie': [
		'https://rbank.by/personal/deposits/',
		'https://rbank.by/individuals/deposits/',
		'https://www.rbank.by/chastnym-klientam/vklady/',
	],
}


def fetch(url: str) -> tuple[int | str, str]:
	try:
		req = Request(url, headers=UA)
		with urlopen(req, timeout=45, context=CTX) as resp:
			raw = resp.read()
			charset = resp.headers.get_content_charset() or 'utf-8'
			return resp.status, raw.decode(charset, errors='replace')
	except HTTPError as exc:
		return exc.code, ''
	except Exception as exc:
		return type(exc).__name__, str(exc)


def summarize(html: str) -> dict:
	rates = re.findall(r'(\d+[.,]\d+|\d+)\s*%', html)
	tables = len(re.findall(r'<table', html, re.I))
	jsonish = len(re.findall(r'application/json|__NEXT_DATA__|window\.__', html, re.I))
	deposit_links = sorted(set(re.findall(r'href=\"([^\"]*(?:deposit|vklad|сбереж|депоз)[^\"]*)\"', html, re.I)))[:12]
	return {
		'len': len(html),
		'rates': len(rates),
		'rate_sample': rates[:8],
		'tables': tables,
		'jsonish': jsonish,
		'deposit_links': deposit_links,
	}


for code, urls in CANDIDATES.items():
	print(f'\n=== {code} ===')
	for url in urls:
		status, body = fetch(url)
		if isinstance(status, int) and status == 200 and len(body) > 500:
			path = CACHE / f'{code}.html'
			path.write_text(body, encoding='utf-8')
			info = summarize(body)
			print('OK', url, info)
			break
		print('FAIL', url, status, body[:80] if isinstance(body, str) else '')
