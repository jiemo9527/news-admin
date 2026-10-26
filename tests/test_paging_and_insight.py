"""首页每页条数、顶部分页，以及按需联网的文章分析。"""
import json
from datetime import timedelta

import httpx
from bs4 import BeautifulSoup

from news_admin import tz
from tests.test_feed import ev, save
from tests.test_web import make_client, make_viewer

FETCH = {'X-Requested-With': 'fetch'}


def cards(html):
    return BeautifulSoup(html, 'html.parser').select('.news-card')


def seed(rt, n=45):
    now = tz.now()
    return save(rt.store, [ev(f'e{i}', f'分页新闻{i}', now - timedelta(minutes=i)) for i in range(n)])


def canonical(rt, event_id):
    return rt.store._rows('SELECT canonical_id FROM events WHERE id=? ORDER BY run_id DESC LIMIT 1', (event_id,))[0]['canonical_id']


def test_per_page_choice_is_offered_on_top_and_bottom_and_remembered(tmp_path):
    rt, client = make_client(tmp_path)
    seed(rt)
    soup = BeautifulSoup(client.get('/', params={'per': 10}).text, 'html.parser')
    assert len(soup.select('.news-card')) == 10
    top, bottom = soup.select_one('nav.pager-top'), soup.select_one('nav[aria-label="分页"]')
    assert top is not None and bottom is not None and top is not bottom
    for nav in (top, bottom):
        options = {a.get_text(strip=True): a for a in nav.select('.per-page a')}
        assert list(options) == ['10', '20', '50']
        assert options['10']['aria-current'] == 'true' and 'per=50' in options['50']['href']
        assert 'per=20' in options['20']['href']  # 明确选择 20 也要写进链接，才能覆盖会话里的偏好
        assert 'per=10' in nav.select_one('a[rel="next"]')['href'] and 'page=2' in nav.select_one('a[rel="next"]')['href']
    assert soup.select_one('form[data-live-form] input[name="per"]')['value'] == '10'
    # 刷新或从导航回到首页时仍按上次的每页条数
    assert len(cards(client.get('/').text)) == 10
    assert len(cards(client.get('/', params={'per': 50}).text)) == 45
    assert len(cards(client.get('/', params={'per': 20}).text)) == 20
    assert len(cards(client.get('/').text)) == 20
    assert len(cards(client.get('/', params={'per': 7}).text)) == 20
    last = BeautifulSoup(client.get('/', params={'per': 10, 'page': 5}).text, 'html.parser')
    assert [c['id'] for c in last.select('.news-card')] == [f'e-e{i}' for i in range(40, 45)]


def test_pager_is_hidden_when_everything_fits_the_smallest_page(tmp_path):
    rt, client = make_client(tmp_path)
    seed(rt, 8)
    soup = BeautifulSoup(client.get('/').text, 'html.parser')
    assert soup.select('.pager') == [] and len(soup.select('.news-card')) == 8


def luna_response(text, urls=(), searched=True):
    output = [{'type': 'web_search_call', 'status': 'completed'}] if searched else []
    output.append({'type': 'message', 'content': [{'type': 'output_text', 'text': text,
                                                    'annotations': [{'type': 'url_citation', 'url': u, 'title': t} for t, u in urls]}]})
    return {'output': output, 'usage': {'total_tokens': 100}}


