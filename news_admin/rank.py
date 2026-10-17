"""事件排序：可解释的线性打分。

score = 影响力 × 2                 （LLM 评级 1-5，取各报道中位数，避免单篇高估）
      + log2(1 + 独立来源数) × 1.5   （边际递减，十家转载不至于压过一条重大独家）
      + 新鲜度（24 小时半衰期，最多 +1）
      + (来源平均权重 - 1)
      - 评论类 2 分
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime

from .models import Event

HALF_LIFE_HOURS = 24


def score_event(event: Event, now: datetime) -> float:
    analyses = [a for a in event.analyses.values()]
    impact = statistics.median(a.impact for a in analyses) if analyses else 2
    opinion = bool(analyses) and all(a.is_opinion for a in analyses)
    times = [a.published_at for a in event.articles if a.published_at]
    freshness = 0.5
    if times:
        hours = max(0.0, (now - max(times)).total_seconds() / 3600)
        freshness = 0.5 ** (hours / HALF_LIFE_HOURS)
    weight = statistics.mean(a.source_weight for a in event.articles)
    score = impact * 2 + math.log2(1 + len(event.sources)) * 1.5 + freshness + (weight - 1)
    if opinion:
        score -= 2
    return round(score, 3)
