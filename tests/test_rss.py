"""RSS 订阅：随机令牌地址、可重置/关闭、免登录、只输出最新 150 条。"""
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from news_admin import tz
from news_admin.models import EventRef
from news_admin.rss import RSS_LIMIT
from tests.test_feed import ev, save
from tests.test_web import make_client, make_viewer


def rss_url(client) -> str:
    field = BeautifulSoup(client.get('/admin/settings').text, 'html.parser').select_one('#rss-url')
    return field['value'] if field else ''


def path_of(url: str) -> str:
    return re.sub(r'^https?://[^/]+', '', url)


def test_rss_is_off_until_admin_generates_an_address_and_reset_invalidates_old_one(tmp_path):
    rt, client = make_client(tmp_path)
    save(rt.store, [ev('a', '美联储降息', tz.now())])
    guest = TestClient(client.app)
    assert rss_url(client) == ''
    assert guest.get('/rss/' + 'x' * 32 + '/rss.xml').status_code == 404

    client.post('/admin/settings/rss', data={'action': 'enable'})
    first = rss_url(client)
    assert re.fullmatch(r'http://testserver/rss/[A-Za-z0-9_-]{32}/rss\.xml', first)
    response = guest.get(path_of(first))  # 无登录可访问
    assert response.status_code == 200 and response.headers['content-type'].startswith('application/rss+xml')
    assert response.content.startswith(b"<?xml version='1.0' encoding='utf-8'?>")
    assert ET.fromstring(response.content).find('channel/item/title').text == '美联储降息'

    client.post('/admin/settings/rss', data={'action': 'reset'})
    second = rss_url(client)
    assert second and second != first
    assert guest.get(path_of(first)).status_code == 404
    assert guest.get(path_of(second)).status_code == 200

    client.post('/admin/settings/rss', data={'action': 'disable'})
    assert rss_url(client) == '' and guest.get(path_of(second)).status_code == 404
    # 其他路径不会因 /rss/ 前缀变成免登录
    assert guest.get('/rss/abc', follow_redirects=False).status_code == 303
    assert guest.post(path_of(second), follow_redirects=False).status_code in (303, 405)


def test_rss_uses_public_base_url_and_viewer_cannot_see_or_reset(tmp_path):
    rt, client = make_client(tmp_path)
    rt.apply_settings(rt.settings.model_copy(update={'public_base_url': 'https://news.example.com'}))
    client.post('/admin/settings/rss', data={'action': 'enable'})
    url = rss_url(client)
    assert url.startswith('https://news.example.com/rss/')
    token = rt.store.get_setting('rss_token')
    viewer = make_viewer(client)
    page = viewer.get('/admin/settings').text
    assert token not in page and '已开启' in page
    assert viewer.post('/admin/settings/rss', data={'action': 'reset'}).status_code == 403
    assert rt.store.get_setting('rss_token') == token
    channel = ET.fromstring(client.get(path_of(url)).content).find('channel')
    assert channel.find('link').text == 'https://news.example.com/'


def test_rss_has_latest_150_items_newest_first_with_latest_versions(tmp_path):
    rt, client = make_client(tmp_path)
    now = tz.now()
    r1 = save(rt.store, [ev(f'e{i}', f'新闻{i}', now - timedelta(minutes=i + 10)) for i in range(160)])
    ref = EventRef(run_id=r1, event_id='e5', headline='新闻5', relation='update')
    save(rt.store, [ev('u5', '新闻5的更新', now, previous=[ref], is_update=True, story='e5')])
    client.post('/admin/settings/rss', data={'action': 'enable'})
    channel = ET.fromstring(client.get(path_of(rss_url(client))).content).find('channel')
    items = channel.findall('item')
    assert len(items) == RSS_LIMIT
    titles = [i.find('title').text for i in items]
    assert titles[0] == '【更新】新闻5的更新' and '新闻5' not in titles  # 同一事件只保留最新版本
    assert titles[1:4] == ['新闻0', '新闻1', '新闻2'] and '新闻149' in titles and '新闻150' not in titles
    dates = [parsedate_to_datetime(i.find('pubDate').text) for i in items]
    assert dates == sorted(dates, reverse=True)
    first = items[0]
    assert first.find('link').text == 'https://x/u5/0'
    assert first.find('guid').get('isPermaLink') == 'false' and first.find('guid').text
    assert '来源：BBC' in first.find('description').text and '评级' in first.find('description').text
    assert len({i.find('guid').text for i in items}) == RSS_LIMIT


def test_rss_strips_xml_invalid_characters(tmp_path):
    rt, client = make_client(tmp_path)
    save(rt.store, [ev('a', '标题\x01含控制符', tz.now())])
    client.post('/admin/settings/rss', data={'action': 'enable'})
    item = ET.fromstring(client.get(path_of(rss_url(client))).content).find('channel/item')
    assert item.find('title').text == '标题含控制符'
