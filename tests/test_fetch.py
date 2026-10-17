from datetime import datetime, timezone

import httpx

from news_admin.fetch import Source, fetch_source

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Fed cuts rates by 25 basis points</title><link>https://news.test/a?utm_source=rss</link>
<description>&lt;p&gt;The Federal Reserve cut its benchmark rate on Wednesday.&lt;/p&gt;</description>
<pubDate>Fri, 17 Oct 2026 02:00:00 GMT</pubDate></item>
<item><title>Old story</title><link>https://news.test/old</link><description>old</description>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
</channel></rss>"""

ARTICLE = '<html><body><article><h1>Fed cuts</h1>' + '<p>The Federal Reserve cut its benchmark interest rate by 25 basis points on Wednesday, citing cooling inflation and a softer labour market across the country.</p>' * 3 + '</article></body></html>'


def handler(request):
    if request.url.path == '/feed':
        return httpx.Response(200, text=RSS)
    if request.url.path == '/a':
        return httpx.Response(200, text=ARTICLE)
    return httpx.Response(404)


def test_fetch_source_reads_rss_filters_old_items_and_extracts_body():
    source = Source(id=1, name='Test', feed_url='https://news.test/feed', weight=1.2)
    result = fetch_source(source, now=datetime(2026, 10, 17, 12, tzinfo=timezone.utc), transport=httpx.MockTransport(handler))
    assert result.error == ''
    assert [a.title for a in result.articles] == ['Fed cuts rates by 25 basis points']
    article = result.articles[0]
    assert article.url == 'https://news.test/a'
    assert 'labour market' in article.body
    assert article.source == 'Test' and article.source_weight == 1.2


def test_body_falls_back_to_feed_description_when_page_fails():
    def failing(request):
        return httpx.Response(200, text=RSS) if request.url.path == '/feed' else httpx.Response(500)

    result = fetch_source(Source(id=1, name='T', feed_url='https://news.test/feed'), now=datetime(2026, 10, 17, 12, tzinfo=timezone.utc), transport=httpx.MockTransport(failing))
    assert result.articles[0].body == 'The Federal Reserve cut its benchmark rate on Wednesday.'


def test_feed_error_is_reported_not_raised():
    result = fetch_source(Source(id=1, name='T', feed_url='https://news.test/missing'), transport=httpx.MockTransport(handler))
    assert result.articles == [] and '404' in result.error
