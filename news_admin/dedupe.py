"""一级去重：精确 URL + 文本近重复（通稿转载）。不调用模型，代价最低，最先执行。

近重复用字符 3-gram 的包含度（较短文本的 shingle 有多少出现在较长文本中），
对「同一篇通稿删改几个字、加记者署名」这类转载最稳；RSS 摘要很短时
SimHash 抖动过大，因此不用。
"""

from __future__ import annotations

import re

from .models import Article
from .normalize import canonical_url

NEAR_DUPLICATE_THRESHOLD = 0.8
MIN_TEXT_CHARS = 30
_TOKEN_RE = re.compile(r'[a-z0-9]+|[\u4e00-\u9fff]', re.I)


def shingles(text: str, size: int = 3) -> set[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    if len(tokens) < size:
        return {''.join(tokens)} if tokens else set()
    return {''.join(tokens[i:i + size]) for i in range(len(tokens) - size + 1)}


def text_similarity(left: str, right: str) -> float:
    a, b = shingles(left), shingles(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _merge(keeper: Article, dropped: Article) -> Article:
    sources = [*keeper.duplicate_sources]
    for name in [dropped.source, *dropped.duplicate_sources]:
        if name != keeper.source and name not in sources:
            sources.append(name)
    return keeper.model_copy(update={'duplicate_sources': sources})


def _prefer(left: Article, right: Article) -> tuple[Article, Article]:
    """信息量更大的一篇作为保留项。"""
    return (left, right) if len(left.body) >= len(right.body) else (right, left)


def dedupe_articles(articles: list[Article]) -> list[Article]:
    kept: list[Article] = []
    by_url: dict[str, int] = {}
    for article in articles:
        key = canonical_url(article.url)
        index = by_url.get(key)
        if index is None and len(article.body) >= MIN_TEXT_CHARS:
            index = next(
                (
                    i
                    for i, existing in enumerate(kept)
                    if len(existing.body) >= MIN_TEXT_CHARS
                    and text_similarity(existing.body, article.body) >= NEAR_DUPLICATE_THRESHOLD
                ),
                None,
            )
        if index is None:
            by_url[key] = len(kept)
            kept.append(article)
            continue
        keeper, dropped = _prefer(kept[index], article)
        kept[index] = _merge(keeper, dropped)
        by_url[key] = index
    return kept
