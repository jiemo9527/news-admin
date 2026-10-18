"""首页汇总：跨运行去重（同一事件的更新只显示最新版本）、时间范围、关键词、分页、90 天保留。"""
from datetime import datetime, timedelta

from news_admin import tz
from news_admin.models import Article, ArticleAnalysis, Event, EventRef, EventSummary, Topic
from news_admin.store import Store

NOW = datetime(2026, 10, 17, 15, tzinfo=tz.TZ)


def ev(id, headline, when, sources=('BBC',), entities=('美联储',), previous=None, is_update=False, score=8.0, story=''):
    arts = [Article(id=f'{id}{i}', source=s, url=f'https://x/{id}/{i}', title=f'{headline} {s}', published_at=when, time_precision='exact')
            for i, s in enumerate(sources)]
    an = ArticleAnalysis(title_zh=headline, region='international', topic='economy', topic_confidence=0.9, content_type='news',
                         has_new_development=True, entities=list(entities), key_facts=['f'], event_fingerprint='x', impact=3, impact_reason='r')
    return Event(id=id, articles=arts, analyses={a.id: an for a in arts}, score=score, is_update=is_update, story_id=story or id,
                 previous=previous or [], summary=EventSummary.model_validate({'headline': headline, 'sentences': [{'text': headline + '。', 'sources': [1]}]}))


def save(store, events, finished=NOW):
    run_id = store.start_run('test')
    store.save_events(run_id, events)
    store.finish_run(run_id, 'completed', {})
    return run_id


def test_feed_shows_latest_version_of_updated_events_but_keeps_follow_ups(tmp_path):
    store = Store(tmp_path / 'n.db')
    r1 = save(store, [ev('a', '美联储降息25个基点', NOW - timedelta(hours=5)), ev('b', '日本地震', NOW - timedelta(hours=4), entities=('日本',))])
    ref_update = EventRef(run_id=r1, event_id='a', headline='美联储降息25个基点', relation='update')
    ref_follow = EventRef(run_id=r1, event_id='a', headline='美联储降息25个基点', relation='follow_up')
    save(store, [ev('a2', '美联储降息，鲍威尔称将视数据而定', NOW - timedelta(hours=1), sources=('BBC', 'CNN'), previous=[ref_update], is_update=True, story='a'),
                 ev('c', '美股因降息大涨', NOW - timedelta(hours=2), previous=[ref_follow], story='a')])
    page = store.feed(now=NOW)
    assert [e.summary.headline for e in page.events] == ['美联储降息，鲍威尔称将视数据而定', '美股因降息大涨', '日本地震']
    assert page.total == 3


def test_feed_time_ranges_use_beijing_calendar_days(tmp_path):
    store = Store(tmp_path / 'n.db')
    save(store, [
        ev('t', '今天的', datetime(2026, 10, 17, 0, 30, tzinfo=tz.TZ)),
        ev('y', '昨天的', datetime(2026, 10, 16, 23, 50, tzinfo=tz.TZ)),
        ev('d3', '前天的', datetime(2026, 10, 15, 8, tzinfo=tz.TZ)),
        ev('w', '六天前', datetime(2026, 10, 11, 8, tzinfo=tz.TZ)),
        ev('old', '八天前', datetime(2026, 10, 9, 8, tzinfo=tz.TZ)),
    ])
    titles = lambda r: [e.summary.headline for e in store.feed(range_=r, now=NOW).events]
    assert titles('today') == ['今天的']
    assert titles('yesterday') == ['昨天的']
    assert titles('3d') == ['今天的', '昨天的', '前天的']
    assert titles('7d') == ['今天的', '昨天的', '前天的', '六天前']
    assert len(titles('all')) == 5


def test_feed_keyword_search_matches_headline_source_entity_and_requires_all_terms(tmp_path):
    store = Store(tmp_path / 'n.db')
    save(store, [ev('a', '美联储降息25个基点', NOW, sources=('Reuters',)), ev('b', '日本地震', NOW, entities=('日本气象厅',))])
    q = lambda text: [e.id for e in store.feed(q=text, now=NOW).events]
    assert q('降息') == ['a']
    assert q('reuters') == ['a']           # 英文不区分大小写
    assert q('气象厅') == ['b']            # 实体
    assert q('美联储 地震') == []           # 多个词同时满足
    assert q('100%') == []                 # 通配符按字面处理


def test_feed_pagination_and_sort_by_score(tmp_path):
    store = Store(tmp_path / 'n.db')
    save(store, [ev(f'e{i}', f'新闻{i}', NOW - timedelta(minutes=i), score=i) for i in range(25)])
    first = store.feed(page=1, per_page=10, now=NOW)
    third = store.feed(page=3, per_page=10, now=NOW)
    assert first.total == 25 and first.pages == 3 and len(third.events) == 5
    assert first.events[0].id == 'e0'                                  # 默认按时间倒序
    assert store.feed(sort='score', per_page=3, now=NOW).events[0].id == 'e24'


def test_retention_deletes_runs_older_than_90_days(tmp_path):
    store = Store(tmp_path / 'n.db')
    old = save(store, [ev('o', '旧闻', NOW - timedelta(days=100))])
    with store._conn() as conn:
        conn.execute('UPDATE runs SET started_at=? WHERE id=?', (tz.iso(NOW - timedelta(days=91)), old))
    keep = save(store, [ev('n', '新闻', NOW)])
    assert store.cleanup(retention_days=90, now=NOW) == 1
    assert store.get_run(old) is None and store.get_run(keep) is not None
    assert [e.id for e in store.feed(now=NOW).events] == ['n']


