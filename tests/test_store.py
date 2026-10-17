from datetime import datetime, timezone

from news_admin.models import Article, ArticleAnalysis, Event, EventSummary
from news_admin.store import Store


def analysis(fp='美联储|降息|利率|美国'):
    return ArticleAnalysis(title_zh='美联储降息', region='international', topic='economy', topic_confidence=0.9, is_opinion=False,
                           entities=['美联储'], key_facts=['降息25个基点'], event_fingerprint=fp, impact=4, impact_reason='r')


def make_event(id='e1', urls=('https://x/1',)):
    arts = [Article(id=f'a{i}', source=f's{i}', url=u, title='Fed', body='b', published_at=datetime(2026, 10, 17, tzinfo=timezone.utc)) for i, u in enumerate(urls)]
    return Event(id=id, articles=arts, analyses={a.id: analysis() for a in arts},
                 summary=EventSummary.model_validate({'headline': '美联储降息', 'sentences': [{'text': '美联储降息25个基点。', 'sources': [1]}]}), score=9.5)


def test_sources_seed_and_toggle(tmp_path):
    store = Store(tmp_path / 'n.db')
    store.seed_sources([{'name': 'A', 'feed_url': 'https://a/rss', 'weight': 1.0}])
    store.seed_sources([{'name': 'A', 'feed_url': 'https://a/rss', 'weight': 1.0}])
    sources = store.list_sources()
    assert len(sources) == 1
    store.set_source_enabled(sources[0].id, False)
    assert store.list_sources(enabled_only=True) == []


def test_run_and_events_round_trip(tmp_path):
    store = Store(tmp_path / 'n.db')
    run_id = store.start_run('manual')
    store.save_events(run_id, [make_event()])
    store.finish_run(run_id, 'completed', {'events': 1})
    loaded = store.load_events(run_id)
    assert loaded[0].summary.headline == '美联储降息'
    assert loaded[0].lead_analysis.event_fingerprint == '美联储|降息|利率|美国'
    assert store.latest_completed_run()['id'] == run_id


def test_previously_published_urls_lookup(tmp_path):
    store = Store(tmp_path / 'n.db')
    run_id = store.start_run('manual')
    store.save_events(run_id, [make_event(urls=('https://x/1', 'https://x/2'))])
    store.finish_run(run_id, 'completed', {})
    seen = store.recent_event_index(hours=48)
    assert seen['https://x/1'] == seen['https://x/2']
    assert seen['https://x/1']['sources'] == {'s0', 's1'}


def test_llm_calls_are_recorded_and_aggregated(tmp_path):
    from news_admin.llm import LLMCall
    store = Store(tmp_path / 'n.db')
    run_id = store.start_run('manual')
    store.record_llm_calls(run_id, [LLMCall('analyze', 'm', 'v', 'h', attempts=1, prompt_tokens=100, completion_tokens=20, elapsed_ms=900),
                                    LLMCall('analyze', 'm', 'v', 'h2', cached=True)])
    stats = store.llm_stats(run_id)
    assert stats['analyze'] == {'calls': 2, 'cached': 1, 'prompt_tokens': 100, 'completion_tokens': 20, 'retries': 0, 'errors': 0}


def test_source_kind_and_link_pattern_persist_and_old_db_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / 'old.db'
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, feed_url TEXT NOT NULL UNIQUE, weight REAL NOT NULL DEFAULT 1.0, max_items INTEGER NOT NULL DEFAULT 20, fetch_body INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1, last_ok_at TEXT, last_error TEXT NOT NULL DEFAULT '', consecutive_failures INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO sources(name, feed_url) VALUES ('A', 'https://a/rss')")
    conn.commit(); conn.close()
    store = Store(path)
    assert store.list_sources()[0].kind == 'rss'
    store.seed_sources([{'name': '早报', 'feed_url': 'https://z/world', 'kind': 'html', 'link_pattern': r'/story\d+'}])
    zb = [s for s in store.list_sources() if s.name == '早报'][0]
    assert (zb.kind, zb.link_pattern) == ('html', r'/story\d+')
    store.upsert_source(zb.id, '早报', 'https://z/world', 1.0, 10, True, kind='html', link_pattern=r'/x\d+')
    assert [s for s in store.list_sources() if s.name == '早报'][0].link_pattern == r'/x\d+'
