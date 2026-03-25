from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any
from urllib.parse import urlparse

from .thumbnail_cache import is_usable_thumbnail_url
from .text_normalization import normalize_body_punctuation

SECTION_ORDER = ['今日要点', '国内', '国际', '科技', '金融', '观察补充']
GENERIC_TITLE_MARKERS = ['live', '快讯', '更新', 'watch', 'video', 'photos', '直播']
TECH_KEYWORDS = ['人工智能', '芯片', '半导体', '机器人', '航天', '卫星', '6g', '算法', '算力', '自动驾驶', '脑机', '科技', '技术', 'ai', 'chip', 'tech', 'space']
FINANCE_KEYWORDS = ['金融', '贸易', '外贸', '通胀', '油价', '关税', '市场', '银行', '股市', '汇率', '投资', '支付', '财政', '利率', '股价', '收购', '并购', '央行', '供应链', 'supply chain', 'tariff', 'trade', 'rates', 'stocks', 'acquisition']
DOMESTIC_KEYWORDS = ['中国', '全国', '国务院', '人大', '政协', '最高法', '最高检', '教育部', '公安部', '发改委', '工信部', '商务部', '证监会', '国家卫健委', '国内', '北京', '上海', '广东', '深圳']
INTERNATIONAL_KEYWORDS = ['国际', '以色列', '伊朗', '美国', '欧洲', '俄罗斯', '乌克兰', '中东', '联合国', '日本', '韩国', '巴基斯坦', '阿富汗', '黎巴嫩', '叙利亚', '古巴', '委内瑞拉', '加拿大', '孟加拉国', '尼日利亚', '阿联酋', 'africa', 'europe', 'iran', 'israel', 'ukraine', 'russia', 'gaza', 'pakistan', 'afghanistan', 'lebanon', 'syria']
CONFLICT_KEYWORDS = ['冲突', '战争', '空袭', '导弹', '袭击', '停火', '军事', '制裁', '航运', 'war', 'strike', 'missile', 'ceasefire']
POLICY_KEYWORDS = ['政策', '法典', '规划', '会议', '立法', '法治', '监管', '条例', 'proposal', 'law', 'policy']
RISK_KEYWORDS = ['战争', '袭击', '爆炸', '导弹', '空袭', '使馆', '大使馆', '护航', '霍尔木兹', '油价', '能源危机', '制裁', '安全', '停火', '军事', 'terror', 'attack', 'embassy', 'oil', 'sanction']
DOMESTIC_SOURCE_NAMES = {'央视新闻', '新华社', '人民网'}
TECH_URL_MARKERS = ['technology', 'tech', 'ai', 'chip', 'chips', 'semiconductor', 'openai', 'nvidia', 'quantum', 'robot', 'space']
FINANCE_URL_MARKERS = ['business', 'markets', 'finance', 'trade', 'energy', 'stocks', 'money', 'currencies', 'investment', 'deal', 'acquisition']
SUMMARY_SENTENCE_LIMIT = 3
MIN_SUMMARY_SENTENCE_COUNT = 2
MIN_SUMMARY_CONTENT_LENGTH = 45
MIN_USABLE_SUMMARY_CONTENT_LENGTH = 24
OBSERVATION_LIMIT = 3
TODAY_TOP_LIMIT = 10
SECTION_ITEM_LIMIT = 20
ANALYTIC_FRAGMENT_MARKERS = [
    '为什么重要', '值得关注', '值得跟踪', '值得继续', '令人费解', '头痛', '担忧',
    '我们所知道的', '分钟阅读', 'why it matters', 'what it means', 'opinion', 'analysis', 'market view', 'outlook', 'signals to watch', 'minute read',
]
GENERIC_MERGE_TOKENS = {'造成', '成一', '一人', '人死', '死亡', '受伤', '伤亡', '警方', '记者'}
GENERIC_MERGE_PHRASES = {'造成一人', '成一人死', '一人死亡', '造成死亡', '警方发言', '告诉记者'}
LEADING_TITLE_NOISE_PATTERNS = [
    re.compile(r'^(?:实时更新|更新|快讯|消息)\s*[:：-]\s*', re.I),
    re.compile(r'^(?:live\s+updates?|updates?)\s*[:：-]\s*', re.I),
    re.compile(r'^据[^,，:：]{0,16}(?:称|消息人士称)\s*[,，:：-]?\s*'),
    re.compile(r'^(?:伊朗战争实时更新|战争实时更新|冲突实时更新)\s*[:：-]\s*'),
]


