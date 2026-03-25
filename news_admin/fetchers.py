from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import re
import xml.etree.ElementTree as ET
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from .config import Paths, TIMEZONE
from .text_normalization import decode_response_text, is_boilerplate_text, normalize_items
from .thumbnail_cache import is_usable_thumbnail_url

USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36'
TIMEOUT = 25
CCTV_EXTRA_URLS = [
    'https://news.cctv.com/china/',
    'https://news.cctv.com/world/',
    'https://news.cctv.com/tech/index.shtml',
    'https://news.cctv.com/law/',
]
CCTV_ARTICLE_RE = re.compile(r'news\.cctv\.com/20\d{2}/\d{2}/\d{2}/ARTI\w+\.shtml', re.I)
REUTERS_HTML_SKIP_TITLES = {
    'latest stories',
    'skip to main content skip to main content',
    'subscribe',
    'only on reuters',
    'search all topics search all topics search all topics',
    'commodities & energy',
    'economics & central banking',
    'equities',
    'politics & general news',
    'middle east',
    'americas',
    'business & finance',
}
REUTERS_HTML_SKIP_SUBSTRINGS = (
    'search all topics',
    'skip to main content',
    'subscribe',
)
REUTERS_SKIP_URL_PATTERNS = [
    re.compile(r'/sports/', re.I),
    re.compile(r'/lifestyle/', re.I),
    re.compile(r'--flm-', re.I),
    re.compile(r'/pt/', re.I),
    re.compile(r'/es/', re.I),
    re.compile(r'/jp/', re.I),
]
CNN_SKIP_TITLE_PATTERNS = [
    re.compile(r'^analysis\s*:', re.I),
    re.compile(r'^sign up to our newsletter', re.I),
    re.compile(r'^subscribe to', re.I),
    re.compile(r'^watch live', re.I),
    re.compile(r'^live updates?', re.I),
    re.compile(r'what you need to know', re.I),
    re.compile(r'^gallery\b', re.I),
]
CNN_SKIP_URL_PATTERNS = [
    re.compile(r'/live-news/', re.I),
    re.compile(r'/specials/', re.I),
    re.compile(r'(^|[-/])analysis([-/]|$)', re.I),
    re.compile(r'/videos?/', re.I),
    re.compile(r'/travel-newsletter', re.I),
    re.compile(r'/gallery/', re.I),
    re.compile(r'-explainer-', re.I),
]
XINHUA_SKIP_URL_PATTERNS = [
    re.compile(r'/comments/', re.I),
    re.compile(r'/zt/', re.I),
]
NYT_SKIP_TITLE_PATTERNS = [
    re.compile(r"^here['’]s the latest$", re.I),
    re.compile(r'^here is the latest$', re.I),
    re.compile(r'^what happened on ', re.I),
    re.compile(r'^这是最新的$'),
    re.compile(r'^这是\d+月\d+日发生的事情$'),
]
NYT_SKIP_URL_PATTERNS = [
    re.compile(r'/live/.*/heres-the-latest/?$', re.I),
    re.compile(r'/live/.*/here-is-the-latest/?$', re.I),
    re.compile(r'/live/.*/this-is-what-happened-on-[^/]+/?$', re.I),
]
GENERIC_SKIP_TITLE_PATTERNS = [
    re.compile(r'^\d+\s*watch\s*:', re.I),
    re.compile(r'^watch\s*:', re.I),
    re.compile(r'^photos?\s*:', re.I),
    re.compile(r'^video\s*:', re.I),
]
SOFT_NEWS_TITLE_PATTERNS = [
    re.compile(r'记者观察'),
    re.compile(r'世界观察'),
    re.compile(r'群像'),
    re.compile(r'嘉年华'),
    re.compile(r'赏花'),
    re.compile(r'圈粉'),
    re.compile(r'诗与远方'),
    re.compile(r'惬意'),
    re.compile(r'红地毯'),
    re.compile(r'画廊|图集|组图'),
    re.compile(r'(?:^|[（(])视频(?:[)）]|$)'),
    re.compile(r'\boscars?\b', re.I),
    re.compile(r'\bred\s+carpet\b', re.I),
    re.compile(r'\bgallery\b', re.I),
    re.compile(r'\bphotos?\b', re.I),
    re.compile(r'\bvideo\b', re.I),
]
SOFT_NEWS_URL_PATTERNS = [
    re.compile(r'/style/', re.I),
    re.compile(r'/travel/', re.I),
    re.compile(r'/sports?/', re.I),
    re.compile(r'/arts/', re.I),
    re.compile(r'/television/', re.I),
    re.compile(r'/fashion/', re.I),
    re.compile(r'/gallery/', re.I),
    re.compile(r'/photos?/', re.I),
    re.compile(r'/videos?/', re.I),
]
DOMESTIC_NON_HARD_NEWS_TITLE_PATTERNS = [
    re.compile(r'正确政绩观|学习教育|中国式现代化|新征程|中国路|为民情怀|实干担当'),
    re.compile(r'厚植|践行|高质量发展'),
    re.compile(r'春耕备耕|春灌累计灌溉|大运河连续五年'),
    re.compile(r'花粉季|山野菜|春菜|养虾人|速查风险'),
    re.compile(r'如何跟上|看中国'),
    re.compile(r'全球文明倡议|世界共鸣|国际传播.*丝路奖|与中国走近'),
    re.compile(r'寰宇平|十五五|重大工程建设|未来五年.*布局'),
]
IMAGE_CREDIT_MARKERS = ('getty images', 'afp', 'reuters', 'ap', 'redux', 'picture alliance', 'global images ukraine')
NON_CONTENT_IMAGE_PATTERNS = [
    re.compile(r'(?:^|/)(?:ewm|qrcode)[^/]*\.(?:jpg|jpeg|png|webp)$', re.I),
    re.compile(r'(?:^|/)(?:sharelogo|logo_share|share)\.(?:jpg|jpeg|png|webp)$', re.I),
    re.compile(r'(?:^|/)(?:newslog200|yangshiwang_logo)[^/]*\.(?:jpg|jpeg|png|webp)$', re.I),
    re.compile(r'(?:^|/)zxcode_[^/]*\.(?:jpg|jpeg|png|webp)$', re.I),
    re.compile(r'(?:^|/)logo[^/]*\.(?:jpg|jpeg|png|webp)$', re.I),
]
URL_DATE_PATTERNS = [
    re.compile(r'/((?:19|20)\d{2})/(\d{2})/(\d{2})(?:/|$)', re.I),
    re.compile(r'/n1/((?:19|20)\d{2})/(\d{2})(\d{2})(?:/|$)', re.I),
    re.compile(r'/((?:19|20)\d{2})(\d{2})(\d{2})(?:/|$)', re.I),
    re.compile(r'story((?:19|20)\d{2})(\d{2})(\d{2})(?:-|$)', re.I),
]
MAX_DATED_URL_AGE_DAYS = 120
MIN_FETCHED_SNIPPET_LENGTH = 45
MIN_FETCHED_SNIPPET_SENTENCES = 2
MAX_FETCHED_SNIPPET_SENTENCES = 3
MAX_FETCHED_SNIPPET_CHARS = 360
DEFAULT_COLLECTION_AGE = timedelta(hours=24)
FALLBACK_COLLECTION_AGE = timedelta(hours=48)
SHANGHAI_TZ = ZoneInfo(TIMEZONE)

