"""Discover deposit URLs from bank homepages."""
from __future__ import annotations

import re
import ssl
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

HOMES = {
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
	'btabank': 'https://www.btabank.by/',
	'zepterbank': 'https://www.zepterbank.by/',
	'statusbank': 'https://www.stbank.by/',
	'technobank': 'https://tb.by/',
	'reshenie': 'https://rbank.by/',
}

KEYWORDS = (
	'deposit', 'vklad', 'vklady', 'deposits', 'сбереж', 'депоз', 'вклад',
	'savings', 'nakoplen',
)


def fetch(url: str) -> str:
	req = Request(url, headers=UA)
	with urlopen(req, timeout=40, context=CTX) as resp:
		return resp.read().decode(resp.headers.get_content_charset() or 'utf-8', errors='replace')


for code, home in HOMES.items():
	print(f'\n=== {code} ({home}) ===')
	try:
		html = fetch(home)
	except Exception as exc:
		print('HOME FAIL', type(exc).__name__, exc)
		continue
	links = set()
	for href, text in re.findall(r'<a[^>]+href=\"([^\"]+)\"[^>]*>([\s\S]*?)</a>', html, re.I):
		blob = (href + ' ' + re.sub('<[^>]+>', ' ', text)).lower()
		if any(k in blob for k in KEYWORDS):
			full = urljoin(home, href.split('#')[0])
			if urlparse(full).netloc and urlparse(home).netloc.split('.')[-2:] == urlparse(full).netloc.split('.')[-2:]:
				links.add(full)
	for url in sorted(links)[:20]:
		print(url)