def clean_title_text(title: str) -> str:
    lowered = title.lower().strip()
    for pattern in LEADING_TITLE_NOISE_PATTERNS:
        lowered = pattern.sub('', lowered)
    lowered = re.sub(r'https?://\S+', ' ', lowered)
    lowered = re.sub(r'[_/|]+', ' ', lowered)
    lowered = re.sub(r'[^\w\u4e00-\u9fff]+', ' ', lowered, flags=re.UNICODE)
    lowered = re.sub(r'\s+', ' ', lowered)
    return lowered.strip()


def normalize_title(title: str) -> str:
    return clean_title_text(title).replace(' ', '')[:120]


def is_generic_title(title: str) -> bool:
    lowered = title.lower()
    return len(normalize_title(title)) < 12 or any(marker in lowered for marker in GENERIC_TITLE_MARKERS)


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r'/+', '/', parsed.path or '/').rstrip('/') or '/'
    return f'{parsed.netloc.lower()}{path}'


def shared_token_count(left: str, right: str) -> int:
    return len(title_tokens(left) & title_tokens(right))


def title_phrases(title: str) -> set[str]:
    normalized = clean_title_text(title)
    if not normalized:
        return set()
    phrases: set[str] = set()
    for chunk in re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]+', normalized):
        if re.fullmatch(r'[\u4e00-\u9fff]+', chunk):
            if len(chunk) >= 4:
                phrases.update(chunk[index:index + 4] for index in range(len(chunk) - 3))
        elif len(chunk) >= 4:
            phrases.add(chunk)
    return phrases


def shared_phrase_count(left: str, right: str) -> int:
    return len(title_phrases(left) & title_phrases(right))


def meaningful_shared_token_count(left: str, right: str) -> int:
    return len((title_tokens(left) & title_tokens(right)) - GENERIC_MERGE_TOKENS)


def meaningful_shared_phrase_count(left: str, right: str) -> int:
    return len((title_phrases(left) & title_phrases(right)) - GENERIC_MERGE_PHRASES)


def story_key(item: dict[str, Any]) -> str:
    title_key = normalize_title(item['title'])
    path_key = re.sub(r'[^\w]+', '', urlparse(item['url']).path.lower())[:48]
    if is_generic_title(item['title']):
        return f'{title_key}|{path_key}'
    return title_key


def title_tokens(title: str) -> set[str]:
    normalized = clean_title_text(title)
    if not normalized:
        return set()
    tokens: set[str] = set()
    for chunk in re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]+', normalized):
        if re.fullmatch(r'[\u4e00-\u9fff]+', chunk):
            if len(chunk) == 1:
                tokens.add(chunk)
            else:
                tokens.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
        elif len(chunk) > 1:
            tokens.add(chunk)
    return tokens


def title_similarity(left: str, right: str) -> float:
    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union if union else 0.0


def merge_text(evidence: list[dict[str, Any]]) -> str:
    best = sorted(
        evidence,
        key=lambda row: (
            -len(str(row.get('snippet', ''))),
            -len(str(row.get('title', ''))),
            int(row.get('rank_order', 0)),
        ),
    )[0]
    return f"{best.get('title', '')} {best.get('snippet', '')}".strip()


def merge_text_similarity(left: str, right: str) -> float:
    left_tokens = text_tokens(left)
    right_tokens = text_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union if union else 0.0


def dedupe_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in sorted(evidence, key=lambda item: (int(item['rank_order']), len(str(item['title'])), normalize_url(str(item['url'])))):
        source_name = str(row['source_name'])
        url_key = normalize_url(str(row['url']))
        title_key = normalize_title(str(row['title']))[:64]
        dedupe_key = (source_name, url_key, title_key)
        if dedupe_key in seen:
            continue
        similar_from_same_source = next(
            (
                existing
                for existing in deduped
                if str(existing['source_name']) == source_name
                and (
                    normalize_url(str(existing['url'])) == url_key
                    or title_key == normalize_title(str(existing['title']))[:64]
                    or title_similarity(str(existing['title']), str(row['title'])) >= 0.84
                )
            ),
            None,
        )
        if similar_from_same_source is not None:
            continue
        seen.add(dedupe_key)
        deduped.append(row)
    return deduped