BODY_PARAGRAPH_SELECTORS = {
    'RT': ['.article__text.text p', '.article__text p', '.article__summary'],
    '新华社': ['#detail p'],
}

BODY_IMAGE_SELECTORS = {
    '新华社': ['#detail img', '#detail source'],
    '人民网': ['#rm_txt_zw img', '.rm_txt_con img', '.box_pic img'],
    '央视新闻': ['.cnt_bd img', '#content_area img', '.content_area img', '.image img', '.text_area img'],
}

GENERIC_BODY_SELECTORS = [
    'section[name="articleBody"] p',
    '[data-testid="article-body"] p',
    '.StoryBodyCompanionColumn p',
    '.article-content p',
    '.ArticleBody-articleBody p',
    '[itemprop="articleBody"] p',
    'article p',
    '.article p',
    '.content p',
    '.text p',
    'p',
]

BYLINE_PATTERNS = [
    re.compile(r'^(?:by|作者|记者|编辑|撰稿人)\b', re.I),
    re.compile(r'^(?:by|作者|记者|编辑|撰稿人)\s*[:：\-]', re.I),
]

BYLINE_ROLE_PATTERNS = [
    re.compile(r'\b(?:director|editor|columnist|correspondent|research fellow|expert|institute|discussion club)\b', re.I),
    re.compile(r'(?:研究员|所长|记者|编辑|专栏作家|评论员|客座研究员|专家)'),
]


def clean_text(value: object) -> str:
    if value is None:
        return ''
    if isinstance(value, list):
        text = ' '.join(str(item) for item in value)
    else:
        text = str(value)
    decoded = unescape(text)
    plain = BeautifulSoup(decoded, 'html.parser').get_text(' ', strip=True) if '<' in decoded and '>' in decoded else decoded
    return re.sub(r'\s+', ' ', plain).strip()


def strip_title_prefix(text: str, title: str) -> str:
    cleaned = clean_text(text)
    title_text = clean_text(title)
    if not cleaned or not title_text:
        return cleaned
    escaped_title = re.escape(title_text)
    return re.sub(rf'^{escaped_title}\s*[-:：|｜,.，。]*\s*', '', cleaned, count=1, flags=re.IGNORECASE).strip()


def text_content_length(text: str) -> int:
    return len(re.sub(r'[\s。！？!?；;，,“”"\'（）()《》<>·:：\-_,.]+', '', text))


def split_text_sentences(text: str) -> list[str]:
    cleaned = clean_text(text)
    if not cleaned:
        return []
    parts = re.split(r'(?<=[。！？!?])\s+|(?<=[.!?])\s+(?=[A-Z0-9"“\'])', cleaned)
    sentences: list[str] = []
    for part in parts:
        candidate = clean_text(part).strip()
        candidate = re.sub(r'^[•·●]+\s*', '', candidate)
        if text_content_length(candidate) >= 12:
            sentences.append(candidate)
    return sentences or ([cleaned] if text_content_length(cleaned) >= 12 else [])


