"""推送逻辑：抓取与推送独立；同一渠道同一事件只推送一次；渠道可编辑。"""
import threading
from datetime import timedelta

from fastapi.testclient import TestClient

from news_admin import runtime as runtime_mod, tz
from news_admin.models import EventRef
from news_admin.store import Store
from tests.test_digest import ev
from tests.test_web import make_client, make_viewer


def setup(tmp_path, monkeypatch):
    rt, client = make_client(tmp_path)
    rt.settings.public_base_url = 'https://news.test'
    rt.store.upsert_channel(None, 'telegram', 'tg', {'bot_token': 'TOKEN-SECRET', 'chat_id': '-1001', 'thread_id': ''})
    sent = []
    monkeypatch.setattr(runtime_mod, 'send', lambda kind, config, text, **kw: sent.append(text))
    return rt, client, sent


def add_run(rt, *events):
    rid = rt.store.start_run('schedule')
    rt.store.save_events(rid, list(events))
    rt.store.finish_run(rid, 'completed', {})
    return rid


def test_manual_push_is_not_repeated_by_scheduled_digest(tmp_path, monkeypatch):
    rt, _, sent = setup(tmp_path, monkeypatch)
    first = add_run(rt, ev('1', '手动已推', 'economy', score=11))
    rt.deliver(first)
    assert '手动已推' in sent[-1]
    add_run(rt, ev('2', '新的新闻', 'economy', score=9))
    rt.deliver_digest()
    assert '新的新闻' in sent[-1] and '手动已推' not in sent[-1]


def test_updates_of_an_already_pushed_event_are_not_pushed_again(tmp_path, monkeypatch):
    rt, _, sent = setup(tmp_path, monkeypatch)
    r1 = add_run(rt, ev('a', '原始事件', 'economy', score=10))
    rt.deliver_digest()
    assert '原始事件' in sent[-1]
    update = ev('b', '原始事件（更多来源）', 'economy', score=12, update=True)
    update.previous = [EventRef(run_id=r1, event_id='a', headline='原始事件', relation='update')]
    add_run(rt, update, ev('c', '另一条', 'politics', score=8))
    sent.clear()
    rt.deliver_digest()
    assert len(sent) == 1 and '另一条' in sent[0] and '原始事件' not in sent[0]
    # 没有新内容时不发空消息
    sent.clear()
    rt.deliver_digest()
    assert sent == []


def test_running_collection_does_not_block_scheduled_push(tmp_path, monkeypatch):
    rt, _, sent = setup(tmp_path, monkeypatch)
    add_run(rt, ev('1', '已完成批次', 'economy'))
    holding, release = threading.Event(), threading.Event()

    def collecting():  # 模拟正在进行的长时间抓取（7 天低速可能持续很久）
        with rt.operation_lock, rt.pipeline._lock:
            holding.set()
            release.wait(10)

    worker = threading.Thread(target=collecting)
    worker.start()
    holding.wait(5)
    pusher = threading.Thread(target=rt.deliver_digest)
    pusher.start()
    pusher.join(3)
    finished = not pusher.is_alive()
    release.set(); worker.join(); pusher.join()
    assert finished, '推送被正在运行的抓取阻塞'
    assert '已完成批次' in sent[-1]


def test_digest_never_reaches_back_more_than_48_hours(tmp_path, monkeypatch):
    rt, _, sent = setup(tmp_path, monkeypatch)
    old = add_run(rt, ev('old', '三天前的旧闻', 'economy', score=12))
    with rt.store._conn() as conn:
        conn.execute('UPDATE runs SET finished_at=? WHERE id=?', (tz.iso(tz.now() - timedelta(days=3)), old))
    rt.store.set_setting('push_cursor_1', tz.iso(tz.now() - timedelta(days=5)))  # 渠道停用多天后重新启用
    add_run(rt, ev('new', '今天的新闻', 'economy', score=8))
    rt.deliver_digest()
    assert '今天的新闻' in sent[-1] and '三天前的旧闻' not in sent[-1]


def test_cli_run_only_collects_and_push_command_uses_digest(tmp_path, monkeypatch):
    from news_admin import __main__ as cli
    rt, _, sent = setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(runtime_mod, 'build_runtime', lambda *a, **k: rt)
    monkeypatch.setattr(rt.pipeline, 'run', lambda trigger: calls.append(trigger) or add_run(rt, ev('x', 'CLI 抓取', 'economy')))
    for argv in (['run'], ['run', '--no-push']):
        assert cli.main(argv) == 0
    assert calls == ['cli', 'cli'] and sent == []
    assert cli.main(['push']) == 0
    assert len(sent) == 1 and 'CLI 抓取' in sent[0]


