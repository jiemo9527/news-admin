"""新闻配图缩略图：下载 → 校验 → 缩放为 WebP → 本地缓存，经 /thumb/<key> 提供。

不直接把外站图片地址放进页面：
- 部分站点防盗链（检查 Referer），直链会显示破图；
- 读者浏览器会向第三方暴露访问记录；
- 原图动辄几百 KB，缩略后约 10-30 KB。
"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError

MAX_BYTES = 8 * 1024 * 1024
THUMB_SIZE = (480, 320)
MIN_SIDE = 120
_KEY_RE = re.compile(r'^[0-9a-f]{32}$')
_BAD_IMAGE_RE = re.compile(r'logo|icon|sprite|avatar|default|placeholder|blank|\.svg(\?|$)|\.gif(\?|$)', re.I)
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'


def choose_image(candidates: list[str]) -> str:
    """从候选中选第一张像新闻配图的（排除 logo、图标、占位图、svg/gif）。"""
    return next((url for url in candidates if url and not _BAD_IMAGE_RE.search(url)), '')


class ThumbnailCache:
    def __init__(self, directory: Path, transport: httpx.BaseTransport | None = None):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self._http = httpx.Client(headers={'User-Agent': USER_AGENT}, timeout=15, follow_redirects=True, transport=transport)

    @staticmethod
    def key_for(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:32]

    def _path(self, key: str) -> Path:
        return self.directory / f'{key}.webp'

    def fetch(self, url: str) -> str:
        """下载并缓存缩略图，返回 key；不可用时返回空串（失败结果也会记下，避免反复请求）。"""
        if not url or not url.startswith(('http://', 'https://')) or _BAD_IMAGE_RE.search(url):
            return ''
        key = self.key_for(url)
        path = self._path(key)
        if path.exists():
            return key
        miss = self.directory / f'{key}.miss'
        if miss.exists():
            return ''
        try:
            response = self._http.get(url)
            if response.status_code >= 400 or not response.headers.get('content-type', '').startswith('image/'):
                raise ValueError('not an image')
            if len(response.content) > MAX_BYTES:
                raise ValueError('too large')
            image = Image.open(io.BytesIO(response.content))
            if min(image.size) < MIN_SIDE:
                raise ValueError('too small')
            image = image.convert('RGB')
            image.thumbnail(THUMB_SIZE)
            buffer = io.BytesIO()
            image.save(buffer, 'WEBP', quality=78, method=4)
        except (httpx.HTTPError, ValueError, UnidentifiedImageError, OSError):
            miss.touch()
            return ''
        path.write_bytes(buffer.getvalue())
        return key

    def prune(self, retention_days: int = 90) -> int:
        """删除超过保留期的缩略图与失败记录。"""
        import time

        cutoff = time.time() - retention_days * 86400
        removed = 0
        for path in self.directory.iterdir():
            if path.suffix in ('.webp', '.miss') and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += int(path.suffix == '.webp')
        return removed

    def read(self, key: str) -> tuple[bytes, str] | None:
        if not _KEY_RE.match(key or ''):
            return None
        path = self._path(key)
        return (path.read_bytes(), 'image/webp') if path.exists() else None
