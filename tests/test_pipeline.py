from datetime import datetime, timezone

from news_admin.fetch import FetchResult, Source
from news_admin.models import Article, ArticleAnalysis, EventSummary
from news_admin.pipeline import Pipeline, PipelineDeps
from news_admin.store import Store

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
FED_BODY = '美联储周三宣布将基准利率下调25个基点，至4.00%至4.25%区间，这是自去年12月以来的首次降息。'


def article(id, source, url, title, body):
    return Article(id=id, source=source, url=url, title=title, body=body, published_at=NOW)


ARTICLES = {
    'A': [article('1', 'A', 'https://a/1', '美联储降息25个基点', FED_BODY)],
    'B': [
        article('2', 'B', 'https://b/1', '美联储宣布降息', FED_BODY.replace('周三', '17日') + '鲍威尔称后续取决于数据。'),
        article('3', 'B', 'https://b/2', '日本发生6级地震', '日本东北部发生6.1级地震，暂无海啸预警。'),
    ],
    'C': [article('4', 'C', 'https://c/1', 'Fed cuts rates', 'The Fed cut rates by 25 basis points to 4.00%-4.25%.')],
}

VECTORS = {'美联储降息25个基点': [1, 0], '美联储宣布降息': [1, 0], 'Fed cuts rates': [0.8, 0.6], '日本发生6级地震': [0, 1]}


def analysis(article):
    quake = '地震' in article.title
    return ArticleAnalysis(
        title_zh=article.title, region='international', topic='environment' if quake else 'economy', topic_confidence=0.9,
        content_type='news', has_new_development=True, entities=[], key_facts=['f'], event_fingerprint='x', impact=3 if quake else 5, impact_reason='r',
    )


def summarize(articles, title, analysis=None):
    return EventSummary.model_validate({'headline': title, 'sentences': [{'text': articles[0].body[:40], 'sources': [1]}]}), False


def make_pipeline(tmp_path, judge_answer=True, fetched=None):
    store = Store(tmp_path / 'n.db')
    store.seed_sources([{'name': n, 'feed_url': f'https://{n}/rss'} for n in 'ABC'])
    fetched = fetched or ARTICLES
    deps = PipelineDeps(
        fetch=lambda source: FetchResult(source=source, articles=fetched.get(source.name, [])),
        analyze=lambda arts: {a.id: analysis(a) for a in arts},
        embed=lambda texts: [VECTORS[t.split('\n')[0]] for t in texts],
        judge_factory=lambda analyses: (lambda x, y: judge_answer),
        summarize=summarize,
        llm_calls=lambda: [],
        now=lambda: NOW,
    )
    return store, Pipeline(store, deps)


def test_pipeline_dedupes_clusters_ranks_and_persists(tmp_path):
    store, pipeline = make_pipeline(tmp_path)
    run_id = pipeline.run('manual')
    run = store.get_run(run_id)
    assert run['status'] == 'completed'
    assert run['stats']['articles_fetched'] == 4
    assert run['stats']['articles_after_dedupe'] == 3
    events = store.load_events(run_id)
    assert [e.summary.headline for e in events] == ['美联储宣布降息', '日本发生6级地震']
    fed = events[0]
    assert set(fed.sources) == {'A', 'B', 'C'}
    assert fed.score > events[1].score


def test_gray_zone_rejection_keeps_events_separate(tmp_path):
    store, pipeline = make_pipeline(tmp_path, judge_answer=False)
    events = store.load_events(pipeline.run('manual'))
    assert len(events) == 3


