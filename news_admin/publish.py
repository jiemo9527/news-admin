from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from .aggregator import build_story_summary, is_low_value_fragment, title_similarity
from .config import Paths, TIMEZONE
from .text_normalization import (
    TranslationCache,
    count_cjk,
    force_visible_chinese_text,
    is_low_information_title_text,
    is_low_value_translation_result,
    normalize_body_punctuation,
    summarize_title_from_text,
)
from .thumbnail_cache import is_usable_thumbnail_url, thumbnail_proxy_src

SOURCE_VIEW_ORDER = ['路透社', 'BBC', '纽约时报', 'CNN', '央视新闻', '新华社', '人民网', 'RT', '联合早报']
PUBLICATION_TRANSLATION_CACHE = TranslationCache()
FALLBACK_SUMMARY_MARKER = '目前公开信息主要来自'


def publication_safe_text(text: object, fallback: object = '') -> str:
    return force_visible_chinese_text(str(text or ''), PUBLICATION_TRANSLATION_CACHE, str(fallback or ''))


def publication_safe_title(text: object, fallback: object = '') -> str:
    return force_visible_chinese_text(str(text or ''), PUBLICATION_TRANSLATION_CACHE, str(fallback or ''), aggressive_strip=False)


def is_low_information_title(text: str) -> bool:
    return is_low_information_title_text(text)


def title_fallback_from_texts(*values: object) -> str:
    for value in values:
        candidate = summarize_title_from_text(publication_safe_text(value, ''))
        if candidate and not is_low_information_title(candidate):
            return candidate
    return ''


def choose_best_publication_title(item_title: object, evidence: list[dict[str, Any]]) -> str:
    candidates: list[str] = []

    def push(value: str) -> None:
        normalized = value.strip()
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    push(publication_safe_title(item_title, item_title) or str(item_title or ''))
    for row in evidence:
        push(publication_safe_title(row.get('title', ''), row.get('title', '')))

    def score(value: str) -> tuple[int, int, int]:
        return (
            0 if is_low_information_title(value) else 1,
            count_cjk(value),
            len(value),
        )

    best = max(candidates, key=score, default='')
    if best and not is_low_information_title(best):
        return best
    snippet_candidate = title_fallback_from_texts(*(row.get('snippet', '') for row in evidence))
    if snippet_candidate:
        return snippet_candidate
    return best or str(item_title or '').strip()


def publication_safe_summary(summary_text: object, safe_title: str, evidence: list[dict[str, Any]]) -> str:
    rebuilt = normalize_body_punctuation(publication_safe_text(build_story_summary(evidence, safe_title), safe_title))
    if rebuilt and FALLBACK_SUMMARY_MARKER not in rebuilt and not is_low_value_fragment(rebuilt):
        return rebuilt
    safe_summary = normalize_body_punctuation(publication_safe_text(summary_text, safe_title))
    if safe_summary and FALLBACK_SUMMARY_MARKER not in safe_summary and not is_low_value_fragment(safe_summary):
        return safe_summary
    for row in evidence:
        candidate = normalize_body_punctuation(publication_safe_text(row.get('snippet', ''), safe_title))
        if candidate and FALLBACK_SUMMARY_MARKER not in candidate and not is_low_value_fragment(candidate):
            return candidate
    return safe_title


def best_source_item_title(item: dict[str, Any]) -> str:
    current_title = str(item.get('title', '') or '')
    original_title = str(item.get('original_title', '') or '')
    current_snippet = str(item.get('snippet', '') or '')
    original_snippet = str(item.get('original_snippet', '') or '')
    safe_current = publication_safe_title(current_title, original_title or current_title)
    if safe_current and not is_low_information_title(safe_current):
        return safe_current
    safe_original = publication_safe_title(original_title, current_title or original_title)
    if safe_original and not is_low_information_title(safe_original):
        return safe_original
    snippet_candidate = title_fallback_from_texts(current_snippet, original_snippet)
    if snippet_candidate:
        return snippet_candidate
    return safe_original or safe_current or snippet_candidate or current_title or original_title


