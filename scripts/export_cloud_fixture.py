"""Export DB fixture with valid UTF-8 strings for Cloud SQL loaddata."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import django

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.apps import apps
from django.core import serializers
from django.core.serializers.json import DjangoJSONEncoder

SKIP_APPS = {'contenttypes', 'sessions', 'admin'}
SKIP_MODELS = {
    'auth.permission',
    'imports.rawimportfile',
}


def clean_value(value):
    if isinstance(value, str):
        return value.encode('utf-8', errors='replace').decode('utf-8')
    if isinstance(value, dict):
        return {key: clean_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_value(item) for item in value]
    return value


def iter_models():
    for model in apps.get_models():
        label = model._meta.label_lower
        if model._meta.app_label in SKIP_APPS or label in SKIP_MODELS:
            continue
        yield model


def main() -> None:
    output = ROOT / 'data' / 'cloud_fixture.json'
    payload = []
    counts: dict[str, int] = {}

    for model in iter_models():
        queryset = model.objects.all().order_by('pk')
        count = queryset.count()
        if not count:
            continue
        chunk = serializers.serialize(
            'python',
            queryset.iterator(chunk_size=500),
            use_natural_foreign_keys=True,
            use_natural_primary_keys=True,
        )
        for item in chunk:
            item['fields'] = clean_value(item['fields'])
            payload.append(item)
        counts[model._meta.label] = count

    output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, cls=DjangoJSONEncoder),
        encoding='utf-8',
    )
    output.read_bytes().decode('utf-8')

    print(f'Wrote {output} ({output.stat().st_size} bytes, {len(payload)} objects)')
    for label, count in sorted(counts.items()):
        print(f'  {label}: {count}')


if __name__ == '__main__':
    main()
