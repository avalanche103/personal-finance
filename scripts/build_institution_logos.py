"""Download institution logos and normalize into unified SVG tiles."""
from __future__ import annotations

import base64
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
import urllib.request

try:
    from PIL import Image
except ImportError:  # pragma: no cover - dev dependency used by logo build script
    Image = None  # type: ignore[misc, assignment]

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'static' / 'img' / 'institutions'
OUT.mkdir(parents=True, exist_ok=True)

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

# High-resolution tiles for large import picker icons.
TILE_SIZE = 256
ICON_SIZE = 224
PADDING = (TILE_SIZE - ICON_SIZE) / 2

SOURCES: dict[str, str | dict] = {
    'finstore': 'https://finstore.by/favicon.png',
    'aigenis': 'https://aigenis.by/wp-content/uploads/2024/02/icon-aigenis-for-moblile-web-1-300x300.png',
    'bnb-bank': 'https://bnb.by/local/templates/itachMain/assets/images/logo/logo.svg',
    'belarusbank': 'https://belarusbank.by/upload/favicon/apple-touch-icon-180x180.png',
    'priorlife': 'https://priorlife.by/i/Logo-25%20let_RGB.svg',
    'binance': 'https://raw.githubusercontent.com/simple-icons/simple-icons/develop/icons/binance.svg',
    'alfabank': {
        'type': 'zip_svg',
        'url': 'https://www.alfabank.by/upload/docs/bank/znak_svg.zip',
        'contains_all': ['#ef3124', '#fff'],
    },
    'nbrb': {
        'type': 'raster_crop',
        'url': 'https://www.nbrb.by/i/logo.png',
        'crop': (0, 0, 75, 56),
        'tile_bg': '#1B4B8C',
    },
    'stravita': {
        'type': 'raster',
        'url': 'https://insurancecompanieslogos.com/wp-content/uploads/stravita-logo.jpg',
        'referer': 'https://insurancecompanieslogos.com/belarus-insurance/',
    },
}


def fetch(url: str, *, referer: str | None = None) -> bytes:
    headers = {
        'User-Agent': USER_AGENT,
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
    }
    if referer:
        headers['Referer'] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


def svg_viewbox(svg_bytes: bytes, override: tuple[float, float, float, float] | None = None) -> tuple[float, float, float, float]:
    if override is not None:
        return override
    root = ET.fromstring(svg_bytes)
    viewbox = root.attrib.get('viewBox')
    if viewbox:
        parts = [float(part) for part in viewbox.split()]
        return tuple(parts)  # type: ignore[return-value]
    width = float(root.attrib.get('width', '24').replace('px', ''))
    height = float(root.attrib.get('height', '24').replace('px', ''))
    return 0, 0, width, height


def strip_svg_document(svg_bytes: bytes) -> str:
    inner = svg_bytes.decode('utf-8')
    inner = re.sub(r'<\?xml[^?]*\?>', '', inner)
    inner = re.sub(r'<!DOCTYPE[^>]*>', '', inner, flags=re.I)
    inner = re.sub(r'<[\w:]*svg[^>]*>', '', inner, count=1, flags=re.I)
    inner = re.sub(r'</[\w:]*svg>\s*$', '', inner, flags=re.I)
    return inner.strip()


def apply_svg_viewbox(svg_bytes: bytes, view_box: tuple[float, float, float, float]) -> bytes:
    text = svg_bytes.decode('utf-8')
    vx, vy, vw, vh = view_box
    new_viewbox = f'{vx} {vy} {vw} {vh}'
    if re.search(r'viewBox=', text, flags=re.I):
        text = re.sub(r'viewBox="[^"]*"', f'viewBox="{new_viewbox}"', text, count=1, flags=re.I)
    else:
        text = re.sub(r'(<[\w:]*svg[^>]*)>', rf'\1 viewBox="{new_viewbox}">', text, count=1, flags=re.I)
    text = re.sub(r'\s(width|height)="[^"]*"', '', text, count=2)
    return text.encode('utf-8')


def crop_raster(image_bytes: bytes, crop: tuple[int, int, int, int]) -> bytes:
    if Image is None:
        raise RuntimeError('Pillow is required for raster_crop logo sources')
    left, top, right, bottom = crop
    image = Image.open(io.BytesIO(image_bytes)).convert('RGBA')
    cropped = image.crop((left, top, right, bottom))
    out = io.BytesIO()
    cropped.save(out, format='PNG')
    return out.getvalue()


