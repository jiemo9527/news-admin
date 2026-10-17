import json
from news_admin.digest import render_push
from tests.test_digest import ev
from tests.test_web import make_client
from news_admin import runtime as runtime_mod, tz


def test_compact_push_url_first_only_title_grade_and_weight():
    e=ev('1','美联储降息','economy',score=10.25,update=True,conflicts=['不要推送此冲突'])
    text=render_push([e], 'https://news.test/briefs/7')
    assert text.splitlines()[0]=='https://news.test/briefs/7'
    assert '1. 美联储降息｜评级 A｜权重 10.25' in text
    for extra in ('第一句','来源','创建','更新','前情','冲突','要闻'):
        assert extra not in text


def test_manual_and_scheduled_deliveries_use_compact_content_and_matching_url(tmp_path,monkeypatch):
    rt,client=make_client(tmp_path)
    rt.settings.public_base_url='https://news.test'
    rt.store.upsert_channel(None,'telegram','test',{'bot_token':'x','chat_id':'1'})
    sent=[]
    monkeypatch.setattr(runtime_mod,'send',lambda kind,config,text,**kw:sent.append(text))
    rid = 0
    for n in (1,2):
        rid=rt.store.start_run('test')
        e=ev(str(n),f'标题{n}','economy',score=10+n)
        rt.store.save_events(rid,[e]);rt.store.finish_run(rid,'completed',{})
    rt.deliver(rid)
    assert sent[-1].splitlines()[0]==f'https://news.test/briefs/{rid}'
    assert '第一句' not in sent[-1]
    rt.deliver_digest()
    text=sent[-1]
    # 标题2 已手动推送过，定时汇总只发未推送过的标题1
    assert '标题1' in text and '标题2' not in text and '第一句' not in text
    link=text.splitlines()[0]
    assert link.startswith('https://news.test/briefs/push/')
    response=client.get(link.replace('https://news.test',''))
    assert response.status_code==200 and '标题1第一句' in response.text and '标题2第一句' not in response.text
    guest=__import__('fastapi.testclient',fromlist=['TestClient']).TestClient(client.app)
    assert guest.get(link.replace('https://news.test',''),follow_redirects=False).status_code==303


def test_missing_public_url_never_sends_linkless_message(tmp_path,monkeypatch):
    rt,_=make_client(tmp_path)
    from tests.test_web import seed_run
    rid=seed_run(rt)
    rt.store.upsert_channel(None,'telegram','test',{'bot_token':'x','chat_id':'1'})
    monkeypatch.setattr(runtime_mod,'send',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('must not send')))
    for result in (rt.deliver(rid),rt.deliver_digest()):
        assert result[0]['status']=='failed'
    assert not rt.store.get_setting('push_cursor_1')


def test_split_messages_keep_url_on_first_line_of_every_part():
    from news_admin.push import send
    import httpx
    payloads=[]
    text='https://news.test/briefs/7\n\n'+'\n\n'.join(f'{i}. '+ '标题'*35 +'｜评级 A｜权重 10.00' for i in range(70))
    def handler(request):
        payloads.append(json.loads(request.content)['text'])
        return httpx.Response(200,json={'ok':True})
    send('telegram',{'bot_token':'x','chat_id':'1'},text,transport=httpx.MockTransport(handler))
    assert len(payloads)>1
    assert all(p.splitlines()[0]=='https://news.test/briefs/7' and len(p)<=3800 for p in payloads)



def test_push_snapshots_survive_backup_and_clear_with_runs(tmp_path):
    from tests.test_data_ops import runtime, seed
    from news_admin.data_ops import export_backup, restore_backup, clear_data
    rt=runtime(tmp_path);_,_,rid=seed(rt)
    key=rt.store.save_push_brief(rid,rt.store.load_events(rid))
    payload=export_backup(rt)
    clear_data(rt)
    assert rt.store.get_push_brief(key) is None
    restore_backup(rt,payload)
    assert len(rt.store.get_push_brief(key)['events'])==1
    rt.store.delete_run(rid)
    assert rt.store.get_push_brief(key) is None


def test_old_backup_without_push_table_still_restores(tmp_path):
    from tests.test_data_ops import runtime, seed
    from news_admin.data_ops import export_backup, restore_backup
    from news_admin.store import Store
    rt=runtime(tmp_path);_,_,rid=seed(rt)
    with rt.store._conn() as c:
        c.execute('DROP TABLE push_briefs')
    payload=export_backup(rt)
    rt.store=Store(rt.store.path)
    restore_backup(rt,payload)
    assert rt.store.get_run(rid)['status']=='completed'


def test_public_url_is_configurable_and_persistent(tmp_path):
    rt,client=make_client(tmp_path)
    client.post('/admin/settings/models',data={'public_base_url':'https://news.test'})
    assert rt.settings.public_base_url=='https://news.test'
    assert 'NEWS_PUBLIC_BASE_URL=https://news.test' in rt.settings.env_path.read_text()
    for bad in ('bad url','https://news.test/#digest','https://news.test/?q=x','https://user:pw@news.test','https://news.test:abc','https://news.test:99999'):
        client.post('/admin/settings/models',data={'public_base_url':bad})
        assert rt.settings.public_base_url=='https://news.test'
        import pytest
        with pytest.raises(ValueError):
            render_push([],bad+'/briefs/1')