def test_sent_history_is_backfilled_from_existing_deliveries(tmp_path):
    store = Store(tmp_path / 'n.db')
    rid = store.start_run('manual')
    store.save_events(rid, [ev('1', '早先手动推送', 'economy')])
    store.finish_run(rid, 'completed', {})
    store.upsert_channel(None, 'telegram', 'tg', {'bot_token': 't', 'chat_id': '1'})
    store.record_delivery(rid, 1, 'sent')
    with store._conn() as conn:
        conn.execute('DROP TABLE pushed_events')  # 模拟升级前的数据库
    reopened = Store(tmp_path / 'n.db')
    canonical = reopened._rows('SELECT canonical_id FROM events')[0]['canonical_id']
    assert reopened.pushed_canonical_ids(1) == {canonical}


def test_clear_and_retention_remove_push_history(tmp_path, monkeypatch):
    from news_admin.data_ops import clear_data
    rt, _, _ = setup(tmp_path, monkeypatch)
    rt.deliver(add_run(rt, ev('1', '新闻', 'economy')))
    assert rt.store.pushed_canonical_ids(1)
    with rt.store._conn() as conn:
        conn.execute('UPDATE pushed_events SET sent_at=?', (tz.iso(tz.now() - timedelta(days=200)),))
        conn.execute('UPDATE runs SET started_at=?', (tz.iso(tz.now() - timedelta(days=200)),))
    rt.store.cleanup(90)
    assert not rt.store.pushed_canonical_ids(1)
    rt.deliver(add_run(rt, ev('2', '新闻2', 'economy')))
    clear_data(rt)
    assert not rt.store.pushed_canonical_ids(1)


def test_old_backup_without_new_push_tables_restores(tmp_path):
    from news_admin.data_ops import export_backup, restore_backup
    from tests.test_data_ops import runtime, seed
    rt = runtime(tmp_path)
    _, _, rid = seed(rt)
    with rt.store._conn() as conn:
        conn.execute('DROP TABLE pushed_events')
        conn.execute('DROP TABLE push_briefs')
    payload = export_backup(rt)
    rt.store = Store(rt.store.path)
    restore_backup(rt, payload)
    assert rt.store.get_run(rid)['status'] == 'completed'
    assert rt.store.pushed_canonical_ids(1) == set()


# ---------- 渠道编辑 ----------

def test_channel_can_be_edited_without_retyping_or_exposing_secrets(tmp_path, monkeypatch):
    rt, client, _ = setup(tmp_path, monkeypatch)
    page = client.get('/admin/channels?edit=1').text
    assert 'value="-1001"' in page and 'TOKEN-SECRET' not in page and '已设置' in page
    client.post('/admin/channels/1', data={'name': '新闻群', 'chat_id': '-1002', 'bot_token': '', 'thread_id': '', 'enabled': '1', 'kind': 'dingtalk'})
    channel = rt.store.get_channel(1)
    assert channel['name'] == '新闻群' and channel['kind'] == 'telegram'
    assert channel['config'] == {'bot_token': 'TOKEN-SECRET', 'chat_id': '-1002', 'thread_id': ''}
    client.post('/admin/channels/1', data={'name': '新闻群', 'chat_id': '', 'bot_token': '', 'enabled': '1'})
    assert rt.store.get_channel(1)['config']['chat_id'] == '-1002'  # 必填项不能被清空
    client.post('/admin/channels/1/toggle')
    assert not rt.store.get_channel(1)['enabled']
    assert 'TOKEN-SECRET' not in client.get('/admin/channels').text


def test_new_channel_requires_required_fields(tmp_path, monkeypatch):
    rt, client, _ = setup(tmp_path, monkeypatch)
    client.post('/admin/channels', data={'kind': 'telegram', 'name': 'x', 'bot_token': 'abc', 'chat_id': ''})
    assert len(rt.store.list_channels()) == 1
    client.post('/admin/channels', data={'kind': 'telegram', 'name': 'x', 'bot_token': 'abc', 'chat_id': '-1003', 'enabled': '1'})
    assert len(rt.store.list_channels()) == 2


