"""个人阅读状态：已读/未读、收藏、关键词历史，以及 24 小时无新内容提醒。"""
import json
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from news_admin import tz
from tests.test_feed import ev, save
from tests.test_web import make_client, make_viewer

NOW = datetime(2026, 10, 17, 15, tzinfo=tz.TZ)
FETCH = {'X-Requested-With': 'fetch'}


def fixed_now(monkeypatch, now=NOW):
    monkeypatch.setattr(tz, 'now', lambda: now)


def cards(html):
    return [node['id'][2:] for node in BeautifulSoup(html, 'html.parser').select('.news-card')]


def canonical(rt, event_id):
    return rt.store._rows('SELECT canonical_id FROM events WHERE id=? ORDER BY run_id DESC LIMIT 1', (event_id,))[0]['canonical_id']


def test_read_toggle_unread_filter_and_three_day_auto_read(tmp_path, monkeypatch):
    fixed_now(monkeypatch)
    rt, client = make_client(tmp_path)
    save(rt.store, [ev('new', '新消息', NOW - timedelta(hours=1)), ev('mid', '前天消息', NOW - timedelta(days=2)),
                    ev('old', '四天前消息', NOW - timedelta(days=4))])
    soup = BeautifulSoup(client.get('/').text, 'html.parser')
    assert soup.select_one('#e-new')['class'] == ['news-card', 'is-unread']
    assert soup.select_one('#e-old')['class'] == ['news-card', 'is-read']
    assert soup.select_one('#e-old form[data-card-action="read"] button').has_attr('disabled')
    assert cards(client.get('/?unread=1').text) == ['new', 'mid']
    assert '只看未读' in client.get('/').text

    response = client.post('/me/read', data={'canonical': canonical(rt, 'new'), 'value': '1'}, headers=FETCH)
    assert response.json() == {'ok': True, 'value': True}
    assert cards(client.get('/?unread=1').text) == ['mid']
    client.post('/me/read', data={'canonical': canonical(rt, 'new'), 'value': '0'}, headers=FETCH)
    assert cards(client.get('/?unread=1').text) == ['new', 'mid']
    assert client.post('/me/read', data={'canonical': 'missing', 'value': '1'}, headers=FETCH).status_code == 404


def test_read_state_is_per_user_and_viewer_can_use_personal_actions(tmp_path, monkeypatch):
    fixed_now(monkeypatch)
    rt, client = make_client(tmp_path)
    save(rt.store, [ev('a', '新闻A', NOW - timedelta(hours=1))])
    viewer = make_viewer(client)
    cid = canonical(rt, 'a')
    assert viewer.post('/me/read', data={'canonical': cid, 'value': '1'}, headers=FETCH).status_code == 200
    assert viewer.post('/me/favorite', data={'canonical': cid, 'value': '1'}, headers=FETCH).status_code == 200
    assert cards(viewer.get('/?unread=1').text) == []
    assert cards(client.get('/?unread=1').text) == ['a']
    assert cards(viewer.get('/favorites').text) == ['a'] and cards(client.get('/favorites').text) == []
    assert viewer.post('/admin/run').status_code == 403


def test_long_press_marks_current_order_above_including_item_across_pages(tmp_path, monkeypatch):
    fixed_now(monkeypatch)
    rt, client = make_client(tmp_path)
    first = save(rt.store, [ev(f'e{i}', f'新闻{i}', NOW - timedelta(minutes=i), score=i) for i in range(25)])
    data = {'canonical': canonical(rt, 'e21'), 'max_run': str(first), 'range': 'today'}
    assert client.post('/me/read/upto', data=data, headers=FETCH).json() == {'ok': True, 'count': 22}
    assert cards(client.get('/?unread=1&range=today').text) == ['e22', 'e23', 'e24']

    rt2, client2 = make_client(tmp_path / 'score')
    run = save(rt2.store, [ev(f'e{i}', f'新闻{i}', NOW - timedelta(minutes=i), score=i) for i in range(5)])
    later = save(rt2.store, [ev('late', '页面打开后新到', NOW, score=99)])
    data = {'canonical': canonical(rt2, 'e2'), 'max_run': str(run), 'sort': 'score'}
    assert client2.post('/me/read/upto', data=data, headers=FETCH).json() == {'ok': True, 'count': 3}
    assert cards(client2.get('/?unread=1&sort=score').text) == ['late', 'e1', 'e0']
    assert later > run
    missing = client2.post('/me/read/upto', data={**data, 'canonical': 'missing'}, headers=FETCH)
    assert missing.status_code == 404


def test_feed_has_long_press_target_and_max_run(tmp_path, monkeypatch):
    fixed_now(monkeypatch)
    rt, client = make_client(tmp_path)
    run = save(rt.store, [ev('a', '新闻A', NOW)])
    results = BeautifulSoup(client.get('/').text, 'html.parser').select_one('[data-live="results"] [data-upto]')
    assert results is not None and results['data-max-run'] == str(run) and results['data-at'] == tz.iso(NOW)