def is_byline_text(text: str) -> bool:
    cleaned = clean_text(text)
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if any(pattern.search(cleaned) for pattern in BYLINE_PATTERNS):
        return True
    if text_content_length(cleaned) > 180:
        return False
    if any(pattern.search(cleaned) for pattern in BYLINE_ROLE_PATTERNS) and not re.search(r'[。！？!?]', cleaned):
        return True
    return lowered.startswith('by ')


def snippet_sentence_count(text: str) -> int:
    return len(split_text_sentences(text))


def ensure_sentence_ending(text: str) -> str:
    cleaned = clean_text(text)
    if not cleaned:
        return ''
    if cleaned[-1] in '.!?。！？':
        return cleaned
    return f'{cleaned}。' if re.search(r'[\u4e00-\u9fff]', cleaned) else f'{cleaned}.'


def ordered_body_selectors(source_name: str) -> list[str]:
    selectors = list(BODY_PARAGRAPH_SELECTORS.get(source_name, []))
    for selector in GENERIC_BODY_SELECTORS:
        if selector not in selectors:
            selectors.append(selector)
    return selectors


def build_snippet_from_candidate_texts(candidate_texts: list[str], title_text: str = '') -> str:
    selected: list[str] = []
    seen: set[str] = set()
    for text in candidate_texts:
        for sentence in split_text_sentences(text):
            candidate = clean_text(sentence)
            if not candidate:
                continue
            if title_text and candidate == title_text:
                continue
            if text_content_length(candidate) < 12:
                continue
            lowered = candidate.lower()
            if 'copyright' in lowered or 'all rights reserved' in lowered:
                continue
            if is_boilerplate_text(candidate) or is_byline_text(candidate):
                continue
            normalized = re.sub(r'\W+', '', lowered, flags=re.UNICODE)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            selected.append(ensure_sentence_ending(candidate))
            joined = ' '.join(selected).strip()
            if len(selected) >= MIN_FETCHED_SNIPPET_SENTENCES and text_content_length(joined) >= MIN_FETCHED_SNIPPET_LENGTH:
                return joined[:MAX_FETCHED_SNIPPET_CHARS].strip()
            if len(selected) >= MAX_FETCHED_SNIPPET_SENTENCES:
                return joined[:MAX_FETCHED_SNIPPET_CHARS].strip()
    joined = ' '.join(selected[:MAX_FETCHED_SNIPPET_SENTENCES]).strip()
    return joined[:MAX_FETCHED_SNIPPET_CHARS].strip()


