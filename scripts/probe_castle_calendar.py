# -*- coding: utf-8 -*-
import re
from urllib.request import Request, urlopen

html = urlopen(Request('https://castle.by/calendar/', headers={'User-Agent': 'Mozilla/5.0'}), timeout=60).read().decode('utf-8', errors='replace')
print('bond-param-label count', html.count('bond-param-label'))
print('bond links', len(set(re.findall(r'/bond/(\d+)/', html))))
# calendar might embed token in link text
samples = re.findall(r'href="/bond/(\d+)/"[^>]*>([^<]{0,80})', html)[:5]
print('link samples', samples)
