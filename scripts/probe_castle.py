# -*- coding: utf-8 -*-
import re
from urllib.request import Request, urlopen

UA = {'User-Agent': 'Mozilla/5.0'}
page = urlopen(Request('https://castle.by/bond/973/', headers=UA), timeout=30).read().decode('utf-8', errors='replace')

params = dict(
    re.findall(r'bond-param-label">([^<]+)</td>\s*<td[^>]*>([^<]+)', page, re.I)
)
for k, v in params.items():
    print(k.strip(), '=>', v.strip())

platform = re.search(r'Finstore|Fainex|Bynex', page, re.I)
print('platform', platform.group(0) if platform else None)
