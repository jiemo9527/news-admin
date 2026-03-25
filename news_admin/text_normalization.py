from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from typing import Any

from ftfy import fix_text

from .config import Paths

CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
LATIN_RE = re.compile(r'[A-Za-z]')
LATINISH_RE = re.compile(r'[A-Za-zÀ-ÖØ-öø-ÿĀ-ſ]')
MOJIBAKE_RE = re.compile(r'[ÃÂäåçéèæïð]|å.|ä.|æ.|ç.|ï.|涓|鍥|鏂|鏃|鐨|绗|鎴|閲|闂|闆|缇|鈥|銆|锛|锟')
HTML_TAG_RE = re.compile(r'<[^>]+>')
SYNDICATION_PATTERNS = [
    re.compile(r'\bThe post\b.*?\bappeared first on\b.*$', re.IGNORECASE),
    re.compile(r'\bappeared first on\b.*$', re.IGNORECASE),
    re.compile(r'\bRead Full Article at RT\.com\b.*$', re.IGNORECASE),
    re.compile(r'这(?:篇|条)?(?:帖子|文章|内容)?首先出现在.*$', re.IGNORECASE),
    re.compile(r'该(?:帖子|文章|内容)?首先出现在.*$', re.IGNORECASE),
    re.compile(r'消息首先出现在.*$', re.IGNORECASE),
    re.compile(r'一文首先出现在.*$', re.IGNORECASE),
    re.compile(r'首先出现在路透社(?:的)?(?:最佳报道|精选|best).*$', re.IGNORECASE),
    re.compile(r'首先出现在.*?(?:精选|最佳报道|best).*$', re.IGNORECASE),
    re.compile(r'在\s*RT\.com\s*阅读(?:完整)?全文.*$', re.IGNORECASE),
    re.compile(r'阅读\s*RT\.com(?:\s*上)?(?:的)?全文.*$', re.IGNORECASE),
    re.compile(r'阅读\s*RT\.com\s*全文.*$', re.IGNORECASE),
    re.compile(r'\b\d+\s*(?:min|minute|minutes)\s*read\b.*$', re.IGNORECASE),
    re.compile(r'\b\d+\s*分钟阅读\b.*$', re.IGNORECASE),
    re.compile(r'为《纽约时报》撰写的文章.*$', re.IGNORECASE),
    re.compile(r'for\s+the\s+new\s+york\s+times.*$', re.IGNORECASE),
]
ERROR_PLACEHOLDER_PATTERNS = [
    re.compile(r'error\s*\d+\s*\(.*?server error.*?\)', re.IGNORECASE),
    re.compile(r"that['’]s an error", re.IGNORECASE),
    re.compile(r'there was an error', re.IGNORECASE),
    re.compile(r'please try again later', re.IGNORECASE),
    re.compile(r"that['’]s all we know", re.IGNORECASE),
]
BOILERPLATE_PATTERNS = [
    re.compile(r'MDDI\s*\(P\)\s*\d+/\d+/\d+', re.IGNORECASE),
    re.compile(r'新报业媒体有限公司版权所有', re.IGNORECASE),
    re.compile(r'在中国的用户请游览\s*zaobao\.com', re.IGNORECASE),
    re.compile(r'信息网络传播视听节目许可证\s*\d+', re.IGNORECASE),
    re.compile(r'网络文化经营许可证', re.IGNORECASE),
    re.compile(r'网络出版服务许可证', re.IGNORECASE),
    re.compile(r'京ICP证\d+', re.IGNORECASE),
    re.compile(r'京公网安备\d+', re.IGNORECASE),
    re.compile(r'人民日报社概况\s*\|', re.IGNORECASE),
    re.compile(r'违法和不良信息举报', re.IGNORECASE),
    re.compile(r'服务邮箱[:：]', re.IGNORECASE),
    re.compile(r'互联网新闻信息服务许可证\s*\d+', re.IGNORECASE),
    re.compile(r'增值电信业务经营许可证', re.IGNORECASE),
    re.compile(r'广播电视节目制作经营许可证', re.IGNORECASE),
    re.compile(r'京ICP备\d+', re.IGNORECASE),
    re.compile(r'点击播报本文', re.IGNORECASE),
    re.compile(r'分享让更多人看到', re.IGNORECASE),
    re.compile(r'about people\.cn', re.IGNORECASE),
]
ENGLISH_ALIAS_PAREN_RE = re.compile(r'[（(]\s*[A-Za-z][A-Za-z0-9 .,&\-\'"/]{1,120}\s*[)）]')
LATIN_FRAGMENT_RE = re.compile(r'[A-Za-z][A-Za-z0-9 .,&\-\'"/]{2,}')
EMPTY_OUTLET_TITLE_RE = re.compile(r'^(?:据|根据)?《》(?:报道|报道称|消息)?[,:：，\-\s]*')
SHORT_NUMERIC_TITLE_RE = re.compile(r'^[\s$€£¥%+\-–—.,:;!?/&()\[\]{}0-9A-Za-zÀ-ÖØ-öø-ÿĀ-ſ]{1,18}$')
TIME_FRAGMENT_RE = re.compile(r'^[\s"\'“”‘’()（）\[\]{}:：,，.\-–—]*\d{1,2}\s*:\s*\d{2}(?:\s*\([^)]*\))?[\s"\'“”‘’()（）\[\]{}:：,，.\-–—]*$')
TRAILING_ATTRIBUTION_RE = re.compile(r'(?:\s|,|，)*(?:塞玛福|彭博(?:新闻社)?|华尔街日报|纽约时报|路透社|联合早报|美联社|BBC|CNN|RT)?(?:报道|报道称|报告)$', re.IGNORECASE)


