"""逐篇结构化分析 + 同一事件判定（聚类模糊区间使用）。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from .cluster import Judge
from .llm import LLMClient, LLMError
from .models import Article, ArticleAnalysis, ContinuityVerdict, Event, Relation, SameEventVerdict, Topic
from .prompts import (
    ANALYZE_SYSTEM, ANALYZE_VERSION, CONTINUITY_SYSTEM, CONTINUITY_VERSION, SAME_EVENT_SYSTEM, SAME_EVENT_VERSION,
    analyze_user, continuity_user, same_event_user,
)

log = logging.getLogger(__name__)


def analyze_articles(client: LLMClient, articles: list[Article], min_topic_confidence: float = 0.5, workers: int = 4) -> dict[str, ArticleAnalysis]:
    def one(article: Article) -> tuple[str, ArticleAnalysis | None]:
        try:
            value = client.structured(ANALYZE_SYSTEM, analyze_user(article), ArticleAnalysis, stage='analyze', prompt_version=ANALYZE_VERSION).value
        except LLMError as exc:
            log.warning('analyze failed for %s: %s', article.url, exc)
            return article.id, None
        if value.topic_confidence < min_topic_confidence:
            value = value.model_copy(update={'topic': Topic.other})
        return article.id, value

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return {article_id: value for article_id, value in pool.map(one, articles) if value is not None}


def make_judge(client: LLMClient, analyses: dict[str, ArticleAnalysis]) -> Judge:
    def judge(left: Article, right: Article) -> bool:
        fp = lambda a: analyses[a.id].event_fingerprint if a.id in analyses else ''
        try:
            verdict = client.structured(
                SAME_EVENT_SYSTEM, same_event_user(left, right, fp(left), fp(right)), SameEventVerdict,
                stage='same_event', prompt_version=SAME_EVENT_VERSION,
            ).value
        except LLMError:
            return False
        return verdict.same_event

    return judge


def make_continuity_judge(client: LLMClient):
    def judge(new: Event, old: Event) -> Relation:
        try:
            return client.structured(
                CONTINUITY_SYSTEM, continuity_user(new, old), ContinuityVerdict,
                stage='continuity', prompt_version=CONTINUITY_VERSION,
            ).value.relation
        except LLMError:
            return Relation.unrelated

    return judge
