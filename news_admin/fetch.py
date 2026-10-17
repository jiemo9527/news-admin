"""抓取：按来源类型取得文章列表，再用 trafilatura 抽取正文；正文失败时退回列表摘要。

来源类型（kind）：
- rss      RSS/Atom
- jsonfeed JSON Feed（如 buzzing.cc 的路透社中文翻译源，优先取简体翻译标题）
- cctv     央视新闻列表接口（JSONP）
- html     网页列表页：按 link_pattern 正则挑文章链接，标题和发布时间从文章页元数据中读取

旧版为每家网站写了上千行选择器和特例过滤；v2 对网页源只保留一个链接正则，
标题、时间、正文都交给通用元数据 / 正文抽取，换站点只改配置不改代码。
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
import httpx
import trafilatura
from bs4 import BeautifulSoup

from .models import Article
from .normalize import canonical_url, clean_text

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'
MIN_BODY_CHARS = 80
SHANGHAI = ZoneInfo('Asia/Shanghai')
SOURCE_KINDS = {'rss': 'RSS/Atom', 'jsonfeed': 'JSON Feed', 'cctv': '央视列表接口', 'html': '网页列表页'}
_JSONP_RE = re.compile(r'^\s*[\w$.]+\((.*)\)\s*;?\s*$', re.S)
_DATE_IN_URL_RE = re.compile(r'(20\d{2})[-/]?(\d{2})[-/]?(\d{2})')


@dataclass
class Source:
    id: int
    name: str
    feed_url: str
    weight: float = 1.0
    max_items: int = 20
    fetch_body: bool = True
    enabled: bool = True
    kind: str = 'rss'
    link_pattern: str = ''


@dataclass
class FetchResult:
    source: Source
    articles: list[Article] = field(default_factory=list)
    error: str = ''


@dataclass
class _Entry:
    url: str
    title: str = ''
    summary: str = ''
    published: datetime | None = None


def _html_to_text(value: str) -> str:
    return clean_text(BeautifulSoup(value or '', 'lxml').get_text(' '))


def _parse_time(value: Any, assume_tz: ZoneInfo | timezone = timezone.utc) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assume_tz)
    return parsed.astimezone(timezone.utc)


# ---------- list fetchers ----------

def _list_rss(client: httpx.Client, source: Source) -> list[_Entry]:
    response = client.get(source.feed_url)
    response.raise_for_status()
    entries = []
    for entry in feedparser.parse(response.content).entries:
        parsed = entry.get('published_parsed') or entry.get('updated_parsed')
        entries.append(_Entry(
            url=entry.get('link', ''),
            title=clean_text(entry.get('title', '')),
            summary=_html_to_text(entry.get('summary', '') or entry.get('description', '')),
            published=datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else None,
        ))
    return entries


def _list_jsonfeed(client: httpx.Client, source: Source) -> list[_Entry]:
    response = client.get(source.feed_url)
    response.raise_for_status()
    entries = []
    for item in response.json().get('items', []):
        if not isinstance(item, dict):
            continue
        translations = item.get('_translations') if isinstance(item.get('_translations'), dict) else {}
        simplified = translations.get('zh-Hans') if isinstance(translations.get('zh-Hans'), dict) else {}
        entries.append(_Entry(
            url=str(item.get('url') or item.get('external_url') or ''),
            title=clean_text(simplified.get('title') or item.get('title', '')),
            summary=clean_text(item.get('summary') or _html_to_text(item.get('content_html', ''))),
            published=_parse_time(item.get('_original_published') or item.get('date_published')),
        ))
    return entries


def _list_cctv(client: httpx.Client, source: Source) -> list[_Entry]:
    response = client.get(source.feed_url)
    response.raise_for_status()
    match = _JSONP_RE.match(response.text)
    payload = json.loads(match.group(1) if match else response.text)
    return [
        _Entry(
            url=str(item.get('url', '')),
            title=clean_text(item.get('title', '')),
            summary=clean_text(item.get('brief', '')),
            published=_parse_time(item.get('focus_date'), SHANGHAI),
        )
        for item in payload.get('data', {}).get('list', [])
    ]


def _list_html(client: httpx.Client, source: Source) -> list[_Entry]:
    if not source.link_pattern:
        raise ValueError('网页列表源必须配置 link_pattern（文章链接正则）')
    pattern = re.compile(source.link_pattern)
    response = client.get(source.feed_url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'lxml')
    seen: dict[str, _Entry] = {}
    for anchor in soup.select('a[href]'):
        try:
            url = str(response.url.join(str(anchor['href']).strip()))
        except (httpx.InvalidURL, ValueError):
            continue
        if not pattern.search(url):
            continue
        entry = seen.setdefault(url, _Entry(url=url))
        text = clean_text(anchor.get_text(' '))
        if len(text) > len(entry.title):
            entry.title = text
        date = _DATE_IN_URL_RE.search(url)
        if date and not entry.published:
            try:
                entry.published = datetime(int(date[1]), int(date[2]), int(date[3]), 12, tzinfo=SHANGHAI).astimezone(timezone.utc)
            except ValueError:
                pass
    return list(seen.values())


LISTERS = {'rss': _list_rss, 'jsonfeed': _list_jsonfeed, 'cctv': _list_cctv, 'html': _list_html}


# ---------- article page ----------

_TITLE_SUFFIX_RE = re.compile(r'\s*(?:--|-|_|\|)\s*[^-_|]{1,12}(?:网|报|社|新闻|News|NEWS)\s*$')


def _page_title(soup: BeautifulSoup) -> str:
    """标题优先级：og:title > h1 > <title>（去掉「--国际--人民网」这类站点后缀和「栏目 |」前缀）。"""
    og = soup.select_one('meta[property="og:title"]')
    if og and clean_text(og.get('content', '')):
        return clean_text(og['content'])
    h1 = soup.select_one('h1')
    if h1 and clean_text(h1.get_text(' ')):
        return clean_text(h1.get_text(' '))
    title = clean_text(soup.title.get_text(' ') if soup.title else '')
    previous = None
    while title != previous:
        previous = title
        title = _TITLE_SUFFIX_RE.sub('', title)
        title = re.sub(r'\s*--\s*[^-]{1,6}$', '', title)
    if ' | ' in title:
        head, tail = title.split(' | ', 1)
        title = tail if len(head) <= 8 else head
    return title.strip()

def _fetch_page(client: httpx.Client, url: str) -> tuple[str, str, datetime | None]:
    """返回 (正文, 页面标题, 发布时间)。失败时返回空值，由调用方降级。"""
    try:
        response = client.get(url)
        if response.status_code >= 400:
            return '', '', None
        data = trafilatura.bare_extraction(
            response.text, url=url, favor_precision=True, with_metadata=True, include_comments=False, include_tables=False
        )
    except (httpx.HTTPError, ValueError):
        return '', '', None
    meta: dict[str, Any] = {}
    if data is not None:
        meta = data.as_dict() if hasattr(data, 'as_dict') else dict(data)
    text = clean_text(meta.get('text') or '')
    soup = BeautifulSoup(response.text, 'lxml')
    published = None
    for selector in (
        'meta[property="article:published_time"]', 'meta[name="publishdate"]',
        'meta[name="PubDate"]', 'meta[itemprop="datePublished"]',
    ):
        tag = soup.select_one(selector)
        if tag and tag.get('content'):
            published = _parse_time(tag['content'], SHANGHAI)
            if published:
                break
    if published is None and meta.get('date'):
        published = _parse_time(f"{meta['date']}T12:00:00", SHANGHAI)
    title = _page_title(soup) or clean_text(meta.get('title') or '')
    return (text if len(text) >= MIN_BODY_CHARS else ''), title, published


def fetch_source(
    source: Source, now: datetime | None = None, max_age: timedelta = timedelta(hours=36),
    transport: httpx.BaseTransport | None = None,
) -> FetchResult:
    now = now or datetime.now(timezone.utc)
    result = FetchResult(source=source)
    lister = LISTERS.get(source.kind)
    if lister is None:
        result.error = f'未知来源类型：{source.kind}'
        return result
    is_html = source.kind == 'html'
    with httpx.Client(headers={'User-Agent': USER_AGENT}, timeout=20, follow_redirects=True, transport=transport) as client:
        try:
            listed = lister(client, source)
        except (httpx.HTTPError, ValueError) as exc:
            result.error = f'{type(exc).__name__}: {exc}'[:300]
            return result

        def fresh(entry: _Entry) -> bool:
            return entry.published is None or now - entry.published <= max_age

        # 网页源的标题和时间要打开文章页才可靠，多取一些候选再按时间过滤
        limit = source.max_items * (2 if is_html else 1)
        candidates = [e for e in listed if e.url and fresh(e) and (e.title or is_html)][:limit]

        def build(entry: _Entry) -> Article | None:
            body, page_title, page_time = ('', '', None)
            if source.fetch_body or is_html:
                body, page_title, page_time = _fetch_page(client, entry.url)
            published = page_time or entry.published
            if published and now - published > max_age:
                return None
            title = (page_title or entry.title) if is_html else entry.title
            if not title:
                return None
            url = canonical_url(entry.url)
            return Article(
                id=hashlib.sha1(url.encode()).hexdigest()[:16],
                source=source.name,
                url=url,
                title=title,
                body=body or entry.summary,
                published_at=published,
                source_weight=source.weight,
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            result.articles = [a for a in pool.map(build, candidates) if a is not None][: source.max_items]
    if not result.articles:
        result.error = '没有时间窗内的条目'
    return result
