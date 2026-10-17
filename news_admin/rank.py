"""事件排序与评级：可解释的线性打分，每个维度的得分都保留下来供页面展示。

总分 = 影响力 × 2                  （模型评级 1-5，取各报道中位数，避免单篇高估）满分 10
     + log2(1 + 独立来源数) × 1.5   （边际递减，十家转载不至于压过一条重大独家）8 家约 4.8
     + 新鲜度（24 小时半衰期）       0-1
     + (来源平均权重 - 1)            如路透 1.2 → +0.2
     - 评论类 2 分

评级（阈值按实际分布校准：单来源 4 级影响力新闻 ≈ 9.5）：
S ≥ 12 且影响力 ≥ 5   重大：多家来源报道的全局性事件
A ≥ 9.5              重要
B ≥ 7.5              值得一看
C 其余               一般
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime

from .models import Event, Rating, ScoreItem

HALF_LIFE_HOURS = 24
GRADES = (('S', 12.0), ('A', 9.5), ('B', 7.5))
GRADE_LABELS = {'S': '重大', 'A': '重要', 'B': '值得一看', 'C': '一般'}


def _grade(total: float, impact: float) -> str:
    for grade, threshold in GRADES:
        if total >= threshold and (grade != 'S' or impact >= 5):
            return grade
    return 'C'


def rate_event(event: Event, now: datetime) -> Rating:
    analyses = list(event.analyses.values())
    impact = statistics.median(a.impact for a in analyses) if analyses else 2
    opinion = bool(analyses) and all(a.is_opinion for a in analyses)
    sources = len(event.sources)

    times = [a.published_at for a in event.articles if a.published_at]
    if times:
        hours = max(0.0, (now - max(times)).total_seconds() / 3600)
        freshness = 0.5 ** (hours / HALF_LIFE_HOURS)
        fresh_detail = f'最新报道 {hours:.0f} 小时前' if hours >= 1 else '最新报道不到 1 小时'
    else:
        freshness, fresh_detail = 0.5, '时间未知，按中间值'
    weight = statistics.mean(a.source_weight for a in event.articles)

    reason = event.lead_analysis.impact_reason if event.lead_analysis else ''
    items = [
        ScoreItem(key='impact', label='影响力', value=f'{impact:g}/5', points=impact * 2, max_points=10, detail=reason),
        ScoreItem(key='sources', label='来源', value=f'{sources} 家', points=math.log2(1 + sources) * 1.5,
                  max_points=round(math.log2(1 + 8) * 1.5, 2), detail='独立来源数，转载合并计'),
        ScoreItem(key='freshness', label='新鲜度', value=f'{freshness:.0%}', points=freshness, max_points=1, detail=fresh_detail),
        ScoreItem(key='weight', label='来源权重', value=f'{weight:.2f}', points=weight - 1, max_points=0.5, detail='后台为来源设置的权重'),
    ]
    if opinion:
        items.append(ScoreItem(key='opinion', label='评论文章', value='是', points=-2, max_points=0, detail='评论/分析类降权'))
    for item in items:
        item.points = round(item.points, 2)
    total = round(sum(i.points for i in items), 2)
    grade = _grade(total, impact)
    return Rating(grade=grade, label=GRADE_LABELS[grade], total=total, items=items)


def score_event(event: Event, now: datetime) -> float:
    return rate_event(event, now).total
