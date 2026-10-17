"""把配置、存储、模型客户端和流水线组装起来；Web、CLI、定时任务共用同一个 Runtime。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from .analyze import analyze_articles, make_continuity_judge, make_judge
from .cluster import ClusterConfig
from .config import Settings, get_settings
from .digest import render_text
from .embeddings import EmbeddingClient, HashingEmbedder
from . import fetch as _fetch
from . import tz
from .fetch import fetch_source
from .filtering import FilterConfig
from .images import ThumbnailCache
from .llm import LLMClient, LLMSettings
from .pipeline import Pipeline, PipelineDeps
from .push import send
from .sources import DEFAULT_SOURCES
from .store import Store
from .summarize import summarize_event

log = logging.getLogger(__name__)


def _module_fetch():
    # 运行时查找，便于测试替换 runtime.fetch_source
    return globals()['fetch_source']


@dataclass
class Runtime:
    settings: Settings
    store: Store
    llm: LLMClient
    embedder: EmbeddingClient | HashingEmbedder
    thumbnails: ThumbnailCache
    pipeline: Pipeline = field(init=False)
    transport: object | None = None  # 测试注入
    operation_lock: object = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        s = self.settings
        deps = PipelineDeps(
            fetch=self._fetch,
            analyze=lambda articles: analyze_articles(self.llm, articles, self.settings.min_topic_confidence, self.settings.llm_concurrency),
            embed=lambda texts: self.embedder(texts),
            judge_factory=lambda analyses: make_judge(self.llm, analyses),
            summarize=lambda articles, title, analysis: summarize_event(self.llm, articles, title, analysis),
            llm_calls=lambda: self.llm.calls,
            continuity_judge=lambda new, old: make_continuity_judge(self.llm)(new, old),
            thumbnail=self.thumbnails.fetch,
            filter_config=FilterConfig(min_impact=s.min_impact, drop_opinion=s.drop_opinion),
            cluster_config=self.cluster_config(),
            max_events=self.max_events,
        )
        self.pipeline = Pipeline(self.store, deps)

    def max_events(self) -> int:
        return self.settings.max_events

    def apply_settings(self, settings: Settings) -> None:
        """后台修改模型/向量/阈值后热更新，不需要重启。运行中的任务继续用旧客户端跑完。"""
        self.settings = settings
        calls = self.llm.calls
        self.llm = make_llm(settings)
        self.llm.calls = calls  # 保留调用记录，统计不中断
        self.embedder = make_embedder(settings)
        self.pipeline.deps.filter_config = FilterConfig(min_impact=settings.min_impact, drop_opinion=settings.drop_opinion)
        self.pipeline.deps.cluster_config = self.cluster_config()

    def test_llm(self) -> tuple[bool, str]:
        import time

        import httpx

        s = self.settings
        started = time.monotonic()
        try:
            with httpx.Client(base_url=s.llm_base_url.rstrip('/'), headers={'Authorization': f'Bearer {s.llm_api_key}'}, timeout=30,
                              transport=self.transport) as client:
                response = client.post('/chat/completions', json={'model': s.llm_model, 'max_tokens': 5,
                                                                  'messages': [{'role': 'user', 'content': '回复 ok'}]})
            if response.status_code >= 400:
                return False, f'HTTP {response.status_code}：{response.text[:160]}'
            response.json()['choices'][0]
        except Exception as exc:
            return False, f'{type(exc).__name__}: {str(exc)[:160]}'
        return True, f'{s.llm_model} 响应正常，用时 {time.monotonic() - started:.1f} 秒'

    def test_embedding(self) -> tuple[bool, str]:
        import time

        import httpx

        s = self.settings
        if not s.embedding_api_key:
            return False, '未填写向量服务 API Key，当前使用字符哈希降级'
        started = time.monotonic()
        try:
            with httpx.Client(base_url=s.embedding_base_url.rstrip('/'), headers={'Authorization': f'Bearer {s.embedding_api_key}'},
                              timeout=30, transport=self.transport) as client:
                response = client.post('/embeddings', json={'model': s.embedding_model, 'input': ['连接测试']})
            if response.status_code >= 400:
                return False, f'HTTP {response.status_code}：{response.text[:160]}'
            dims = len(response.json()['data'][0]['embedding'])
        except Exception as exc:
            return False, f'{type(exc).__name__}: {str(exc)[:160]}'
        return True, f'{s.embedding_model} 返回 {dims} 维向量，用时 {time.monotonic() - started:.1f} 秒'

    def _fetch(self, source):
        window = self.store.get_setting('fetch_window', self.settings.fetch_window)
        return _module_fetch()(source, since=tz.fetch_since(window))

    def fetch_preview(self, source):
        """后台「测试抓取」：按当前时间窗抓一次列表，不调用模型、不入库。"""
        return self._fetch(source)

    def cleanup(self) -> dict:
        days = int(self.store.get_setting('retention_days', str(self.settings.retention_days)))
        return {'runs': self.store.cleanup(days), 'thumbnails': self.thumbnails.prune(days)}

    def cluster_config(self) -> ClusterConfig:
        s = self.settings
        high, low = (0.75, 0.40) if self.embedding_degraded else (0.86, 0.72)
        return ClusterConfig(
            high=s.cluster_high if s.cluster_high is not None else high,
            low=s.cluster_low if s.cluster_low is not None else low,
            window_hours=s.cluster_window_hours,
        )

    @property
    def embedding_degraded(self) -> bool:
        return isinstance(self.embedder, HashingEmbedder)

    def publication_url(self, run_id: int) -> str:
        base = self.settings.public_base_url.rstrip('/')
        return f'{base}/briefs/{run_id}' if base else ''

    def collect(self, trigger: str = 'manual') -> int:
        """抓取/分析/入库/生成简报，不推送。"""
        with self.operation_lock:
            run_id = self.pipeline.run(trigger)
            self.cleanup()
            return run_id

    def deliver_digest(self) -> list[dict]:
        """汇总自该渠道上次成功推送以来的所有批次，不只取最后一期。初次覆盖近24小时，最多50条。"""
        from datetime import timedelta

        with self.operation_lock:
            until = tz.iso()
            latest = self.store.latest_completed_run()
            if latest is None:
                return []
            results = []
            for channel in self.store.list_channels(enabled_only=True):
                key = f"push_cursor_{channel['id']}"
                since = self.store.get_setting(key, tz.iso(tz.now() - timedelta(hours=24)))
                rows = self.store._rows(
                    "SELECT e.run_id,e.payload_json,e.canonical_id FROM events e JOIN runs r ON r.id=e.run_id "
                    "WHERE r.status='completed' AND r.finished_at>? AND r.finished_at<=? ORDER BY e.score DESC,e.run_id DESC",
                    (since, until),
                )
                # latest version of each event; keep source run for delivery logs
                chosen = {}
                from .models import Event
                for row in rows:
                    previous = chosen.get(row['canonical_id'])
                    if previous is None or row['run_id'] > previous['run_id']:
                        chosen[row['canonical_id']] = row
                events = [Event.model_validate_json(r['payload_json']) for r in chosen.values()]
                events.sort(key=lambda e: e.score, reverse=True)
                events = events[:self.max_events()]
                if not events:
                    continue
                text = render_text(events, title='新闻简报 · ' + tz.now().strftime('%m-%d %H:%M'))
                try:
                    send(channel['kind'], channel['config'], text, subject='新闻简报 · ' + tz.now().strftime('%m-%d %H:%M'))
                    self.store.record_delivery(latest['id'], channel['id'], 'sent')
                    self.store.set_setting(key, until)
                    results.append({'channel': channel['name'], 'status': 'sent'})
                except Exception as exc:
                    self.store.record_delivery(latest['id'], channel['id'], 'failed', str(exc)[:300])
                    results.append({'channel': channel['name'], 'status': 'failed'})
            return results

    def run_and_deliver(self, trigger: str = 'manual') -> int:
        run_id = self.pipeline.run(trigger)
        try:
            self.cleanup()
        except Exception:  # 清理失败不影响本次结果
            log.exception('cleanup failed')
        self.deliver(run_id)
        return run_id

    def deliver(self, run_id: int, channel_ids: list[int] | None = None) -> list[dict]:
        events = self.store.load_events(run_id)
        results = []
        if not events:
            return results
        text = render_text(events, web_url=self.publication_url(run_id), title=f'新闻简报 #{run_id}')
        for channel in self.store.list_channels(enabled_only=channel_ids is None):
            if channel_ids is not None and channel['id'] not in channel_ids:
                continue
            try:
                send(channel['kind'], channel['config'], text, subject=f'新闻简报 #{run_id}')
                self.store.record_delivery(run_id, channel['id'], 'sent')
                results.append({'channel': channel['name'], 'status': 'sent'})
            except Exception as exc:
                log.warning('delivery to %s failed: %s', channel['name'], exc)
                self.store.record_delivery(run_id, channel['id'], 'failed', str(exc)[:300])
                results.append({'channel': channel['name'], 'status': 'failed', 'error': str(exc)})
        return results


def make_llm(settings: Settings) -> LLMClient:
    return LLMClient(
        LLMSettings(base_url=settings.llm_base_url, api_key=settings.llm_api_key, model=settings.llm_model),
        cache_dir=settings.cache_dir / 'llm',
    )


def make_embedder(settings: Settings) -> EmbeddingClient | HashingEmbedder:
    if settings.embedding_api_key:
        return EmbeddingClient(
            settings.embedding_base_url, settings.embedding_api_key, settings.embedding_model, cache_dir=settings.cache_dir / 'embeddings'
        )
    log.warning('未配置 NEWS_EMBEDDING_API_KEY，使用字符哈希向量降级，跨语言聚类效果会变差')
    return HashingEmbedder()


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    store = Store(settings.db_path)
    store.seed_sources(DEFAULT_SOURCES)
    return Runtime(
        settings=settings, store=store, llm=make_llm(settings), embedder=make_embedder(settings),
        thumbnails=ThumbnailCache(settings.data_dir / 'thumbs'),
    )
