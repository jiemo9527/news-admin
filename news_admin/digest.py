"""把事件组织成简报：要闻 + 按主题分栏；纯文本版用于 IM/邮件推送。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import REGION_LABELS, TOPIC_LABELS, Event, Topic


@dataclass
class Section:
    title: str
    events: list[Event] = field(default_factory=list)


def event_topic(event: Event) -> Topic:
    analysis = event.lead_analysis
    return analysis.topic if analysis else Topic.other


def event_region_label(event: Event) -> str:
    analysis = event.lead_analysis
    return REGION_LABELS[analysis.region] if analysis else ''


def build_sections(events: list[Event], top_n: int = 5) -> list[Section]:
    ranked = sorted(events, key=lambda e: e.score, reverse=True)
    sections = [Section('要闻', ranked[:top_n])] if ranked else []
    by_topic: dict[Topic, Section] = {}
    for event in ranked[top_n:]:
        topic = event_topic(event)
        by_topic.setdefault(topic, Section(TOPIC_LABELS[topic])).events.append(event)
    order = list(TOPIC_LABELS)
    sections.extend(by_topic[t] for t in sorted(by_topic, key=order.index))
    return sections


def event_text(event: Event) -> str:
    summary = event.summary
    headline = summary.headline if summary else event.lead.title
    lines = [('【更新】' if event.is_update else '') + headline]
    if summary:
        lines.append(''.join(s.text for s in summary.sentences))
        lines.extend(f'说法不一：{c}' for c in summary.conflicts)
    lines.append('来源：' + '、'.join(event.sources))
    return '\n'.join(lines)


def render_text(events: list[Event], web_url: str = '', title: str = '新闻简报', top_n: int = 5) -> str:
    blocks = [title]
    for section in build_sections(events, top_n):
        blocks.append(f'== {section.title} ==')
        blocks.extend(f'{i}. {event_text(e)}' for i, e in enumerate(section.events, 1))
    if web_url:
        blocks.append(f'完整版：{web_url}')
    return '\n\n'.join(blocks)