def best_source_item_snippet(item: dict[str, Any], safe_title: str) -> str:
    current_snippet = str(item.get('snippet', '') or '')
    original_snippet = str(item.get('original_snippet', '') or '')
    safe_current = normalize_body_punctuation(publication_safe_text(current_snippet, safe_title))
    if safe_current and FALLBACK_SUMMARY_MARKER not in safe_current and not is_low_value_translation_result(safe_current) and not is_low_value_fragment(safe_current):
        return safe_current
    safe_original = normalize_body_punctuation(publication_safe_text(original_snippet, safe_title))
    if safe_original and FALLBACK_SUMMARY_MARKER not in safe_original and not is_low_value_translation_result(safe_original) and not is_low_value_fragment(safe_original):
        return safe_original
    return safe_current or safe_original or ''


def filter_relevant_evidence(evidence: list[dict[str, Any]], safe_title: str) -> list[dict[str, Any]]:
    if len(evidence) <= 1:
        return evidence
    scored = [
        (
            row,
            title_similarity(str(row.get('title', '')), safe_title),
            title_similarity(str(row.get('snippet', '')), safe_title),
        )
        for row in evidence
    ]
    best_title_similarity = max(score[1] for score in scored)
    filtered = [
        row for row, row_title_similarity, row_snippet_similarity in scored
        if row_title_similarity >= 0.34
        or row_snippet_similarity >= 0.24
        or row_title_similarity >= max(0.18, best_title_similarity - 0.18)
    ]
    return filtered or [max(scored, key=lambda score: (score[1], score[2]))[0]]


