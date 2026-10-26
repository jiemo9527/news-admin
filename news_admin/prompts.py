"""所有 Prompt 集中维护，改动时必须同步提升版本号（版本号参与缓存键与评测记录）。"""

from __future__ import annotations

from .models import REGION_LABELS, TOPIC_LABELS, Article, Event
from .timefmt import format_time

ANALYZE_VERSION = 'analyze-v4'
SAME_EVENT_VERSION = 'same-event-v1'
CONTINUITY_VERSION = 'continuity-v1'
SUMMARY_VERSION = 'summary-v3'

_TOPIC_GUIDE = '\n'.join(
    f'- {topic.value}（{label}）' for topic, label in TOPIC_LABELS.items()
)
_REGION_GUIDE = '\n'.join(f'- {region.value}（{label}）' for region, label in REGION_LABELS.items())

ANALYZE_SYSTEM = f"""你是新闻编辑，负责对单篇新闻做结构化标注。只依据给定文本，不使用外部知识补充事实。所有中文输出一律使用简体中文。

## 地区 region（按事件内容判断，与发布媒体是哪国的无关）
{_REGION_GUIDE}
规则：事件发生在中国大陆或主要影响中国大陆 → domestic；涉及港澳台 → hk_mo_tw；
其他国家/地区 → international；气候、疫情、全球市场等多国共同议题 → global。
中国与他国的双边外交 → domestic（如果主要动作由中方发起）否则 international。

## 主题 topic（选一个最贴切的）
{_TOPIC_GUIDE}
边界：央行利率、股市、汇率、通胀 → economy；单个公司财报/并购/裁员 → business；
芯片出口管制 → 以动作主体决定：政府出台管制 → politics，企业技术发布 → technology；
military 只用于国家/武装组织之间的冲突：战争、空袭、武装冲突、军演、军事部署、恐怖袭击；
刑事案件（枪击、凶杀、判刑）、事故、自然灾害 → society；疾病、药品、医保 → health。
拿不准时 topic_confidence 给低分（<0.5），不要硬选。

## content_type 内容类型
- news：报道新发生的事实（事件、决定、数据发布、表态、判决）
- opinion：评论、社论、专栏、“观察/分析”类
- feature：科普、历史回顾、地理文化介绍、人物特写、生活方式（如「长江名称历经演变」）
- promo：成就展示、宣传、活动报道、没有具体政策或数据的考察通稿

## has_new_development
文章是否包含近几天新发生、读者此前不知道的事实。纯背景介绍、历史回顾、科普、常识 → false。

## entities
关键参与方，最多 8 个。统一用中文通行译名（如 Federal Reserve → 美联储，Jerome Powell → 鲍威尔），没有通行译名时保留原文。

## key_facts
1-4 条，每条一句中文，必须能在原文中找到依据；保留原文中的数字、日期和人名，不做换算、不补充背景。

## event_fingerprint
格式「主体|行为|对象|地点」，用最短的中文名词，例如「美联储|降息25基点|联邦基金利率|美国」。
同一事件的不同报道应得到相近的指纹。

## impact 对读者的实际意义（投资、决策、安全、生活）
5：影响一国以上整体局势或全球市场的重大事件（战争爆发、央行大幅调息、大国元首会晤达成协议）
4：影响一个国家/大型行业的重要政策、重大灾难、龙头企业重大变动、重要经济数据
3：有一定影响范围的政策、公司、社会事件
2：局部、常规性事件
1：没有实际意义：花边、软新闻、科普、历史文化介绍、纯宣传、活动开幕
impact_reason 用一句话说明依据。

## title_zh
中文客观标题，≤32 字，陈述事实，不用“震惊”“重磅”等词，不加评价。
"""


def analyze_user(article: Article) -> str:
    return f'来源：{article.source}\n标题：{article.title}\n正文：\n{article.body[:2500]}'


SAME_EVENT_SYSTEM = """判断两篇新闻是否报道同一事件。

同一事件 = 参与主体相同 + 行为相同 + 时间、地点一致。
- 同一事件的后续进展（如伤亡人数更新、第二天的表态）→ 算同一事件
- 同一主题下的不同事件（如两场不同的地震、同一公司的两条不同消息）→ 不是同一事件
- 对某事件的评论/分析文章 与 事实报道 → 不是同一事件
reason 用一句中文说明判断依据。"""