def count_cjk(text: str) -> int:
    return len(CJK_RE.findall(text))


def count_mojibake_markers(text: str) -> int:
    return len(MOJIBAKE_RE.findall(text)) + text.count('\ufffd')


def score_decoded_text(text: str) -> int:
    return count_cjk(text) * 4 - count_mojibake_markers(text) * 10 - text.count('\x00') * 20


def decode_response_text(response: Any) -> str:
    header_encoding = (response.encoding or '').strip()
    apparent_encoding = (getattr(response, 'apparent_encoding', None) or '').strip()

    def decode_with(encoding: str) -> str:
        return response.content.decode(encoding, errors='replace')

    if header_encoding and header_encoding.lower() not in {'iso-8859-1', 'latin-1'}:
        try:
            return decode_with(header_encoding)
        except Exception:
            pass
    if apparent_encoding:
        try:
            apparent_text = decode_with(apparent_encoding)
            if count_mojibake_markers(apparent_text) <= max(2, len(apparent_text) // 400):
                return apparent_text
        except Exception:
            pass

    candidates: list[str] = []
    for encoding in [apparent_encoding, 'utf-8', 'gb18030', 'gbk', 'big5']:
        if encoding and encoding.lower() not in {item.lower() for item in candidates}:
            candidates.append(encoding)
    best_text = ''
    best_score = -10**9
    for encoding in candidates:
        try:
            text = response.content.decode(encoding, errors='replace')
        except Exception:
            continue
        score = score_decoded_text(text)
        if header_encoding and encoding.lower() == header_encoding.lower() and header_encoding.lower() not in {'iso-8859-1', 'latin-1'}:
            score += 5
        if score > best_score:
            best_text = text
            best_score = score
    return best_text or response.text


def repair_text(text: str) -> str:
    value = text.strip()
    if not value:
        return value
    fixed = fix_text(value).strip()
    best = fixed if score_decoded_text(fixed) >= score_decoded_text(value) else value
    if count_mojibake_markers(best) == 0:
        return best
    try:
        repaired = best.encode('latin1', errors='ignore').decode('utf-8', errors='ignore').strip()
    except Exception:
        return best
    repaired_fixed = fix_text(repaired).strip()
    return repaired_fixed if score_decoded_text(repaired_fixed) > score_decoded_text(best) else best


def strip_html_text(text: str) -> str:
    value = unescape(text).strip()
    if not value:
        return ''
    value = re.sub(r'(?is)<script.*?>.*?</script>', ' ', value)
    value = re.sub(r'(?is)<style.*?>.*?</style>', ' ', value)
    value = re.sub(r'(?i)<br\s*/?>', ' ', value)
    value = HTML_TAG_RE.sub(' ', value)
    value = re.sub(r'\s+', ' ', value)
    return value.strip()


def strip_syndication_tail(text: str) -> str:
    value = text.strip()
    if not value:
        return ''
    for pattern in SYNDICATION_PATTERNS:
        value = pattern.sub('', value).strip()
    value = re.sub(r'\s+', ' ', value).strip(' -:：;；,.，。!！?？')
    return value


def is_error_placeholder_text(text: str) -> bool:
    normalized = re.sub(r'\s+', ' ', text).strip()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in ERROR_PLACEHOLDER_PATTERNS)