def should_merge_groups(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    left_title = str(left[0]['title'])
    right_title = str(right[0]['title'])
    left_normalized = normalize_title(left_title)
    right_normalized = normalize_title(right_title)
    similarity = title_similarity(left_title, right_title)
    shared = shared_token_count(left_title, right_title)
    shared_phrases = shared_phrase_count(left_title, right_title)
    meaningful_shared = meaningful_shared_token_count(left_title, right_title)
    meaningful_phrases = meaningful_shared_phrase_count(left_title, right_title)
    left_text = merge_text(left)
    right_text = merge_text(right)
    content_similarity = merge_text_similarity(left_text, right_text)
    left_content_tokens = text_tokens(left_text)
    right_content_tokens = text_tokens(right_text)
    content_shared = len(left_content_tokens & right_content_tokens)
    content_overlap_floor = min(len(left_content_tokens), len(right_content_tokens)) or 1
    left_tokens = title_tokens(left_title)
    right_tokens = title_tokens(right_title)
    overlap_floor = min(len(left_tokens), len(right_tokens)) or 1
    if left_normalized and right_normalized and left_normalized == right_normalized:
        return True
    if similarity >= 0.8:
        return True
    if meaningful_phrases >= 2 and meaningful_shared >= 3:
        return True
    if meaningful_shared >= 4 and similarity >= 0.5 and meaningful_shared / overlap_floor >= 0.55:
        return True
    if content_similarity >= 0.52 and content_shared >= 8:
        return True
    if similarity >= 0.28 and meaningful_phrases >= 1 and content_similarity >= 0.42:
        return True
    if similarity >= 0.28 and meaningful_shared >= 5 and content_shared / content_overlap_floor >= 0.4:
        return True
    if left_normalized and right_normalized and min(len(left_normalized), len(right_normalized)) >= 12 and (left_normalized in right_normalized or right_normalized in left_normalized):
        return True
    return False


def canonical_lead(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(
        evidence,
        key=lambda row: (
            is_analytic_fragment(str(row['title'])),
            is_generic_title(str(row['title'])),
            int(row['rank_order']),
            -len(title_tokens(str(row['title']))),
            -len(str(row['title'])),
        ),
    )[0]


def collapse_source_evidence(evidence: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        by_source[str(row['source_name'])].append(row)
    collapsed: list[dict[str, Any]] = []
    for source_name, rows in by_source.items():
        _ = source_name
        best = sorted(
            rows,
            key=lambda row: (
                -title_similarity(str(row['title']), title),
                is_analytic_fragment(str(row['title'])),
                is_generic_title(str(row['title'])),
                int(row['rank_order']),
                -len(str(row.get('snippet', ''))),
                -len(str(row['title'])),
            ),
        )[0]
        collapsed.append(best)
    collapsed.sort(key=lambda row: (int(row['rank_order']), is_analytic_fragment(str(row['title'])), is_generic_title(str(row['title'])), -len(str(row['title']))))
    return collapsed


def choose_thumbnail(evidence: list[dict[str, Any]]) -> str:
    for row in evidence:
        thumbnail = str(row.get('thumbnail_url', '')).strip()
        if is_usable_thumbnail_url(thumbnail):
            return thumbnail
    return ''


def keyword_hits(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    hits = 0
    for keyword in keywords:
        needle = keyword.lower()
        if re.fullmatch(r'[a-z0-9][a-z0-9\s&._+-]*', needle):
            if re.search(rf'(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])', lowered):
                hits += 1
            continue
        if needle in lowered:
            hits += 1
    return hits


def clean_fragment_text(text: str) -> str:
    value = re.sub(r'\s+', ' ', text).strip(' \t\r\n-:：;；,.，。!！?？')
    value = re.sub(r'^[•·●]+\s*', '', value)
    value = value.lstrip('”')
    value = re.sub(r'\s+\d+\s*(分钟|小时|天)前(?:\s+\S+){0,2}$', '', value)
    value = re.sub(r'\s+\d+\s*分钟阅读.*$', '', value)
    value = re.sub(r'阅读\s+[A-Za-z0-9._-]+(?:\s+上)?(?:的)?全文', '', value, flags=re.IGNORECASE)
    value = re.sub(r'\s+\d+\s*minute\s+read.*$', '', value, flags=re.IGNORECASE)
    value = re.sub(r'read\s+[^.]{0,40}full\s+article', '', value, flags=re.IGNORECASE)
    value = re.sub(r'我们所知道的$', '', value)
    value = re.sub(r'\s+', ' ', value).strip(' \t\r\n-:：;；,.，。!！?？')
    return value


def finalize_sentence(text: str) -> str:
    value = clean_fragment_text(text)
    if not value:
        return ''
    return normalize_body_punctuation(value)


def summary_content_length(text: str) -> int:
    return len(re.sub(r'[\s。！？!?；;，,“”"\'（）()《》<>·:：-]+', '', text))


def split_into_fragments(text: str) -> list[str]:
    cleaned = clean_fragment_text(text)
    if not cleaned:
        return []
    parts = re.split(r'[。！？!?；;]+|(?<=[.])\s+(?=[A-Z0-9"“\'])', cleaned)
    return [part for part in (clean_fragment_text(piece) for piece in parts) if len(part) >= 8]


def text_tokens(text: str) -> set[str]:
    normalized = clean_title_text(text)
    if not normalized:
        return set()
    tokens: set[str] = set()
    for chunk in re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]+', normalized):
        if re.fullmatch(r'[\u4e00-\u9fff]+', chunk):
            if len(chunk) <= 2:
                tokens.add(chunk)
            else:
                tokens.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
        elif len(chunk) > 1:
            tokens.add(chunk)
    return tokens


def is_low_value_fragment(text: str) -> bool:
    lowered = text.lower()
    if len(text) < 10:
        return True
    if re.fullmatch(r'[\s"\'“”‘’()（）\[\]{}:：,，.\-–—]*\d{1,2}\s*:\s*\d{2}(?:\s*\([^)]*\))?[\s"\'“”‘’()（）\[\]{}:：,，.\-–—]*', text):
        return True
    if re.fullmatch(r'[\s$€£¥%+\-–—.,:;!?/&()\[\]{}0-9]+', text):
        return True
    if len(text) < 24 and any(text.count(left) > text.count(right) for left, right in [('“', '”'), ('（', '）'), ('(', ')')]):
        return True
    if lowered.startswith(('视频', 'live', 'watch')):
        return True
    if '从视频中剪辑而来' in text:
        return True
    if '全文' in text and ('阅读' in text or 'read' in lowered):
        return True
    return False


def is_analytic_fragment(text: str) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in ANALYTIC_FRAGMENT_MARKERS):
        return True
    return False


def is_distinct_fragment(candidate: str, selected: list[str]) -> bool:
    candidate_tokens = text_tokens(candidate)
    if not candidate_tokens:
        return False
    for existing in selected:
        existing_tokens = text_tokens(existing)
        overlap = len(candidate_tokens & existing_tokens)
        floor = min(len(candidate_tokens), len(existing_tokens)) or 1
        if overlap / floor >= 0.72:
            return False
    return True


def collect_summary_fragments(evidence: list[dict[str, Any]], title: str) -> list[str]:
    _ = title
    candidates: list[str] = []
    for row in evidence:
        snippet = str(row.get('snippet', ''))
        if snippet:
            candidates.extend(split_into_fragments(snippet))
    selected: list[str] = []
    for fragment in candidates:
        if is_low_value_fragment(fragment):
            continue
        if is_analytic_fragment(fragment):
            continue
        if not is_distinct_fragment(fragment, selected):
            continue
        selected.append(fragment)
        if len(selected) >= SUMMARY_SENTENCE_LIMIT:
            break
    return selected


def build_story_summary(evidence: list[dict[str, Any]], title: str) -> str:
    fragments = collect_summary_fragments(evidence, title)
    sentences: list[str] = []
    for fragment in fragments[:SUMMARY_SENTENCE_LIMIT]:
        sentence = finalize_sentence(fragment)
        if sentence and sentence not in sentences:
            sentences.append(sentence)
    if len(sentences) < MIN_SUMMARY_SENTENCE_COUNT or summary_content_length(' '.join(sentences)) < MIN_SUMMARY_CONTENT_LENGTH:
        seen_fragments = [clean_fragment_text(sentence.rstrip('。！？!?')) for sentence in sentences if sentence]
        for row in evidence:
            for candidate_text in [
                str(row.get('snippet', '')),
            ]:
                for fragment in split_into_fragments(candidate_text):
                    if is_low_value_fragment(fragment):
                        continue
                    if is_analytic_fragment(fragment):
                        continue
                    if not is_distinct_fragment(fragment, seen_fragments):
                        continue
                    sentence = finalize_sentence(fragment)
                    if not sentence or sentence in sentences:
                        continue
                    sentences.append(sentence)
                    seen_fragments.append(fragment)
                    if len(sentences) >= SUMMARY_SENTENCE_LIMIT:
                        break
                    if len(sentences) >= MIN_SUMMARY_SENTENCE_COUNT and summary_content_length(' '.join(sentences)) >= MIN_SUMMARY_CONTENT_LENGTH:
                        return normalize_body_punctuation(' '.join(sentences))
                if len(sentences) >= SUMMARY_SENTENCE_LIMIT:
                    break
            if len(sentences) >= SUMMARY_SENTENCE_LIMIT:
                break
    summary = normalize_body_punctuation(' '.join(sentences))
    if len(sentences) >= MIN_SUMMARY_SENTENCE_COUNT and summary_content_length(summary) >= MIN_SUMMARY_CONTENT_LENGTH:
        return summary
    if summary and not is_low_value_fragment(clean_fragment_text(summary)):
        return summary
    title_fallback = finalize_sentence(clean_fragment_text(title))
    if len(evidence) <= 1 and title_fallback and not is_low_value_fragment(clean_fragment_text(title_fallback)):
        return title_fallback
    source_names = '、'.join(dict.fromkeys(str(row['source_name']) for row in evidence[:3]))
    fallback_detail = f'目前公开信息主要来自{source_names}等报道，更多细节仍在持续披露中' if source_names else '目前公开信息仍较为有限，更多细节仍在持续披露中'
    fallback_sentence = finalize_sentence(fallback_detail)
    if fallback_sentence and fallback_sentence not in sentences:
        sentences.append(fallback_sentence)
    return normalize_body_punctuation(' '.join(sentences))


def observation_watchpoints(title: str, snippet: str, section: str) -> str:
    combined = f'{title} {snippet}'.lower()
    if '霍尔木兹' in title or '海峡' in title:
        return '接下来重点看是否真的形成联合护航、伊朗是否继续压缩通行空间，以及油价和航运保险成本会不会同步抬升。'
    if '学校' in title and ('爆炸' in title or '袭击' in title):
        return '接下来重点看当地警方调查结果、社区安保是否升级，以及事件会不会继续放大族群对立和社会安全焦虑。'
    if '大使馆' in title or '使馆' in title:
        return '接下来重点看驻外机构安保是否升级、涉事国家会不会公开回应，以及这类袭击是否引发更直接的外交和军事报复。'
    if keyword_hits(combined, CONFLICT_KEYWORDS):
        return '接下来重点看冲突范围是否继续外溢、能源与航运通道是否受阻，以及主要国家会不会转向停火谈判或追加军事部署。'
    if keyword_hits(combined, FINANCE_KEYWORDS):
        return '接下来重点看油价、运价、汇率和政策口径会不会同步变化，判断它是短期波动还是会传导到更广泛的产业链与市场预期。'
    if keyword_hits(combined, TECH_KEYWORDS):
        return '接下来重点看是否出现监管表态、企业订单、资本投入或供应链调整，判断这条线索会不会从概念讨论转成产业动作。'
    if keyword_hits(combined, POLICY_KEYWORDS) or section == '国内':
        return '接下来重点看配套文件、地方落实和执行反馈，因为真正影响往往不是来自表态本身，而是随后落地的节奏与力度。'
    return '接下来重点看是否出现连续更新、官方回应和次生影响，只有这些后续信号出现，事件才会从单点新闻转成持续议题。'


def build_observation_analysis(title: str, evidence: list[dict[str, Any]], section: str) -> str:
    combined = f'{title} {evidence[0].get("snippet", "")}'.lower()
    topic = clean_fragment_text(title)
    if keyword_hits(combined, CONFLICT_KEYWORDS):
        return finalize_sentence(f'{topic}背后反映的不是单一事件，而是安全局势和外部回应正在继续相互推高')
    if keyword_hits(combined, FINANCE_KEYWORDS):
        return finalize_sentence(f'{topic}说明市场和政策预期已经开始围绕同一条主线重新定价')
    if keyword_hits(combined, TECH_KEYWORDS):
        return finalize_sentence(f'{topic}显示相关议题正从新闻热度走向产业和监管层面的持续博弈')
    if keyword_hits(combined, POLICY_KEYWORDS) or section == '国内':
        return finalize_sentence(f'{topic}值得继续跟踪，因为真正影响往往出现在后续执行和地方落地阶段')
    return finalize_sentence(f'{topic}值得继续跟踪，因为它正在从单点消息演变成更长链条的持续议题')


def build_observation_blocks(title: str, evidence: list[dict[str, Any]], section: str) -> list[dict[str, str]]:
    source_count = len({str(row['source_name']) for row in evidence})
    story_summary = build_story_summary(evidence, title)
    lead_snippet = str(evidence[0].get('snippet') or title)
    blocks = [
        {
            'title': '重复率信号',
            'text': finalize_sentence(f'这条线索在本轮被 {source_count} 个来源反复提及，已经从单点消息变成需要持续跟踪的主题'),
        },
        {
            'title': '核心脉络',
            'text': finalize_sentence(story_summary or title),
        },
        {
            'title': '后续关注',
            'text': finalize_sentence(observation_watchpoints(title, lead_snippet, section)),
        },
    ]
    return blocks


def choose_section(evidence: list[dict[str, Any]], title: str, snippet: str) -> str:
    category_votes = [str(row['category']) for row in evidence]
    counts = Counter(category_votes)
    urls = ' '.join(str(row.get('url', '')) for row in evidence)
    combined = f'{title} {snippet} {urls}'
    tech_text_hits = keyword_hits(f'{title} {snippet}', TECH_KEYWORDS)
    tech_url_hits = keyword_hits(urls, TECH_URL_MARKERS)
    finance_text_hits = keyword_hits(f'{title} {snippet}', FINANCE_KEYWORDS)
    finance_url_hits = keyword_hits(urls, FINANCE_URL_MARKERS)
    tech_hits = tech_text_hits + tech_url_hits
    finance_hits = finance_text_hits + finance_url_hits
    domestic_hits = keyword_hits(f'{title} {snippet}', DOMESTIC_KEYWORDS)
    international_hits = keyword_hits(f'{title} {snippet}', INTERNATIONAL_KEYWORDS)
    source_names = {str(row['source_name']) for row in evidence}
    domestic_source_only = bool(source_names) and source_names.issubset(DOMESTIC_SOURCE_NAMES)
    if tech_url_hits > 0 or (tech_text_hits >= 2 and international_hits == 0):
        return '科技'
    if finance_url_hits > 0 or (finance_text_hits >= 2 and international_hits <= 1):
        return '金融'
    if international_hits > domestic_hits:
        return '国际'
    if domestic_source_only and domestic_hits > 0 and international_hits == 0:
        return '国内'
    if counts.get('国际', 0) > 0 and not domestic_source_only:
        return '国际'
    if domestic_source_only and international_hits == 0:
        return '国内'
    if domestic_hits > international_hits and counts.get('国内', 0) > counts.get('国际', 0):
        return '国内'
    if counts.get('国际', 0) > 0 or international_hits >= domestic_hits:
        return '国际'
    return '观察补充'


def impact_analysis(title: str, snippet: str, source_count: int, section: str) -> str:
    combined = f'{title} {snippet}'.lower()
    parts = [f'重复率信号：该主题被 {source_count} 个来源同时提及，说明其已进入本轮信息流核心位置。']
    if keyword_hits(combined, CONFLICT_KEYWORDS):
        parts.append('可能影响：短期内更容易传导到能源、航运、避险情绪和跨境市场波动，后续需继续跟踪局势是否升级或出现停火窗口。')
    elif keyword_hits(combined, FINANCE_KEYWORDS):
        parts.append('可能影响：后续更可能影响市场预期、产业链成本与政策节奏，值得连续观察价格、贸易和监管信号是否共振。')
    elif keyword_hits(combined, TECH_KEYWORDS):
        parts.append('可能影响：后续通常会传导到产业投资、供应链布局和监管讨论，重点看是否出现配套政策、订单或资本加码。')
    elif keyword_hits(combined, POLICY_KEYWORDS) or section == '国内':
        parts.append('可能影响：这类政策与治理信号往往会在未来一段时间持续释放执行细节，建议继续观察地方落实、配套文件和市场反馈。')
    else:
        parts.append('可能影响：如果后续仍被多来源重复报道，通常意味着事件正在从单点新闻演变为持续议题，需要观察是否出现政策、市场或社会层面的连锁反应。')
    return normalize_body_punctuation(' '.join(parts))


def build_summary(evidence: list[dict[str, Any]], title: str, section: str) -> str:
    _ = section
    return build_story_summary(evidence, title)


def source_diversity_bonus(evidence: list[dict[str, Any]]) -> float:
    unique_sources = {str(row['source_name']) for row in evidence}
    categories = {str(row['category']) for row in evidence}
    return len(unique_sources) * 8 + len(categories) * 1.2


def prominence_bonus(evidence: list[dict[str, Any]]) -> float:
    return sum(max(0.2, 2.4 - min(12, int(row['rank_order'])) * 0.18) for row in evidence[:5])


def generic_title_penalty(title: str) -> float:
    return 4.0 if is_generic_title(title) else 0.0


def aggregate_candidates(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        source = result['source']
        for index, item in enumerate(result['items']):
            row = {
                'source_id': source['id'],
                'source_name': source['name'],
                'category': source['category'],
                'title': item['title'],
                'url': item['url'],
                'snippet': item.get('snippet', ''),
                'thumbnail_url': item.get('thumbnail_url', ''),
                'published_at': item.get('published_at', ''),
                'rank_order': index,
                'original_title': item.get('original_title', ''),
                'original_snippet': item.get('original_snippet', ''),
            }
            grouped[story_key(row)].append(row)
    merged_group_keys: list[tuple[str, list[dict[str, Any]]]] = []
    for key, evidence in grouped.items():
        merged = False
        for index, (existing_key, existing_evidence) in enumerate(merged_group_keys):
            if should_merge_groups(evidence, existing_evidence):
                merged_group_keys[index] = (existing_key, existing_evidence + evidence)
                merged = True
                break
        if not merged:
            merged_group_keys.append((key, evidence))

    groups: list[dict[str, Any]] = []
    for key, evidence in merged_group_keys:
        evidence = dedupe_evidence(evidence)
        lead = canonical_lead(evidence)
        title = str(lead['title'])
        evidence = collapse_source_evidence(evidence, title)
        lead = canonical_lead(evidence)
        title = str(lead['title'])
        snippet = str(lead.get('snippet', ''))
        category = choose_section(evidence, title, snippet)
        unique_sources = {str(row['source_name']) for row in evidence}
        score = source_diversity_bonus(evidence) + prominence_bonus(evidence) + max(1.0, len(title) / 48) - generic_title_penalty(title)
        summary_text = build_summary(evidence, title, category)
        analysis_text = build_observation_analysis(title, evidence, category)
        thumbnail_url = choose_thumbnail(evidence)
        groups.append(
            {
                'canonical_key': key,
                'category': category,
                'title': title,
                'score': round(score, 2),
                'source_count': len(unique_sources),
                'summary_text': summary_text,
                'analysis_text': analysis_text,
                'thumbnail_url': thumbnail_url,
                'evidence': evidence,
            }
        )
    groups.sort(key=lambda row: (-float(row['score']), -int(row['source_count']), str(row['title'])))
    return groups


def make_observation_items(groups: list[dict[str, Any]], used_keys: set[str]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    ranked_groups = sorted(groups, key=lambda group: (-int(group['source_count']), -float(group['score']), str(group['title'])))
    for group in ranked_groups:
        if group['canonical_key'] in used_keys:
            continue
        observations.append(
            {
                'title': f"观察：{group['title']}",
                'summary_text': group['analysis_text'],
                'score': group['score'],
                'source_count': group['source_count'],
                'evidence': group['evidence'],
                'thumbnail_url': group.get('thumbnail_url', ''),
                'analysis_blocks': build_observation_blocks(group['title'], group['evidence'], str(group['category'])),
            }
        )
        if len(observations) >= OBSERVATION_LIMIT:
            break
    return observations


def make_risk_items(groups: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    candidates = [
        group
        for group in groups
        if keyword_hits(f"{group['title']} {group['summary_text']}", RISK_KEYWORDS) > 0
    ]
    candidates.sort(
        key=lambda group: (
            -keyword_hits(f"{group['title']} {group['summary_text']}", RISK_KEYWORDS),
            -int(group['source_count']),
            -float(group['score']),
            str(group['title']),
        )
    )
    return candidates[:limit]


def select_today_top(groups: list[dict[str, Any]], limit: int = TODAY_TOP_LIMIT) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    for category in ['国内', '国际', '科技', '金融']:
        preferred = sorted(
            [group for group in groups if group['category'] == category],
            key=lambda item: (-int(item['source_count']), -float(item['score']), str(item['title'])),
        )
        for group in preferred:
            if group['category'] == category and group['canonical_key'] not in selected_keys:
                selected.append(group)
                selected_keys.add(group['canonical_key'])
                break
    for group in groups:
        if len(selected) >= limit:
            break
        if group['canonical_key'] in selected_keys:
            continue
        selected.append(group)
        selected_keys.add(group['canonical_key'])
    selected.sort(key=lambda item: (-int(item['source_count']), -float(item['score']), str(item['title'])))
    return selected[:limit]


def sectioned_groups(groups: list[dict[str, Any]], update_items: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    _ = update_items
    bucket: dict[str, list[dict[str, Any]]] = {key: [] for key in SECTION_ORDER}
    for group in groups:
        bucket.setdefault(str(group['category']), []).append(group)
    sections: list[dict[str, Any]] = []
    used_keys: set[str] = set()
    top = select_today_top(groups, TODAY_TOP_LIMIT)
    if top:
        sections.append({'title': '今日要点', 'items': top})
        used_keys.update(str(item['canonical_key']) for item in top)
    for category in ['国内', '国际', '科技', '金融']:
        items = bucket.get(category, [])[:SECTION_ITEM_LIMIT]
        if items:
            sections.append({'title': category, 'items': items})
            used_keys.update(str(item['canonical_key']) for item in items)
    observation_items = make_observation_items(groups, used_keys)
    if observation_items:
        sections.append({'title': '观察补充', 'items': observation_items})
    return sections


def render_text_digest(sections: list[dict[str, Any]], web_url: str = '') -> str:
    lines: list[str] = []
    if web_url:
        lines.append(f'前往Web阅读：{web_url}')
        lines.append('')
    for section_index, section in enumerate(sections, start=1):
        lines.append(f'{section_index}) {section["title"]}')
        for index, item in enumerate(section['items'], start=1):
            unique_evidence = list(dict.fromkeys((str(e['source_name']), str(e['url'])) for e in item['evidence']))
            sources = '、'.join(source_name for source_name, _ in unique_evidence[:4])
            lines.append(f'{index}. {item["title"]}')
            lines.append(f'来源：{sources} · 重复命中：{item["source_count"]}')
            lines.append('链接：')
            for source_name, url in unique_evidence[:4]:
                lines.append(f'- {source_name}: {url}')
            label = '观察' if section['title'] == '观察补充' else '摘要'
            lines.append(f'{label}：{item["summary_text"]}')
            if section['title'] == '观察补充':
                for block in item.get('analysis_blocks', []):
                    lines.append(f'{block["title"]}：{block["text"]}')
        lines.append('')
    return '\n'.join(lines).strip() + '\n'