def cleaned_feed_snippet_candidates(title: str, *values: object) -> list[str]:
    title_text = clean_text(title)
    candidates: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = strip_title_prefix(clean_text(value), title_text)
        cleaned = re.sub(r'原文地址\s*:\s*https?://\S+', ' ', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\(\s*https?://\S+\s*\)', ' ', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\(\s*www\.[^)]+\)', ' ', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\bwhy it matters\b', ' ', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\b\d{1,2}:\d{2}\b', ' ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -:：;；,.，。')
        if not cleaned:
            continue
        normalized = cleaned.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(cleaned)
    return candidates


def feed_snippet_from_values(title: str, *values: object) -> str:
    candidates = cleaned_feed_snippet_candidates(title, *values)
    return build_snippet_from_candidate_texts(candidates, clean_text(title))


def clause_split_snippet(text: str) -> str:
    cleaned = clean_text(text)
    if not cleaned or snippet_sentence_count(cleaned) >= MIN_FETCHED_SNIPPET_SENTENCES:
        return cleaned
    clauses = [clean_text(part).strip(' -:：;；,.，') for part in re.split(r'[，,;；:：]\s*', cleaned)]
    selected = [clause for clause in clauses if text_content_length(clause) >= 10]
    if len(selected) < 2:
        return cleaned
    rebuilt = selected[:MAX_FETCHED_SNIPPET_SENTENCES]
    separator = '。 ' if re.search(r'[\u4e00-\u9fff]', cleaned) else '. '
    snippet = separator.join(rebuilt).strip()
    if snippet and snippet[-1] not in '.!?。！？':
        snippet += '。' if separator.startswith('。') else '.'
    return snippet[:MAX_FETCHED_SNIPPET_CHARS].strip()


def is_reuters_html_article(title: str, href: str) -> bool:
    parsed = urlparse(href)
    path = parsed.path.strip('/')
    lowered_title = title.lower().strip()
    if parsed.fragment:
        return False
    if lowered_title in REUTERS_HTML_SKIP_TITLES:
        return False
    if not path or path in {'news', 'feed', 'only-on-reuters'}:
        return False
    if path.startswith(('category/', 'topic/', 'region/')):
        return False
    if '/' in path:
        return False
    if any(marker in lowered_title for marker in REUTERS_HTML_SKIP_SUBSTRINGS):
        return False
    return True


def is_cnn_article(title: str, href: str) -> bool:
    if not href:
        return False
    lowered_title = title.lower().strip()
    if any(pattern.search(lowered_title) for pattern in CNN_SKIP_TITLE_PATTERNS):
        return False
    if any(pattern.search(href) for pattern in CNN_SKIP_URL_PATTERNS):
        return False
    parsed = urlparse(href)
    path = parsed.path or ''
    if not re.search(r'/20\d{2}/\d{2}/\d{2}/', path):
        return False
    return True


def normalize_to_shanghai(value: datetime) -> datetime:
    return value.replace(tzinfo=SHANGHAI_TZ) if value.tzinfo is None else value.astimezone(SHANGHAI_TZ)


def parse_published_value(raw: object) -> datetime | None:
    text = clean_text(raw)
    if not text:
        return None
    for parser in [datetime.fromisoformat, parsedate_to_datetime]:
        try:
            candidate = parser(text.replace('Z', '+00:00')) if parser is datetime.fromisoformat else parser(text)
            if candidate is not None:
                return normalize_to_shanghai(candidate)
        except Exception:
            continue
    chinese_match = re.search(r'((?:19|20)\d{2})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?', text)
    if chinese_match:
        year, month, day, hour, minute, second = chinese_match.groups()
        try:
            return datetime(
                int(year),
                int(month),
                int(day),
                int(hour or '0'),
                int(minute or '0'),
                int(second or '0'),
                tzinfo=SHANGHAI_TZ,
            )
        except ValueError:
            return None
    compact_match = re.search(r'((?:19|20)\d{2})\s*(\d{1,2})\s*/\s*(\d{1,2})\s*(\d{1,2}):(\d{2})(?::(\d{2}))?', text)
    if compact_match:
        year, month, day, hour, minute, second = compact_match.groups()
        try:
            return datetime(
                int(year),
                int(month),
                int(day),
                int(hour or '0'),
                int(minute or '0'),
                int(second or '0'),
                tzinfo=SHANGHAI_TZ,
            )
        except ValueError:
            return None
    western_match = re.search(r'((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?', text)
    if western_match:
        year, month, day, hour, minute, second = western_match.groups()
        try:
            return datetime(
                int(year),
                int(month),
                int(day),
                int(hour or '0'),
                int(minute or '0'),
                int(second or '0'),
                tzinfo=SHANGHAI_TZ,
            )
        except ValueError:
            return None
    if text.isdigit():
        try:
            timestamp = int(text)
            if len(text) >= 13:
                return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).astimezone(SHANGHAI_TZ)
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(SHANGHAI_TZ)
        except Exception:
            return None
    return None


def extract_url_date(url: str) -> datetime | None:
    for pattern in URL_DATE_PATTERNS:
        match = pattern.search(url)
        if not match:
            continue
        year, month, day = match.groups()
        try:
            return datetime(int(year), int(month), int(day), 0, 0, 0, tzinfo=SHANGHAI_TZ)
        except ValueError:
            return None
    return None


def is_recent_dated_url(url: str, max_age_days: int = MAX_DATED_URL_AGE_DAYS) -> bool:
    published_at = extract_url_date(url)
    if published_at is None:
        return True
    now = datetime.now(SHANGHAI_TZ)
    if published_at > now + timedelta(days=2):
        return False
    return now - published_at <= timedelta(days=max_age_days)


def element_published_at(element: Any, fallback_url: str) -> str:
    containers = [element]
    parent = getattr(element, 'parent', None)
    depth = 0
    while parent is not None and depth < 3:
        containers.append(parent)
        parent = getattr(parent, 'parent', None)
        depth += 1
    attrs = ['datetime', 'data-time', 'data-date', 'data-published', 'data-timestamp', 'timestamp']
    for container in containers:
        for attr in attrs:
            published_at = parse_published_value(getattr(container, 'get', lambda *_: '')(attr, ''))
            if published_at is not None:
                return published_at.isoformat()
        if hasattr(container, 'select'):
            for node in container.select('time[datetime], [data-time], [data-date], [data-published], [data-timestamp], [timestamp]'):
                for attr in attrs:
                    published_at = parse_published_value(node.get(attr, ''))
                    if published_at is not None:
                        return published_at.isoformat()
    published_at = extract_url_date(fallback_url)
    return published_at.isoformat() if published_at is not None else ''


def item_published_at(item: dict[str, str]) -> datetime | None:
    explicit = parse_published_value(item.get('published_at', ''))
    if explicit is not None:
        return explicit
    return extract_url_date(str(item.get('url', '')))


def has_precise_timestamp(item: dict[str, str]) -> bool:
    published_at = item_published_at(item)
    if published_at is None:
        return False
    return any([published_at.hour, published_at.minute, published_at.second])


def is_within_collection_window(item: dict[str, str], collected_at: datetime, max_age: timedelta) -> bool:
    published_at = item_published_at(item)
    if published_at is None:
        return False
    if published_at > collected_at + timedelta(hours=2):
        return False
    return collected_at - published_at <= max_age


def first_published_at_from_html(html: str, source_name: str, article_url: str) -> str:
    if not html or '<' not in html or '>' not in html:
        return ''
    soup = BeautifulSoup(html, 'html.parser')
    for selector in [
        'meta[property="article:published_time"]',
        'meta[property="article:modified_time"]',
        'meta[name="publishdate"]',
        'meta[name="PubDate"]',
        'meta[name="publish_date"]',
        'meta[name="date"]',
        'meta[itemprop="datePublished"]',
        'meta[itemprop="dateModified"]',
        'time[datetime]',
        '[data-time]',
        '[data-date]',
        '[data-published]',
        '[data-timestamp]',
        '[timestamp]',
    ]:
        for node in soup.select(selector):
            values = [
                node.get('content', ''),
                node.get('datetime', ''),
                node.get('data-time', ''),
                node.get('data-date', ''),
                node.get('data-published', ''),
                node.get('data-timestamp', ''),
                node.get('timestamp', ''),
                node.get_text(' ', strip=True),
            ]
            for value in values:
                published_at = parse_published_value(value)
                if published_at is not None and any([published_at.hour, published_at.minute, published_at.second]):
                    return published_at.isoformat()
    if source_name == '新华社':
        block = soup.select_one('.header-time')
        if block is not None:
            published_at = parse_published_value(block.get_text(' ', strip=True))
            if published_at is not None:
                return published_at.isoformat()
    if source_name == '人民网':
        block = soup.select_one('#newstime')
        if block is not None:
            published_at = parse_published_value(block.get_text(' ', strip=True))
            if published_at is not None:
                return published_at.isoformat()
    if source_name == '央视新闻':
        for selector in ['.info', '.info1']:
            block = soup.select_one(selector)
            if block is not None:
                published_at = parse_published_value(block.get_text(' ', strip=True))
                if published_at is not None:
                    return published_at.isoformat()
    if source_name == '联合早报':
        for pattern in [r'"datePublished"\s*:\s*"([^"]+)"', r'"dateModified"\s*:\s*"([^"]+)"']:
            match = re.search(pattern, html)
            if not match:
                continue
            published_at = parse_published_value(match.group(1))
            if published_at is not None:
                return published_at.isoformat()
    published_at = extract_url_date(article_url)
    return published_at.isoformat() if published_at is not None else ''


def is_image_credit_title(title: str) -> bool:
    normalized = title.strip().lower()
    if '/' not in normalized:
        return False
    if not any(marker in normalized for marker in IMAGE_CREDIT_MARKERS):
        return False
    alpha_tokens = re.findall(r'[a-z]{2,}', normalized)
    if not alpha_tokens:
        return False
    return not any(token in {'war', 'attack', 'attacks', 'killed', 'kills', 'fire', 'election', 'update', 'updates'} for token in alpha_tokens)


def should_keep_item(source_name: str, title: str, url: str, via: str) -> bool:
    lowered_title = title.strip().lower()
    if any(pattern.search(lowered_title) for pattern in GENERIC_SKIP_TITLE_PATTERNS):
        return False
    if any(pattern.search(title) for pattern in SOFT_NEWS_TITLE_PATTERNS):
        return False
    if any(pattern.search(url) for pattern in SOFT_NEWS_URL_PATTERNS):
        return False
    if source_name == '纽约时报' and any(pattern.search(title) for pattern in NYT_SKIP_TITLE_PATTERNS):
        return False
    if source_name == '纽约时报' and any(pattern.search(url) for pattern in NYT_SKIP_URL_PATTERNS):
        return False
    if source_name == 'CNN' and is_image_credit_title(title):
        return False
    if source_name == 'CNN' and via == 'rss' and not is_recent_dated_url(url):
        return False
    if source_name == '新华社' and any(pattern.search(url) for pattern in XINHUA_SKIP_URL_PATTERNS):
        return False
    if source_name in {'央视新闻', '新华社', '人民网'} and any(pattern.search(title) for pattern in DOMESTIC_NON_HARD_NEWS_TITLE_PATTERNS):
        return False
    if source_name == '路透社' and any(pattern.search(url) for pattern in REUTERS_SKIP_URL_PATTERNS):
        return False
    return True


def fetch_url(url: str) -> requests.Response:
    return requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT)


