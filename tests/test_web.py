from datetime import datetime, timezone

from fastapi.testclient import TestClient

from news_admin.config import Settings
from news_admin.models import Article, ArticleAnalysis, Event, EventSummary
from news_admin.runtime import build_runtime
from news_admin.web import create_app


def make_client(tmp_path, login=True):
    settings = Settings(data_dir=tmp_path, admin_password='pw', secret_key='s', llm_api_key='k', env_path=tmp_path / '.env', _env_file=None)
    rt = build_runtime(settings)
    app = create_app(rt, start_scheduler=False)
    client = TestClient(app)
    if login:
        client.post('/login', data={'username': 'admin', 'password': 'pw'})
    return rt, client


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
    _, client = make_client(tmp_path, login=False)
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
    client.post(f"/admin/channels/{channel['id']}", data={'name': 'tg2', 'bot_token': '', 'chat_id': '2', 'enabled': '1'})
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


def test_password_change_when_not_pinned_by_env(tmp_path, caplog):
    settings = Settings(data_dir=tmp_path, secret_key='s', llm_api_key='k', env_path=tmp_path / '.env', _env_file=None)
    rt = build_runtime(settings)
    client = TestClient(create_app(rt, start_scheduler=False))
    import re
    initial = re.search(r'初始密码：([^\s]+)', caplog.text).group(1)
    assert not (tmp_path / 'admin_password.txt').exists()
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
    rt, client = make_client(tmp_path, login=False)
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



def test_model_settings_are_saved_to_env_file_and_applied_without_restart(tmp_path):
    rt, client = make_client(tmp_path)
    (tmp_path / '.env').write_text('# keep me\nNEWS_LLM_API_KEY=old-secret\nOTHER=1\n', encoding='utf-8')
    login(client)
    page = client.get('/admin/settings').text
    assert 'old-secret' not in page and 'name="llm_model"' in page
    client.post('/admin/settings/models', data={
        'llm_base_url': 'https://gw.test/v1', 'llm_model': 'gpt-x', 'llm_api_key': '', 'llm_concurrency': '6',
        'embedding_base_url': 'https://emb.test/v1', 'embedding_model': 'Qwen/Qwen3-Embedding-4B', 'embedding_api_key': 'new-emb-key',
        'cluster_high': '0.84', 'cluster_low': '', 'min_impact': '3', 'max_events': '50',
    })
    env = (tmp_path / '.env').read_text(encoding='utf-8')
    assert '# keep me' in env and 'OTHER=1' in env
    assert 'NEWS_LLM_API_KEY=old-secret' in env                 # 留空 = 不修改密钥
    assert 'NEWS_LLM_MODEL=gpt-x' in env and 'NEWS_EMBEDDING_API_KEY=new-emb-key' in env
    assert 'NEWS_MAX_EVENTS=50' in env and 'NEWS_CLUSTER_HIGH=0.84' in env and 'NEWS_CLUSTER_LOW' not in env
    assert rt.llm.settings.model == 'gpt-x' and rt.llm.settings.base_url == 'https://gw.test/v1' and rt.llm.settings.api_key == 'k'
    assert rt.embedder.model == 'Qwen/Qwen3-Embedding-4B' and not rt.embedding_degraded
    assert rt.cluster_config().high == 0.84 and rt.pipeline.deps.filter_config.min_impact == 3
    assert rt.max_events() == 50
    reloaded = Settings(data_dir=tmp_path, _env_file=tmp_path / '.env')
    assert reloaded.llm_model == 'gpt-x' and reloaded.max_events == 50


