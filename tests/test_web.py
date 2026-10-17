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
    an = ArticleAnalysis(title_zh='美联储降息', region='international', topic='economy', topic_confidence=0.9, is_opinion=False,
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