def filter_source_items(source: dict[str, Any], items: list[dict[str, str]], collected_at: datetime | None = None, max_age: timedelta = DEFAULT_COLLECTION_AGE) -> list[dict[str, str]]:
    source_name = str(source.get('name', ''))
    collected = collected_at or datetime.now(SHANGHAI_TZ)
    filtered: list[dict[str, str]] = []
    for item in items:
        title = str(item.get('title', ''))
        url = str(item.get('url', ''))
        via = str(item.get('via', ''))
        if not is_within_collection_window(item, collected, max_age):
            continue
        if source_name == 'CNN' and not is_cnn_article(title, url):
            continue
        if not should_keep_item(source_name, title, url, via):
            continue
        filtered.append(item)
    return filtered


def first_srcset_candidate(value: object) -> str:
    if not isinstance(value, str):
        return ''
    return value.split(' ')[0].strip()


def image_candidates_from_node(node: Any, base_url: str) -> list[str]:
    candidates: list[str] = []
    if node is None:
        return candidates
    if getattr(node, 'name', None) == 'meta':
        value = clean_text(node.get('content', ''))
        if value:
            candidates.append(urljoin(base_url, value))
        return candidates
    for raw in [
        node.get('src', ''),
        node.get('data-src', ''),
        node.get('data-original', ''),
        node.get('data-original-src', ''),
        node.get('poster', ''),
        first_srcset_candidate(node.get('srcset')),
        first_srcset_candidate(node.get('data-srcset')),
    ]:
        value = clean_text(raw)
        if value:
            candidates.append(urljoin(base_url, value))
    return candidates