def is_boilerplate_text(text: str) -> bool:
    normalized = re.sub(r'\s+', ' ', text).strip()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in BOILERPLATE_PATTERNS)


def sanitize_news_text(text: str) -> str:
    value = strip_syndication_tail(strip_html_text(repair_text(text.strip())))
    if is_error_placeholder_text(value) or is_boilerplate_text(value):
        return ''
    return value


def normalize_punctuation_spacing(text: str) -> str:
    value = re.sub(r'\s+', ' ', text).strip()
    if not value:
        return ''
    value = re.sub(r'\s*([，。！？；：])\s*', r'\1', value)
    value = re.sub(r'\s*([,;:!?])\s*', r'\1 ', value)
    value = re.sub(r'([。！？!?]){2,}', r'\1', value)
    value = re.sub(r'([，,；;：:]){2,}', r'\1', value)
    value = re.sub(r'\(\s*\)', '', value)
    value = re.sub(r'（\s*）', '', value)
    value = re.sub(r'《\s+', '《', value)
    value = re.sub(r'\s+》', '》', value)
    value = re.sub(r'“\s+', '“', value)
    value = re.sub(r'\s+”', '”', value)
    value = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', value)
    value = re.sub(r'\s+', ' ', value)
    return value.strip(' -:：;；,.，。!！?？')


def normalize_body_punctuation(text: str) -> str:
    value = re.sub(r'\s+', ' ', str(text or '')).strip()
    if not value:
        return ''
    value = re.sub(r'(?<=\d)\s*[，,。．]\s*(?=\d{3}(?:\D|$))', ',', value)
    value = re.sub(r'(?<=\d)\s*[：:]\s*(?=\d)', ':', value)
    value = re.sub(r'\s*([，。！？；：])\s*', r'\1', value)
    value = re.sub(r'\s*([,;!?])\s*', r'\1 ', value)
    value = re.sub(r'(?<=\d),\s+(?=\d{3}(?:\D|$))', ',', value)
    value = re.sub(r'([。！？!?]){2,}', r'\1', value)
    value = re.sub(r'([，,；;：:]){2,}', r'\1', value)
    value = re.sub(r'\(\s*\)', '', value)
    value = re.sub(r'（\s*）', '', value)
    value = re.sub(r'《\s+', '《', value)
    value = re.sub(r'\s+》', '》', value)
    value = re.sub(r'“\s+', '“', value)
    value = re.sub(r'\s+”', '”', value)
    value = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', value)
    value = re.sub(r'([。！？；：])\s+(?=[\u4e00-\u9fffA-Za-z0-9“"（(])', r'\1', value)
    value = re.sub(r'\s+', ' ', value).strip(' -:：;；,.，!！?？')
    if value and value[-1] not in '。！？!?':
        value += '。'
    return value