def test_second_run_drops_unchanged_events_and_marks_updates(tmp_path):
    store, pipeline = make_pipeline(tmp_path)
    pipeline.run('manual')
    second = dict(ARTICLES)
    second['D'] = [article('5', 'D', 'https://d/1', '美联储宣布降息', '美联储降息后，美股三大指数收涨。')]
    store.seed_sources([{'name': 'D', 'feed_url': 'https://D/rss'}])
    VECTORS['美联储宣布降息'] = [1, 0]
    pipeline.deps.fetch = lambda source: FetchResult(source=source, articles=second.get(source.name, []))
    run_id = pipeline.run('manual')
    events = store.load_events(run_id)
    assert len(events) == 1
    assert events[0].is_update is True
    assert store.get_run(run_id)['stats']['events_unchanged'] == 1


def test_failing_source_is_recorded_and_run_still_completes(tmp_path):
    store, pipeline = make_pipeline(tmp_path)
    original = pipeline.deps.fetch
    pipeline.deps.fetch = lambda s: FetchResult(source=s, error='boom') if s.name == 'C' else original(s)
    run_id = pipeline.run('manual')
    assert store.get_run(run_id)['status'] == 'completed'
    health = {s.name: store.source_health()[s.id] for s in store.list_sources()}
    assert health['C']['consecutive_failures'] == 1 and health['C']['last_error'] == 'boom'


def test_clustering_embeds_normalized_analysis_text_not_raw_text(tmp_path):
    from news_admin.pipeline import analysis_embedding_text

    a = ARTICLES['C'][0]
    text = analysis_embedding_text(a, analysis(a))
    assert text.startswith('Fed cuts rates') and 'x' in text
    assert analysis_embedding_text(a, None).startswith('Fed cuts rates\nThe Fed')


def test_renamed_source_with_same_urls_is_not_an_update(tmp_path):
    store, pipeline = make_pipeline(tmp_path)
    pipeline.run('manual')
    renamed = {'A2': [a.model_copy(update={'source': 'A2'}) for a in ARTICLES['A']], 'B': ARTICLES['B'], 'C': ARTICLES['C']}
    store.seed_sources([{'name': 'A2', 'feed_url': 'https://A2/rss'}])
    pipeline.deps.fetch = lambda source: FetchResult(source=source, articles=renamed.get(source.name, []))
    run_id = pipeline.run('manual')
    assert store.load_events(run_id) == []
    assert store.get_run(run_id)['stats']['events_unchanged'] == 2


def test_low_value_articles_are_filtered_with_reason(tmp_path):
    from news_admin.models import ArticleAnalysis
    fetched = {'A': [article('9', 'A', 'https://a/9', '长江名称历经演变', '长江古称江，历代名称变化。')], **{k: v for k, v in ARTICLES.items() if k != 'A'}}
    store, pipeline = make_pipeline(tmp_path, fetched=fetched)
    VECTORS['长江名称历经演变'] = [0.5, 0.5]
    original = pipeline.deps.analyze

    def analyze(arts):
        result = original([a for a in arts if a.id != '9'])
        result['9'] = ArticleAnalysis(title_zh='长江名称历经演变', region='domestic', topic='culture', topic_confidence=0.9,
                                      content_type='feature', has_new_development=False, entities=['长江'], key_facts=['长江古称江'],
                                      event_fingerprint='长江|名称演变|-|中国', impact=1, impact_reason='历史科普')
        return result

    pipeline.deps.analyze = analyze
    run_id = pipeline.run('manual')
    stats = store.get_run(run_id)['stats']
    assert stats['articles_filtered'] == 1
    assert stats['filtered'][0]['title'] == '长江名称历经演变' and '无新进展' in stats['filtered'][0]['reason']
    assert all('长江' not in e.summary.headline for e in store.load_events(run_id))