def test_existing_rows_are_backfilled_for_the_feed(tmp_path):
    store = Store(tmp_path / 'n.db')
    save(store, [ev('a', '美联储降息', NOW)])
    with store._conn() as conn:
        conn.execute("UPDATE events SET canonical_id='', event_time=NULL, search_text=''")
    assert [e.id for e in Store(tmp_path / 'n.db').feed(q='美联储', now=NOW).events] == ['a']


def test_feed_topic_filter(tmp_path):
    store = Store(tmp_path / 'n.db')
    quake = ev('b', '日本地震', NOW)
    for an in quake.analyses.values():
        an.topic = Topic.society
    save(store, [ev('a', '美联储降息', NOW), quake])
    assert [e.id for e in store.feed(topic='society', now=NOW).events] == ['b']
    assert store.topic_counts() == {'economy': 1, 'society': 1}


def test_events_sharing_an_article_url_collapse_even_without_update_link(tmp_path):
    store = Store(tmp_path / 'n.db')
    save(store, [ev('a', '巴西毁林创新低', NOW - timedelta(hours=6))])
    second = ev('b', '巴西亚马孙毁林面积创38年最低', NOW - timedelta(hours=1), sources=('BBC', '央视'))
    second.articles[0] = second.articles[0].model_copy(update={'url': 'https://x/a/0'})   # 与上一期共享一篇报道
    save(store, [second])
    assert [e.summary.headline for e in store.feed(now=NOW).events] == ['巴西亚马孙毁林面积创38年最低']


def test_recompute_merges_legacy_rows_by_shared_url(tmp_path):
    store = Store(tmp_path / 'n.db')
    save(store, [ev('a', '旧版本', NOW - timedelta(hours=6))])
    b = ev('b', '新版本', NOW - timedelta(hours=1))
    b.articles[0] = b.articles[0].model_copy(update={'url': 'https://x/a/0'})
    save(store, [b])
    with store._conn() as conn:
        conn.execute("UPDATE events SET canonical_id = run_id || ':' || id")
        conn.execute("DELETE FROM settings WHERE key='feed_index_version'")
    assert [e.summary.headline for e in Store(tmp_path / 'n.db').feed(now=NOW).events] == ['新版本']


def test_shared_article_url_wins_over_update_reference_to_a_different_event(tmp_path):
    """同一篇报道只属于一个事件：即使「更新」指向故事线里另一条（后续）事件，也按共享链接合并，首页不出现两条相同新闻。"""
    store = Store(tmp_path / 'n.db')
    first = save(store, [ev('rt', '伊朗称两艘油轮触雷', NOW - timedelta(hours=8))])
    follow_ref = EventRef(run_id=first, event_id='rt', headline='伊朗称两艘油轮触雷', relation='follow_up')
    second = save(store, [ev('cn', '伊朗海军称多艘油轮起火', NOW - timedelta(hours=3), previous=[follow_ref], story='rt')])
    update_ref = EventRef(run_id=second, event_id='cn', headline='伊朗海军称多艘油轮起火', relation='update')
    again = ev('rt', '伊朗称两艘油轮触雷', NOW - timedelta(hours=8), previous=[update_ref, follow_ref], is_update=True, story='rt')
    save(store, [again])
    headlines = [e.summary.headline for e in store.feed(now=NOW).events]
    assert sorted(headlines) == ['伊朗海军称多艘油轮起火', '伊朗称两艘油轮触雷']
    # 线上已有的旧数据：索引规则升级后重算即可合并
    with store._conn() as conn:
        conn.execute("UPDATE events SET canonical_id = ? WHERE run_id=3", (f'{second}:cn',))
        conn.execute("UPDATE settings SET value='2' WHERE key='feed_index_version'")
    assert sorted(e.summary.headline for e in Store(tmp_path / 'n.db').feed(now=NOW).events) == sorted(headlines)


def test_rebuild_carries_push_read_and_favorite_records_to_the_new_key(tmp_path):
    """索引重算换了 canonical_id 时，推送记录、已读和收藏跟着迁移：不重复推送、阅读状态不丢。"""
    store = Store(tmp_path / 'n.db')
    save(store, [ev('a', '泽连斯基在峰会发言', NOW - timedelta(hours=6))])
    uid = store.create_user('reader', 'x', 'viewer')
    with store._conn() as conn:
        conn.execute("UPDATE events SET canonical_id='7:deleted'")   # 旧规则留下的、指向已删除事件的键
        conn.execute("INSERT INTO pushed_events VALUES (1, '7:deleted', ?)", (tz.iso(NOW),))
        conn.execute("INSERT INTO read_events VALUES (?, '7:deleted', ?)", (uid, tz.iso(NOW)))
        conn.execute("INSERT INTO favorites VALUES (?, '7:deleted', ?)", (uid, tz.iso(NOW)))
        conn.execute("UPDATE settings SET value='2' WHERE key='feed_index_version'")
    store = Store(tmp_path / 'n.db')
    key = store._rows('SELECT canonical_id FROM events')[0]['canonical_id']
    assert key == '1:a'
    assert key in store.pushed_canonical_ids(1)
    item = store.feed(user_id=uid, now=NOW).items[0]
    assert item.read and item.favorite
