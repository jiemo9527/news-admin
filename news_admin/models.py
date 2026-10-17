"""领域模型与大模型结构化输出 Schema。

LLM 输出模型（ArticleAnalysis / SameEventVerdict / EventSummary）同时用于：
1. 生成 JSON Schema 发给模型（response_format=json_schema）；
2. 校验模型返回值，失败时把校验错误回传给模型重试。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Region(str, Enum):
    domestic = 'domestic'
    hk_mo_tw = 'hk_mo_tw'
    international = 'international'
    global_ = 'global'


class Topic(str, Enum):
    politics = 'politics'
    military = 'military'
    economy = 'economy'
    business = 'business'
    technology = 'technology'
    society = 'society'
    health = 'health'
    environment = 'environment'
    culture = 'culture'
    other = 'other'


REGION_LABELS = {
    Region.domestic: '国内',
    Region.hk_mo_tw: '港澳台',
    Region.international: '国际',
    Region.global_: '全球',
}

TOPIC_LABELS = {
    Topic.politics: '政治外交',
    Topic.military: '军事安全',
    Topic.economy: '宏观经济与金融',
    Topic.business: '产业与公司',
    Topic.technology: '科技',
    Topic.society: '社会民生',
    Topic.health: '健康医药',
    Topic.environment: '环境能源',
    Topic.culture: '文体娱乐',
    Topic.other: '其他',
}


class Article(BaseModel):
    """抓取并标准化后的单篇文章。"""

    id: str
    source: str
    url: str
    title: str
    body: str = ''
    published_at: datetime | None = None
    source_weight: float = 1.0
    duplicate_sources: list[str] = Field(default_factory=list)

    @property
    def text_for_analysis(self) -> str:
        return f'{self.title}\n{self.body}'.strip()


class _LLMOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')


ShortText = Annotated[str, Field(min_length=1, max_length=60)]


class ArticleAnalysis(_LLMOutput):
    """逐篇分析结果。"""

    title_zh: str = Field(description='中文标题，客观陈述事实，不超过32字，不加评价词', max_length=40)
    region: Region = Field(description='事件发生地/主要影响范围，按内容判断，与来源媒体无关')
    topic: Topic = Field(description='主题，从枚举中选最贴切的一个')
    topic_confidence: float = Field(ge=0, le=1, description='主题判断的把握程度')
    is_opinion: bool = Field(description='是否为评论、分析、社论，而非事实报道')
    entities: list[ShortText] = Field(max_length=8, description='关键参与方：人名、机构、国家、公司，使用中文通行译名')
    key_facts: list[Annotated[str, Field(max_length=80)]] = Field(
        min_length=1, max_length=4, description='文章中可核实的关键事实，每条一句中文，不加推测'
    )
    event_fingerprint: str = Field(
        max_length=60, description='格式：主体|行为|对象|地点，用于判断不同文章是否同一事件'
    )
    impact: int = Field(ge=1, le=5, description='影响力评级，标准见系统提示')
    impact_reason: str = Field(max_length=60, description='一句话说明评级理由')


class SameEventVerdict(_LLMOutput):
    same_event: bool
    reason: str = Field(max_length=80)


class SummarySentence(_LLMOutput):
    text: str = Field(max_length=90, description='一句中文陈述，不含来源编号')
    sources: list[int] = Field(min_length=1, description='支撑本句的来源编号，从1开始')


class EventSummary(_LLMOutput):
    headline: str = Field(max_length=32, description='事件标题，客观、不超过32字')
    sentences: list[SummarySentence] = Field(min_length=1, max_length=4)
    conflicts: list[Annotated[str, Field(max_length=90)]] = Field(
        default_factory=list, max_length=2, description='来源之间说法不一致之处，写明各方说法；没有则为空'
    )

    @field_validator('headline')
    @classmethod
    def _strip_headline(cls, value: str) -> str:
        return value.strip()


class Event(BaseModel):
    """聚类后的事件。"""

    id: str
    articles: list[Article]
    analyses: dict[str, ArticleAnalysis] = Field(default_factory=dict)
    summary: EventSummary | None = None
    summary_degraded: bool = False
    score: float = 0.0
    is_update: bool = False

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for article in self.articles:
            for name in [article.source, *article.duplicate_sources]:
                if name not in seen:
                    seen.append(name)
        return seen

    @property
    def lead(self) -> Article:
        return self.articles[0]

    @property
    def lead_analysis(self) -> ArticleAnalysis | None:
        return self.analyses.get(self.lead.id)