def test_model_settings_reject_bad_values(tmp_path):
    rt, client = make_client(tmp_path)
    login(client)
    before = rt.llm.settings.model
    for bad in ({'llm_base_url': 'ftp://x'}, {'llm_model': 'a\nNEWS_ADMIN_PASSWORD=x'}, {'max_events': '0'}, {'cluster_high': '1.5'}):
        data = {'llm_base_url': 'https://gw.test/v1', 'llm_model': 'gpt-x', 'llm_concurrency': '4', 'embedding_base_url': 'https://e.test/v1',
                'embedding_model': 'm', 'min_impact': '2', 'max_events': '30', 'cluster_high': '', 'cluster_low': '', **bad}
        client.post('/admin/settings/models', data=data)
        assert rt.llm.settings.model == before, bad
    assert not (tmp_path / '.env').exists() or 'NEWS_ADMIN_PASSWORD' not in (tmp_path / '.env').read_text(encoding='utf-8')


def test_connection_tests_report_results(tmp_path, monkeypatch):
    rt, client = make_client(tmp_path)
    login(client)
    monkeypatch.setattr(rt, 'test_llm', lambda: (True, 'gpt-x 响应 0.8 秒'))
    monkeypatch.setattr(rt, 'test_embedding', lambda: (False, 'HTTP 401'))
    assert 'gpt-x 响应 0.8 秒' in client.post('/admin/settings/test/llm', follow_redirects=True).text
    assert 'HTTP 401' in client.post('/admin/settings/test/embedding', follow_redirects=True).text



def make_viewer(client, name='reader', password='reader-pass1'):
    client.post('/admin/users', data={'username': name, 'password': password, 'role': 'viewer'})
    other = TestClient(client.app)
    other.post('/login', data={'username': name, 'password': password})
    return other


def test_guests_cannot_see_anything(tmp_path):
    rt, client = make_client(tmp_path)
    run_id = seed_run(rt)
    guest = TestClient(client.app)
    for path in ('/', '/?q=美联储', '/briefs', f'/briefs/{run_id}', '/thumb/' + 'a' * 32, '/admin', '/admin/settings', '/admin/users'):
        r = guest.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers['location'].startswith('/login'), path
    assert guest.get('/login').status_code == 200 and '美联储' not in guest.get('/login').text
    assert guest.get('/static/style.css').status_code == 200
    assert guest.get('/healthz').json() == {'ok': True}
    assert guest.post('/admin/run', follow_redirects=False).status_code == 303


def test_legacy_admin_password_is_migrated_to_users_table(tmp_path):
    from news_admin.auth import hash_password
    settings = Settings(data_dir=tmp_path, secret_key='s', llm_api_key='k', env_path=tmp_path / '.env', _env_file=None)
    rt = build_runtime(settings)
    rt.store.set_setting('admin_password_hash', hash_password('legacy-pass1'))
    client = TestClient(create_app(rt, start_scheduler=False))
    client.post('/login', data={'username': 'admin', 'password': 'legacy-pass1'})
    assert client.get('/admin', follow_redirects=False).status_code == 200
    assert rt.store.get_user_by_name('admin')['role'] == 'admin'
    assert not (tmp_path / 'admin_password.txt').exists()


def test_viewer_can_browse_admin_but_cannot_change_anything(tmp_path):
    rt, client = make_client(tmp_path)
    run_id = seed_run(rt)
    viewer = make_viewer(client)
    assert '美联储降息25个基点' in viewer.get('/').text
    for path in ('/admin', f'/admin/runs/{run_id}', '/admin/sources', '/admin/channels', '/admin/settings', f'/briefs/{run_id}', '/briefs'):
        r = viewer.get(path)
        assert r.status_code == 200, path
        assert '/delete"' not in r.text and 'action="/admin/run"' not in r.text and '一键添加' not in r.text, path
    assert viewer.get('/admin/users').status_code == 403
    sid = rt.store.list_sources()[0].id
    data = {'cron': '* * * * *', 'enabled': '1', 'fetch_window': '24h', 'retention_days': '30', 'llm_model': 'x'}
    for path in ('/admin/run', f'/admin/sources/{sid}/delete', f'/admin/sources/{sid}/toggle', f'/admin/briefs/{run_id}/delete',
                 '/admin/settings', '/admin/schedule', '/admin/settings/models', '/admin/users'):
        assert viewer.post(path, data=data).status_code == 403, path
    assert rt.store.get_run(run_id) is not None and rt.store.list_sources()[0].enabled
    assert rt.store.get_setting('schedule_enabled', '0') == '0'
    viewer.post('/admin/password', data={'current': 'reader-pass1', 'new': 'reader-pass2', 'confirm': 'reader-pass2'})
    again = TestClient(client.app)
    again.post('/login', data={'username': 'reader', 'password': 'reader-pass2'})
    assert again.get('/', follow_redirects=False).status_code == 200