def is_non_content_image(url: str) -> bool:
    cleaned = clean_text(url)
    if not cleaned:
        return True
    lowered = cleaned.lower()
    return any(pattern.search(lowered) for pattern in NON_CONTENT_IMAGE_PATTERNS)


def is_usable_content_image(url: str) -> bool:
    return is_usable_thumbnail_url(url) and not is_non_content_image(url)


def first_source_body_image_url(html: str, base_url: str, source_name: str) -> str:
    if not html or '<' not in html or '>' not in html:
        return ''
    soup = BeautifulSoup(html, 'html.parser')
    for selector in BODY_IMAGE_SELECTORS.get(source_name, []):
        for node in soup.select(selector):
            for candidate in image_candidates_from_node(node, base_url):
                if is_usable_content_image(candidate):
                    return candidate
    if source_name == '央视新闻':
        for candidate in re.findall(r'(?:https?:)?//p\d+\.img\.cctvpic\.com/photoworkspace/contentimg/[^\"\'\s>]+', html, re.I):
            absolute = urljoin(base_url, candidate)
            if is_usable_content_image(absolute):
                return absolute
    return ''


def first_image_url_from_html(html: str, base_url: str, source_name: str = '') -> str:
    if not html or '<' not in html or '>' not in html:
        return ''
    source_image = first_source_body_image_url(html, base_url, source_name)
    if source_image:
        return source_image
    if source_name in {'新华社', '人民网'}:
        return ''
    soup = BeautifulSoup(html, 'html.parser')
    fallback = ''
    for selector in ['meta[property="og:image"]', 'meta[name="twitter:image"]', 'img', 'source']:
        for node in soup.select(selector):
            for candidate in image_candidates_from_node(node, base_url):
                if is_usable_content_image(candidate):
                    return candidate
                if not fallback and is_usable_thumbnail_url(candidate):
                    fallback = candidate
    return fallback


def first_text_snippet_from_html(html: str, title: str = '', source_name: str = '') -> str:
    if not html or '<' not in html or '>' not in html:
        return ''
    soup = BeautifulSoup(html, 'html.parser')
    title_text = clean_text(title)
    paragraph_texts: list[str] = []
    seen_paragraphs: set[str] = set()
    for selector in ordered_body_selectors(source_name):
        for node in soup.select(selector):
            text = clean_text(node.get_text(' ', strip=True))
            if not text or text in seen_paragraphs:
                continue
            seen_paragraphs.add(text)
            paragraph_texts.append(text)
    paragraph_snippet = build_snippet_from_candidate_texts(paragraph_texts, title_text)
    if snippet_sentence_count(paragraph_snippet) >= MIN_FETCHED_SNIPPET_SENTENCES and text_content_length(paragraph_snippet) >= MIN_FETCHED_SNIPPET_LENGTH:
        return paragraph_snippet
    candidates: list[str] = []
    for selector in ['meta[name="description"]', 'meta[property="og:description"]', 'meta[name="twitter:description"]']:
        for node in soup.select(selector):
            text = clean_text(node.get('content', ''))
            if text:
                candidates.append(text)
    fallback_snippet = build_snippet_from_candidate_texts(candidates + paragraph_texts, title_text)
    if fallback_snippet:
        return fallback_snippet
    return paragraph_snippet


def element_image_url(element: Any, base_url: str) -> str:
    containers = [element]
    parent = getattr(element, 'parent', None)
    depth = 0
    while parent is not None and depth < 3:
        containers.append(parent)
        parent = getattr(parent, 'parent', None)
        depth += 1
    fallback = ''
    for container in containers:
        if getattr(container, 'name', None) in {'img', 'source', 'video'}:
            for candidate in image_candidates_from_node(container, base_url):
                if is_usable_thumbnail_url(candidate):
                    return candidate
                if not fallback:
                    fallback = candidate
        if hasattr(container, 'select_one'):
            for image in container.select('img, source, video, meta[property="og:image"], meta[name="twitter:image"]'):
                for candidate in image_candidates_from_node(image, base_url):
                    if is_usable_thumbnail_url(candidate):
                        return candidate
                    if not fallback:
                        fallback = candidate
    return fallback


def rss_thumbnail_url(node: ET.Element, link: str, description: str) -> str:
    namespace_tags = {'content', 'thumbnail'}
    for child in list(node):
        tag_name = child.tag.rsplit('}', 1)[-1].lower()
        if tag_name in namespace_tags:
            candidate = clean_text(child.attrib.get('url', '') or child.attrib.get('href', ''))
            if candidate:
                return urljoin(link, candidate)
        if tag_name == 'enclosure' and str(child.attrib.get('type', '')).startswith('image/'):
            candidate = clean_text(child.attrib.get('url', ''))
            if candidate:
                return urljoin(link, candidate)
    content_node = next((child for child in list(node) if child.tag.rsplit('}', 1)[-1].lower() == 'encoded'), None)
    content_html = content_node.text if content_node is not None and content_node.text else ''
    return first_image_url_from_html(content_html or description, link)