def test_long_press_uses_page_load_snapshot_time_and_stable_ties(tmp_path, monkeypatch):
    fixed_now(monkeypatch)
    rt, client = make_client(tmp_path)
    # 同时间、同分数：首页顺序与长按计算顺序必须一致（按唯一键兜底）
    run = save(rt.store, [ev(f't{i}', f'同分{i}', NOW - timedelta(hours=1), score=5) for i in range(6)])
    shown = cards(client.get('/').text)
    assert shown == sorted(shown, key=lambda e: canonical(rt, e), reverse=True)
    target = shown[2]
    # 页面在午夜前打开，午夜后才长按：仍按打开时的「今天」计算
    fixed_now(monkeypatch, NOW.replace(hour=23, minute=59))
    late_page = BeautifulSoup(client.get('/?range=today').text, 'html.parser').select_one('[data-upto]')
    fixed_now(monkeypatch, NOW.replace(hour=23, minute=59) + timedelta(minutes=5))
    data = {'canonical': canonical(rt, target), 'max_run': late_page['data-max-run'], 'at': late_page['data-at'], 'range': 'today'}
    assert client.post('/me/read/upto', data=data, headers=FETCH).json() == {'ok': True, 'count': 3}
    assert cards(client.get('/?unread=1').text) == shown[3:]
    bad = client.post('/me/read/upto', data={**data, 'at': 'not-a-time', 'range': 'all'}, headers=FETCH)
    assert bad.status_code == 200  # 无效时间退回服务器当前时间，不报错
    future = client.post('/me/read/upto', data={**data, 'at': tz.iso(NOW + timedelta(days=30))}, headers=FETCH)
    assert future.status_code == 404  # 不信任未来时间：退回当前时间（已是第二天），「今天」里找不到该条
    assert run


def test_feed_shown_is_bounded_by_the_same_run_as_long_press(tmp_path, monkeypatch):
    fixed_now(monkeypatch)
    rt, _ = make_client(tmp_path)
    first = save(rt.store, [ev('a', '新闻A', NOW - timedelta(minutes=5))])
    save(rt.store, [ev('b', '新闻B', NOW)])
    assert [e.id for e in rt.store.feed(max_run=first).events] == ['a']
    assert [e.id for e in rt.store.feed().events] == ['b', 'a']


def test_explicitly_read_old_event_stays_read(tmp_path, monkeypatch):
    fixed_now(monkeypatch)
    rt, client = make_client(tmp_path)
    save(rt.store, [ev('old', '四天前', NOW - timedelta(days=4))])
    client.post('/me/read', data={'canonical': canonical(rt, 'old'), 'value': '1'}, headers=FETCH)
    button = BeautifulSoup(client.get('/').text, 'html.parser').select_one('#e-old form[data-card-action="read"] button')
    assert button.has_attr('disabled') and button.get_text(strip=True) == '已读'


def test_brief_and_push_snapshot_mark_all_read(tmp_path, monkeypatch):
    fixed_now(monkeypatch)
    rt, client = make_client(tmp_path)
    run = save(rt.store, [ev('a', '新闻A', NOW), ev('b', '新闻B', NOW - timedelta(minutes=1))])
    other = save(rt.store, [ev('c', '新闻C', NOW - timedelta(minutes=2))])
    brief = client.get(f'/briefs/{run}').text
    assert f'action="/me/read/brief/{run}"' in brief and '全部标为已读' in brief
    client.post(f'/me/read/brief/{run}', headers={'referer': f'http://testserver/briefs/{run}'})
    assert cards(client.get('/?unread=1').text) == ['c']

    key = rt.store.save_push_brief(other, rt.store.load_events(other))
    assert f'action="/me/read/push/{key}"' in client.get(f'/briefs/push/{key}').text
    response = client.post(f'/me/read/push/{key}', follow_redirects=False)
    assert response.status_code == 303 and cards(client.get('/?unread=1').text) == []
    assert client.post('/me/read/push/missing').status_code == 404


def test_favorites_toggle_and_list(tmp_path, monkeypatch):
    fixed_now(monkeypatch)
    rt, client = make_client(tmp_path)
    save(rt.store, [ev('a', '新闻A', NOW), ev('b', '新闻B', NOW - timedelta(minutes=1))])
    assert '还没有收藏' in client.get('/favorites').text
    for event_id in ('b', 'a'):
        assert client.post('/me/favorite', data={'canonical': canonical(rt, event_id), 'value': '1'}, headers=FETCH).json()['value'] is True
    assert cards(client.get('/favorites').text) == ['a', 'b']
    soup = BeautifulSoup(client.get('/').text, 'html.parser')
    assert soup.select_one('#e-a form[data-card-action="favorite"] button')['aria-pressed'] == 'true'
    assert 'href="/favorites"' in client.get('/').text
    client.post('/me/favorite', data={'canonical': canonical(rt, 'a'), 'value': '0'}, headers=FETCH)
    assert cards(client.get('/favorites').text) == ['b']


