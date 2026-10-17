from datetime import datetime, timezone

from fastapi.testclient import TestClient

from news_admin.config import Settings
from news_admin.models import Article, ArticleAnalysis, Event, EventSummary
from news_admin.runtime import build_runtime
from news_admin.web import create_app


def make_client(tmp_path):
    settings = Settings(data_dir=tmp_path, admin_password='pw', secret_key='s', llm_api_key='k', _env_file=None)
    rt = build_runtime(settings)
    app = create_app(rt, start_scheduler=False)
    return rt, TestClient(app)


def seed_run(rt):
    run_id = rt.store.start_run('test')
    a = Article(id='a', source='BBC', url='https://x/1', title='Fed cuts', published_at=datetime.now(timezone.utc))
    an = ArticleAnalysis(title_zh='美联储降息', region='international', topic='economy', topic_confidence=0.9, content_type='news', has_new_development=True,
                         entities=['美联储'], key_facts=['降息'], event_fingerprint='x', impact=4, impact_reason='r')
    s = EventSummary.model_validate({'headline': '美联储降息25个基点', 'sentences': [{'text': '美联储宣布降息。', 'sources': [1]}], 'conflicts': ['A称X；B称Y']})
    rt.store.save_events(run_id, [Event(id='e1', articles=[a], analyses={'a': an}, summary=s, score=9)])
    rt.store.finish_run(run_id, 'completed', {'events_published': 1})
    return run_id


def test_public_brief_renders_citations_and_conflicts(tmp_path):
    rt, client = make_client(tmp_path)
    run_id = seed_run(rt)
    home = client.get('/')
    assert home.status_code == 200 and '美联储降息25个基点' in home.text
    html = client.get(f'/briefs/{run_id}').text
    assert '美联储降息25个基点' in html and '[1]' in html and '说法不一：A称X；B称Y' in html and '国际' in html


def test_admin_requires_login(tmp_path):
    _, client = make_client(tmp_path)
    assert client.get('/admin', follow_redirects=False).status_code == 303
    client.post('/login', data={'username': 'admin', 'password': 'wrong'})
    assert client.get('/admin', follow_redirects=False).status_code == 303
    client.post('/login', data={'username': 'admin', 'password': 'pw'})
    assert client.get('/admin').status_code == 200


def test_admin_pages_render_and_channel_secret_is_kept_when_blank(tmp_path):
    rt, client = make_client(tmp_path)
    run_id = seed_run(rt)
    client.post('/login', data={'username': 'admin', 'password': 'pw'})
    for path in ('/admin', f'/admin/runs/{run_id}', '/admin/sources', '/admin/channels'):
        assert client.get(path).status_code == 200, path
    client.post('/admin/channels', data={'kind': 'telegram', 'name': 'tg', 'bot_token': 'SECRET', 'chat_id': '1', 'enabled': '1'})
    channel = rt.store.list_channels()[0]
    client.post('/admin/channels', data={'kind': 'telegram', 'channel_id': channel['id'], 'name': 'tg2', 'bot_token': '', 'chat_id': '2', 'enabled': '1'})
    updated = rt.store.list_channels()[0]
    assert updated['config'] == {'bot_token': 'SECRET', 'chat_id': '2', 'thread_id': ''} and updated['name'] == 'tg2'


def test_invalid_cron_is_rejected(tmp_path):
    rt, client = make_client(tmp_path)
    client.post('/login', data={'username': 'admin', 'password': 'pw'})
    client.post('/admin/schedule', data={'cron': 'not cron', 'enabled': '1'})
    assert rt.store.get_setting('schedule_enabled', '0') == '0'


def test_cluster_thresholds_follow_embedder(tmp_path):
    from news_admin.config import Settings
    from news_admin.runtime import build_runtime

    degraded = build_runtime(Settings(data_dir=tmp_path / 'a', _env_file=None))
    assert (degraded.cluster_config().high, degraded.cluster_config().low) == (0.75, 0.40)
    real = build_runtime(Settings(data_dir=tmp_path / 'b', embedding_api_key='k', _env_file=None))
    assert (real.cluster_config().high, real.cluster_config().low) == (0.86, 0.72)
    override = build_runtime(Settings(data_dir=tmp_path / 'c', cluster_low=0.5, _env_file=None))
    assert override.cluster_config().low == 0.5


