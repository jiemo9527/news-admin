"""按需文章分析：只调用 gpt-5.6-luna（Responses API + web_search）检索相关信息与最新进展。

- 只在用户点击时调用，不进入抓取流水线，不影响采集/推送；
- 结果按（事件版本, Prompt 版本）缓存 6 小时，多个账号共享；
- 每个账号每小时限次（缓存命中不计），全局并发受限；
- 输出只作纯文本展示，来源链接仅保留 http(s)。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from . import tz
from .models import Event
from .prompts import INSIGHT_SYSTEM, INSIGHT_VERSION, insight_user

ANALYSIS_MODEL = 'gpt-5.6-luna'
CACHE_SECONDS = 6 * 3600
USER_LIMIT_PER_HOUR = 20
MAX_CONCURRENT = 3
TIMEOUT_SECONDS = 180

_CITATION_RE = re.compile(r'\s*[（(]\s*\[([^\]\n]{1,80})\]\((?:https?://)?[^)\s]+\)\s*[)）]')
_LINK_RE = re.compile(r'\[([^\]\n]{1,200})\]\((?:https?://)?[^)\s]+\)')
_EMPHASIS_RE = re.compile(r'\*\*|__|`')
_HEADING_RE = re.compile(r'^\s{0,3}#{1,6}\s*', re.M)


class InsightError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def clean_text(text: str) -> str:
    """模型的 Markdown 转纯文本：内联引用改为（来源名），去掉链接语法、加粗和标题符号。"""
    text = _CITATION_RE.sub(lambda m: f'（{m.group(1).strip()}）', text)
    text = _LINK_RE.sub(lambda m: m.group(1), text)
    text = _EMPHASIS_RE.sub('', text)
    text = _HEADING_RE.sub('', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _safe_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ''
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        return ''
    return url.strip()[:500]


def parse_response(data: dict) -> dict:
    output = data.get('output') or []
    text, sources, seen = [], [], set()
    searched = any(item.get('type') == 'web_search_call' for item in output)
    for item in output:
        if item.get('type') != 'message':
            continue
        for part in item.get('content') or []:
            if part.get('type') not in ('output_text', 'text'):
                continue
            text.append(str(part.get('text') or ''))
            for note in part.get('annotations') or []:
                url = _safe_url(str(note.get('url') or ''))
                if url and url not in seen:
                    seen.add(url)
                    title = ' '.join(str(note.get('title') or '').split())[:120] or urlsplit(url).hostname
                    sources.append({'title': title, 'url': url})
    body = clean_text(''.join(text))
    if not body:
        raise InsightError(502, '模型没有返回分析内容')
    return {'text': body, 'sources': sources[:12], 'searched': searched}


@dataclass
class InsightService:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _history: dict = field(default_factory=lambda: defaultdict(deque))
    _keys: dict = field(default_factory=dict)
    _slots: threading.BoundedSemaphore = field(default_factory=lambda: threading.BoundedSemaphore(MAX_CONCURRENT))

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._keys.setdefault(key, threading.Lock())

    def _take_quota(self, user_id: int) -> None:
        limit = USER_LIMIT_PER_HOUR
        now = time.monotonic()
        with self._lock:
            used = self._history[user_id]
            while used and now - used[0] > 3600:
                used.popleft()
            if len(used) >= limit:
                raise InsightError(429, f'分析次数已达每小时 {limit} 次上限，请稍后再试')
            used.append(now)

    def analyze(self, rt, user_id: int, run_id: int, canonical_id: str, event: Event) -> dict:
        key = hashlib.sha256(json.dumps([ANALYSIS_MODEL, INSIGHT_VERSION, canonical_id, run_id], ensure_ascii=False).encode()).hexdigest()[:32]
        folder = rt.settings.cache_dir / 'insight'
        path = folder / f'{key}.json'
        # 同一事件并发点击只调用一次，后到的请求等待并读取缓存
        with self._key_lock(key):
            cached = self._read(path)
            if cached:
                return {**cached, 'cached': True}
            self._take_quota(user_id)
            if not self._slots.acquire(timeout=5):
                raise InsightError(429, '当前分析请求较多，请稍后再试')
            try:
                result = self._call(rt, event)
            finally:
                self._slots.release()
            result.update(model=ANALYSIS_MODEL, created_at=tz.iso())
            folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            tmp.replace(path)
            return {**result, 'cached': False}

    @staticmethod
    def _read(path) -> dict | None:
        try:
            if time.time() - path.stat().st_mtime > CACHE_SECONDS:
                return None
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) and data.get('text') else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _call(rt, event: Event) -> dict:
        s = rt.settings
        if not s.llm_api_key:
            raise InsightError(503, '未配置大模型 API Key')
        body = {
            'model': ANALYSIS_MODEL,
            'instructions': INSIGHT_SYSTEM,
            'input': insight_user(event, tz.now()),
            'tools': [{'type': 'web_search'}],
        }
        try:
            with httpx.Client(base_url=s.llm_base_url.rstrip('/'), headers={'Authorization': f'Bearer {s.llm_api_key}'},
                              timeout=TIMEOUT_SECONDS, transport=rt.transport) as client:
                response = client.post('/responses', json=body)
        except httpx.TimeoutException as exc:
            raise InsightError(504, '模型响应超时，请稍后重试') from exc
        except httpx.HTTPError as exc:
            raise InsightError(502, f'无法连接模型服务：{type(exc).__name__}') from exc
        if response.status_code >= 400:
            raise InsightError(502, f'模型服务返回 HTTP {response.status_code}')
        try:
            data = response.json()
        except ValueError as exc:
            raise InsightError(502, '模型服务返回了无效数据') from exc
        if not isinstance(data, dict):
            raise InsightError(502, '模型服务返回了无效数据')
        return parse_response(data)