def has_visible_english_leakage(text: str) -> bool:
    return bool(LATIN_FRAGMENT_RE.search(text))


def strip_residual_english(text: str) -> str:
    value = text.strip()
    if not value:
        return ''
    value = ENGLISH_ALIAS_PAREN_RE.sub('', value)
    value = re.sub(r'(?:(?<=^)|(?<=[。！？!?；;]))\s*[A-Za-z][A-Za-z0-9 .,&\-\'"/]{3,}(?=(?:[。！？!?；;]|$))', ' ', value)
    value = re.sub(r'(?:(?<=^)|(?<=[，,]))\s*[A-Za-z][A-Za-z0-9 .,&\-\'"/]{5,}(?=(?:[，,。！？!?；;]|$))', ' ', value)
    value = re.sub(r'\b[A-Za-z]{2,}(?:[-/][A-Za-z]{2,})*\b', ' ', value)
    value = re.sub(r'\b[A-Za-z][A-Za-z0-9]{1,}\b', ' ', value)
    value = re.sub(r'[A-Za-z][A-Za-z0-9&./\-]{1,}', ' ', value)
    value = re.sub(r'\s+', ' ', value)
    return normalize_punctuation_spacing(value)


def is_low_value_translation_result(text: str) -> bool:
    normalized = normalize_punctuation_spacing(text)
    if not normalized:
        return True
    if TIME_FRAGMENT_RE.fullmatch(normalized):
        return True
    if count_cjk(normalized) >= 4:
        return False
    if re.fullmatch(r"[\s'\"`$€£¥%+\-–—.,:;!?/&()\[\]{}0-9A-Za-zÀ-ÖØ-öø-ÿĀ-ſ]+", normalized):
        return True
    if len(normalized) <= 4:
        return True
    return False


def is_low_information_title_text(text: str) -> bool:
    normalized = normalize_punctuation_spacing(text)
    if not normalized:
        return True
    if EMPTY_OUTLET_TITLE_RE.match(normalized):
        return True
    if TIME_FRAGMENT_RE.fullmatch(normalized):
        return True
    if SHORT_NUMERIC_TITLE_RE.fullmatch(normalized):
        return True
    if count_cjk(normalized) >= 4 and len(normalized) >= 8:
        return False
    if LATINISH_RE.search(normalized) and count_cjk(normalized) < 4:
        return True
    if count_cjk(normalized) <= 1 and len(normalized) <= 16:
        return True
    return False


def summarize_title_from_text(text: str, limit: int = 38) -> str:
    normalized = normalize_punctuation_spacing(text)
    if not normalized:
        return ''
    for part in re.split(r'[。！？!?；;]+', normalized):
        candidate = normalize_punctuation_spacing(part)
        candidate = EMPTY_OUTLET_TITLE_RE.sub('', candidate).strip(' -:：;；,.，。!！?？"\'“”‘’()（）[]{}')
        candidate = TRAILING_ATTRIBUTION_RE.sub('', candidate).strip(' -:：;；,.，。!！?？"\'“”‘’()（）[]{}')
        if not candidate or is_low_information_title_text(candidate):
            continue
        if len(candidate) > limit:
            candidate = candidate[:limit].rstrip(' -:：;；,.，。!！?？')
        if candidate and not is_low_information_title_text(candidate):
            return candidate
    return ''


def contains_chinese(text: str) -> bool:
    return bool(CJK_RE.search(text))


def needs_translation(text: str) -> bool:
    normalized = text.strip()
    return bool(normalized) and not contains_chinese(normalized) and bool(LATIN_RE.search(normalized))


def split_translation_segments(text: str) -> list[str]:
    normalized = re.sub(r'\s+', ' ', text).strip()
    if not normalized:
        return []
    parts = re.split(r'(?<=[。！？!?])\s+|(?<=[.])\s+(?=[A-Z0-9"“\'])', normalized)
    return [part.strip() for part in parts if part.strip()]


