"""事件聚类：向量相似度 + 时间窗，模糊区间交给 LLM 判定。

- sim ≥ high：直接视为同一事件（省钱）
- low ≤ sim < high：调用 judge（LLM）按「主体+行为+时间地点」判定
- sim < low：不同事件，不调用模型
- 发布时间相差超过 window_hours 的文章不合并
用并查集合并，结果与输入顺序无关。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .models import Article

Embed = Callable[[list[str]], list[Sequence[float]]]
Judge = Callable[[Article, Article], bool]


@dataclass
class ClusterConfig:
    high: float = 0.86
    low: float = 0.72
    window_hours: float = 48
    max_judge_calls: int = 60


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def _within_window(left: Article, right: Article, hours: float) -> bool:
    if not left.published_at or not right.published_at:
        return True
    return abs((left.published_at - right.published_at).total_seconds()) <= hours * 3600


def embedding_text(article: Article) -> str:
    return f'{article.title}\n{article.body[:300]}'


def cluster_articles(
    articles: list[Article], embed: Embed, judge: Judge, config: ClusterConfig | None = None,
    text_of: Callable[[Article], str] = embedding_text,
) -> list[list[Article]]:
    config = config or ClusterConfig()
    if not articles:
        return []
    vectors = embed([text_of(a) for a in articles])
    parent = list(range(len(articles)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pairs = []
    for i in range(len(articles)):
        for j in range(i + 1, len(articles)):
            if _within_window(articles[i], articles[j], config.window_hours):
                pairs.append((cosine(vectors[i], vectors[j]), i, j))
    pairs.sort(reverse=True)

    judge_calls = 0
    for sim, i, j in pairs:
        if sim < config.low:
            break
        root_i, root_j = find(i), find(j)
        if root_i == root_j:
            continue
        if sim < config.high:
            if judge_calls >= config.max_judge_calls:
                continue
            judge_calls += 1
            if not judge(articles[i], articles[j]):
                continue
        parent[root_j] = root_i

    groups: dict[int, list[int]] = {}
    for index in range(len(articles)):
        groups.setdefault(find(index), []).append(index)
    ordered = sorted(groups.values(), key=lambda members: (-len(members), members[0]))
    return [[articles[i] for i in members] for members in ordered]