def parse_rss(url: str) -> list[dict[str, str]]:
    if not url:
        return []
    response = fetch_url(url)
    response.raise_for_status()
    content_type = str(response.headers.get('content-type', '')).lower()
    if url.lower().endswith('.json') or 'application/json' in content_type:
        payload = json.loads(decode_response_text(response) or '{}')
        items: list[dict[str, str]] = []
        for entry in payload.get('items', []):
            if not isinstance(entry, dict):
                continue
            translations = entry.get('_translations', {})
            simplified = translations.get('zh-Hans', {}) if isinstance(translations, dict) else {}
            title = clean_text(simplified.get('title', '') if isinstance(simplified, dict) else '') or clean_text(entry.get('title', ''))
            link = clean_text(entry.get('url', ''))
            description = feed_snippet_from_values(
                title,
                entry.get('summary', ''),
                entry.get('content_text', ''),
            ) or clean_text(entry.get('summary', ''))
            thumbnail_url = clean_text(entry.get('image', ''))
            published_at = clean_text(entry.get('_original_published', '') or entry.get('date_published', ''))
            if title and link:
                items.append({'title': title, 'url': link, 'snippet': description, 'via': 'rss', 'thumbnail_url': thumbnail_url, 'published_at': published_at})
        return items
    root = ET.fromstring(decode_response_text(response))
    items: list[dict[str, str]] = []
    for node in root.findall('.//item'):
        title = clean_text(node.findtext('title', default=''))
        link = clean_text(node.findtext('link', default=''))
        raw_description = node.findtext('description', default='') or ''
        content_node = next((child for child in list(node) if child.tag.rsplit('}', 1)[-1].lower() == 'encoded'), None)
        encoded_html = content_node.text if content_node is not None and content_node.text else ''
        description = feed_snippet_from_values(title, raw_description, encoded_html) or clean_text(raw_description)
        thumbnail_url = rss_thumbnail_url(node, link, raw_description)
        published_at = ''
        for child in list(node):
            tag_name = child.tag.rsplit('}', 1)[-1].lower()
            if tag_name in {'pubdate', 'published', 'updated', 'date'}:
                parsed = parse_published_value(child.text or '')
                if parsed is not None:
                    published_at = parsed.isoformat()
                    break
        if title and link:
            items.append({'title': title, 'url': link, 'snippet': description, 'via': 'rss', 'thumbnail_url': thumbnail_url, 'published_at': published_at})
    return items


def parse_html_document(source: dict[str, Any], source_url: str) -> list[dict[str, str]]:
    response = fetch_url(source_url)
    response.raise_for_status()
    soup = BeautifulSoup(decode_response_text(response), 'html.parser')
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    include_patterns = [re.compile(str(pattern), re.I) for pattern in source['include_patterns']]
    exclude_patterns = [re.compile(str(pattern), re.I) for pattern in source['exclude_patterns']]
    selectors = [str(selector) for selector in source['selectors']] or ['a']
    for selector in selectors:
        for element in soup.select(selector):
            href = clean_text(element.get('href', ''))
            if href and not href.startswith('http'):
                href = urljoin(source_url, href)
            parts = [
                clean_text(element.get('aria-label', '')),
                clean_text(element.get('title', '')),
                clean_text(element.get_text(' ', strip=True)),
            ]
            title = clean_text(' '.join(part for part in parts if part))
            if not href or not title or len(title) < 8:
                continue
            if str(source.get('name', '')) == '央视新闻' and not CCTV_ARTICLE_RE.search(href):
                continue
            if str(source.get('name', '')) == '路透社' and not is_reuters_html_article(title, href):
                continue
            if str(source.get('name', '')) == 'CNN' and not is_cnn_article(title, href):
                continue
            if include_patterns and not any(pattern.search(href) for pattern in include_patterns):
                continue
            if str(source.get('name', '')) == '央视新闻' and any(marker in title for marker in ['热解读', '央视快评']):
                continue
            if any(pattern.search(title) for pattern in exclude_patterns):
                continue
            key = (href, title)
            if key in seen:
                continue
            seen.add(key)
            items.append({'title': title, 'url': href, 'snippet': '', 'via': 'html', 'thumbnail_url': element_image_url(element, source_url), 'published_at': element_published_at(element, href)})
    return items


def parse_html(source: dict[str, Any]) -> list[dict[str, str]]:
    source_urls = [str(source['url'])]
    if str(source.get('name', '')) == '央视新闻':
        source_urls.extend(CCTV_EXTRA_URLS)
    merged_items: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for source_url in source_urls:
        for item in parse_html_document(source, source_url):
            if item['url'] in seen_urls:
                continue
            seen_urls.add(item['url'])
            merged_items.append(item)
    return merged_items


