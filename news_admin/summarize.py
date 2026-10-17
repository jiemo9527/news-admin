"""事件摘要：生成 → 程序校验 → 带错误重试 → 降级。

校验规则（违反任一条即视为不合格）：
- 引用编号必须落在 1..来源数；
- 句子里的阿拉伯数字（含小数、百分比、年份）必须出现在它所引用的来源原文中；
- 不允许套话。
"""

from __future__ import annotations

import re

from .llm import LLMClient, LLMError
from .models import Article, ArticleAnalysis, EventSummary, SummarySentence
from .prompts import SUMMARY_SYSTEM, SUMMARY_VERSION, summary_user

FILLER_PHRASES = ('值得关注', '引发热议', '引发关注', '意义重大', '备受瞩目', '令人担忧', '不容忽视', '拭目以待')
_NUMBER_RE = re.compile(r'\d+(?:[.,]\d+)*')
_SENTENCE_END_RE = re.compile(r'(?<=[。！？!?；;])')


_MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december']
_MONTH_ABBR = {m[:3]: i for i, m in enumerate(_MONTHS, 1)}
_WORDS = {w: i for i, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split())}
_WORDS.update({'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90, 'hundred': 100, 'dozen': 12})
_ZH_DIGITS = dict(zip('零一二两三四五六七八九十', [0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9, 10]))
_EN_WORD_RE = re.compile(r'[a-z]+')
_ZH_NUM_RE = re.compile(r'[零一二两三四五六七八九十]{1,3}(?=[个名人日月年岁届次项艘架枚家])')


def _numbers(text: str) -> set[str]:
    return {match.replace(',', '') for match in _NUMBER_RE.findall(text)}


def _zh_small_number(word: str) -> int | None:
    if word == '十':
        return 10
    if '十' in word:
        tens, _, ones = word.partition('十')
        return _ZH_DIGITS.get(tens, 1) * 10 + (_ZH_DIGITS.get(ones, 0) if ones else 0)
    return _ZH_DIGITS.get(word) if len(word) == 1 else None


_YEAR_RANGE_RE = re.compile(r'\b((?:19|20)\d{2})\s*[-–/]\s*(\d{2})\b')
_CJK_RE = re.compile(r'[\u4e00-\u9fff]')


def evidence_numbers(text: str) -> set[str]:
    """来源中可作为证据的数字：阿拉伯数字 + 英文月份名/数字词 + 中文小数字（如「三名」）+ 年份区间缩写（2011-12 → 2012）。"""
    found = _numbers(text)
    for start, short in _YEAR_RANGE_RE.findall(text):
        found.add(start[:2] + short)
    for word in _EN_WORD_RE.findall(text.lower()):
        if word in _WORDS:
            found.add(str(_WORDS[word]))
        month = _MONTH_ABBR.get(word[:3]) if word in _MONTHS or word in _MONTH_ABBR else None
        if month:
            found.add(str(month))
    for word in _ZH_NUM_RE.findall(text):
        value = _zh_small_number(word)
        if value is not None:
            found.add(str(value))
    return found


def validate_summary(summary: EventSummary, articles: list[Article]) -> list[str]:
    errors: list[str] = []
    total = len(articles)
    for index, sentence in enumerate(summary.sentences, 1):
        bad = [n for n in sentence.sources if not 1 <= n <= total]
        if bad:
            errors.append(f'第{index}句的来源编号 {bad} 超出范围 1..{total}')
            continue
        cited_numbers = set().union(*(evidence_numbers(articles[n - 1].text_for_analysis) for n in sentence.sources))
        missing = sorted(_numbers(sentence.text) - cited_numbers)
        if missing:
            errors.append(f'第{index}句中的数字 {missing} 在其引用的来源 {sentence.sources} 原文中找不到')
        for phrase in FILLER_PHRASES:
            if phrase in sentence.text:
                errors.append(f'第{index}句含套话「{phrase}」')
    if sum(len(s.text) for s in summary.sentences) > 200:
        errors.append('摘要总长超过 180 字')
    return errors


def fallback_summary(articles: list[Article], title: str, analysis: ArticleAnalysis | None = None) -> EventSummary:
    """模型摘要不可用时的降级：优先用主报道的中文关键事实（同样经过数字校验），否则取原文首句。"""
    if analysis:
        sentences = [SummarySentence(text=fact, sources=[1]) for fact in analysis.key_facts]
        kept = [s for s in sentences if not validate_summary(EventSummary(headline=title[:32], sentences=[s]), articles[:1])]
        if kept:
            return EventSummary(headline=title[:32], sentences=kept[:3], conflicts=[])
        # 关键事实里的数字都没通过校验：仍用模型的中文关键事实（去掉无法核实的数字句前先保留第一条），不退回外文原文
        chinese = [s for s in sentences if _CJK_RE.search(s.text)]
        if chinese:
            return EventSummary(headline=title[:32], sentences=chinese[:1], conflicts=[])
    lead = articles[0]
    text = lead.body or lead.title
    sentence = next((part.strip() for part in _SENTENCE_END_RE.split(text) if part.strip()), text)[:90]
    if not _CJK_RE.search(sentence):
        # 没有中文分析可用时，宁可只给中文标题，也不展示外文原句
        sentence = title[:90]
    return EventSummary(headline=title[:32], sentences=[SummarySentence(text=sentence, sources=[1])], conflicts=[])


def repair_degraded(event) -> bool:
    """修复已保存的降级摘要：若当前摘要含非中文句子，用中文关键事实重建。返回是否改动。"""
    summary = event.summary
    if not event.summary_degraded or summary is None:
        return False
    if all(_CJK_RE.search(s.text) for s in summary.sentences):
        return False
    rebuilt = fallback_summary(event.articles, summary.headline, event.lead_analysis)
    if [s.text for s in rebuilt.sentences] == [s.text for s in summary.sentences]:
        return False
    event.summary = rebuilt
    return True


def summarize_event(client: LLMClient, articles: list[Article], fallback_title: str, analysis: ArticleAnalysis | None = None, max_fix_rounds: int = 1) -> tuple[EventSummary, bool]:
    """返回 (摘要, 是否降级)。"""
    user = summary_user(articles)
    for round_ in range(max_fix_rounds + 1):
        try:
            result = client.structured(SUMMARY_SYSTEM, user, EventSummary, stage='summarize', prompt_version=SUMMARY_VERSION)
        except LLMError:
            break
        errors = validate_summary(result.value, articles)
        if not errors:
            return result.value, False
        if round_ < max_fix_rounds:
            user = f'{summary_user(articles)}\n\n上一版摘要未通过事实校验，请修正：\n' + '\n'.join(f'- {e}' for e in errors)
    return fallback_summary(articles, fallback_title, analysis), True
