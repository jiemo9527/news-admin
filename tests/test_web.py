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
    home = client.get('/', follow_redirects=False)
    assert home.headers['location'] == f'/briefs/{run_id}'
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
