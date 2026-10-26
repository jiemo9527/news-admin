"""Browser regressions using isolated test data, without a server or scheduler."""
from datetime import timedelta
import time
from urllib.parse import urlsplit

import pytest

from news_admin import tz
from tests.test_feed import ev, save
from tests.test_web import make_client


@pytest.fixture(scope='module')
def chrome():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel='chrome', headless=True)
        except playwright.Error as exc:
            if 'not found' in str(exc) or "doesn't exist" in str(exc):
                pytest.skip('Installed Chrome is required for browser regressions')
            raise
        yield browser
        browser.close()


@pytest.mark.parametrize('width,height', [
    (1920, 1080), (1366, 900), (1366, 768), (768, 1024), (390, 844), (360, 800),
])
def test_pagination_returns_to_first_news_after_refresh(tmp_path, chrome, width, height):
    rt, client = make_client(tmp_path)
    now = tz.now()
    save(rt.store, [ev(f'e{i}', f'分页测试新闻{i}', now - timedelta(minutes=i)) for i in range(45)])
    page = chrome.new_page(viewport={'width': width, 'height': height})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def serve(route):
        url = urlsplit(route.request.url)
        if url.query and url.path == '/':
            # The old smooth scroll finishes before this delayed replacement.
            time.sleep(0.8)
        response = client.get(url.path + (f'?{url.query}' if url.query else ''))
        route.fulfill(status=response.status_code, body=response.content,
                      content_type=response.headers.get('content-type', 'text/html'))

    page.route('http://testserver/**', serve)
    try:
        page.goto('http://testserver/')
        # Next page, short final page, previous page, then a numbered link.
        for selector, expected in [
            ('nav[aria-label="分页"] a[rel="next"]', 'e-e20'),
            ('nav[aria-label="分页"] a[rel="next"]', 'e-e40'),
            ('nav[aria-label="分页"] a[rel="prev"]', 'e-e20'),
            ('nav[aria-label="分页"] .pager-pages a[href="/"]', 'e-e0'),
            # 顶部分页同样可用
            ('nav.pager-top a[rel="next"]', 'e-e20'),
        ]:
            page.locator(selector).click()
            page.wait_for_function("id => document.querySelector('.news-card')?.id === id", arg=expected)
            page.wait_for_function("""() => {
                const card = document.querySelector('.news-card').getBoundingClientRect();
                const header = document.querySelector('.topbar').getBoundingClientRect();
                const filters = document.querySelector('[data-live-form]').getBoundingClientRect();
                const covered = Math.max(header.bottom, filters.bottom);
                return card.top >= covered && card.top < window.innerHeight - 40;
            }""", timeout=4000)
        assert not errors
    finally:
        page.close()
        client.close()


@pytest.mark.parametrize('width,height', [(1366, 768), (390, 844)])
def test_per_page_switch_and_analysis_popover(tmp_path, chrome, width, height):
    import httpx
    rt, client = make_client(tmp_path)
    now = tz.now()
    save(rt.store, [ev(f'e{i}', f'分页测试新闻{i}', now - timedelta(minutes=i)) for i in range(45)])
    calls = []

    def luna(request):
        calls.append(request)
        time.sleep(0.4)
        return httpx.Response(200, json={'output': [
            {'type': 'web_search_call', 'status': 'completed'},
            {'type': 'message', 'content': [{'type': 'output_text', 'text': '【核心事实】美联储降息。<img src=x onerror=alert(1)>\n【最新进展】2026-10-26 暂无更新。',
                                             'annotations': [{'type': 'url_citation', 'url': 'https://www.federalreserve.gov/a', 'title': 'Fed'}]}]}]})

    rt.transport = httpx.MockTransport(luna)
    page = chrome.new_page(viewport={'width': width, 'height': height})
    errors, dialogs = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda d: (dialogs.append(d.message), d.dismiss()))

    def serve(route):
        url = urlsplit(route.request.url)
        method = route.request.method
        target = url.path + (f'?{url.query}' if url.query else '')
        if method == 'POST':
            response = client.post(target, content=route.request.post_data_buffer,
                                   headers={k: v for k, v in route.request.headers.items() if k.lower() in ('content-type', 'x-requested-with')})
        else:
            response = client.get(target)
        route.fulfill(status=response.status_code, body=response.content,
                      content_type=response.headers.get('content-type', 'text/html'))

    page.route('http://testserver/**', serve)
    try:
        page.goto('http://testserver/')
        top = page.locator('nav.pager-top')
        assert top.is_visible() and page.locator('nav.pager-top .per-page a').all_inner_texts() == ['10', '20', '50']
        top.locator('.per-page a', has_text='50').click()
        page.wait_for_function("() => document.querySelectorAll('.news-card').length === 45")
        assert 'per=50' in page.url and page.locator('input[name="per"]').input_value() == '50'
        assert page.locator('nav.pager-top a[rel="next"]').count() == 0
        page.reload()
        assert page.locator('.news-card').count() == 45  # 刷新后保持
        page.locator('nav.pager-top .per-page a', has_text='10').click()
        page.wait_for_function("() => document.querySelectorAll('.news-card').length === 10")
        # 输入搜索后仍保持每页条数
        page.fill('input[name="q"]', '分页测试')
        page.wait_for_function("() => location.search.includes('q=') && document.querySelectorAll('.news-card').length === 10")

        page.evaluate("""() => { window.__sawLoading = false; new MutationObserver(() => {
            if (document.querySelector('.insight-pop .insight-loading')) window.__sawLoading = true;
        }).observe(document.body, { childList: true, subtree: true }); }""")
        page.locator('#e-e0 [data-analyze]').click()
        pop = page.locator('.insight-pop')
        pop.wait_for()
        page.wait_for_selector('.insight-pop .insight-section h3')
        assert page.evaluate('window.__sawLoading')
        text = pop.inner_text()
        assert '核心事实' in text and '最新进展' in text and 'gpt-5.6-luna' in text and '已联网检索' in text
        assert pop.locator('img').count() == 0 and '<img' in text  # 模型输出只当文本显示
        link = pop.locator('.insight-sources a')
        assert link.get_attribute('href') == 'https://www.federalreserve.gov/a' and link.get_attribute('rel') == 'noopener noreferrer'
        box, vw, vh = pop.bounding_box(), width, height
        assert box['x'] >= 0 and box['x'] + box['width'] <= vw + 1 and box['y'] >= 0 and box['y'] + box['height'] <= vh + 1
        if width <= 760:
            assert abs(box['y'] + box['height'] - vh) <= 1 and box['width'] >= vw - 1
        else:
            assert box['y'] >= 56  # 不遮挡顶部导航
        assert not dialogs  # 点按钮不触发长按「至此已读」
        page.keyboard.press('Escape')
        assert pop.count() == 0
        page.locator('#e-e0 [data-analyze]').click()
        page.wait_for_selector('.insight-pop .insight-section h3')
        assert 'cached' not in page.locator('.insight-pop').inner_text() and len(calls) == 1
        page.mouse.click(5, height - 5 if width > 760 else 60)
        assert page.locator('.insight-pop').count() == 0
        assert not errors
    finally:
        page.close()
        client.close()
