from __future__ import annotations

import io
import importlib
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

from .config import Paths

USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36'
PLACEHOLDER_MARKERS = (
    'diandi.png',
    'placeholder',
    'default',
    'logo',
    'icon',
    'ewm',
    'qrcode',
    'zxcode',
    'share_ap',
    'qrcode-app',
)
THUMBNAIL_SIZE = (480, 360)
CACHE_VERSION = 2


def load_pillow_modules() -> tuple[Any, Any]:
    image_module = importlib.import_module('PIL.Image')
    image_ops_module = importlib.import_module('PIL.ImageOps')
    return image_module, image_ops_module


def is_usable_thumbnail_url(url: str) -> bool:
    value = url.strip()
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme not in {'http', 'https'}:
        return False
    lowered = value.lower()
    if lowered.endswith('.svg'):
        return False
    return not any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def thumbnail_proxy_src(url: str) -> str:
    return f'/thumb?src={quote(url, safe="")}'


def _cache_paths(url: str) -> tuple[Path, Path]:
    key = sha256(url.encode('utf-8')).hexdigest()
    return Paths.thumbnails_dir / f'{key}.jpg', Paths.thumbnails_dir / f'{key}.json'


def _read_meta(meta_path: Path) -> dict[str, Any]:
    try:
        return json.loads(meta_path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def get_cached_thumbnail(url: str) -> tuple[Path, str]:
    if not is_usable_thumbnail_url(url):
        raise ValueError('thumbnail url is not usable')
    file_path, meta_path = _cache_paths(url)
    if file_path.exists() and meta_path.exists():
        meta = _read_meta(meta_path)
        if int(meta.get('cache_version', 0) or 0) == CACHE_VERSION:
            content_type = str(meta.get('content_type', '')).strip() or 'image/jpeg'
            return file_path, content_type

    response = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=20)
    response.raise_for_status()
    content_type = str(response.headers.get('Content-Type', '')).split(';', 1)[0].strip()
    if not content_type.startswith('image/'):
        raise ValueError('thumbnail is not an image response')

    image_module, image_ops_module = load_pillow_modules()
    image = image_module.open(io.BytesIO(response.content))
    image = image_ops_module.exif_transpose(image)
    if image.mode not in {'RGB', 'L'}:
        image = image.convert('RGB')
    elif image.mode == 'L':
        image = image.convert('RGB')
    image = image_ops_module.fit(image, THUMBNAIL_SIZE, method=image_module.Resampling.LANCZOS, centering=(0.5, 0.5))
    image.save(file_path, format='JPEG', quality=86, optimize=True)
    meta_path.write_text(
        json.dumps({'cache_version': CACHE_VERSION, 'content_type': 'image/jpeg', 'source_content_type': content_type, 'url': url, 'cached_at': datetime.now(timezone.utc).isoformat(), 'size': list(THUMBNAIL_SIZE)}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return file_path, 'image/jpeg'
