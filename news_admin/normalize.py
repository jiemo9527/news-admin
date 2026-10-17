"""文本与 URL 标准化：不调用模型，保证后续去重和缓存键稳定。"""

from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {'fbclid', 'gclid', 'yclid', 'mc_cid', 'mc_eid', 'spm', 'from', 'ref', 'ref_src', 'cmpid', 'share'}
ZERO_WIDTH_RE = re.compile('[\u200b-\u200f\u2060\ufeff]')
SPACE_RE = re.compile(r'\s+')


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    path = re.sub(r'/+', '/', parts.path or '/')
    path = re.sub(r'\.amp$|/amp$', '', path)
    path = path.rstrip('/') or '/'
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if not key.lower().startswith('utm_') and key.lower() not in TRACKING_PARAMS
    )
    return urlunsplit((parts.scheme.lower() or 'https', host, path if path != '/' else '', urlencode(query), ''))


def clean_text(text: str) -> str:
    text = html.unescape(text or '')
    text = unicodedata.normalize('NFKC', text)
    text = ZERO_WIDTH_RE.sub('', text)
    return SPACE_RE.sub(' ', text).strip()
