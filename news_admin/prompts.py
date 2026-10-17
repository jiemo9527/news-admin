"""所有 Prompt 集中维护，改动时必须同步提升版本号（版本号参与缓存键与评测记录）。"""

from __future__ import annotations

from .models import REGION_LABELS, TOPIC_LABELS, Article

ANALYZE_VERSION = 'analyze-v3'
SAME_EVENT_VERSION = 'same-event-v1'
SUMMARY_VERSION = 'summary-v2'

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

## is_opinion
评论、社论、专栏、“分析”类文章为 true；事实报道为 false。

## entities
关键参与方，最多 8 个。统一用中文通行译名（如 Federal Reserve → 美联储，Jerome Powell → 鲍威尔），没有通行译名时保留原文。

## key_facts
1-4 条，每条一句中文，必须能在原文中找到依据；保留原文中的数字、日期和人名，不做换算、不补充背景。

## event_fingerprint
格式「主体|行为|对象|地点」，用最短的中文名词，例如「美联储|降息25基点|联邦基金利率|美国」。
同一事件的不同报道应得到相近的指纹。

## impact 影响力评级
5：影响一国以上整体局势或全球市场的重大事件（战争爆发、央行大幅调息、大国元首会晤达成协议）
4：影响一个国家/大型行业的重要政策、重大灾难、龙头企业重大变动
3：有一定影响范围的政策、公司、社会事件
2：局部、常规性事件
1：花边、软新闻
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


SUMMARY_SYSTEM = """你是新闻编辑，把同一事件的多篇报道写成一段中文简报。

硬性要求：
0. 一律使用简体中文（来源是繁体也要转成简体）。
1. 只使用来源中出现的事实，不推测原因、不预测后续、不评论，不写“值得关注”“引发热议”“意义重大”之类的话。
2. 每句话都要在 sources 中列出支撑它的来源编号（输入中的 [1] [2]…），不要把编号写进 text。
3. 数字、日期、人名、机构名照抄来源原文，不要换算或四舍五入；英文来源的人名和机构可用通行中文译名。
4. 2-4 句，总长不超过 180 字，最重要的事实放第一句。
5. 来源之间说法不一致（数字、责任方、因果）时，在 conflicts 中写明“A 称……；B 称……”，正文只写一致的部分。
6. headline：≤32 字，客观陈述事实。"""


def summary_user(articles: list[Article]) -> str:
    parts = []
    for index, article in enumerate(articles, 1):
        parts.append(f'[{index}] {article.source}｜{article.title}\n{article.body[:1200]}')
    return '\n\n'.join(parts)
