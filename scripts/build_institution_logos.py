"""Download institution logos and normalize into unified SVG tiles."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'static' / 'img' / 'institutions'
OUT.mkdir(parents=True, exist_ok=True)

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

SOURCES = {
    'finstore': 'https://finstore.by/favicon.png',
    'aigenis': 'https://aigenis.by/wp-content/uploads/2024/03/icon-aigenis-for-moblile-web.png',
    'bnb-bank': 'https://bnb.by/local/templates/itachMain/assets/images/logo/logo.svg',
    'belarusbank': 'https://belarusbank.by/upload/favicon/apple-touch-icon-120x120.png',
    'priorlife': 'https://priorlife.by/i/Logo-25%20let_RGB.svg',
    'alfabank-sprite': 'https://alfabank.by/new_alfa/local/assets/build/sprite.svg',
    'binance': 'https://raw.githubusercontent.com/simple-icons/simple-icons/develop/icons/binance.svg',
}

TILE_SIZE = 40
ICON_SIZE = 28
PADDING = (TILE_SIZE - ICON_SIZE) / 2


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read()


def extract_alfabank_logo(sprite_bytes: bytes) -> bytes:
    text = sprite_bytes.decode('utf-8')
    match = re.search(
        r'<symbol[^>]+id="alfabank_logo"[^>]*>(.*?)</symbol>',
        text,
        re.S,
    )
    if not match:
        raise ValueError('alfabank_logo symbol not found')
    inner = match.group(1).strip()
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 32">{inner}</svg>'
    ).encode('utf-8')


def svg_viewbox(svg_bytes: bytes) -> tuple[float, float, float, float]:
    root = ET.fromstring(svg_bytes)
    viewbox = root.attrib.get('viewBox')
    if viewbox:
        parts = [float(part) for part in viewbox.split()]
        return tuple(parts)  # type: ignore[return-value]
    width = float(root.attrib.get('width', '24').replace('px', ''))
    height = float(root.attrib.get('height', '24').replace('px', ''))
    return 0, 0, width, height


def wrap_svg_tile(slug: str, svg_bytes: bytes) -> str:
    _, _, vw, vh = svg_viewbox(svg_bytes)
    scale = min(ICON_SIZE / vw, ICON_SIZE / vh)
    w = vw * scale
    h = vh * scale
    x = PADDING + (ICON_SIZE - w) / 2
    y = PADDING + (ICON_SIZE - h) / 2
    inner = svg_bytes.decode('utf-8')
    inner = re.sub(r'<\?xml[^?]*\?>', '', inner)
    inner = re.sub(r'<!DOCTYPE[^>]*>', '', inner, flags=re.I)
    inner = re.sub(r'<svg[^>]*>', '', inner, count=1)
    inner = re.sub(r'</svg>\s*$', '', inner)
    inner = inner.replace('fill="currentColor"', 'fill="#EF3124"')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {TILE_SIZE} {TILE_SIZE}" role="img" aria-label="{slug}">
  <rect width="{TILE_SIZE}" height="{TILE_SIZE}" rx="8" fill="var(--institution-logo-bg, #f3f4f6)"/>
  <g transform="translate({x:.2f} {y:.2f}) scale({scale:.4f})">
    {inner.strip()}
  </g>
</svg>
'''


def wrap_raster_tile(slug: str, image_bytes: bytes, ext: str) -> str:
    import base64

    mime = 'image/png' if ext == 'png' else 'image/jpeg'
    encoded = base64.b64encode(image_bytes).decode('ascii')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {TILE_SIZE} {TILE_SIZE}" role="img" aria-label="{slug}">
  <rect width="{TILE_SIZE}" height="{TILE_SIZE}" rx="8" fill="var(--institution-logo-bg, #f3f4f6)"/>
  <image x="{PADDING}" y="{PADDING}" width="{ICON_SIZE}" height="{ICON_SIZE}" preserveAspectRatio="xMidYMid meet" xlink:href="data:{mime};base64,{encoded}"/>
</svg>
'''


def main() -> None:
    for slug, url in SOURCES.items():
        try:
            data = fetch(url)
            if slug == 'alfabank-sprite':
                data = extract_alfabank_logo(data)
                slug = 'alfabank'
            if url.endswith('.svg') or data.lstrip().startswith(b'<'):
                tile = wrap_svg_tile(slug, data)
            else:
                ext = 'png' if url.endswith('.png') or data[:8] == b'\x89PNG\r\n\x1a\n' else 'jpg'
                tile = wrap_raster_tile(slug, data, ext)
            (OUT / f'{slug}.svg').write_text(tile, encoding='utf-8')
            print(f'OK {slug}')
        except Exception as exc:
            print(f'FAIL {slug}: {exc}')

    # Stravita fallback — stylized mark (site unavailable)
    stravita = '''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" role="img" aria-label="stravita">
  <rect width="40" height="40" rx="8" fill="#E8F0FA"/>
  <path d="M12 28V12h6.2c3.4 0 5.6 1.8 5.6 4.6 0 2-1.1 3.4-2.9 4l3.4 7.4h-3.6l-3-6.6H15.2V28H12zm3.2-9.2h2.8c1.6 0 2.5-.8 2.5-2s-.9-2-2.5-2h-2.8v4z" fill="#003DA5"/>
</svg>
'''
    (OUT / 'stravita.svg').write_text(stravita, encoding='utf-8')
    print('OK stravita (fallback)')


if __name__ == '__main__':
    main()
