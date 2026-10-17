import logging
import re
from datetime import timedelta

import httpx
from fastapi.testclient import TestClient

from news_admin import tz
from news_admin.auth import verify_password
from news_admin.config import Settings
from news_admin.runtime import build_runtime
from news_admin.web import create_app
from news_admin.fetch import Source, fetch_source


def test_week_window_and_runtime_slow_mode(tmp_path, monkeypatch):
    from news_admin import runtime as module
    rt=build_runtime(Settings(data_dir=tmp_path,env_path=tmp_path/'.env',_env_file=None))
    rt.store.set_setting('fetch_window','7d')
    seen={}
    monkeypatch.setattr(module,'fetch_source',lambda source,**kw: seen.update(kw))
    rt._fetch(rt.store.list_sources()[0])
    assert seen['slow'] is True
    assert abs((tz.now()-seen['since']).total_seconds()-7*86400)<2
    assert '7d' in tz.FETCH_WINDOWS


def test_slow_requests_are_serial_and_delayed_and_stop_on_rate_limit(monkeypatch):
    from news_admin import fetch as module
    sleeps=[]
    monkeypatch.setattr(module.time,'sleep',lambda seconds:sleeps.append(seconds))
    monkeypatch.setattr(module.random,'uniform',lambda a,b:3)
    rss='''<rss version="2.0"><channel><title>x</title><item><title>A</title><link>https://n.test/a</link></item><item><title>B</title><link>https://n.test/b</link></item></channel></rss>'''
    requests=[]
    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200,text=rss) if request.url.path=='/rss' else httpx.Response(429)
    result=fetch_source(Source(1,'test','https://n.test/rss'),slow=True,transport=httpx.MockTransport(handler))
    assert requests==['/rss','/a']
    assert sleeps and all(3<=s<=6 for s in sleeps)
    assert '429' in result.error


def test_first_account_is_logged_once_never_written_as_text(tmp_path,caplog):
    s=Settings(data_dir=tmp_path,env_path=tmp_path/'.env',_env_file=None)
    rt=build_runtime(s)
    with caplog.at_level(logging.WARNING):
        app=create_app(rt,start_scheduler=False)
    text=caplog.text
    password=re.search(r'初始密码：([^\s]+)',text).group(1)
    assert '初始化管理员：admin' in text
    assert verify_password(password,rt.store.get_user_by_name('admin')['password_hash'])
    assert not (tmp_path/'admin_password.txt').exists()
    client=TestClient(app)
    client.post('/login',data={'username':'admin','password':password})
    assert client.get('/admin').status_code==200
    caplog.clear()
    create_app(rt,start_scheduler=False)
    assert '初始密码' not in caplog.text


def test_settings_file_can_live_inside_docker_volume(tmp_path,monkeypatch):
    from news_admin.config import get_settings
    path=tmp_path/'config.env';path.write_text('NEWS_LLM_MODEL=durable-model\n',encoding='utf-8')
    monkeypatch.setenv('NEWS_ENV_PATH',str(path))
    settings=get_settings()
    assert settings.llm_model=='durable-model' and settings.env_path==path