def test_personal_actions_without_javascript_redirect_back_safely(tmp_path, monkeypatch):
    fixed_now(monkeypatch)
    rt, client = make_client(tmp_path)
    save(rt.store, [ev('a', '新闻A', NOW)])
    data = {'canonical': canonical(rt, 'a'), 'value': '1'}
    local = client.post('/me/favorite', data=data, headers={'referer': 'http://testserver/?range=today'}, follow_redirects=False)
    assert local.headers['location'] == '/?range=today'
    external = client.post('/me/read', data=data, headers={'referer': 'https://evil.test/x'}, follow_redirects=False)
    assert external.headers['location'] == '/'


def test_search_history_saves_collapses_typing_limits_and_clears(tmp_path):
    rt, client = make_client(tmp_path)
    for query in ('美', '美联', '美联储', '  日本   地震 ', '<script>alert(1)</script>'):
        response = client.post('/me/search-history', data={'q': query}, headers=FETCH)
    assert response.json()['items'] == ['<script>alert(1)</script>', '日本 地震', '美联储']
    html = client.get('/').text
    assert html.count('<script>alert') == 0 and 'data-q="日本 地震"' in html and '最近搜索' in html
    for i in range(25):
        client.post('/me/search-history', data={'q': f'关键词{i:02d}'}, headers=FETCH)
    items = client.post('/me/search-history', data={'q': '最后'}, headers=FETCH).json()['items']
    assert len(items) == 20 and items[0] == '最后' and '关键词05' not in items
    assert client.post('/me/search-history/clear', headers=FETCH).json() == {'items': []}
    assert 'data-q=' not in client.get('/').text


def test_search_history_is_per_user(tmp_path):
    rt, client = make_client(tmp_path)
    viewer = make_viewer(client)
    viewer.post('/me/search-history', data={'q': '只读用户关键词'}, headers=FETCH)
    assert '只读用户关键词' in viewer.get('/').text and '只读用户关键词' not in client.get('/').text


def test_stale_warning_appears_after_24_hours_without_new_content(tmp_path, monkeypatch):
    rt, client = make_client(tmp_path)
    save(rt.store, [ev('a', '新闻A', NOW)])
    warning = lambda: BeautifulSoup(client.get('/').text, 'html.parser').select_one('[data-autohide="5000"]')
    assert warning() is None
    with rt.store._conn() as conn:
        conn.execute('UPDATE events SET created_at=?', (tz.iso(tz.now() - timedelta(hours=25)),))
    toast = warning()
    assert toast is not None and '24 小时没有新内容' in toast.get_text() and 'toast-warn' in toast['class']
    assert client.get('/briefs').text.count('24 小时没有新内容') == 0


def test_user_state_cleanup_clear_delete_and_old_backup_restore(tmp_path, monkeypatch):
    from news_admin.data_ops import clear_data, export_backup, restore_backup
    fixed_now(monkeypatch)
    rt, client = make_client(tmp_path)
    save(rt.store, [ev('a', '新闻A', NOW)])
    viewer = make_viewer(client)
    cid = canonical(rt, 'a')
    for c in (client, viewer):
        c.post('/me/read', data={'canonical': cid, 'value': '1'}, headers=FETCH)
        c.post('/me/favorite', data={'canonical': cid, 'value': '1'}, headers=FETCH)
        c.post('/me/search-history', data={'q': '测试'}, headers=FETCH)
    reader = rt.store.get_user_by_name('reader')
    rt.store.delete_user(reader['id'])
    for table in ('read_events', 'favorites', 'search_history'):
        assert rt.store._rows(f'SELECT COUNT(*) AS n FROM {table}')[0]['n'] == 1

    with rt.store._conn() as conn:
        conn.execute('UPDATE runs SET started_at=?', (tz.iso(NOW - timedelta(days=100)),))
    rt.store.cleanup(retention_days=90, now=NOW)
    assert rt.store._rows('SELECT COUNT(*) AS n FROM read_events')[0]['n'] == 0
    assert rt.store._rows('SELECT COUNT(*) AS n FROM favorites')[0]['n'] == 0

    save(rt.store, [ev('b', '新闻B', NOW)])
    client.post('/me/read', data={'canonical': canonical(rt, 'b'), 'value': '1'}, headers=FETCH)
    clear_data(rt)
    for table in ('read_events', 'favorites', 'search_history'):
        assert rt.store._rows(f'SELECT COUNT(*) AS n FROM {table}')[0]['n'] == 0

    save(rt.store, [ev('c', '新闻C', NOW)])
    with rt.store._conn() as conn:
        for table in ('read_events', 'favorites', 'search_history'):
            conn.execute(f'DROP TABLE {table}')
    payload = export_backup(rt)
    from news_admin.store import Store
    rt.store = Store(rt.store.path)
    restore_backup(rt, payload)
    fresh = TestClient(client.app)
    fresh.post('/login', data={'username': 'admin', 'password': 'pw'})
    assert fresh.post('/me/read', data={'canonical': canonical(rt, 'c'), 'value': '1'}, headers=FETCH).json()['ok']
    assert json.loads(json.dumps(fresh.post('/me/search-history', data={'q': '恢复后'}, headers=FETCH).json())) == {'items': ['恢复后']}