def should_translate_segment(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if needs_translation(normalized):
        return True
    latin_count = len(LATIN_RE.findall(normalized))
    cjk_count = count_cjk(normalized)
    return latin_count >= 24 and latin_count > max(8, cjk_count)


def translate_segments(segments: list[str], cache: 'TranslationCache') -> str:
    pending = [segment for segment in segments if should_translate_segment(segment)]
    if not pending:
        return sanitize_news_text(' '.join(segments))
    translated_map = cache.translate_texts(pending)
    rebuilt: list[str] = []
    for segment in segments:
        translated = translated_map.get(segment, segment)
        cleaned = sanitize_news_text(translated) or sanitize_news_text(segment) or segment
        rebuilt.append(cleaned)
    merged = re.sub(r'\s+', ' ', ' '.join(part for part in rebuilt if part)).strip()
    return sanitize_news_text(merged)


def normalize_multilingual_text(text: str, cache: 'TranslationCache') -> str:
    normalized = sanitize_news_text(text)
    if not normalized:
        return ''
    segments = split_translation_segments(normalized)
    if needs_translation(normalized) and len(segments) > 1:
        return translate_segments(segments, cache) or normalized
    if needs_translation(normalized):
        translated = cache.translate_texts([normalized]).get(normalized, normalized)
        cleaned = sanitize_news_text(translated) or normalized
        translated_segments = split_translation_segments(cleaned)
        if len(translated_segments) > 1 and any(should_translate_segment(segment) for segment in translated_segments):
            return translate_segments(translated_segments, cache) or cleaned
        return cleaned
    if len(segments) <= 1:
        if should_translate_segment(normalized):
            translated = cache.translate_texts([normalized]).get(normalized, normalized)
            return sanitize_news_text(translated) or normalized
        return normalized
    return translate_segments(segments, cache) or normalized


def force_visible_chinese_text(text: str, cache: 'TranslationCache', fallback: str = '', aggressive_strip: bool = True) -> str:
    primary = sanitize_news_text(text)
    fallback_text = sanitize_news_text(fallback)
    if not primary:
        primary = fallback_text
    if not primary:
        return ''

    candidates: list[str] = []

    def push(value: str) -> None:
        cleaned = normalize_punctuation_spacing(sanitize_news_text(value))
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    push(primary)
    push(normalize_multilingual_text(primary, cache))
    if has_visible_english_leakage(primary):
        translated = cache.force_translate_texts([primary]).get(primary, primary)
        push(translated)
        push(normalize_multilingual_text(translated, cache))
    if fallback_text:
        push(fallback_text)
        push(normalize_multilingual_text(fallback_text, cache))
        if has_visible_english_leakage(fallback_text):
            translated_fallback = cache.force_translate_texts([fallback_text]).get(fallback_text, fallback_text)
            push(translated_fallback)
            push(normalize_multilingual_text(translated_fallback, cache))

    stripped_candidates: list[str] = []
    for candidate in candidates:
        if not has_visible_english_leakage(candidate) and not is_low_value_translation_result(candidate):
            return candidate
        stripped = strip_residual_english(candidate)
        if stripped and stripped not in stripped_candidates:
            stripped_candidates.append(stripped)

    for stripped in stripped_candidates:
        if not has_visible_english_leakage(stripped) and not is_low_value_translation_result(stripped) and (aggressive_strip or count_cjk(stripped) >= 4):
            return stripped

    if aggressive_strip:
        for stripped in stripped_candidates:
            if stripped and not is_low_value_translation_result(stripped):
                return stripped
    else:
        for stripped in stripped_candidates:
            if stripped and count_cjk(stripped) >= 4 and not is_low_value_translation_result(stripped):
                return stripped
    for candidate in candidates:
        if candidate:
            return candidate
    return ''


class TranslationCache:
    def __init__(self, cache_path: Path | None = None) -> None:
        self.cache_path = cache_path or (Paths.data_dir / 'translation_cache.json')
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, str] = {}
        if self.cache_path.exists():
            try:
                self.data = json.loads(self.cache_path.read_text(encoding='utf-8'))
            except Exception:
                self.data = {}
        self._translator: Any | None = None

    def _get_translator(self) -> Any:
        if self._translator is None:
            from deep_translator import GoogleTranslator

            self._translator = GoogleTranslator(source='auto', target='zh-CN')
        return self._translator

    def save(self) -> None:
        self.cache_path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')

    def _translate_texts(self, texts: list[str], force: bool = False) -> dict[str, str]:
        unique_pending = []
        seen: set[str] = set()
        for text in texts:
            cached = self.data.get(text, '')
            if cached and is_error_placeholder_text(cached):
                self.data.pop(text, None)
                cached = ''
            if cached and is_low_value_translation_result(cached) and bool(LATIN_RE.search(text)):
                self.data.pop(text, None)
                cached = ''
            if cached and cached.strip() == text.strip() and (force or should_translate_segment(text)):
                self.data.pop(text, None)
                cached = ''
            if text in seen or cached or (not force and not should_translate_segment(text)):
                continue
            seen.add(text)
            unique_pending.append(text)
        if unique_pending:
            translator = self._get_translator()
            try:
                translated_batch = translator.translate_batch(unique_pending)
                if len(translated_batch) != len(unique_pending):
                    raise RuntimeError('translate_batch returned unexpected size')
                for source, translated in zip(unique_pending, translated_batch):
                    cleaned = str(translated).strip() or source
                    if is_error_placeholder_text(cleaned) or is_low_value_translation_result(cleaned):
                        cleaned = source
                    self.data[source] = cleaned
            except Exception:
                for source in unique_pending:
                    try:
                        translated = translator.translate(source)
                        cleaned = str(translated).strip() or source
                        if is_error_placeholder_text(cleaned) or is_low_value_translation_result(cleaned):
                            cleaned = source
                        self.data[source] = cleaned
                    except Exception:
                        self.data[source] = source
            self.save()
        return {text: self.data.get(text, text) for text in texts}

    def translate_texts(self, texts: list[str]) -> dict[str, str]:
        return self._translate_texts(texts, force=False)

    def force_translate_texts(self, texts: list[str]) -> dict[str, str]:
        return self._translate_texts(texts, force=True)


