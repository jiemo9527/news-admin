import io

import httpx
from PIL import Image

from news_admin.images import ThumbnailCache, choose_image


def png_bytes(w, h, color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new('RGB', (w, h), color).save(buf, 'PNG')
    return buf.getvalue()


def handler(request):
    path = request.url.path
    if path == '/big.png':
        return httpx.Response(200, content=png_bytes(1200, 800), headers={'content-type': 'image/png'})
    if path == '/tiny.png':
        return httpx.Response(200, content=png_bytes(40, 40), headers={'content-type': 'image/png'})
    if path == '/html':
        return httpx.Response(200, text='<html></html>', headers={'content-type': 'text/html'})
    return httpx.Response(404)


def test_thumbnail_is_resized_cached_and_served_as_webp(tmp_path):
    calls = []

    def counting(request):
        calls.append(request.url.path)
        return handler(request)

    cache = ThumbnailCache(tmp_path, transport=httpx.MockTransport(counting))
    key = cache.fetch('https://img.test/big.png')
    assert key
    data, mime = cache.read(key)
    assert mime == 'image/webp'
    image = Image.open(io.BytesIO(data))
    assert image.width <= 480 and image.height <= 320
    assert cache.fetch('https://img.test/big.png') == key
    assert calls == ['/big.png']


def test_tiny_images_icons_and_non_images_are_rejected(tmp_path):
    cache = ThumbnailCache(tmp_path, transport=httpx.MockTransport(handler))
    assert cache.fetch('https://img.test/tiny.png') == ''
    assert cache.fetch('https://img.test/html') == ''
    assert cache.fetch('https://img.test/missing.png') == ''
    assert cache.fetch('') == ''
    assert cache.read('../../etc/passwd') is None


def test_choose_image_skips_logos_and_placeholders():
    assert choose_image(['https://x/logo.png', 'https://x/default-share.jpg', 'https://x/photo.jpg']) == 'https://x/photo.jpg'
    assert choose_image(['https://x/sprite.svg']) == ''
