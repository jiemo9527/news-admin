"""抓取：只用 RSS/Atom 拿列表，正文用 trafilatura 抽取；失败降级为 feed 摘要。

旧版为每家网站手写 CSS 选择器和几十条过滤规则，维护成本高且经常失效。
v2 只接受提供 feed 的来源，正文抽取交给通用算法。
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
import trafilatura
from bs4 import BeautifulSoup

from .models import Article
from .normalize import canonical_url, clean_text

USER_AGENT = 'Mozilla/5.0 (compatible; news-admin/2.0; +https://github.com/jiemo9527/news-admin)'
MIN_BODY_CHARS = 80


@dataclass
class Source:
    id: int
    name: str
    feed_url: str
    weight: float = 1.0
    max_items: int = 20
    fetch_body: bool = True
    enabled: bool = True


@dataclass
class FetchResult:
    source: Source
    articles: list[Article] = field(default_factory=list)
    error: str = ''


def _html_to_text(value: str) -> str:
    return clean_text(BeautifulSoup(value or '', 'lxml').get_text(' '))


def _entry_time(entry) -> datetime | None:
    parsed = entry.get('published_parsed') or entry.get('updated_parsed')
    return datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else None


def _extract_body(client: httpx.Client, url: str) -> str:
    try:
        response = client.get(url)
        if response.status_code >= 400:
            return ''
        text = trafilatura.extract(response.text, include_comments=False, include_tables=False, favor_precision=True) or ''
    except (httpx.HTTPError, ValueError):
        return ''
    text = clean_text(text)
    return text if len(text) >= MIN_BODY_CHARS else ''


def fetch_source(source: Source, now: datetime | None = None, max_age: timedelta = timedelta(hours=36), transport: httpx.BaseTransport | None = None) -> FetchResult:
    now = now or datetime.now(timezone.utc)
    result = FetchResult(source=source)
    with httpx.Client(headers={'User-Agent': USER_AGENT}, timeout=20, follow_redirects=True, transport=transport) as client:
        try:
            response = client.get(source.feed_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            result.error = f'{type(exc).__name__}: {exc}'[:300]
            return result
        feed = feedparser.parse(response.content)
        entries = []
        for entry in feed.entries:
            published = _entry_time(entry)
            if published and now - published > max_age:
                continue
            link = entry.get('link', '')
            title = clean_text(entry.get('title', ''))
            if not link or not title:
                continue
            entries.append((entry, published, link, title))
            if len(entries) >= source.max_items:
                break

        def build(item) -> Article:
            entry, published, link, title = item
            summary = _html_to_text(entry.get('summary', '') or entry.get('description', ''))
            body = _extract_body(client, link) if source.fetch_body else ''
            url = canonical_url(link)
            return Article(
                id=hashlib.sha1(url.encode()).hexdigest()[:16],
                source=source.name,
                url=url,
                title=title,
                body=body or summary,
                published_at=published,
                source_weight=source.weight,
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            result.articles = list(pool.map(build, entries))
    if not result.articles and not result.error:
        result.error = 'feed 中没有时间窗内的条目'
    return result