def test_html_source_requires_valid_link_pattern(tmp_path):
    rt, client = make_client(tmp_path)
    client.post('/login', data={'username': 'admin', 'password': 'pw'})
    before = len(rt.store.list_sources())
    client.post('/admin/sources', data={'name': 'x', 'feed_url': 'https://x.test/', 'kind': 'html', 'link_pattern': ''})
    client.post('/admin/sources', data={'name': 'y', 'feed_url': 'https://y.test/', 'kind': 'html', 'link_pattern': '(['})
    assert len(rt.store.list_sources()) == before
    client.post('/admin/sources', data={'name': 'z', 'feed_url': 'https://z.test/', 'kind': 'html', 'link_pattern': r'/story\d+'})
    z = [s for s in rt.store.list_sources() if s.name == 'z'][0]
    assert z.kind == 'html' and z.link_pattern == r'/story\d+'


def test_brief_shows_time_thumbnail_and_previous_coverage(tmp_path):
    import io
    from PIL import Image
    from news_admin.models import EventRef
    rt, client = make_client(tmp_path)
    key = 'a' * 32
    buf = io.BytesIO(); Image.new('RGB', (10, 10)).save(buf, 'WEBP')
    (rt.thumbnails.directory / f'{key}.webp').write_bytes(buf.getvalue())
    run_id = rt.store.start_run('test')
    a = Article(id='a', source='BBC', url='https://x/1', title='Fed', published_at=datetime.now(timezone.utc), time_precision='exact')
    b = Article(id='b', source='人民网', url='https://x/2', title='b', published_at=datetime.now(timezone.utc), time_precision='date')
    an = ArticleAnalysis(title_zh='美股大涨', region='international', topic='economy', topic_confidence=0.9, content_type='news', has_new_development=True,
                         entities=[], key_facts=['f'], event_fingerprint='x', impact=4, impact_reason='r')
    s = EventSummary.model_validate({'headline': '美股因降息大涨', 'sentences': [{'text': '美股收涨。', 'sources': [1]}]})
    ref = EventRef(run_id=1, event_id='old', headline='美联储降息25个基点', published_at=datetime.now(timezone.utc), time_precision='exact', relation='follow_up')
    rt.store.save_events(run_id, [Event(id='e1', articles=[a, b], analyses={'a': an}, summary=s, score=9, thumbnail=key, previous=[ref])])
    rt.store.finish_run(run_id, 'completed', {})
    html = client.get(f'/briefs/{run_id}').text
    assert f'/thumb/{key}' in html
    assert '前情' in html and '美联储降息25个基点' in html and '/briefs/1#e-old' in html
    assert '创建 刚刚' in html and '未注明时刻' not in html and '创建 今天' in html
    response = client.get(f'/thumb/{key}')
    assert response.status_code == 200 and response.headers['content-type'] == 'image/webp'
    assert client.get('/thumb/' + 'b' * 32).status_code == 404


def test_brief_shows_rating_breakdown(tmp_path):
    from news_admin.rank import rate_event
    rt, client = make_client(tmp_path)
    run_id = rt.store.start_run('test')
    a = Article(id='a', source='BBC', url='https://x/1', title='Fed', published_at=datetime.now(timezone.utc), time_precision='exact')
    an = ArticleAnalysis(title_zh='t', region='international', topic='economy', topic_confidence=0.9, content_type='news', has_new_development=True,
                         entities=[], key_facts=['f'], event_fingerprint='x', impact=4, impact_reason='影响美国货币政策')
    e = Event(id='e1', articles=[a], analyses={'a': an}, summary=EventSummary.model_validate({'headline': '美联储降息', 'sentences': [{'text': '降息。', 'sources': [1]}]}))
    e.rating = rate_event(e, datetime.now(timezone.utc)); e.score = e.rating.total
    rt.store.save_events(run_id, [e]); rt.store.finish_run(run_id, 'completed', {})
    html = client.get(f'/briefs/{run_id}').text
    assert 'grade-A' in html and '评级 <b>A</b>' in html
    for text in ('影响力', '4/5', '来源', '1 家', '新鲜度', '来源权重', '影响美国货币政策'):
        assert text in html


def test_runtime_fetch_uses_configured_window_and_cleans_up(tmp_path, monkeypatch):
    from news_admin import runtime as runtime_mod, tz
    from news_admin.fetch import FetchResult
    rt, _ = make_client(tmp_path)
    seen = {}

    def fake_fetch(source, since=None, **kw):
        seen['since'] = since
        return FetchResult(source=source)

    monkeypatch.setattr(runtime_mod, 'fetch_source', fake_fetch)
    rt.store.set_setting('fetch_window', '24h')
    rt.pipeline.deps.fetch(rt.store.list_sources()[0])
    assert abs((tz.now() - seen['since']).total_seconds() - 86400) < 5
    rt.store.set_setting('fetch_window', 'today_yesterday')
    rt.pipeline.deps.fetch(rt.store.list_sources()[0])
    assert seen['since'] == tz.day_start(days_ago=1)