def enrich_missing_thumbnails(items: list[dict[str, str]], limit: int = 36, source_name: str = '') -> list[dict[str, str]]:
    enriched: list[dict[str, str]] = []
    fetched = 0
    seen_urls: set[str] = set()
    for item in items:
        row = dict(item)
        thumbnail_url = str(row.get('thumbnail_url', '')).strip()
        snippet = clean_text(row.get('snippet', ''))
        title = clean_text(row.get('title', ''))
        article_url = str(row.get('url', '')).strip()
        via = str(row.get('via', '')).strip()
        published_at = str(row.get('published_at', '')).strip()
        suspicious_thumbnail = bool(thumbnail_url) and is_non_content_image(thumbnail_url)
        source_thumbnail_refresh = source_name in {'新华社', '人民网', '央视新闻'} and (not thumbnail_url or suspicious_thumbnail)
        needs_thumbnail = source_thumbnail_refresh or not is_usable_thumbnail_url(thumbnail_url)
        force_snippet_refresh = source_name == 'RT' and via == 'rss'
        needs_snippet = force_snippet_refresh or len(snippet) < MIN_FETCHED_SNIPPET_LENGTH or snippet_sentence_count(snippet) < MIN_FETCHED_SNIPPET_SENTENCES
        needs_precise_time = bool(article_url) and (not published_at or not has_precise_timestamp(row))
        if (not needs_thumbnail and not needs_snippet and not needs_precise_time) or not article_url or article_url in seen_urls or fetched >= limit:
            enriched.append(row)
            continue
        seen_urls.add(article_url)
        fetched += 1
        try:
            response = fetch_url(article_url)
            response.raise_for_status()
            html = decode_response_text(response)
            if needs_snippet:
                page_snippet = first_text_snippet_from_html(html, str(row.get('title', '')), source_name)
                if page_snippet and (force_snippet_refresh or len(page_snippet) > len(snippet)):
                    row['snippet'] = page_snippet
            if needs_precise_time:
                page_published_at = first_published_at_from_html(html, source_name, article_url)
                if page_published_at:
                    row['published_at'] = page_published_at
            thumbnail_url = first_image_url_from_html(html, article_url, source_name)
            if is_usable_content_image(thumbnail_url):
                row['thumbnail_url'] = thumbnail_url
            elif source_thumbnail_refresh:
                row['thumbnail_url'] = ''
        except Exception:
            pass
        current_snippet = clean_text(row.get('snippet', ''))
        if current_snippet and snippet_sentence_count(current_snippet) < MIN_FETCHED_SNIPPET_SENTENCES:
            row['snippet'] = clause_split_snippet(current_snippet)
        enriched.append(row)
    return enriched


def merge_items(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for group in groups:
        for item in group:
            url = item['url']
            if url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(item)
    return merged


def write_source_artifact(run_id: int, source_slug: str, payload: dict[str, Any]) -> str:
    run_dir = Paths.artifacts_dir / f'run-{run_id}'
    run_dir.mkdir(parents=True, exist_ok=True)
    relpath = f'artifacts/run-{run_id}/{source_slug}.json'
    file_path = Paths.data_dir / relpath
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return relpath


def slugify(name: str) -> str:
    cleaned = re.sub(r'[^\w\u4e00-\u9fff-]+', '_', name, flags=re.UNICODE)
    return cleaned.strip('_') or 'source'


def fetch_source(source: dict[str, Any], run_id: int) -> dict[str, Any]:
    raw_rss_items: list[dict[str, str]] = []
    raw_html_items: list[dict[str, str]] = []
    rss_items: list[dict[str, str]] = []
    html_items: list[dict[str, str]] = []
    errors: list[str] = []
    collected_at = datetime.now(SHANGHAI_TZ)
    if source.get('rss_url'):
        try:
            raw_rss_items = parse_rss(str(source['rss_url']))
            rss_items = filter_source_items(source, raw_rss_items, collected_at)
        except Exception as exc:
            errors.append(f'rss: {exc}')
    try:
        raw_html_items = parse_html(source)
        html_items = filter_source_items(source, raw_html_items, collected_at)
    except Exception as exc:
        errors.append(f'html: {exc}')
    if len(merge_items(rss_items, html_items)) < 3:
        rss_items = filter_source_items(source, raw_rss_items, collected_at, FALLBACK_COLLECTION_AGE) if raw_rss_items else []
        html_items = filter_source_items(source, raw_html_items, collected_at, FALLBACK_COLLECTION_AGE) if raw_html_items else []
    merged = merge_items(rss_items, html_items)
    thumbnail_limit = 80 if str(source.get('name', '')) in {'新华社', '人民网', '央视新闻'} else 36
    merged = merged[:30]
    merged = enrich_missing_thumbnails(merged, limit=min(thumbnail_limit, len(merged)), source_name=str(source.get('name', '')))
    merged = normalize_items(merged)
    for item in merged:
        snippet = clean_text(item.get('snippet', ''))
        if snippet and snippet_sentence_count(snippet) < MIN_FETCHED_SNIPPET_SENTENCES:
            item['snippet'] = clause_split_snippet(snippet)
    artifact = {
        'source': source['name'],
        'url': source['url'],
        'rss_url': source.get('rss_url', ''),
        'rss_count': len(rss_items),
        'html_count': len(html_items),
        'items': merged,
        'errors': errors,
    }
    relpath = write_source_artifact(run_id, slugify(str(source['name'])), artifact)
    return {
        'items': merged,
        'artifact_relpath': relpath,
        'errors': errors,
    }