def test_channel_test_send_and_delete_cleans_push_state(tmp_path, monkeypatch):
    rt, client, sent = setup(tmp_path, monkeypatch)
    client.post('/admin/channels/1/test')
    assert len(sent) == 1 and '测试' in sent[0]
    rt.deliver(add_run(rt, ev('1', '新闻', 'economy')))
    rt.store.set_setting('push_cursor_1', tz.iso())
    client.post('/admin/channels/1/delete')
    assert rt.store.get_channel(1) is None
    assert not rt.store.get_setting('push_cursor_1') and not rt.store.pushed_canonical_ids(1)


def test_viewer_sees_channels_without_secrets_and_cannot_edit(tmp_path, monkeypatch):
    rt, client, sent = setup(tmp_path, monkeypatch)
    viewer = make_viewer(client)
    page = viewer.get('/admin/channels?edit=1').text
    assert 'TOKEN-SECRET' not in page and 'value="-1001"' not in page
    for path in ('/admin/channels/1', '/admin/channels/1/toggle', '/admin/channels/1/test', '/admin/channels/1/delete'):
        assert viewer.post(path, data={'chat_id': '9'}).status_code == 403
    assert rt.store.get_channel(1)['config']['chat_id'] == '-1001' and sent == []


def test_manual_push_button_requires_confirmation_text(tmp_path, monkeypatch):
    rt, client, _ = setup(tmp_path, monkeypatch)
    rid = add_run(rt, ev('1', '新闻', 'economy'))
    page = client.get(f'/admin/runs/{rid}').text
    assert '手动推送本期' in page and 'confirm(' in page
    guest = TestClient(client.app)
    assert guest.post(f'/admin/runs/{rid}/deliver', follow_redirects=False).status_code == 303


def test_backfill_pairs_each_delivery_with_its_own_snapshot(tmp_path):
    store = Store(tmp_path / 'n.db')
    rid = store.start_run('manual')
    store.save_events(rid, [ev('a', '发给A', 'economy'), ev('b', '发给B', 'economy'), ev('c', '只在简报', 'economy')])
    store.finish_run(rid, 'completed', {})
    for name in ('A', 'B', 'C'):
        store.upsert_channel(None, 'telegram', name, {'bot_token': 't', 'chat_id': name})
    canon = {r['id']: r['canonical_id'] for r in store._rows('SELECT id, canonical_id FROM events')}
    events = {e.id: e for e in store.load_events(rid)}
    t0 = tz.now() - timedelta(hours=1)
    rows = [  # 旧版行为：每个渠道先存快照再发送；渠道 C 是手动推送整期（没有快照）
        ('snap', [events['c']], t0 - timedelta(minutes=4)),                                 # 演练快照，没有对应推送
        ('snap', [events['a']], t0), ('dlv', (1, 'sent'), t0 + timedelta(seconds=40)),
        ('snap', [events['b']], t0 + timedelta(minutes=4)), ('dlv', (2, 'sent'), t0 + timedelta(minutes=4, seconds=5)),
        ('snap', [events['a'], events['b']], t0 + timedelta(minutes=5)), ('dlv', (1, 'failed'), t0 + timedelta(minutes=5, seconds=5)),
        ('dlv', (3, 'sent'), t0 + timedelta(minutes=30)),
    ]
    with store._conn() as conn:
        conn.execute('DELETE FROM pushed_events')
    for kind, data, when in rows:
        if kind == 'snap':
            key = store.save_push_brief(rid, data)
            with store._conn() as conn:
                conn.execute('UPDATE push_briefs SET created_at=? WHERE id=?', (tz.iso(when), key))
        else:
            store.record_delivery(rid, data[0], data[1])
            with store._conn() as conn:
                conn.execute('UPDATE deliveries SET created_at=? WHERE id=(SELECT MAX(id) FROM deliveries)', (tz.iso(when),))
    store._backfill_pushed_events()
    assert store.pushed_canonical_ids(1) == {canon['a']}
    assert store.pushed_canonical_ids(2) == {canon['b']}
    assert store.pushed_canonical_ids(3) == {canon['a'], canon['b'], canon['c']}


