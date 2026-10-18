"""流水线编排：
fetch → dedupe → analyze → 价值过滤 → cluster → 前序关联 → rank → summarize → 配图 → 持久化。

所有外部依赖（抓取、模型、向量、图片）通过 PipelineDeps 注入，测试中可替换为假实现。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import tz
from .cluster import ClusterConfig, Embed, Judge, cluster_articles, cosine, embedding_text
from .dedupe import dedupe_articles
from .fetch import FetchResult, Source
from .filtering import FilterConfig, filter_reason
from .images import choose_image
from .llm import LLMCall
from .models import Article, ArticleAnalysis, Event, EventRef, EventSummary, Relation
from .rank import rate_event
from .summarize import fallback_summary
from .store import Store

log = logging.getLogger(__name__)

ContinuityJudge = Callable[[Event, Event], Relation]


@dataclass
class ContinuityConfig:
    lookback_days: float = 7
    candidates_per_event: int = 2
    max_judge_calls: int = 40
    max_refs: int = 4


@dataclass
class PipelineDeps:
    fetch: Callable[[Source], FetchResult]
    analyze: Callable[[list[Article]], dict[str, ArticleAnalysis]]
    embed: Embed
    judge_factory: Callable[[dict[str, ArticleAnalysis]], Judge]
    summarize: Callable[[list[Article], str, ArticleAnalysis | None], tuple[EventSummary, bool]]
    llm_calls: Callable[[], list[LLMCall]]
    continuity_judge: ContinuityJudge | None = None
    thumbnail: Callable[[str], str] | None = None
    now: Callable[[], datetime] = tz.now
    cluster_config: ClusterConfig | None = None
    filter_config: FilterConfig = field(default_factory=FilterConfig)
    continuity_config: ContinuityConfig = field(default_factory=ContinuityConfig)
    # 每期简报收录数；可以是数字或返回数字的函数（后台改了立即生效）
    max_events: int | Callable[[], int] = 50


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


def _event_text(event: Event) -> str:
    return analysis_embedding_text(event.lead, event.lead_analysis)


def _entities(event: Event) -> set[str]:
    return {e for a in event.analyses.values() for e in a.entities}


def _ref(run_id: int, event: Event, relation: str, reason: str = '') -> EventRef:
    first = event.first_published
    return EventRef(
        run_id=run_id,
        event_id=event.id,
        headline=event.summary.headline if event.summary else event.lead.title,
        published_at=first.published_at if first else None,
        time_precision=first.time_precision if first else 'none',
        relation=relation,  # type: ignore[arg-type]
        reason=reason,
    )


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

            articles, filtered = self._filter(articles, analyses)
            stats['articles_filtered'] = len(filtered)
            stats['filtered'] = filtered[:80]

            config = self.deps.cluster_config or ClusterConfig()
            groups = cluster_articles(
                articles, self.deps.embed, self.deps.judge_factory(analyses), config,
                text_of=lambda a: analysis_embedding_text(a, analyses.get(a.id)),
            )
            stats['clusters'] = len(groups)

            events = []
            for group in groups:
                event = Event(
                    id=_event_id(group), articles=_order_articles(group),
                    analyses={a.id: analyses[a.id] for a in group if a.id in analyses},
                )
                event.story_id = event.id
                events.append(event)
            events, continuity = self._link_history(run_id, events, config)
            stats.update(continuity)

            now = self.deps.now()
            for event in events:
                event.rating = rate_event(event, now)
                event.score = event.rating.total
            events.sort(key=lambda e: e.score, reverse=True)
            limit = self.deps.max_events() if callable(self.deps.max_events) else self.deps.max_events
            brief, rest = events[:limit], events[limit:]

            # 简报内的事件：模型写摘要并做事实校验；简报外的事件也全部入库（首页汇总可见），
            # 摘要直接用分析阶段已有的中文关键事实，不再额外调用模型
            degraded = 0
            for event in brief:
                title = event.lead_analysis.title_zh if event.lead_analysis else event.lead.title
                event.summary, event.summary_degraded = self.deps.summarize(event.articles, title, event.lead_analysis)
                degraded += int(event.summary_degraded)
            for event in rest:
                event.in_brief = False
                title = event.lead_analysis.title_zh if event.lead_analysis else event.lead.title
                event.summary = fallback_summary(event.articles, title, event.lead_analysis)
            stats['events_stored'] = len(events)
            stats['events_published'] = len(brief)
            stats['summaries_degraded'] = degraded

            stats['events_with_thumbnail'] = self._attach_thumbnails(events)

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

    def _filter(self, articles: list[Article], analyses: dict[str, ArticleAnalysis]) -> tuple[list[Article], list[dict]]:
        kept, dropped = [], []
        for article in articles:
            analysis = analyses.get(article.id)
            reason = filter_reason(analysis, self.deps.filter_config) if analysis else None
            if reason:
                dropped.append({
                    'source': article.source, 'title': analysis.title_zh if analysis else article.title,
                    'url': article.url, 'reason': reason,
                })
            else:
                kept.append(article)
        return kept, dropped

    def _link_history(self, run_id: int, events: list[Event], config: ClusterConfig) -> tuple[list[Event], dict]:
        """把本批事件与近 N 天已发布事件关联：

        - 同一事件、没有新报道（URL 都发布过）→ 不再发布
        - 同一事件、有新报道 → 标「更新」，引用上一次的报道
        - 后续进展（同一条故事线上的新事件）→ 引用前序报道，继承故事线 ID
        候选：共享 URL 直接视为同一事件；否则按向量相似度 + 共同实体取前几个候选交给 LLM 判断。
        """
        cc = self.deps.continuity_config
        everything = self.store.recent_events(days=cc.lookback_days, exclude_run=run_id, per_story=False)
        # 相似度候选：每条故事线只取最新一次；链接归属：查全部已发布事件（同一故事线的早期报道也算），
        # 否则后续报道出现后，旧报道再被抓到会被误当成后续报道的「更新」重复入库
        seen_stories: set[str] = set()
        history = []
        for rid, old in everything:
            story = old.story_id or old.id
            if story not in seen_stories:
                seen_stories.add(story)
                history.append((rid, old))
        stats = {'events_unchanged': 0, 'events_updated': 0, 'events_follow_up': 0, 'continuity_judge_calls': 0}
        if not history or not events:
            return events, stats
        url_owner: dict[str, tuple[int, Event]] = {}
        for rid, old in everything:  # 按运行从新到旧，同一链接归最近一次发布它的事件
            for a in old.articles:
                url_owner.setdefault(a.url, (rid, old))
        old_vectors = self.deps.embed([_event_text(old) for _, old in history])
        new_vectors = self.deps.embed([_event_text(e) for e in events])
        kept: list[Event] = []
        for event, vector in zip(events, new_vectors):
            match, relation, reason = None, None, ''
            owner = next((url_owner[a.url] for a in event.articles if a.url in url_owner), None)
            if owner:
                match, relation, reason = owner, Relation.same_event, '共享报道链接'
            else:
                entities = _entities(event)
                scored = []
                for index, (rid, old) in enumerate(history):
                    sim = cosine(vector, old_vectors[index])
                    shared = len(entities & _entities(old))
                    if sim >= config.low or (shared >= 2 and sim >= config.low - 0.15):
                        scored.append((sim + 0.03 * shared, sim, rid, old))
                scored.sort(key=lambda x: x[0], reverse=True)
                for _, sim, rid, old in scored[: cc.candidates_per_event]:
                    if sim >= config.high:
                        match, relation, reason = (rid, old), Relation.same_event, f'相似度 {sim:.2f}'
                        break
                    if self.deps.continuity_judge is None or stats['continuity_judge_calls'] >= cc.max_judge_calls:
                        break
                    stats['continuity_judge_calls'] += 1
                    verdict = self.deps.continuity_judge(event, old)
                    if verdict != Relation.unrelated:
                        match, relation, reason = (rid, old), verdict, '模型判定'
                        break
            if match is None:
                kept.append(event)
                continue
            rid, old = match
            if relation == Relation.same_event and {a.url for a in event.articles} <= {a.url for a in old.articles}:
                stats['events_unchanged'] += 1
                continue
            label = 'update' if relation == Relation.same_event else 'follow_up'
            event.is_update = relation == Relation.same_event
            event.story_id = old.story_id or old.id
            chain = [_ref(rid, old, label, reason), *old.previous]
            seen: set[tuple[int, str]] = set()
            refs = []
            for ref in chain:
                key = (ref.run_id, ref.event_id)
                if key not in seen:
                    seen.add(key)
                    refs.append(ref)
            event.previous = refs[: cc.max_refs]
            stats['events_updated' if event.is_update else 'events_follow_up'] += 1
            kept.append(event)
        return kept, stats

    def _attach_thumbnails(self, events: list[Event]) -> int:
        if self.deps.thumbnail is None:
            return 0
        count = 0
        for event in events:
            for article in event.articles:
                url = choose_image([article.image_url])
                key = self.deps.thumbnail(url) if url else ''
                if key:
                    event.thumbnail = key
                    count += 1
                    break
        return count
