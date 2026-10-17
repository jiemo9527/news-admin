from datetime import timedelta
import httpx
from news_admin import tz, fetch
from news_admin.fetch import Source,fetch_source


def test_week_slow_fetch_ignores_source_item_limit(monkeypatch):
    monkeypatch.setattr(fetch.time,'sleep',lambda _:None)
    now=tz.now()
    rss='<rss><channel>'+''.join(f'<item><title>新闻{i}</title><link>https://n.test/{i}</link></item>' for i in range(25))+'</channel></rss>'
    source=Source(1,'test','https://n.test/rss',max_items=2,fetch_body=False)
    transport=httpx.MockTransport(lambda r:httpx.Response(200,text=rss))
    result=fetch_source(source,now=now,since=now-timedelta(days=7),slow=True,transport=transport)
    assert len(result.articles)==25
    assert source.max_items==2
    regular=fetch_source(source,now=now,transport=transport)
    assert len(regular.articles)==2



def test_week_jsonfeed_follows_next_url_deduplicates_and_stops_at_old_page(monkeypatch):
    monkeypatch.setattr(fetch.time,'sleep',lambda _:None)
    now=tz.now();seen=[]
    def handler(r):
        seen.append(r.url.path)
        page=int(r.url.path.strip('/') or '1')
        items=[{'url':f'https://n.test/article{page}','title':str(page),'date_published':(now-timedelta(days=page if page<3 else 9)).isoformat()}]
        return httpx.Response(200,json={'items':items,'next_url':f'https://n.test/{page+1}'})
    result=fetch_source(Source(1,'j','https://n.test/1',kind='jsonfeed',max_items=1,fetch_body=False),now=now,since=now-timedelta(days=7),slow=True,transport=httpx.MockTransport(handler))
    assert len(result.articles)==2
    assert seen==['/1','/2','/3']



def test_week_html_paginates_and_stops_on_repeated_articles(monkeypatch):
    monkeypatch.setattr(fetch.time,'sleep',lambda _:None)
    seen=[]
    def handler(r):
        seen.append(r.url.path)
        if r.url.path.startswith('/article'):
            return httpx.Response(200,text='<h1>标题'+r.url.path+'</h1>')
        n=1 if r.url.path=='/list' else 2
        article=1 if r.url.path=='/repeat' else n
        return httpx.Response(200,text=f'<a href="/article{article}">新闻</a><a href="/list2" rel="next">下一页</a>' if n==1 else '<a href="/article2">新闻</a><a href="/list2" rel="next">下一页</a>')
    result=fetch_source(Source(1,'h','https://n.test/list',kind='html',link_pattern=r'/article',max_items=1),slow=True,transport=httpx.MockTransport(handler))
    assert len(result.articles)==2 and seen==['/list','/list2','/article1','/article2']


def test_week_google_searches_each_day_not_when_one_day(monkeypatch):
    from urllib.parse import parse_qs
    monkeypatch.setattr(fetch.time,'sleep',lambda _:None)
    now=tz.now();queries=[]
    def handler(r):
        q=parse_qs(r.url.query.decode())['q'][0];queries.append(q)
        return httpx.Response(200,text=f'<rss><channel><item><title>A</title><link>https://n.test/{len(queries)}</link></item></channel></rss>')
    result=fetch_source(Source(1,'ap','https://news.google.com/rss/search?q=when:1d+site:apnews.com',fetch_body=False,max_items=1),now=now,since=now-timedelta(days=7),slow=True,transport=httpx.MockTransport(handler))
    assert len(queries)==8 and len(result.articles)==8
    assert all('when:1d' not in q and 'after:' in q and 'before:' in q for q in queries)


def test_week_pagination_rate_limit_keeps_list_data_without_more_requests(monkeypatch):
    monkeypatch.setattr(fetch.time,'sleep',lambda _:None)
    seen=[]
    def handler(r):
        seen.append(r.url.path)
        if r.url.path=='/1':
            return httpx.Response(200,json={'items':[{'url':'https://n.test/article','title':'A'}],'next_url':'/2'})
        return httpx.Response(429)
    result=fetch_source(Source(1,'j','https://n.test/1',kind='jsonfeed'),slow=True,transport=httpx.MockTransport(handler))
    assert seen==['/1','/2'] and len(result.articles)==1 and '429' in result.error


def test_week_rejects_off_site_next_and_regular_does_not_paginate(monkeypatch):
    monkeypatch.setattr(fetch.time,'sleep',lambda _:None)
    seen=[]
    def handler(r):
        seen.append(str(r.url))
        return httpx.Response(200,json={'items':[{'url':'https://n.test/a','title':'A'}],'next_url':'https://other.test/2'})
    source=Source(1,'j','https://n.test/1',kind='jsonfeed',fetch_body=False)
    for slow in (True,False):
        fetch_source(source,slow=slow,transport=httpx.MockTransport(handler))
    assert seen==['https://n.test/1']*2



def test_week_html_keeps_prior_list_when_next_is_blocked(monkeypatch):
    monkeypatch.setattr(fetch.time,'sleep',lambda _:None)
    seen=[]
    def handler(r):
        seen.append(r.url.path)
        return httpx.Response(200,text='<a href="/article1">有效新闻</a><a rel="next" href="/2">下一页</a>') if r.url.path=='/1' else httpx.Response(403)
    r=fetch_source(Source(1,'h','https://n.test/1',kind='html',link_pattern=r'/article'),slow=True,transport=httpx.MockTransport(handler))
    assert len(r.articles)==1 and seen==['/1','/2'] and '403' in r.error


def test_week_pagination_does_not_follow_off_site_redirect(monkeypatch):
    monkeypatch.setattr(fetch.time,'sleep',lambda _:None)
    seen=[]
    def handler(r):
        seen.append(str(r.url))
        if r.url.path=='/1':
            return httpx.Response(200,json={'items':[{'url':'https://n.test/a','title':'A'}],'next_url':'/2'})
        return httpx.Response(302,headers={'location':'https://other.test/steal'})
    r=fetch_source(Source(1,'j','https://n.test/1',kind='jsonfeed',fetch_body=False),slow=True,transport=httpx.MockTransport(handler))
    assert len(r.articles)==1 and not any('other.test' in u for u in seen)