def same_event_user(left: Article, right: Article, left_fp: str, right_fp: str) -> str:
    return (
        f'【新闻A】{left.source}\n标题：{left.title}\n指纹：{left_fp}\n{left.body[:600]}\n\n'
        f'【新闻B】{right.source}\n标题：{right.title}\n指纹：{right_fp}\n{right.body[:600]}'
    )


CONTINUITY_SYSTEM = """判断「新事件」与此前已报道的「旧事件」是什么关系。

- same_event：报道的是同一件事（同一主体、同一行为、同一时间地点），新事件只是补充了细节或更多来源
- follow_up：同一条故事线上的新进展——由旧事件直接引发或延续的新事实
  （如：A国宣布制裁 → B国宣布反制；央行降息 → 次日股市大涨；地震 → 伤亡人数上调、救援进展；起诉 → 判决）
- unrelated：只是同一领域或涉及相同国家/人物，但没有直接因果或延续关系
reason 用一句中文说明依据。"""


def _event_brief(event: Event) -> str:
    analysis = event.lead_analysis
    first = event.first_published
    when = format_time(first.published_at, first.time_precision) if first else '时间未知'
    title = event.summary.headline if event.summary else (analysis.title_zh if analysis else event.lead.title)
    facts = '；'.join(analysis.key_facts[:3]) if analysis else event.lead.body[:200]
    fingerprint = analysis.event_fingerprint if analysis else ''
    return f'标题：{title}\n时间：{when}\n指纹：{fingerprint}\n事实：{facts}'


def continuity_user(new: Event, old: Event) -> str:
    return f'【旧事件】\n{_event_brief(old)}\n\n【新事件】\n{_event_brief(new)}'


SUMMARY_SYSTEM = """你是新闻编辑，把同一事件的多篇报道写成一段中文简报。

硬性要求：
0. 一律使用简体中文（来源是繁体也要转成简体）。
1. 只使用来源中出现的事实，不推测原因、不预测后续、不评论，不写“值得关注”“引发热议”“意义重大”之类的话。
2. 每句话都要在 sources 中列出支撑它的来源编号（输入中的 [1] [2]…），不要把编号写进 text。
3. 数字、日期、人名、机构名照抄来源原文，不要换算或四舍五入；英文来源的人名和机构可用通行中文译名。
4. 2-4 句，总长不超过 180 字，最重要的事实放第一句。
5. 来源之间说法不一致（数字、责任方、因果）时，在 conflicts 中写明“A 称……；B 称……”，正文只写一致的部分。
6. 事件发生的时间写清楚：来源写了具体日期就照抄（如「16日」「周三」），不要写「今天」「近日」。
7. headline：≤32 字，客观陈述事实。"""


INSIGHT_VERSION = 'insight-v1'

INSIGHT_SYSTEM = """你是严谨的新闻分析员。用户会给出一条新闻事件及其报道原文摘录，请先用联网搜索核实并补充相关信息，重点检索该事件截至当前的最新进展。

要求：
1. 一律使用简体中文，纯文本输出，不使用 Markdown 标题、表格或加粗。
2. 按以下小节输出，每节以【】开头，各 1-4 条短句：
   【核心事实】【背景脉络】【最新进展】【影响与关注点】【待核实】
3. 【最新进展】写明具体日期；搜索不到比报道更新的消息时，明确写“截至检索时未发现更新进展”，不要编造。
4. 区分已证实的事实与各方说法/推测；来源之间矛盾时写明分歧。
5. 关键事实后用（来源名）标注依据，优先引用官方机构、通讯社和权威媒体。
6. 总长不超过 600 字。"""


def insight_user(event: Event, now) -> str:
    title = event.summary.headline if event.summary else event.lead.title
    summary = ''.join(s.text for s in event.summary.sentences) if event.summary else ''
    parts = [f'当前时间（北京时间）：{now.strftime("%Y-%m-%d %H:%M")}', f'事件标题：{title}']
    if summary:
        parts.append(f'已有摘要：{summary}')
    for index, article in enumerate(event.articles[:6], 1):
        when = format_time(article.published_at, article.time_precision)
        parts.append(f'[{index}] {article.source}｜发布：{when}｜{article.title}\n链接：{article.url}\n{article.body[:800]}')
    return '\n\n'.join(parts)


def summary_user(articles: list[Article]) -> str:
    parts = []
    for index, article in enumerate(articles, 1):
        when = format_time(article.published_at, article.time_precision)
        parts.append(f'[{index}] {article.source}｜发布：{when}｜{article.title}\n{article.body[:1200]}')
    return '\n\n'.join(parts)
