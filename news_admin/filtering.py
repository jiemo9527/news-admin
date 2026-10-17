"""信息价值过滤：在聚类之前去掉对读者没有实际意义的内容。

判断依据全部来自 analyze 阶段的结构化输出，规则是确定性的、可在后台调整：
- 没有新进展的科普/历史/背景文章（如「长江名称历经演变」）→ 丢弃
- 宣传、成就展示类且影响力 < 3 → 丢弃
- 影响力低于 min_impact（默认 2）→ 丢弃
- 评论文章默认保留但降权（rank 中处理），可配置为全部丢弃

被丢弃的文章及原因写入运行记录，后台可查，便于发现误杀。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ArticleAnalysis, ContentType


@dataclass
class FilterConfig:
    min_impact: int = 2
    drop_opinion: bool = False
    min_promo_impact: int = 3


def filter_reason(analysis: ArticleAnalysis, config: FilterConfig | None = None) -> str | None:
    """返回丢弃原因；保留时返回 None。"""
    config = config or FilterConfig()
    if analysis.content_type == ContentType.feature and not analysis.has_new_development:
        return '科普/背景类，无新进展'
    if analysis.content_type == ContentType.promo and analysis.impact < config.min_promo_impact:
        return f'宣传类且影响力 {analysis.impact} < {config.min_promo_impact}'
    if config.drop_opinion and analysis.content_type == ContentType.opinion:
        return '评论类（已配置不收录）'
    if analysis.impact < config.min_impact:
        return f'影响力 {analysis.impact} < {config.min_impact}'
    return None
