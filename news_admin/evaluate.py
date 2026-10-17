"""评测：在人工标注数据上衡量分类、聚类和摘要质量，用于对比模型 / Prompt 版本 / 阈值。

标注文件为 JSONL，每行一篇文章：
{"id": "...", "source": "...", "url": "...", "title": "...", "body": "...", "published_at": "ISO 时间",
 "gold_region": "domestic|hk_mo_tw|international|global",
 "gold_topic": "politics|military|economy|business|technology|society|health|environment|culture|other",
 "gold_event": "人工给的事件编号，同一事件相同"}

用 `python -m news_admin export-eval <run_id>` 导出某次运行的文章作为待标注模板（gold_* 留空）。
"""

from __future__ import annotations

import json
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

from .analyze import analyze_articles, make_judge
from .cluster import cluster_articles
from .pipeline import analysis_embedding_text
from .models import Article
from .summarize import summarize_event, validate_summary


def _pairs(groups: list[list[str]]) -> set[frozenset[str]]:
    return {frozenset(pair) for group in groups for pair in combinations(group, 2)}


def cluster_prf(gold: dict[str, str], predicted: list[list[str]]) -> tuple[float, float, float]:
    """成对 Precision/Recall/F1：两篇文章被放在同一组，是否与人工标注一致。"""
    gold_groups: dict[str, list[str]] = {}
    for article_id, event in gold.items():
        gold_groups.setdefault(event, []).append(article_id)
    gold_pairs = _pairs(list(gold_groups.values()))
    pred_pairs = _pairs(predicted)
    if not pred_pairs:
        return (1.0, 0.0 if gold_pairs else 1.0, 0.0 if gold_pairs else 1.0)
    hit = len(gold_pairs & pred_pairs)
    precision = hit / len(pred_pairs)
    recall = hit / len(gold_pairs) if gold_pairs else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def label_accuracy(gold: dict[str, str], predicted: dict[str, str]) -> dict[str, Any]:
    ids = [i for i in gold if i in predicted]
    correct = sum(gold[i] == predicted[i] for i in ids)
    confusions = Counter(f'{gold[i]}→{predicted[i]}' for i in ids if gold[i] != predicted[i])
    return {'n': len(ids), 'accuracy': correct / len(ids) if ids else 0.0, 'confusions': dict(confusions.most_common(10))}


def load_labeled(path: str | Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]
    return [row for row in rows if row.get('gold_event')]


def export_for_labeling(runtime, run_id: int, path: str | Path) -> int:
    rows = []
    for event in runtime.store.load_events(run_id):
        for article in event.articles:
            analysis = event.analyses.get(article.id)
            rows.append({
                **article.model_dump(mode='json', include={'id', 'source', 'url', 'title', 'body', 'published_at'}),
                'gold_region': '', 'gold_topic': '', 'gold_event': '',
                'model_region': analysis.region.value if analysis else '',
                'model_topic': analysis.topic.value if analysis else '',
                'model_event': event.id,
            })
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf-8')
    return len(rows)


def run_eval(runtime, data_path: str | Path, summarize_limit: int = 15) -> dict[str, Any]:
    rows = load_labeled(data_path)
    if not rows:
        raise SystemExit(f'{data_path} 中没有已标注（gold_event 非空）的样本')
    articles = [Article.model_validate({k: v for k, v in r.items() if k in Article.model_fields}) for r in rows]
    calls_before = len(runtime.llm.calls)

    analyses = analyze_articles(runtime.llm, articles, runtime.settings.min_topic_confidence)
    region = label_accuracy({r['id']: r['gold_region'] for r in rows if r.get('gold_region')}, {k: v.region.value for k, v in analyses.items()})
    topic = label_accuracy({r['id']: r['gold_topic'] for r in rows if r.get('gold_topic')}, {k: v.topic.value for k, v in analyses.items()})

    config = runtime.pipeline.deps.cluster_config
    groups = cluster_articles(
        articles, runtime.embedder, make_judge(runtime.llm, analyses), config,
        text_of=lambda a: analysis_embedding_text(a, analyses.get(a.id)),
    )
    precision, recall, f1 = cluster_prf({r['id']: r['gold_event'] for r in rows}, [[a.id for a in g] for g in groups])

    multi = [g for g in groups if len(g) > 1][:summarize_limit] or groups[:summarize_limit]
    summary_stats = Counter()
    for group in multi:
        summary, degraded = summarize_event(runtime.llm, group, group[0].title, analyses.get(group[0].id))
        summary_stats['total'] += 1
        summary_stats['degraded'] += int(degraded)
        summary_stats['valid'] += int(not validate_summary(summary, group))

    calls = runtime.llm.calls[calls_before:]
    return {
        'model': runtime.settings.llm_model,
        'embedding': getattr(runtime.embedder, 'model', ''),
        'thresholds': {'high': config.high, 'low': config.low} if config else {},
        'samples': len(rows),
        'region': region,
        'topic': topic,
        'cluster': {'precision': round(precision, 3), 'recall': round(recall, 3), 'f1': round(f1, 3), 'predicted_groups': len(groups),
                    'gold_events': len({r['gold_event'] for r in rows})},
        'summary': {'total': summary_stats['total'], 'passed_fact_check': summary_stats['valid'], 'degraded': summary_stats['degraded']},
        'llm': {
            'calls': len(calls), 'cached': sum(c.cached for c in calls),
            'prompt_tokens': sum(c.prompt_tokens for c in calls), 'completion_tokens': sum(c.completion_tokens for c in calls),
        },
    }