def build_source_item_lookup(candidates: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        source_name = str(candidate.get('source', {}).get('name', ''))
        for item in candidate.get('items', []):
            url = str(item.get('url', '') or '').strip()
            if not url:
                continue
            lookup[url] = {
                'source_name': source_name,
                'title': str(item.get('title', '') or ''),
                'original_title': str(item.get('original_title', '') or ''),
                'snippet': str(item.get('snippet', '') or ''),
                'original_snippet': str(item.get('original_snippet', '') or ''),
            }
    return lookup


def slugify_fragment(value: str) -> str:
    return ''.join(char if char.isalnum() else '-' for char in value).strip('-') or 'section'


def section_anchor_id(view_key: str, index: int, title: str) -> str:
    return f'{view_key}-section-{index}-{slugify_fragment(title)}'


def render_section_nav(sections: list[dict[str, Any]], view_key: str, hidden: bool = False) -> str:
    tags = ''.join(
        f"<a class='publication-tag' data-section-link href='#{escape(section_anchor_id(view_key, index, str(section['title'])), quote=True)}'>{escape(str(section['title']))}</a>"
        for index, section in enumerate(sections, start=1)
    )
    hidden_attr = ' hidden' if hidden else ''
    return f"<nav class='publication-tags' data-view='{escape(view_key, quote=True)}' aria-label='栏目导航'{hidden_attr}>{tags}</nav>"


def render_view_switch() -> str:
    return "<button class='publication-switch' type='button' data-current-view='category' aria-pressed='false'>切换到按来源分</button>"


def render_source_search() -> str:
    return (
        "<div class='publication-search' data-view='source' hidden>"
        "<label class='publication-search-label' for='source-search-input'>关键词搜索</label>"
        "<input id='source-search-input' class='publication-search-input' type='search' placeholder='按来源分中搜索标题、摘要、来源' autocomplete='off' />"
        "<button class='publication-search-clear' type='button'>清空</button>"
        "</div>"
        "<p class='publication-search-empty' data-view='source' hidden>没有匹配当前关键词的来源文章。</p>"
    )


def build_source_sections(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in SOURCE_VIEW_ORDER}
    fallback_sources: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        source = candidate['source']
        source_name = str(source['name'])
        bucket = grouped if source_name in grouped else fallback_sources
        bucket.setdefault(source_name, [])
        for item in candidate['items']:
            provisional_title = best_source_item_title(item)
            safe_snippet = best_source_item_snippet(item, provisional_title)
            evidence = [
                {
                    'source_name': source_name,
                    'url': item['url'],
                    'title': provisional_title,
                    'snippet': safe_snippet,
                    'published_at': item.get('published_at', ''),
                    'rank_order': 0,
                }
            ]
            safe_title = choose_best_publication_title(provisional_title, evidence)
            evidence[0]['title'] = safe_title
            safe_summary = publication_safe_summary(build_story_summary(evidence, safe_title), safe_snippet or safe_title, evidence)
            bucket[source_name].append(
                {
                    'title': safe_title,
                    'summary_text': safe_summary,
                    'thumbnail_url': item.get('thumbnail_url', ''),
                    'analysis_blocks': [],
                    'source_count': 1,
                    'evidence': evidence,
                }
            )
    source_sections: list[dict[str, Any]] = []
    for source_name in SOURCE_VIEW_ORDER + sorted(name for name in fallback_sources if fallback_sources[name]):
        items = grouped.get(source_name, []) or fallback_sources.get(source_name, [])
        if not items:
            continue
        source_sections.append({'title': source_name, 'items': items})
    return source_sections


def unique_evidence_rows(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in evidence:
        key = (str(row['source_name']), str(row['url']))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def render_thumbnail(item: dict[str, Any], section_title: str) -> str:
    thumbnail_url = str(item.get('thumbnail_url', '')).strip()
    if is_usable_thumbnail_url(thumbnail_url):
        proxied = thumbnail_proxy_src(thumbnail_url)
        return f"<div class='item-thumb'><img src='{escape(proxied, quote=True)}' alt='{escape(str(item['title']), quote=True)}' loading='lazy' onerror=\"this.parentNode.classList.add('item-thumb-fallback');this.remove();\" /><span>{escape(section_title)}</span></div>"
    return f"<div class='item-thumb item-thumb-fallback'><span>{escape(section_title)}</span></div>"


def format_card_datetime(value: object) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        current = datetime.fromisoformat(text).astimezone(ZoneInfo(TIMEZONE))
        if not any([current.hour, current.minute, current.second]):
            return ''
        if current.second == 0:
            return current.strftime('%Y-%m-%d %H:%M')
        return current.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def latest_card_datetime(evidence: list[dict[str, Any]]) -> str:
    latest: datetime | None = None
    for row in evidence:
        text = str(row.get('published_at', '') or '').strip()
        if not text:
            continue
        try:
            current = datetime.fromisoformat(text).astimezone(ZoneInfo(TIMEZONE))
        except Exception:
            continue
        if not any([current.hour, current.minute, current.second]):
            continue
        if latest is None or current > latest:
            latest = current
    if latest is None:
        return ''
    if latest.second == 0:
        return latest.strftime('%Y-%m-%d %H:%M')
    return latest.strftime('%Y-%m-%d %H:%M:%S')


def render_section(section: dict[str, Any], section_index: int, view_key: str, preferred_source: str = '', source_lookup: dict[str, dict[str, str]] | None = None) -> str:
    cards: list[str] = []
    for index, item in enumerate(section['items'], start=1):
        raw_evidence = unique_evidence_rows(item['evidence'])
        evidence = []
        for row in raw_evidence:
            source_item = (source_lookup or {}).get(str(row.get('url', '') or '').strip(), {})
            source_title = source_item.get('original_title') or source_item.get('title') or row.get('title', '')
            source_snippet = source_item.get('original_snippet') or source_item.get('snippet') or row.get('snippet', '')
            safe_row_title = publication_safe_title(source_title, row.get('title', '')) or str(row.get('title', ''))
            safe_row_snippet = normalize_body_punctuation(publication_safe_text(source_snippet, safe_row_title))
            evidence.append({**row, 'title': safe_row_title, 'snippet': safe_row_snippet})
        grouped_title = str(item.get('title', '') or '').strip()
        if view_key == 'source' or not grouped_title or is_low_information_title(grouped_title):
            safe_title = choose_best_publication_title(item.get('title', ''), evidence)
        else:
            safe_title = grouped_title
        if view_key == 'source':
            evidence = filter_relevant_evidence(evidence, safe_title)
        grouped_summary = str(item.get('summary_text', '') or '').strip()
        if view_key == 'source' or not grouped_summary or FALLBACK_SUMMARY_MARKER in grouped_summary or is_low_value_fragment(grouped_summary):
            safe_summary = publication_safe_summary(item.get('summary_text', ''), safe_title, evidence) or safe_title
        else:
            safe_summary = grouped_summary
        primary = next((row for row in evidence if preferred_source and str(row['source_name']) == preferred_source), evidence[0])
        published_at = latest_card_datetime(evidence) or format_card_datetime(primary.get('published_at', ''))
        source_links = ' · '.join(
            f"<a href='{escape(row['url'], quote=True)}' target='_blank' rel='noopener noreferrer'>{escape(row['source_name'])}</a>"
            for row in evidence[:5]
        )
        analysis_blocks = ''.join(
            f"<p><strong>{escape(str(block['title']))}：</strong>{escape(normalize_body_punctuation(publication_safe_text(block.get('text', ''), safe_summary or safe_title)))}</p>"
            for block in item.get('analysis_blocks', [])
        )
        thumbnail = render_thumbnail(item, str(section['title']))
        search_text = ' '.join(
            [
                safe_title,
                safe_summary,
                ' '.join(str(row.get('source_name', '')) for row in evidence),
            ]
        )
        meta_parts = [f"发布时间{escape(published_at)}（北京时间）"] if published_at else []
        if source_links:
            meta_parts.append(f"来源链接：{source_links}")
        meta_line = ' · '.join(meta_parts)
        cards.append(
            f"<article class='item-card' data-search-text='{escape(search_text, quote=True)}'><div class='item-num'>{index}</div>{thumbnail}<div class='item-main'><h3><a href='{escape(primary['url'], quote=True)}' target='_blank' rel='noopener noreferrer'>{escape(safe_title)}</a></h3><p>{escape(safe_summary)}</p>{analysis_blocks}<p class='meta'>{meta_line}</p></div></article>"
        )
    header = f"<div class='section-head'><h2>{escape(section['title'])}</h2><span class='section-count'>{len(section['items'])} 条</span></div>"
    anchor_id = section_anchor_id(view_key, section_index, str(section['title']))
    return f"<details class='section section-collapsible' id='{escape(anchor_id, quote=True)}' data-section-anchor open><summary class='section-summary'>{header}</summary><div class='item-list'>{''.join(cards)}</div></details>"


def render_section_view(sections: list[dict[str, Any]], view_key: str, hidden: bool = False, source_lookup: dict[str, dict[str, str]] | None = None) -> str:
    blocks = [render_section(section, index, view_key, str(section['title']) if view_key == 'source' else '', source_lookup) for index, section in enumerate(sections, start=1)]
    hidden_attr = ' hidden' if hidden else ''
    extra = render_source_search() if view_key == 'source' else ''
    return f"<div class='publication-view' data-view='{escape(view_key, quote=True)}'{hidden_attr}>{extra}{''.join(blocks)}</div>"


def render_view_toggle_script() -> str:
    return """<script>
(() => {
  const switchButton = document.querySelector('.publication-switch');
  const views = Array.from(document.querySelectorAll('.publication-view'));
  const navs = Array.from(document.querySelectorAll('.publication-tags[data-view]'));
  const searchBars = Array.from(document.querySelectorAll('.publication-search[data-view]'));
  const emptyStates = Array.from(document.querySelectorAll('.publication-search-empty[data-view]'));
  const storageKey = 'news-admin-publication-view';
  const labels = {
    category: '切换到按来源分',
    source: '切换到按类型分'
  };
  const updateActiveSection = (targetView) => {
    const activeView = document.querySelector(`.publication-view[data-view="${targetView}"]`);
    const nav = document.querySelector(`.publication-tags[data-view="${targetView}"]`);
    if (!activeView || !nav) {
      return;
    }
    const sections = Array.from(activeView.querySelectorAll('[data-section-anchor]'));
    const links = Array.from(nav.querySelectorAll('[data-section-link]'));
    links.forEach((link) => link.classList.remove('publication-tag-active'));
    if (!sections.length || !links.length) {
      return;
    }
    const viewportOffset = 160;
    let current = sections[0];
    for (const section of sections) {
      const rect = section.getBoundingClientRect();
      if (rect.top <= viewportOffset) {
        current = section;
      }
    }
    const currentId = current.getAttribute('id');
    const activeLink = currentId ? nav.querySelector(`[href="#${currentId}"]`) : null;
    if (activeLink) {
      activeLink.classList.add('publication-tag-active');
    }
  };
  const setupObserver = (targetView) => {
    const activeView = document.querySelector(`.publication-view[data-view="${targetView}"]`);
    const nav = document.querySelector(`.publication-tags[data-view="${targetView}"]`);
    if (!activeView || !nav || nav.dataset.scrollspyReady === 'true') {
      return;
    }
    nav.dataset.scrollspyReady = 'true';
    const sections = Array.from(activeView.querySelectorAll('[data-section-anchor]'));
    const links = Array.from(nav.querySelectorAll('[data-section-link]'));
    const activateLink = (sectionId) => {
      links.forEach((link) => link.classList.toggle('publication-tag-active', link.getAttribute('href') === `#${sectionId}`));
    };
    if ('IntersectionObserver' in window) {
      const observer = new IntersectionObserver((entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (visible && visible.target && visible.target.id) {
          activateLink(visible.target.id);
        }
      }, { rootMargin: '-20% 0px -55% 0px', threshold: [0.2, 0.4, 0.6] });
      sections.forEach((section) => observer.observe(section));
    }
    links.forEach((link) => {
      link.addEventListener('click', () => {
        const target = link.getAttribute('href');
        if (!target) {
          return;
        }
        const section = document.querySelector(target);
        if (!section) {
          return;
        }
        activateLink(section.id);
      });
    });
    window.addEventListener('scroll', () => updateActiveSection(targetView), { passive: true });
    updateActiveSection(targetView);
  };
  const applySourceFilter = () => {
    const input = document.querySelector('.publication-search-input');
    const sourceView = document.querySelector('.publication-view[data-view="source"]');
    const emptyState = document.querySelector('.publication-search-empty[data-view="source"]');
    if (!input || !sourceView || !emptyState) {
      return;
    }
    const query = input.value.trim().toLowerCase();
    const sections = Array.from(sourceView.querySelectorAll('[data-section-anchor]'));
    let visibleCards = 0;
    sections.forEach((section) => {
      const cards = Array.from(section.querySelectorAll('.item-card'));
      let sectionVisible = 0;
      cards.forEach((card) => {
        const haystack = (card.dataset.searchText || '').toLowerCase();
        const matched = !query || haystack.includes(query);
        card.hidden = !matched;
        if (matched) {
          sectionVisible += 1;
          visibleCards += 1;
        }
      });
      section.hidden = sectionVisible === 0;
    });
    emptyState.hidden = visibleCards > 0;
    updateActiveSection('source');
  };
  const activate = (targetView) => {
    if (switchButton) {
      switchButton.dataset.currentView = targetView;
      switchButton.textContent = labels[targetView] || labels.category;
    }
    for (const view of views) {
      view.hidden = view.dataset.view !== targetView;
    }
    for (const nav of navs) {
      nav.hidden = nav.dataset.view !== targetView;
    }
    for (const searchBar of searchBars) {
      searchBar.hidden = searchBar.dataset.view !== targetView;
    }
    for (const emptyState of emptyStates) {
      emptyState.hidden = emptyState.dataset.view !== targetView || !emptyState.textContent;
    }
    if (targetView === 'source') {
      applySourceFilter();
    } else {
      const emptyState = document.querySelector('.publication-search-empty[data-view="source"]');
      if (emptyState) {
        emptyState.hidden = true;
      }
    }
    setupObserver(targetView);
    updateActiveSection(targetView);
    try {
      window.localStorage.setItem(storageKey, targetView);
    } catch (error) {
      void error;
    }
  };
  const searchInput = document.querySelector('.publication-search-input');
  const clearButton = document.querySelector('.publication-search-clear');
  if (searchInput) {
    searchInput.addEventListener('input', applySourceFilter);
  }
  if (clearButton && searchInput) {
    clearButton.addEventListener('click', () => {
      searchInput.value = '';
      applySourceFilter();
      searchInput.focus();
    });
  }
  if (switchButton) {
    switchButton.addEventListener('click', () => {
      const currentView = switchButton.dataset.currentView === 'source' ? 'source' : 'category';
      activate(currentView === 'category' ? 'source' : 'category');
    });
  }
  let initialView = 'category';
  try {
    const savedView = window.localStorage.getItem(storageKey);
    if (savedView === 'source' || savedView === 'category') {
      initialView = savedView;
    }
  } catch (error) {
    void error;
  }
  activate(initialView);
})();
</script>"""


def render_publication_html(title: str, sections: list[dict[str, Any]], publication_path: str, source_candidates: list[dict[str, Any]] | None = None) -> str:
    now = datetime.now(timezone.utc).astimezone(ZoneInfo(TIMEZONE))
    updated = now.strftime('%Y-%m-%d %H:%M:%S')
    published_item_total = sum(len(section['items']) for section in sections)
    classified_item_total = sum(len(section['items']) for section in sections if str(section['title']) in {'国内', '国际', '科技', '金融'})
    source_sections = build_source_sections(source_candidates or [])
    source_lookup = build_source_item_lookup(source_candidates or [])
    source_item_total = sum(len(section['items']) for section in source_sections)
    switcher = render_view_switch()
    category_nav = render_section_nav(sections, 'category')
    source_nav = render_section_nav(source_sections, 'source', hidden=True)
    category_view = render_section_view(sections, 'category', source_lookup=source_lookup)
    source_view = render_section_view(source_sections, 'source', hidden=True, source_lookup=source_lookup)
    toggle_script = render_view_toggle_script()
    return f"""<!doctype html>
<html lang='zh-CN'>
<head>
  <meta charset='utf-8' />
  <meta name='viewport' content='width=device-width, initial-scale=1' />
  <title>{escape(title)}</title>
  <link rel='stylesheet' href='/static/style.css' />
</head>
<body class='publication-body'>
  <main class='wrap'>
    <section class='hero'>
      <div class='eyebrow'>新闻聚合发布页</div>
      <h1>{escape(title)}</h1>
      <div class='hero-meta'>
        <span>更新时间：{escape(updated)}（上海时间）</span>
        <span>页面路径：{escape(publication_path)}</span>
        <span>分类条数：{classified_item_total} / 所有条数：{source_item_total}</span>
      </div>
    </section>
    <section class='publication-nav-shell'>
      <div class='publication-nav-inner'>
        {switcher}
        {category_nav}
        {source_nav}
      </div>
    </section>
    <section class='grid'>
      <div class='content'>
        {category_view}
        {source_view}
        <div class='publication-tail-space' aria-hidden='true'></div>
      </div>
    </section>
    <footer class='panel'>
      <p>聚合作者信息 <a href='https://t.me/jiemo123' target='_blank' rel='noopener noreferrer'>https://t.me/jiemo123</a></p>
    </footer>
    {toggle_script}
  </main>
</body>
</html>"""


def generate_publication_token() -> str:
    return secrets.token_urlsafe(9).replace('-', 'x').replace('_', 'y')


def publication_relpath(token: str) -> str:
    return f'publications/{token}.html'


def publication_path(token: str) -> str:
    return f'/published/{token}'


def save_publication(run_id: int, html: str, token: str) -> tuple[str, str]:
    _ = run_id
    relpath = f'publications/{token}.html'
    latest_relpath = 'publications/latest.html'
    file_path = Paths.data_dir / relpath
    file_path.parent.mkdir(parents=True, exist_ok=True)
    _ = file_path.write_text(html, encoding='utf-8')
    latest_path = Paths.data_dir / latest_relpath
    _ = latest_path.write_text(html, encoding='utf-8')
    return relpath, latest_relpath