def test_user_management_guards(tmp_path):
    rt, client = make_client(tmp_path)
    admin = rt.store.get_user_by_name('admin')
    client.post('/admin/users', data={'username': 'reader', 'password': 'short', 'role': 'viewer'})
    client.post('/admin/users', data={'username': 'bad name!', 'password': 'long-enough', 'role': 'viewer'})
    assert [u['username'] for u in rt.store.list_users()] == ['admin']
    client.post('/admin/users', data={'username': 'reader', 'password': 'reader-pass1', 'role': 'viewer'})
    client.post('/admin/users', data={'username': 'READER', 'password': 'reader-pass1', 'role': 'admin'})
    assert [u['username'] for u in rt.store.list_users()] == ['admin', 'reader']
    reader = rt.store.get_user_by_name('reader')
    html = client.get('/admin/users').text
    assert 'reader' in html and '只读用户' in html and '管理员' in html
    for action in ('delete', 'toggle'):
        client.post(f"/admin/users/{admin['id']}/{action}")
    client.post(f"/admin/users/{admin['id']}/role", data={'role': 'viewer'})
    me = rt.store.get_user(admin['id'])
    assert me and me['enabled'] and me['role'] == 'admin'
    viewer = TestClient(client.app)
    viewer.post('/login', data={'username': 'reader', 'password': 'reader-pass1'})
    assert viewer.get('/', follow_redirects=False).status_code == 200
    client.post(f"/admin/users/{reader['id']}/password", data={'password': 'reset-pass-9'})
    client.post(f"/admin/users/{reader['id']}/toggle")
    assert viewer.get('/', follow_redirects=False).status_code == 303
    client.post(f"/admin/users/{reader['id']}/toggle")
    fresh = TestClient(client.app)
    fresh.post('/login', data={'username': 'reader', 'password': 'reset-pass-9'})
    assert fresh.get('/', follow_redirects=False).status_code == 200
    client.post(f"/admin/users/{reader['id']}/role", data={'role': 'admin'})
    assert fresh.get('/admin/users').status_code == 200
    client.post(f"/admin/users/{reader['id']}/delete")
    assert rt.store.get_user(reader['id']) is None
    assert fresh.get('/', follow_redirects=False).status_code == 303


def test_feed_source_filter_and_live_search_markup(tmp_path):
    rt, client = make_client(tmp_path)
    seed_run(rt)
    run_id = rt.store.start_run('test')
    a = Article(id='r', source='路透社', url='https://x/r', title='Quake', published_at=datetime.now(timezone.utc))
    an = ArticleAnalysis(title_zh='日本地震', region='international', topic='society', topic_confidence=0.9, content_type='news',
                         has_new_development=True, entities=[], key_facts=['f'], event_fingerprint='x', impact=3, impact_reason='r')
    rt.store.save_events(run_id, [Event(id='q', articles=[a], analyses={'r': an},
                                        summary=EventSummary.model_validate({'headline': '日本发生地震', 'sentences': [{'text': '地震。', 'sources': [1]}]}))])
    rt.store.finish_run(run_id, 'completed', {})
    html = client.get('/').text
    assert 'data-live-form' in html and 'data-live="results"' in html and '/static/app.js' in html
    assert '<option value="路透社"' in html and 'BBC（1）' in html
    only = client.get('/', params={'source': '路透社'}).text
    assert '日本发生地震' in only and '美联储降息25个基点' not in only and 'selected>路透社' in only
    assert client.get('/static/app.js').status_code == 200


