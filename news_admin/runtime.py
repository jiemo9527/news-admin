"""把配置、存储、模型客户端和流水线组装起来；Web、CLI、定时任务共用同一个 Runtime。"""

from __future__ import annotations

import logging
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

    def __post_init__(self) -> None:
        s = self.settings
        deps = PipelineDeps(
            fetch=self._fetch,
            analyze=lambda articles: analyze_articles(self.llm, articles, s.min_topic_confidence, s.llm_concurrency),
            embed=self.embedder,
            judge_factory=lambda analyses: make_judge(self.llm, analyses),
            summarize=lambda articles, title, analysis: summarize_event(self.llm, articles, title, analysis),
            llm_calls=lambda: self.llm.calls,
            continuity_judge=make_continuity_judge(self.llm),
            thumbnail=self.thumbnails.fetch,
            filter_config=FilterConfig(min_impact=s.min_impact, drop_opinion=s.drop_opinion),
            cluster_config=self.cluster_config(),
            max_events=s.max_events,
        )
        self.pipeline = Pipeline(self.store, deps)

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


def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or get_settings()
    store = Store(settings.db_path)
    store.seed_sources(DEFAULT_SOURCES)
    llm = LLMClient(
        LLMSettings(base_url=settings.llm_base_url, api_key=settings.llm_api_key, model=settings.llm_model),
        cache_dir=settings.cache_dir / 'llm',
    )
    if settings.embedding_api_key:
        embedder: EmbeddingClient | HashingEmbedder = EmbeddingClient(
            settings.embedding_base_url, settings.embedding_api_key, settings.embedding_model, cache_dir=settings.cache_dir / 'embeddings'
        )
    else:
        log.warning('未配置 NEWS_EMBEDDING_API_KEY，使用字符哈希向量降级，跨语言聚类效果会变差')
        embedder = HashingEmbedder()
    return Runtime(settings=settings, store=store, llm=llm, embedder=embedder, thumbnails=ThumbnailCache(settings.data_dir / 'thumbs'))