def test_home_feed_filters_and_pagination_links(tmp_path):
    rt, client = make_client(tmp_path)
    seed_run(rt)
    html = client.get('/', params={'q': '美联储', 'range': '3d', 'sort': 'score'}).text
    assert '<mark>美联储</mark>降息25个基点' in html
    assert client.get('/', params={'q': '<script>'}).text.count('<script>') == 0
    assert 'name="q"' in html and 'value="美联储"' in html
    for label in ('今天', '昨天', '3天内', '一周内'):
        assert label in html
    assert 'aria-current="page"' in html
    miss = client.get('/', params={'q': '不存在的关键词'}).text
    assert '没有符合条件的新闻' in miss
    assert client.get('/', params={'range': 'bogus', 'page': '-3'}).status_code == 200


def test_settings_page_saves_fetch_window_and_retention(tmp_path):
    rt, client = make_client(tmp_path)
    client.post('/login', data={'username': 'admin', 'password': 'pw'})
    assert client.get('/admin/settings').status_code == 200
    client.post('/admin/settings', data={'fetch_window': '24h', 'retention_days': '30'})
    assert rt.store.get_setting('fetch_window') == '24h' and rt.store.get_setting('retention_days') == '30'
    client.post('/admin/settings', data={'fetch_window': 'evil', 'retention_days': '0'})
    assert rt.store.get_setting('fetch_window') == '24h' and rt.store.get_setting('retention_days') == '30'


def test_password_change_when_not_pinned_by_env(tmp_path):
    settings = Settings(data_dir=tmp_path, secret_key='s', llm_api_key='k', _env_file=None)
    rt = build_runtime(settings)
    client = TestClient(create_app(rt, start_scheduler=False))
    initial = (tmp_path / 'admin_password.txt').read_text(encoding='utf-8').strip()
    client.post('/login', data={'username': 'admin', 'password': initial})
    assert client.get('/admin').status_code == 200
    client.post('/admin/password', data={'current': 'wrong', 'new': 'n3w-Passw0rd', 'confirm': 'n3w-Passw0rd'})
    client.post('/admin/password', data={'current': initial, 'new': 'short', 'confirm': 'short'})
    client.post('/logout')
    client.post('/login', data={'username': 'admin', 'password': initial})
    assert client.get('/admin', follow_redirects=False).status_code == 200
    client.post('/admin/password', data={'current': initial, 'new': 'n3w-Passw0rd', 'confirm': 'n3w-Passw0rd'})
    assert not (tmp_path / 'admin_password.txt').exists()
    assert rt.store.get_setting('admin_password') == ''
    client.post('/logout')
    client.post('/login', data={'username': 'admin', 'password': initial})
    assert client.get('/admin', follow_redirects=False).status_code == 303
    client.post('/login', data={'username': 'admin', 'password': 'n3w-Passw0rd'})
    assert client.get('/admin', follow_redirects=False).status_code == 200


def test_password_pinned_by_env_cannot_be_changed_in_ui(tmp_path):
    rt, client = make_client(tmp_path)
    client.post('/login', data={'username': 'admin', 'password': 'pw'})
    assert '由环境变量' in client.get('/admin/settings').text
    client.post('/admin/password', data={'current': 'pw', 'new': 'n3w-Passw0rd', 'confirm': 'n3w-Passw0rd'})
    client.post('/logout')
    client.post('/login', data={'username': 'admin', 'password': 'pw'})
    assert client.get('/admin', follow_redirects=False).status_code == 200



def login(client):
    client.post('/login', data={'username': 'admin', 'password': 'pw'})


def test_footer_has_github_link_and_feed_header_sentence_is_gone(tmp_path):
    rt, client = make_client(tmp_path)
    seed_run(rt)
    html = client.get('/').text
    assert 'href="https://github.com/jiemo9527/news-admin"' in html
    assert '同一事件只显示最新版本' not in html and '保留近' not in html


def test_admin_can_delete_a_brief_but_anonymous_cannot(tmp_path):
    rt, client = make_client(tmp_path)
    run_id = seed_run(rt)
    client.post(f'/admin/briefs/{run_id}/delete')
    assert rt.store.get_run(run_id) is not None
    login(client)
    assert '删除' in client.get('/briefs').text
    client.post(f'/admin/briefs/{run_id}/delete')
    assert rt.store.get_run(run_id) is None
    assert client.get(f'/briefs/{run_id}').status_code == 404
    assert '美联储降息25个基点' not in client.get('/').text