def test_republished_url_is_unchanged_even_after_a_follow_up_in_the_same_story(tmp_path):
    """同一故事线出现后续报道后，旧报道链接再次被抓到，不能当成后续报道的「更新」重新入库。"""
    from news_admin.models import Relation
    store, pipeline = make_pipeline(tmp_path)
    pipeline.run('manual')
    follow = {'D': [article('7', 'D', 'https://d/7', '美股因降息大涨', '美联储降息后美股三大指数收涨2%。')]}
    store.seed_sources([{'name': 'D', 'feed_url': 'https://D/rss'}])
    VECTORS['美股因降息大涨'] = [0.8, 0.6]
    pipeline.deps.fetch = lambda source: FetchResult(source=source, articles=follow.get(source.name, []))
    pipeline.deps.continuity_judge = lambda new, old: Relation.follow_up
    pipeline.run('manual')
    again = {'C': ARTICLES['C']}  # 第一次已发布过的 https://c/1
    pipeline.deps.fetch = lambda source: FetchResult(source=source, articles=again.get(source.name, []))
    run_id = pipeline.run('manual')
    assert store.load_events(run_id, brief_only=False) == []
    assert store.get_run(run_id)['stats']['events_unchanged'] == 1
    assert len(store.feed().events) == 3


def test_follow_up_event_references_previous_story(tmp_path):
    from news_admin.models import Relation
    store, pipeline = make_pipeline(tmp_path)
    first_run = pipeline.run('manual')
    first_fed = store.load_events(first_run)[0]
    follow = {'D': [article('7', 'D', 'https://d/7', '美股因降息大涨', '美联储降息后美股三大指数收涨2%。')]}
    store.seed_sources([{'name': 'D', 'feed_url': 'https://D/rss'}])
    VECTORS['美股因降息大涨'] = [0.8, 0.6]
    asked = []

    def judge(new, old):
        asked.append((new.lead.title, old.lead.title))
        return Relation.follow_up if '美联储' in old.lead.title else Relation.unrelated

    pipeline.deps.fetch = lambda source: FetchResult(source=source, articles=follow.get(source.name, []))
    pipeline.deps.continuity_judge = judge
    run_id = pipeline.run('manual')
    event = store.load_events(run_id)[0]
    assert event.is_update is False
    assert event.previous[0].relation == 'follow_up'
    assert event.previous[0].run_id == first_run and event.previous[0].event_id == first_fed.id
    assert event.story_id == first_fed.story_id
    assert store.get_run(run_id)['stats']['events_follow_up'] == 1


def test_thumbnail_uses_first_article_with_usable_image(tmp_path):
    imgs = {'A': [article('1', 'A', 'https://a/1', '美联储降息25个基点', FED_BODY).model_copy(update={'image_url': 'https://x/logo.png'})],
            'B': [article('2', 'B', 'https://b/1', '美联储宣布降息', FED_BODY + '更多').model_copy(update={'image_url': 'https://x/photo.jpg'})]}
    store, pipeline = make_pipeline(tmp_path, fetched=imgs)
    pipeline.deps.thumbnail = lambda url: 'k' * 32 if url == 'https://x/photo.jpg' else ''
    events = store.load_events(pipeline.run('manual'))
    assert events[0].thumbnail == 'k' * 32



def test_all_events_are_stored_but_only_top_n_enter_the_brief(tmp_path):
    store, pipeline = make_pipeline(tmp_path)
    calls = []
    original = pipeline.deps.summarize

    def counting(articles, title, analysis=None):
        calls.append(title)
        return original(articles, title, analysis)

    pipeline.deps.summarize = counting
    pipeline.deps.max_events = lambda: 1
    run_id = pipeline.run('manual')
    everything = store.load_events(run_id, brief_only=False)
    brief = store.load_events(run_id)
    assert [e.in_brief for e in everything] == [True, False]
    assert [e.summary.headline for e in brief] == ['美联储宣布降息']
    assert calls == ['美联储宣布降息']                     # 简报外的事件不调用模型写摘要
    quake = everything[1]
    assert quake.summary.sentences and quake.summary_degraded is False   # 用中文关键事实作摘要
    stats = store.get_run(run_id)['stats']
    assert stats['events_stored'] == 2 and stats['events_published'] == 1
    assert store.brief_index()[0]['events'] == 1
    assert len(store.feed().events) == 2                       # 首页汇总包含全部入库事件
