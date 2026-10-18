from datetime import datetime, timedelta, timezone

import pytest
from bs4 import BeautifulSoup

from news_admin import tz
from tests.test_feed import ev, save
from tests.test_web import make_client


@pytest.mark.parametrize('range_, labels', [
    ('today', ['14:00', '00:00']),
    ('3d', ['2026-10-17', '2026-10-16', '2026-10-15']),
    ('7d', ['2026-10-17', '2026-10-16', '2026-10-15', '2026-10-11']),
])
def test_home_timeline_groups_by_local_hour_or_day(tmp_path, monkeypatch, range_, labels):
    now = datetime(2026, 10, 17, 15, tzinfo=tz.TZ)
    monkeypatch.setattr(tz, 'now', lambda: now)
    rt, client = make_client(tmp_path)
    updated = ev('updated', '更新报道', now - timedelta(days=1))
    updated.articles[0].updated_at = now - timedelta(minutes=10)
    save(rt.store, [
        updated,
        ev('same-hour', '同小时报道', now - timedelta(minutes=30)),
        ev('midnight', '零点报道', datetime(2026, 10, 16, 16, 30, tzinfo=timezone.utc)),
        ev('yesterday', '前一日', now - timedelta(days=1)),
        ev('third', '第三天', now - timedelta(days=2)),
        ev('seventh', '第七天', now - timedelta(days=6)),
    ])
    soup = BeautifulSoup(client.get('/', params={'range': range_}).text, 'html.parser')
    timeline = soup.select_one('.news-timeline')
    assert timeline is not None
    assert [node.get_text(strip=True) for node in timeline.select('.timeline-label')] == labels
    expected = rt.store.feed(range_=range_).events
    assert [node['id'] for node in timeline.select('.news-card')] == [f'e-{event.id}' for event in expected]
    assert timeline.select_one('.news-card')['id'] == 'e-updated'


@pytest.mark.parametrize('range_, sort', [('all', 'time'), ('today', 'score'), ('3d', 'score'), ('7d', 'score')])
def test_home_timeline_is_absent_for_other_views(tmp_path, range_, sort):
    rt, client = make_client(tmp_path)
    save(rt.store, [ev('one', '新闻', tz.now())])
    soup = BeautifulSoup(client.get('/', params={'range': range_, 'sort': sort}).text, 'html.parser')
    assert soup.select_one('.news-card') is not None
    assert soup.select_one('.news-timeline') is None
    assert soup.select_one('.timeline-label') is None


def test_timeline_keeps_pagination_search_and_empty_state(tmp_path, monkeypatch):
    now = datetime(2026, 10, 17, 15, tzinfo=tz.TZ)
    monkeypatch.setattr(tz, 'now', lambda: now)
    rt, client = make_client(tmp_path)
    save(rt.store, [ev(f'e{i}', f'测试新闻{i}', now - timedelta(minutes=i)) for i in range(25)])
    params = {'range': 'today', 'q': '测试', 'source': 'BBC', 'topic': 'economy'}
    first = BeautifulSoup(client.get('/', params=params).text, 'html.parser')
    second = BeautifulSoup(client.get('/', params={**params, 'page': 2}).text, 'html.parser')
    assert len(first.select('.news-card')) == 20
    assert len(second.select('.news-card')) == 5
    assert second.select_one('.timeline-label').get_text(strip=True) == '14:00'
    next_page = first.select_one('a[rel="next"]')
    assert next_page is not None
    linked = BeautifulSoup(client.get(next_page['href']).text, 'html.parser')
    assert [node['id'] for node in linked.select('.news-card')] == [node['id'] for node in second.select('.news-card')]
    assert linked.select_one('mark').get_text() == '测试'
    empty = BeautifulSoup(client.get('/', params={'range': 'today', 'q': '不存在'}).text, 'html.parser')
    assert empty.select_one('.news-timeline') is None
    assert '没有符合条件的新闻' in empty.get_text()


def test_timeline_uses_indexed_fallback_time_for_undated_events(tmp_path, monkeypatch):
    now = datetime(2026, 10, 17, 15, tzinfo=tz.TZ)
    monkeypatch.setattr(tz, 'now', lambda: now)
    rt, client = make_client(tmp_path)
    save(rt.store, [ev('undated', '未注明时间', None)])
    soup = BeautifulSoup(client.get('/?range=today').text, 'html.parser')
    assert soup.select_one('.timeline-label').get_text(strip=True) == '15:00'
