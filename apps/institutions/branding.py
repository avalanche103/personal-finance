from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders

INSTITUTION_LOGO_SLUGS = frozenset(
    {
        'aigenis',
        'alfabank',
        'belarusbank',
        'binance',
        'bnb-bank',
        'finstore',
        'priorlife',
        'stravita',
    }
)

INSTITUTION_ACCENT_COLORS = {
    'aigenis': '#1B3A6B',
    'alfabank': '#EF3124',
    'belarusbank': '#0054A6',
    'binance': '#F0B90B',
    'bnb-bank': '#006838',
    'bynex': '#2563EB',
    'finstore': '#00A3E0',
    'income-sources': '#6B7280',
    'priorlife': '#E85D04',
    'stravita': '#003DA5',
}


def institution_logo_path(slug: str) -> str | None:
    normalized = (slug or '').strip().lower()
    if normalized not in INSTITUTION_LOGO_SLUGS:
        return None
    static_path = f'img/institutions/{normalized}.svg'
    if finders.find(static_path):
        return static_path
    file_path = Path(settings.BASE_DIR) / 'static' / static_path
    return static_path if file_path.is_file() else None


def institution_initials(name: str) -> str:
    cleaned = (name or '').strip()
    if not cleaned:
        return '?'
    parts = cleaned.replace('-', ' ').split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return cleaned[:2].upper()


def institution_accent_color(slug: str) -> str:
    return INSTITUTION_ACCENT_COLORS.get((slug or '').strip().lower(), '#6B7280')
