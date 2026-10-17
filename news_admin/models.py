"""领域模型与大模型结构化输出 Schema。

LLM 输出模型（ArticleAnalysis / SameEventVerdict / EventSummary）同时用于：
1. 生成 JSON Schema 发给模型（response_format=json_schema）；
2. 校验模型返回值，失败时把校验错误回传给模型重试。
"""

from __future__ import annotations

from datetime import datetime

from . import tz as _tz
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class ContentType(str, Enum):
    news = 'news'
    opinion = 'opinion'
    feature = 'feature'
    promo = 'promo'


class Relation(str, Enum):
    same_event = 'same_event'
    follow_up = 'follow_up'
    unrelated = 'unrelated'


TimePrecision = Literal['exact', 'date', 'none']

CONTENT_TYPE_LABELS = {
    ContentType.news: '新闻',
    ContentType.opinion: '评论',
    ContentType.feature: '特写/科普',
    ContentType.promo: '宣传',
}

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
    updated_at: datetime | None = None
    time_precision: TimePrecision = 'none'
    fetched_at: datetime | None = None
    image_url: str = ''
    source_weight: float = 1.0
    duplicate_sources: list[str] = Field(default_factory=list)

    @field_validator('published_at', 'updated_at', 'fetched_at')
    @classmethod
    def _beijing(cls, value: datetime | None) -> datetime | None:
        return _tz.to_local(value) if value else None

    @model_validator(mode='before')
    @classmethod
    def _legacy_precision(cls, data: object) -> object:
        """v2 早期快照没有 time_precision：有时间但精度未知时按「只有日期」处理，不冒充精确时刻。"""
        if isinstance(data, dict) and 'time_precision' not in data and data.get('published_at'):
            data = {**data, 'time_precision': 'date'}
        return data

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
    content_type: ContentType = Field(description='news 事实报道 / opinion 评论分析 / feature 科普历史特写 / promo 宣传成就展示')
    has_new_development: bool = Field(description='是否报道了近期新发生的事实（事件、决定、数据、表态），而不是背景介绍或回顾')
    entities: list[ShortText] = Field(max_length=8, description='关键参与方：人名、机构、国家、公司，使用中文通行译名')
    key_facts: list[Annotated[str, Field(max_length=80)]] = Field(
        min_length=1, max_length=4, description='文章中可核实的关键事实，每条一句中文，不加推测'
    )
    event_fingerprint: str = Field(
        max_length=60, description='格式：主体|行为|对象|地点，用于判断不同文章是否同一事件'
    )
    impact: int = Field(ge=1, le=5, description='影响力评级，标准见系统提示')
    impact_reason: str = Field(max_length=60, description='一句话说明评级理由')

    @model_validator(mode='before')
    @classmethod
    def _upgrade_v1(cls, data: object) -> object:
        """兼容 v1 快照：is_opinion → content_type，缺失的 has_new_development 视为 True。"""
        if isinstance(data, dict) and 'content_type' not in data and 'is_opinion' in data:
            data = dict(data)
            data['content_type'] = 'opinion' if data.pop('is_opinion') else 'news'
            data.setdefault('has_new_development', True)
        return data

    @property
    def is_opinion(self) -> bool:
        return self.content_type == ContentType.opinion


class ContinuityVerdict(_LLMOutput):
    relation: Relation
    reason: str = Field(max_length=80)


class EventRef(BaseModel):
    """指向之前简报中的事件。"""

    run_id: int
    event_id: str
    headline: str
    published_at: datetime | None = None
    time_precision: TimePrecision = 'none'
    relation: Literal['update', 'follow_up']

    @field_validator('published_at')
    @classmethod
    def _beijing(cls, value: datetime | None) -> datetime | None:
        return _tz.to_local(value) if value else None
    reason: str = ''

    @model_validator(mode='after')
    def _known_time_is_at_least_a_date(self) -> 'EventRef':
        # 有时间戳却标为 none（早期快照）时，至少日期是可信的
        if self.published_at is not None and self.time_precision == 'none':
            self.time_precision = 'date'
        return self


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


class ScoreItem(BaseModel):
    """排序分的一个维度。"""

    key: str
    label: str
    value: str
    points: float
    max_points: float
    detail: str = ''


class Rating(BaseModel):
    grade: Literal['S', 'A', 'B', 'C']
    label: str
    total: float
    items: list[ScoreItem]


class Event(BaseModel):
    """聚类后的事件。"""

    id: str
    articles: list[Article]
    analyses: dict[str, ArticleAnalysis] = Field(default_factory=dict)
    summary: EventSummary | None = None
    summary_degraded: bool = False
    score: float = 0.0
    rating: Rating | None = None
    is_update: bool = False
    thumbnail: str = ''
    story_id: str = ''
    previous: list[EventRef] = Field(default_factory=list)

    def time_points(self) -> list[tuple[datetime, str]]:
        points = [(a.published_at, a.time_precision) for a in self.articles if a.published_at]
        points += [(a.updated_at, 'exact') for a in self.articles if a.updated_at]
        return points

    @property
    def first_published(self) -> Article | None:
        dated = [a for a in self.articles if a.published_at]
        return min(dated, key=lambda a: a.published_at) if dated else None

    @property
    def last_updated(self) -> datetime | None:
        times = [t for a in self.articles for t in (a.published_at, a.updated_at) if t]
        return max(times) if times else None

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
