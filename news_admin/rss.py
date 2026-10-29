"""RSS 2.0 输出：与首页同一份数据（同一事件只取最新版本），按时间取最新 RSS_LIMIT 条。

订阅地址带随机令牌（settings.rss_token），免登录访问；管理员可重置令牌让旧地址立即失效，或关闭订阅。
"""

from __future__ import annotations

import re
import secrets
import xml.etree.ElementTree as ET
from email.utils import format_datetime

from . import tz
from .digest import event_region_label
from .models import TOPIC_LABELS
from .rank import GRADE_LABELS
from .store import FeedItem

RSS_LIMIT = 150
TOKEN_SETTING = 'rss_token'
TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9_-]{32}$')
# XML 1.0 不允许的控制字符（抓取正文偶尔会带进来）
_INVALID_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]')


def new_token() -> str:
    return secrets.token_urlsafe(24)  # 32 个 URL 安全字符


def rss_path(token: str) -> str:
    return f'/rss/{token}/rss.xml'


def _sub(parent: ET.Element, tag: str, text: str = '', **attrs: str) -> ET.Element:
    node = ET.SubElement(parent, tag, attrs)
    node.text = _INVALID_XML.sub('', text or '')
    return node


def _http(url: str) -> bool:
    return url.startswith(('http://', 'https://'))


def _description(item: FeedItem) -> str:
    event = item.event
    lines: list[str] = []
    if event.summary:
        lines.append(''.join(s.text for s in event.summary.sentences))
        lines.extend(f'说法不一：{c}' for c in event.summary.conflicts)
    elif event.lead_analysis:
        lines.extend(event.lead_analysis.key_facts)
    context = [r for r in event.previous if r.relation == 'follow_up']
    if context:
        lines.append(f'前情：{context[0].headline}')
    meta = [f'评级 {event.rating.grade}（{GRADE_LABELS.get(event.rating.grade, "")}）'] if event.rating else []
    lines.append(' · '.join([*meta, f'权重 {event.score:.2f}']))
    lines.append('来源：' + '、'.join(event.sources))
    lines.extend(f'相关报道：{u}' for u in dict.fromkeys(a.url for a in event.articles[1:] if _http(a.url)))
    return '\n'.join(lines)


def render_rss(items: list[FeedItem], site_url: str, title: str = '新闻汇总', now=None) -> bytes:
    """items 已按时间倒序。<link> 指向原文（订阅者通常没有站内登录）。"""
    now = tz.to_local(now) if now else tz.now()
    rss = ET.Element('rss', version='2.0')
    channel = ET.SubElement(rss, 'channel')
    _sub(channel, 'title', title)
    _sub(channel, 'link', site_url)
    _sub(channel, 'description', f'最新 {RSS_LIMIT} 条新闻事件，同一事件只保留最新版本')
    _sub(channel, 'language', 'zh-CN')
    _sub(channel, 'lastBuildDate', format_datetime(now))
    _sub(channel, 'ttl', '30')
    for item in items:
        event = item.event
        analysis = event.lead_analysis
        headline = event.summary.headline if event.summary else (analysis.title_zh if analysis else event.lead.title)
        node = ET.SubElement(channel, 'item')
        _sub(node, 'title', ('【更新】' if event.is_update else '') + ' '.join(headline.split()))
        if _http(event.lead.url):
            _sub(node, 'link', event.lead.url)
        _sub(node, 'guid', item.canonical_id or event.id, isPermaLink='false')
        _sub(node, 'description', _description(item))
        published = tz.parse(item.event_time)
        if published:
            _sub(node, 'pubDate', format_datetime(published))
        if analysis:
            _sub(node, 'category', TOPIC_LABELS[analysis.topic])
            region = event_region_label(event)
            if region:
                _sub(node, 'category', region)
    return ET.tostring(rss, encoding='utf-8', xml_declaration=True)
