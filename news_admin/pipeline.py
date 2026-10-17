"""流水线编排：fetch → dedupe → analyze → cluster → 跨批次比对 → summarize → rank → 持久化。

所有外部依赖（抓取、模型、向量）通过 PipelineDeps 注入，测试中可替换为假实现。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from .cluster import ClusterConfig, Embed, Judge, cluster_articles, cosine, embedding_text
from .dedupe import dedupe_articles
from .fetch import FetchResult, Source
from .llm import LLMCall
from .models import Article, ArticleAnalysis, Event, EventSummary
from .rank import score_event
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class PipelineDeps:
    fetch: Callable[[Source], FetchResult]
    analyze: Callable[[list[Article]], dict[str, ArticleAnalysis]]
    embed: Embed
    judge_factory: Callable[[dict[str, ArticleAnalysis]], Judge]
    summarize: Callable[[list[Article], str, ArticleAnalysis | None], tuple[EventSummary, bool]]
    llm_calls: Callable[[], list[LLMCall]]
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    cluster_config: ClusterConfig | None = None
    max_events: int = 30


class PipelineBusy(RuntimeError):
    pass


def _order_articles(articles: list[Article]) -> list[Article]:
    """事件内主报道：来源权重高、正文信息量大者优先。"""
    return sorted(articles, key=lambda a: (-a.source_weight, -len(a.body)))


def analysis_embedding_text(article: Article, analysis: ArticleAnalysis | None) -> str:
    """聚类用文本：优先用模型归一化后的中文标题 + 事件指纹 + 关键事实。

    原始标题/正文中英混杂，同一事件的中英文报道在向量空间里相距很远；
    统一成中文结构化表述后，即使用较弱的向量模型也能把它们聚到一起。
    """
    if analysis is None:
        return embedding_text(article)
    return '\n'.join([analysis.title_zh, analysis.event_fingerprint, *analysis.key_facts[:2]])


def _event_id(articles: list[Article]) -> str:
    return hashlib.sha1('|'.join(sorted(a.url for a in articles)).encode()).hexdigest()[:12]


class Pipeline:
    def __init__(self, store: Store, deps: PipelineDeps):
        self.store = store
        self.deps = deps
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def run(self, trigger: str = 'manual') -> int:
        if not self._lock.acquire(blocking=False):
            raise PipelineBusy('已有任务在运行')
        try:
            return self._run(trigger)
        finally:
            self._lock.release()

    def _run(self, trigger: str) -> int:
        run_id = self.store.start_run(trigger)
        stats: dict = {}
        calls_before = len(self.deps.llm_calls())
        try:
            articles = self._fetch_all(run_id)
            stats['articles_fetched'] = len(articles)

            articles = dedupe_articles(articles)
            stats['articles_after_dedupe'] = len(articles)

            analyses = self.deps.analyze(articles)
            stats['articles_analyzed'] = len(analyses)

            config = self.deps.cluster_config or ClusterConfig()
            groups = cluster_articles(
                articles, self.deps.embed, self.deps.judge_factory(analyses), config,
                text_of=lambda a: analysis_embedding_text(a, analyses.get(a.id)),
            )
            stats['clusters'] = len(groups)

            events = [
                Event(id=_event_id(group), articles=_order_articles(group), analyses={a.id: analyses[a.id] for a in group if a.id in analyses})
                for group in groups
            ]
            events, unchanged = self._compare_with_history(run_id, events, config)
            stats['events_unchanged'] = unchanged

            now = self.deps.now()
            for event in events:
                event.score = score_event(event, now)
            events.sort(key=lambda e: e.score, reverse=True)
            events = events[: self.deps.max_events]

            degraded = 0
            for event in events:
                title = event.lead_analysis.title_zh if event.lead_analysis else event.lead.title
                event.summary, event.summary_degraded = self.deps.summarize(event.articles, title, event.lead_analysis)
                degraded += int(event.summary_degraded)
            stats['events_published'] = len(events)
            stats['summaries_degraded'] = degraded

            self.store.save_events(run_id, events)
            self.store.record_llm_calls(run_id, self.deps.llm_calls()[calls_before:])
            stats['llm'] = self.store.llm_stats(run_id)
            self.store.finish_run(run_id, 'completed', stats)
            return run_id
        except Exception as exc:
            log.exception('pipeline failed')
            self.store.record_llm_calls(run_id, self.deps.llm_calls()[calls_before:])
            self.store.finish_run(run_id, 'failed', stats, f'{type(exc).__name__}: {exc}')
            raise

    def _fetch_all(self, run_id: int) -> list[Article]:
        articles: list[Article] = []
        for source in self.store.list_sources(enabled_only=True):
            try:
                result = self.deps.fetch(source)
            except Exception as exc:  # 单个来源失败不影响整批
                result = FetchResult(source=source, error=f'{type(exc).__name__}: {exc}')
            self.store.record_source_result(run_id, source.id, len(result.articles), result.error)
            articles.extend(result.articles)
        return articles

    def _compare_with_history(self, run_id: int, events: list[Event], config: ClusterConfig) -> tuple[list[Event], int]:
        """与近 48 小时已发布事件比对：
        - 命中旧事件且所有报道都已发布过 → 丢弃（不重复推送）
        - 命中旧事件且出现新的报道（新 URL）→ 标记为更新
        用 URL 而不是来源名判断，来源改名不会被误判为更新。
        命中方式：共享 URL，或主报道向量相似度 ≥ 高阈值。
        """
        index = self.store.recent_event_index(hours=config.window_hours, exclude_run=run_id)
        previous = {group['event_key']: group for group in index.values()}
        previous_leads = self._previous_leads()
        lead_vectors = self.deps.embed([text for _, text in previous_leads]) if previous_leads else []
        kept: list[Event] = []
        unchanged = 0
        for event in events:
            match = next((index[a.url] for a in event.articles if a.url in index), None)
            if match is None and previous_leads:
                vector = self.deps.embed([analysis_embedding_text(event.lead, event.lead_analysis)])[0]
                best = max(range(len(previous_leads)), key=lambda i: cosine(vector, lead_vectors[i]))
                if cosine(vector, lead_vectors[best]) >= config.high:
                    match = previous.get(previous_leads[best][0])
            if match is None:
                kept.append(event)
            elif {a.url for a in event.articles} <= match['urls']:
                unchanged += 1
            else:
                event.is_update = True
                kept.append(event)
        return kept, unchanged

    def _previous_leads(self) -> list[tuple[str, str]]:
        run = self.store.latest_completed_run()
        if not run:
            return []
        return [
            (f"{run['id']}:{event.id}", analysis_embedding_text(event.lead, event.lead_analysis))
            for event in self.store.load_events(run['id'])
        ]
