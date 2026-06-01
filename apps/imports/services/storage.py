import hashlib
import mimetypes
from datetime import datetime
from pathlib import Path

from django.conf import settings


def calculate_upload_checksum(uploaded_file) -> str:
    hasher = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        hasher.update(chunk)
    uploaded_file.seek(0)
    return hasher.hexdigest()


def persist_uploaded_file(uploaded_file, checksum: str) -> dict:
    extension = Path(uploaded_file.name).suffix.lower()
    today_path = datetime.now().strftime('%Y/%m/%d')
    target_dir = settings.IMPORT_RAW_DIR / today_path
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f'{checksum}{extension}'

    with target_path.open('wb') as destination:
        for chunk in uploaded_file.chunks():
            destination.write(chunk)

    uploaded_file.seek(0)
    mime_type = uploaded_file.content_type or mimetypes.guess_type(uploaded_file.name)[0] or ''
    return {
        'stored_path': str(target_path),
        'file_type': extension.lstrip('.'),
        'mime_type': mime_type,
        'size_bytes': uploaded_file.size,
        'checksum': checksum,
    }