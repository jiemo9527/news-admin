from datetime import datetime, timedelta, timezone

import httpx

from news_admin import tz
from news_admin.fetch import Source, fetch_source
from news_admin.models import Article


def test_now_and_iso_are_beijing_time():
    assert tz.now().utcoffset() == timedelta(hours=8)
    assert tz.iso(datetime(2026, 10, 17, 4, 0, tzinfo=timezone.utc)) == '2026-10-17T12:00:00+08:00'
    assert tz.iso(datetime(2026, 10, 17, 12, 0)) == '2026-10-17T12:00:00+08:00'  # 无时区 = 北京时间


def test_day_start_and_fetch_window():
    now = datetime(2026, 10, 17, 1, 30, tzinfo=tz.TZ)
    assert tz.day_start(now) == datetime(2026, 10, 17, tzinfo=tz.TZ)
    assert tz.day_start(now, days_ago=2) == datetime(2026, 10, 15, tzinfo=tz.TZ)
    assert tz.fetch_since('today_yesterday', now) == datetime(2026, 10, 16, tzinfo=tz.TZ)
    assert tz.fetch_since('24h', now) == now - timedelta(hours=24)


def test_models_keep_times_in_beijing_time():
    a = Article(id='a', source='s', url='u', title='t', published_at='2026-10-17T04:00:00Z', time_precision='exact')
    assert a.published_at.isoformat() == '2026-10-17T12:00:00+08:00'
    assert '2026-10-17T12:00:00+08:00' in a.model_dump_json()


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Yesterday early Beijing</title><link>https://n.test/a</link><description>a</description><pubDate>Thu, 15 Oct 2026 20:00:00 GMT</pubDate></item>
<item><title>Day before yesterday late</title><link>https://n.test/b</link><description>b</description><pubDate>Thu, 15 Oct 2026 15:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_fetch_keeps_items_since_start_of_yesterday_beijing_time():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text=RSS) if r.url.path == '/rss' else httpx.Response(404))
    source = Source(id=1, name='T', feed_url='https://n.test/rss', fetch_body=False)
    now = datetime(2026, 10, 17, 9, tzinfo=tz.TZ)
    result = fetch_source(source, now=now, since=tz.fetch_since('today_yesterday', now), transport=transport)
    assert [a.title for a in result.articles] == ['Yesterday early Beijing']
    assert result.articles[0].published_at.isoformat() == '2026-10-16T04:00:00+08:00'
