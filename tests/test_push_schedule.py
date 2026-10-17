from news_admin import tz
from news_admin.config import Settings
from news_admin.runtime import build_runtime
from news_admin import runtime as runtime_mod
from tests.test_store import make_event


def test_collect_never_pushes(tmp_path, monkeypatch):
    rt=build_runtime(Settings(data_dir=tmp_path, _env_file=None))
    calls=[]
    monkeypatch.setattr(rt.pipeline, 'run', lambda trigger: calls.append(trigger) or 99)
    monkeypatch.setattr(rt, 'cleanup', lambda: {})
    monkeypatch.setattr(rt, 'deliver', lambda rid: calls.append('push'))
    assert rt.collect('schedule') == 99 and calls == ['schedule']


def test_scheduled_digest_uses_all_runs_and_per_channel_success_cursor(tmp_path, monkeypatch):
    rt=build_runtime(Settings(data_dir=tmp_path, public_base_url='https://news.test', _env_file=None))
    rt.store.upsert_channel(None,'telegram','ok',{'bot_token':'a','chat_id':'1'})
    rt.store.upsert_channel(None,'telegram','fail',{'bot_token':'b','chat_id':'2'})
    for id in ('one','two'):
        rid=rt.store.start_run('test')
        event=make_event(id=id,urls=(f'https://x/{id}',))
        event.articles[0].published_at=tz.now()
        event.summary.headline=f'新闻{id}'
        rt.store.save_events(rid,[event]); rt.store.finish_run(rid,'completed',{})
    texts=[]
    def send(kind, config, text, **kwargs):
        texts.append(text)
        if config['chat_id']=='2':
            raise RuntimeError('network failure')
    monkeypatch.setattr(runtime_mod,'send',send)
    results=rt.deliver_digest()
    assert [r['status'] for r in results] == ['sent','failed']
    assert '新闻one' in texts[0] and '新闻two' in texts[0]
    assert rt.store.get_setting('push_cursor_1') and not rt.store.get_setting('push_cursor_2')
    texts.clear()
    results=rt.deliver_digest()
    # 成功的渠道没有新内容，不再发送；失败的渠道下次重试
    assert len(texts)==1 and [r['status'] for r in results]==['empty','failed']