def test_failed_runs_can_be_deleted_by_admin(tmp_path):
    rt, client = make_client(tmp_path)
    failed = rt.store.start_run('manual')
    rt.store.finish_run(failed, 'failed', {}, 'boom')
    assert f'/admin/runs/{failed}/delete' in client.get('/admin').text
    client.post(f'/admin/runs/{failed}/delete')
    assert rt.store.get_run(failed) is None
    running = rt.store.start_run('manual')
    client.post(f'/admin/runs/{running}/delete')
    assert rt.store.get_run(running) is not None


def test_sources_render_as_cards(tmp_path):
    rt, client = make_client(tmp_path)
    html = client.get('/admin/sources').text
    assert html.count('preset source-card') == len(rt.store.list_sources())



def test_generated_credentials_are_strong_unambiguous_and_shown_once(tmp_path):
    import re as _re
    from news_admin.auth import AMBIGUOUS, generate_password, generate_username
    pw = {generate_password() for _ in range(50)}
    assert len(pw) == 50
    for value in pw:
        assert _re.fullmatch(r'[A-Za-z0-9]{5}(-[A-Za-z0-9]{5}){3}', value)
        assert not set(value) & set(AMBIGUOUS)
        assert _re.search('[A-Z]', value) and _re.search('[a-z]', value) and _re.search('[0-9]', value)
    assert _re.fullmatch(r'[a-z][a-z2-9]{6,}', generate_username())

    rt, client = make_client(tmp_path)
    page = client.post('/admin/users', data={'username': '', 'role': 'viewer'}, follow_redirects=True).text
    m = _re.search(r'账号：(\S+)\s+密码：(\S+)', page)
    assert m and 'data-copy' in page and '只显示这一次' in page
    username, password = m.groups()
    assert rt.store.get_user_by_name(username)['role'] == 'viewer'
    assert password not in client.get('/admin/users').text           # 刷新后不再显示
    other = TestClient(client.app)
    other.post('/login', data={'username': username, 'password': password})
    assert other.get('/', follow_redirects=False).status_code == 200

    uid = rt.store.get_user_by_name(username)['id']
    page = client.post(f'/admin/users/{uid}/password', data={}, follow_redirects=True).text
    new_password = _re.search(r'密码：(\S+)', page).group(1)
    assert new_password != password
    assert other.get('/', follow_redirects=False).status_code == 303   # 旧会话失效
    again = TestClient(client.app)
    again.post('/login', data={'username': username, 'password': new_password})
    assert again.get('/', follow_redirects=False).status_code == 200


def test_login_and_visits_are_logged_with_time_and_ip(tmp_path):
    rt, client = make_client(tmp_path)
    viewer = make_viewer(client)
    bad = TestClient(client.app)
    bad.post('/login', data={'username': 'reader', 'password': 'wrong-password'})
    bad.post('/login', data={'username': 'nobody', 'password': 'x'})
    viewer.get('/')
    viewer.get('/briefs')
    viewer.get('/static/style.css')
    viewer.post('/logout')
    rows = rt.store.list_access(limit=50)
    events = [(r['username'], r['event']) for r in rows]
    assert ('reader', 'login') in events and ('reader', 'login_failed') in events and ('nobody', 'login_failed') in events
    assert ('reader', 'logout') in events
    visits = [r for r in rows if r['username'] == 'reader' and r['event'] == 'visit']
    assert len(visits) == 1 and visits[0]['hits'] == 3 and visits[0]['path'] == '/briefs'   # 登录后跳转首页 + / + /briefs；静态文件不计
    assert all(r['ip'] == 'testclient' for r in rows)
    html = client.get('/admin/users').text
    assert '登录与访问记录' in html and 'testclient' in html and '登录失败' in html
    assert reader_last_ip(rt) == 'testclient'