def normalize_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    repaired_items: list[dict[str, str]] = []
    title_candidates: list[str] = []
    snippet_candidates: list[str] = []
    for item in items:
        original_title = sanitize_news_text(item.get('title', ''))
        original_snippet = sanitize_news_text(item.get('snippet', ''))
        title_candidates.append(original_title)
        snippet_candidates.append(original_snippet)
        repaired_items.append(
            {
                'title': original_title,
                'snippet': original_snippet,
                'url': item['url'],
                'via': item.get('via', ''),
                'thumbnail_url': item.get('thumbnail_url', '').strip(),
                'published_at': item.get('published_at', '').strip(),
            }
        )
    cache = TranslationCache()
    normalized: list[dict[str, str]] = []
    for item in repaired_items:
        translated_title = force_visible_chinese_text(item['title'], cache, item['title']) or item['title']
        if is_low_information_title_text(translated_title):
            title_from_snippet = summarize_title_from_text(force_visible_chinese_text(item['snippet'], cache, item['title'])) if item['snippet'] else ''
            if title_from_snippet:
                translated_title = title_from_snippet
        translated_snippet = force_visible_chinese_text(item['snippet'], cache, translated_title) if item['snippet'] else ''
        row = dict(item)
        if translated_title != item['title']:
            row['original_title'] = item['title']
            row['title'] = translated_title
        if translated_snippet and translated_snippet != item['snippet']:
            row['original_snippet'] = item['snippet']
            row['snippet'] = translated_snippet
        elif not translated_snippet and is_error_placeholder_text(item['snippet']):
            row['snippet'] = ''
        normalized.append(row)
    return normalized
