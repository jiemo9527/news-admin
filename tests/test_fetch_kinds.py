from datetime import datetime, timezone

import httpx

from news_admin.fetch import Source, fetch_source

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)

JSON_FEED = {
    'version': 'https://jsonfeed.org/version/1',
    'items': [
        {
            'title': '主要開發銀行加大支持力度',
            'summary': 'Major development banks step up support for critical minerals value chains',
            'url': 'https://www.reuters.com/world/a-2026-10-17/',
            'date_published': '2026-10-17T04:32:32Z',
            '_original_published': '2026-10-17T04:14:56Z',
            '_translations': {'zh-Hans': {'title': '主要开发银行加大支持力度'}},
        },
        {'title': '旧闻', 'url': 'https://www.reuters.com/old/', 'date_published': '2024-01-01T00:00:00Z'},
    ],
}

CCTV_JSONP = 'world({"data":{"total":2,"list":[' \
    '{"title":"伊朗称油轮触雷爆炸","brief":"伊朗革命卫队海军17日称，一艘超级油轮在霍尔木兹海峡触雷后爆炸。","url":"https://news.cctv.com/2026/10/17/ARTIa.shtml","focus_date":"2026-10-17 11:34:13"},' \
    '{"title":"旧闻","brief":"x","url":"https://news.cctv.com/2024/01/01/ARTIb.shtml","focus_date":"2024-01-01 08:00:00"}]}})'

LIST_PAGE = """<html><body>
<a href="/news/world/story20261017-1">伊朗称油轮触雷</a>
<a href="/news/world/story20261017-1"><img alt="photo"></a>
<a href="/news/world/story20240101-2">旧闻标题足够长</a>
<a href="/about">关于我们</a>
</body></html>"""

ARTICLE_PAGE = """<html><head><title>伊朗称一油轮在霍尔木兹海峡触雷爆炸 | 联合早报</title>
<meta property="og:title" content="伊朗称一油轮在霍尔木兹海峡触雷爆炸">
<meta property="article:published_time" content="2026-10-17T03:00:00+08:00"></head>
<body><article><p>伊朗伊斯兰革命卫队海军说，一艘超级油轮在霍尔木兹海峡触雷后发生大规模爆炸并起火，船员已全部撤离，事故原因仍在调查。</p><p>伊朗方面表示，周边海域航运暂未中断，但已要求过往船只提高警惕，海事部门正在评估水雷来源和后续清理工作。</p></article></body></html>"""

OLD_ARTICLE = """<html><head><meta property="og:title" content="旧闻标题足够长">
<meta property="article:published_time" content="2024-01-01T03:00:00+08:00"></head><body><p>旧</p></body></html>"""


def handler(request):
    path = request.url.path
    if path == '/feed.json':
        return httpx.Response(200, json=JSON_FEED)
    if path == '/world_1.jsonp':
        return httpx.Response(200, text=CCTV_JSONP)
    if path == '/news/world':
        return httpx.Response(200, text=LIST_PAGE)
    if path == '/news/world/story20261017-1':
        return httpx.Response(200, text=ARTICLE_PAGE)
    if path == '/news/world/story20240101-2':
        return httpx.Response(200, text=OLD_ARTICLE)
    return httpx.Response(404)


def test_json_feed_prefers_simplified_translation_and_filters_old_items():
    source = Source(id=1, name='路透社', feed_url='https://buzzing.test/feed.json', kind='jsonfeed', fetch_body=False)
    result = fetch_source(source, now=NOW, transport=httpx.MockTransport(handler))
    assert result.error == ''
    assert [a.title for a in result.articles] == ['主要开发银行加大支持力度']
    article = result.articles[0]
    assert 'critical minerals' in article.body
    assert article.published_at == datetime(2026, 10, 17, 4, 14, 56, tzinfo=timezone.utc)


def test_cctv_jsonp_list_api():
    source = Source(id=2, name='央视新闻', feed_url='https://news.cctv.test/world_1.jsonp', kind='cctv', fetch_body=False)
    result = fetch_source(source, now=NOW, transport=httpx.MockTransport(handler))
    assert [a.title for a in result.articles] == ['伊朗称油轮触雷爆炸']
    assert result.articles[0].body.startswith('伊朗革命卫队海军17日称')
    assert result.articles[0].published_at == datetime(2026, 10, 17, 3, 34, 13, tzinfo=timezone.utc)


def test_html_list_page_uses_link_pattern_and_article_metadata():
    source = Source(id=3, name='联合早报', feed_url='https://zaobao.test/news/world', kind='html', link_pattern=r'/news/world/story\d+')
    result = fetch_source(source, now=NOW, transport=httpx.MockTransport(handler))
    assert result.error == ''
    assert [a.title for a in result.articles] == ['伊朗称一油轮在霍尔木兹海峡触雷爆炸']
    article = result.articles[0]
    assert article.url == 'https://zaobao.test/news/world/story20261017-1'
    assert '船员已全部撤离' in article.body
    assert article.published_at == datetime(2026, 10, 16, 19, 0, tzinfo=timezone.utc)


def test_html_source_without_pattern_is_reported():
    source = Source(id=4, name='x', feed_url='https://zaobao.test/news/world', kind='html')
    result = fetch_source(source, now=NOW, transport=httpx.MockTransport(handler))
    assert result.articles == [] and 'link_pattern' in result.error


def test_page_title_prefers_h1_and_strips_site_suffix_from_title_tag():
    from news_admin.fetch import _page_title
    from bs4 import BeautifulSoup

    people = BeautifulSoup('<html><head><title>中蒙第二条跨境铁路架梁全部完成--国际--人民网</title></head><body><h1></h1></body></html>', 'lxml')
    xinhua = BeautifulSoup('<html><head><title>美国AI投资潮引发三大风险-新华网</title></head><body><h1>新闻分析丨美国AI投资潮引发三大风险</h1></body></html>', 'lxml')
    column = BeautifulSoup('<html><head><title>认知工作室 | 巴黎国际车展折射欧洲车市“中国热”--国际--人民网</title></head><body></body></html>', 'lxml')
    assert _page_title(people) == '中蒙第二条跨境铁路架梁全部完成'
    assert _page_title(xinhua) == '新闻分析丨美国AI投资潮引发三大风险'
    assert _page_title(column) == '巴黎国际车展折射欧洲车市“中国热”'