def wrap_svg_tile(
    slug: str,
    svg_bytes: bytes,
    *,
    view_box: tuple[float, float, float, float] | None = None,
    tile_bg: str | None = None,
) -> str:
    if view_box is not None:
        svg_bytes = apply_svg_viewbox(svg_bytes, view_box)
    _, _, vw, vh = svg_viewbox(svg_bytes)
    scale = min(ICON_SIZE / vw, ICON_SIZE / vh)
    w = vw * scale
    h = vh * scale
    x = PADDING + (ICON_SIZE - w) / 2
    y = PADDING + (ICON_SIZE - h) / 2
    inner = strip_svg_document(svg_bytes)
    inner = inner.replace('fill="currentColor"', 'fill="#EF3124"')
    bg = tile_bg or 'var(--institution-logo-bg, #f3f4f6)'
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {TILE_SIZE} {TILE_SIZE}" role="img" aria-label="{slug}">
  <rect width="{TILE_SIZE}" height="{TILE_SIZE}" rx="24" fill="{bg}"/>
  <g transform="translate({x:.2f} {y:.2f}) scale({scale:.6f})">
    {inner}
  </g>
</svg>
'''


def wrap_raster_tile(slug: str, image_bytes: bytes, ext: str, *, tile_bg: str | None = None) -> str:
    mime = 'image/png' if ext == 'png' else 'image/jpeg'
    encoded = base64.b64encode(image_bytes).decode('ascii')
    bg = tile_bg or 'var(--institution-logo-bg, #f3f4f6)'
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {TILE_SIZE} {TILE_SIZE}" role="img" aria-label="{slug}">
  <rect width="{TILE_SIZE}" height="{TILE_SIZE}" rx="24" fill="{bg}"/>
  <image x="{PADDING}" y="{PADDING}" width="{ICON_SIZE}" height="{ICON_SIZE}" preserveAspectRatio="xMidYMid meet" xlink:href="data:{mime};base64,{encoded}"/>
</svg>
'''


def load_zip_svg(url: str, *, index: int | None = None, contains: str | None = None, contains_all: list[str] | None = None) -> bytes:
    archive_bytes = fetch(url)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = sorted(name for name in archive.namelist() if name.lower().endswith('.svg'))
        if not names:
            raise ValueError('No SVG files in archive')
        if contains_all:
            for name in names:
                body_text = archive.read(name).decode('utf-8', errors='ignore').lower()
                if all(needle.lower() in body_text for needle in contains_all):
                    return archive.read(name)
            raise ValueError(f'No SVG containing all of {contains_all!r}')
        if contains:
            needle = contains.lower()
            for name in names:
                body = archive.read(name)
                if needle in body.decode('utf-8', errors='ignore').lower():
                    return body
            raise ValueError(f'No SVG containing {contains!r}')
        return archive.read(names[index or 0])


def resolve_source(slug: str, source: str | dict) -> tuple[bytes, str]:
    if isinstance(source, str):
        data = fetch(source)
        if source.endswith('.svg') or data.lstrip().startswith(b'<'):
            return data, 'svg'
        ext = 'png' if source.endswith('.png') or data[:8] == b'\x89PNG\r\n\x1a\n' else 'jpg'
        return data, ext

    source_type = source['type']
    if source_type == 'svg':
        return fetch(source['url']), 'svg'
    if source_type == 'zip_svg':
        return load_zip_svg(
            source['url'],
            index=source.get('index'),
            contains=source.get('contains'),
            contains_all=source.get('contains_all'),
        ), 'svg'
    if source_type == 'raster':
        data = fetch(source['url'], referer=source.get('referer'))
        ext = 'jpg' if source['url'].lower().endswith('.jpg') else 'png'
        return data, ext
    if source_type == 'raster_crop':
        data = fetch(source['url'], referer=source.get('referer'))
        cropped = crop_raster(data, tuple(source['crop']))
        return cropped, 'png'
    raise ValueError(f'Unsupported source config for {slug}: {source_type}')


def main() -> None:
    for slug, source in SOURCES.items():
        try:
            data, kind = resolve_source(slug, source)
            if kind == 'svg':
                view_box = source.get('view_box') if isinstance(source, dict) else None
                tile_bg = source.get('tile_bg') if isinstance(source, dict) else None
                tile = wrap_svg_tile(slug, data, view_box=view_box, tile_bg=tile_bg)
            else:
                tile_bg = source.get('tile_bg') if isinstance(source, dict) else None
                tile = wrap_raster_tile(slug, data, kind, tile_bg=tile_bg)
            (OUT / f'{slug}.svg').write_text(tile, encoding='utf-8')
            print(f'OK {slug}')
        except Exception as exc:
            print(f'FAIL {slug}: {exc}')


if __name__ == '__main__':
    main()