def test_update_does_not_show_previous_block_but_follow_up_shows_context(tmp_path):
    from news_admin.models import EventRef
    rt, client = make_client(tmp_path)
    run_id = rt.store.start_run('test')

    def make(eid, url, headline, relation):
        a = Article(id=eid, source='BBC', url=url, title='t', published_at=datetime.now(timezone.utc), time_precision='exact')
        an = ArticleAnalysis(title_zh=headline, region='international', topic='society', topic_confidence=0.9, content_type='news',
                             has_new_development=True, entities=[], key_facts=['f'], event_fingerprint='x', impact=3, impact_reason='r')
        ref = EventRef(run_id=1, event_id='old', headline=f'旧报道{eid}', relation=relation)
        return Event(id=eid, articles=[a], analyses={eid: an}, is_update=relation == 'update', previous=[ref],
                     summary=EventSummary.model_validate({'headline': headline, 'sentences': [{'text': headline + '。', 'sources': [1]}]}))

    rt.store.save_events(run_id, [make('u', 'https://x/u', '墨西哥三人获刑', 'update'), make('f', 'https://x/f', '美股大涨', 'follow_up')])
    rt.store.finish_run(run_id, 'completed', {})
    for html in (client.get('/').text, client.get(f'/briefs/{run_id}').text):
        assert '旧报道u' not in html and '此前' not in html
        assert '旧报道f' in html and '前情' in html


def test_reference_list_shows_chinese_title_with_original_as_tooltip(tmp_path):
    rt, client = make_client(tmp_path)
    run_id = rt.store.start_run('test')
    a = Article(id='a', source='Guardian', url='https://x/1', title='Brazilian Amazon deforestation falls', published_at=datetime.now(timezone.utc))
    an = ArticleAnalysis(title_zh='巴西亚马孙毁林面积创新低', region='international', topic='environment', topic_confidence=0.9, content_type='news',
                         has_new_development=True, entities=[], key_facts=['f'], event_fingerprint='x', impact=4, impact_reason='r')
    rt.store.save_events(run_id, [Event(id='e', articles=[a], analyses={'a': an},
                                        summary=EventSummary.model_validate({'headline': '巴西毁林创新低', 'sentences': [{'text': '毁林下降。', 'sources': [1]}]}))])
    rt.store.finish_run(run_id, 'completed', {})
    for html in (client.get('/').text, client.get(f'/briefs/{run_id}').text):
        assert 'Guardian｜巴西亚马孙毁林面积创新低' in html
        assert 'title="原题：Brazilian Amazon deforestation falls"' in html


def test_sources_page_offers_presets_edit_prefill_and_quick_add(tmp_path):
    rt, client = make_client(tmp_path)
    login(client)
    html = client.get('/admin/sources').text
    assert '推荐来源' in html and '法广中文' in html.split('id="presets"')[1] and '一键添加' in html
    client.post('/admin/sources/preset', data={'feed_url': 'https://www.rfi.fr/cn/rss'})
    added = [s for s in rt.store.list_sources() if s.name == '法广中文']
    assert added and added[0].enabled
    assert '法广中文' not in client.get('/admin/sources').text.split('id="presets"')[1]
    client.post('/admin/sources/preset', data={'feed_url': 'https://evil.test/rss'})
    assert not any(s.feed_url == 'https://evil.test/rss' for s in rt.store.list_sources())
    edit = client.get(f'/admin/sources?edit={added[0].id}').text
    assert 'value="法广中文"' in edit and f'name="source_id" value="{added[0].id}"' in edit


def test_deleted_default_source_stays_deleted_after_restart(tmp_path):
    rt, client = make_client(tmp_path)
    login(client)
    bbc = next(s for s in rt.store.list_sources() if s.name == 'BBC')
    client.post(f'/admin/sources/{bbc.id}/delete')
    settings = Settings(data_dir=tmp_path, admin_password='pw', secret_key='s', llm_api_key='k', _env_file=None)
    assert not any(s.name == 'BBC' for s in build_runtime(settings).store.list_sources())


def test_source_test_fetch_reports_result(tmp_path, monkeypatch):
    from news_admin import runtime as runtime_mod
    from news_admin.fetch import FetchResult
    rt, client = make_client(tmp_path)
    login(client)
    monkeypatch.setattr(runtime_mod, 'fetch_source', lambda source, since=None, **kw: FetchResult(
        source=source, articles=[Article(id='x', source=source.name, url='https://x/1', title='测试标题一', published_at=datetime.now(timezone.utc))]))
    sid = rt.store.list_sources()[0].id
    page = client.post(f'/admin/sources/{sid}/test', follow_redirects=True).text
    assert '抓到 1 条' in page and '测试标题一' in page