def test_analysis_button_uses_luna_web_search_and_caches(tmp_path):
    rt, client = make_client(tmp_path)
    seed(rt, 3)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=luna_response(
            '【核心事实】**美联储**降息。([fed.gov](https://www.federalreserve.gov/a))\n【最新进展】2026-10-26 暂无新消息。<script>x</script>',
            [('Fed', 'https://www.federalreserve.gov/a'), ('bad', 'javascript:alert(1)'), ('Fed', 'https://www.federalreserve.gov/a')]))

    rt.transport = httpx.MockTransport(handler)
    soup = BeautifulSoup(client.get('/').text, 'html.parser')
    button = soup.select_one('#e-e0 button[data-analyze]')
    assert button is not None and button['type'] == 'button'
    cid = canonical(rt, 'e0')
    data = client.post('/me/analyze', data={'canonical': cid}, headers=FETCH).json()
    body = json.loads(seen[0].content)
    assert seen[0].url.path == '/v1/responses'
    assert body['model'] == 'gpt-5.6-luna' and body['tools'] == [{'type': 'web_search'}]
    assert '分页新闻0' in json.dumps(body, ensure_ascii=False) and '最新进展' in json.dumps(body, ensure_ascii=False)
    assert data['model'] == 'gpt-5.6-luna' and data['searched'] is True and data['cached'] is False
    assert '**' not in data['text'] and '](' not in data['text'] and '美联储降息' in data['text']
    assert data['sources'] == [{'title': 'Fed', 'url': 'https://www.federalreserve.gov/a'}]
    again = client.post('/me/analyze', data={'canonical': cid}, headers=FETCH).json()
    assert again['cached'] is True and again['text'] == data['text'] and len(seen) == 1

    viewer = make_viewer(client)
    assert viewer.post('/me/analyze', data={'canonical': cid}, headers=FETCH).status_code == 200
    assert client.post('/me/analyze', data={'canonical': 'missing'}, headers=FETCH).status_code == 404


def test_admin_can_switch_analysis_off_and_viewer_cannot_change_it(tmp_path):
    rt, client = make_client(tmp_path)
    seed(rt, 2)
    rt.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=luna_response('【核心事实】x')))
    viewer = make_viewer(client)
    assert viewer.post('/admin/settings/analysis', data={'enabled': '0'}).status_code == 403
    settings_page = client.get('/admin/settings').text
    assert 'action="/admin/settings/analysis"' in settings_page and 'gpt-5.6-luna' in settings_page
    client.post('/admin/settings/analysis', data={})
    assert rt.store.get_setting('analysis_enabled') == '0'
    assert 'data-analyze' not in client.get('/').text and 'data-analyze' not in client.get('/favorites').text
    blocked = client.post('/me/analyze', data={'canonical': canonical(rt, 'e0')}, headers=FETCH)
    assert blocked.status_code == 403 and '关闭' in blocked.json()['detail']
    client.post('/admin/settings/analysis', data={'enabled': '1'})
    assert 'data-analyze' in client.get('/').text


def test_analysis_reports_upstream_failure_and_missing_search(tmp_path):
    rt, client = make_client(tmp_path)
    seed(rt, 2)
    rt.transport = httpx.MockTransport(lambda r: httpx.Response(500, text='upstream exploded'))
    failed = client.post('/me/analyze', data={'canonical': canonical(rt, 'e0')}, headers=FETCH)
    assert failed.status_code == 502 and 'HTTP 500' in failed.json()['detail']
    rt.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=luna_response('【核心事实】仅依据报道', searched=False)))
    ok = client.post('/me/analyze', data={'canonical': canonical(rt, 'e1')}, headers=FETCH).json()
    assert ok['searched'] is False and ok['sources'] == []


def test_analysis_limits_requests_per_user(tmp_path, monkeypatch):
    from news_admin import insight
    rt, client = make_client(tmp_path)
    seed(rt, 3)
    monkeypatch.setattr(insight, 'USER_LIMIT_PER_HOUR', 1)
    rt.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=luna_response('【核心事实】x')))
    assert client.post('/me/analyze', data={'canonical': canonical(rt, 'e0')}, headers=FETCH).status_code == 200
    # 已缓存的结果不计次数
    assert client.post('/me/analyze', data={'canonical': canonical(rt, 'e0')}, headers=FETCH).status_code == 200
    limited = client.post('/me/analyze', data={'canonical': canonical(rt, 'e1')}, headers=FETCH)
    assert limited.status_code == 429 and '稍后' in limited.json()['detail']