def reader_last_ip(rt):
    return next(u for u in rt.store.list_users() if u['username'] == 'reader')['last_ip']


def test_forwarded_ip_is_only_trusted_when_configured(tmp_path):
    rt, client = make_client(tmp_path)
    spoof = TestClient(client.app)
    spoof.post('/login', data={'username': 'admin', 'password': 'pw'}, headers={'X-Forwarded-For': '1.2.3.4'})
    assert rt.store.list_access(limit=1)[0]['ip'] == 'testclient'
    rt.settings = rt.settings.model_copy(update={'trust_proxy': True})
    spoof.post('/login', data={'username': 'admin', 'password': 'pw'}, headers={'X-Forwarded-For': '1.2.3.4, 10.0.0.1'})
    assert rt.store.list_access(limit=1)[0]['ip'] == '1.2.3.4'


def test_old_access_logs_are_cleaned_up(tmp_path):
    from datetime import timedelta
    from news_admin import tz as _tz
    rt, client = make_client(tmp_path)
    with rt.store._conn() as conn:
        conn.execute("UPDATE access_log SET first_at=?, last_at=?", (_tz.iso(_tz.now() - timedelta(days=100)),) * 2)
    rt.cleanup()
    assert rt.store.list_access() == []



def test_push_and_fetch_schedules_are_independent(tmp_path):
    rt, client = make_client(tmp_path)
    client.post('/admin/schedule', data={'cron': '2 */2 * * *', 'enabled': '1'})
    assert rt.store.get_setting('schedule_cron') == '2 */2 * * *'
    html = client.get('/admin').text
    assert 'name="push_cron"' in html and '0 6,20 * * *' in html
    client.post('/admin/push-schedule', data={'push_cron': '0 7,21 * * *', 'enabled': '1'})
    assert rt.store.get_setting('push_cron') == '0 7,21 * * *'
    assert rt.store.get_setting('schedule_cron') == '2 */2 * * *'
    client.post('/admin/push-schedule', data={'push_cron': 'wrong', 'enabled': '1'})
    assert rt.store.get_setting('push_cron') == '0 7,21 * * *'
    scheduler = client.app.state.scheduler
    assert str(scheduler.get_job('pipeline').trigger.timezone) == 'Asia/Shanghai'
    assert str(scheduler.get_job('push_digest').trigger.timezone) == 'Asia/Shanghai'


def test_maintenance_admin_only_and_requires_password(tmp_path):
    rt, client = make_client(tmp_path)
    rid=seed_run(rt)
    viewer=make_viewer(client)
    assert viewer.get('/admin/data').status_code == 403
    assert viewer.get('/admin/data/export').status_code == 403
    assert client.get('/admin/data').status_code == 200
    for data in ({'password': 'wrong'}, {}):
        client.post('/admin/data/clear', data=data)
        assert rt.store.get_run(rid) is not None
    backup = client.post('/admin/data/export', data={'password': 'pw'})
    assert backup.status_code == 200 and backup.headers['content-type'] == 'application/zip'
    client.post('/admin/data/clear', data={'password': 'pw', 'confirm': '清空数据'})
    assert rt.store.get_run(rid) is None
    assert [u['username'] for u in rt.store.list_users()] == ['admin']
    assert client.get('/admin', follow_redirects=False).status_code == 200
    response = client.post('/admin/data/restore', data={'password': 'pw', 'confirm': '恢复数据'},
                           files={'file': ('backup.zip', backup.content, 'application/zip')}, follow_redirects=False)
    assert response.status_code == 303
    assert rt.store.get_run(rid) is not None
    assert 'reader' in [u['username'] for u in rt.store.list_users()]


def test_brief_category_navigation_has_persistent_active_state(tmp_path):
    rt, client = make_client(tmp_path)
    rid=seed_run(rt)
    html=client.get(f'/briefs/{rid}').text
    assert 'brief-nav' in html and 'data-brief-nav' in html
    assert '/static/brief.js' in html