def test_restoring_old_backup_backfills_history(tmp_path):
    from news_admin.data_ops import export_backup, restore_backup
    from tests.test_data_ops import runtime, seed
    rt = runtime(tmp_path)
    _, _, rid = seed(rt)
    rt.store.upsert_channel(None, 'telegram', 'tg', {'bot_token': 't', 'chat_id': '1'})
    rt.store.record_delivery(rid, 1, 'sent')
    with rt.store._conn() as conn:
        conn.execute('DROP TABLE pushed_events')
    payload = export_backup(rt)
    rt.store = Store(rt.store.path)
    with rt.store._conn() as conn:
        conn.execute('DELETE FROM pushed_events')
    restore_backup(rt, payload)
    assert rt.store.pushed_canonical_ids(1)


def test_retention_keeps_history_while_event_still_exists(tmp_path, monkeypatch):
    rt, _, _ = setup(tmp_path, monkeypatch)
    rid = add_run(rt, ev('1', '长期事件', 'economy'))
    rt.deliver(rid)
    with rt.store._conn() as conn:
        conn.execute('UPDATE pushed_events SET sent_at=?', (tz.iso(tz.now() - timedelta(days=200)),))
    rt.store.cleanup(90)
    assert rt.store.pushed_canonical_ids(1)


def test_bookkeeping_failure_after_successful_send_is_not_reported_as_failure(tmp_path, monkeypatch):
    rt, _, sent = setup(tmp_path, monkeypatch)
    add_run(rt, ev('1', '已发出', 'economy'))
    monkeypatch.setattr(runtime_mod.time if hasattr(runtime_mod, 'time') else __import__('time'), 'sleep', lambda *_: None)
    monkeypatch.setattr(rt.store, 'record_delivery', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db locked')))
    results = rt.deliver_digest()
    assert results[0]['status'] == 'sent' and len(sent) == 1
    sent.clear()
    rt.deliver_digest()
    assert sent == []


def test_concurrent_push_from_another_process_is_skipped(tmp_path, monkeypatch):
    rt, _, sent = setup(tmp_path, monkeypatch)
    add_run(rt, ev('1', '新闻', 'economy'))
    other = Store(rt.store.path)              # 模拟命令行 push 的另一个进程
    token = other.acquire_lease('push')
    assert rt.deliver_digest()[0]['status'] == 'busy' and sent == []
    other.release_lease('push', token)
    assert rt.deliver_digest()[0]['status'] == 'sent'


def test_manual_and_scheduled_push_are_mutually_exclusive_until_history_is_recorded(tmp_path, monkeypatch):
    rt, _, _ = setup(tmp_path, monkeypatch)
    rid = add_run(rt, ev('1', '同一条', 'economy'))
    sent, in_send, release = [], threading.Event(), threading.Event()

    def slow_send(kind, config, text, **kw):
        in_send.set(); release.wait(5); sent.append(text)
    monkeypatch.setattr(runtime_mod, 'send', slow_send)
    manual = threading.Thread(target=rt.deliver, args=(rid,))
    manual.start(); in_send.wait(5)
    other = Store(rt.store.path)                      # 其他进程此时拿不到推送租约
    assert other.acquire_lease('push') is None
    scheduled = threading.Thread(target=rt.deliver_digest)
    scheduled.start()
    release.set(); manual.join(); scheduled.join()
    assert sum('同一条' in t for t in sent) == 1


def test_lease_is_renewed_and_only_owner_can_renew(tmp_path):
    store = Store(tmp_path / 'n.db')
    token = store.acquire_lease('push', seconds=1)
    assert store.acquire_lease('push') is None
    assert store.acquire_lease('push', token=token) == token
    store.release_lease('push', token)
    assert store.acquire_lease('push') is not None


def test_lost_lease_stops_sending(tmp_path, monkeypatch):
    rt, _, sent = setup(tmp_path, monkeypatch)
    rt.store.upsert_channel(None, 'telegram', 'tg2', {'bot_token': 't', 'chat_id': '2'})
    add_run(rt, ev('1', '新闻', 'economy'))
    original = runtime_mod.send

    def steal_after_first(kind, config, text, **kw):
        original(kind, config, text, **kw)
        with rt.store._conn() as conn:  # 模拟租约被其他进程接管
            conn.execute("UPDATE settings SET value=? WHERE key='lease_push'", (tz.iso(tz.now() + timedelta(hours=1)) + '|other',))
    monkeypatch.setattr(runtime_mod, 'send', steal_after_first)
    results = rt.deliver_digest()
    assert len(sent) == 1 and results[-1]['status'] == 'busy'
